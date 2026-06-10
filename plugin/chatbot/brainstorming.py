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
import re
import traceback
from typing import Any, Iterable, cast

from plugin.framework.tool import ToolBase, ToolContext
from plugin.writer.specialized_base import ToolWriterBrainstormingBase

log = logging.getLogger(__name__)

# document_research tools merged into brainstorming sessions (read-only discovery).
_BRAINSTORMING_DOC_RESEARCH_TOOL_NAMES = frozenset(
    {
        "list_nearby_files",
        "grep_nearby_files",
        "delegate_read_document",
        "search_embeddings",
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


def collect_brainstorming_tools(ctx: ToolContext) -> list[ToolBase]:
    """Tools for the brainstorming smol sub-agent (brainstorming domain + doc research reads)."""
    from plugin.framework.constants import document_research_uses_embeddings

    registry = ctx.services.get("tools")
    primary = registry.get_tools(doc=ctx.doc, doc_type=ctx.doc_type, active_domain="brainstorming", exclude_tiers=())
    doc_res = registry.get_tools(doc=ctx.doc, doc_type=ctx.doc_type, active_domain="document_research", exclude_tiers=())
    allow = set(_BRAINSTORMING_DOC_RESEARCH_TOOL_NAMES)
    if not document_research_uses_embeddings():
        allow.discard("search_embeddings")
    by_name = {t.name: t for t in primary if t.name}
    for t in doc_res:
        if t.name in allow and t.name not in by_name:
            by_name[t.name] = t
    return list(by_name.values())


class BrainstormResearchWeb(ToolWriterBrainstormingBase):
    """Web research for brainstorming (public topics); returns plain text for the sub-agent to format as HTML."""

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


class SaveDesignSpec(ToolWriterBrainstormingBase):
    """Write the approved design spec into the active Writer document (HTML array only)."""

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
        content = _normalize_html_content_array(kwargs.get("content"))
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


class BrainstormingFinishedTool(ToolBase):
    """Ends the brainstorming session and returns control to the main assistant."""

    name = "brainstorming_finished"
    description = "Ends brainstorming after the design spec is saved and reviewed. message must be HTML."
    tier = "specialized_control"
    is_final_answer_tool = True
    is_mutation = False
    parameters = {
        "type": "object",
        "properties": {
            "message": {"type": "string", "description": "HTML handoff message for the chat sidebar."},
            "spec_saved": {"type": "boolean", "description": "True if save_design_spec was called this session."},
        },
        "required": ["message"],
    }

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        from plugin.framework.i18n import _

        message = kwargs.get("message") or _("Brainstorming complete.")
        spec_saved = bool(kwargs.get("spec_saved", False))
        return {"status": "finished", "result": str(message), "spec_saved": spec_saved}


def _run_brainstorming_agent(ctx: ToolContext, *, query: str, history_text: str | None, topic: str | None) -> dict[str, Any]:
    """Run one turn of the brainstorming smol sub-agent."""
    from plugin.framework.errors import format_error_payload, ToolExecutionError
    from plugin.chatbot.smol_agent import SmolToolAdapter, build_toolcalling_agent
    from plugin.contrib.smolagents.memory import ActionStep, FinalAnswerStep, ToolCall
    from plugin.chatbot.smol_examples import get_examples_block
    from plugin.framework.constants import get_brainstorming_sub_agent_instructions

    status_callback = getattr(ctx, "status_callback", None)
    append_thinking_callback = getattr(ctx, "append_thinking_callback", None)
    chat_append_callback = getattr(ctx, "chat_append_callback", None)
    stop_checker = getattr(ctx, "stop_checker", None)

    if history_text and len(history_text) > 4000:
        history_text = "..." + history_text[-4000:]

    if status_callback:
        status_callback("Brainstorming...")

    domain_tools = collect_brainstorming_tools(ctx)
    finish_tool = BrainstormingFinishedTool()
    smol_tools = [SmolToolAdapter(t, ctx, safe=True, main_thread_sync=True, inputs_style="specialized") for t in domain_tools]
    smol_tools.append(SmolToolAdapter(finish_tool, ctx, safe=False, inputs_style="librarian"))

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
    final_ans = None
    document_open_step_index = 0

    run_stream = cast("Iterable", agent.run(task, stream=True))
    for step in run_stream:
        if stop_checker and stop_checker():
            return format_error_payload(ToolExecutionError("Brainstorming stopped by user.", code="USER_STOPPED"))
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
                        spec_match = re.search(r"'spec_saved': (True|False)", obs_str) or re.search(r'"spec_saved": (true|false)', obs_str, re.I)
                        spec_saved = spec_match.group(1).lower() == "true" if spec_match else False
                        append_thinking_callback(msg + "\n")
                        return {"status": "finished", "result": handoff or "Brainstorming complete.", "spec_saved": spec_saved}
                append_thinking_callback(msg + "\n")
            elif step.observations:
                obs_str = str(step.observations)
                if "'status': 'finished'" in obs_str or '"status": "finished"' in obs_str:
                    match = re.search(r"'result': '([^']*)'", obs_str) or re.search(r'"result": "([^"]*)"', obs_str)
                    handoff = match.group(1) if match else None
                    return {"status": "finished", "result": handoff or "Brainstorming complete.", "spec_saved": False}
        elif isinstance(step, FinalAnswerStep):
            final_ans = step.output

    return {"status": "ok", "result": str(final_ans)}


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
        from plugin.framework.errors import format_error_payload, ToolExecutionError

        query = kwargs.get("query")
        try:
            return _run_brainstorming_agent(
                ctx,
                query=str(query or ""),
                history_text=kwargs.get("history_text"),
                topic=kwargs.get("topic"),
            )
        except Exception as e:
            tb = traceback.format_exc()
            log.error("Brainstorming error: %s", e)
            err = ToolExecutionError(f"Brainstorming failed: {str(e)}\n\n{tb}", details={"query": query})
            return format_error_payload(err)


def start_brainstorming_session_from_delegate(ctx: ToolContext, *, task: str) -> dict[str, Any]:
    """First turn when main agent delegates domain=brainstorming."""
    topic = str(task or "").strip()
    return _run_brainstorming_agent(ctx, query=topic or "Let's brainstorm.", history_text=None, topic=topic)
