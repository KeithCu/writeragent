# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Venv-side IPC helpers (stdout pipe to LibreOffice host)."""

from __future__ import annotations

import sys
import uuid
from typing import Any

from plugin.scripting.ipc import (
    DEFAULT_MAX_PAYLOAD_BYTES,
    IpcFrameError,
    read_pickle_frame,
    write_pickle_frame,
)


def _write_frame(payload: dict[str, Any]) -> None:
    try:
        write_pickle_frame(sys.stdout.buffer, payload)
    except IpcFrameError as exc:
        # write_pickle_frame now defaults to the read cap. An omitted write
        # used to be uncapped, so a too-large child frame left unread bytes
        # on the host pipe. Reply with a small error frame instead.
        error_frame: dict[str, Any] = {
            "status": "error",
            "message": str(exc),
            "error": str(exc),
        }
        if "id" in payload:
            error_frame["id"] = payload["id"]
        write_pickle_frame(
            sys.stdout.buffer,
            error_frame,
            max_payload_bytes=DEFAULT_MAX_PAYLOAD_BYTES,
        )


def _read_host_response(context: str) -> dict[str, Any]:
    response = read_pickle_frame(
        sys.stdin.buffer, require_dict=True, max_payload_bytes=DEFAULT_MAX_PAYLOAD_BYTES
    )
    if response is None:
        raise ConnectionError(f"Lost connection to LibreOffice host during {context}")
    return response


class UserStopped(Exception):
    """Host reported Stop. The child turn must return USER_STOPPED.

    Smolagents catches Exception inside a tool and continues the loop, so
    ``run_turn`` also walks ``__cause__`` on the step error.
    """


def _raise_if_stopped(response: dict[str, Any]) -> None:
    # The host llm_request reply must include code USER_STOPPED. Without it
    # rpc_llm raises RuntimeError and run_turn keeps stepping.
    if response.get("code") == "USER_STOPPED":
        raise UserStopped(str(response.get("message") or "Stopped by user."))


def emit_worker_event(event: dict[str, Any]) -> None:
    _write_frame({"type": "worker_event", "event": event})


def rpc_tool(tool_name: str, **kwargs: Any) -> Any:
    """Call a WriterAgent host tool via the worker IPC protocol."""
    call_id = str(uuid.uuid4())
    _write_frame({"type": "tool_call", "id": call_id, "tool": tool_name, "args": kwargs})
    response = _read_host_response("tool call")
    _raise_if_stopped(response)
    if response.get("status") == "error":
        raise RuntimeError(response.get("message", "Tool call failed"))
    return response.get("result")


def rpc_llm(
    *,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    model: str | None = None,
    max_tokens: int | None = None,
) -> dict[str, Any]:
    """Request an LLM completion from the host (API keys remain on host)."""
    call_id = str(uuid.uuid4())
    frame: dict[str, Any] = {
        "type": "llm_request",
        "id": call_id,
        "messages": messages,
    }
    if tools:
        frame["tools"] = tools
    if model:
        frame["model"] = model
    if max_tokens is not None:
        frame["max_tokens"] = max_tokens
    _write_frame(frame)
    response = _read_host_response("LLM request")
    _raise_if_stopped(response)
    if response.get("status") == "error":
        raise RuntimeError(response.get("message", "LLM request failed"))
    result = response.get("result")
    if not isinstance(result, dict):
        return {"role": "assistant", "content": "", "tool_calls": None}
    return result
