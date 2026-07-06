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
import re
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from plugin.framework.errors import ToolExecutionError, format_error_payload
from plugin.framework.json_utils import safe_json_loads

log = logging.getLogger("writeragent.web_research_deep")

MAX_CONTEXT_WORDS = 25000

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
StopChecker = Callable[[], bool] | None
StatusCallback = Callable[[str], None] | None
ProgressCallback = Callable[["ResearchProgress"], None] | None


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
        parsed_queries = [
            {"query": str(item["query"]).strip(), "researchGoal": str(item["researchGoal"]).strip()}
            for item in candidate_queries
            if isinstance(item, dict) and item.get("query") and item.get("researchGoal")
        ]
        if parsed_queries:
            return parsed_queries[:num_queries]

    line_queries: list[dict[str, str]] = []
    current_query: dict[str, str] = {}
    for raw_line in response.replace("```json", "").replace("```", "").splitlines():
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


def parse_follow_up_questions_response(response: str, num_questions: int) -> list[str]:
    parsed = _load_repaired_json(response)
    candidate_questions = parsed
    if isinstance(parsed, dict):
        candidate_questions = parsed.get("questions") or parsed.get("followUpQuestions") or parsed.get("items")

    if isinstance(candidate_questions, list):
        parsed_questions = [str(item).strip() for item in candidate_questions if str(item).strip()]
        if parsed_questions:
            return parsed_questions[:num_questions]

    line_questions: list[str] = []
    for raw_line in response.replace("```json", "").replace("```", "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        question_match = QUESTION_LINE_PATTERN.match(line)
        if question_match:
            line_questions.append(question_match.group("question").strip())
    return line_questions[:num_questions]


def parse_research_results_response(response: str, num_learnings: int) -> dict[str, Any]:
    parsed = _load_repaired_json(response)

    if isinstance(parsed, dict):
        learnings_payload = parsed.get("learnings", [])
        follow_up_payload = parsed.get("followUpQuestions") or parsed.get("questions") or []
        learnings: list[str] = []
        citations: dict[str, str] = {}
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
                        citations[learning] = citation
        questions = [str(item).strip() for item in follow_up_payload if str(item).strip()]
        if learnings or questions:
            return {
                "learnings": learnings[:num_learnings],
                "followUpQuestions": questions[:num_learnings],
                "citations": citations,
            }

    line_learnings: list[str] = []
    line_questions: list[str] = []
    line_citations: dict[str, str] = {}
    for raw_line in response.replace("```json", "").replace("```", "").splitlines():
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
                    line_citations[learning] = citation
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


def trim_context_to_word_limit(context_list: list[str], max_words: int = MAX_CONTEXT_WORDS) -> list[str]:
    total_words = 0
    trimmed_context: list[str] = []
    for item in reversed(context_list):
        words = count_words(item)
        if total_words + words <= max_words:
            trimmed_context.insert(0, item)
            total_words += words
        elif not trimmed_context:
            text = " ".join(str(part) for part in item) if isinstance(item, list) else str(item)
            trimmed_context.insert(0, " ".join(text.split()[:max_words]))
            break
        else:
            break
    return trimmed_context


def _check_stopped(stop_checker: StopChecker) -> dict[str, Any] | None:
    if stop_checker and stop_checker():
        return format_error_payload(ToolExecutionError("Web search stopped by user.", code="USER_STOPPED"))
    return None


@dataclass
class ResearchProgress:
    """Tracks deep-research progress for sidebar status updates."""

    current_round: int = 1
    max_rounds: int = 1
    completed_queries: int = 0
    max_sub_queries: int = 14
    current_query: str | None = None

    def status_text(self) -> str:
        q = (self.current_query or "")[:50]
        return (
            f"Deep research round {self.current_round}/{self.max_rounds}, "
            f"query {self.completed_queries}/{self.max_sub_queries}"
            + (f": {q}..." if q else "")
        )


@dataclass
class _ResearchAccumulator:
    learnings: list[str] = field(default_factory=list)
    citations: dict[str, str] = field(default_factory=dict)
    context_chunks: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    completed_queries: int = 0
    budget_lock: threading.Lock = field(default_factory=threading.Lock)


def _emit_progress(
    progress: ResearchProgress,
    status_callback: StatusCallback,
    on_progress: ProgressCallback,
) -> None:
    if on_progress:
        on_progress(progress)
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
        return {
            "score": score,
            "knowledge_gaps": gaps,
            "suggested_queries": queries,
            "stop": bool(stop_flag) if stop_flag is not None else False,
            "reasoning": str(parsed.get("reasoning") or ""),
        }
    return {"score": 0.0, "knowledge_gaps": [], "suggested_queries": [], "stop": False, "reasoning": ""}


def assess_research_coverage(
    llm_chat: LlmChatFn,
    original_query: str,
    learnings: list[str],
    citations: dict[str, str],
    *,
    quality_threshold: int,
) -> dict[str, Any]:
    cited = []
    for learning in learnings[:40]:
        citation = citations.get(learning, "")
        cited.append(f"{learning} [Source: {citation}]" if citation else learning)
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
    response = _llm_chat(llm_chat, messages, max_tokens=1200)
    return parse_assessment_response(response)


def _llm_chat(llm_chat: LlmChatFn, messages: list[dict[str, str]], max_tokens: int = 1000) -> str:
    return llm_chat(messages, max_tokens)


def generate_search_queries(llm_chat: LlmChatFn, query: str, num_queries: int) -> list[dict[str, str]]:
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
                'Return ONLY a JSON array of objects using this exact schema:\n'
                '[{"query": "<search query>", "researchGoal": "<research goal>"}]\n\n'
                f"Prompt: {query}"
            ),
        },
    ]
    response = _llm_chat(llm_chat, messages, max_tokens=1500)
    return parse_search_queries_response(response, num_queries)


def generate_research_plan(llm_chat: LlmChatFn, query: str, search_results: str, num_questions: int = 3) -> list[str]:
    current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    messages = [
        {
            "role": "system",
            "content": (
                "You are an expert researcher. Your task is to analyze the original query and search results, "
                "then generate targeted questions that explore different aspects and time periods of the topic. "
                "Return valid JSON only."
            ),
        },
        {
            "role": "user",
            "content": (
                f"Original query: {query}\n\nCurrent time: {current_time}\n\nSearch results:\n{search_results}\n\n"
                f"Based on these results, the original query, and the current time, generate {num_questions} unique questions. "
                f"Each question should explore a different aspect or time period of the topic, considering recent developments up to {current_time}.\n\n"
                'Return ONLY a JSON object using this exact schema:\n'
                '{"questions": ["<question 1>", "<question 2>"]}'
            ),
        },
    ]
    response = _llm_chat(llm_chat, messages, max_tokens=1500)
    return parse_follow_up_questions_response(response, num_questions)


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
    response = _llm_chat(llm_chat, messages, max_tokens=1000)
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
    context_with_citations = list(learnings)
    context_with_citations.extend(context_chunks)
    trimmed = trim_context_to_word_limit(context_with_citations)
    evidence = "\n\n".join(trimmed)
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
    return _llm_chat(llm_chat, messages, max_tokens=4096)


def _coerce_agent_result(result: str | dict[str, Any]) -> str:
    if isinstance(result, dict):
        if result.get("status") == "error":
            raise ToolExecutionError(str(result.get("message") or "Sub-query research failed."))
        if result.get("status") == "ok":
            return str(result.get("result") or "")
        if "result" in result:
            return str(result.get("result") or "")
        raise ToolExecutionError(str(result.get("message") or "Sub-query research failed."))
    return str(result)


def _extract_urls_from_text(text: str) -> list[str]:
    return list(dict.fromkeys(URL_PATTERN.findall(text or "")))


def _merge_branch_results(acc: _ResearchAccumulator, branch: dict[str, Any]) -> None:
    acc.learnings.extend(branch.get("learnings") or [])
    acc.citations.update(branch.get("citations") or {})
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
    on_progress: ProgressCallback,
) -> dict[str, Any] | None:
    stopped = _check_stopped(stop_checker)
    if stopped is not None:
        return {"error": stopped}

    with acc.budget_lock:
        if acc.completed_queries >= max_sub_queries:
            return None
        acc.completed_queries += 1
        progress.completed_queries = acc.completed_queries

    sub_query = serp_query["query"]
    research_goal = serp_query.get("researchGoal") or ""
    progress.current_query = sub_query
    _emit_progress(progress, status_callback, on_progress)

    try:
        raw = run_web_agent(sub_query, research_goal, None)
        sub_context = _coerce_agent_result(raw)
    except ToolExecutionError as exc:
        log.warning("deep_research: sub-query failed (%s): %s", sub_query, exc)
        return None

    results = process_research_results(llm_chat, sub_query, sub_context)
    sources = _extract_urls_from_text(sub_context)
    for url in results.get("citations") or {}:
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
    on_progress: ProgressCallback,
) -> dict[str, Any] | None:
    if not serp_queries:
        return None

    workers = max(1, min(concurrency, len(serp_queries)))
    error_payload: dict[str, Any] | None = None

    def _task(sq: dict[str, str]) -> dict[str, Any] | None:
        return _process_one_sub_query(
            sq,
            run_web_agent=run_web_agent,
            llm_chat=llm_chat,
            stop_checker=stop_checker,
            acc=acc,
            max_sub_queries=max_sub_queries,
            progress=progress,
            status_callback=status_callback,
            on_progress=on_progress,
        )

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="deep-research") as pool:
        futures = {pool.submit(_task, sq): sq for sq in serp_queries}
        for future in as_completed(futures):
            stopped = _check_stopped(stop_checker)
            if stopped is not None:
                error_payload = stopped
                break
            try:
                branch = future.result()
            except Exception as exc:
                sq = futures[future]
                log.warning("deep_research: parallel sub-query error (%s): %s", sq.get("query"), exc)
                continue
            if isinstance(branch, dict) and branch.get("error"):
                error_payload = branch["error"]
                break
            if branch:
                _merge_branch_results(acc, branch)

    return error_payload


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
    on_progress: ProgressCallback,
    breadth: int,
    max_rounds: int,
    concurrency: int,
    max_sub_queries: int,
    quality_threshold: int,
) -> dict[str, Any]:
    acc = _ResearchAccumulator()
    progress = ResearchProgress(max_rounds=max_rounds, max_sub_queries=max_sub_queries)
    next_gap_queries: list[str] = []

    for round_num in range(1, max_rounds + 1):
        stopped = _check_stopped(stop_checker)
        if stopped is not None:
            return {"error": stopped}

        progress.current_round = round_num
        _emit_progress(progress, status_callback, on_progress)

        if round_num == 1:
            if status_callback:
                status_callback(f"Planning {breadth} research queries...")
            serp_queries = generate_search_queries(llm_chat, combined_query, num_queries=breadth)
        elif next_gap_queries:
            serp_queries = _serp_from_suggested_queries(next_gap_queries[:breadth])
        else:
            gap_text = combined_query + "\n\nPrior learnings:\n" + "\n".join(acc.learnings[-20:])
            serp_queries = generate_search_queries(llm_chat, gap_text, num_queries=breadth)

        if not serp_queries:
            log.warning("deep_research: no search queries for round %s", round_num)
            break

        remaining = max_sub_queries - acc.completed_queries
        if remaining <= 0:
            break
        serp_queries = serp_queries[: min(len(serp_queries), breadth, remaining)]

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
            on_progress=on_progress,
        )
        if loop_error is not None:
            return {"error": loop_error}

        if acc.completed_queries >= max_sub_queries:
            break

        if status_callback:
            status_callback("Deep research: assessing coverage...")
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

    unique_learnings = list(dict.fromkeys(acc.learnings))
    trimmed_context = trim_context_to_word_limit(acc.context_chunks)
    return {
        "learnings": unique_learnings,
        "citations": acc.citations,
        "context": trimmed_context,
        "sources": list(dict.fromkeys(acc.sources)),
    }


def run_deep_research(
    query: str,
    history_text: str | None,
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
    on_progress: ProgressCallback = None,
    depth: int | None = None,
) -> str | dict[str, Any]:
    """Run adaptive multi-round deep research; returns report string or error payload dict."""
    # depth kept for backward compatibility; max_rounds is primary.
    if depth is not None and max_rounds == 3:
        max_rounds = depth

    stopped = _check_stopped(stop_checker)
    if stopped is not None:
        return stopped

    if status_callback:
        status_callback("Deep research: planning strategy...")

    search_snippet = initial_search_snippet
    if not search_snippet.strip():
        search_snippet = "(No preview search results; proceed from the query alone.)"

    follow_up_questions = generate_research_plan(llm_chat, query, search_snippet, num_questions=3)
    if not follow_up_questions:
        follow_up_questions = [query]

    qa_pairs = [f"Q: {q}\nA: Automatically proceeding with research" for q in follow_up_questions]
    combined_query = f"Initial Query: {query}\n"
    if history_text:
        combined_query += f"Conversation history:\n{history_text}\n"
    combined_query += "Follow-up Questions and Answers:\n" + "\n".join(qa_pairs)

    loop_result = _run_adaptive_research_loop(
        combined_query,
        query,
        llm_chat=llm_chat,
        run_web_agent=run_web_agent,
        stop_checker=stop_checker,
        status_callback=status_callback,
        on_progress=on_progress,
        breadth=breadth,
        max_rounds=max_rounds,
        concurrency=concurrency,
        max_sub_queries=max_sub_queries,
        quality_threshold=quality_threshold,
    )
    if loop_result.get("error"):
        return loop_result["error"]

    learnings = loop_result.get("learnings") or []
    context_chunks = loop_result.get("context") or []
    citations = loop_result.get("citations") or {}
    sources = loop_result.get("sources") or []

    cited_learnings: list[str] = []
    for learning in learnings:
        citation = citations.get(learning, "")
        if citation:
            cited_learnings.append(f"{learning} [Source: {citation}]")
        else:
            cited_learnings.append(learning)

    if status_callback:
        status_callback("Deep research: synthesizing report...")

    stopped = _check_stopped(stop_checker)
    if stopped is not None:
        return stopped

    return synthesize_deep_report(
        llm_chat,
        query,
        cited_learnings,
        context_chunks,
        plain_text_format,
        sources=sources,
    )
