# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for heartbeat and result frames in plugin.scripting.venv.worker_harness."""

from __future__ import annotations

import io
import threading

import plugin.scripting.ipc
from plugin.scripting.ipc import (
    FRAME_HEARTBEAT,
    FRAME_RESULT,
    read_frame_payload,
)
from plugin.scripting.venv_worker import parse_worker_stdout_frame
from plugin.scripting.venv.worker_harness import (
    emit_heartbeat,
    write_result_frame,
)


class _GateStream:
    def __init__(self, buf: io.BytesIO) -> None:
        self.buf = buf
        self.first_write_entered = threading.Event()
        self.release_write = threading.Event()

    def write(self, b: bytes) -> int:
        if not self.first_write_entered.is_set():
            self.first_write_entered.set()
            self.release_write.wait(timeout=5.0)
        return self.buf.write(b)

    def flush(self) -> None:
        self.buf.flush()


def test_write_and_parse_heartbeat_frame():
    buf = io.BytesIO()
    emit_heartbeat(buf, {"phase": "extract"})
    buf.seek(0)
    payload = read_frame_payload(buf)
    assert payload is not None
    data = parse_worker_stdout_frame(payload)
    assert data["frame_type"] == FRAME_HEARTBEAT
    assert data["payload"]["phase"] == "extract"


def test_heartbeat_emitter_writes_frame():
    buf = io.BytesIO()
    emit_heartbeat(buf, {"phase": "embed", "paragraphs": 2})
    buf.seek(0)
    payload = read_frame_payload(buf)
    assert payload is not None
    data = parse_worker_stdout_frame(payload)
    assert data["frame_type"] == FRAME_HEARTBEAT


def test_write_result_frame():
    buf = io.BytesIO()
    write_result_frame(buf, {"id": "1", "status": "ok", "result": {"mode": "cold"}})
    buf.seek(0)
    payload = read_frame_payload(buf)
    assert payload is not None
    data = parse_worker_stdout_frame(payload)
    assert data["frame_type"] == FRAME_RESULT
    assert data["status"] == "ok"


def test_emit_holds_tool_call_lock_across_the_write():
    buf = io.BytesIO()
    stream = _GateStream(buf)
    t = threading.Thread(target=lambda: emit_heartbeat(stream, {"phase": "a"}))
    t.start()
    try:
        assert stream.first_write_entered.wait(timeout=2.0)
        assert plugin.scripting.ipc._tool_call_lock.acquire(blocking=False) is False
    finally:
        stream.release_write.set()
        t.join(timeout=2.0)

    write_result_frame(stream, {"id": "9", "status": "ok", "result": {"n": 1}})
    buf.seek(0)
    p1 = read_frame_payload(buf)
    assert p1 is not None
    d1 = parse_worker_stdout_frame(p1)
    assert d1["frame_type"] == FRAME_HEARTBEAT
    assert d1["payload"]["phase"] == "a"

    p2 = read_frame_payload(buf)
    assert p2 is not None
    d2 = parse_worker_stdout_frame(p2)
    assert d2["frame_type"] == FRAME_RESULT
    assert d2["id"] == "9"
