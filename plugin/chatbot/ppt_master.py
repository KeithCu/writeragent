# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""PPT-Master sidebar sub-agent (Impress/Draw only; venv-hosted smol loop)."""

from __future__ import annotations

import logging
from typing import Any

from plugin.framework.prompts import get_chat_response_format_instructions
from plugin.framework.tool import ToolBase, ToolContext

log = logging.getLogger(__name__)

# Live PPT-Master turns use the skill pack (SKILL.md), not this constant.
PPT_MASTER_SUB_AGENT_INSTRUCTIONS = """PPT-MASTER MODE (venv worker):
You run the upstream ppt-master workflow with filesystem + script access in the user Python venv.

WORKFLOW:
1. SKILL.md and routing files are pre-loaded; use read_ppt_master_workflow_file for references/ when needed.
2. Use run_ppt_master_script for upstream commands under scripts/ (project_manager, pdf_to_md, svg_to_pptx, etc.).
3. Use read_project_file / write_project_file for project artifacts (svg_output/, design_spec.md, …).
4. When exports are ready, call export_presentation_project on the host to import into the active Impress/Draw document.
5. validate_ppt_master_project checks project artifacts before export.
6. apply_ppt_master_template_fill and apply_ppt_master_native_enhance for template-fill and enhancement routes.

REQUIREMENTS:
- Configured user Python venv with ppt-master requirements.txt installed.
- PPT-Master data path must contain SKILL.md and scripts/.

HTML RULES:
- reply_to_user and ppt_master_finished messages must be HTML (see CHAT RESPONSE FORMAT).

COMPLETION:
- reply_to_user: continue the PPT-Master session.
- ppt_master_finished: end when the deck is done or the user switches back to Chat mode. Set exported=true if export_presentation_project succeeded."""


def get_ppt_master_sub_agent_instructions(ctx: Any | None = None) -> str:
    """Full system instructions for the PPT-Master smol sub-agent (Impress/Draw sidebar)."""
    parts = [
        PPT_MASTER_SUB_AGENT_INSTRUCTIONS,
        get_chat_response_format_instructions(ctx),
    ]
    return "\n\n".join(parts)


def _selected_chat_model(ctx: ToolContext) -> str | None:
    """Send handlers pass the sidebar model id via ToolContext.doc (not the UNO document)."""
    doc = ctx.doc
    if doc is None or hasattr(doc, "getURL"):
        return None
    text = str(doc).strip()
    return text or None


def _run_ppt_master_venv_agent(
    ctx: ToolContext,
    *,
    query: str = "",
    history_text: str | None = None,
    topic: str | None = None,
    model: str | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    from plugin.framework.errors import ToolExecutionError, format_error_payload
    from plugin.ppt_master.venv.host import ppt_master_session_id, run_ppt_master_venv_turn

    status_callback = getattr(ctx, "status_callback", None)
    append_thinking_callback = getattr(ctx, "append_thinking_callback", None)
    stop_checker = getattr(ctx, "stop_checker", None)

    if status_callback:
        status_callback("PPT-Master...")

    from plugin.framework.uno_context import get_active_document, get_ctx
    from plugin.framework.thread_guard import on_main_thread
    from plugin.framework.queue_executor import execute_on_main_thread

    # Bugfix: The tool runs on a background thread (is_async=True). Accessing ctx.doc,
    # calling get_active_document(), or calling getURL() off the main thread (including via
    # _selected_chat_model) causes a UNO thread safety violation. Wrapping these in
    # execute_on_main_thread ensures they execute safely on the main thread.
    def _resolve_session_and_model() -> tuple[str, str | None]:
        uno_doc = ctx.doc if hasattr(ctx.doc, "getURL") else get_active_document(get_ctx())
        sess_id = ppt_master_session_id(uno_doc)
        selected_model = _selected_chat_model(ctx)
        return sess_id, selected_model

    if on_main_thread():
        session_id, resolved_model = _resolve_session_and_model()
    else:
        session_id, resolved_model = execute_on_main_thread(_resolve_session_and_model)

    def on_worker_event(event: dict[str, Any]) -> None:
        kind = event.get("kind")
        if kind == "status" and status_callback:
            text = event.get("text")
            if text:
                status_callback(str(text))
        elif kind == "tool" and append_thinking_callback:
            append_thinking_callback(f"Running tool: {event.get('name')} {event.get('arguments', '')}\n")
        elif kind == "thinking" and append_thinking_callback:
            text = event.get("text")
            if text:
                append_thinking_callback(str(text))

    if stop_checker and stop_checker():
        return format_error_payload(ToolExecutionError("PPT-Master stopped by user.", code="USER_STOPPED"))

    return run_ppt_master_venv_turn(
        ctx.ctx,
        query=query,
        history_text=history_text,
        topic=topic,
        model=model or resolved_model,
        session_id=session_id,
        on_worker_event=on_worker_event,
        stop_checker=stop_checker,
    )


class PptMasterSessionTool(ToolBase):
    name = "ppt_master_session"
    description = "PPT-Master presentation workflow sub-agent (venv worker + host UNO export)."
    tier = "specialized_control"
    is_mutation = False
    long_running = True
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "User message."},
            "history_text": {"type": "string", "description": "Prior conversation."},
            "topic": {"type": "string", "description": "Original deck topic."},
            "model": {"type": "string", "description": "Sidebar model id (optional)."},
        },
        "required": ["query"],
    }

    def is_async(self) -> bool:
        return True

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        from plugin.chatbot.smol_agent import run_subagent_tool

        return run_subagent_tool("PPT-Master", _run_ppt_master_venv_agent, ctx, **kwargs)



__all__ = ["PptMasterSessionTool"]
