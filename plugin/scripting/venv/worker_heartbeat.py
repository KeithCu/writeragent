# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Length-prefixed heartbeat/result frames on the venv worker stdout pipe."""
from __future__ import annotations

import sys
from typing import Any, BinaryIO, Callable

from plugin.scripting.ipc import read_frame_payload, unpack_pickle_frame, write_pickle_frame

FRAME_HEARTBEAT = "heartbeat"
FRAME_RESULT = "result"


def write_frame(stream: BinaryIO, payload: dict[str, Any]) -> None:
    """Write one pickle frame (4-byte big-endian length prefix)."""
    write_pickle_frame(stream, payload)


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

    return read_frame_payload(stream, read_exact=lambda nbytes: read_exact(stream, nbytes, deadline))


def parse_frame(frame_bytes: bytes) -> dict[str, Any]:
    if not frame_bytes:
        return {}
    data = unpack_pickle_frame(frame_bytes)
    return data if isinstance(data, dict) else {}
