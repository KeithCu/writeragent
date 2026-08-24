# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""Brainstorming sub-agent: multi-turn design exploration via specialized delegate."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, ClassVar

if TYPE_CHECKING:
    from plugin.contrib.smolagents.memory import ToolCall

from plugin.doc.document_research import DOC_RESEARCH_DISCOVERY_TOOL_NAMES, filter_document_research_discovery_tools
from plugin.doc.specialized_base import _field_from_tool_arguments
from plugin.chatbot.smol_examples import normalize_html_content_array
from plugin.framework.tool import ToolBase, ToolContext
from plugin.writer.specialized_base import ToolWriterSpecialBase

log = logging.getLogger(__name__)

_normalize_html_content_array = normalize_html_content_array


def collect_brainstorming_tools(ctx: ToolContext) -> list[ToolBase]:
    """Tools for the brainstorming smol sub-agent (brainstorming domain + doc research reads)."""
    registry = ctx.services.get("tools")
    primary = registry.get_tools(doc_type=ctx.doc_type, uno_services_supported=ctx.uno_services_supported, active_domain="brainstorming", exclude_tiers=())
    doc_res = registry.get_tools(doc_type=ctx.doc_type, uno_services_supported=ctx.uno_services_supported, active_domain="document_research", exclude_tiers=())
    doc_res = filter_document_research_discovery_tools(doc_res, ctx.ctx)
    allow = set(DOC_RESEARCH_DISCOVERY_TOOL_NAMES)
    by_name = {t.name: t for t in primary if t.name}
    for t in doc_res:
        if t.name in allow and t.name not in by_name:
            by_name[t.name] = t
    return list(by_name.values())


_BRAINSTORMING_CORE_TOOLS = frozenset(["get_document_content", "get_document_tree", "search_in_document"])


class BrainstormResearchWeb(ToolWriterSpecialBase):
    """Web research for brainstorming (public topics); returns plain text for the sub-agent to format as HTML."""

    specialized_domain: ClassVar[str | None] = "brainstorming"
    required_core_tools: ClassVar[frozenset[str] | None] = _BRAINSTORMING_CORE_TOOLS
    intent = "review"
    name = "brainstorm_research_web"
    description = "Search the public web for context during brainstorming. Reformats findings as HTML in reply_to_user."
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
        return WebResearchTool().execute(ctx, query=query)


class SaveDesignSpec(ToolWriterSpecialBase):
    """Write the approved design spec into the active Writer document (HTML array only)."""

    specialized_domain: ClassVar[str | None] = "brainstorming"
    required_core_tools: ClassVar[frozenset[str] | None] = _BRAINSTORMING_CORE_TOOLS
    intent = "review"
    name = "save_design_spec"
    description = (
        "Save the approved design spec to the active Writer document. "
        "content must be a JSON array of HTML strings (one fragment per block). No Markdown."
    )
    is_mutation = True
    parameters = {
        "type": "object",
        "properties": {
            "content": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of HTML fragments (e.g. <h1>, <p>, <ul>). No Markdown.",
            },
            "target": {
                "type": "string",
                "enum": ["beginning", "end", "full_document"],
                "description": "Where to insert. Default end. Use full_document only when the doc is empty.",
            },
        },
        "required": ["content"],
    }

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        content = normalize_html_content_array(kwargs.get("content"))
        if not content:
            return self._tool_error("content must be a non-empty array of HTML strings.", code="INVALID_CONTENT")

        target = kwargs.get("target") or "end"
        if target not in ("beginning", "end", "full_document"):
            target = "end"

        registry = ctx.services.get("tools")
        apply_tool = registry.get("apply_document_content")
        if apply_tool is None:
            return self._tool_error("apply_document_content is not available.", code="TOOL_NOT_FOUND")

        return apply_tool.execute_safe(ctx, content=content, target=target)


def _run_brainstorming_agent(ctx: ToolContext, *, query: str = "", history_text: str | None = None, topic: str | None = None, **kwargs: Any) -> dict[str, Any]:
    """Run one turn of the brainstorming smol sub-agent."""
    from plugin.chatbot.smol_agent import SmolAgentExecutor, SmolToolAdapter, build_toolcalling_agent
    from plugin.chatbot.smol_examples import get_examples_block
    from plugin.framework.prompts import get_brainstorming_sub_agent_instructions

    status_callback = getattr(ctx, "status_callback", None)
    append_thinking_callback = getattr(ctx, "append_thinking_callback", None)
    chat_append_callback = getattr(ctx, "chat_append_callback", None)

    if history_text and len(history_text) > 4000:
        history_text = "..." + history_text[-4000:]

    if status_callback:
        status_callback("Brainstorming...")

    from plugin.chatbot.sticky_reply import BRAINSTORMING_REPLY_SPEC, StickyReplyToUserTool, interpret_sticky_final_answer

    domain_tools = collect_brainstorming_tools(ctx)
    smol_tools = [SmolToolAdapter(t, ctx, safe=True, inputs_style="specialized") for t in domain_tools]
    smol_tools.append(SmolToolAdapter(StickyReplyToUserTool(BRAINSTORMING_REPLY_SPEC), ctx, safe=False, inputs_style="librarian"))

    instructions = get_brainstorming_sub_agent_instructions(ctx.ctx)
    if topic and topic.strip():
        instructions += f"\n\n[BRAINSTORMING TOPIC]\n{topic.strip()}\n"

    agent = build_toolcalling_agent(
        ctx,
        smol_tools,
        instructions=instructions,
        final_answer_tool_name="reply_to_user",
        examples_block=get_examples_block("brainstorming"),
        status_callback=status_callback,
    )

    task = f"### CONVERSATION HISTORY:\n{history_text or 'None'}\n\n### CURRENT QUERY:\n{query}"
    document_open_step_index = 0

    def tool_call_handler(step: ToolCall) -> Any:
        nonlocal document_open_step_index
        if step.name == "delegate_read_document" and chat_append_callback:
            from plugin.chatbot.web_research_chat import document_open_step_chat_text

            path_or_name = _field_from_tool_arguments(step.arguments, "path_or_name")
            chat_append_callback(document_open_step_chat_text(path_or_name, document_open_step_index))
            document_open_step_index += 1
        if append_thinking_callback:
            append_thinking_callback(f"Running tool: {step.name} with {step.arguments}\n")
        if status_callback:
            status_callback(f"{step.name}...")
        return None

    executor = SmolAgentExecutor(ctx)
    res = executor.execute_safe(
        agent,
        task,
        tool_call_handler=tool_call_handler,
        stop_message="Brainstorming stopped by user.",
        error_prefix="Brainstorming failed",
    )
    if isinstance(res, dict) and res.get("status") == "error":
        return res
    return interpret_sticky_final_answer(res, leave_status=BRAINSTORMING_REPLY_SPEC.leave_status)


class BrainstormingSessionTool(ToolBase):
    """Orchestrator for one turn of the brainstorming sub-agent (sidebar session)."""

    name = "brainstorming_session"
    description = "Brainstorming design exploration sub-agent."
    tier = "specialized_control"
    is_mutation = False
    long_running = True
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "User message or initial task."},
            "history_text": {"type": "string", "description": "Previous conversation text."},
            "topic": {"type": "string", "description": "Original brainstorming topic from delegate task."},
        },
        "required": ["query"],
    }

    def is_async(self) -> bool:
        return True

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        from plugin.chatbot.smol_agent import run_subagent_tool

        return run_subagent_tool("Brainstorming", _run_brainstorming_agent, ctx, **kwargs)

