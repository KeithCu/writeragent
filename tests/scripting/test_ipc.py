# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for shared subprocess IPC framing helpers."""

from __future__ import annotations

import io
import logging
import os
import signal
import pickle
import subprocess
import sys
import time
from unittest.mock import MagicMock

import pytest

from plugin.scripting.ipc import (
    DEFAULT_MAX_PAYLOAD_BYTES,
    IpcFrameError,
    IpcPartialFrameTimeout,
    pack_pickle_frame,
    read_frame_payload,
    read_json_line,
    read_pickle_frame,
    read_pickle_frame_with_timeout,
    unpack_pickle_frame,
    write_json_line,
    write_packed_frame_with_timeout,
    write_pickle_frame,
    write_pickle_frame_with_timeout,
)


def test_pickle_frame_roundtrip_with_bytes():
    buf = io.BytesIO()
    write_pickle_frame(buf, {"status": "ok", "buffer": b"\x00\x01split"})
    buf.seek(0)
    assert read_pickle_frame(buf, require_dict=True) == {"status": "ok", "buffer": b"\x00\x01split"}


def test_unpack_allows_numpy_reconstruct_and_rejects_ctypeslib():
    pytest.importorskip("numpy")
    import numpy as np

    buf = io.BytesIO()
    write_pickle_frame(buf, {"status": "ok", "result": np.array([1, 2, 3], dtype=np.int64)})
    buf.seek(0)
    decoded = read_pickle_frame(buf, require_dict=True)
    assert list(decoded["result"]) == [1, 2, 3]

    class LoadLibrary:
        def __reduce__(self):
            return (np.ctypeslib.load_library, ("no-such-lib", "."))

    payload = pickle.dumps(LoadLibrary(), protocol=5)
    with pytest.raises(ValueError, match="not allowed"):
        unpack_pickle_frame(payload)


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


def test_truncated_pickle_payload_is_value_error():
    """A length-valid truncated pickle is EOFError from load(); callers catch ValueError."""
    with pytest.raises(ValueError, match="Ran out of input"):
        unpack_pickle_frame(b"\x80\x04")


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


def test_pack_pickle_frame_defaults_to_max_payload():
    from plugin.scripting.ipc import DEFAULT_MAX_PAYLOAD_BYTES, IpcFrameError, pack_pickle_frame
    with pytest.raises(IpcFrameError, match="maximum payload"):
        pack_pickle_frame({"text": "x" * (DEFAULT_MAX_PAYLOAD_BYTES + 10)})
    frame = pack_pickle_frame({"ok": True}, max_payload_bytes=None)
    assert frame


def test_ipc_error_subclasses():
    from plugin.scripting.ipc import IpcFrameError, IpcFrameReadError, IpcPayloadSizeError

    assert issubclass(IpcPayloadSizeError, IpcFrameError)
    assert issubclass(IpcFrameReadError, IpcFrameError)

    with pytest.raises(IpcPayloadSizeError):
        pack_pickle_frame({"x": "a" * (DEFAULT_MAX_PAYLOAD_BYTES + 1)})

    with pytest.raises(IpcFrameReadError):
        read_frame_payload(io.BytesIO(b"\x00\x00\x00\x00"), frame_label="zero frame")


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

    monkeypatch.setattr(ipc.os, "get_blocking", lambda fd: True)
    monkeypatch.setattr(ipc.os, "set_blocking", fake_set_blocking)
    monkeypatch.setattr(ipc.os, "read", lambda fd, n: b"rest")
    stream = MagicMock()
    stream.fileno.return_value = 7
    assert ipc._unread_pipe_bytes(stream) == b"rest"
    assert seen == [(7, False), (7, True)]
    stream.read.assert_not_called()


def test_nonblocking_skips_on_win32(monkeypatch):
    """On Windows or when os.set_blocking is absent, _nonblocking yields cleanly without error."""
    from plugin.scripting import ipc

    monkeypatch.setattr(ipc.sys, "platform", "win32")

    def boom(*_args, **_kwargs):
        raise AssertionError("os.set_blocking must not run on win32")

    monkeypatch.setattr(ipc.os, "set_blocking", boom)
    with ipc._nonblocking(42):
        pass


def test_nonblocking_restores_blocking_posix(monkeypatch):
    """On POSIX, _nonblocking records initial blocking state and restores it on normal exit or exception."""
    from plugin.scripting import ipc

    monkeypatch.setattr(ipc.sys, "platform", "linux")
    events: list[tuple[str, int, bool | None]] = []

    def fake_get_blocking(fd: int) -> bool:
        events.append(("get", fd, None))
        return True

    def fake_set_blocking(fd: int, blocking: bool) -> None:
        events.append(("set", fd, blocking))

    monkeypatch.setattr(ipc.os, "get_blocking", fake_get_blocking)
    monkeypatch.setattr(ipc.os, "set_blocking", fake_set_blocking)

    # Normal exit
    with ipc._nonblocking(10):
        pass
    assert events == [("get", 10, None), ("set", 10, False), ("set", 10, True)]

    # Exception exit restores blocking
    events.clear()
    with pytest.raises(RuntimeError, match="boom"):
        with ipc._nonblocking(11):
            raise RuntimeError("boom")
    assert events == [("get", 11, None), ("set", 11, False), ("set", 11, True)]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX peek sets the pipe non-blocking")
def test_bad_length_prefix_restores_blocking_mode():
    """A garbage length must not leave the pipe non-blocking for the next read."""
    import struct

    read_fd, write_fd = os.pipe()
    os.write(write_fd, struct.pack("!I", 50_000_000) + b"LEFTOVER")
    try:
        with os.fdopen(read_fd, "rb", buffering=0) as reader:
            with pytest.raises(IpcFrameError, match="stdout_rest=b'LEFTOVER'"):
                read_pickle_frame(reader)
            assert os.get_blocking(read_fd) is True
    finally:
        os.close(write_fd)


def test_write_pickle_frame_retries_short_writes():
    """A raw pipe write can return one byte. The frame must still be complete."""

    class Short:
        def __init__(self) -> None:
            self.buf = bytearray()

        def write(self, data: bytes) -> int:
            piece = bytes(data[:1])
            if not piece:
                return 0
            self.buf += piece
            return 1

        def flush(self) -> None:
            return None

    stream = Short()
    write_pickle_frame(stream, {"a": 1})
    assert read_pickle_frame(io.BytesIO(stream.buf), require_dict=True) == {"a": 1}


def test_write_json_line_retries_short_writes():
    class Short:
        def __init__(self) -> None:
            self.parts: list[str] = []

        def write(self, data: str) -> int:
            if not data:
                return 0
            self.parts.append(data[:1])
            return 1

        def flush(self) -> None:
            return None

    stream = Short()
    write_json_line(stream, {"status": "ready"})
    assert "".join(stream.parts) == '{"status": "ready"}\n'


def test_write_pickle_frame_zero_length_write_raises():
    class Stuck:
        def write(self, data: bytes) -> int:
            return 0

        def flush(self) -> None:
            return None

    with pytest.raises(OSError, match="zero bytes"):
        write_pickle_frame(Stuck(), {"a": 1})


def test_read_frame_payload_loops_on_short_reads():
    frame = pack_pickle_frame({"a": 1})

    class OneByte:
        def __init__(self) -> None:
            self.i = 0

        def read(self, n: int) -> bytes:
            if self.i >= len(frame):
                return b""
            piece = frame[self.i : self.i + 1]
            self.i += 1
            return piece

    payload = read_frame_payload(OneByte())
    assert payload is not None
    assert unpack_pickle_frame(payload) == {"a": 1}


def test_read_frame_payload_short_header_then_eof_is_none():
    state = {"n": 0}

    class ShortEOF:
        def read(self, n: int) -> bytes:
            state["n"] += 1
            if state["n"] == 1:
                return b"\x00\x00"
            return b""

    assert read_frame_payload(ShortEOF()) is None


def test_json_line_roundtrip():
    buf = io.StringIO()
    write_json_line(buf, {"status": "ready"})
    buf.seek(0)

    assert read_json_line(buf) == {"status": "ready"}


@pytest.mark.parametrize(
    "match, value",
    [
        pytest.param("Invalid JSON line", "{not-json}\n", id="test_invalid_json_line_raises"),
        pytest.param("must contain an object", "[1, 2]\n", id="test_json_line_non_object_raises"),
    ],
)
def test_invalid_json_line_raises(match, value):
    with pytest.raises(ValueError, match=match):
        read_json_line(io.StringIO(value))

def test_pickle_frame_timeout_on_pipe():
    read_fd, write_fd = os.pipe()
    try:
        with os.fdopen(read_fd, "rb", buffering=0) as reader:
            with pytest.raises(subprocess.TimeoutExpired):
                read_pickle_frame_with_timeout(reader, 0.05)
    finally:
        os.close(write_fd)


def test_pickle_frame_timeout_mid_frame_is_partial():
    """Bytes already read are a desynced pipe, not a clean TimeoutExpired."""
    read_fd, write_fd = os.pipe()
    try:
        os.write(write_fd, b"\x00\x00")
        with os.fdopen(read_fd, "rb", buffering=0) as reader:
            with pytest.raises(IpcPartialFrameTimeout, match="timeout mid-frame"):
                read_pickle_frame_with_timeout(reader, 0.05)
    finally:
        os.close(write_fd)


def test_pickle_frame_write_timeout_on_unread_pipe():
    """A reader that stops consuming stdin must not block the writer past the deadline."""
    read_fd, write_fd = os.pipe()
    try:
        import fcntl

        fcntl.fcntl(write_fd, fcntl.F_SETPIPE_SZ, 4096)
    except (ImportError, AttributeError, OSError):
        pass
    writer = os.fdopen(write_fd, "wb", buffering=0)
    try:
        started = time.monotonic()
        with pytest.raises(subprocess.TimeoutExpired):
            write_pickle_frame_with_timeout(writer, {"blob": b"x" * (256 * 1024)}, 0.2)
        assert time.monotonic() - started < 2.0
    finally:
        writer.close()
        os.close(read_fd)


def test_pickle_frame_write_with_timeout_roundtrip():
    import threading

    read_fd, write_fd = os.pipe()
    got: dict[str, object] = {}

    def _reader() -> None:
        with os.fdopen(read_fd, "rb", buffering=0) as reader:
            got["msg"] = read_pickle_frame(reader)

    thread = threading.Thread(target=_reader)
    thread.start()
    try:
        with os.fdopen(write_fd, "wb", buffering=0) as writer:
            write_pickle_frame_with_timeout(writer, {"status": "ok"}, 2.0)
    finally:
        thread.join(timeout=2)
    assert got["msg"] == {"status": "ok"}
    assert not thread.is_alive()


@pytest.mark.skipif(sys.platform == "win32", reason="is_alive aborts the select write loop; Windows joins a blocking write")
def test_pickle_frame_write_aborts_when_child_exits_and_pipe_is_full():
    read_fd, write_fd = os.pipe()
    os.set_blocking(write_fd, False)
    try:
        try:
            while True:
                os.write(write_fd, b"x" * 65536)
        except BlockingIOError:
            pass
        os.set_blocking(write_fd, True)
        with os.fdopen(write_fd, "wb", buffering=0) as writer:
            started = time.monotonic()
            with pytest.raises(BrokenPipeError, match="child exited"):
                write_pickle_frame_with_timeout(
                    writer,
                    {"blob": b"y" * 65536},
                    2.0,
                    is_alive=lambda: False,
                )
            assert time.monotonic() - started < 1.5
    finally:
        os.close(read_fd)


def test_json_line_timeout_on_pipe():
    read_fd, write_fd = os.pipe()
    try:
        with os.fdopen(read_fd, "r", encoding="utf-8") as reader:
            with pytest.raises(subprocess.TimeoutExpired):
                read_json_line(reader, timeout_sec=0.01)
    finally:
        os.close(write_fd)


def test_win32_read_raising_stop_checker_keeps_reading(monkeypatch):
    """A raising stop_checker is not a stop; the peek loop still returns the bytes."""
    from plugin.scripting import ipc

    stream = MagicMock()
    stream.fileno.return_value = 3
    stream.read.return_value = b"abcd"
    monkeypatch.setattr(ipc, "_peek_pipe_bytes_available", lambda fd: 4)
    monkeypatch.setattr(ipc.time, "monotonic", lambda: 0.0)
    seen: list[BaseException] = []

    def stop_checker() -> bool:
        if not seen:
            err = KeyError("stop_checker blew up")
            seen.append(err)
            raise err
        return False

    data = ipc._read_bytes_with_timeout_win32(
        stream, 4, 10.0, 10.0, cmd="frame", stop_checker=stop_checker
    )
    assert data == b"abcd"
    assert seen


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
        ipc._read_bytes_with_timeout_win32(stream, 4, 0.01, 0.01, cmd="IPC frame")
    assert slept == [0.001]
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
        ipc._readline_with_timeout_win32(stream, 0.01, 1024)
    assert slept == [0.0]


def test_win32_partial_json_line_times_out_then_resumes(monkeypatch):
    """Queued bytes without a newline must not block in readline past the deadline."""
    from plugin.scripting import ipc

    partial = b'{"status": "partial"'
    rest = b"}\n"
    stage = {"phase": "partial"}

    def peek(fd: int) -> int:
        if stage["phase"] == "partial":
            return len(partial)
        if stage["phase"] == "wait":
            return 0
        return len(rest)

    def fake_read(fd: int, n: int) -> bytes:
        if stage["phase"] == "partial":
            stage["phase"] = "wait"
            return partial
        return rest

    class Stream:
        def __init__(self) -> None:
            self.readline_calls = 0

        def fileno(self) -> int:
            return 7

        def readline(self, limit: int = -1) -> str:
            self.readline_calls += 1
            raise AssertionError("readline")

    stream = Stream()
    clock = {"t": 0.0}

    monkeypatch.setattr(ipc, "_peek_pipe_bytes_available", peek)
    monkeypatch.setattr(ipc.os, "read", fake_read)
    monkeypatch.setattr(ipc.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(ipc.time, "sleep", lambda sec: clock.__setitem__("t", clock["t"] + 1.0))

    with pytest.raises(subprocess.TimeoutExpired):
        ipc._readline_with_timeout_win32(stream, 0.2, 1024)
    assert stream.readline_calls == 0

    clock["t"] = 0.0
    stage["phase"] = "rest"
    assert ipc._readline_with_timeout_win32(stream, 1.0, 1024) == '{"status": "partial"}\n'
    assert stream.readline_calls == 0


def test_oversize_pending_json_line_is_restored(monkeypatch):
    """A line over max_bytes must still be pending, not dropped mid-line."""
    from plugin.scripting import ipc

    chunk = b"x" * 20
    reads = {"n": 0}

    def fake_read(fd: int, n: int) -> bytes:
        reads["n"] += 1
        return chunk

    class Stream:
        def fileno(self) -> int:
            return 3

        def readline(self, limit: int = -1) -> str:
            raise AssertionError("readline")

    stream = Stream()
    monkeypatch.setattr(ipc, "_peek_pipe_bytes_available", lambda fd: len(chunk))
    monkeypatch.setattr(ipc.os, "read", fake_read)
    with pytest.raises(ValueError, match="exceeds"):
        ipc._readline_with_timeout_win32(stream, 1.0, 10)
    assert reads["n"] == 1
    with pytest.raises(ValueError, match="exceeds"):
        ipc._readline_with_timeout_win32(stream, 1.0, 10)
    assert reads["n"] == 1


def test_untimed_read_json_line_uses_saved_partial(monkeypatch):
    """timeout_sec=None must not skip a partial saved by a timed read."""
    from plugin.scripting import ipc

    partial = b'{"a": 1'
    stage = {"phase": "partial"}

    def peek(fd: int) -> int:
        return len(partial) if stage["phase"] == "partial" else 0

    def fake_read(fd: int, n: int) -> bytes:
        stage["phase"] = "wait"
        return partial

    class Stream:
        def fileno(self) -> int:
            return 7

        def readline(self, limit: int = -1) -> str:
            return "}\n"

    stream = Stream()
    clock = {"t": 0.0}
    monkeypatch.setattr(ipc, "_peek_pipe_bytes_available", peek)
    monkeypatch.setattr(ipc.os, "read", fake_read)
    monkeypatch.setattr(ipc.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(ipc.time, "sleep", lambda sec: clock.__setitem__("t", clock["t"] + 1.0))
    with pytest.raises(subprocess.TimeoutExpired):
        ipc._readline_with_timeout_win32(stream, 0.2, 1024)
    assert read_json_line(stream) == {"a": 1}


def test_exchange_tool_call_returns_result_when_id_matches(monkeypatch):
    from plugin.scripting import ipc

    written: dict[str, object] = {}

    def _write(stream, message, **kwargs):
        written["message"] = message

    def _read(stream, **kwargs):
        assert kwargs["max_payload_bytes"] == DEFAULT_MAX_PAYLOAD_BYTES
        assert kwargs["require_dict"] is True
        message = written["message"]
        assert isinstance(message, dict)
        return {"status": "ok", "id": message["id"], "result": {"n": 1}}

    monkeypatch.setattr(ipc, "write_pickle_frame", _write)
    monkeypatch.setattr(ipc, "read_pickle_frame", _read)
    assert ipc.exchange_tool_call("get_named_python_script", {"name": "a"}) == {"n": 1}


@pytest.mark.skipif(not hasattr(signal, "alarm"), reason="signal.alarm is POSIX")
def test_exchange_tool_call_reads_reply_before_budget_timeout(monkeypatch):
    """SIGALRM during the tool read used to desync the next cell. Consume the reply first."""
    import signal

    from plugin.scripting import ipc

    written: dict[str, object] = {}
    reads = {"n": 0}

    def _alarm(seconds: int) -> int:
        if seconds == 0:
            return 1
        return 0

    clock = {"t": 0.0}

    def _monotonic() -> float:
        clock["t"] += 5.0
        return clock["t"]

    def _write(stream, message, **kwargs):
        written["message"] = message

    def _read(stream, **kwargs):
        reads["n"] += 1
        message = written["message"]
        assert isinstance(message, dict)
        return {"status": "ok", "id": message["id"], "result": {"n": 1}}

    monkeypatch.setattr(signal, "alarm", _alarm)
    monkeypatch.setattr(ipc.time, "monotonic", _monotonic)
    monkeypatch.setattr(ipc, "write_pickle_frame", _write)
    monkeypatch.setattr(ipc, "read_pickle_frame", _read)
    with pytest.raises(TimeoutError, match="tool call"):
        ipc.exchange_tool_call("get_named_python_script", {"name": "a"})
    assert reads["n"] == 1


@pytest.mark.skipif(not hasattr(signal, "alarm"), reason="signal.alarm is POSIX")
def test_resume_script_alarm_rearms_only_when_time_remains(monkeypatch):
    import signal

    from plugin.scripting.ipc import _resume_script_alarm

    armed: list[int] = []
    monkeypatch.setattr(signal, "alarm", lambda seconds: armed.append(seconds) or 0)
    assert _resume_script_alarm((5, __import__("time").monotonic())) is False
    assert armed and armed[0] >= 1
    assert _resume_script_alarm((1, __import__("time").monotonic() - 10)) is True
    assert len(armed) == 1


def test_json_line_burst_returns_each_line():
    read_fd, write_fd = os.pipe()
    os.write(write_fd, b'{"a": 1}\n{"b": 2}\n')
    os.close(write_fd)
    with os.fdopen(read_fd, "r", encoding="utf-8") as reader:
        assert read_json_line(reader, timeout_sec=1.0) == {"a": 1}
        assert read_json_line(reader, timeout_sec=1.0) == {"b": 2}


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX readline must not block after select")
def test_json_line_partial_line_times_out_and_resumes():
    """select-then-readline used to wait for a newline after the deadline."""
    read_fd, write_fd = os.pipe()
    os.write(write_fd, b'{"status": "partial"')
    try:
        with os.fdopen(read_fd, "r", encoding="utf-8") as reader:
            started = time.monotonic()
            with pytest.raises(subprocess.TimeoutExpired):
                read_json_line(reader, timeout_sec=0.2)
            assert time.monotonic() - started < 1.0
            assert os.get_blocking(read_fd) is True
            os.write(write_fd, b"}\n")
            assert read_json_line(reader, timeout_sec=1.0) == {"status": "partial"}
    finally:
        os.close(write_fd)


def test_exchange_tool_call_user_stopped_is_not_a_normal_error(monkeypatch):
    from plugin.scripting import ipc

    written: dict[str, object] = {}

    def _write(stream, message, **kwargs):
        written["message"] = message

    def _read(stream, **kwargs):
        message = written["message"]
        assert isinstance(message, dict)
        return {
            "status": "error",
            "id": message["id"],
            "code": "USER_STOPPED",
            "message": "Stopped by user.",
        }

    monkeypatch.setattr(ipc, "write_pickle_frame", _write)
    monkeypatch.setattr(ipc, "read_pickle_frame", _read)
    with pytest.raises(ipc.UserStopped, match="Stopped by user") as raised:
        ipc.exchange_tool_call("apply_document_content", {})
    assert not isinstance(raised.value, Exception)


def test_exchange_tool_call_plain_error_stays_runtime_error(monkeypatch):
    from plugin.scripting import ipc

    written: dict[str, object] = {}

    def _write(stream, message, **kwargs):
        written["message"] = message

    def _read(stream, **kwargs):
        message = written["message"]
        assert isinstance(message, dict)
        return {"status": "error", "id": message["id"], "message": "boom"}

    monkeypatch.setattr(ipc, "write_pickle_frame", _write)
    monkeypatch.setattr(ipc, "read_pickle_frame", _read)
    with pytest.raises(RuntimeError, match="boom"):
        ipc.exchange_tool_call("apply_document_content", {})


def test_exchange_tool_call_preserves_error_code(monkeypatch):
    """RPC error responses with a code field attach .code to the raised RuntimeError."""
    from plugin.scripting import ipc

    written: dict[str, object] = {}

    def _write(stream, message, **kwargs):
        written["message"] = message

    def _read(stream, **kwargs):
        message = written["message"]
        assert isinstance(message, dict)
        return {"status": "error", "id": message["id"], "code": "CUSTOM_ERROR", "message": "custom failure"}

    monkeypatch.setattr(ipc, "write_pickle_frame", _write)
    monkeypatch.setattr(ipc, "read_pickle_frame", _read)
    with pytest.raises(RuntimeError, match="custom failure") as exc_info:
        ipc.exchange_tool_call("apply_document_content", {})
    assert getattr(exc_info.value, "code", None) == "CUSTOM_ERROR"


def test_exchange_tool_call_rejects_mismatched_id(monkeypatch):
    from plugin.scripting import ipc

    drained: list[object] = []
    monkeypatch.setattr(ipc, "write_pickle_frame", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        ipc,
        "read_pickle_frame",
        lambda *args, **kwargs: {"status": "ok", "id": "other", "result": {"n": 1}},
    )
    monkeypatch.setattr(ipc, "_drain_queued_pipe_bytes", lambda stream, **kwargs: drained.append(stream))
    with pytest.raises(RuntimeError, match="does not match"):
        ipc.exchange_tool_call("get_named_python_script", {})
    assert len(drained) == 1


def test_drain_without_fileno_does_not_read():
    """A stream with no fd cannot be drained without blocking."""
    from plugin.scripting import ipc

    class NoFd:
        def read(self, n: int = -1) -> bytes:
            raise AssertionError(f"read({n})")

    ipc._drain_queued_pipe_bytes(NoFd(), timeout_sec=5)


def test_drain_queued_pipe_bytes_returns_when_idle():
    """An open pipe with nothing queued must not block until the deadline."""
    from plugin.scripting import ipc

    read_fd, write_fd = os.pipe()
    stream = os.fdopen(read_fd, "rb")
    try:
        os.write(write_fd, b"xyz")
        assert stream.read(1) == b"x"
        started = time.monotonic()
        ipc._drain_queued_pipe_bytes(stream, timeout_sec=0.5)
        assert time.monotonic() - started < 0.4
        started = time.monotonic()
        ipc._drain_queued_pipe_bytes(stream, timeout_sec=0.5)
        assert time.monotonic() - started < 0.3
    finally:
        os.close(write_fd)
        stream.close()


@pytest.mark.skipif(sys.platform == "win32", reason="the mismatch drain needs non-blocking pipe reads (POSIX)")
def test_exchange_tool_call_drains_buffered_tail_after_id_mismatch(monkeypatch):
    """A foreign frame plus bytes already in the stdin buffer must not desync the next call.

    What was wrong: read_pickle_frame fills BufferedReader ahead. Raising on
    the id check left that tail for the next exchange_tool_call.
    """
    import threading

    from plugin.scripting import ipc

    in_r, in_w = os.pipe()
    out_r, out_w = os.pipe()
    stdin_buf = os.fdopen(in_r, "rb")
    stdout_buf = os.fdopen(out_w, "wb", buffering=0)
    stdout_reader = os.fdopen(out_r, "rb", buffering=0)

    class _PipeEnd:
        def __init__(self, buffer: object) -> None:
            self.buffer = buffer

    monkeypatch.setattr(sys, "stdin", _PipeEnd(stdin_buf))
    monkeypatch.setattr(sys, "stdout", _PipeEnd(stdout_buf))
    os.write(
        in_w,
        pack_pickle_frame({"status": "ok", "id": "stale", "result": {"n": 0}})
        + pack_pickle_frame({"status": "ok", "id": "tail", "result": {"n": 9}}),
    )
    worker: threading.Thread | None = None
    try:
        started = time.monotonic()
        with pytest.raises(RuntimeError, match="does not match"):
            ipc.exchange_tool_call("list_named_python_scripts", {})
        assert time.monotonic() - started < 1.0

        holder: dict[str, object] = {}

        def _second() -> None:
            try:
                holder["result"] = ipc.exchange_tool_call("list_named_python_scripts", {})
            except BaseException as exc:
                holder["error"] = exc

        worker = threading.Thread(target=_second, daemon=True)
        worker.start()
        first_req = read_pickle_frame_with_timeout(stdout_reader, 2.0, require_dict=True)
        second_req = read_pickle_frame_with_timeout(stdout_reader, 2.0, require_dict=True)
        assert isinstance(first_req, dict) and isinstance(second_req, dict)
        assert first_req.get("id") != second_req.get("id")
        os.write(
            in_w,
            pack_pickle_frame(
                {"status": "ok", "id": second_req["id"], "result": {"n": 1}}
            ),
        )
        worker.join(2.0)
        assert holder.get("error") is None
        assert holder.get("result") == {"n": 1}
    finally:
        try:
            os.close(in_w)
        except OSError:
            pass
        for closer in (stdin_buf, stdout_buf, stdout_reader):
            try:
                closer.close()
            except OSError:
                pass
        if worker is not None:
            worker.join(1.0)


def test_json_line_timeout_falls_back_when_fileno_not_int():
    """Non-int fileno() (e.g. MagicMock) must use readline, not PeekNamedPipe/select."""
    stream = MagicMock()
    stream.fileno.return_value = MagicMock()  # not an int
    stream.readline.return_value = '{"status": "ready"}\n'
    assert read_json_line(stream, timeout_sec=0.01) == {"status": "ready"}
    stream.readline.assert_called_once()


def test_packed_frame_write_without_int_fileno_logs_and_writes(caplog: pytest.LogCaptureFixture) -> None:
    """A stream whose fileno is not an int is written with no deadline.

    Popen pipes always have an int fileno. BytesIO takes this path, and it
    used to do so silently. A real pipe that landed here would wedge the
    caller that holds the worker lock.
    """
    stream = io.BytesIO()
    frame = pack_pickle_frame({"status": "ok"})
    with caplog.at_level(logging.DEBUG, logger="writeragent.scripting.ipc"):
        write_packed_frame_with_timeout(stream, frame, 0.01)
    assert "without a deadline" in caplog.text
    stream.seek(0)
    assert read_pickle_frame(stream) == {"status": "ok"}


def test_hostile_reduce_bytes_raises_value_error():
    import pickle
    from plugin.scripting.ipc import unpack_pickle_frame

    class BadBytes:
        def __reduce__(self):
            return (bytes, (10**12,))

    payload = pickle.dumps(BadBytes(), protocol=5)
    with pytest.raises(ValueError, match="is not allowed|Unable to allocate|MemoryError"):
        unpack_pickle_frame(payload)


def test_roundtrip_ndarray_complex_containers():
    import numpy as np
    import pickle
    from plugin.scripting.ipc import unpack_pickle_frame

    obj1 = 1 + 2j
    obj2 = np.array([1, 2, 3], dtype=np.float64)
    obj3 = {"a": [1, 2, 3], "b": b"xyz"}

    assert unpack_pickle_frame(pickle.dumps(obj1, protocol=5)) == obj1

    res2 = unpack_pickle_frame(pickle.dumps(obj2, protocol=5))
    assert isinstance(res2, np.ndarray)
    assert np.array_equal(res2, obj2)

    obj4 = np.arange(12.).reshape(3, 4)[:, ::2]
    res4 = unpack_pickle_frame(pickle.dumps(obj4, protocol=5))
    assert isinstance(res4, np.ndarray)
    assert np.array_equal(res4, obj4)

    assert unpack_pickle_frame(pickle.dumps(obj3, protocol=5)) == obj3

def test_numpy_arbitrary_submodule_import_fails():
    import pickle
    import numpy as np
    from plugin.scripting.ipc import unpack_pickle_frame

    class BadNumpy:
        def __reduce__(self):
            return (np.ctypeslib.load_library, ("libc.so.6", "/lib"))

    payload = pickle.dumps(BadNumpy(), protocol=5)
    with pytest.raises(ValueError, match="is not allowed"):
        unpack_pickle_frame(payload)

def test_read_pickle_frame_with_timeout_win32_is_alive_honored(monkeypatch):
    import sys
    from plugin.scripting import ipc
    from unittest.mock import MagicMock

    monkeypatch.setattr(sys, "platform", "win32")

    stream = MagicMock()
    stream.fileno.return_value = 3
    # Return valid frame size (e.g. 4 bytes length) to prevent IpcFrameError
    stream.read.side_effect = [b"\x00\x00\x00\x04", b"\x00\x00\x00\x00"]
    monkeypatch.setattr(ipc, "_peek_pipe_bytes_available", lambda fd: 4)
    monkeypatch.setattr(ipc, "_decode_pickle_payload", lambda *args, **kwargs: {"ok": 1})

    # is_alive returning True should NOT cause TimeoutExpired
    res = ipc.read_pickle_frame_with_timeout(stream, 1.0, is_alive=lambda: True)
    assert res == {"ok": 1}

    # is_alive returning False SHOULD cause TimeoutExpired
    stream.read.side_effect = [b"\x00\x00\x00\x04", b"\x00\x00\x00\x00"]
    with pytest.raises(subprocess.TimeoutExpired):
        ipc.read_pickle_frame_with_timeout(stream, 1.0, is_alive=lambda: False)


def test_claim_ipc_channel_redirects_stdout_and_preserves_framing():
    """print() in child must go to stderr and not corrupt the claimed IPC channel."""
    code = """
import os, sys
from plugin.scripting.ipc import claim_ipc_channel, write_json_line

ipc_stream = claim_ipc_channel()
print("Stray library debug print to stdout")
sys.stdout.flush()
write_json_line(ipc_stream, {"status": "ok", "value": 42})
"""
    proc = subprocess.Popen(
        [sys.executable, "-c", code],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=False,
    )
    stdout, stderr = proc.communicate(timeout=5.0)
    assert proc.returncode == 0
    assert b"Stray library debug print to stdout" in stderr
    assert b"Stray library debug print to stdout" not in stdout

    import json
    line = stdout.decode("utf-8").strip()
    data = json.loads(line)
    assert data == {"status": "ok", "value": 42}
