# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Length-prefixed JSON messages for the Monaco editor child process (stdin/stdout).

Message types (``type`` field):

- ``ready`` child → LO
- ``load`` LO → child: ``code``, optional ``title``, ``data_binding``, ``plain_text_label``, optional ``save_as_plain`` (initial checkbox: off for ``=PYTHON()``, on for plain-string cells)
- ``save`` child → LO: ``code``, optional ``save_as_plain``, ``data_binding`` (range text for formula suffix)
- ``saved`` / ``error`` LO → child
- ``closed`` / ``cancel`` either direction
"""

from __future__ import annotations

import json
import struct
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
