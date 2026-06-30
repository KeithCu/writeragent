# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""PPT-Master sidebar sub-agent (Impress/Draw only; hidden from main chat tool list)."""

from __future__ import annotations

import logging
import re
import traceback
from typing import Any, ClassVar, Iterable, cast

from plugin.draw.base import ToolDrawPptMasterBase
from plugin.framework.tool import ToolBase, ToolContext

log = logging.getLogger(__name__)

# Draw/Impress tools merged into PPT-Master sessions (build decks with existing APIs).
_PPT_MASTER_DRAW_CORE_TOOL_NAMES = frozenset(
    {
        "add_slide",
        "delete_slide",
        "read_slide_text",
        "get_presentation_info",
        "set_active_page",
        "list_pages",
        "get_draw_tree",
        "list_placeholders",
        "get_placeholder_text",
        "set_placeholder_text",
        "get_document_content",
        "get_document_tree",
        "search_in_document",
    }
)

_PPT_MASTER_SHAPE_TOOL_NAMES = frozenset(
    {
        "upsert_shape",
        "delete_shape",
        "get_draw_summary",
        "shapes_connect",
        "shapes_group",
    }
)

_PPT_MASTER_NOTES_TOOL_NAMES = frozenset(
    {
        "get_speaker_notes",
        "set_speaker_notes",
    }
)


def collect_ppt_master_tools(ctx: ToolContext) -> list[ToolBase]:
    """Tools for the PPT-Master smol sub-agent: draw/impress + ppt-master domain."""
    registry = ctx.services.get("tools")
    by_name: dict[str, ToolBase] = {}

    pm = registry.get_tools(
        doc=ctx.doc,
        doc_type=ctx.doc_type,
        uno_services_supported=ctx.uno_services_supported,
        active_domain="ppt-master",
        exclude_tiers=(),
    )
    for t in pm:
        if t.name:
            by_name[t.name] = t

    core = registry.get_tools(
        doc=ctx.doc,
        doc_type=ctx.doc_type,
        uno_services_supported=ctx.uno_services_supported,
        exclude_tiers=("specialized", "specialized_control", "mcp"),
    )
    for t in core:
        if t.name in _PPT_MASTER_DRAW_CORE_TOOL_NAMES and t.name not in by_name:
            by_name[t.name] = t

    for domain, allow in (
        ("shapes", _PPT_MASTER_SHAPE_TOOL_NAMES),
        ("speaker_notes", _PPT_MASTER_NOTES_TOOL_NAMES),
    ):
        spec = registry.get_tools(
            doc=ctx.doc,
            doc_type=ctx.doc_type,
            uno_services_supported=ctx.uno_services_supported,
            active_domain=domain,
            exclude_tiers=(),
        )
        for t in spec:
            if t.name in allow and t.name not in by_name:
                by_name[t.name] = t

    return list(by_name.values())


class PptMasterFinishedTool(ToolBase):
    name = "ppt_master_finished"
    description = "End the PPT-Master session after the deck is exported or the user is done. message must be HTML."
    tier = "specialized_control"
    is_final_answer_tool = True
    is_mutation = False
    parameters = {
        "type": "object",
        "properties": {
            "message": {"type": "string", "description": "HTML handoff for the chat sidebar."},
            "exported": {"type": "boolean", "description": "True if export_presentation_project succeeded this session."},
        },
        "required": ["message"],
    }

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        from plugin.framework.i18n import _

        message = kwargs.get("message") or _("PPT-Master session complete.")
        exported = bool(kwargs.get("exported", False))
        return {"status": "finished", "result": str(message), "exported": exported}


def _run_ppt_master_agent(ctx: ToolContext, *, query: str, history_text: str | None, topic: str | None) -> dict[str, Any]:
    from plugin.chatbot.smol_agent import SmolToolAdapter, build_toolcalling_agent
    from plugin.chatbot.smol_examples import get_examples_block
    from plugin.contrib.smolagents.memory import ActionStep, FinalAnswerStep, ToolCall
    from plugin.framework.constants import get_ppt_master_sub_agent_instructions
    from plugin.framework.errors import ToolExecutionError, format_error_payload
    from plugin.ppt_master.paths import apply_data_root_env

    apply_data_root_env(ctx.ctx)

    status_callback = getattr(ctx, "status_callback", None)
    append_thinking_callback = getattr(ctx, "append_thinking_callback", None)
    stop_checker = getattr(ctx, "stop_checker", None)

    if history_text and len(history_text) > 4000:
        history_text = "..." + history_text[-4000:]

    if status_callback:
        status_callback("PPT-Master...")

    domain_tools = collect_ppt_master_tools(ctx)
    finish_tool = PptMasterFinishedTool()
    smol_tools = [SmolToolAdapter(t, ctx, safe=True, main_thread_sync=True, inputs_style="specialized") for t in domain_tools]
    smol_tools.append(SmolToolAdapter(finish_tool, ctx, safe=False, inputs_style="librarian"))

    instructions = get_ppt_master_sub_agent_instructions(ctx.ctx)
    if topic and topic.strip():
        instructions += f"\n\n[PPT-MASTER TOPIC]\n{topic.strip()}\n"

    agent = build_toolcalling_agent(
        ctx,
        smol_tools,
        instructions=instructions,
        final_answer_tool_name="reply_to_user",
        examples_block=get_examples_block("ppt-master"),
        status_callback=status_callback,
    )

    task = f"### CONVERSATION HISTORY:\n{history_text or 'None'}\n\n### CURRENT QUERY:\n{query}"
    final_ans = None

    run_stream = cast("Iterable", agent.run(task, stream=True))
    for step in run_stream:
        if stop_checker and stop_checker():
            return format_error_payload(ToolExecutionError("PPT-Master stopped by user.", code="USER_STOPPED"))
        if isinstance(step, ToolCall):
            if append_thinking_callback:
                append_thinking_callback(f"Running tool: {step.name} with {step.arguments}\n")
            if status_callback:
                status_callback(f"{step.name}...")
        elif isinstance(step, ActionStep):
            if append_thinking_callback:
                msg_parts = [f"Step {step.step_number}:\n"]
                if step.model_output:
                    mo = step.model_output
                    msg_parts.append(f"{(mo.strip() if isinstance(mo, str) else str(mo).strip())}\n")
                if step.observations:
                    msg_parts.append(f"Observation: {str(step.observations).strip()}\n")
                    obs_str = str(step.observations)
                    if "'status': 'finished'" in obs_str or '"status": "finished"' in obs_str:
                        match = re.search(r"'result': '([^']*)'", obs_str) or re.search(r'"result": "([^"]*)"', obs_str)
                        handoff = match.group(1) if match else None
                        exp_match = re.search(r"'exported': (True|False)", obs_str) or re.search(r'"exported": (true|false)', obs_str, re.I)
                        exported = exp_match.group(1).lower() == "true" if exp_match else False
                        msg_parts.append("\n")
                        append_thinking_callback("".join(msg_parts))
                        return {"status": "finished", "result": handoff or "PPT-Master complete.", "exported": exported}
                msg_parts.append("\n")
                append_thinking_callback("".join(msg_parts))
        elif isinstance(step, FinalAnswerStep):
            final_ans = step.output

    return {"status": "ok", "result": str(final_ans)}


class PptMasterSessionTool(ToolBase):
    name = "ppt_master_session"
    description = "PPT-Master presentation workflow sub-agent."
    tier = "specialized_control"
    is_mutation = False
    long_running = True
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "User message."},
            "history_text": {"type": "string", "description": "Prior conversation."},
            "topic": {"type": "string", "description": "Original deck topic."},
        },
        "required": ["query"],
    }

    def is_async(self) -> bool:
        return True

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        from plugin.framework.errors import ToolExecutionError, format_error_payload

        query = kwargs.get("query")
        try:
            return _run_ppt_master_agent(
                ctx,
                query=str(query or ""),
                history_text=kwargs.get("history_text"),
                topic=kwargs.get("topic"),
            )
        except Exception as e:
            tb = traceback.format_exc()
            log.error("PPT-Master error: %s", e)
            err = ToolExecutionError(f"PPT-Master failed: {str(e)}\n\n{tb}", details={"query": query})
            return format_error_payload(err)
