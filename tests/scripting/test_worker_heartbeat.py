# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for plugin.scripting.venv.worker_heartbeat."""

from __future__ import annotations

import io
import pickle
import struct

from plugin.scripting.venv.worker_heartbeat import (
    FRAME_HEARTBEAT,
    FRAME_RESULT,
    HeartbeatEmitter,
    parse_frame,
    write_frame,
    write_result_frame,
)


def test_write_and_parse_heartbeat_frame():
    buf = io.BytesIO()
    write_frame(buf, {"frame_type": FRAME_HEARTBEAT, "payload": {"phase": "extract"}})
    raw = buf.getvalue()
    size = struct.unpack("!I", raw[:4])[0]
    data = parse_frame(raw[4 : 4 + size])
    assert data["frame_type"] == FRAME_HEARTBEAT
    assert data["payload"]["phase"] == "extract"


def test_heartbeat_emitter_writes_frame():
    buf = io.BytesIO()
    HeartbeatEmitter(buf).emit({"phase": "embed", "paragraphs": 2})
    data = parse_frame(buf.getvalue()[4:])
    assert data["frame_type"] == FRAME_HEARTBEAT


def test_write_result_frame():
    buf = io.BytesIO()
    write_result_frame(buf, {"id": "1", "status": "ok", "result": {"mode": "cold"}})
    data = parse_frame(buf.getvalue()[4:])
    assert data["frame_type"] == FRAME_RESULT
    assert data["status"] == "ok"
