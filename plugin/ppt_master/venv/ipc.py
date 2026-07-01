# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Venv-side IPC helpers (stdout pipe to LibreOffice host)."""

from __future__ import annotations

import pickle
import struct
import sys
import uuid
from typing import Any


def _write_frame(payload: dict[str, Any]) -> None:
    out = pickle.dumps(payload, protocol=5)
    stream = sys.stdout.buffer
    stream.write(struct.pack("!I", len(out)))
    stream.write(out)
    stream.flush()


def emit_worker_event(event: dict[str, Any]) -> None:
    _write_frame({"type": "worker_event", "event": event})


def rpc_tool(tool_name: str, **kwargs: Any) -> Any:
    """Call a WriterAgent host tool via the worker IPC protocol."""
    call_id = str(uuid.uuid4())
    _write_frame({"type": "tool_call", "id": call_id, "tool": tool_name, "args": kwargs})
    header = sys.stdin.buffer.read(4)
    if not header or len(header) < 4:
        raise ConnectionError("Lost connection to LibreOffice host during tool call")
    size = struct.unpack("!I", header)[0]
    resp_payload = sys.stdin.buffer.read(size)
    response = pickle.loads(resp_payload)  # nosec B301
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
    header = sys.stdin.buffer.read(4)
    if not header or len(header) < 4:
        raise ConnectionError("Lost connection to LibreOffice host during LLM request")
    size = struct.unpack("!I", header)[0]
    resp_payload = sys.stdin.buffer.read(size)
    response = pickle.loads(resp_payload)  # nosec B301
    if response.get("status") == "error":
        raise RuntimeError(response.get("message", "LLM request failed"))
    result = response.get("result")
    if not isinstance(result, dict):
        return {"role": "assistant", "content": "", "tool_calls": None}
    return result
