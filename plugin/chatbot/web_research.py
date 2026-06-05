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

import logging
import os
from typing import Any, cast

from plugin.framework.tool import ToolBase
from plugin.framework.errors import format_error_payload, ToolExecutionError
from plugin.contrib.smolagents.agents import ToolCallingAgent

log = logging.getLogger("writeragent.web_research")


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


def _research_cache_result_fields(
    event: str,
    cache_key: str,
    cache_path: str | None,
    max_age_days: int,
    *,
    stem_lang: str | None = None,
    matched_key: str | None = None,
    jaccard: float | None = None,
) -> dict[str, Any]:
    from plugin.contrib.smolagents.default_tools import _web_cache_list_keys

    all_keys = _web_cache_list_keys(cache_path, "research", max_age_days) if cache_path else []
    fields: dict[str, Any] = {
        "research_cache_event": event,
        "research_cache_key": cache_key,
        "research_cache_keys": all_keys,
    }
    if stem_lang:
        fields["research_cache_lang"] = stem_lang
    if matched_key:
        fields["research_cache_matched_key"] = matched_key
    if jaccard is not None:
        fields["research_cache_jaccard"] = round(jaccard, 2)
    return fields


def _write_research_cache(
    ctx: Any,
    cache_path: str,
    unique_key: str,
    result_text: str,
    cache_max_mb: int,
    max_age_days: int,
    stem_lang: str,
) -> dict[str, Any]:
    from plugin.chatbot.web_research_cache import format_research_cache_key
    from plugin.contrib.smolagents.default_tools import _web_cache_set

    storage_key = format_research_cache_key(stem_lang, unique_key)
    _web_cache_set(cache_path, "research", storage_key, result_text, cache_max_mb * 1024 * 1024)
    return _research_cache_result_fields("saved", unique_key, cache_path, max_age_days, stem_lang=stem_lang)


class WebResearchTool(ToolCalcWebResearchBase, ToolDrawWebResearchBase):
    doc_types = ["writer", "calc", "draw", "impress"]

    def execute(self, ctx, **kwargs):
        query = kwargs.get("query")
        history_text = kwargs.get("history_text")

        query_str = str(query or "")
        from plugin.chatbot.web_research_cache import resolve_research_locale

        _lo_locale, stem_lang = resolve_research_locale(ctx.ctx, getattr(ctx, "doc", None))
        unique_key = _get_unique_words_key(query_str, snowball_lang=stem_lang)

        from plugin.framework.config import get_config_bool_safe, get_config_int, user_config_dir, get_config_int_safe
        cache_enabled = get_config_bool_safe(ctx.ctx, "web_research_cache_enabled")
        udir = user_config_dir(ctx.ctx)
        cache_path = os.path.join(udir, "writeragent_web_cache.db") if udir else None
        cache_max_age_days = get_config_int(ctx.ctx, "web_cache_validity_days")

        if cache_enabled and cache_path and os.path.exists(cache_path) and unique_key:
            try:
                from plugin.chatbot.web_research_cache import lookup_research_cache
                from plugin.framework.i18n import _

                jaccard_percent = get_config_int(ctx.ctx, "web_research_cache_jaccard_percent")
                min_overlap = get_config_int(ctx.ctx, "web_research_cache_min_overlap")
                hit = lookup_research_cache(
                    cache_path,
                    unique_key,
                    stem_lang,
                    cache_max_age_days,
                    jaccard_percent,
                    min_overlap,
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
                        matched_key=matched_raw_key if event == "hit_fuzzy" else None,
                        jaccard=score if event == "hit_fuzzy" else None,
                    )
                    return {"status": "ok", "message": _("Web research completed."), "result": cached, **cache_fields}
            except Exception as e:
                log.warning("Failed to lookup web research cache: %s", e)

        try:
            from plugin.framework.config import get_api_config
            from plugin.framework.client.llm_client import LlmClient
            from plugin.chatbot.smol_agent import WriterAgentSmolModel, SmolAgentExecutor
            from plugin.contrib.smolagents.default_tools import DuckDuckGoSearchTool, VisitWebpageTool
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

        config = get_api_config(ctx.ctx)
        max_tokens = get_config_int(ctx.ctx, "chat_max_tokens")
        max_steps = get_config_int(ctx.ctx, "chatbot.max_tool_rounds")

        udir = user_config_dir(ctx.ctx)
        raw_mb = get_config_int(ctx.ctx, "web_cache_max_mb")
        cache_max_mb = 0 if raw_mb <= 0 else max(1, min(500, raw_mb))
        cache_path = os.path.join(udir, "writeragent_web_cache.db") if (udir and cache_max_mb > 0) else None

        stop_checker = getattr(ctx, "stop_checker", None)
        cancel_scope = getattr(ctx, "send_cancellation", None)
        smol_model = WriterAgentSmolModel(LlmClient(config, ctx.ctx, cancellation_scope=cancel_scope), max_tokens=max_tokens, status_callback=status_callback, stop_checker=stop_checker)


        from plugin.framework.constants import WEB_RESEARCH_PLAIN_TEXT_FORMAT

        base_intro = (
            "You are a research assistant. Use the conversation context provided below to resolve any ambiguity in the user's query. "
            "Avoid visiting Yelp (yelp.com) links, as Yelp blocks automated requests and returns 403 errors; rely on other sources instead."
        )
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

        from plugin.chatbot.smol_examples import get_examples_block

        agent = WebResearchToolCallingAgent(
            tools=[DuckDuckGoSearchTool(cache_path=cache_path, cache_max_mb=cache_max_mb, cache_max_age_days=cache_max_age_days), VisitWebpageTool(cache_path=cache_path, cache_max_mb=cache_max_mb, cache_max_age_days=cache_max_age_days)],
            model=smol_model,
            max_steps=max_steps,
            instructions=instructions,
            system_prompt_examples=get_examples_block("web_research"),
        )

        task = f"### CONVERSATION HISTORY:\n{history_text or 'None'}\n\n### CURRENT QUERY:\n{query}"
        web_search_step_index = 0
        prompt_for_web_research = False
        try:
            from plugin.framework.config import get_config, as_bool

            prompt_for_web_research = as_bool(get_config(ctx.ctx, "chatbot.prompt_for_web_research"))
        except (ValueError, TypeError):
            pass

        def tool_call_handler(step):
            nonlocal web_search_step_index
            if stop_checker and stop_checker():
                return format_error_payload(ToolExecutionError("Web search stopped by user.", code="USER_STOPPED"))
            status_msg = ""
            if step.name == "web_search":
                q = _web_search_query_from_arguments(step.arguments)
                if append_thinking_callback:
                    append_thinking_callback(f"Running tool: {step.name} with {{'query': '{q}'}}\n")

                if prompt_for_web_research and approval_callback:
                    approval_result = approval_callback(q, "web_search", step.arguments)
                    if isinstance(approval_result, tuple):
                        proceed, query_override = approval_result[0], (approval_result[1] if len(approval_result) > 1 else None)
                    else:
                        proceed, query_override = approval_result, None

                    if not proceed:
                        return format_error_payload(ToolExecutionError("Web search stopped by user.", code="USER_STOPPED"))
                    if query_override is not None:
                        _apply_web_search_query_override(step, query_override)
                        q = query_override

                if chat_append_callback:
                    q_norm = _norm_research_query(q)
                    query_norm = _norm_research_query(cast("str", query)) if query is not None else ""
                    if not (web_search_step_index == 0 and q_norm == query_norm):
                        from plugin.chatbot.web_research_chat import web_search_engine_step_chat_text

                        chat_append_callback(web_search_engine_step_chat_text(q, web_search_step_index))

                web_search_step_index += 1
                status_msg = f"Search: {q[:25]}"
            elif step.name == "visit_webpage":
                url = str(step.arguments.get("url", "")) if isinstance(step.arguments, dict) else ""
                if append_thinking_callback:
                    append_thinking_callback(f"Running tool: {step.name} with {{'url': '{url}'}}\n")
                from plugin.framework.url_utils import get_url_domain

                status_msg = f"Read: {get_url_domain(url)}"
            else:
                if append_thinking_callback:
                    append_thinking_callback(f"Running tool: {step.name} with {step.arguments}\n")
                status_msg = str(step.name)

            if status_callback and status_msg:
                status_callback(f"{status_msg}...")
            return None

        executor = SmolAgentExecutor(ctx)
        final_ans = executor.execute_safe(agent, task, tool_call_handler=tool_call_handler, stop_message="Web search stopped by user.", error_prefix="Web search failed")

        cache_fields: dict[str, Any] = {}
        if isinstance(final_ans, dict) and "status" in final_ans:
            if final_ans.get("status") == "ok" and cache_enabled and cache_path and unique_key:
                try:
                    raw_mb = get_config_int_safe(ctx.ctx, "web_cache_max_mb")
                    cache_max_mb = 0 if raw_mb <= 0 else max(1, min(500, raw_mb))
                    cache_fields = _write_research_cache(ctx, cache_path, unique_key, str(final_ans.get("result", "")), cache_max_mb, cache_max_age_days, stem_lang)
                except Exception as e:
                    log.warning("Failed to write to web research cache: %s", e)
            if cache_fields:
                return {**final_ans, **cache_fields}
            return final_ans

        result_str = str(final_ans)
        if cache_enabled and cache_path and unique_key:
            try:
                raw_mb = get_config_int_safe(ctx.ctx, "web_cache_max_mb")
                cache_max_mb = 0 if raw_mb <= 0 else max(1, min(500, raw_mb))
                cache_fields = _write_research_cache(ctx, cache_path, unique_key, result_str, cache_max_mb, cache_max_age_days, stem_lang)
            except Exception as e:
                log.warning("Failed to write to web research cache: %s", e)

        from plugin.framework.i18n import _

        out: dict[str, Any] = {"status": "ok", "message": _("Web research completed."), "result": result_str}
        if cache_fields:
            out.update(cache_fields)
        return out


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
