# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2024 John Balis
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
"""Deep web research orchestrator (breadth/depth loop ported from gpt-researcher).

Invoked from web_research when the sidebar passes deep=True; each sub-query
delegates to the existing shallow web ReAct sub-agent (_run_web_agent).
"""

from __future__ import annotations

import logging
import queue
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from plugin.framework.config_schema import as_bool
from plugin.framework.constants import now_aware

from plugin.framework.errors import ToolExecutionError, format_error_payload
from plugin.framework.i18n import _
from plugin.framework.json_utils import safe_json_loads
from plugin.framework.worker_pool import run_in_background
from plugin.chatbot.web_research import _norm_research_query

log = logging.getLogger("writeragent.web_research_deep")

MAX_CONTEXT_WORDS = 25000
# Stop joins the pool. A worker that sees the captured checker returns and
# the join finishes. One stuck in HTTP must not hold the send forever.
_STOP_POOL_JOIN_SEC = 30.0

JSON_BLOCK_PATTERNS = [
    re.compile(r"```(?:json)?\s*(?P<payload>[\s\S]*?)```", re.IGNORECASE),
    re.compile(r"(?P<payload>\[[\s\S]*\])"),
    re.compile(r"(?P<payload>\{[\s\S]*\})"),
]

QUERY_LINE_PATTERN = re.compile(r"^(?:[-*]|\d+[.)])?\s*Query:\s*(?P<query>.+)$", re.IGNORECASE)
GOAL_LINE_PATTERN = re.compile(r"^(?:[-*]|\d+[.)])?\s*(?:Goal|Research Goal):\s*(?P<goal>.+)$", re.IGNORECASE)
QUESTION_LINE_PATTERN = re.compile(r"^(?:[-*]|\d+[.)])?\s*(?:Question:\s*)?(?P<question>.+\?)$", re.IGNORECASE)
LEARNING_LINE_PATTERN = re.compile(
    r"^(?:[-*]|\d+[.)])?\s*Learning(?:\s*\[(?P<citation>[^\]]+)\])?:\s*(?P<learning>.+)$",
    re.IGNORECASE,
)
URL_PATTERN = re.compile(r"https?://[^\s\]\)>\",;]+")

WebAgentRunner = Callable[[str, str, str | None], str | dict[str, Any]]
LlmChatFn = Callable[[list[dict[str, str]], int], str]
# Each pool task returns its own (run_web_agent, extraction llm_chat). The
# parent llm_chat stays on the planning thread and is not safe to share.
WorkerFactory = Callable[[], tuple[WebAgentRunner, LlmChatFn, Callable[[], None]]]
StopChecker = Callable[[], bool] | None
StatusCallback = Callable[[str], None] | None


def _unfenced_lines(response: str) -> list[str]:
    return response.replace("```json", "").replace("```", "").splitlines()


def _extract_json_payloads(response: str) -> list[str]:
    candidates: list[str] = []
    seen: set[str] = set()
    for pattern in JSON_BLOCK_PATTERNS:
        for match in pattern.finditer(response):
            candidate = match.group("payload").strip()
            if candidate and candidate not in seen:
                candidates.append(candidate)
                seen.add(candidate)
    return candidates


def _load_repaired_json(response: str) -> Any:
    for candidate in [response.strip(), *_extract_json_payloads(response)]:
        if not candidate:
            continue
        parsed = safe_json_loads(candidate, default=None)
        if parsed is not None:
            return parsed
    return None


def parse_search_queries_response(response: str, num_queries: int) -> list[dict[str, str]]:
    parsed = _load_repaired_json(response)
    candidate_queries = parsed
    if isinstance(parsed, dict):
        candidate_queries = parsed.get("queries") or parsed.get("searchQueries") or parsed.get("items")

    if isinstance(candidate_queries, list):
        parsed_queries = []
        for item in candidate_queries:
            if isinstance(item, dict):
                q = str(item.get("query") or "").strip()
                g = str(item.get("researchGoal") or "").strip() or f"Research: {q}"
                if q:
                    parsed_queries.append({"query": q, "researchGoal": g})
            elif isinstance(item, str) and item.strip():
                q = item.strip()
                parsed_queries.append({"query": q, "researchGoal": f"Research: {q}"})
        if parsed_queries:
            return parsed_queries[:num_queries]

    line_queries: list[dict[str, str]] = []
    current_query: dict[str, str] = {}
    for raw_line in _unfenced_lines(response):
        line = raw_line.strip()
        if not line:
            continue
        query_match = QUERY_LINE_PATTERN.match(line)
        goal_match = GOAL_LINE_PATTERN.match(line)
        if query_match:
            if current_query.get("query") and current_query.get("researchGoal"):
                line_queries.append(current_query)
            current_query = {"query": query_match.group("query").strip()}
        elif goal_match and current_query.get("query"):
            current_query["researchGoal"] = goal_match.group("goal").strip()
    if current_query.get("query") and current_query.get("researchGoal"):
        line_queries.append(current_query)
    return line_queries[:num_queries]


def parse_research_results_response(response: str, num_learnings: int) -> dict[str, Any]:
    parsed = _load_repaired_json(response)

    if isinstance(parsed, dict):
        learnings_payload = parsed.get("learnings", [])
        follow_up_payload = parsed.get("followUpQuestions") or parsed.get("questions") or []
        learnings: list[str] = []
        citations: dict[str, list[str]] = {}
        if isinstance(learnings_payload, list):
            for item in learnings_payload:
                if isinstance(item, dict):
                    learning = str(item.get("insight") or item.get("learning") or "").strip()
                    citation = str(item.get("sourceUrl") or item.get("citation") or "").strip()
                else:
                    learning = str(item).strip()
                    citation = ""
                if learning:
                    learnings.append(learning)
                    if citation:
                        citations.setdefault(learning, []).append(citation)
        # A bare string used to be iterated here. ``"What about beta?"`` became
        # one fake question per character (a dict became its keys). Sibling
        # parsers already require a list before they walk the payload.
        questions = (
            [str(item).strip() for item in follow_up_payload if str(item).strip()]
            if isinstance(follow_up_payload, list)
            else []
        )
        if learnings or questions:
            return {
                "learnings": learnings[:num_learnings],
                "followUpQuestions": questions[:num_learnings],
                "citations": citations,
            }

    line_learnings: list[str] = []
    line_questions: list[str] = []
    line_citations: dict[str, list[str]] = {}
    for raw_line in _unfenced_lines(response):
        line = raw_line.strip()
        if not line:
            continue
        learning_match = LEARNING_LINE_PATTERN.match(line)
        question_match = QUESTION_LINE_PATTERN.match(line)
        if learning_match:
            learning = learning_match.group("learning").strip()
            citation = (learning_match.group("citation") or "").strip()
            if not citation:
                url_match = URL_PATTERN.search(learning)
                if url_match:
                    citation = url_match.group(0)
                    learning = learning.replace(citation, "").strip(" -")
            if learning:
                line_learnings.append(learning)
                if citation:
                    line_citations.setdefault(learning, []).append(citation)
        elif question_match:
            line_questions.append(question_match.group("question").strip())
    return {
        "learnings": line_learnings[:num_learnings],
        "followUpQuestions": line_questions[:num_learnings],
        "citations": line_citations,
    }


def count_words(text: Any) -> int:
    if isinstance(text, list):
        text = " ".join(str(item) for item in text)
    return len(str(text).split())


def _trim_evidence(
    learnings: list[str],
    context_chunks: list[str],
    max_words: int = MAX_CONTEXT_WORDS,
) -> tuple[list[str], list[str]]:
    total_words = 0
    trimmed_learnings: list[str] = []
    for item in learnings:
        words = count_words(item)
        if total_words + words <= max_words:
            trimmed_learnings.append(item)
            total_words += words
        else:
            if total_words < max_words:
                text = " ".join(str(part) for part in item) if isinstance(item, list) else str(item)
                rem = max_words - total_words
                trimmed_learnings.append(" ".join(text.split()[:rem]))
                total_words += rem
            break

    trimmed_context: list[str] = []
    for item in reversed(context_chunks):
        words = count_words(item)
        if total_words + words <= max_words:
            trimmed_context.insert(0, item)
            total_words += words
        elif not trimmed_context and total_words < max_words:
            text = " ".join(str(part) for part in item) if isinstance(item, list) else str(item)
            rem = max_words - total_words
            trimmed_context.insert(0, " ".join(text.split()[:rem]))
            break
        else:
            break

    return trimmed_learnings, trimmed_context


def _user_stopped_payload() -> dict[str, Any]:
    return format_error_payload(ToolExecutionError("Web search stopped by user.", code="USER_STOPPED"))


def _check_stopped(stop_checker: StopChecker) -> dict[str, Any] | None:
    if stop_checker and stop_checker():
        return _user_stopped_payload()
    return None


@dataclass
class ResearchProgress:
    """Tracks deep-research progress for sidebar status updates."""

    current_round: int = 1
    max_rounds: int = 1
    started_queries: int = 0
    max_sub_queries: int = 14
    current_query: str | None = None

    def status_text(self) -> str:
        q = (self.current_query or "")[:50]
        return (
            _("Deep research round {round}/{rounds}, query {started}/{max}").format(
                round=self.current_round,
                rounds=self.max_rounds,
                started=self.started_queries,
                max=self.max_sub_queries,
            )
            + (f": {q}..." if q else "")
        )


@dataclass
class _ResearchAccumulator:
    learnings: list[str] = field(default_factory=list)
    citations: dict[str, list[str]] = field(default_factory=dict)
    context_chunks: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    started_queries: int = 0
    budget_lock: threading.Lock = field(default_factory=threading.Lock)
    last_branch_error: dict[str, Any] | None = None


def _emit_progress(
    progress: ResearchProgress,
    status_callback: StatusCallback,
) -> None:
    if status_callback:
        status_callback(progress.status_text())


def parse_assessment_response(response: str) -> dict[str, Any]:
    parsed = _load_repaired_json(response)
    if isinstance(parsed, dict):
        score_raw = parsed.get("score")
        score = 0.0
        if score_raw is not None:
            try:
                score = float(score_raw)
            except (TypeError, ValueError):
                score = 0.0
        gaps_raw = parsed.get("knowledge_gaps") or parsed.get("gaps") or []
        queries_raw = parsed.get("suggested_queries") or parsed.get("queries") or []
        gaps = [str(g).strip() for g in gaps_raw if str(g).strip()] if isinstance(gaps_raw, list) else []
        queries = [str(q).strip() for q in queries_raw if str(q).strip()] if isinstance(queries_raw, list) else []
        stop_flag = parsed.get("stop")
        # ``bool("false")``, ``bool("0")``, and ``bool("no")`` are True, so a
        # stringified flag ended the research loop early. `as_bool` correctly
        # handles standard boolean token strings. Parse tokens the way score
        # is coerced above: only a real boolean counts, and bad input falls
        # back to "do not stop".
        stop = as_bool(stop_flag) if stop_flag is not None else False
        return {
            "score": score,
            "knowledge_gaps": gaps,
            "suggested_queries": queries,
            "stop": stop,
            "reasoning": str(parsed.get("reasoning") or ""),
        }
    return {"score": 0.0, "knowledge_gaps": [], "suggested_queries": [], "stop": False, "reasoning": ""}


def _format_cited_learnings(learnings: list[str], citations: dict[str, list[str]]) -> list[str]:
    """Attach source URLs to each learning (assessment and synthesis share the format)."""
    return [
        f"{learning} [Sources: {', '.join(citations[learning])}]"
        if citations.get(learning)
        else learning
        for learning in learnings
    ]


def assess_research_coverage(
    llm_chat: LlmChatFn,
    original_query: str,
    learnings: list[str],
    citations: dict[str, list[str]],
    *,
    quality_threshold: int,
) -> dict[str, Any]:
    unique_learnings = list(dict.fromkeys(learnings))
    cited = _format_cited_learnings(unique_learnings[-40:], citations)
    evidence = "\n".join(cited) or "(No learnings yet.)"
    messages = [
        {
            "role": "system",
            "content": (
                "You evaluate whether web research sufficiently answers the user's question. "
                "Return valid JSON only."
            ),
        },
        {
            "role": "user",
            "content": (
                f"Original question:\n{original_query}\n\n"
                f"Collected learnings:\n{evidence}\n\n"
                f"Quality threshold for stopping: {quality_threshold}/10.\n\n"
                "Return ONLY a JSON object:\n"
                '{"score": <1-10>, "knowledge_gaps": ["<gap>"], '
                '"suggested_queries": ["<follow-up search query>"], "stop": <true|false>, '
                '"reasoning": "<brief explanation>"}\n'
                "Set stop true when score >= threshold and gaps are empty or minor."
            ),
        },
    ]
    response = llm_chat(messages, 1200)
    return parse_assessment_response(response)


def _local_clock_stamp() -> str:
    """Weekday + ISO date like LlmClient's date line, plus local clock."""
    return now_aware().strftime("%A, %Y-%m-%d %H:%M:%S")


def generate_search_queries(llm_chat: LlmChatFn, query: str, num_queries: int) -> list[dict[str, str]]:
    current_time = _local_clock_stamp()
    messages = [
        {
            "role": "system",
            "content": (
                "You are an expert researcher generating search queries. "
                "Return valid JSON only. Do not include markdown, code fences, bullets, numbering, or prose."
            ),
        },
        {
            "role": "user",
            "content": (
                f"Given the following prompt, generate {num_queries} unique search queries to research the topic thoroughly. "
                "For each query, provide a research goal.\n\n"
                "Each query should cover a different aspect, time period, or perspective of the topic.\n\n"
                'Return ONLY a JSON array of objects using this exact schema:\n'
                '[{"query": "<search query>", "researchGoal": "<research goal>"}]\n\n'
                f"Current time: {current_time}. Prefer recent information; where the topic is fast-changing, "
                "word the queries to surface up-to-date results.\n\n"
                f"Prompt: {query}"
            ),
        },
    ]
    response = llm_chat(messages, 1500)
    return parse_search_queries_response(response, num_queries)


def process_research_results(llm_chat: LlmChatFn, query: str, context: str, num_learnings: int = 3) -> dict[str, Any]:
    messages = [
        {
            "role": "system",
            "content": "You are an expert researcher analyzing search results. Return valid JSON only.",
        },
        {
            "role": "user",
            "content": (
                f"Given the following research results for the query '{query}', extract key learnings and suggest "
                "follow-up questions. For each learning, include a citation to the source URL if available.\n\n"
                "Return ONLY a JSON object using this exact schema:\n"
                '{"learnings": [{"insight": "<insight>", "sourceUrl": "<url or empty string>"}], '
                '"followUpQuestions": ["<question 1>", "<question 2>"]}\n\n'
                f"Research results:\n{context}"
            ),
        },
    ]
    response = llm_chat(messages, 1000)
    return parse_research_results_response(response, num_learnings)


def synthesize_deep_report(
    llm_chat: LlmChatFn,
    query: str,
    learnings: list[str],
    context_chunks: list[str],
    plain_text_format: str,
    *,
    sources: list[str] | None = None,
) -> str:
    trimmed_learnings, trimmed_chunks = _trim_evidence(learnings, context_chunks)
    evidence_parts = list(trimmed_learnings)
    evidence_parts.extend(trimmed_chunks)
    evidence = "\n\n".join(evidence_parts)
    source_block = ""
    if sources:
        unique_sources = list(dict.fromkeys(s for s in sources if s))
        if unique_sources:
            source_block = "\n\nSources consulted:\n" + "\n".join(f"- {u}" for u in unique_sources[:50])
    messages = [
        {
            "role": "system",
            "content": (
                "You are an expert research writer. Synthesize the collected evidence into one comprehensive "
                "plain-text research report. Use only the provided evidence; cite sources inline when URLs appear."
            ),
        },
        {
            "role": "user",
            "content": (
                f"Original research request:\n{query}\n\n"
                f"Collected evidence:\n{evidence}{source_block}\n\n"
                f"{plain_text_format}\n"
                "Write the full report as plain text (no markdown code fences)."
            ),
        },
    ]
    return llm_chat(messages, 4096)


def _partial_report_from_evidence(
    query: str,
    cited_learnings: list[str],
    sources: list[str] | None,
) -> str:
    """Plain-text fallback when the synthesis LLM call fails."""
    lines = [
        _("Research notes (automatic synthesis failed) for: {query}").format(query=query),
        "",
    ]
    lines.extend(cited_learnings)
    unique_sources = list(dict.fromkeys(s for s in (sources or []) if s))
    if unique_sources:
        lines.append("")
        lines.append(_("Sources consulted:"))
        lines.extend(f"- {url}" for url in unique_sources[:50])
    return "\n".join(lines)


def _coerce_agent_result(result: str | dict[str, Any]) -> str:
    if result is None:
        return ""
    if isinstance(result, dict):
        if result.get("status") == "error":
            raise ToolExecutionError(
                str(result.get("message") or "Sub-query research failed."),
                code=str(result.get("code") or "TOOL_EXECUTION_ERROR"),
            )
        if result.get("status") == "ok":
            val = result.get("result")
            return str(val) if val is not None else ""
        if "result" in result:
            val = result.get("result")
            return str(val) if val is not None else ""
        raise ToolExecutionError(str(result.get("message") or "Sub-query research failed."))
    return str(result)


def _extract_urls_from_text(text: str) -> list[str]:
    return list(dict.fromkeys(URL_PATTERN.findall(text or "")))


def _merge_branch_results(acc: _ResearchAccumulator, branch: dict[str, Any]) -> None:
    acc.learnings.extend(branch.get("learnings") or [])
    for learning, urls in (branch.get("citations") or {}).items():
        acc.citations.setdefault(learning, []).extend(urls)
    ctx = branch.get("context")
    if ctx:
        acc.context_chunks.append(str(ctx))
    for url in branch.get("sources") or []:
        if url and url not in acc.sources:
            acc.sources.append(url)


def _process_one_sub_query(
    serp_query: dict[str, str],
    *,
    run_web_agent: WebAgentRunner,
    llm_chat: LlmChatFn,
    stop_checker: StopChecker,
    acc: _ResearchAccumulator,
    max_sub_queries: int,
    progress: ResearchProgress,
    status_callback: StatusCallback,
) -> dict[str, Any] | None:
    stopped = _check_stopped(stop_checker)
    if stopped is not None:
        return {"error": stopped}

    sub_query = serp_query["query"]
    research_goal = serp_query.get("researchGoal") or ""

    with acc.budget_lock:
        if acc.started_queries >= max_sub_queries:
            return None
        acc.started_queries += 1
        progress.started_queries = acc.started_queries
        progress.current_query = sub_query

    _emit_progress(progress, status_callback)

    try:
        raw = run_web_agent(sub_query, research_goal, None)
        sub_context = _coerce_agent_result(raw)
    except ToolExecutionError as exc:
        payload = format_error_payload(exc)
        if getattr(exc, "code", None) == "USER_STOPPED":
            return {"error": payload}
        log.warning("deep_research: sub-query failed (%s): %s", sub_query, exc)
        acc.last_branch_error = payload
        return None

    # The agent returned. Stop during that call must not start extraction,
    # or the pool join waits on another HTTP request.
    stopped = _check_stopped(stop_checker)
    if stopped is not None:
        return {"error": stopped}

    try:
        results = process_research_results(llm_chat, sub_query, sub_context)
    except Exception as exc:
        if getattr(exc, "code", None) == "USER_STOPPED":
            raise
        log.warning("deep_research: extraction failed for %s: %s", sub_query, exc)
        results = {}

    sources = _extract_urls_from_text(sub_context)
    # parse_research_results_response stores {learning sentence: list[source url]}.
    citation_map = results.get("citations") or {}
    citation_urls = []
    if isinstance(citation_map, dict):
        for urls in citation_map.values():
            citation_urls.extend(urls)
    else:
        citation_urls = citation_map

    for url in citation_urls:
        if url and url not in sources:
            sources.append(url)

    return {
        "learnings": results.get("learnings") or [],
        "citations": results.get("citations") or {},
        "context": sub_context,
        "sources": sources,
        "followUpQuestions": results.get("followUpQuestions") or [],
        "researchGoal": research_goal,
    }


def _run_sub_queries_parallel(
    serp_queries: list[dict[str, str]],
    *,
    run_web_agent: WebAgentRunner,
    llm_chat: LlmChatFn,
    stop_checker: StopChecker,
    acc: _ResearchAccumulator,
    max_sub_queries: int,
    concurrency: int,
    progress: ResearchProgress,
    status_callback: StatusCallback,
    worker_factory: WorkerFactory | None = None,
) -> dict[str, Any] | None:
    if not serp_queries:
        return None

    workers = max(1, min(concurrency, len(serp_queries)))
    work_q: queue.Queue[dict[str, str] | None] = queue.Queue()
    result_q: queue.Queue[tuple[dict[str, str], Any]] = queue.Queue()

    for sq in serp_queries:
        work_q.put(sq)

    def _worker_fn() -> None:
        # worker_factory builds a fresh LlmClient + extraction chat on this
        # thread. Falling back to the shared callables is for unit tests that
        # do not open HTTP; production always passes a factory.
        if worker_factory is not None:
            task_run_web_agent, task_llm_chat, worker_cleanup = worker_factory()
        else:
            task_run_web_agent, task_llm_chat, worker_cleanup = run_web_agent, llm_chat, None
        try:
            while True:
                sq = work_q.get()
                if sq is None:
                    break
                # Stop can land after submit and before this body. Return without
                # building a client so the pool join is not stuck in HTTP.
                stopped = _check_stopped(stop_checker)
                if stopped is not None:
                    result_q.put((sq, {"error": stopped}))
                    continue
                try:
                    branch = _process_one_sub_query(
                        sq,
                        run_web_agent=task_run_web_agent,
                        llm_chat=task_llm_chat,
                        stop_checker=stop_checker,
                        acc=acc,
                        max_sub_queries=max_sub_queries,
                        progress=progress,
                        status_callback=status_callback,
                    )
                    result_q.put((sq, branch))
                except Exception as exc:
                    result_q.put((sq, exc))
        finally:
            if worker_cleanup is not None:
                worker_cleanup()

    handles = [
        run_in_background(_worker_fn, name=f"deep-research-{idx}", dedicated=True)
        for idx in range(workers)
    ]

    error_payload: dict[str, Any] | None = None
    completed = 0
    total = len(serp_queries)

    try:
        while completed < total:
            stopped = _check_stopped(stop_checker)
            if stopped is not None:
                error_payload = stopped
                return error_payload
            try:
                sq, res = result_q.get(timeout=0.25)
            except queue.Empty:
                continue

            completed += 1
            if isinstance(res, Exception):
                if getattr(res, "code", None) == "USER_STOPPED":
                    # process_research_results raises USER_STOPPED. Do not
                    # store it on the accumulator and continue sibling queries.
                    raise res
                log.warning("deep_research: parallel sub-query error (%s): %s", sq.get("query"), res)
                acc.last_branch_error = format_error_payload(res)
                continue

            branch = res
            if isinstance(branch, dict) and branch.get("error"):
                error_payload = branch["error"]
                if isinstance(error_payload, dict) and error_payload.get("code") == "USER_STOPPED":
                    return error_payload
                # Emulate the `break` from original `as_completed` when an error branch is found
                break
            if branch:
                _merge_branch_results(acc, branch)
        return error_payload
    finally:
        # Cancel unstarted work on Stop, then join the pool.
        while not work_q.empty():
            try:
                work_q.get_nowait()
            except queue.Empty:
                break
        for _ in range(workers):
            work_q.put(None)

        # No public join timeout. The worker set is what shutdown(wait=True) joins.
        deadline = time.monotonic() + _STOP_POOL_JOIN_SEC
        for handle in handles:
            remaining = deadline - time.monotonic()
            handle.join(timeout=0.0 if remaining <= 0 else remaining)
        stuck = [h for h in handles if h.is_alive()]
        if stuck:
            log.warning(
                "deep_research: %d pool worker(s) still in HTTP after %.0fs Stop join",
                len(stuck),
                _STOP_POOL_JOIN_SEC,
            )


def _serp_from_suggested_queries(queries: list[str]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for q in queries:
        text = str(q).strip()
        if text:
            out.append({"query": text, "researchGoal": f"Fill knowledge gap: {text}"})
    return out


def _run_adaptive_research_loop(
    combined_query: str,
    original_query: str,
    *,
    llm_chat: LlmChatFn,
    run_web_agent: WebAgentRunner,
    stop_checker: StopChecker,
    status_callback: StatusCallback,
    breadth: int,
    max_rounds: int,
    concurrency: int,
    max_sub_queries: int,
    quality_threshold: int,
    worker_factory: WorkerFactory | None = None,
) -> dict[str, Any]:
    acc = _ResearchAccumulator()
    progress = ResearchProgress(max_rounds=max_rounds, max_sub_queries=max_sub_queries)
    next_gap_queries: list[str] = []
    seen: set[str] = set()

    for round_num in range(1, max_rounds + 1):
        stopped = _check_stopped(stop_checker)
        if stopped is not None:
            return {"error": stopped}

        progress.current_round = round_num
        _emit_progress(progress, status_callback)

        if round_num == 1:
            if status_callback:
                status_callback(_("Planning {count} research queries...").format(count=breadth))
            raw_queries = generate_search_queries(llm_chat, combined_query, num_queries=breadth)
        else:
            raw_queries = _serp_from_suggested_queries(next_gap_queries[:breadth])

        serp_queries = []
        for sq in raw_queries:
            q_norm = _norm_research_query(sq.get("query", ""))
            if q_norm and q_norm not in seen:
                seen.add(q_norm)
                serp_queries.append(sq)

        if not serp_queries:
            log.warning("deep_research: no search queries for round %s", round_num)
            break

        remaining = max_sub_queries - acc.started_queries
        if remaining <= 0:
            break
        serp_queries = serp_queries[: min(len(serp_queries), breadth, remaining)]

        learnings_before = len(acc.learnings)
        chunks_before = len(acc.context_chunks)

        loop_error = _run_sub_queries_parallel(
            serp_queries,
            run_web_agent=run_web_agent,
            llm_chat=llm_chat,
            stop_checker=stop_checker,
            acc=acc,
            max_sub_queries=max_sub_queries,
            concurrency=concurrency,
            progress=progress,
            status_callback=status_callback,
            worker_factory=worker_factory,
        )
        if loop_error is not None:
            return {"error": loop_error}

        branches_merged = (len(acc.learnings) > learnings_before) or (len(acc.context_chunks) > chunks_before)
        if not branches_merged:
            if not acc.learnings and not acc.context_chunks:
                return {"error": acc.last_branch_error or format_error_payload(ToolExecutionError("All research sub-queries failed."))}
            break

        if acc.started_queries >= max_sub_queries:
            break

        if round_num < max_rounds:
            if status_callback:
                status_callback(_("Deep research: assessing coverage..."))
            assessment = assess_research_coverage(
                llm_chat,
                original_query,
                acc.learnings,
                acc.citations,
                quality_threshold=quality_threshold,
            )
            score = assessment.get("score") or 0.0
            gaps = assessment.get("knowledge_gaps") or []
            next_gap_queries = assessment.get("suggested_queries") or gaps

            if assessment.get("stop") or score >= quality_threshold or not next_gap_queries:
                log.info("deep_research: stopping after round %s (score=%s)", round_num, score)
                break
        else:
            log.info("deep_research: stopping after round %s (reached max rounds %s)", round_num, max_rounds)

    unique_learnings = list(dict.fromkeys(acc.learnings))
    return {
        "learnings": unique_learnings,
        "citations": acc.citations,
        "context": acc.context_chunks,
        "sources": list(dict.fromkeys(acc.sources)),
        "branch_error": acc.last_branch_error,
    }


def run_deep_research(
    query: str,
    *,
    llm_chat: LlmChatFn,
    run_web_agent: WebAgentRunner,
    stop_checker: StopChecker,
    status_callback: StatusCallback,
    breadth: int,
    plain_text_format: str,
    initial_search_snippet: str = "",
    max_rounds: int = 3,
    concurrency: int = 2,
    max_sub_queries: int = 14,
    quality_threshold: int = 7,
    worker_factory: WorkerFactory | None = None,
) -> str | dict[str, Any]:
    """Run adaptive multi-round deep research; returns report string or error payload dict."""
    stopped = _check_stopped(stop_checker)
    if stopped is not None:
        return stopped

    try:
        search_snippet = initial_search_snippet.strip() or "(No preview search results; proceed from the query alone.)"
        combined_query = f"Initial Query: {query}\n"
        combined_query += f"Preview search results:\n{search_snippet}\n"

        loop_result = _run_adaptive_research_loop(
            combined_query,
            query,
            llm_chat=llm_chat,
            run_web_agent=run_web_agent,
            stop_checker=stop_checker,
            status_callback=status_callback,
            breadth=breadth,
            max_rounds=max_rounds,
            concurrency=concurrency,
            max_sub_queries=max_sub_queries,
            quality_threshold=quality_threshold,
            worker_factory=worker_factory,
        )
        if loop_result.get("error"):
            return loop_result["error"]

        learnings = loop_result.get("learnings") or []
        context_chunks = loop_result.get("context") or []
        citations = loop_result.get("citations") or {}
        sources = loop_result.get("sources") or []

        if not learnings and not context_chunks:
            # A failed extraction drops only the extracted learnings; the sub-agent
            # report in ``context`` is still evidence. Error only when both are empty,
            # or the run would synthesize from nothing.
            err = loop_result.get("branch_error")
            if isinstance(err, dict) and err.get("status") == "error":
                return err
            return format_error_payload(
                ToolExecutionError(_("Web research collected no usable evidence."))
            )

        cited_learnings = _format_cited_learnings(learnings, citations)

        if status_callback:
            status_callback(_("Deep research: synthesizing report..."))

        stopped = _check_stopped(stop_checker)
        if stopped is not None:
            return stopped

        try:
            return synthesize_deep_report(
                llm_chat,
                query,
                cited_learnings,
                context_chunks,
                plain_text_format,
                sources=sources,
            )
        except Exception as exc:
            # A timeout here used to discard the whole run; return the raw notes.
            # USER_STOPPED must not take that path. The notes are a plain string,
            # and execute() cached every non-error string as a finished deep
            # report, so the next identical query never retried synthesis.
            if getattr(exc, "code", None) == "USER_STOPPED":
                raise
            log.exception("deep_research: synthesis failed; returning collected evidence")
            return {
                "status": "ok",
                "message": _("Web research completed."),
                "result": _partial_report_from_evidence(query, cited_learnings, sources),
                "cacheable": False,
            }
    except ToolExecutionError as exc:
        # Planning, a sub-query, or synthesis can raise USER_STOPPED.
        # execute() only caches status "ok", so the stopped payload must be
        # returned here. Letting it become a partial report string cached it.
        if getattr(exc, "code", None) == "USER_STOPPED":
            return _user_stopped_payload()
        raise
