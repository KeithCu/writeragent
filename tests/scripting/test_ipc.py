# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for shared subprocess IPC framing helpers."""

from __future__ import annotations

import io
import os
import signal
import pickle
import struct
import subprocess
import sys
import threading
import time
import types
from typing import Callable
from unittest.mock import MagicMock

import pytest

import plugin.framework.ipc
from plugin.framework.send_cancellation import SendCancellation
from plugin.framework.ipc import (
    DEFAULT_MAX_PAYLOAD_BYTES,
    IpcFrameError,
    IpcFrameReadError,
    IpcPartialFrameTimeout,
    pack_pickle_frame,
    read_frame_payload,
    read_json_line,
    read_pickle_frame,
    unpack_pickle_frame,
    write_child_ipc_frame,
    write_json_line,
    write_packed_frame_with_timeout,
    write_pickle_frame,
)


def _patch_ipc_clock(
    monkeypatch: pytest.MonkeyPatch,
    monotonic: Callable[[], float],
    sleep: Callable[[float], object] | None = None,
) -> list[float]:
    """Swap the module-level time name in ipc to keep clock fakes private to ipc.

    ipc reaches the clock through its own module-level time name, so swapping
    that name keeps the fake private to ipc. Patching time.monotonic on the
    shared time module would also feed every other thread in the pytest worker;
    idle reapers left by earlier tests read the clock about once a second, and
    one such read steals a value from a scripted clock.
    """
    slept: list[float] = []
    sleep_fn = slept.append if sleep is None else sleep
    fake_time = types.SimpleNamespace(monotonic=monotonic, sleep=sleep_fn)
    monkeypatch.setattr(plugin.framework.ipc, "time", fake_time)
    return slept


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


def test_unknown_pickle_opcode_is_value_error():
    """An unknown opcode is KeyError from pickle._Unpickler. Callers still see ValueError."""
    with pytest.raises(ValueError, match="invalid load key"):
        unpack_pickle_frame(b"notpickl")


def test_unpickler_handler_keyerror_is_value_error(monkeypatch):
    from plugin.framework.ipc import AllowlistUnpickler, unpack_pickle_frame

    def handler(unpickler):
        raise KeyError("missing-state")

    monkeypatch.setitem(AllowlistUnpickler.dispatch, ord("X"), handler)
    with pytest.raises(ValueError, match="missing-state"):
        unpack_pickle_frame(b"X")


def test_truncated_pickle_frame_returns_none():
    payload = pack_pickle_frame({"status": "ok"})
    truncated = payload[:-2]

    with pytest.raises(IpcFrameReadError):
        read_pickle_frame(io.BytesIO(truncated))


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
    from plugin.framework.ipc import DEFAULT_MAX_PAYLOAD_BYTES, IpcFrameError, pack_pickle_frame
    with pytest.raises(IpcFrameError, match="maximum payload"):
        pack_pickle_frame({"text": "x" * (DEFAULT_MAX_PAYLOAD_BYTES + 10)})


def test_text_error_prefix_is_invalid_frame_with_header_repr():
    """Garbage length prefix keeps stdout_rest= on IpcFrameError."""
    with pytest.raises(IpcFrameError, match=r"header=b'Erro'.*stdout_rest=b'r: boom\\n'"):
        read_pickle_frame(
            io.BytesIO(b"Error: boom\n"),
            max_payload_bytes=DEFAULT_MAX_PAYLOAD_BYTES,
        )


def test_unread_pipe_bytes_skips_set_blocking_on_win32(monkeypatch):
    """Windows/ty: os.set_blocking is POSIX-only; skip the non-blocking peek."""
    from plugin.framework import ipc

    monkeypatch.setattr(ipc.sys, "platform", "win32")

    def boom(*_args, **_kwargs):
        raise AssertionError("os.set_blocking must not run on win32")

    monkeypatch.setattr(ipc.os, "set_blocking", boom)
    stream = MagicMock()
    stream.fileno.return_value = 3
    assert ipc._unread_pipe_bytes(stream) == b""
    stream.read.assert_not_called()


def test_invalid_frame_includes_stdout_rest_when_win32_peek_skipped(monkeypatch):
    """Win32 skip still attaches stdout_rest= (empty) on IpcFrameError."""
    from plugin.framework import ipc

    monkeypatch.setattr(ipc.sys, "platform", "win32")

    def boom(*_args, **_kwargs):
        raise AssertionError("os.set_blocking must not run on win32")

    monkeypatch.setattr(ipc.os, "set_blocking", boom)
    stream = MagicMock()
    stream.fileno.return_value = 3
    stream.read.return_value = b"Erro"
    with pytest.raises(IpcFrameError, match=r"header=b'Erro'.*stdout_rest=b''"):
        read_pickle_frame(stream, max_payload_bytes=DEFAULT_MAX_PAYLOAD_BYTES)
    stream.read.assert_called_once_with(4)


def test_nonblocking_skips_on_win32(monkeypatch):
    """On Windows or when os.set_blocking is absent, _nonblocking yields cleanly without error."""
    from plugin.framework import ipc

    monkeypatch.setattr(ipc.sys, "platform", "win32")

    def boom(*_args, **_kwargs):
        raise AssertionError("os.set_blocking must not run on win32")

    monkeypatch.setattr(ipc.os, "set_blocking", boom)
    with ipc._nonblocking(42):
        pass


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

    with pytest.raises(IpcFrameReadError):
        read_frame_payload(ShortEOF())
    assert read_frame_payload(io.BytesIO()) is None
    frame = pack_pickle_frame({"a": 1})
    stream = io.BytesIO(frame)
    assert read_frame_payload(stream) is not None
    assert read_frame_payload(stream) is None


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
                read_pickle_frame(reader, 0.05)
    finally:
        os.close(write_fd)


def test_pickle_frame_timeout_mid_frame_is_partial():
    """Bytes already read are a desynced pipe, not a clean TimeoutExpired."""
    read_fd, write_fd = os.pipe()
    try:
        os.write(write_fd, b"\x00\x00")
        with os.fdopen(read_fd, "rb", buffering=0) as reader:
            with pytest.raises(IpcPartialFrameTimeout, match="timeout mid-frame"):
                read_pickle_frame(reader, 0.05)
    finally:
        os.close(write_fd)


def test_header_only_stall_is_partial_timeout():
    read_fd, write_fd = os.pipe()
    try:
        os.write(write_fd, struct.pack("!I", 10))
        with os.fdopen(read_fd, "rb", buffering=0) as reader:
            t0 = time.monotonic()
            with pytest.raises(IpcPartialFrameTimeout, match="timeout mid-frame"):
                read_frame_payload(reader, 0.3)
            assert time.monotonic() - t0 < 2.0
    finally:
        os.close(write_fd)


def test_header_consumed_timeout_is_partial_timeout_no_fd():
    class HeaderThenStall:
        def __init__(self):
            self.header = struct.pack("!I", 10)

        def read(self, n):
            if self.header:
                out, self.header = self.header[:n], self.header[n:]
                return out
            time.sleep(10)
            return b""

    with pytest.raises(IpcPartialFrameTimeout, match="timeout mid-frame"):
        read_frame_payload(HeaderThenStall(), 0.2)


def test_read_frame_payload_pipe_eof_mid_header_raises_truncated_fast():
    read_fd, write_fd = os.pipe()
    try:
        os.write(write_fd, b"\x00\x00")
        os.close(write_fd)
        write_fd = -1
        with os.fdopen(read_fd, "rb", buffering=0) as reader:
            t0 = time.monotonic()
            with pytest.raises(IpcFrameReadError):
                read_frame_payload(reader, 5.0)
            assert time.monotonic() - t0 < 1.5
    finally:
        if write_fd >= 0:
            os.close(write_fd)


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="Windows joins a blocking write thread; closing an in-process pipe while blocked in CRT write crashes",
)
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
            write_packed_frame_with_timeout(writer, pack_pickle_frame({"blob": b"x" * (256 * 1024)}), 0.2)
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
            write_packed_frame_with_timeout(writer, pack_pickle_frame({"status": "ok"}), 2.0)
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
                write_packed_frame_with_timeout(
                    writer,
                    pack_pickle_frame({"blob": b"y" * 65536}),
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


def test_win32_read_raising_cancel_keeps_reading(monkeypatch):
    """A raising cancel scope is not a stop; the peek loop still returns the bytes."""
    import struct
    from plugin.framework import ipc

    monkeypatch.setattr(sys, "platform", "win32")
    stream = MagicMock()
    stream.fileno.return_value = 3
    stream.read.side_effect = [struct.pack("!I", 4), b"abcd"]
    monkeypatch.setattr(ipc, "_peek_pipe_bytes_available", lambda fd: 4)
    _patch_ipc_clock(monkeypatch, lambda: 0.0)
    seen: list[BaseException] = []

    class _RaisingCancel(SendCancellation):
        def is_cancelled(self) -> bool:
            if not seen:
                err = KeyError("is_cancelled blew up")
                seen.append(err)
                raise err
            return False

    data = ipc.read_frame_payload(
        stream, 10.0, cmd="frame", cancel=_RaisingCancel()
    )
    assert data == b"abcd"
    assert seen


def _raising_then_false_cancel_scope():
    seen: list[BaseException] = []

    class _RaisingCancel(SendCancellation):
        def is_cancelled(self) -> bool:
            if not seen:
                err = KeyError("is_cancelled blew up")
                seen.append(err)
                raise err
            return False

    return seen, _RaisingCancel()


def test_read_frame_payload_raising_cancel_threaded_path_returns_frame():
    """A broken cancel scope must not escape or abandon the queued frame (no-fd)."""
    frame = pack_pickle_frame({"status": "ok", "result": 1})
    stream = io.BytesIO(frame)
    seen, cancel = _raising_then_false_cancel_scope()
    payload = read_frame_payload(stream, 5.0, cancel=cancel)
    assert seen
    assert unpack_pickle_frame(payload) == {"status": "ok", "result": 1}


def test_read_frame_payload_raising_cancel_select_path_returns_frame():
    """A broken cancel scope must not escape or abandon the queued frame (fd path)."""
    frame = pack_pickle_frame({"status": "ok", "result": 1})
    r_fd, w_fd = os.pipe()
    os.write(w_fd, frame)
    os.close(w_fd)
    reader = os.fdopen(r_fd, "rb", buffering=0)
    seen, cancel = _raising_then_false_cancel_scope()
    try:
        payload = read_frame_payload(reader, 5.0, cancel=cancel)
    finally:
        reader.close()
    assert seen
    assert unpack_pickle_frame(payload) == {"status": "ok", "result": 1}


def test_win32_pickle_read_timeout_clamps_when_peek_crosses_deadline(monkeypatch):
    """Peek returns 1 ms before the deadline, so the clamp sleeps only the remainder.

    Sleeping the full 1 ms slice would overshoot the deadline, while an un-clamped
    negative remainder would raise ValueError. The next poll check raises TimeoutExpired.
    """
    from plugin.framework import ipc

    monkeypatch.setattr(sys, "platform", "win32")
    times = iter([0.0, 0.009, 0.011, 0.011])
    slept = _patch_ipc_clock(monkeypatch, lambda: next(times))
    monkeypatch.setattr(ipc, "_peek_pipe_bytes_available", lambda fd: 0)
    stream = MagicMock()
    stream.fileno.return_value = 3
    with pytest.raises(subprocess.TimeoutExpired):
        ipc.read_frame_payload(stream, 0.01, deadline=0.01, cmd="IPC frame")
    assert slept == [0.001]
    assert all(sec >= 0.0 for sec in slept)
    stream.read.assert_not_called()


def test_win32_pickle_read_breaks_when_child_exits_before_ready(monkeypatch):
    """When peek is empty and child exits (is_alive() is False), win32 read breaks to EOF rather than timing out."""
    from plugin.framework import ipc

    monkeypatch.setattr(sys, "platform", "win32")
    _patch_ipc_clock(monkeypatch, lambda: 0.0)
    monkeypatch.setattr(ipc, "_peek_pipe_bytes_available", lambda fd: 0)
    stream = MagicMock()
    stream.fileno.return_value = 3
    # Initial is_alive=False returns None immediately
    assert ipc.read_frame_payload(stream, 10.0, cmd="IPC frame", is_alive=lambda: False) is None

    # Becoming false inside polling loop breaks out and returns None
    alive_iter = iter([True, False])
    assert ipc.read_frame_payload(stream, 10.0, cmd="IPC frame", is_alive=lambda: next(alive_iter)) is None


def test_win32_read_drains_queued_bytes_when_child_already_dead(monkeypatch):
    import struct
    from unittest.mock import MagicMock
    from plugin.framework import ipc

    monkeypatch.setattr(sys, "platform", "win32")
    slept = _patch_ipc_clock(monkeypatch, time.monotonic)
    monkeypatch.setattr(ipc, "_peek_pipe_bytes_available", MagicMock(side_effect=[4, 4]))

    stream = MagicMock()
    stream.fileno.return_value = 3
    header = struct.pack("!I", 4)
    stream.read.side_effect = [header, b"abcd"]

    res = ipc.read_frame_payload(
        stream,
        100.0,
        cmd="IPC frame",
        is_alive=lambda: False,
    )
    assert res == b"abcd"
    assert slept == []


def test_win32_readline_sleep_clamps_when_peek_crosses_deadline(monkeypatch):
    """PeekNamedPipe can finish after the deadline; sleep(negative) is ValueError."""
    from plugin.framework import ipc

    monkeypatch.setattr(ipc.sys, "platform", "win32")
    times = iter([0.0, 0.009, 0.011, 0.011])
    slept = _patch_ipc_clock(monkeypatch, lambda: next(times))
    monkeypatch.setattr(ipc, "_peek_pipe_bytes_available", lambda fd: 0)
    stream = MagicMock()
    stream.fileno.return_value = 3
    with pytest.raises(subprocess.TimeoutExpired):
        ipc._readline_with_timeout(stream, 0.01, 1024)
    assert slept == [0.0]
    assert all(sec >= 0.0 for sec in slept)


def test_win32_partial_json_line_times_out_then_resumes(monkeypatch):
    """Queued bytes without a newline must not block in readline past the deadline."""
    from plugin.framework import ipc

    monkeypatch.setattr(ipc.sys, "platform", "win32")
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
    _patch_ipc_clock(monkeypatch, lambda: clock["t"], sleep=lambda sec: clock.__setitem__("t", clock["t"] + 1.0))

    with pytest.raises(subprocess.TimeoutExpired):
        ipc._readline_with_timeout(stream, 0.2, 1024)
    assert stream.readline_calls == 0

    clock["t"] = 0.0
    stage["phase"] = "rest"
    assert ipc._readline_with_timeout(stream, 1.0, 1024) == '{"status": "partial"}\n'
    assert stream.readline_calls == 0


def test_oversize_pending_json_line_is_restored(monkeypatch):
    """A line over max_bytes must still be pending, not dropped mid-line."""
    from plugin.framework import ipc

    monkeypatch.setattr(ipc.sys, "platform", "win32")
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
        ipc._readline_with_timeout(stream, 1.0, 10)
    assert reads["n"] == 1
    with pytest.raises(ValueError, match="exceeds"):
        ipc._readline_with_timeout(stream, 1.0, 10)
    assert reads["n"] == 1


def test_untimed_read_json_line_uses_saved_partial(monkeypatch):
    """timeout_sec=None must not skip a partial saved by a timed read.

    The tail is the next ``os.read``. ``readline()`` would decode a saved
    prefix that ends mid-character before the continuation bytes arrive.
    """
    from plugin.framework import ipc

    monkeypatch.setattr(ipc.sys, "platform", "win32")
    partial = b'{"a": 1'
    stage = {"phase": "partial"}

    def peek(fd: int) -> int:
        return len(partial) if stage["phase"] == "partial" else 0

    def fake_read(fd: int, n: int) -> bytes:
        if stage["phase"] == "partial":
            stage["phase"] = "tail"
            return partial
        if stage["phase"] == "tail":
            stage["phase"] = "done"
            return b"}\n"
        return b""

    class Stream:
        def fileno(self) -> int:
            return 7

        def readline(self, limit: int = -1) -> str:
            raise AssertionError("readline")

    stream = Stream()
    clock = {"t": 0.0}
    monkeypatch.setattr(ipc, "_peek_pipe_bytes_available", peek)
    monkeypatch.setattr(ipc.os, "read", fake_read)
    _patch_ipc_clock(monkeypatch, lambda: clock["t"], sleep=lambda sec: clock.__setitem__("t", clock["t"] + 1.0))
    with pytest.raises(subprocess.TimeoutExpired):
        ipc._readline_with_timeout(stream, 0.2, 1024)
    assert read_json_line(stream) == {"a": 1}


def test_exchange_tool_call_returns_result_when_id_matches(monkeypatch):
    from plugin.framework import ipc

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


@pytest.mark.skipif(not hasattr(signal, "setitimer"), reason="setitimer is POSIX")
def test_exchange_tool_call_reads_reply_before_budget_timeout(monkeypatch):
    """On budget timeout during a tool read, consume the reply before raising so the next call stays in sync."""
    import signal

    from plugin.framework import ipc

    written: dict[str, object] = {}
    reads = {"n": 0}

    def _setitimer(which: int, seconds: float, interval: float = 0.0) -> tuple[float, float]:
        if seconds == 0.0:
            return (1.0, 0.0)
        return (0.0, 0.0)

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

    monkeypatch.setattr(signal, "setitimer", _setitimer)
    _patch_ipc_clock(monkeypatch, _monotonic)
    monkeypatch.setattr(ipc, "write_pickle_frame", _write)
    monkeypatch.setattr(ipc, "read_pickle_frame", _read)
    with pytest.raises(TimeoutError, match="tool call"):
        ipc.exchange_tool_call("get_named_python_script", {"name": "a"})
    assert reads["n"] == 1


@pytest.mark.skipif(not hasattr(signal, "setitimer"), reason="setitimer is POSIX")
def test_resume_script_alarm_rearms_only_when_time_remains(monkeypatch):
    import signal

    from plugin.framework.ipc import _resume_script_alarm

    armed: list[float] = []

    def _setitimer(which: int, seconds: float, interval: float = 0.0) -> tuple[float, float]:
        armed.append(seconds)
        return (0.0, 0.0)

    monkeypatch.setattr(signal, "setitimer", _setitimer)
    assert _resume_script_alarm((5.0, 0.0, time.monotonic())) is False
    assert armed and armed[0] == pytest.approx(5.0, abs=0.05)
    assert _resume_script_alarm((1.0, 0.0, time.monotonic() - 10)) is True
    assert len(armed) == 1


@pytest.mark.skipif(not hasattr(signal, "setitimer"), reason="setitimer is POSIX")
def test_resume_script_alarm_keeps_fractional_remainder(monkeypatch):
    """_resume_script_alarm must re-arm with the fractional remainder, not round up to a whole second."""
    import signal

    from plugin.framework.ipc import _resume_script_alarm

    armed: list[float] = []

    def _setitimer(which: int, seconds: float, interval: float = 0.0) -> tuple[float, float]:
        armed.append(seconds)
        return (0.0, 0.0)

    monkeypatch.setattr(signal, "setitimer", _setitimer)
    assert _resume_script_alarm((0.4, 0.0, time.monotonic())) is False
    assert armed
    assert 0 < armed[0] <= 0.4


def test_json_line_burst_returns_each_line():
    read_fd, write_fd = os.pipe()
    os.write(write_fd, b'{"a": 1}\n{"b": 2}\n')
    os.close(write_fd)
    with os.fdopen(read_fd, "r", encoding="utf-8") as reader:
        assert read_json_line(reader, timeout_sec=1.0) == {"a": 1}
        assert read_json_line(reader, timeout_sec=1.0) == {"b": 2}


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX readline must not block after select")
def test_json_line_partial_line_times_out_and_resumes():
    """After select times out on a partial line, readline must not block waiting for a newline."""
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
    from plugin.framework import ipc

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
    from plugin.framework import ipc

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
    from plugin.framework import ipc

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
    from plugin.framework import ipc

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
    from plugin.framework import ipc

    class NoFd:
        def read(self, n: int = -1) -> bytes:
            raise AssertionError(f"read({n})")

    ipc._drain_queued_pipe_bytes(NoFd(), timeout_sec=5)


def test_drain_queued_pipe_bytes_returns_when_idle():
    """An open pipe with nothing queued must not block until the deadline."""
    from plugin.framework import ipc

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

    read_pickle_frame may prefetch the stdin buffer. On id mismatch, drain the
    tail before raising so the next exchange_tool_call does not read a stale frame.
    """
    import threading

    from plugin.framework import ipc

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
        first_req = read_pickle_frame(stdout_reader, 2.0, require_dict=True)
        second_req = read_pickle_frame(stdout_reader, 2.0, require_dict=True)
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


def test_timed_json_read_without_fd_uses_saved_pending():
    """A timed read with no real fd must not drop a partial line saved earlier."""
    from plugin.framework import ipc

    class Stream:
        def fileno(self) -> object:
            return MagicMock()

        def readline(self, limit: int = -1) -> str:
            return "1}\n"

    stream = Stream()
    ipc._save_json_line_pending(stream, bytearray(b'{"a": '))
    assert read_json_line(stream, timeout_sec=0.01) == {"a": 1}


def test_json_line_without_newline_at_eof_parses():
    assert read_json_line(io.StringIO('{"a": 1}')) == {"a": 1}


def test_json_line_truncated_at_eof_names_the_cause():
    with pytest.raises(ValueError, match="truncated at EOF"):
        read_json_line(io.StringIO("{"))


def test_finish_saved_json_line_keeps_a_split_utf8_character():
    """A timed read can stop between the two bytes of U+00E9.

    Decoding that saved prefix before readline() turned the first byte
    into U+FFFD and decoded the continuation as a second character.
    """
    import json

    from plugin.framework.ipc import _finish_saved_json_line

    read_fd, write_fd = os.pipe()
    os.write(write_fd, b'\xa9"}\n')
    os.close(write_fd)
    stream = io.TextIOWrapper(io.FileIO(read_fd, closefd=True), encoding="utf-8")
    try:
        line = _finish_saved_json_line(stream, bytearray(b'{"a": "\xc3'), 1024)
    finally:
        stream.close()
    assert json.loads(line) == {"a": "é"}


def test_joined_write_aborts_when_child_is_dead() -> None:
    """A dead child on the joined write path does not wait out the timeout.

    POSIX select checks is_alive. The Windows path joins a thread and used
    to ignore is_alive, so a dead child blocked for the full deadline.
    """
    import threading

    from plugin.framework.ipc import _write_bytes_until_joined

    release = threading.Event()

    class _Slow:
        def write(self, data: bytes) -> int:
            release.wait(timeout=5.0)
            return len(data)

        def flush(self) -> None:
            return None

    started = time.monotonic()
    try:
        with pytest.raises(BrokenPipeError, match="child exited"):
            _write_bytes_until_joined(_Slow(), b"abcdef", 30.0, is_alive=lambda: False)  # type: ignore[arg-type]
    finally:
        release.set()
    assert time.monotonic() - started < 2.0


def test_packed_frame_write_without_int_fileno_writes() -> None:
    """A stream whose fileno is not an int is written with no deadline.

    Popen pipes always have an int fileno, so skipping the deadline here is safe.
    """
    stream = io.BytesIO()
    frame = pack_pickle_frame({"status": "ok"})
    write_packed_frame_with_timeout(stream, frame, 0.01)
    stream.seek(0)
    assert read_pickle_frame(stream) == {"status": "ok"}


def test_hostile_reduce_bytes_raises_value_error():
    import pickle
    from plugin.framework.ipc import unpack_pickle_frame

    class BadBytes:
        def __reduce__(self):
            return (bytes, (10**12,))

    payload = pickle.dumps(BadBytes(), protocol=5)
    with pytest.raises(ValueError, match="is not allowed|Unable to allocate|MemoryError"):
        unpack_pickle_frame(payload)


def test_roundtrip_ndarray_complex_containers():
    import numpy as np
    import pickle
    from plugin.framework.ipc import unpack_pickle_frame

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

@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(b"\x80\x02cnumpy\nndarray\nK\x02\x85\x85\x81.", id="newobj"),
        pytest.param(b"\x80\x02cnumpy\nndarray\nK\x02\x85\x85}\x92.", id="newobj_ex"),
        pytest.param(b"(cnumpy\nndarray\n(I2\nto.", id="obj"),
        pytest.param(b"((I2\ntinumpy\nndarray\n.", id="inst"),
    ],
)
def test_unpack_rejects_ndarray_constructor_opcodes(payload: bytes):
    """NEWOBJ, NEWOBJ_EX, OBJ, and INST call ndarray without going through REDUCE.

    The shape is (2,). A missing guard allocates 16 bytes, not a huge array.
    The class stays allowlisted because a view passes it to _reconstruct.
    """
    with pytest.raises(ValueError, match="ndarray"):
        unpack_pickle_frame(payload)


def test_allowlist_unpickler_pins_private_pickle_dispatch():
    """Guards live on pickle._Unpickler. A Python that moves that class or the opcode bytes fails here."""
    from plugin.framework.ipc import AllowlistUnpickler

    assert issubclass(AllowlistUnpickler, pickle._Unpickler)
    assert pickle.REDUCE[0] in pickle._Unpickler.dispatch
    assert pickle.NEWOBJ[0] in pickle._Unpickler.dispatch
    assert pickle.NEWOBJ_EX[0] in pickle._Unpickler.dispatch
    assert AllowlistUnpickler._instantiate is not pickle._Unpickler._instantiate


def test_unpack_rejects_direct_ndarray_allocation():
    """ndarray(shape) allocates from a few pickle bytes. Views do not call it."""
    import numpy as np

    class Bomb:
        def __reduce__(self):
            return (np.ndarray, ((DEFAULT_MAX_PAYLOAD_BYTES + 1,), np.dtype("u1")))

    payload = pickle.dumps(Bomb(), protocol=5)
    with pytest.raises(ValueError, match="ndarray"):
        unpack_pickle_frame(payload)


def test_unpack_rejects_huge_numpy_reconstruct():
    """_reconstruct(ndarray, huge_shape, dtype) is the same allocation, via the view path."""
    import numpy as np
    from numpy._core.multiarray import _reconstruct

    class Bomb:
        def __reduce__(self):
            return (_reconstruct, (np.ndarray, (DEFAULT_MAX_PAYLOAD_BYTES + 1,), np.dtype("u1")))

    payload = pickle.dumps(Bomb(), protocol=5)
    with pytest.raises(ValueError, match="reconstruct allocates"):
        unpack_pickle_frame(payload)


def test_unpack_rejects_huge_numpy_reconstruct_inst_and_obj():
    """INST and OBJ calling _reconstruct allocate past the frame cap."""
    n = DEFAULT_MAX_PAYLOAD_BYTES + 1
    inst = (
        b"(cnumpy\nndarray\n(I" + str(n).encode() + b"\ntS'u1'\n"
        b"inumpy._core.multiarray\n_reconstruct\n."
    )
    obj = (
        b"(cnumpy._core.multiarray\n_reconstruct\n"
        b"cnumpy\nndarray\n(I" + str(n).encode() + b"\ntS'u1'\no."
    )
    with pytest.raises(ValueError, match="reconstruct allocates"):
        unpack_pickle_frame(inst)
    with pytest.raises(ValueError, match="reconstruct allocates"):
        unpack_pickle_frame(obj)


def test_unpack_bytearray8_rejects_oversized_allocation():
    """BYTEARRAY8 length exceeding available frame data is rejected without allocation."""
    import tracemalloc

    frame_body = pickle.BYTEARRAY8 + struct.pack("<Q", 4_000_000)
    payload = b"\x80\x05\x95" + struct.pack("<Q", len(frame_body)) + frame_body

    tracemalloc.start()
    try:
        tracemalloc.reset_peak()
        with pytest.raises(ValueError):
            unpack_pickle_frame(payload)
        _current, peak = tracemalloc.get_traced_memory()
        assert peak < 1_000_000
    finally:
        tracemalloc.stop()

    assert unpack_pickle_frame(pickle.dumps(bytearray(b"hello"), protocol=5)) == bytearray(b"hello")
    large_ba = bytearray(b"x" * 8192)
    assert unpack_pickle_frame(pickle.dumps(large_ba, protocol=5)) == large_ba


@pytest.mark.parametrize(
    "payload",
    [
        b"\x80\x05J\x01",
        b"\x80\x05M\x01",
        b"\x80\x05G\x01\x02\x03",
        b"\x80\x05\x95\x01",
        b"\x80\x02J\x01",
    ],
)
def test_unpack_struct_error_mapped_to_value_error(payload):
    """Truncated headers raising struct.error are mapped to ValueError."""
    with pytest.raises(ValueError, match="unpack requires a buffer"):
        unpack_pickle_frame(payload)


@pytest.mark.parametrize(
    ("shape", "dtype_spec"),
    [
        ((1,), "V" + str(DEFAULT_MAX_PAYLOAD_BYTES + 1)),
        ((1,), b"V" + str(DEFAULT_MAX_PAYLOAD_BYTES + 1).encode()),
        ((1,), ("u1", (DEFAULT_MAX_PAYLOAD_BYTES + 1,))),
        ((DEFAULT_MAX_PAYLOAD_BYTES // 8 + 1,), "f8"),
        ((DEFAULT_MAX_PAYLOAD_BYTES // 4 + 1,), "U"),
    ],
)
def test_unpack_rejects_numpy_reconstruct_specs_exceeding_cap(shape, dtype_spec):
    import numpy as np
    from numpy._core.multiarray import _reconstruct

    class Bomb:
        def __reduce__(self):
            return (_reconstruct, (np.ndarray, shape, dtype_spec))

    payload = pickle.dumps(Bomb(), protocol=5)
    with pytest.raises(ValueError, match="reconstruct allocates"):
        unpack_pickle_frame(payload)


@pytest.mark.parametrize(
    ("shape", "dtype_spec", "expected_nbytes"),
    [
        ((4,), "f8", 32),
        ((3,), b"b", 3),
    ],
)
def test_unpack_allows_numpy_reconstruct_under_cap(shape, dtype_spec, expected_nbytes):
    import numpy as np
    from numpy._core.multiarray import _reconstruct

    class OkArray:
        def __reduce__(self):
            return (_reconstruct, (np.ndarray, shape, dtype_spec))

    payload = pickle.dumps(OkArray(), protocol=5)
    res = unpack_pickle_frame(payload)
    assert res.nbytes == expected_nbytes


def test_numpy_arbitrary_submodule_import_fails():
    import pickle
    import numpy as np
    from plugin.framework.ipc import unpack_pickle_frame

    class BadNumpy:
        def __reduce__(self):
            return (np.ctypeslib.load_library, ("libc.so.6", "/lib"))

    payload = pickle.dumps(BadNumpy(), protocol=5)
    with pytest.raises(ValueError, match="is not allowed"):
        unpack_pickle_frame(payload)

def test_read_pickle_frame_win32_is_alive_honored(monkeypatch):
    import sys
    from plugin.framework import ipc
    from unittest.mock import MagicMock

    monkeypatch.setattr(sys, "platform", "win32")

    stream = MagicMock()
    stream.fileno.return_value = 3
    # Return valid frame size (e.g. 4 bytes length) to prevent IpcFrameError
    stream.read.side_effect = [b"\x00\x00\x00\x04", b"\x00\x00\x00\x00"]
    monkeypatch.setattr(ipc, "_peek_pipe_bytes_available", lambda fd: 4)

    # is_alive returning True should NOT cause TimeoutExpired
    res = ipc.read_pickle_frame(stream, 1.0, is_alive=lambda: True, unpacker=lambda _payload: {"ok": 1})
    assert res == {"ok": 1}

    # is_alive returning False drains queued bytes when peek > 0
    stream.read.side_effect = [b"\x00\x00\x00\x04", b"\x00\x00\x00\x00"]
    assert ipc.read_pickle_frame(stream, 1.0, is_alive=lambda: False, unpacker=lambda _payload: {"ok": 1}) == {"ok": 1}

    # When peek is 0 and is_alive is False, it returns None without sleeping out the deadline
    slept = _patch_ipc_clock(monkeypatch, time.monotonic)
    stream.read.reset_mock()
    monkeypatch.setattr(ipc, "_peek_pipe_bytes_available", lambda fd: 0)
    assert ipc.read_pickle_frame(stream, 10.0, is_alive=lambda: False, unpacker=lambda _payload: {"ok": 1}) is None
    stream.read.assert_not_called()
    assert slept == []




def test_get_child_ipc_stream_prefers_claim_over_replaced_stdout(monkeypatch):
    """A replaced sys.stdout must not hide the pipe claim_ipc_channel already took."""
    from plugin.framework import ipc

    claimed = io.BytesIO()
    monkeypatch.setattr(ipc, "_child_ipc_stream", claimed)

    class Replaced:
        def fileno(self) -> int:
            return 7

        buffer = io.BytesIO()

    monkeypatch.setattr(sys, "stdout", Replaced())
    assert ipc.get_child_ipc_stream() is claimed


def test_get_child_ipc_stream_uses_stdout_buffer_when_unclaimed(monkeypatch):
    from plugin.framework import ipc

    monkeypatch.setattr(ipc, "_child_ipc_stream", None)
    buf = io.BytesIO()

    class Stdout:
        buffer = buf

        def fileno(self) -> int:
            return 1

    monkeypatch.setattr(sys, "stdout", Stdout())
    assert ipc.get_child_ipc_stream() is buf


def test_claim_ipc_channel_redirects_stdout_and_preserves_framing():
    """print() in child must go to stderr and not corrupt the claimed IPC channel."""
    code = """
import os, sys
from plugin.framework.ipc import claim_ipc_channel, write_json_line

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


def test_claim_ipc_stdin_returns_raw_and_does_not_read_ahead(monkeypatch):
    from plugin.framework import ipc

    monkeypatch.setattr(ipc, "_child_ipc_stdin", None)
    read_fd, write_fd = os.pipe()
    reader = os.fdopen(read_fd, "rb")

    class _FakeStdStream:
        def __init__(self, buf):
            self.buffer = buf

    monkeypatch.setattr(sys, "stdin", _FakeStdStream(reader))
    claimed = None
    try:
        claimed = ipc.claim_ipc_stdin()
        assert isinstance(claimed, io.FileIO)
        os.write(write_fd, b"abcdef")
        assert claimed.read(2) == b"ab"
        if sys.platform != "win32":
            os.set_blocking(claimed.fileno(), False)
            assert os.read(claimed.fileno(), 16) == b"cdef"
        assert ipc.claim_ipc_stdin() is claimed
        assert ipc.get_child_ipc_stdin() is claimed
    finally:
        try:
            os.close(write_fd)
        except OSError:
            pass
        if claimed is not None:
            try:
                claimed.close()
            except OSError:
                pass
        else:
            try:
                reader.close()
            except OSError:
                pass


def test_claim_ipc_stdin_does_not_cache_bytesio(monkeypatch):
    from plugin.framework import ipc

    monkeypatch.setattr(ipc, "_child_ipc_stdin", None)

    class _FakeStdStream:
        def __init__(self, buf):
            self.buffer = buf

    b1 = io.BytesIO(b"one")
    monkeypatch.setattr(sys, "stdin", _FakeStdStream(b1))
    assert ipc.claim_ipc_stdin() is b1
    assert ipc._child_ipc_stdin is None

    b2 = io.BytesIO(b"two")
    monkeypatch.setattr(sys, "stdin", _FakeStdStream(b2))
    assert ipc.claim_ipc_stdin() is b2
    assert ipc._child_ipc_stdin is None


def test_exchange_tool_call_win32_drain_drops_raw_stdin_tail(monkeypatch):
    import threading

    from plugin.framework import ipc

    in_r, in_w = os.pipe()
    out_r, out_w = os.pipe()
    stdin_buf = os.fdopen(in_r, "rb", buffering=0)
    stdout_buf = os.fdopen(out_w, "wb", buffering=0)
    stdout_reader = os.fdopen(out_r, "rb", buffering=0)

    class _PipeEnd:
        def __init__(self, buffer: object) -> None:
            self.buffer = buffer

    monkeypatch.setattr(sys, "stdin", _PipeEnd(stdin_buf))
    monkeypatch.setattr(sys, "stdout", _PipeEnd(stdout_buf))
    monkeypatch.setattr(ipc, "_child_ipc_stdin", stdin_buf)
    if sys.platform != "win32":
        import fcntl
        import struct
        import termios

        def _fake_peek(fd: int) -> int:
            res = fcntl.ioctl(fd, termios.FIONREAD, struct.pack("i", 0))
            return struct.unpack("i", res)[0]

        monkeypatch.setattr(ipc, "_peek_pipe_bytes_available", _fake_peek)
    if ipc.sys.platform != "win32":
        monkeypatch.setattr(ipc.sys, "platform", "win32")

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
        first_req = read_pickle_frame(stdout_reader, 2.0, require_dict=True)
        second_req = read_pickle_frame(stdout_reader, 2.0, require_dict=True)
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


def test_write_child_ipc_frame_holds_tool_call_lock():
    class GateStream:
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

    buf = io.BytesIO()
    stream = GateStream(buf)
    msg = {"status": "ok", "value": 42}
    t = threading.Thread(target=lambda: write_child_ipc_frame(stream, msg))
    t.start()
    try:
        assert stream.first_write_entered.wait(timeout=2.0)
        assert plugin.framework.ipc._tool_call_lock.acquire(blocking=False) is False
    finally:
        stream.release_write.set()
        t.join(timeout=2.0)

    buf.seek(0)
    payload = read_frame_payload(buf)
    assert payload is not None
    assert unpack_pickle_frame(payload) == msg


def test_read_frame_payload_dead_child_returns_none():
    r_fd, w_fd = os.pipe()
    try:
        reader = os.fdopen(r_fd, "rb", buffering=0)
        try:
            t0 = time.monotonic()
            res = read_frame_payload(reader, 5.0, is_alive=lambda: False)
            assert res is None
            assert time.monotonic() - t0 < 1.5
        finally:
            reader.close()
    finally:
        os.close(w_fd)


def test_read_frame_payload_stop_checker_raises_timeout_expired():
    r_fd, w_fd = os.pipe()
    try:
        reader = os.fdopen(r_fd, "rb", buffering=0)
        try:
            scope = SendCancellation()
            scope.cancel()
            t0 = time.monotonic()
            with pytest.raises(subprocess.TimeoutExpired) as exc_info:
                read_frame_payload(reader, 30.0, cancel=scope)
            assert exc_info.value.timeout == 30.0
            assert time.monotonic() - t0 < 1.0
        finally:
            reader.close()
    finally:
        os.close(w_fd)


def test_read_frame_payload_explicit_deadline():
    r_fd, w_fd = os.pipe()
    try:
        reader = os.fdopen(r_fd, "rb", buffering=0)
        try:
            t0 = time.monotonic()
            with pytest.raises(subprocess.TimeoutExpired) as exc_info:
                read_frame_payload(reader, 12.0, deadline=time.monotonic() + 0.3)
            elapsed = time.monotonic() - t0
            assert exc_info.value.timeout == 12.0
            assert 0.2 <= elapsed < 2.0
        finally:
            reader.close()
    finally:
        os.close(w_fd)


def test_read_frame_payload_partial_frame_raises_partial():
    r_fd, w_fd = os.pipe()
    try:
        os.write(w_fd, b"\x00\x00")
        reader = os.fdopen(r_fd, "rb", buffering=0)
        try:
            with pytest.raises(IpcPartialFrameTimeout, match="timeout mid-frame"):
                read_frame_payload(reader, 30.0, deadline=time.monotonic() + 0.3)
        finally:
            reader.close()
    finally:
        os.close(w_fd)


def test_read_frame_payload_default_max_payload_bytes_applied():
    too_big = struct.pack("!I", DEFAULT_MAX_PAYLOAD_BYTES + 1)
    with pytest.raises(IpcFrameError):
        read_frame_payload(io.BytesIO(too_big), 2.0)


class TestReadFramePayloadWithTimeoutNoFd:
    """Tests for the no-fd (BytesIO / thread join) reader."""

    def test_reads_valid_response(self):
        response = {"status": "ok", "result": 42, "id": "test"}
        raw = pack_pickle_frame(response)
        stdout = io.BytesIO(raw)
        got = read_frame_payload(stdout, timeout_sec=5.0)
        assert got is not None
        decoded = pickle.loads(got)
        assert decoded["status"] == "ok"
        assert decoded["result"] == 42

    def test_returns_none_on_eof(self):
        stdout = io.BytesIO(b"")
        got = read_frame_payload(stdout, timeout_sec=2.0)
        assert got is None

    def test_raises_on_short_header(self):
        stdout = io.BytesIO(b"\x00\x00")
        with pytest.raises(IpcFrameReadError):
            read_frame_payload(stdout, timeout_sec=2.0)

    def test_raises_on_truncated_payload(self):
        header = struct.pack("!I", 100)
        stdout = io.BytesIO(header + b"short")
        with pytest.raises(IpcFrameReadError):
            read_frame_payload(stdout, timeout_sec=2.0)

    def test_timeout_raises(self):
        """A blocking read that never yields data should raise TimeoutExpired."""

        class SlowIO(io.RawIOBase):
            def readable(self):
                return True

            def readinto(self, b):
                time.sleep(10)
                return 0

        slow = io.BufferedReader(SlowIO())
        with pytest.raises(subprocess.TimeoutExpired):
            read_frame_payload(slow, timeout_sec=1.0)

    def test_propagates_read_error(self):
        class ErrorIO(io.RawIOBase):
            def readable(self):
                return True

            def readinto(self, b):
                raise IOError("pipe broken")

        broken = io.BufferedReader(ErrorIO())
        with pytest.raises(IOError, match="pipe broken"):
            read_frame_payload(broken, timeout_sec=2.0)


def test_host_pickle_leaf_types():
    import datetime
    import decimal
    from plugin.framework.ipc import (
        HOST_PICKLE_LEAF_TYPES,
        PICKLE_PROTOCOL,
        _SafeUnpickler,
    )

    sample_values = [
        None,
        True,
        False,
        42,
        3.14,
        "hello",
        b"bytes",
        bytearray(b"bytearray"),
        1 + 2j,
    ]
    for val in sample_values:
        assert isinstance(val, HOST_PICKLE_LEAF_TYPES)
        frame = pickle.dumps(val, protocol=PICKLE_PROTOCOL)
        loaded = _SafeUnpickler(io.BytesIO(frame)).load()
        assert loaded == val
        assert type(loaded) is type(val)

    for rejected in [datetime.datetime(2026, 8, 13), decimal.Decimal("1.5")]:
        frame = pickle.dumps(rejected, protocol=PICKLE_PROTOCOL)
        with pytest.raises(pickle.UnpicklingError):
            _SafeUnpickler(io.BytesIO(frame)).load()



