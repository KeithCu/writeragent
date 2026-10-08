# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Length-prefixed heartbeat/result frames on the venv worker stdout pipe."""
from __future__ import annotations

from typing import Any, BinaryIO

from plugin.scripting.ipc import (
    DEFAULT_MAX_PAYLOAD_BYTES,
    get_child_ipc_stream,
    unpack_pickle_frame,
    write_pickle_frame,
)

FRAME_HEARTBEAT = "heartbeat"
FRAME_RESULT = "result"


class HeartbeatEmitter:
    """Emit heartbeat frames on the worker stdout pipe during long trusted jobs."""

    _stream: BinaryIO

    def __init__(self, stream: BinaryIO | None = None) -> None:
        self._stream = stream if stream is not None else get_child_ipc_stream()

    def emit(self, payload: dict[str, Any]) -> None:
        write_pickle_frame(
            self._stream,
            {"frame_type": FRAME_HEARTBEAT, "payload": dict(payload)},
            max_payload_bytes=DEFAULT_MAX_PAYLOAD_BYTES,
        )


def write_result_frame(stream: BinaryIO, response: dict[str, Any]) -> None:
    """Write the terminal result frame for a heartbeat-enabled worker request."""
    frame = dict(response)
    frame["frame_type"] = FRAME_RESULT
    write_pickle_frame(stream, frame, max_payload_bytes=DEFAULT_MAX_PAYLOAD_BYTES)


def parse_frame(frame_bytes: bytes) -> dict[str, Any]:
    if not frame_bytes:
        return {}
    data = unpack_pickle_frame(frame_bytes)
    return data if isinstance(data, dict) else {}
