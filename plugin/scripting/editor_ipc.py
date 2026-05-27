# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Monaco editor IPC protocol and failure formatting for user-visible dialogs."""

from __future__ import annotations

import json
import struct
import traceback
from typing import Any, IO

EDITOR_DEFAULT_TITLE = " "

# Cap payloads to avoid accidental OOM from a corrupted length header.
_MAX_PAYLOAD_BYTES = 16 * 1024 * 1024


def read_message(stream: IO[bytes]) -> dict[str, Any] | None:
    """Read one message from *stream*. Returns None on clean EOF."""
    header = stream.read(4)
    if not header or len(header) < 4:
        return None
    size = struct.unpack("!I", header)[0]
    if size <= 0 or size > _MAX_PAYLOAD_BYTES:
        raise ValueError(f"Invalid editor message size: {size}")
    payload = stream.read(size)
    if len(payload) < size:
        return None
    decoded = json.loads(payload.decode("utf-8"))
    if not isinstance(decoded, dict):
        raise ValueError("Editor message must be a JSON object")
    return decoded


def write_message(stream: IO[bytes], message: dict[str, Any]) -> None:
    """Write one JSON object to *stream* with a 4-byte big-endian length prefix."""
    payload = json.dumps(message, ensure_ascii=False).encode("utf-8")
    if len(payload) > _MAX_PAYLOAD_BYTES:
        raise ValueError("Editor message exceeds maximum payload size")
    stream.write(struct.pack("!I", len(payload)))
    stream.write(payload)
    stream.flush()


def message_type(message: dict[str, Any]) -> str:
    """Return the ``type`` field or empty string."""
    raw = message.get("type")
    return str(raw) if raw is not None else ""


def exception_traceback(exc: BaseException) -> str:
    """Full traceback string for *exc*."""
    return "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))


def failure_detail(*, detail: str = "", exc: BaseException | None = None) -> str:
    """Combine subprocess stderr, probe output, and/or an exception traceback."""
    chunks: list[str] = []
    if detail.strip():
        chunks.append(detail.strip())
    if exc is not None:
        chunks.append(exception_traceback(exc).rstrip())
    return "\n\n".join(chunks)


def failure_message(summary: str, *, detail: str = "", exc: BaseException | None = None) -> str:
    """Build a msgbox body: *summary* plus optional detail/traceback blocks."""
    body = failure_detail(detail=detail, exc=exc)
    if body:
        return f"{summary}\n\n{body}"
    return summary
