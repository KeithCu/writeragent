# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for editor IPC protocol and failure formatting."""

from __future__ import annotations

import io
import json
import struct

import pytest

from plugin.scripting.editor_ipc import exception_traceback, failure_message, read_message, write_message


def test_roundtrip_simple():
    buf = io.BytesIO()
    write_message(buf, {"type": "ready", "version": 1})
    buf.seek(0)
    msg = read_message(buf)
    assert msg == {"type": "ready", "version": 1}


def test_roundtrip_unicode():
    buf = io.BytesIO()
    write_message(buf, {"type": "load", "code": "print('日本語')"})
    buf.seek(0)
    msg = read_message(buf)
    assert msg is not None
    assert msg["code"] == "print('日本語')"


def test_eof_returns_none():
    buf = io.BytesIO()
    assert read_message(buf) is None


def test_truncated_payload_returns_none():
    buf = io.BytesIO()
    buf.write(struct.pack("!I", 100))
    buf.write(b"short")
    buf.seek(0)
    assert read_message(buf) is None


def test_invalid_size_raises():
    buf = io.BytesIO()
    buf.write(struct.pack("!I", 32 * 1024 * 1024))
    buf.seek(0)
    with pytest.raises(ValueError, match="Invalid editor message size"):
        read_message(buf)


def test_exception_traceback_includes_frame():
    try:
        raise ValueError("probe failure")
    except ValueError as e:
        tb = exception_traceback(e)
    assert "ValueError: probe failure" in tb
    assert "test_exception_traceback_includes_frame" in tb


def test_failure_message_combines_summary_detail_and_trace():
    try:
        raise RuntimeError("boom")
    except RuntimeError as e:
        msg = failure_message("Summary", detail="stderr line", exc=e)
    assert msg.startswith("Summary\n\n")
    assert "stderr line" in msg
    assert "RuntimeError: boom" in msg
