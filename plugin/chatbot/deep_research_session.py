# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""Deep Research sidebar sub-agent: breadth/depth web research + document apply."""

from __future__ import annotations

import logging
import traceback
from typing import Any, ClassVar, Iterable, cast

from plugin.framework.tool import ToolBase, ToolContext

log = logging.getLogger(__name__)

_DEEP_RESEARCH_CORE_TOOLS = frozenset(["get_document_content", "get_document_tree", "search_in_document", "apply_document_content"])


def collect_deep_research_tools(ctx: ToolContext) -> list[ToolBase]:
    """Tools for the Deep Research smol sub-agent (domain + required core tools)."""
    registry = ctx.services.get("tools")
    return registry.get_tools(
        doc_type=ctx.doc_type,
        uno_services_supported=ctx.uno_services_supported,
        active_domain="deep_research",
        exclude_tiers=(),
    )


class DeepResearchWebTool(ToolBase):
    """Multi-step public web research (sidebar Deep Research only; not shallow web_research)."""

    tier = "specialized"
    specialized_domain: ClassVar[str | None] = "deep_research"
    specialized_cross_cutting: ClassVar[bool] = True
    required_core_tools: ClassVar[frozenset[str] | None] = _DEEP_RESEARCH_CORE_TOOLS
    doc_types = ["writer", "calc", "draw", "impress"]
    intent = "review"
    name = "deep_research_web"
    description = (
        "Run breadth/depth public web research on a topic. Returns plain text; "
        "format as HTML and insert with apply_document_content."
    )
    is_mutation = False
    long_running = True
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Research question or topic."},
        },
        "required": ["query"],
    }

    def is_async(self) -> bool:
        return True

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        from plugin.chatbot.web_research import WebResearchTool

        query = kwargs.get("query")
        return WebResearchTool().execute(ctx, query=query, deep=True)


def _run_deep_research_agent(ctx: ToolContext, *, query: str, history_text: str | None) -> dict[str, Any]:
    """Run one turn of the Deep Research smol sub-agent."""
    from plugin.framework.errors import format_error_payload, ToolExecutionError
    from plugin.chatbot.smol_agent import SmolToolAdapter, build_toolcalling_agent
    from plugin.contrib.smolagents.memory import ActionStep, FinalAnswerStep, ToolCall
    from plugin.chatbot.smol_examples import get_examples_block
    from plugin.framework.constants import get_deep_research_sub_agent_instructions

    status_callback = getattr(ctx, "status_callback", None)
    append_thinking_callback = getattr(ctx, "append_thinking_callback", None)
    stop_checker = getattr(ctx, "stop_checker", None)

    if history_text and len(history_text) > 4000:
        history_text = "..." + history_text[-4000:]

    if status_callback:
        status_callback("Deep research...")

    domain_tools = collect_deep_research_tools(ctx)
    smol_tools = [SmolToolAdapter(t, ctx, safe=True, main_thread_sync=True, inputs_style="specialized") for t in domain_tools]

    instructions = get_deep_research_sub_agent_instructions(ctx.ctx)
    agent = build_toolcalling_agent(
        ctx,
        smol_tools,
        instructions=instructions,
        final_answer_tool_name="reply_to_user",
        examples_block=get_examples_block("deep_research"),
        status_callback=status_callback,
    )

    task = f"### CONVERSATION HISTORY:\n{history_text or 'None'}\n\n### CURRENT QUERY:\n{query}"
    final_ans = None

    run_stream = cast("Iterable", agent.run(task, stream=True))
    for step in run_stream:
        if stop_checker and stop_checker():
            return format_error_payload(ToolExecutionError("Deep research stopped by user.", code="USER_STOPPED"))
        if isinstance(step, ToolCall):
            if append_thinking_callback:
                append_thinking_callback(f"Running tool: {step.name} with {step.arguments}\n")
            if status_callback:
                status_callback(f"{step.name}...")
        elif isinstance(step, ActionStep):
            if append_thinking_callback:
                msg = f"Step {step.step_number}:\n"
                if step.model_output:
                    mo = step.model_output
                    msg += f"{(mo.strip() if isinstance(mo, str) else str(mo).strip())}\n"
                if step.observations:
                    msg += f"Observation: {str(step.observations).strip()}\n"
                append_thinking_callback(msg + "\n")
        elif isinstance(step, FinalAnswerStep):
            final_ans = step.output

    return {"status": "ok", "result": str(final_ans)}


class DeepResearchSessionTool(ToolBase):
    """Orchestrator for one turn of the Deep Research sub-agent (sidebar session)."""

    name = "deep_research_session"
    description = "Deep Research sub-agent (multi-step web research + optional document insert)."
    tier = "specialized_control"
    is_mutation = False
    long_running = True
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "User message or research task."},
            "history_text": {"type": "string", "description": "Previous conversation text."},
        },
        "required": ["query"],
    }

    def is_async(self) -> bool:
        return True

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        from plugin.framework.errors import format_error_payload, ToolExecutionError

        query = kwargs.get("query")
        try:
            return _run_deep_research_agent(
                ctx,
                query=str(query or ""),
                history_text=kwargs.get("history_text"),
            )
        except Exception as e:
            tb = traceback.format_exc()
            log.error("Deep research agent error: %s", e)
            err = ToolExecutionError(f"Deep research failed: {str(e)}\n\n{tb}", details={"query": query})
            return format_error_payload(err)
