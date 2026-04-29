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

from plugin.framework.tool_base import ToolBase
from plugin.framework.errors import format_error_payload, ToolExecutionError
from plugin.contrib.smolagents.agents import ToolCallingAgent

log = logging.getLogger("writeragent.web_research")


class ToolWriterWebResearchBase(ToolBase):
    name = "web_research"
    description = (
        "Perform deep web research to answer complex questions. "
        "Bypasses document context to search the live web."
    )
    doc_types = ["Writer"]
    parameters = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "The research query or question.",
            },
            "history_text": {
                "type": "string",
                "description": "Recent conversation history for context.",
            },
        },
        "required": ["query"],
    }

    def is_async(self):
        return True


class ToolCalcWebResearchBase(ToolWriterWebResearchBase):
    doc_types = ["Calc"]


class ToolDrawWebResearchBase(ToolWriterWebResearchBase):
    doc_types = ["Draw", "Impress"]


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
        budget_msg = (
            f"Step budget: {used} step(s) used, {remaining} step(s) remaining (maximum {max_steps}). "
            f"You are on step {current_step} of {max_steps}."
        )
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


class WebResearchTool(ToolCalcWebResearchBase, ToolDrawWebResearchBase):
    def execute(self, ctx, **kwargs):
        query = kwargs.get("query")
        history_text = kwargs.get("history_text")

        try:
            from plugin.framework.config import get_api_config, get_config_int, user_config_dir
            from plugin.modules.http.client import LlmClient
            from plugin.framework.smol_model import WriterAgentSmolModel
            from plugin.contrib.smolagents.default_tools import DuckDuckGoSearchTool, VisitWebpageTool
            from plugin.framework.smol_executor import SmolAgentExecutor
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
        max_steps = get_config_int(ctx.ctx, "chat_max_tool_rounds")

        udir = user_config_dir(ctx.ctx)
        raw_mb = get_config_int(ctx.ctx, "web_cache_max_mb")
        cache_max_mb = 0 if raw_mb <= 0 else max(1, min(500, raw_mb))
        cache_max_age_days = get_config_int(ctx.ctx, "web_cache_validity_days")
        cache_path = os.path.join(udir, "localwriter_web_cache.db") if (udir and cache_max_mb > 0) else None

        smol_model = WriterAgentSmolModel(
            LlmClient(config, ctx.ctx), max_tokens=max_tokens,
            status_callback=status_callback,
        )

        import datetime
        today = datetime.date.today().strftime("%A, %Y-%m-%d")
        base_intro = (
            f"You are a research assistant. Today's date is {today}. "
            "Use the conversation context provided below to resolve any ambiguity in the user's query."
        )
        tool_steps_budget = max_steps - 1
        budget_text = (
            f"Step limit: at most {max_steps} agent steps total (each step is one tool call, "
            "including final_answer). "
            f"Use at most {tool_steps_budget} step(s) for web_search and visit_webpage; "
            "reserve your last step for the final_answer tool so the run finishes before the hard limit. "
            "If you have enough evidence earlier, call final_answer sooner."
        )
        instructions = f"{base_intro}\n\n{budget_text}"

        agent = WebResearchToolCallingAgent(
            tools=[
                DuckDuckGoSearchTool(cache_path=cache_path, cache_max_mb=cache_max_mb, cache_max_age_days=cache_max_age_days),
                VisitWebpageTool(cache_path=cache_path, cache_max_mb=cache_max_mb, cache_max_age_days=cache_max_age_days),
            ],
            model=smol_model,
            max_steps=max_steps,
            instructions=instructions,
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
                elif not prompt_for_web_research and chat_append_callback:
                    q_norm = _norm_research_query(q)
                    query_norm = _norm_research_query(cast("str", query)) if query is not None else ""
                    if not (web_search_step_index == 0 and q_norm == query_norm):
                        from plugin.modules.chatbot.web_research_chat import web_search_engine_step_chat_text
                        chat_append_callback(web_search_engine_step_chat_text(q, web_search_step_index, approval_required=False))
                
                web_search_step_index += 1
                status_msg = f"Search: {q[:25]}"
            elif step.name == "visit_webpage":
                url = str(step.arguments.get("url", "")) if isinstance(step.arguments, dict) else ""
                if append_thinking_callback:
                    append_thinking_callback(f"Running tool: {step.name} with {{'url': '{url}'}}\n")
                from plugin.framework.utils import get_url_domain
                status_msg = f"Read: {get_url_domain(url)}"
            else:
                if append_thinking_callback:
                    append_thinking_callback(f"Running tool: {step.name} with {step.arguments}\n")
                status_msg = str(step.name)

            if status_callback and status_msg:
                status_callback(f"{status_msg}...")
            return None

        executor = SmolAgentExecutor(ctx)
        final_ans = executor.execute_safe(
            agent, task, tool_call_handler=tool_call_handler,
            stop_message="Web search stopped by user.",
            error_prefix="Web search failed"
        )

        if isinstance(final_ans, dict) and "status" in final_ans:
            return final_ans

        from plugin.framework.i18n import _
        return {
            "status": "ok",
            "message": _("Web research completed."),
            "result": str(final_ans),
        }


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
