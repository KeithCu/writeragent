# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Length-prefixed heartbeat/result frames on the venv worker stdout pipe."""
from __future__ import annotations

from typing import Any, BinaryIO

from plugin.scripting.ipc import (
    DEFAULT_MAX_PAYLOAD_BYTES,
    FRAME_HEARTBEAT,
    FRAME_RESULT,
    write_child_ipc_frame,
)


class HeartbeatEmitter:
    """Emit heartbeat frames on the worker stdout pipe during long trusted jobs."""

    _stream: BinaryIO

    def __init__(self, stream: BinaryIO | Any) -> None:
        self._stream = stream

    def emit(self, payload: dict[str, Any]) -> None:
        # Heartbeat and result frames share _tool_call_lock with exchange_tool_call.
        # _write_all's header and payload are separate writes; a second thread in
        # a host tool_call would otherwise split the length prefix. The lock is not
        # reentrant: do not call emit on the thread that is inside exchange_tool_call.
        write_child_ipc_frame(
            self._stream,
            {"frame_type": FRAME_HEARTBEAT, "payload": dict(payload)},
            max_payload_bytes=DEFAULT_MAX_PAYLOAD_BYTES,
        )


def write_result_frame(stream: BinaryIO, response: dict[str, Any]) -> None:
    """Write the terminal result frame for a heartbeat-enabled worker request."""
    frame = dict(response)
    frame["frame_type"] = FRAME_RESULT
    write_child_ipc_frame(stream, frame, max_payload_bytes=DEFAULT_MAX_PAYLOAD_BYTES)
