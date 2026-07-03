# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Monaco editor IPC protocol (pickle protocol 5) and failure formatting for user-visible dialogs."""

from __future__ import annotations

import traceback
from typing import Any, IO

from plugin.scripting.ipc import IpcFrameError, pack_pickle_frame, read_frame_payload, unpack_pickle_frame

EDITOR_DEFAULT_TITLE = " "

# Cap payloads to avoid accidental OOM from a corrupted length header.
_MAX_PAYLOAD_BYTES = 16 * 1024 * 1024


def read_message(stream: IO[bytes]) -> dict[str, Any] | None:
    """Read one pickle-framed message from *stream*. Returns None on clean EOF."""
    payload = read_frame_payload(stream, max_payload_bytes=_MAX_PAYLOAD_BYTES, frame_label="editor message")
    if payload is None:
        return None
    try:
        decoded = unpack_pickle_frame(payload)
    except ValueError as e:
        raise ValueError(f"Invalid editor message pickle: {e}") from e
    if not isinstance(decoded, dict):
        raise ValueError("Editor message must be a dict")
    return decoded


def write_message(stream: IO[bytes], message: dict[str, Any]) -> None:
    """Write one dict to *stream* as pickle protocol 5 with a 4-byte big-endian length prefix."""
    try:
        frame = pack_pickle_frame(message, max_payload_bytes=_MAX_PAYLOAD_BYTES)
    except IpcFrameError as exc:
        raise ValueError("Editor message exceeds maximum payload size") from exc
    stream.write(frame)
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
