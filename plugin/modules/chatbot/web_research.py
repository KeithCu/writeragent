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
import logging

from plugin.framework.tool_base import ToolBase

log = logging.getLogger(__name__)


def _web_search_query_from_arguments(arguments) -> str:
    """Best-effort ``query`` string from smolagents ``ToolCall.arguments`` (dict or JSON string)."""
    if isinstance(arguments, dict):
        return str(arguments.get("query", "") or "")
    if isinstance(arguments, str):
        from plugin.framework.errors import safe_json_loads
        parsed = safe_json_loads(arguments)
        if isinstance(parsed, dict):
            return str(parsed.get("query", "") or "")
    return ""


def _apply_web_search_query_override(step, query_override: str) -> bool:
    """Force ``query`` on ``step.arguments`` for DuckDuckGo. Returns True if arguments were coerced."""
    args = step.arguments
    coercion = False
    if isinstance(args, dict):
        new_args = args
    elif isinstance(args, str):
        coercion = True
        from plugin.framework.errors import safe_json_loads
        parsed = safe_json_loads(args)
        new_args = parsed if isinstance(parsed, dict) else {"query": query_override}
    else:
        coercion = True
        new_args = {"query": query_override}
    new_args["query"] = query_override
    step.arguments = new_args
    return coercion


def _norm_research_query(s: str) -> str:
    """Normalize for comparing outer research request to first DDG ``web_search`` query."""
    import re

    return re.sub(r"\s+", " ", (s or "").strip()).casefold()


class WebResearchTool(ToolBase):
    name = "web_research"
    description = "Search the web to answer questions or find information."
    parameters = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Search query"
            },
            "history_text": {
                "type": "string",
                "description": "Previous conversation text for context"
            }
        },
        "required": ["query"]
    }
    tier = "agent"
    is_mutation = False
    long_running = True

    def is_async(self):
        """Run in a background thread so HITL approval can use the main-thread queue/drain loop."""
        return True

    def execute(self, ctx, **kwargs):
        query = kwargs.get("query")
        history_text = kwargs.get("history_text")
        from plugin.framework.errors import format_error_payload, ToolExecutionError
        import os
        from plugin.framework.utils import get_url_domain

        try:
            from plugin.framework.config import get_api_config, get_config_int, user_config_dir
            from plugin.modules.http.client import LlmClient
            from plugin.framework.smol_model import WriterAgentSmolModel
            from plugin.contrib.smolagents.agents import ToolCallingAgent
            from plugin.contrib.smolagents.default_tools import DuckDuckGoSearchTool, VisitWebpageTool
            from plugin.contrib.smolagents.memory import ActionStep, FinalAnswerStep, ToolCall
        except (ImportError, ValueError, TypeError) as e:
            return format_error_payload(ToolExecutionError(f"Failed to load required dependencies: {e}"))

        status_callback = getattr(ctx, "status_callback", None)
        append_thinking_callback = getattr(ctx, "append_thinking_callback", None)
        stop_checker = getattr(ctx, "stop_checker", None)
        approval_callback = getattr(ctx, "approval_callback", None)
        chat_append_callback = getattr(ctx, "chat_append_callback", None)

        if history_text:
            # Truncate if extremely long, though the agent will handle it
            if len(history_text) > 4000:
                history_text = "..." + history_text[-4000:]

        try:
            if status_callback:
                status_callback("Sub-agent starting web search: " + str(query or ""))

            config = get_api_config(ctx.ctx)
            max_tokens = int(config.get("chat_max_tokens", 2048))
            max_steps = int(config.get("search_web_max_steps", 20))

            udir = user_config_dir(ctx.ctx)
            raw_mb = get_config_int(ctx.ctx, "web_cache_max_mb", 50)
            cache_max_mb = 0 if raw_mb <= 0 else max(1, min(500, raw_mb))
            cache_max_age_days = get_config_int(ctx.ctx, "web_cache_validity_days", 7)
            cache_path = os.path.join(udir, "localwriter_web_cache.db") if (udir and cache_max_mb > 0) else None

            smol_model = WriterAgentSmolModel(
                LlmClient(config, ctx.ctx), max_tokens=max_tokens,
                status_callback=status_callback,
            )

            instructions = "You are a research assistant. Use the conversation context provided below to resolve any ambiguity in the user's query."
            agent = ToolCallingAgent(
                tools=[
                    DuckDuckGoSearchTool(cache_path=cache_path, cache_max_mb=cache_max_mb, cache_max_age_days=cache_max_age_days),
                    VisitWebpageTool(cache_path=cache_path, cache_max_mb=cache_max_mb, cache_max_age_days=cache_max_age_days),
                ],
                model=smol_model,
                max_steps=max_steps,
                instructions=instructions,
            )

            task = f"### CONVERSATION HISTORY:\n{history_text or 'None'}\n\n### CURRENT QUERY:\n{query}"
            
            final_ans = None

            prompt_for_web_research = False
            try:
                from plugin.framework.config import get_config, as_bool
                prompt_for_web_research = as_bool(
                    get_config(ctx.ctx, "chatbot.prompt_for_web_research")
                )
            except (ValueError, TypeError) as ex:
                log.warning("prompt_for_web_research config read failed: %s", ex)

            log.debug(
                "web_research: prompt_for_web_research=%s approval_callback=%s",
                prompt_for_web_research,
                approval_callback is not None,
            )

            web_search_step_index = 0
            from typing import Iterable, cast
            run_stream = cast(Iterable, agent.run(task, stream=True))
            for step in run_stream:
                if stop_checker and stop_checker():
                    return format_error_payload(ToolExecutionError("Web search stopped by user.", code="USER_STOPPED"))
                if isinstance(step, ToolCall):
                    status_msg = ""
                    if step.name == "web_search":
                        q = _web_search_query_from_arguments(step.arguments)

                        if append_thinking_callback:
                            append_thinking_callback(f"Running tool: {step.name} with {{'query': '{q}'}}\n")

                        from plugin.modules.chatbot.web_research_chat import (
                            web_search_engine_step_chat_text,
                        )

                        if prompt_for_web_research and approval_callback:
                            log.info(
                                "web_research: requesting approval for web_search query=%r",
                                q[:200] if q else "",
                            )
                            approval_result = approval_callback(
                                q,
                                "web_search",
                                step.arguments,
                            )
                            if isinstance(approval_result, tuple):
                                proceed = bool(approval_result[0])
                                query_override = (
                                    approval_result[1]
                                    if len(approval_result) > 1
                                    else None
                                )
                            else:
                                proceed = bool(approval_result)
                                query_override = None
                            log.info(
                                "web_research: approval result proceed=%s has_override=%s arg_type=%s",
                                proceed,
                                query_override is not None,
                                type(step.arguments).__name__,
                            )
                            if not proceed:
                                log.info("web_research: user rejected web_search approval")
                                return format_error_payload(
                                    ToolExecutionError(
                                        "Web search stopped by user.",
                                        code="USER_STOPPED",
                                    )
                                )
                            if query_override is not None:
                                coerced = _apply_web_search_query_override(step, query_override)
                                q = query_override
                                if coerced:
                                    log.warning(
                                        "web_research: coerced tool arguments to dict for query override "
                                        "(original type was not a mutable dict)"
                                    )
                                log.info(
                                    "web_research: effective web_search query (truncated)=%r coercion=%s",
                                    (q[:200] if q else ""),
                                    coerced,
                                )
                        elif prompt_for_web_research and not approval_callback:
                            log.warning(
                                "web_research: prompt_for_web_research set but no approval_callback (UI cannot prompt)"
                            )
                        elif not prompt_for_web_research and chat_append_callback:
                            # Skip a duplicate full `[Web search]` preview when the first DDG query
                            # matches the outer `query` passed into this tool (same text as user intent).
                            q_norm = _norm_research_query(q)
                            query_norm = _norm_research_query(cast(str, query)) if query is not None else ""
                            skip_redundant_preview = (
                                web_search_step_index == 0
                                and q_norm == query_norm
                            )
                            if not skip_redundant_preview:
                                chat_append_callback(
                                    web_search_engine_step_chat_text(
                                        q,
                                        web_search_step_index,
                                        approval_required=False,
                                    )
                                )
                                # Only advance after a real append so a skipped first
                                # (duplicate of outer block) does not force the next
                                # search to use `[Additional web search]`.
                                web_search_step_index += 1

                        if len(q) > 25:
                            q = q[:22] + "..."
                        status_msg = f"Search: {q}"
                    elif step.name == "visit_webpage":
                        url = str(step.arguments.get("url", "")) if isinstance(step.arguments, dict) else ""

                        if append_thinking_callback:
                            append_thinking_callback(f"Running tool: {step.name} with {{'url': '{url}'}}\n")

                        domain = get_url_domain(url)
                        status_msg = f"Read: {domain}"
                    else:
                        if append_thinking_callback:
                            append_thinking_callback(f"Running tool: {step.name} with {step.arguments}\n")
                        status_msg = str(step.name)

                    if status_callback and status_msg:
                        status_callback(f"{status_msg}...")

                elif isinstance(step, ActionStep):
                    if append_thinking_callback:
                        msg = f"Step {step.step_number}:\n"
                        if step.model_output:
                            mo = step.model_output
                            msg += f"{(mo.strip() if isinstance(mo, str) else str(mo).strip())}\n"
                        else:
                            mom = getattr(step, "model_output_message", None)
                            if mom is not None and getattr(mom, "content", None):
                                mc = mom.content
                                msg += f"{(mc.strip() if isinstance(mc, str) else str(mc).strip())}\n"

                        if step.observations:
                            msg += f"Observation: {str(step.observations).strip()}\n"

                        append_thinking_callback(msg + "\n")
                elif isinstance(step, FinalAnswerStep):
                    final_ans = step.output

            from plugin.framework.i18n import _

            return {
                "status": "ok",
                "message": _("Web research completed."),
                "result": str(final_ans),
            }
        except Exception as e:
            from plugin.framework.errors import NetworkError
            if isinstance(e, NetworkError):
                log.error("Web search NetworkError: %s", e)
            else:
                log.error("Web search error: %s", e)
            err = ToolExecutionError(f"Web search failed: {str(e)}", details={"query": query})
            return format_error_payload(err)
