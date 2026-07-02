# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""PPT-Master sidebar sub-agent (Impress/Draw only; venv-hosted smol loop)."""

from __future__ import annotations

import logging
import traceback
from typing import Any

from plugin.draw.base import ToolDrawPptMasterBase
from plugin.framework.tool import ToolBase, ToolContext

log = logging.getLogger(__name__)


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
    query: str,
    history_text: str | None,
    topic: str | None,
    model: str | None = None,
) -> dict[str, Any]:
    from plugin.framework.errors import ToolExecutionError, format_error_payload
    from plugin.ppt_master.venv.host import ppt_master_session_id, run_ppt_master_venv_turn

    status_callback = getattr(ctx, "status_callback", None)
    append_thinking_callback = getattr(ctx, "append_thinking_callback", None)
    stop_checker = getattr(ctx, "stop_checker", None)

    if status_callback:
        status_callback("PPT-Master...")

    from plugin.framework.uno_context import get_active_document, get_ctx
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
        from plugin.framework.errors import ToolExecutionError, format_error_payload

        query = kwargs.get("query")
        try:
            return _run_ppt_master_venv_agent(
                ctx,
                query=str(query or ""),
                history_text=kwargs.get("history_text"),
                topic=kwargs.get("topic"),
                model=kwargs.get("model"),
            )
        except Exception as e:
            tb = traceback.format_exc()
            log.error("PPT-Master error: %s", e)
            err = ToolExecutionError(f"PPT-Master failed: {str(e)}\n\n{tb}", details={"query": query})
            return format_error_payload(err)


# Re-export for tests that import ppt-master domain tool base.
__all__ = ["PptMasterSessionTool", "ToolDrawPptMasterBase"]
