# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for shared subprocess IPC framing helpers."""

from __future__ import annotations

import io
import os
import subprocess

import pytest

from plugin.scripting.ipc import (
    IpcFrameError,
    pack_pickle_frame,
    read_frame_payload,
    read_json_line,
    read_pickle_frame,
    unpack_pickle_frame,
    write_json_line,
    write_pickle_frame,
)


def test_pickle_frame_roundtrip():
    buf = io.BytesIO()
    write_pickle_frame(buf, {"status": "ok", "result": [1, 2, 3]})
    buf.seek(0)

    assert read_pickle_frame(buf, require_dict=True) == {"status": "ok", "result": [1, 2, 3]}


def test_pack_unpack_pickle_payload():
    frame = pack_pickle_frame({"type": "worker_event", "event": {"phase": "start"}})
    payload = read_frame_payload(io.BytesIO(frame))

    assert payload is not None
    assert unpack_pickle_frame(payload) == {"type": "worker_event", "event": {"phase": "start"}}


def test_truncated_pickle_frame_returns_none():
    payload = pack_pickle_frame({"status": "ok"})
    truncated = payload[:-2]

    assert read_pickle_frame(io.BytesIO(truncated)) is None


def test_pickle_frame_size_limit_raises():
    frame = pack_pickle_frame({"text": "x" * 100})

    with pytest.raises(IpcFrameError, match="Invalid test frame size"):
        read_frame_payload(io.BytesIO(frame), max_payload_bytes=8, frame_label="test frame")


def test_json_line_roundtrip():
    buf = io.StringIO()
    write_json_line(buf, {"status": "ready"})
    buf.seek(0)

    assert read_json_line(buf) == {"status": "ready"}


def test_invalid_json_line_raises():
    with pytest.raises(ValueError, match="Invalid JSON line"):
        read_json_line(io.StringIO("{not-json}\n"))


def test_json_line_non_object_raises():
    with pytest.raises(ValueError, match="must contain an object"):
        read_json_line(io.StringIO("[1, 2]\n"))


def test_json_line_timeout_on_pipe():
    read_fd, write_fd = os.pipe()
    try:
        with os.fdopen(read_fd, "r", encoding="utf-8") as reader:
            with pytest.raises(subprocess.TimeoutExpired):
                read_json_line(reader, timeout_sec=0.01)
    finally:
        os.close(write_fd)
