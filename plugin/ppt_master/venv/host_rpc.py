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
    stop_checker = payload.get("_stop_checker")
    if not callable(stop_checker):
        stop_checker = None

    from plugin.framework.errors import ToolExecutionError
    from plugin.framework.queue_executor import execute_on_main_thread

    # Build the client on the send cancellation scope so sidebar Stop closes
    # this call too. The scope registers the live client and must not be
    # pickled to the child; dispatch attaches it on the way in, the same
    # way as _stop_checker.
    cancellation_scope = payload.get("_cancellation_scope")
    try:
        # get_ctx() is @main_thread_only, so a background worker marshals it
        # with execute_on_main_thread. Keep that call inside the try: a raise
        # must go back to the child as an RPC error, or the child waits until
        # timeout with no reply.
        ctx = execute_on_main_thread(get_ctx)
        if max_tokens is None:
            max_tokens = get_config_int("chat_max_tokens")

        client = LlmClient(get_api_config(), ctx, cancellation_scope=cancellation_scope)
        result = client.request_with_tools(
            messages,
            max_tokens=int(max_tokens),
            tools=tools if tools else None,
            model=model,
            prepend_dev_build_system_prefix=False,
            stop_checker=stop_checker,
        )
        if not isinstance(result, dict):
            raise TypeError("LLM response is not a dictionary")

        is_stopped = client._stopped
        if not is_stopped and stop_checker is not None:
            try:
                is_stopped = stop_checker()
            except Exception:
                log.exception("stop_checker raised in ppt_master handle_llm_request")
                return {"status": "error", "message": "stop_checker raised exception"}

        if is_stopped:
            return {"status": "error", "code": "USER_STOPPED", "message": "Stopped by user."}

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

    except ToolExecutionError as exc:
        # Preserve USER_STOPPED. A generic error string lets the child keep
        # the turn going after Stop.
        if getattr(exc, "code", None) == "USER_STOPPED":
            return {"status": "error", "code": "USER_STOPPED", "message": exc.message}
        log.exception("ppt-master llm_request failed")
        return {"status": "error", "message": str(exc)}
    except Exception as exc:
        log.exception("ppt-master llm_request failed")
        return {"status": "error", "message": str(exc)}


def dispatch_worker_response(
    response: dict[str, Any],
    *,
    stdin_write: Callable[[bytes], None],
    on_worker_event: Callable[[dict[str, Any]], None] | None = None,
    stop_checker: Callable[[], bool] | None = None,
    cancellation_scope: Any | None = None,
) -> bool:
    """Handle intermediate worker frames. Returns True if caller should keep reading."""
    if not isinstance(response, dict):
        return False

    frame_type = response.get("type")
    if frame_type == "worker_event":
        event = response.get("event")
        if on_worker_event and isinstance(event, dict):
            # A raising UI callback is logged and the read loop continues, same
            # as the heartbeat callback, so the pipe stays aligned. Escaping
            # the loop kills the worker after exec_started (_fail_no_replay)
            # or leaves the child blocked on an unread request.
            try:
                on_worker_event(event)
            except Exception:
                log.exception("on_worker_event failed (ignoring)")
        return True

    # Note: tool_call is not handled here because it's already handled first
    # by venv_worker._maybe_dispatch_intermediate_response.

    if frame_type == "llm_request":
        call_id = response.get("id")
        payload = dict(response)
        payload.pop("type", None)
        payload.pop("id", None)
        payload.pop("_stop_checker", None)
        payload.pop("_cancellation_scope", None)
        if stop_checker is not None:
            payload["_stop_checker"] = stop_checker
        if cancellation_scope is not None:
            payload["_cancellation_scope"] = cancellation_scope
        llm_out = handle_llm_request(payload)
        llm_response = {"status": llm_out.get("status", "error"), "id": call_id}
        if llm_out.get("status") == "ok":
            llm_response["result"] = llm_out.get("result")
        else:
            llm_response["message"] = llm_out.get("message", "LLM request failed")
            # Copy the handler's code through. The child raises UserStopped
            # only when code is USER_STOPPED; any other error is RuntimeError
            # and run_turn keeps taking steps until max_tool_rounds.
            if llm_out.get("code"):
                llm_response["code"] = llm_out["code"]
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
        except Exception as exc:
            # Unpicklable LLM reply.
            frame = pack_pickle_frame(
                {"status": "error", "id": call_id, "message": f"Result not serializable: {exc}", "error": f"Result not serializable: {exc}"},
                max_payload_bytes=DEFAULT_MAX_PAYLOAD_BYTES,
            )
        try:
            stdin_write(frame)
        except (OSError, ValueError):
            log.warning("venv llm_request reply failed (worker pipe closed)", exc_info=True)
        return True

    return False
