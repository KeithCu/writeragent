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
"""Web research tool using smolagents sub-agent."""

# Postponed annotations: on Python before 3.13, threading.Lock is a factory
# (builtin_function_or_method), so `Lock | None` raises at class-body time and
# aborts ChatbotModule.initialize (missing librarian_onboarding / web_research).
from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass
from typing import Any, Callable

from plugin.framework.tool import ToolBase
from plugin.framework.errors import format_error_payload, ToolExecutionError
from plugin.contrib.smolagents.agents import ToolCallingAgent

log = logging.getLogger("writeragent.web_research")


@dataclass
class WebAgentRunParams:
    """Shared setup for one shallow ReAct web-research sub-agent run."""

    smol_model: Any
    max_steps: int
    cache_path: str | None
    cache_max_mb: int
    cache_max_age_days: int
    cdp_enabled: bool
    cdp_url: str | None
    stop_checker: Callable[[], bool] | None
    status_callback: Callable[[str], None] | None
    append_thinking_callback: Callable[[str], None] | None
    approval_callback: Any
    chat_append_callback: Callable[[str], None] | None
    prompt_for_web_research: bool
    outer_query: str
    visited_urls: set[str] | None = None
    visited_urls_lock: threading.Lock | None = None
    max_steps_override: int | None = None
    deep_sub_agent: bool = False


class ToolWriterWebResearchBase(ToolBase):
    name = "web_research"
    description = "Perform deep web research to answer complex questions. Bypasses document context to search the live web."
    doc_types = ["writer"]
    parameters = {"type": "object", "properties": {"query": {"type": "string", "description": "The research query or question."}, "history_text": {"type": "string", "description": "Recent conversation history for context."}}, "required": ["query"]}

    def is_async(self):
        return True


class ToolCalcWebResearchBase(ToolWriterWebResearchBase):
    doc_types = ["calc"]


class ToolDrawWebResearchBase(ToolWriterWebResearchBase):
    doc_types = ["draw", "impress"]


class WebResearchToolCallingAgent(ToolCallingAgent):
    """Subclass of ToolCallingAgent that injects the step budget into the prompt on each step."""

    def augment_messages_for_step(self, messages):
        from plugin.contrib.smolagents.models import ChatMessage, MessageRole

        # Re-calculate remaining steps
        current_step = getattr(self, "step_number", 1)
        max_steps = getattr(self, "max_steps", 20)
        remaining = max_steps - current_step + 1
        used = current_step - 1

        # Match test expectations in plugin/tests/test_web_research_step_budget.py
        budget_msg = f"Step budget: {used} step(s) used, {remaining} step(s) remaining (maximum {max_steps}). You are on step {current_step} of {max_steps}."
        if remaining <= 2:
            budget_msg += " You are almost out of steps! You MUST call final_answer in your next turn with your best summary."

        if messages and (messages[-1].role == MessageRole.USER or messages[-1].role == MessageRole.TOOL_RESPONSE):
            # Prepend to last user-equivalent message to avoid consecutive USER roles after conversion
            last_msg = messages[-1]
            # Ensure content is a list of parts to satisfy smolagents' merge-assertion (models.py:376)
            if isinstance(last_msg.content, str):
                last_msg.content = [{"type": "text", "text": last_msg.content}]
            elif last_msg.content is None:
                last_msg.content = []

            if isinstance(last_msg.content, list):
                last_msg.content.insert(0, {"type": "text", "text": budget_msg})
        else:
            # Use list-of-dicts format to satisfy smolagents' merge-assertion
            messages.append(ChatMessage(role=MessageRole.USER, content=[{"type": "text", "text": budget_msg}]))
        return messages


def _get_unique_words_key(query: str, *, snowball_lang: str = "english") -> str:
    """Normalize query, filter locale-aware fluff, sort unique words and return space-separated key."""
    from plugin.chatbot.web_research_cache import get_research_fluff_words, tokenize_query_words

    if not query:
        return ""
    fluff = get_research_fluff_words(snowball_lang=snowball_lang)
    unique = sorted({w for w in tokenize_query_words(query) if w not in fluff and len(w) >= 3})
    return " ".join(unique)


def _get_embedding_words_text(query: str, *, snowball_lang: str = "english") -> str:
    """Normalize query terms for embeddings while preserving original word order."""
    from plugin.chatbot.web_research_cache import get_research_fluff_words, tokenize_query_words

    if not query:
        return ""
    fluff = get_research_fluff_words(snowball_lang=snowball_lang)
    seen: set[str] = set()
    ordered: list[str] = []
    for token in tokenize_query_words(query):
        if token in fluff or len(token) < 3 or token in seen:
            continue
        seen.add(token)
        ordered.append(token)
    return " ".join(ordered)


def _research_cache_result_fields(
    event: str,
    cache_key: str,
    cache_path: str | None,
    max_age_days: int,
    *,
    stem_lang: str | None = None,
    matched_key: str | None = None,
    score: float | None = None,
) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "research_cache_event": event,
        "research_cache_key": cache_key,
    }
    if stem_lang:
        fields["research_cache_lang"] = stem_lang
    if matched_key:
        fields["research_cache_matched_key"] = matched_key
    if score is not None:
        if event == "hit_embedding":
            fields["research_cache_similarity"] = round(score, 2)
        else:
            fields["research_cache_jaccard"] = round(score, 2)
    return fields


def _write_research_cache(
    ctx: Any,
    cache_path: str,
    unique_key: str,
    result_text: str,
    cache_max_mb: int,
    max_age_days: int,
    stem_lang: str,
    embedding_text: str | None = None,
) -> dict[str, Any]:
    from plugin.chatbot.web_research_cache import format_research_cache_key
    from plugin.chatbot.web_research_cache import enqueue_research_cache_embedding_backfill, enqueue_research_cache_embedding_for_row
    from plugin.contrib.smolagents.default_tools import _web_cache_set

    storage_key = format_research_cache_key(stem_lang, unique_key)
    _web_cache_set(cache_path, "research", storage_key, result_text, cache_max_mb * 1024 * 1024)
    if embedding_text:
        enqueue_research_cache_embedding_for_row(getattr(ctx, "ctx", ctx), cache_path, storage_key, embedding_text)
    enqueue_research_cache_embedding_backfill(getattr(ctx, "ctx", ctx), cache_path, max_age_days)
    return _research_cache_result_fields("saved", unique_key, cache_path, max_age_days, stem_lang=stem_lang)


from plugin.contrib.smolagents.tools import Tool


def _normalize_visit_url(url: str) -> str:
    return str(url or "").strip().rstrip("/")


class _VisitWebpageDedupTool(Tool):
    """Wraps visit_webpage and skips URLs already read in this deep-research run."""

    name = "visit_webpage"
    description = "Visits a webpage at the given url and reads its content as a markdown string. Use this to browse webpages."
    inputs = {"url": {"type": "string", "description": "The url of the webpage to visit."}}
    output_type = "string"

    def __init__(self, inner: Tool, visited_urls: set[str] | None, visited_urls_lock: threading.Lock | None) -> None:
        super().__init__()
        self._inner = inner
        self._visited_urls = visited_urls
        self._visited_urls_lock = visited_urls_lock

    def forward(self, url: str) -> str:
        key = _normalize_visit_url(url)
        if key and self._visited_urls is not None:
            lock = self._visited_urls_lock
            if lock:
                with lock:
                    if key in self._visited_urls:
                        return f"(Already visited in this research run: {key})"
                    self._visited_urls.add(key)
            elif key in self._visited_urls:
                return f"(Already visited in this research run: {key})"
            else:
                self._visited_urls.add(key)
        return self._inner.forward(url)


class VisitWebpageCdpTool(Tool):
    name = "visit_webpage"
    description = "Visits a webpage at the given url and reads its content as a markdown string. Use this to browse webpages."
    inputs = {"url": {"type": "string", "description": "The url of the webpage to visit."}}
    output_type = "string"

    def __init__(self, cdp_url: str, max_output_length: int = 40000, **kwargs):
        super().__init__()
        self.cdp_url = cdp_url
        self.max_output_length = max_output_length

    def forward(self, url: str) -> str:
        from plugin.contrib.cdp.browser_cdp_tool import browser_cdp
        import json
        import time

        try:
            targets_raw = browser_cdp("Target.getTargets")
            targets_data = json.loads(targets_raw)
            if not targets_data.get("success"):
                return f"Failed to list browser targets: {targets_data.get('error')}"
            
            targets = targets_data.get("result", {}).get("targetInfos", [])
            page_target = next((t for t in targets if t.get("type") == "page"), None)
            if page_target is None:
                created_raw = browser_cdp("Target.createTarget", {"url": "about:blank"})
                created_data = json.loads(created_raw)
                target_id = created_data.get("result", {}).get("targetId")
            else:
                target_id = page_target["targetId"]
                
            if not target_id:
                return "Failed to find or create page target"

            nav_raw = browser_cdp("Page.navigate", {"url": url}, target_id=target_id)
            nav_data = json.loads(nav_raw)
            if not nav_data.get("success"):
                return f"Failed to navigate to {url}: {nav_data.get('error')}"
            
            time.sleep(3.0)
            
            eval_raw = browser_cdp(
                "Runtime.evaluate",
                {"expression": "document.body.innerText", "returnByValue": True},
                target_id=target_id
            )
            eval_data = json.loads(eval_raw)
            if not eval_data.get("success"):
                return f"Failed to retrieve page text content: {eval_data.get('error')}"
            
            text = eval_data.get("result", {}).get("result", {}).get("value") or ""
            if not text:
                text = eval_data.get("result", {}).get("result", {}).get("description") or ""
                
            if len(text) > self.max_output_length:
                return text[:self.max_output_length] + f"\n..._This content has been truncated to stay below {self.max_output_length} characters_...\n"
            return text
        except Exception as e:
            return f"Error visiting webpage via CDP: {e}"


def _run_web_agent(
    ctx: Any,
    query: str,
    history_text: str | None,
    params: WebAgentRunParams,
    *,
    research_goal: str | None = None,
) -> str | dict[str, Any]:
    """Run one shallow ReAct web-research sub-agent; returns report text or error payload dict."""
    from plugin.chatbot.smol_agent import SmolAgentExecutor
    from plugin.chatbot.smol_examples import get_examples_block
    from plugin.contrib.smolagents.default_tools import DuckDuckGoSearchTool, VisitWebpageTool
    from plugin.framework.prompts import WEB_RESEARCH_PLAIN_TEXT_FORMAT

    max_steps = params.max_steps_override if params.max_steps_override else params.max_steps
    base_intro = (
        "You are a research assistant. Use the conversation context provided below to resolve any ambiguity in the user's query. "
        "Avoid visiting Yelp (yelp.com) links, as Yelp blocks automated requests and returns 403 errors; rely on other sources instead."
    )
    if params.deep_sub_agent and research_goal:
        base_intro += f"\n\nResearch goal for this sub-task: {research_goal.strip()}"
    tool_steps_budget = max_steps - 1
    budget_text = (
        f"Step limit: at most {max_steps} agent steps total (each step is one tool call, "
        "including final_answer). "
        f"Use at most {tool_steps_budget} step(s) for web_search and visit_webpage; "
        "reserve your last step for the final_answer tool so the run finishes before the hard limit. "
        "If you have enough evidence earlier, call final_answer sooner."
    )
    if max_steps > 20:
        half_steps = max_steps // 2
        budget_text += (
            f" IMPORTANT: If the user's query is a simple question that does not require deep or extensive research "
            f"(e.g., local recommendations, basic facts, simple translations), try to be highly efficient and finish "
            f"in at most half of your step budget (i.e., {half_steps} steps or fewer). Do not use all steps if a quick search suffices."
        )
    instructions = (
        f"{base_intro}\n\n{budget_text}\n\n{WEB_RESEARCH_PLAIN_TEXT_FORMAT}\n"
        "Return the full research report as plain text in final_answer; the main agent receives it in the delegate tool result."
    )

    visit_inner = (
        VisitWebpageCdpTool(cdp_url=params.cdp_url)
        if (params.cdp_enabled and params.cdp_url)
        else VisitWebpageTool(cache_path=params.cache_path, cache_max_mb=params.cache_max_mb, cache_max_age_days=params.cache_max_age_days)
    )
    visit_tool = _VisitWebpageDedupTool(visit_inner, params.visited_urls, params.visited_urls_lock)
    agent = WebResearchToolCallingAgent(
        tools=[
            DuckDuckGoSearchTool(cache_path=params.cache_path, cache_max_mb=params.cache_max_mb, cache_max_age_days=params.cache_max_age_days),
            visit_tool,
        ],
        model=params.smol_model,
        max_steps=max_steps,
        instructions=instructions,
        system_prompt_examples=get_examples_block("web_research"),
    )

    task = f"### CONVERSATION HISTORY:\n{history_text or 'None'}\n\n### CURRENT QUERY:\n{query}"
    web_search_step_index = 0
    stop_checker = params.stop_checker

    def tool_call_handler(step):
        nonlocal web_search_step_index
        if stop_checker and stop_checker():
            return format_error_payload(ToolExecutionError("Web search stopped by user.", code="USER_STOPPED"))
        status_msg = ""
        if step.name == "web_search":
            q = _web_search_query_from_arguments(step.arguments)
            if params.append_thinking_callback:
                params.append_thinking_callback(f"Running tool: {step.name} with {{'query': '{q}'}}\n")

            prompt_and_approve = params.prompt_for_web_research and params.approval_callback

            def _append_search_preview(query_for_engine: str, *, skip_dedup: bool) -> None:
                if not params.chat_append_callback:
                    return
                if not skip_dedup:
                    q_norm = _norm_research_query(query_for_engine)
                    query_norm = _norm_research_query(params.outer_query)
                    if web_search_step_index == 0 and q_norm == query_norm:
                        return
                from plugin.chatbot.web_research_chat import web_search_engine_step_chat_text

                params.chat_append_callback(web_search_engine_step_chat_text(query_for_engine, web_search_step_index))

            if prompt_and_approve:
                # Show the engine query before blocking on Accept/Change/Reject so the sidebar
                # transcript matches what the user is approving (skip outer-query dedup).
                _append_search_preview(q, skip_dedup=True)
                approval_result = params.approval_callback(q, "web_search", step.arguments)
                if isinstance(approval_result, tuple):
                    proceed, query_override = approval_result[0], (approval_result[1] if len(approval_result) > 1 else None)
                else:
                    proceed, query_override = approval_result, None

                if not proceed:
                    return format_error_payload(ToolExecutionError("Web search stopped by user.", code="USER_STOPPED"))
                if query_override is not None:
                    _apply_web_search_query_override(step, query_override)
                    q = query_override
                    _append_search_preview(q, skip_dedup=True)
            else:
                _append_search_preview(q, skip_dedup=False)

            web_search_step_index += 1
            status_msg = f"Search: {q[:25]}"
        elif step.name == "visit_webpage":
            url = str(step.arguments.get("url", "")) if isinstance(step.arguments, dict) else ""
            norm_url = _normalize_visit_url(url)
            if norm_url and params.visited_urls is not None:
                lock = params.visited_urls_lock
                if lock:
                    with lock:
                        params.visited_urls.add(norm_url)
                else:
                    params.visited_urls.add(norm_url)
            if params.append_thinking_callback:
                params.append_thinking_callback(f"Running tool: {step.name} with {{'url': '{url}'}}\n")
            from plugin.framework.url_utils import get_url_domain

            status_msg = f"Read: {get_url_domain(url)}"
        else:
            if params.append_thinking_callback:
                params.append_thinking_callback(f"Running tool: {step.name} with {step.arguments}\n")
            status_msg = str(step.name)

        if params.status_callback and status_msg:
            params.status_callback(f"{status_msg}...")
        return None

    executor = SmolAgentExecutor(ctx)
    return executor.execute_safe(agent, task, tool_call_handler=tool_call_handler, stop_message="Web search stopped by user.", error_prefix="Web search failed")


def _run_deep_web_research(
    ctx: Any,
    query_str: str,
    history_text: str | None,
    agent_params: WebAgentRunParams,
    *,
    cache_path: str | None,
    cache_max_mb: int,
    cache_max_age_days: int,
    plain_text_format: str,
) -> str | dict[str, Any]:
    """Breadth/depth research loop; each sub-query reuses the shallow ReAct sub-agent."""
    from plugin.contrib.smolagents.default_tools import DuckDuckGoSearchTool
    from plugin.framework.config import get_config_int, get_config_int_safe
    from plugin.chatbot.web_research_deep import run_deep_research

    llm_client = agent_params.smol_model.api

    def llm_chat(messages: list[dict[str, str]], max_tok: int) -> str:
        return llm_client.chat_completion_sync(messages, max_tokens=max_tok, prepend_dev_build_system_prefix=False)

    visited_urls: set[str] = set()
    visited_lock = threading.Lock()
    deep_params = WebAgentRunParams(
        smol_model=agent_params.smol_model,
        max_steps=agent_params.max_steps,
        cache_path=agent_params.cache_path,
        cache_max_mb=agent_params.cache_max_mb,
        cache_max_age_days=agent_params.cache_max_age_days,
        cdp_enabled=agent_params.cdp_enabled,
        cdp_url=agent_params.cdp_url,
        stop_checker=agent_params.stop_checker,
        status_callback=agent_params.status_callback,
        append_thinking_callback=agent_params.append_thinking_callback,
        approval_callback=agent_params.approval_callback,
        chat_append_callback=agent_params.chat_append_callback,
        prompt_for_web_research=agent_params.prompt_for_web_research,
        outer_query=agent_params.outer_query,
        visited_urls=visited_urls,
        visited_urls_lock=visited_lock,
        deep_sub_agent=True,
    )
    sub_steps = get_config_int_safe("chatbot.deep_research_sub_agent_steps")
    if sub_steps > 0:
        deep_params.max_steps_override = sub_steps
    elif agent_params.max_steps > 0:
        deep_params.max_steps_override = max(agent_params.max_steps + 1, int(agent_params.max_steps * 1.5))

    def run_sub_agent(sub_query: str, research_goal: str, sub_history: str | None) -> str | dict[str, Any]:
        return _run_web_agent(ctx, sub_query, sub_history, deep_params, research_goal=research_goal or None)

    initial_snippet = ""
    try:
        preview = DuckDuckGoSearchTool(cache_path=cache_path, cache_max_mb=cache_max_mb, cache_max_age_days=cache_max_age_days)
        initial_snippet = str(preview.forward(query_str))[:4000]
    except Exception as preview_exc:
        log.debug("deep_research preview search skipped: %s", preview_exc)

    if agent_params.status_callback:
        agent_params.status_callback("Deep research: starting...")

    max_rounds = get_config_int_safe("chatbot.deep_research_max_rounds")
    if max_rounds <= 0:
        max_rounds = get_config_int("chatbot.deep_research_depth")

    return run_deep_research(
        query_str,
        history_text,
        llm_chat=llm_chat,
        run_web_agent=run_sub_agent,
        stop_checker=agent_params.stop_checker,
        status_callback=agent_params.status_callback,
        breadth=get_config_int("chatbot.deep_research_breadth"),
        max_rounds=max_rounds,
        concurrency=get_config_int("chatbot.deep_research_concurrency"),
        max_sub_queries=get_config_int("chatbot.deep_research_max_sub_queries"),
        quality_threshold=get_config_int("chatbot.deep_research_quality_threshold"),
        plain_text_format=plain_text_format,
        initial_search_snippet=initial_snippet,
    )


class WebResearchTool(ToolCalcWebResearchBase, ToolDrawWebResearchBase):
    doc_types = ["writer", "calc", "draw", "impress"]

    def execute(self, ctx, **kwargs):
        query = kwargs.get("query")
        history_text = kwargs.get("history_text")

        query_str = str(query or "")
        from plugin.chatbot.web_research_cache import resolve_research_locale

        _lo_locale, stem_lang = resolve_research_locale(ctx.ctx, getattr(ctx, "doc", None))
        unique_key = _get_unique_words_key(query_str, snowball_lang=stem_lang)
        embedding_text = _get_embedding_words_text(query_str, snowball_lang=stem_lang)

        from plugin.framework.config import get_config_bool_safe, get_config_int, user_config_dir, get_config_int_safe
        cache_enabled = get_config_bool_safe("web_research_cache_enabled")
        udir = user_config_dir()
        cache_path = os.path.join(udir, "writeragent_web_cache.db") if udir else None
        cache_max_age_days = get_config_int("web_cache_validity_days")

        if cache_enabled and cache_path and os.path.exists(cache_path) and unique_key:
            try:
                from plugin.chatbot.web_research_cache import enqueue_research_cache_embedding_backfill, lookup_research_cache
                from plugin.framework.i18n import _

                jaccard_percent = get_config_int("web_research_cache_jaccard_percent")
                embedding_percent = get_config_int("web_research_cache_embedding_percent")
                min_overlap = get_config_int("web_research_cache_min_overlap")
                enqueue_research_cache_embedding_backfill(ctx.ctx, cache_path, cache_max_age_days)
                hit = lookup_research_cache(
                    cache_path,
                    unique_key,
                    stem_lang,
                    cache_max_age_days,
                    jaccard_percent,
                    min_overlap,
                    ctx=ctx.ctx,
                    embedding_percent=embedding_percent,
                    embedding_text=embedding_text,
                )
                if hit is not None:
                    event, display_key, matched_raw_key, score, cached = hit
                    log.debug("web_cache: research %s for key: %s", event, display_key)
                    cache_fields = _research_cache_result_fields(
                        event,
                        display_key,
                        cache_path,
                        cache_max_age_days,
                        stem_lang=stem_lang,
                        matched_key=matched_raw_key if event in ("hit_fuzzy", "hit_embedding") else None,
                        score=score if event in ("hit_fuzzy", "hit_embedding") else None,
                    )
                    return {"status": "ok", "message": _("Web research completed."), "result": cached, **cache_fields}
            except Exception as e:
                log.warning("Failed to lookup web research cache: %s", e)

        try:
            from plugin.framework.config import get_api_config
            from plugin.framework.client.llm_client import LlmClient
            from plugin.chatbot.smol_agent import WriterAgentSmolModel
        except (ImportError, ValueError, TypeError) as e:
            return format_error_payload(ToolExecutionError(f"Failed to load required dependencies: {e}"))

        status_callback = getattr(ctx, "status_callback", None)
        append_thinking_callback = getattr(ctx, "append_thinking_callback", None)
        approval_callback = getattr(ctx, "approval_callback", None)
        chat_append_callback = getattr(ctx, "chat_append_callback", None)

        if history_text:
            if len(history_text) > 4000:
                history_text = "..." + history_text[-4000:]

        if status_callback:
            status_callback("Sub-agent starting web search: " + str(query or ""))

        config = get_api_config()
        max_tokens = get_config_int("chat_max_tokens")
        max_steps = get_config_int("chatbot.max_tool_rounds")

        udir = user_config_dir()
        raw_mb = get_config_int("web_cache_max_mb")
        cache_max_mb = 0 if raw_mb <= 0 else max(1, min(500, raw_mb))
        cache_path = os.path.join(udir, "writeragent_web_cache.db") if (udir and cache_max_mb > 0) else None

        from plugin.framework.config import get_config
        browser_type = "off"
        try:
            val = get_config("chatbot.web_research_browser")
            if isinstance(val, str):
                browser_type = val
        except Exception:
            pass

        cdp_enabled = (browser_type in ["chrome", "firefox"])
        cdp_url = None
        if cdp_enabled:
            try:
                from plugin.contrib.cdp.browser_cdp_tool import get_local_chrome_cdp_url
                cdp_url = get_local_chrome_cdp_url(ctx.ctx, browser_type)
                log.info("CDP web research enabled (%s). Local debug WS URL: %s", browser_type, cdp_url)
            except Exception as e:
                log.warning("Failed to launch or connect to local %s via CDP: %s. Falling back to static HTTP.", browser_type, e)
                cdp_enabled = False

        stop_checker = getattr(ctx, "stop_checker", None)
        cancel_scope = getattr(ctx, "send_cancellation", None)
        smol_model = WriterAgentSmolModel(LlmClient(config, ctx.ctx, cancellation_scope=cancel_scope), max_tokens=max_tokens, status_callback=status_callback, stop_checker=stop_checker)

        prompt_for_web_research = False
        try:
            from plugin.framework.config import get_config, as_bool

            prompt_for_web_research = as_bool(get_config("chatbot.prompt_for_web_research"))
        except (ValueError, TypeError):
            pass

        agent_params = WebAgentRunParams(
            smol_model=smol_model,
            max_steps=max_steps,
            cache_path=cache_path,
            cache_max_mb=cache_max_mb,
            cache_max_age_days=cache_max_age_days,
            cdp_enabled=cdp_enabled,
            cdp_url=cdp_url,
            stop_checker=stop_checker,
            status_callback=status_callback,
            append_thinking_callback=append_thinking_callback,
            approval_callback=approval_callback,
            chat_append_callback=chat_append_callback,
            prompt_for_web_research=prompt_for_web_research,
            outer_query=query_str,
        )

        from plugin.framework.prompts import WEB_RESEARCH_PLAIN_TEXT_FORMAT

        deep = bool(kwargs.get("deep"))

        try:
            if deep:
                final_ans = _run_deep_web_research(
                    ctx,
                    query_str,
                    history_text,
                    agent_params,
                    cache_path=cache_path,
                    cache_max_mb=cache_max_mb,
                    cache_max_age_days=cache_max_age_days,
                    plain_text_format=WEB_RESEARCH_PLAIN_TEXT_FORMAT,
                )
            else:
                final_ans = _run_web_agent(ctx, query_str, history_text, agent_params)

            cache_fields: dict[str, Any] = {}
            if isinstance(final_ans, dict) and "status" in final_ans:
                if final_ans.get("status") == "ok" and cache_enabled and cache_path and unique_key:
                    try:
                        raw_mb = get_config_int_safe("web_cache_max_mb")
                        cache_max_mb = 0 if raw_mb <= 0 else max(1, min(500, raw_mb))
                        cache_fields = _write_research_cache(ctx, cache_path, unique_key, str(final_ans.get("result", "")), cache_max_mb, cache_max_age_days, stem_lang, embedding_text=embedding_text)
                    except Exception as e:
                        log.warning("Failed to write to web research cache: %s", e)
                if cache_fields:
                    return {**final_ans, **cache_fields}
                return final_ans

            result_str = str(final_ans)
            if cache_enabled and cache_path and unique_key:
                try:
                    raw_mb = get_config_int_safe("web_cache_max_mb")
                    cache_max_mb = 0 if raw_mb <= 0 else max(1, min(500, raw_mb))
                    cache_fields = _write_research_cache(ctx, cache_path, unique_key, result_str, cache_max_mb, cache_max_age_days, stem_lang, embedding_text=embedding_text)
                except Exception as e:
                    log.warning("Failed to write to web research cache: %s", e)

            from plugin.framework.i18n import _

            out: dict[str, Any] = {"status": "ok", "message": _("Web research completed."), "result": result_str}
            if cache_fields:
                out.update(cache_fields)
            return out
        finally:
            if cdp_enabled:
                try:
                    from plugin.contrib.cdp.browser_cdp_tool import cleanup_local_chrome
                    cleanup_local_chrome()
                except Exception as e:
                    log.warning("Failed to clean up local Chrome process: %s", e)


def _web_search_query_from_arguments(arguments: Any) -> str:
    if arguments is None:
        return ""
    if isinstance(arguments, dict):
        return str(arguments.get("query", ""))
    if isinstance(arguments, str):
        try:
            from plugin.framework.errors import safe_json_loads

            data = safe_json_loads(arguments)
            if isinstance(data, dict):
                return str(data.get("query", ""))
        except Exception:
            pass
    return ""


def _apply_web_search_query_override(step: Any, query_override: str) -> bool:
    if not isinstance(step.arguments, dict):
        step.arguments = {"query": query_override}
        return True
    step.arguments["query"] = query_override
    return False


def _norm_research_query(q: str) -> str:
    return " ".join(q.lower().split()).rstrip("?").rstrip(".")
