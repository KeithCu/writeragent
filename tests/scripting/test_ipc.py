# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for shared subprocess IPC framing helpers."""

from __future__ import annotations

import io
import logging
import os
import pickle
import subprocess
from unittest.mock import MagicMock

import pytest

from plugin.scripting.ipc import (
    DEFAULT_MAX_PAYLOAD_BYTES,
    IpcFrameError,
    pack_pickle_frame,
    read_frame_payload,
    read_json_line,
    read_pickle_frame,
    read_pickle_frame_with_timeout,
    unpack_pickle_frame,
    write_json_line,
    write_pickle_frame,
)


def test_pickle_frame_roundtrip_with_bytes():
    buf = io.BytesIO()
    write_pickle_frame(buf, {"status": "ok", "buffer": b"\x00\x01split"})
    buf.seek(0)
    assert read_pickle_frame(buf, require_dict=True) == {"status": "ok", "buffer": b"\x00\x01split"}


def test_unpack_rejects_reduce_gadget():
    class Boom:
        def __reduce__(self):
            return (eval, ("1+1",))

    payload = pickle.dumps(Boom(), protocol=5)
    with pytest.raises(ValueError, match="not allowed"):
        unpack_pickle_frame(payload)


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


def test_pickle_frame_default_cap_rejects_oversized_header():
    import struct

    oversized = struct.pack("!I", DEFAULT_MAX_PAYLOAD_BYTES + 1) + b"x"
    with pytest.raises(IpcFrameError, match="Invalid IPC frame size"):
        read_pickle_frame(io.BytesIO(oversized), max_payload_bytes=DEFAULT_MAX_PAYLOAD_BYTES)


def test_text_error_prefix_is_invalid_frame_with_header_repr(caplog, capsys):
    """Garbage length prefix keeps stdout_rest= and logs at error, not stderr."""
    with caplog.at_level(logging.ERROR, logger="writeragent.scripting.ipc"):
        with pytest.raises(IpcFrameError, match=r"header=b'Erro'.*stdout_rest=b'r: boom\\n'"):
            read_pickle_frame(
                io.BytesIO(b"Error: boom\n"),
                max_payload_bytes=DEFAULT_MAX_PAYLOAD_BYTES,
            )
    assert "stdout_rest=b'r: boom\\n'" in caplog.text
    err = capsys.readouterr().err
    assert "ipc leftover peek" not in err
    assert "peek_skipped" not in err


def test_unread_pipe_bytes_skips_set_blocking_on_win32(monkeypatch, caplog):
    """Windows/ty: os.set_blocking is POSIX-only; skip the non-blocking peek."""
    from plugin.scripting import ipc

    monkeypatch.setattr(ipc.sys, "platform", "win32")

    def boom(*_args, **_kwargs):
        raise AssertionError("os.set_blocking must not run on win32")

    monkeypatch.setattr(ipc.os, "set_blocking", boom)
    stream = MagicMock()
    stream.fileno.return_value = 3
    with caplog.at_level(logging.INFO, logger="writeragent.scripting.ipc"):
        assert ipc._unread_pipe_bytes(stream) == b""
    assert "leftover peek skipped" not in caplog.text
    stream.read.assert_not_called()


def test_invalid_frame_includes_stdout_rest_when_win32_peek_skipped(monkeypatch, caplog, capsys):
    """Win32 skip still attaches stdout_rest= (empty) on IpcFrameError."""
    from plugin.scripting import ipc

    monkeypatch.setattr(ipc.sys, "platform", "win32")

    def boom(*_args, **_kwargs):
        raise AssertionError("os.set_blocking must not run on win32")

    monkeypatch.setattr(ipc.os, "set_blocking", boom)
    stream = MagicMock()
    stream.fileno.return_value = 3
    stream.read.return_value = b"Erro"
    with caplog.at_level(logging.ERROR, logger="writeragent.scripting.ipc"):
        with pytest.raises(IpcFrameError, match=r"header=b'Erro'.*stdout_rest=b''"):
            read_pickle_frame(stream, max_payload_bytes=DEFAULT_MAX_PAYLOAD_BYTES)
    assert "stdout_rest=b''" in caplog.text
    err = capsys.readouterr().err
    assert "ipc leftover peek" not in err
    assert "peek_skipped" not in err
    stream.read.assert_called_once_with(4)


def test_unread_pipe_bytes_posix_peek_uses_set_blocking(monkeypatch):
    """Unix leftover peek still uses set_blocking; mock so Windows pytest can run it."""
    from plugin.scripting import ipc

    monkeypatch.setattr(ipc.sys, "platform", "linux")
    seen: list[tuple[int, bool]] = []

    def fake_set_blocking(fd: int, blocking: bool) -> None:
        seen.append((fd, blocking))

    monkeypatch.setattr(ipc.os, "set_blocking", fake_set_blocking)
    monkeypatch.setattr(ipc.os, "read", lambda fd, n: b"rest")
    stream = MagicMock()
    stream.fileno.return_value = 7
    assert ipc._unread_pipe_bytes(stream) == b"rest"
    assert seen == [(7, False)]
    stream.read.assert_not_called()


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


def test_pickle_frame_timeout_on_pipe():
    read_fd, write_fd = os.pipe()
    try:
        with os.fdopen(read_fd, "rb", buffering=0) as reader:
            with pytest.raises(subprocess.TimeoutExpired):
                read_pickle_frame_with_timeout(reader, 0.05)
    finally:
        os.close(write_fd)


def test_json_line_timeout_on_pipe():
    read_fd, write_fd = os.pipe()
    try:
        with os.fdopen(read_fd, "r", encoding="utf-8") as reader:
            with pytest.raises(subprocess.TimeoutExpired):
                read_json_line(reader, timeout_sec=0.01)
    finally:
        os.close(write_fd)


def test_win32_pickle_read_timeout_clamps_when_peek_crosses_deadline(monkeypatch):
    """PeekNamedPipe can finish after the deadline; sleep(negative) is ValueError."""
    from plugin.scripting import ipc

    slept: list[float] = []
    monkeypatch.setattr(ipc.time, "sleep", lambda sec: slept.append(sec))
    times = iter([0.0, 0.009, 0.011, 0.011])
    monkeypatch.setattr(ipc.time, "monotonic", lambda: next(times))
    monkeypatch.setattr(ipc, "_peek_pipe_bytes_available", lambda fd: 0)
    stream = MagicMock()
    stream.fileno.return_value = 3
    with pytest.raises(subprocess.TimeoutExpired):
        ipc._read_bytes_with_timeout_win32(stream, 4, 0.01, cmd="IPC frame")
    assert slept == [0.0]
    stream.read.assert_not_called()


def test_win32_readline_sleep_clamps_when_peek_crosses_deadline(monkeypatch):
    """PeekNamedPipe can finish after the deadline; sleep(negative) is ValueError."""
    from plugin.scripting import ipc

    slept: list[float] = []
    monkeypatch.setattr(ipc.time, "sleep", lambda sec: slept.append(sec))
    times = iter([0.0, 0.009, 0.011, 0.011])
    monkeypatch.setattr(ipc.time, "monotonic", lambda: next(times))
    monkeypatch.setattr(ipc, "_peek_pipe_bytes_available", lambda fd: 0)
    stream = MagicMock()
    stream.fileno.return_value = 3
    with pytest.raises(subprocess.TimeoutExpired):
        ipc._readline_with_timeout_win32(stream, 0.01)
    assert slept == [0.0]


def test_json_line_timeout_falls_back_when_fileno_not_int():
    """Non-int fileno() (e.g. MagicMock) must use readline, not PeekNamedPipe/select."""
    stream = MagicMock()
    stream.fileno.return_value = MagicMock()  # not an int
    stream.readline.return_value = '{"status": "ready"}\n'
    assert read_json_line(stream, timeout_sec=0.01) == {"status": "ready"}
    stream.readline.assert_called_once()
