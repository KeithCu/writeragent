# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Length-prefixed heartbeat/result frames on the venv worker stdout pipe."""
from __future__ import annotations

import pickle
import struct
import sys
from typing import Any, BinaryIO, Callable

FRAME_HEARTBEAT = "heartbeat"
FRAME_RESULT = "result"


def write_frame(stream: BinaryIO, payload: dict[str, Any]) -> None:
    """Write one pickle frame (4-byte big-endian length prefix)."""
    out = pickle.dumps(payload, protocol=5)
    stream.write(struct.pack("!I", len(out)))
    stream.write(out)
    stream.flush()


class HeartbeatEmitter:
    """Emit heartbeat frames on the worker stdout pipe during long trusted jobs."""

    def __init__(self, stream: BinaryIO | None = None) -> None:
        self._stream = stream if stream is not None else sys.stdout.buffer

    def emit(self, payload: dict[str, Any]) -> None:
        write_frame(self._stream, {"frame_type": FRAME_HEARTBEAT, "payload": dict(payload)})


def write_result_frame(stream: BinaryIO, response: dict[str, Any]) -> None:
    """Write the terminal result frame for a heartbeat-enabled worker request."""
    frame = dict(response)
    frame["frame_type"] = FRAME_RESULT
    write_frame(stream, frame)


def read_frame(stream: BinaryIO, *, deadline: float, read_exact: Callable[[BinaryIO, int, float], bytes]) -> bytes | None:
    """Read one length-prefixed frame before *deadline*; return None on timeout/EOF."""

    header = read_exact(stream, 4, deadline)
    if len(header) < 4:
        return None
    size = struct.unpack("!I", header)[0]
    return read_exact(stream, size, deadline)


def parse_frame(frame_bytes: bytes) -> dict[str, Any]:
    if not frame_bytes:
        return {}
    data = pickle.loads(frame_bytes)  # nosec B301 — trusted worker child
    return data if isinstance(data, dict) else {}
