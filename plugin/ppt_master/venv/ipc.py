# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Venv-side IPC helpers (stdout pipe to LibreOffice host)."""

from __future__ import annotations

import sys
import uuid
from typing import Any

from plugin.scripting.ipc import read_pickle_frame, write_pickle_frame


def _write_frame(payload: dict[str, Any]) -> None:
    write_pickle_frame(sys.stdout.buffer, payload)


def _read_host_response(context: str) -> dict[str, Any]:
    response = read_pickle_frame(sys.stdin.buffer, require_dict=True)
    if response is None:
        raise ConnectionError(f"Lost connection to LibreOffice host during {context}")
    return response


def emit_worker_event(event: dict[str, Any]) -> None:
    _write_frame({"type": "worker_event", "event": event})


def rpc_tool(tool_name: str, **kwargs: Any) -> Any:
    """Call a WriterAgent host tool via the worker IPC protocol."""
    call_id = str(uuid.uuid4())
    _write_frame({"type": "tool_call", "id": call_id, "tool": tool_name, "args": kwargs})
    response = _read_host_response("tool call")
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
    if response.get("status") == "error":
        raise RuntimeError(response.get("message", "LLM request failed"))
    result = response.get("result")
    if not isinstance(result, dict):
        return {"role": "assistant", "content": "", "tool_calls": None}
    return result
