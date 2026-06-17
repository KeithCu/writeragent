# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""Writing Plan sub-agent: multi-turn plan-driven document writing via specialized delegate."""

from __future__ import annotations

import logging
import re
import traceback
from typing import Any, Iterable, ClassVar, cast

from plugin.framework.tool import ToolBase, ToolContext
from plugin.writer.specialized_base import ToolWriterSpecialBase

log = logging.getLogger(__name__)

_WRITING_DOC_RESEARCH_TOOL_NAMES = frozenset(
    {
        "list_nearby_files",
        "grep_nearby_files",
        "delegate_read_document",
        "search_nearby_files",
    }
)


def _field_from_tool_arguments(arguments: Any, field: str) -> Any:
    if arguments is None:
        return None
    if isinstance(arguments, dict):
        return arguments.get(field)
    if isinstance(arguments, str):
        try:
            from plugin.framework.errors import safe_json_loads

            data = safe_json_loads(arguments)
            if isinstance(data, dict):
                return data.get(field)
        except Exception:
            pass
    return None


def _normalize_html_content_array(content: Any) -> list[str] | None:
    """Accept list of HTML strings or a single string (coerce to one-element list)."""
    if content is None:
        return None
    if isinstance(content, str):
        text = content.strip()
        return [text] if text else None
    if isinstance(content, list):
        out: list[str] = []
        for item in content:
            if item is None:
                continue
            s = str(item).strip()
            if s:
                out.append(s)
        return out if out else None
    return None


def collect_writing_tools(ctx: ToolContext) -> list[ToolBase]:
    """Tools for the writing plan smol sub-agent."""
    from plugin.doc.document_research import filter_document_research_discovery_tools

    registry = ctx.services.get("tools")
    primary = registry.get_tools(doc=ctx.doc, doc_type=ctx.doc_type, active_domain="writing_plan", exclude_tiers=())
    doc_res = registry.get_tools(doc=ctx.doc, doc_type=ctx.doc_type, active_domain="document_research", exclude_tiers=())
    doc_res = filter_document_research_discovery_tools(doc_res, ctx.ctx)
    allow = set(_WRITING_DOC_RESEARCH_TOOL_NAMES)
    by_name = {t.name: t for t in primary if t.name}
    for t in doc_res:
        if t.name in allow and t.name not in by_name:
            by_name[t.name] = t
    return list(by_name.values())


_WRITING_PLAN_CORE_TOOLS = frozenset(["get_document_content", "get_document_tree", "search_in_document"])


class WritingResearchWeb(ToolWriterSpecialBase):
    """Web research for writing plans (public topics); returns plain text for the sub-agent to format as HTML."""

    specialized_domain: ClassVar[str | None] = "writing_plan"
    required_core_tools: ClassVar[frozenset[str] | None] = _WRITING_PLAN_CORE_TOOLS
    intent = "edit"
    name = "writing_research_web"
    description = "Search the public web for context during document writing. Reformats findings as HTML in reply_to_user."
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


class WriteDocumentSection(ToolWriterSpecialBase):
    """Write a specific section of the document."""

    specialized_domain: ClassVar[str | None] = "writing_plan"
    required_core_tools: ClassVar[frozenset[str] | None] = _WRITING_PLAN_CORE_TOOLS
    intent = "edit"
    name = "write_document_section"
    description = (
        "Write/append a specific section's content to the document. "
        "content must be a JSON array of HTML strings (one fragment per block). No Markdown."
    )
    is_mutation = True
    parameters = {
        "type": "object",
        "properties": {
            "content": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of HTML fragments for this section's content. No Markdown.",
            },
            "target": {
                "type": "string",
                "enum": ["beginning", "end", "selection"],
                "description": "Where to insert. Default end.",
            },
        },
        "required": ["content"],
    }

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        content = _normalize_html_content_array(kwargs.get("content"))
        if not content:
            return self._tool_error("content must be a non-empty array of HTML strings.", code="INVALID_CONTENT")

        target = kwargs.get("target") or "end"
        registry = ctx.services.get("tools")
        apply_tool = registry.get("apply_document_content")
        if apply_tool is None:
            return self._tool_error("apply_document_content is not available.", code="TOOL_NOT_FOUND")

        return apply_tool.execute_safe(ctx, content=content, target=target)


class WritingPlanFinishedTool(ToolBase):
    """Ends the writing plan session and returns control to the main assistant."""

    name = "writing_plan_finished"
    description = "Ends the plan-driven writing session. message must be HTML."
    tier = "specialized_control"
    is_final_answer_tool = True
    is_mutation = False
    parameters = {
        "type": "object",
        "properties": {
            "message": {"type": "string", "description": "HTML handoff message for the chat sidebar."},
            "plan_completed": {"type": "boolean", "description": "True if the writing plan is fully executed."},
        },
        "required": ["message"],
    }

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        from plugin.framework.i18n import _

        message = kwargs.get("message") or _("Writing session complete.")
        plan_completed = bool(kwargs.get("plan_completed", False))
        return {"status": "finished", "result": str(message), "plan_completed": plan_completed}


def _run_writing_agent(ctx: ToolContext, *, query: str, history_text: str | None, topic: str | None) -> dict[str, Any]:
    """Run one turn of the writing plan smol sub-agent."""
    from plugin.framework.errors import format_error_payload, ToolExecutionError
    from plugin.chatbot.smol_agent import SmolToolAdapter, build_toolcalling_agent
    from plugin.contrib.smolagents.memory import ActionStep, FinalAnswerStep, ToolCall
    from plugin.chatbot.smol_examples import get_examples_block
    from plugin.framework.constants import get_writing_sub_agent_instructions

    status_callback = getattr(ctx, "status_callback", None)
    append_thinking_callback = getattr(ctx, "append_thinking_callback", None)
    chat_append_callback = getattr(ctx, "chat_append_callback", None)
    stop_checker = getattr(ctx, "stop_checker", None)

    if history_text and len(history_text) > 4000:
        history_text = "..." + history_text[-4000:]

    if status_callback:
        status_callback("Writing...")

    domain_tools = collect_writing_tools(ctx)
    finish_tool = WritingPlanFinishedTool()
    smol_tools = [SmolToolAdapter(t, ctx, safe=True, main_thread_sync=True, inputs_style="specialized") for t in domain_tools]
    smol_tools.append(SmolToolAdapter(finish_tool, ctx, safe=False, inputs_style="librarian"))

    instructions = get_writing_sub_agent_instructions(ctx.ctx)
    if topic and topic.strip():
        instructions += f"\n\n[WRITING TASK / CONTEXT]\n{topic.strip()}\n"

    agent = build_toolcalling_agent(
        ctx,
        smol_tools,
        instructions=instructions,
        final_answer_tool_name="reply_to_user",
        examples_block=get_examples_block("writing_plan"),
        status_callback=status_callback,
    )

    task = f"### CONVERSATION HISTORY:\n{history_text or 'None'}\n\n### CURRENT QUERY:\n{query}"
    final_ans = None
    document_open_step_index = 0

    run_stream = cast("Iterable", agent.run(task, stream=True))
    for step in run_stream:
        if stop_checker and stop_checker():
            return format_error_payload(ToolExecutionError("Writing stopped by user.", code="USER_STOPPED"))
        if isinstance(step, ToolCall):
            if step.name == "delegate_read_document" and chat_append_callback:
                from plugin.chatbot.document_research_chat import document_open_step_chat_text

                path_or_name = _field_from_tool_arguments(step.arguments, "path_or_name")
                chat_append_callback(document_open_step_chat_text(path_or_name, document_open_step_index))
                document_open_step_index += 1
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
                    obs_str = str(step.observations)
                    if "'status': 'finished'" in obs_str or '"status": "finished"' in obs_str:
                        match = re.search(r"'result': '([^']*)'", obs_str) or re.search(r'"result": "([^"]*)"', obs_str)
                        handoff = match.group(1) if match else None
                        plan_match = re.search(r"'plan_completed': (True|False)", obs_str) or re.search(r'"plan_completed": (true|false)', obs_str, re.I)
                        plan_completed = plan_match.group(1).lower() == "true" if plan_match else False
                        append_thinking_callback(msg + "\n")
                        return {"status": "finished", "result": handoff or "Writing session complete.", "plan_completed": plan_completed}
                append_thinking_callback(msg + "\n")
            elif step.observations:
                obs_str = str(step.observations)
                if "'status': 'finished'" in obs_str or '"status": "finished"' in obs_str:
                    match = re.search(r"'result': '([^']*)'", obs_str) or re.search(r'"result": "([^"]*)"', obs_str)
                    handoff = match.group(1) if match else None
                    return {"status": "finished", "result": handoff or "Writing session complete.", "plan_completed": False}
        elif isinstance(step, FinalAnswerStep):
            final_ans = step.output

    return {"status": "ok", "result": str(final_ans)}


class WritingPlanSessionTool(ToolBase):
    """Orchestrator for one turn of the writing plan sub-agent (sidebar session)."""

    name = "writing_plan_session"
    description = "Writing Plan document-generation sub-agent."
    tier = "specialized_control"
    is_mutation = False
    long_running = True
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "User message or initial task."},
            "history_text": {"type": "string", "description": "Previous conversation text."},
            "topic": {"type": "string", "description": "Original context/topic for writing task."},
        },
        "required": ["query"],
    }

    def is_async(self) -> bool:
        return True

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        from plugin.framework.errors import format_error_payload, ToolExecutionError

        query = kwargs.get("query")
        try:
            return _run_writing_agent(
                ctx,
                query=str(query or ""),
                history_text=kwargs.get("history_text"),
                topic=kwargs.get("topic"),
            )
        except Exception as e:
            tb = traceback.format_exc()
            log.error("Writing plan agent error: %s", e)
            err = ToolExecutionError(f"Writing plan failed: {str(e)}\n\n{tb}", details={"query": query})
            return format_error_payload(err)
