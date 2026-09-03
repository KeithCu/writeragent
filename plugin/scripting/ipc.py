# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared subprocess IPC framing helpers.

This module owns the outer pipe protocol only: Pickle5 frames for trusted private
binary subprocess pipes, and newline-delimited JSON for small text protocols.
Payload-specific envelopes such as split_grid remain in payload_codec.py.
"""
from __future__ import annotations

import builtins
import io
import logging
import os
import json
import pickle
import select
import struct
import subprocess
import sys
import time
from typing import Any, Callable, IO

log = logging.getLogger("writeragent.scripting.ipc")

PICKLE_PROTOCOL = 5
FRAME_HEADER_SIZE = 4

# Shared cap for editor IPC and the venv-worker host read path. A corrupt 4-byte
# length prefix without this bound can OOM the LibreOffice process. Keep editor
# and worker on the same inventory — do not pass unbounded read_frame_payload
# on either path.
DEFAULT_MAX_PAYLOAD_BYTES = 16 * 1024 * 1024

# Host unpickle of child/editor frames: builtins only. Protocol 5 bytes (split_grid
# buffers) do not go through find_class. Do not add numpy or application classes.
_SAFE_PICKLE_BUILTINS = frozenset({
    "dict",
    "list",
    "tuple",
    "set",
    "frozenset",
    "bytes",
    "bytearray",
    "str",
    "int",
    "float",
    "complex",
    "bool",
})


class _SafeUnpickler(pickle.Unpickler):
    def find_class(self, module: str, name: str) -> Any:
        if module in ("builtins", "__builtin__") and name in _SAFE_PICKLE_BUILTINS:
            return getattr(builtins, name)
        # Child split_grid / ndarray results reconstruct via numpy (host then unpacks to lists).
        if module == "numpy" or module.startswith("numpy."):
            return super().find_class(module, name)
        raise pickle.UnpicklingError(f"global {module}.{name} is not allowed")


class IpcFrameError(ValueError):
    """Raised when a framed IPC message has an invalid length or payload."""


def _validate_frame_size(size: int, *, max_payload_bytes: int | None, frame_label: str) -> None:
    if size <= 0 or (max_payload_bytes is not None and size > max_payload_bytes):
        header = struct.pack("!I", size & 0xFFFFFFFF)
        raise IpcFrameError(
            f"Invalid {frame_label} size: {size} (header={header!r})"
        )


def pack_pickle_frame(message: Any, *, max_payload_bytes: int | None = None) -> bytes:
    """Return one Pickle5 message framed with a 4-byte big-endian length prefix."""
    payload = pickle.dumps(message, protocol=PICKLE_PROTOCOL)
    if max_payload_bytes is not None and len(payload) > max_payload_bytes:
        raise IpcFrameError(f"Pickle frame exceeds maximum payload size: {len(payload)}")
    return struct.pack("!I", len(payload)) + payload


def write_pickle_frame(stream: IO[bytes], message: Any, *, max_payload_bytes: int | None = None) -> None:
    """Write one Pickle5 length-prefixed message to a binary pipe."""
    stream.write(pack_pickle_frame(message, max_payload_bytes=max_payload_bytes))
    stream.flush()


def _unread_pipe_bytes(stream: IO[bytes], n: int = 512) -> bytes:
    """Best-effort leftover bytes after a bad length prefix (non-blocking on real pipes)."""
    try:
        fd = stream.fileno()
    except (AttributeError, OSError, ValueError, io.UnsupportedOperation):
        fd = None
    if isinstance(fd, int):
        # os.set_blocking is POSIX-only; skip the non-blocking peek on win32.
        # BytesIO/mocks still fall through to stream.read (no real fileno).
        if sys.platform == "win32" or not hasattr(os, "set_blocking"):
            # Do not stream.read() here — that can block on a live pipe.
            return b""
        try:
            os.set_blocking(fd, False)
            return os.read(fd, n)
        except (BlockingIOError, OSError, AttributeError, ValueError):
            return b""
    try:
        data = stream.read(n)
    except Exception:
        return b""
    return data if isinstance(data, (bytes, bytearray)) else b""


def read_frame_payload(
    stream: IO[bytes],
    *,
    max_payload_bytes: int | None = None,
    frame_label: str = "IPC frame",
    read_exact: Callable[[int], bytes] | None = None,
) -> bytes | None:
    """Read one length-prefixed payload. Return None on clean EOF or truncation."""
    reader = read_exact if read_exact is not None else stream.read
    header = reader(FRAME_HEADER_SIZE)
    if not header or len(header) < FRAME_HEADER_SIZE:
        return None
    size = struct.unpack("!I", header)[0]
    try:
        _validate_frame_size(size, max_payload_bytes=max_payload_bytes, frame_label=frame_label)
    except IpcFrameError as exc:
        rest = _unread_pipe_bytes(stream)
        # stdout_rest= is leftover pipe bytes after a garbage length prefix
        # (empty when the POSIX peek is skipped on win32).
        msg = f"{exc} stdout_rest={rest!r}"
        log.error("%s", msg)
        raise IpcFrameError(msg) from None
    payload = reader(size)
    if len(payload) < size:
        return None
    return payload


def unpack_pickle_frame(payload: bytes) -> Any:
    """Decode one Pickle5 payload; only builtin containers/scalars (defense in depth)."""
    try:
        return _SafeUnpickler(io.BytesIO(payload)).load()
    except pickle.UnpicklingError as exc:
        raise ValueError(str(exc)) from exc


def _decode_pickle_payload(
    payload: bytes | None,
    *,
    frame_label: str,
    require_dict: bool,
) -> Any | None:
    if payload is None:
        return None
    decoded = unpack_pickle_frame(payload)
    if require_dict and not isinstance(decoded, dict):
        raise ValueError(f"{frame_label} must contain a dict")
    return decoded


def read_pickle_frame(
    stream: IO[bytes],
    *,
    max_payload_bytes: int | None = None,
    frame_label: str = "IPC frame",
    require_dict: bool = False,
) -> Any | None:
    """Read and unpickle one length-prefixed message. Return None on EOF/truncation."""
    payload = read_frame_payload(stream, max_payload_bytes=max_payload_bytes, frame_label=frame_label)
    return _decode_pickle_payload(payload, frame_label=frame_label, require_dict=require_dict)


def read_pickle_frame_with_timeout(
    stream: IO[bytes],
    timeout_sec: float,
    *,
    max_payload_bytes: int | None = None,
    frame_label: str = "IPC frame",
    require_dict: bool = False,
    is_alive: Callable[[], bool] | None = None,
) -> Any | None:
    """Read one pickle frame, bounding the whole header+payload with *timeout_sec*.

    POSIX uses ``select`` in a deadline loop so a partial frame cannot hang the
    parent after the first byte. Windows uses a daemon reader thread (pipes are
    not selectable). Raises ``subprocess.TimeoutExpired`` on deadline.
    Returns None on clean EOF or truncation.
    """
    timeout_sec = max(0.0, float(timeout_sec))
    if sys.platform == "win32":
        # PeekNamedPipe, not a daemon thread blocked in ReadFile. Closing the
        # pipe while that thread is still in ReadFile crashed the xdist worker
        # (CI 33453184665: gw1 died in test_pickle_frame_timeout_on_pipe — that
        # was the Windows hang). Same poll style as _readline_with_timeout_win32.

        def _read_exact_win32(n: int) -> bytes:
            return _read_bytes_with_timeout_win32(
                stream, n, timeout_sec, cmd=frame_label
            )

        payload = read_frame_payload(
            stream,
            max_payload_bytes=max_payload_bytes,
            frame_label=frame_label,
            read_exact=_read_exact_win32,
        )
        return _decode_pickle_payload(payload, frame_label=frame_label, require_dict=require_dict)

    deadline = time.monotonic() + timeout_sec

    def _read_exact(n: int) -> bytes:
        buf = bytearray()
        while len(buf) < n:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(cmd=frame_label, timeout=timeout_sec)
            ready, _unused, _unused2 = select.select([stream], [], [], min(1.0, remaining))
            if ready:
                chunk = stream.read(n - len(buf))
                if not chunk:
                    return bytes(buf)
                buf.extend(chunk)
            elif is_alive is not None and not is_alive():
                break
        return bytes(buf)

    payload = read_frame_payload(
        stream,
        max_payload_bytes=max_payload_bytes,
        frame_label=frame_label,
        read_exact=_read_exact,
    )
    return _decode_pickle_payload(payload, frame_label=frame_label, require_dict=require_dict)


def write_json_line(stream: IO[str], payload: dict[str, Any]) -> None:
    """Write one JSON object followed by a newline to a text-mode pipe."""
    stream.write(json.dumps(payload) + "\n")
    stream.flush()


def _read_bytes_with_timeout_win32(
    stream: IO[bytes],
    n: int,
    timeout_sec: float,
    *,
    cmd: str,
) -> bytes:
    """Read *n* bytes from a Windows pipe without a stuck ReadFile thread.

    Polls ``PeekNamedPipe`` until bytes are queued, then reads only what is
    available. Raises ``TimeoutExpired`` on deadline. Falls back to a blocking
    ``read`` when ``fileno()`` is not a real pipe fd (BytesIO / mocks).
    """
    try:
        fd = stream.fileno()
    except (AttributeError, OSError, ValueError):
        return stream.read(n)
    if not isinstance(fd, int):
        return stream.read(n)

    deadline = time.monotonic() + max(0.0, timeout_sec)
    buf = bytearray()
    while len(buf) < n:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=timeout_sec)
        avail = _peek_pipe_bytes_available(fd)
        if avail is None:
            chunk = stream.read(n - len(buf))
            if not chunk:
                return bytes(buf)
            buf.extend(chunk)
            continue
        if avail > 0:
            chunk = stream.read(min(n - len(buf), avail))
            if not chunk:
                return bytes(buf)
            buf.extend(chunk)
            continue
        # Re-read the clock: PeekNamedPipe can cross the deadline.
        time.sleep(max(0.0, min(0.001, deadline - time.monotonic())))
    return bytes(buf)


def _peek_pipe_bytes_available(fd: int) -> int | None:
    """Return queued byte count for a Windows pipe fd, or None when the pipe is closed."""
    if sys.platform != "win32":
        return None
    import ctypes
    import msvcrt
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    peek_named_pipe = kernel32.PeekNamedPipe
    peek_named_pipe.argtypes = [
        wintypes.HANDLE,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.DWORD),
    ]
    peek_named_pipe.restype = wintypes.BOOL

    avail = wintypes.DWORD(0)
    handle = msvcrt.get_osfhandle(fd)
    if peek_named_pipe(handle, None, 0, None, ctypes.byref(avail), None):
        return int(avail.value)
    if ctypes.get_last_error() in (109, 233):  # BROKEN_PIPE / NO_DATA
        return None
    raise OSError(ctypes.get_last_error(), ctypes.FormatError(ctypes.get_last_error()))


def _readline_with_timeout_win32(stream: IO[str], timeout_sec: float, *, cmd: str = "IPC JSON line") -> str:
    """Windows path: poll pipe with PeekNamedPipe; readline only when bytes are queued."""
    try:
        fd = stream.fileno()
    except (AttributeError, OSError, ValueError):
        return stream.readline()
    # MagicMock.fileno() returns another mock that coerces to int; PeekNamedPipe then
    # hits the console FD and raises errno 1. Match the POSIX isinstance(fd, int) gate.
    if not isinstance(fd, int):
        return stream.readline()

    deadline = time.monotonic() + max(0.0, timeout_sec)
    while time.monotonic() < deadline:
        avail = _peek_pipe_bytes_available(fd)
        if avail is None:
            return stream.readline()
        if avail > 0:
            return stream.readline()
        # PeekNamedPipe can cross the deadline; sleep(negative) is ValueError.
        time.sleep(max(0.0, min(0.001, deadline - time.monotonic())))

    raise subprocess.TimeoutExpired(cmd=cmd, timeout=timeout_sec)


def _readline_with_timeout(stream: IO[str], timeout_sec: float | None) -> str:
    if timeout_sec is None:
        return stream.readline()

    # Windows select.select() only supports sockets, not pipes (WinError 10038).
    if sys.platform == "win32":
        return _readline_with_timeout_win32(stream, timeout_sec)

    try:
        fd = stream.fileno()
    except (AttributeError, OSError, ValueError):
        fd = None
    if isinstance(fd, int):
        ready, _unused, _unused2 = select.select([stream], [], [], max(0.0, timeout_sec))
        if not ready:
            raise subprocess.TimeoutExpired(cmd="IPC JSON line", timeout=timeout_sec)
        return stream.readline()

    return stream.readline()


def read_json_line(stream: IO[str], *, timeout_sec: float | None = None) -> dict[str, Any] | None:
    """Read one newline-delimited JSON object. Return None on clean EOF."""
    line = _readline_with_timeout(stream, timeout_sec)
    if not line:
        return None
    try:
        payload = json.loads(line.strip())
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON line: {line!r}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"JSON line must contain an object: {payload!r}")
    return payload
