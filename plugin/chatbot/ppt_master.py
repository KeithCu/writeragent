# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""PPT-Master sidebar sub-agent (Impress/Draw only; venv-hosted smol loop)."""

from __future__ import annotations

from typing import Any, Callable, TYPE_CHECKING

from plugin.chatbot.sub_agent_tools import SubAgentSessionToolBase, _session_tool_parameters

if TYPE_CHECKING:
    from plugin.framework.tool import ToolContext


def _run_ppt_master_venv_agent(
    ctx: ToolContext,
    *,
    query: str = "",
    history_text: str | None = None,
    topic: str | None = None,
) -> dict[str, Any]:
    from plugin.framework.errors import ToolExecutionError, format_error_payload
    from plugin.ppt_master.venv.host import run_ppt_master_venv_turn
    from plugin.scripting.session_manager import ppt_master_session_id

    status_callback = getattr(ctx, "status_callback", None)
    append_thinking_callback = getattr(ctx, "append_thinking_callback", None)
    stop_checker = getattr(ctx, "stop_checker", None)

    if status_callback:
        status_callback("PPT-Master...")

    from plugin.framework.uno_context import get_active_document, get_ctx
    from plugin.framework.queue_executor import run_on_main

    # The tool runs on a background thread (is_async=True). ctx.doc and
    # get_active_document() are main-thread-only; run_on_main keeps
    # that access on VCL.
    def _resolve_session() -> str:
        uno_doc = ctx.doc if hasattr(ctx.doc, "getURL") else get_active_document(get_ctx())
        return ppt_master_session_id(uno_doc)

    session_id = run_on_main(_resolve_session)

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
        model=None,
        session_id=session_id,
        on_worker_event=on_worker_event,
        stop_checker=stop_checker,
        cancellation_scope=getattr(ctx, "send_cancellation", None),
    )


class PptMasterSessionTool(SubAgentSessionToolBase):
    name: str | None = "ppt_master_session"
    description: str = "PPT-Master presentation workflow sub-agent (venv worker + host UNO export)."
    agent_label: str = "PPT-Master"
    runner: Callable[..., dict[str, Any]] | None = staticmethod(_run_ppt_master_venv_agent)
    parameters: dict[str, Any] | None = _session_tool_parameters(
        "Original deck topic.",
        query_description="User message.",
        history_description="Prior conversation.",
    )
