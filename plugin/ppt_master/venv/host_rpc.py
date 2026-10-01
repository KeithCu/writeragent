# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Host-side handlers for venv worker RPC (LLM + tool dispatch on main thread)."""

from __future__ import annotations

import logging
from typing import Any, Callable

from plugin.scripting.ipc import DEFAULT_MAX_PAYLOAD_BYTES, IpcFrameError, pack_pickle_frame

log = logging.getLogger(__name__)


def handle_llm_request(payload: dict[str, Any]) -> dict[str, Any]:
    """Run LlmClient.request_with_tools for a venv ppt-master turn (keys stay on host)."""
    from plugin.framework.client.llm_client import LlmClient
    from plugin.framework.config import get_api_config, get_config_int
    from plugin.framework.uno_context import get_ctx

    messages = payload.get("messages")
    if not isinstance(messages, list):
        return {"status": "error", "message": "llm_request requires messages list."}

    tools = payload.get("tools")
    model = payload.get("model")
    max_tokens = payload.get("max_tokens")
    if max_tokens is None:
        max_tokens = get_config_int("chat_max_tokens")

    stop_checker = payload.get("_stop_checker")
    if not callable(stop_checker):
        stop_checker = None

    from plugin.framework.queue_executor import execute_on_main_thread

    # Bugfix: handle_llm_request is called on a background worker thread.
    # get_ctx() is decorated with @main_thread_only, so calling it directly
    # triggers a thread safety violation. Wrapping it in execute_on_main_thread
    # marshals it safely to the main thread.
    ctx = execute_on_main_thread(get_ctx)
    client = LlmClient(get_api_config(), ctx)
    try:
        result = client.request_with_tools(
            messages,
            max_tokens=int(max_tokens),
            tools=tools if tools else None,
            model=model,
            prepend_dev_build_system_prefix=False,
            stop_checker=stop_checker,
        )
    except Exception as exc:
        log.exception("ppt-master llm_request failed")
        return {"status": "error", "message": str(exc)}

    return {
        "status": "ok",
        "result": {
            "role": result.get("role", "assistant"),
            "content": result.get("content") or "",
            "tool_calls": result.get("tool_calls"),
            "finish_reason": result.get("finish_reason"),
            "usage": result.get("usage"),
        },
    }


def dispatch_worker_response(
    response: dict[str, Any],
    *,
    stdin_write: Callable[[bytes], None],
    on_worker_event: Callable[[dict[str, Any]], None] | None = None,
    stop_checker: Callable[[], bool] | None = None,
) -> bool:
    """Handle intermediate worker frames. Returns True if caller should keep reading."""
    if not isinstance(response, dict):
        return False

    frame_type = response.get("type")
    if frame_type == "worker_event":
        event = response.get("event")
        if on_worker_event and isinstance(event, dict):
            on_worker_event(event)
        return True

    if frame_type == "tool_call":
        from plugin.scripting.host_rpc import handle_tool_call_frame

        return handle_tool_call_frame(response, stdin_write=stdin_write, caller="ppt_master_venv")

    if frame_type == "llm_request":
        call_id = response.get("id")
        payload = dict(response)
        payload.pop("type", None)
        payload.pop("id", None)
        if stop_checker is not None:
            payload["_stop_checker"] = stop_checker
        llm_out = handle_llm_request(payload)
        llm_response = {"status": llm_out.get("status", "error"), "id": call_id}
        if llm_out.get("status") == "ok":
            llm_response["result"] = llm_out.get("result")
        else:
            llm_response["message"] = llm_out.get("message", "LLM request failed")
        try:
            frame = pack_pickle_frame(llm_response, max_payload_bytes=DEFAULT_MAX_PAYLOAD_BYTES)
        except IpcFrameError as exc:
            # An oversized LLM reply used to be written with no cap. The child
            # read stops at DEFAULT_MAX_PAYLOAD_BYTES and leaves the rest on
            # the pipe, so the next frame is garbage. Send a small error frame.
            frame = pack_pickle_frame(
                {"status": "error", "id": call_id, "message": str(exc), "error": str(exc)},
                max_payload_bytes=DEFAULT_MAX_PAYLOAD_BYTES,
            )
        stdin_write(frame)
        return True

    return False
