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
import threading
import time
import uuid
from typing import Any, BinaryIO, Callable, IO, cast
from weakref import WeakKeyDictionary

log = logging.getLogger("writeragent.scripting.ipc")

PICKLE_PROTOCOL = 5
FRAME_HEADER_SIZE = 4

# Child writes this before user code, trusted actions, or a ppt turn. The host
# must not replay the request after seeing it: in-process side effects may
# already have run without a tool_call frame. A death before this frame is
# still a failed start and may be retried.
EXEC_STARTED = "exec_started"

# Shared cap for editor IPC and the venv-worker host read path. A corrupt 4-byte
# length prefix without this bound can OOM the LibreOffice process. Keep editor
# and worker on the same inventory — do not pass unbounded read_frame_payload
# on either path.
DEFAULT_MAX_PAYLOAD_BYTES = 16 * 1024 * 1024

_child_ipc_stream: BinaryIO | None = None


def claim_ipc_channel() -> BinaryIO:
    """Claim stdout (fd 1) for child IPC and redirect fd 1 to stderr (fd 2).

    Returns a private, unbuffered binary stream connected to the original stdout fd.
    Any stray print() or library writes to stdout will land on stderr, preventing
    protocol corruption.
    """
    global _child_ipc_stream
    if _child_ipc_stream is not None:
        return _child_ipc_stream
    try:
        fileno = sys.stdout.fileno()
    except (AttributeError, io.UnsupportedOperation, OSError):
        fileno = None
    if fileno != 1:
        return cast("BinaryIO", getattr(sys.stdout, "buffer", sys.stdout))

    try:
        sys.stdout.flush()
    except Exception:
        pass
    try:
        sys.stderr.flush()
    except Exception:
        pass
    ipc_fd = os.dup(1)
    os.dup2(2, 1)
    _child_ipc_stream = cast("BinaryIO", os.fdopen(ipc_fd, "wb", buffering=0))
    return _child_ipc_stream


def get_child_ipc_stream() -> BinaryIO:
    """Return the private child IPC stream if claimed, else sys.stdout.buffer."""
    global _child_ipc_stream
    try:
        fileno = sys.stdout.fileno()
    except (AttributeError, io.UnsupportedOperation, OSError):
        fileno = None
    if fileno != 1:
        return cast("BinaryIO", getattr(sys.stdout, "buffer", sys.stdout))
    if _child_ipc_stream is not None:
        return _child_ipc_stream
    return cast("BinaryIO", getattr(sys.stdout, "buffer", sys.stdout))


# Host unpickle of child/editor frames: builtins, plus the NumPy reconstruct
# entry points a ndarray/dtype pickle actually calls. Protocol 5 bytes
# (split_grid buffers) do not go through find_class. Do not allow every
# numpy.* name: REDUCE runs the resolved callable inside LibreOffice, so
# numpy.ctypeslib.load_library would execute in the host process.
_SAFE_PICKLE_BUILTINS = frozenset({
    "complex",
})

# Names a NumPy pickle REDUCE actually invokes (ndarray via _frombuffer,
# dtype, scalar). Not load, save, or ctypeslib.
_NUMPY_RECONSTRUCT_PAIRS = frozenset({
    ("numpy._core.numeric", "_frombuffer"),
    ("numpy.core.numeric", "_frombuffer"),
    ("numpy._core.multiarray", "_reconstruct"),
    ("numpy.core.multiarray", "_reconstruct"),
    ("numpy._core.multiarray", "scalar"),
    ("numpy.core.multiarray", "scalar"),
    ("numpy", "dtype"),
    ("numpy", "ndarray"),
})


class _SafeUnpickler(pickle.Unpickler):
    def find_class(self, module: str, name: str) -> Any:
        if module in ("builtins", "__builtin__") and name in _SAFE_PICKLE_BUILTINS:
            return getattr(builtins, name)
        if (module, name) in _NUMPY_RECONSTRUCT_PAIRS:
            mod = sys.modules.get(module)
            if mod is not None:
                return getattr(mod, name)
            return super().find_class(module, name)
        raise pickle.UnpicklingError(f"global {module}.{name} is not allowed")


class IpcFrameError(ValueError):
    """Raised when a framed IPC message has an invalid length or payload."""


class IpcPayloadSizeError(IpcFrameError):
    """Raised when an outgoing IPC frame payload exceeds the maximum allowed bytes."""


class IpcFrameReadError(IpcFrameError):
    """Raised when reading a framed IPC message fails due to invalid size or stream desync."""


class UserStopped(BaseException):
    """Host refused a tool call because the user pressed Stop.

    ``BaseException`` so a script ``except Exception`` cannot treat Stop as an
    ordinary tool failure and keep calling ``wa.*``. The sandbox and harness
    turn this into a terminal frame with code ``USER_STOPPED``.
    """


# load() failures that are not already ValueError. A protocol header with no
# body raises EOFError from the C unpickler; UnpicklingError is not a
# ValueError. Callers (venv worker, editor) only treat ValueError as a bad frame.
_PICKLE_LOAD_ERRORS = (
    pickle.UnpicklingError,
    EOFError,
    AttributeError,
    ImportError,
    IndexError,
    TypeError,
    OverflowError,
    RecursionError,
    MemoryError,
)


def _validate_frame_size(size: int, *, max_payload_bytes: int | None, frame_label: str) -> None:
    if size <= 0 or (max_payload_bytes is not None and size > max_payload_bytes):
        header = struct.pack("!I", size & 0xFFFFFFFF)
        raise IpcFrameReadError(
            f"Invalid {frame_label} size: {size} (header={header!r})"
        )


def pack_pickle_frame(
    message: Any, *, max_payload_bytes: int | None = DEFAULT_MAX_PAYLOAD_BYTES
) -> bytes:
    """Return one Pickle5 message framed with a 4-byte big-endian length prefix.

    The default used to be None, so an omitted write was uncapped while the
    matching read is capped and leaves extra bytes on the pipe. Pass None only
    to opt out.
    """
    payload = pickle.dumps(message, protocol=PICKLE_PROTOCOL)
    if max_payload_bytes is not None and len(payload) > max_payload_bytes:
        raise IpcPayloadSizeError(f"Pickle frame exceeds maximum payload size: {len(payload)}")
    return struct.pack("!I", len(payload)) + payload


def write_pickle_frame(
    stream: IO[bytes], message: Any, *, max_payload_bytes: int | None = DEFAULT_MAX_PAYLOAD_BYTES
) -> None:
    """Write one Pickle5 length-prefixed message to a binary pipe.

    The default used to be None, so an omitted write was uncapped while the
    matching read is capped and leaves extra bytes on the pipe. Pass None only
    to opt out.
    """
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
        # What was wrong: set_blocking(False) was left in place. A later read
        # on this fd (compute/kokoro loops catch the frame error and read
        # again) returned None or raised BlockingIOError, which the frame
        # reader treats as EOF.
        # Why this works: the peek is only for the error text. Put the fd
        # back the way it was so the next read blocks for a real frame.
        was_blocking = True
        try:
            was_blocking = os.get_blocking(fd)
        except OSError:
            was_blocking = True
        try:
            os.set_blocking(fd, False)
            return os.read(fd, n)
        except (BlockingIOError, OSError, AttributeError, ValueError):
            return b""
        finally:
            try:
                os.set_blocking(fd, was_blocking)
            except OSError:
                pass
    try:
        data = stream.read(n)
    except Exception:
        return b""
    return data if isinstance(data, (bytes, bytearray)) else b""


def read_frame_payload(
    stream: IO[bytes],
    *,
    max_payload_bytes: int | None = DEFAULT_MAX_PAYLOAD_BYTES,
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
        raise IpcFrameReadError(msg) from None
    payload = reader(size)
    if len(payload) < size:
        return None
    return payload


def unpack_pickle_frame(payload: bytes) -> Any:
    """Decode one Pickle5 payload; only builtin containers/scalars (defense in depth)."""
    try:
        return _SafeUnpickler(io.BytesIO(payload)).load()
    except _PICKLE_LOAD_ERRORS as exc:
        # What was wrong: only UnpicklingError became ValueError. A length-valid
        # truncated payload (protocol header, no body) raises EOFError, which
        # escaped PythonWorkerManager.execute and left the child on the pipe.
        # Why this works: the same ValueError contract the worker already maps
        # to WORKER_IPC_ERROR, and it kills that child instead of replaying.
        raise ValueError(str(exc)) from exc


def _decode_pickle_payload(
    payload: bytes | None,
    *,
    frame_label: str,
    require_dict: bool,
    unpacker: Callable[[bytes], Any] | None = None,
) -> Any | None:
    if payload is None:
        return None
    decode_fn = unpacker if unpacker is not None else unpack_pickle_frame
    decoded = decode_fn(payload)
    if require_dict and not isinstance(decoded, dict):
        raise ValueError(f"{frame_label} must contain a dict")
    return decoded


def read_pickle_frame(
    stream: IO[bytes],
    *,
    max_payload_bytes: int | None = DEFAULT_MAX_PAYLOAD_BYTES,
    frame_label: str = "IPC frame",
    require_dict: bool = False,
    unpacker: Callable[[bytes], Any] | None = None,
) -> Any | None:
    """Read and unpickle one length-prefixed message. Return None on EOF/truncation."""
    payload = read_frame_payload(stream, max_payload_bytes=max_payload_bytes, frame_label=frame_label)
    return _decode_pickle_payload(payload, frame_label=frame_label, require_dict=require_dict, unpacker=unpacker)


# One lock for every venv → host tool_call on this pipe. The LibrePy named-script
# fallback used to write a frame with no lock, so two calls could interleave.
_tool_call_lock = threading.Lock()


def _pause_script_alarm() -> tuple[int, float] | None:
    """Turn off SIGALRM for the tool_call read. Return (seconds left, monotonic start).

    The script alarm used to fire inside this stdin read. The harness then wrote
    an error frame while the host was still writing the tool reply, and the next
    cell read that reply as a request.
    """
    if sys.platform == "win32":
        return None
    try:
        import signal

        remaining = signal.alarm(0)
    except (AttributeError, ValueError, OSError):
        return None
    if not remaining:
        return None
    return int(remaining), time.monotonic()


# Upper bound for discarding a desynced tool_call tail. Queued bytes return
# immediately; the deadline only stops a peer that keeps the pipe full.
_TOOL_CALL_MISMATCH_DRAIN_SEC = 0.05


def _stream_fileno(stream: IO[bytes]) -> int | None:
    try:
        fd = stream.fileno()
    except (AttributeError, OSError, ValueError, io.UnsupportedOperation):
        return None
    if isinstance(fd, int):
        return fd
    return None


def _drain_queued_pipe_bytes(
    stream: IO[bytes], *, timeout_sec: float = _TOOL_CALL_MISMATCH_DRAIN_SEC
) -> None:
    """Drop bytes already queued on *stream* after a tool_call id mismatch.

    What was wrong: ``exchange_tool_call`` consumed one pickle frame, then
    raised ``RuntimeError`` when ``id`` did not match. ``sys.stdin.buffer``
    is a BufferedReader, so ``read(n)`` often pulls the next frame into that
    wrapper (the kernel pipe can look empty). The raise discarded the foreign
    frame and left the tail in place. Every later call under
    ``_tool_call_lock`` read that tail as its reply, so the worker stayed
    one frame behind for the rest of the process.
    Why this works: the caller still holds the lock. This reads through the
    same stream, with the fd non-blocking and a deadline, so both the
    wrapper cache and the kernel queue are dropped. ``os.read`` is not used:
    it skips the cache and the next ``stream.read`` would still return the
    tail. Blocking mode is restored so the next call waits for a new frame.
    Windows pipes that reject non-blocking mode fall back to PeekNamedPipe,
    same as ``_unread_pipe_bytes`` (kernel bytes only).
    """
    deadline = time.monotonic() + max(0.0, float(timeout_sec))
    fd = _stream_fileno(stream)
    if fd is None:
        try:
            stream.read()
        except Exception:
            log.exception("tool_call id-mismatch drain failed")
        return
    if hasattr(os, "set_blocking") and _drain_nonblocking_stream(stream, fd, deadline):
        return
    _drain_peek_available(stream, fd, deadline)


def _drain_nonblocking_stream(stream: IO[bytes], fd: int, deadline: float) -> bool:
    """Drain *stream* without blocking. Return False if the fd cannot be set."""
    # Win32 anonymous pipes: set_blocking is not the peek path. A failed
    # set_blocking must not fall through to a blocking stream.read.
    if sys.platform == "win32":
        return False
    was_blocking = True
    try:
        was_blocking = os.get_blocking(fd)
    except OSError:
        was_blocking = True
    try:
        os.set_blocking(fd, False)
    except (OSError, AttributeError):
        return False
    try:
        while time.monotonic() < deadline:
            try:
                chunk = stream.read(65536)
            except (BlockingIOError, InterruptedError):
                return True
            except OSError:
                log.exception("tool_call id-mismatch drain failed")
                return True
            # Non-blocking FileIO/BufferedReader returns None when the queue
            # is empty (not only b""). Stop; do not spin until the deadline.
            if not chunk:
                return True
        return True
    finally:
        # What was wrong on the length-prefix peek: set_blocking(False) was
        # left in place, and the next frame read treated EAGAIN as EOF.
        try:
            os.set_blocking(fd, was_blocking)
        except OSError:
            pass


def _drain_peek_available(stream: IO[bytes], fd: int, deadline: float) -> None:
    """Windows: drop only bytes PeekNamedPipe already reports."""
    if sys.platform != "win32":
        return
    while time.monotonic() < deadline:
        try:
            avail = _peek_pipe_bytes_available(fd)
        except OSError:
            log.exception("tool_call id-mismatch drain failed")
            return
        if not avail:
            return
        try:
            chunk = stream.read(min(int(avail), 65536))
        except OSError:
            log.exception("tool_call id-mismatch drain failed")
            return
        if not chunk:
            return


def _resume_script_alarm(paused: tuple[int, float] | None) -> bool:
    """Restore the script alarm. Return True when the budget was already spent.

    The tool reply has been consumed by then, so the timeout is a normal script
    error instead of a desynced pipe.
    """
    if paused is None:
        return False
    remaining, started = paused
    left = remaining - (time.monotonic() - started)
    if left <= 0:
        return True
    if sys.platform == "win32":
        return False
    try:
        import signal

        signal.alarm(max(1, int(left + 0.999)))
    except (AttributeError, ValueError, OSError):
        return False
    return False


def exchange_tool_call(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Write one tool_call frame and read its response. Check the echoed id.

    The host echoes ``id`` on both success and error. A mismatch means this
    response belongs to another call. Bytes already queued behind it are
    discarded before the error is raised so the next call is not paired
    with that tail.
    """
    call_id = str(uuid.uuid4())
    request = {"type": "tool_call", "id": call_id, "tool": tool_name, "args": args}
    paused = _pause_script_alarm()
    overdue = False
    try:
        with _tool_call_lock:
            # What was wrong: exchange_tool_call wrote to sys.stdout.buffer, which lands on
            # stderr when child stdout is dup2'd to protect IPC framing from stray prints.
            # Why this fixes it: get_child_ipc_stream() writes to the original claimed IPC stream.
            write_pickle_frame(
                get_child_ipc_stream(),
                request,
                max_payload_bytes=DEFAULT_MAX_PAYLOAD_BYTES,
            )
            response = read_pickle_frame(
                sys.stdin.buffer,
                require_dict=True,
                max_payload_bytes=DEFAULT_MAX_PAYLOAD_BYTES,
                frame_label="tool_call response",
            )
            # Drain while _tool_call_lock is still held. Releasing it first
            # lets the next exchange read the tail this call already pulled.
            # A drain failure must not replace the id mismatch error.
            if isinstance(response, dict) and response.get("id") != call_id:
                try:
                    _drain_queued_pipe_bytes(sys.stdin.buffer)
                except Exception:
                    log.exception("tool_call id-mismatch drain failed")
    finally:
        overdue = _resume_script_alarm(paused)
    if overdue:
        raise TimeoutError(
            "Code execution exceeded the maximum execution time during a tool call"
        )
    if response is None:
        raise ConnectionError("Lost connection to LibreOffice host during tool call")
    if response.get("id") != call_id:
        raise RuntimeError(
            f"tool_call response id {response.get('id')!r} does not match request {call_id!r}"
        )
    # What was wrong: host_rpc sends code USER_STOPPED, and this dropped it
    # into RuntimeError. A script ``except Exception`` kept running after Stop.
    # Why this works: UserStopped is BaseException, so that handler does not
    # run and the turn ends with the same code.
    if response.get("code") == "USER_STOPPED":
        message = response.get("message") or response.get("error") or "Stopped by user."
        raise UserStopped(str(message))
    if response.get("status") == "error":
        # What was wrong: exchange_tool_call dropped response.get("code") when raising RuntimeError.
        # How: RuntimeError was instantiated with only the message/error string.
        # Why this change fixes it: copy the code attribute onto the raised RuntimeError so callers can inspect it.
        err = RuntimeError(response.get("message", response.get("error", "Unknown error")))
        code = response.get("code")
        if code is not None:
            setattr(err, "code", code)
        raise err
    return response.get("result", {})


def read_pickle_frame_with_timeout(
    stream: IO[bytes],
    timeout_sec: float,
    *,
    max_payload_bytes: int | None = DEFAULT_MAX_PAYLOAD_BYTES,
    frame_label: str = "IPC frame",
    require_dict: bool = False,
    is_alive: Callable[[], bool] | None = None,
    unpacker: Callable[[bytes], Any] | None = None,
) -> Any | None:
    """Read one pickle frame, bounding the whole header+payload with *timeout_sec*.

    POSIX uses ``select`` in a deadline loop so a partial frame cannot hang the
    parent after the first byte. Windows uses a daemon reader thread (pipes are
    not selectable). Raises ``subprocess.TimeoutExpired`` on deadline.
    Returns None on clean EOF or truncation.
    """
    timeout_sec = max(0.0, float(timeout_sec))
    deadline = time.monotonic() + timeout_sec

    if sys.platform == "win32":
        # PeekNamedPipe, not a daemon thread blocked in ReadFile. Closing the
        # pipe while that thread is still in ReadFile crashed the xdist worker
        # (CI 33453184665: gw1 died in test_pickle_frame_timeout_on_pipe — that
        # was the Windows hang). Same poll style as _readline_with_timeout_win32.

        alive_fn = is_alive
        stop_checker = (lambda: not alive_fn()) if alive_fn is not None else None

        def _read_exact_win32(n: int) -> bytes:
            return _read_bytes_with_timeout_win32(stream, n, deadline, timeout_sec, cmd=frame_label, stop_checker=stop_checker)

        payload = read_frame_payload(
            stream,
            max_payload_bytes=max_payload_bytes,
            frame_label=frame_label,
            read_exact=_read_exact_win32,
        )
        return _decode_pickle_payload(payload, frame_label=frame_label, require_dict=require_dict, unpacker=unpacker)

    def _read_exact(n: int) -> bytes:
        buf = bytearray()
        while len(buf) < n:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                if len(buf) > 0:
                    raise ConnectionError(f"{frame_label} stream desynchronized: timeout mid-frame")
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
    return _decode_pickle_payload(payload, frame_label=frame_label, require_dict=require_dict, unpacker=unpacker)


def write_json_line(stream: IO[str] | IO[bytes], payload: dict[str, Any]) -> None:
    """Write one JSON object followed by a newline to a text or binary pipe."""
    # What was wrong: write_json_line required IO[str], failing on binary IPC streams returned by claim_ipc_channel().
    # Why this fixes it: supports both text and binary streams by encoding to utf-8 when writing bytes.
    line = json.dumps(payload) + "\n"
    try:
        cast("Any", stream).write(line)
    except TypeError:
        cast("Any", stream).write(line.encode("utf-8"))
    stream.flush()


def _read_bytes_with_timeout_win32(
    stream: IO[bytes],
    n: int,
    deadline: float,
    timeout_sec: float,
    *,
    cmd: str,
    stop_checker: Callable[[], bool] | None = None,
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

    buf = bytearray()
    while len(buf) < n:
        if stop_checker and stop_checker():
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=timeout_sec)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            if len(buf) > 0:
                raise ConnectionError(f"{cmd} stream desynchronized: timeout mid-frame")
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


def _readline_with_timeout_win32(stream: IO[str], timeout_sec: float, max_bytes: int, *, cmd: str = "IPC JSON line") -> str:
    """Windows path: poll pipe with PeekNamedPipe; readline only when bytes are queued."""
    try:
        fd = stream.fileno()
    except (AttributeError, OSError, ValueError):
        return stream.readline(max_bytes + 1)
    # MagicMock.fileno() returns another mock that coerces to int; PeekNamedPipe then
    # hits the console FD and raises errno 1. Match the POSIX isinstance(fd, int) gate.
    if not isinstance(fd, int):
        return stream.readline(max_bytes + 1)

    deadline = time.monotonic() + max(0.0, timeout_sec)
    while time.monotonic() < deadline:
        avail = _peek_pipe_bytes_available(fd)
        if avail is None:
            return stream.readline(max_bytes + 1)
        if avail > 0:
            return stream.readline(max_bytes + 1)
        # PeekNamedPipe can cross the deadline; sleep(negative) is ValueError.
        time.sleep(max(0.0, min(0.001, deadline - time.monotonic())))

    raise subprocess.TimeoutExpired(cmd=cmd, timeout=timeout_sec)


# Bytes read past a newline, or a partial line saved when the deadline fires.
# Keyed by the text stream so the next read_json_line continues that line.
# TextIOWrapper.readline() is not used: after select says the fd is readable it
# still blocks until a newline, so a partial JSON line ignored the timeout.
_json_line_pending: WeakKeyDictionary[Any, bytearray] = WeakKeyDictionary()


def _pop_json_line_pending(stream: IO[str]) -> bytearray:
    try:
        return bytearray(_json_line_pending.pop(stream, b""))
    except TypeError:
        return bytearray()


def _save_json_line_pending(stream: IO[str], pending: bytearray) -> None:
    if not pending:
        return
    try:
        _json_line_pending[stream] = pending
    except TypeError:
        return


def _take_json_line(pending: bytearray) -> tuple[str, bytearray] | None:
    nl = pending.find(b"\n")
    if nl < 0:
        return None
    line = bytes(pending[: nl + 1]).decode("utf-8", errors="replace")
    return line, bytearray(pending[nl + 1 :])


def _read_available_line_bytes(fd: int) -> bytes | None:
    """Queued pipe bytes, ``b""`` on EOF, or None when nothing is ready.

    Read the fd, not ``TextIOWrapper.buffer.read1``. ``read1(4096)`` can leave
    the rest of an 8KB fill in the userspace buffer, and a later ``select`` on
    the fd then waits even though those bytes were already pulled. ``os.read``
    returns ``b""`` for EOF and raises ``BlockingIOError`` when the non-blocking
    fd has nothing, so a quiet pipe is not treated as EOF.

    The caller must be the only reader of this fd. A wrapper ``readline`` would
    desync its own buffer from this read.
    """
    if sys.platform == "win32":
        # No non-blocking pipe reads on win32; report "nothing available".
        return None
    was_blocking = True
    try:
        was_blocking = os.get_blocking(fd)
    except OSError:
        was_blocking = True
    try:
        os.set_blocking(fd, False)
        try:
            return os.read(fd, 4096)
        except BlockingIOError:
            return None
    finally:
        try:
            os.set_blocking(fd, was_blocking)
        except OSError:
            pass


def _readline_with_timeout_posix(stream: IO[str], fd: int, timeout_sec: float, max_bytes: int) -> str:
    """Read one line, returning when the deadline passes even without a newline."""
    deadline = time.monotonic() + max(0.0, float(timeout_sec))
    pending = _pop_json_line_pending(stream)
    while True:
        if len(pending) > max_bytes:
            raise ValueError(f"JSON line exceeds {max_bytes} bytes")
        taken = _take_json_line(pending)
        if taken is not None:
            line, rest = taken
            _save_json_line_pending(stream, rest)
            return line
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            # Keep the partial line. The caller timed out, but a later read
            # of this stream should still see those bytes.
            _save_json_line_pending(stream, pending)
            raise subprocess.TimeoutExpired(cmd="IPC JSON line", timeout=timeout_sec)
        ready, _unused, _unused2 = select.select([fd], [], [], min(1.0, remaining))
        if not ready:
            continue
        piece = _read_available_line_bytes(fd)
        if piece is None:
            continue
        if piece == b"":
            try:
                _json_line_pending.pop(stream, None)
            except TypeError:
                pass
            return pending.decode("utf-8", errors="replace")
        pending.extend(piece)


def _readline_with_timeout(stream: IO[str], timeout_sec: float | None, max_bytes: int) -> str:
    if timeout_sec is None:
        return stream.readline(max_bytes + 1)

    # Windows select.select() only supports sockets, not pipes (WinError 10038).
    if sys.platform == "win32":
        return _readline_with_timeout_win32(stream, timeout_sec, max_bytes)

    try:
        fd = stream.fileno()
    except (AttributeError, OSError, ValueError):
        fd = None
    if isinstance(fd, int):
        return _readline_with_timeout_posix(stream, fd, timeout_sec, max_bytes)

    return stream.readline(max_bytes + 1)


def read_json_line(
    stream: IO[str],
    *,
    timeout_sec: float | None = None,
    max_bytes: int = DEFAULT_MAX_PAYLOAD_BYTES,
) -> dict[str, Any] | None:
    """Read one newline-delimited JSON object. Return None on clean EOF."""
    line = _readline_with_timeout(stream, timeout_sec, max_bytes)
    if not line:
        return None
    encoded = line.encode("utf-8", errors="replace")
    if len(encoded) > max_bytes:
        raise ValueError(f"JSON line exceeds {max_bytes} bytes")
    try:
        payload = json.loads(line.strip())
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON line: {line!r}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"JSON line must contain an object: {payload!r}")
    return payload
