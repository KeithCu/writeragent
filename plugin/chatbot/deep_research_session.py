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
from typing import Any, ClassVar

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


def _run_deep_research_agent(ctx: ToolContext, *, query: str = "", history_text: str | None = None, **kwargs: Any) -> dict[str, Any]:
    """Run one turn of the Deep Research smol sub-agent."""
    from plugin.chatbot.smol_agent import SmolAgentExecutor, SmolToolAdapter, build_toolcalling_agent
    from plugin.chatbot.smol_examples import get_examples_block
    from plugin.framework.prompts import get_deep_research_sub_agent_instructions

    status_callback = getattr(ctx, "status_callback", None)

    if history_text and len(history_text) > 4000:
        history_text = "..." + history_text[-4000:]

    if status_callback:
        status_callback("Deep research...")

    domain_tools = collect_deep_research_tools(ctx)
    smol_tools = [SmolToolAdapter(t, ctx, safe=True, inputs_style="specialized") for t in domain_tools]

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
    executor = SmolAgentExecutor(ctx)
    res = executor.execute_safe(agent, task, stop_message="Deep research stopped by user.", error_prefix="Deep research failed")
    if isinstance(res, dict) and res.get("status") == "error":
        return res
    return {"status": "ok", "result": str(res)}


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
        from plugin.chatbot.smol_agent import run_subagent_tool

        return run_subagent_tool("Deep research", _run_deep_research_agent, ctx, **kwargs)

