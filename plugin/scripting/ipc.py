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

import json
import pickle
import select
import struct
import subprocess
import sys
import time
from typing import Any, Callable, IO

PICKLE_PROTOCOL = 5
FRAME_HEADER_SIZE = 4


class IpcFrameError(ValueError):
    """Raised when a framed IPC message has an invalid length or payload."""


def _validate_frame_size(size: int, *, max_payload_bytes: int | None, frame_label: str) -> None:
    if size <= 0 or (max_payload_bytes is not None and size > max_payload_bytes):
        raise IpcFrameError(f"Invalid {frame_label} size: {size}")


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
    _validate_frame_size(size, max_payload_bytes=max_payload_bytes, frame_label=frame_label)
    payload = reader(size)
    if len(payload) < size:
        return None
    return payload


def unpack_pickle_frame(payload: bytes) -> Any:
    """Decode one trusted Pickle5 payload read from a private subprocess pipe."""
    try:
        return pickle.loads(payload)  # nosec B301
    except pickle.UnpicklingError as exc:
        raise ValueError(str(exc)) from exc


def read_pickle_frame(
    stream: IO[bytes],
    *,
    max_payload_bytes: int | None = None,
    frame_label: str = "IPC frame",
    require_dict: bool = False,
) -> Any | None:
    """Read and unpickle one length-prefixed message. Return None on EOF/truncation."""
    payload = read_frame_payload(stream, max_payload_bytes=max_payload_bytes, frame_label=frame_label)
    if payload is None:
        return None
    decoded = unpack_pickle_frame(payload)
    if require_dict and not isinstance(decoded, dict):
        raise ValueError(f"{frame_label} must contain a dict")
    return decoded


def write_json_line(stream: IO[str], payload: dict[str, Any]) -> None:
    """Write one JSON object followed by a newline to a text-mode pipe."""
    stream.write(json.dumps(payload) + "\n")
    stream.flush()


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
        time.sleep(min(0.001, deadline - time.monotonic()))

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
        ready, _, _ = select.select([stream], [], [], max(0.0, timeout_sec))
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
