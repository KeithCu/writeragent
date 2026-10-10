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
from contextlib import contextmanager
from typing import Any, BinaryIO, Callable, IO, Iterator, cast
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


class AllowlistUnpickler(pickle.Unpickler):
    """Host unpickler: REDUCE may only call names listed on the subclass.

    Two policies share this check. ``_SafeUnpickler`` (scripting / editor
    frames) allows ``complex`` and the NumPy reconstruct entry points, and
    may import a reconstruct module that is not loaded yet. Compute's
    ``RestrictedUnpickler`` allows builtin containers and scalars only, and
    never imports. A name off the list never becomes ``getattr`` on an
    arbitrary module, so a child cannot REDUCE into ``os.system``.
    """

    _builtins_allow: frozenset[str] = frozenset()
    _module_allow: frozenset[tuple[str, str]] = frozenset()
    _deny_tail: str = "is not allowed"
    _import_missing_modules: bool = False

    def find_class(self, module: str, name: str) -> Any:
        if module in ("builtins", "__builtin__") and name in self._builtins_allow:
            return getattr(builtins, name)
        if (module, name) in self._module_allow:
            mod = sys.modules.get(module)
            if mod is not None:
                return getattr(mod, name)
            if self._import_missing_modules:
                return super().find_class(module, name)
        raise pickle.UnpicklingError(f"global {module}.{name} {self._deny_tail}")


class _SafeUnpickler(AllowlistUnpickler):
    _builtins_allow: frozenset[str] = _SAFE_PICKLE_BUILTINS
    _module_allow: frozenset[tuple[str, str]] = _NUMPY_RECONSTRUCT_PAIRS
    _deny_tail: str = "is not allowed"
    _import_missing_modules: bool = True


class IpcFrameError(ValueError):
    """Raised when a framed IPC message has an invalid length or payload."""


class IpcPayloadSizeError(IpcFrameError):
    """Raised when an outgoing IPC frame payload exceeds the maximum allowed bytes."""


class IpcFrameReadError(IpcFrameError):
    """Raised when reading a framed IPC message fails due to invalid size or stream desync."""


class IpcPartialFrameTimeout(ConnectionError):
    """Deadline expired after at least one byte of a frame had arrived.

    The pipe is no longer on a frame boundary. Callers must kill the child.
    A clean timeout before any byte is ``subprocess.TimeoutExpired``, which
    a vision worker may still late-drain.
    """


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


def _write_all(stream: IO[Any], data: bytes | str) -> None:
    """Write every byte of *data*, then flush.

    ``write()`` once, ignoring the count, is not enough. The child IPC stream
    is unbuffered (``buffering=0``), and ``SIGALRM`` in this process can land
    mid-transfer. A truncated 4-byte length prefix permanently desyncs the
    pipe. Keep writing the remainder. A zero-length or ``None`` return is a
    stuck pipe, not a short count to retry forever.
    """
    if isinstance(data, str):
        written = 0
        total = len(data)
        while written < total:
            n = stream.write(data[written:])
            if not isinstance(n, int) or n <= 0:
                raise OSError("zero bytes written to pipe")
            written += n
    else:
        view = memoryview(data)
        written = 0
        while written < len(view):
            n = stream.write(view[written:])
            if not isinstance(n, int) or n <= 0:
                raise OSError("zero bytes written to pipe")
            written += n
    stream.flush()


def write_pickle_frame(
    stream: IO[bytes], message: Any, *, max_payload_bytes: int | None = DEFAULT_MAX_PAYLOAD_BYTES
) -> None:
    """Write one Pickle5 length-prefixed message to a binary pipe.

    The default used to be None, so an omitted write was uncapped while the
    matching read is capped and leaves extra bytes on the pipe. Pass None only
    to opt out.
    """
    _write_all(stream, pack_pickle_frame(message, max_payload_bytes=max_payload_bytes))


def _write_bytes_until_joined(stream: IO[bytes], payload: bytes, timeout_sec: float) -> None:
    """Bound a blocking write by joining a daemon thread.

    Windows pipes are not selectable. ``TimeoutExpired`` leaves this thread
    blocked in ``write`` until the caller kills the child and the pipe breaks.
    The helper does not kill.
    """
    errors: list[Exception] = []

    def _writer() -> None:
        try:
            _write_all(stream, payload)
        except Exception as exc:
            errors.append(exc)

    writer = threading.Thread(target=_writer, name="ipc-stdin-write", daemon=True)
    writer.start()
    writer.join(timeout=max(0.01, timeout_sec))
    if writer.is_alive():
        raise subprocess.TimeoutExpired(cmd="IPC frame", timeout=timeout_sec)
    if errors:
        raise errors[0]


def _write_fd_with_timeout(
    fd: int,
    payload: bytes,
    timeout_sec: float,
    *,
    is_alive: Callable[[], bool] | None = None,
) -> None:
    """Write *payload* to a POSIX pipe fd, or raise ``TimeoutExpired``.

    A short count already in the pipe desynchronizes the next frame. This
    raises instead of resuming the rest later. The caller kills the child.
    """
    deadline = time.monotonic() + max(0.0, timeout_sec)
    view = memoryview(payload)
    offset = 0
    with _nonblocking(fd):
        while offset < len(view):
            if is_alive is not None and not is_alive():
                raise BrokenPipeError("IPC frame write aborted: child exited")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(cmd="IPC frame", timeout=timeout_sec)
            try:
                _ready_read, writable, _ready_err = select.select([], [fd], [], min(1.0, remaining))
            except InterruptedError:
                continue
            if not writable:
                if is_alive is not None and not is_alive():
                    raise BrokenPipeError("IPC frame write aborted: child exited")
                continue
            try:
                written = os.write(fd, view[offset:])
            except BlockingIOError:
                continue
            except InterruptedError:
                continue
            if written <= 0:
                raise OSError("zero bytes written to pipe")
            offset += written


def write_pickle_frame_with_timeout(
    stream: IO[bytes],
    message: Any,
    timeout_sec: float,
    *,
    max_payload_bytes: int | None = DEFAULT_MAX_PAYLOAD_BYTES,
    is_alive: Callable[[], bool] | None = None,
) -> None:
    """Write one Pickle5 frame, bounding the pipe write with *timeout_sec*.

    The frame is packed first. ``IpcFrameError`` means no byte was written and
    the child is still aligned. POSIX uses non-blocking ``os.write`` plus
    ``select`` (``is_alive`` is consulted on that loop only). Windows pipes are
    not selectable, so a daemon thread is joined for the deadline.
    ``TimeoutExpired`` can mean a partial frame is already in the pipe; the
    caller kills that child. A stream with no real fileno (``BytesIO``) is
    written without a deadline.
    """
    frame = pack_pickle_frame(message, max_payload_bytes=max_payload_bytes)
    timeout_sec = max(0.0, float(timeout_sec))
    if sys.platform == "win32":
        _write_bytes_until_joined(stream, frame, timeout_sec)
        return
    try:
        fd = stream.fileno()
    except (AttributeError, OSError, ValueError, io.UnsupportedOperation):
        fd = None
    if not isinstance(fd, int):
        _write_all(stream, frame)
        return
    _write_fd_with_timeout(fd, frame, timeout_sec, is_alive=is_alive)


@contextmanager
def _nonblocking(fd: int) -> Iterator[None]:
    """Make *fd* non-blocking for the block, then restore the previous mode.

    Leaving ``set_blocking(False)`` in place makes the next read treat
    ``EAGAIN`` or ``None`` as EOF and drop the following frame. Every exit,
    including an exception, puts the mode back.
    """
    # Bugfix: What was wrong: ty on Windows failed because os.get_blocking and
    # os.set_blocking are POSIX-only standard library functions.
    # How it happened: _nonblocking was extracted as a standalone helper without
    # the platform check that guarded earlier inline calls.
    # Why this change: Skip non-blocking configuration on win32 or when set_blocking
    # is unavailable, allowing static type checkers to narrow out the POSIX-only
    # os calls on Windows and avoiding runtime AttributeError.
    if sys.platform == "win32" or not hasattr(os, "set_blocking"):
        yield
        return
    was_blocking = True
    try:
        if hasattr(os, "get_blocking"):
            was_blocking = os.get_blocking(fd)
    except OSError:
        was_blocking = True
    os.set_blocking(fd, False)
    try:
        yield
    finally:
        try:
            os.set_blocking(fd, was_blocking)
        except OSError:
            pass


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
        # Restore blocking when the peek ends. Leaving set_blocking(False) in
        # place made a later read (compute/kokoro loops catch the frame error
        # and read again) return None or raise BlockingIOError, which the
        # frame reader treats as EOF. The peek is only for the error text.
        try:
            with _nonblocking(fd):
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
    max_payload_bytes: int | None = DEFAULT_MAX_PAYLOAD_BYTES,
    frame_label: str = "IPC frame",
    read_exact: Callable[[int], bytes] | None = None,
) -> bytes | None:
    """Read one length-prefixed payload. Return None on clean EOF or truncation."""

    def reader(n: int) -> bytes:
        if read_exact is not None:
            return read_exact(n)
        # One short stream.read(n) is not EOF. A raw stream can return part of
        # the 4-byte header; dropping those bytes starts the next read
        # mid-frame. Loop until n bytes or a real empty read. Empty means EOF.
        # Callers that pass read_exact already loop (or time out) themselves.
        buf = bytearray()
        while len(buf) < n:
            chunk = stream.read(n - len(buf))
            if not chunk:
                return bytes(buf)
            buf.extend(chunk)
        return bytes(buf)

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
        # A length-valid truncated payload (protocol header, no body) raises
        # EOFError, not only UnpicklingError. That escaped
        # PythonWorkerManager.execute and left the child on the pipe. Map it
        # to the same ValueError the worker already turns into
        # WORKER_IPC_ERROR, which kills that child instead of replaying.
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

    After a tool_call id mismatch, one pickle frame has already been consumed
    and the raise discarded it. ``sys.stdin.buffer`` is a BufferedReader, so
    ``read(n)`` often pulls the next frame into that wrapper (the kernel pipe
    can look empty). The tail then stayed in place, and every later call under
    ``_tool_call_lock`` read it as its reply, so the worker stayed one frame
    behind for the rest of the process. The caller still holds the lock. This
    reads through the same stream, with the fd non-blocking and a deadline, so
    both the wrapper cache and the kernel queue are dropped. ``os.read`` is
    not used: it skips the cache and the next ``stream.read`` would still
    return the tail. Blocking mode is restored so the next call waits for a
    new frame.
    Windows pipes that reject non-blocking mode fall back to PeekNamedPipe,
    same as ``_unread_pipe_bytes`` (kernel bytes only).
    """
    deadline = time.monotonic() + max(0.0, float(timeout_sec))
    fd = _stream_fileno(stream)
    if fd is None:
        # stream.read() with no size waits until EOF. This branch is only
        # streams with no fileno, and a sized read can still block, so it is
        # not a best-effort drain. Without an fd there is nothing to poll.
        # Leave the bytes where they are instead of hanging.
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
    try:
        with _nonblocking(fd):
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
    except (OSError, AttributeError):
        # set_blocking failed before the fd changed. Caller uses PeekNamedPipe.
        return False


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
            # exchange_tool_call must write the claimed IPC stream.
            # sys.stdout.buffer lands on stderr when child stdout is dup2'd
            # to protect IPC framing from stray prints.
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
    # host_rpc sends code USER_STOPPED. Raising RuntimeError lets a script
    # ``except Exception`` keep running after Stop. UserStopped is
    # BaseException, so that handler does not run and the turn ends with the
    # same code.
    if response.get("code") == "USER_STOPPED":
        message = response.get("message") or response.get("error") or "Stopped by user."
        raise UserStopped(str(message))
    if response.get("status") == "error":
        # Copy response code onto the RuntimeError. Dropping it left callers
        # with only the message string.
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
                # Any byte already taken desynchronizes the pipe. A clean
                # timeout (empty buffer) stays TimeoutExpired so a vision
                # late-drain can still read the one frame the child will write.
                if len(buf) > 0:
                    raise IpcPartialFrameTimeout(f"{frame_label} stream desynchronized: timeout mid-frame")
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
    """Write one JSON object followed by a newline to a text or binary pipe.

    claim_ipc_channel() returns a binary stream. Encode to utf-8 when write()
    rejects a str.
    """
    line = json.dumps(payload) + "\n"
    try:
        _write_all(cast("Any", stream), line)
    except TypeError:
        _write_all(cast("Any", stream), line.encode("utf-8"))


def _stop_requested(stop_checker: Callable[[], bool] | None) -> bool:
    """True when the user pressed Stop.

    A checker that raises (KeyError, or anything outside the IPC except
    tuples) is not a stop. Calling it bare escaped ``run_code_in_user_venv``
    and left the child blocked on an unread request. Log it and keep reading
    so the pipe stays aligned. A tool_call frame is still answered in
    ``host_rpc``, which has a frame to reply to.
    """
    if stop_checker is None:
        return False
    try:
        return bool(stop_checker())
    except Exception:
        log.exception("stop_checker failed (continuing)")
        return False


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
        if _stop_requested(stop_checker):
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=timeout_sec)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            if len(buf) > 0:
                raise IpcPartialFrameTimeout(f"{cmd} stream desynchronized: timeout mid-frame")
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


def _line_from_pending(stream: IO[str], pending: bytearray, max_bytes: int) -> str | None:
    """Return one complete line from *pending*, or None when it has no newline.

    Pop the line only after the size check. Raising on an oversize line after
    the pop dropped those bytes, so the next read started mid-line. Put the
    partial back before raising so the next read of this stream still sees
    the same bytes.
    """
    if len(pending) > max_bytes:
        _save_json_line_pending(stream, pending)
        raise ValueError(f"JSON line exceeds {max_bytes} bytes")
    taken = _take_json_line(pending)
    if taken is None:
        return None
    line, rest = taken
    _save_json_line_pending(stream, rest)
    return line


def _finish_saved_json_line(stream: IO[str], pending: bytearray, max_bytes: int) -> str:
    """Blocking read of the tail after a timed read saved *pending*.

    The partial was already taken off the pipe. ``readline`` returns only the
    rest. An untimed ``read_json_line`` used to ignore this buffer and parse
    from the middle of the line.
    """
    tail = stream.readline(max_bytes + 1)
    if not isinstance(tail, str):
        tail = ""
    line = pending.decode("utf-8", errors="replace") + tail
    encoded = line.encode("utf-8", errors="replace")
    if len(encoded) > max_bytes:
        _save_json_line_pending(stream, bytearray(encoded))
        raise ValueError(f"JSON line exceeds {max_bytes} bytes")
    return line


def _readline_blocking_with_pending(stream: IO[str], max_bytes: int) -> str:
    pending = _pop_json_line_pending(stream)
    if not pending:
        return stream.readline(max_bytes + 1)
    return _finish_saved_json_line(stream, pending, max_bytes)


def _readline_with_timeout_win32(stream: IO[str], timeout_sec: float, max_bytes: int, *, cmd: str = "IPC JSON line") -> str:
    """Windows path: poll PeekNamedPipe and read only bytes already queued.

    ``avail > 0`` only means some bytes are queued, not a full line.
    ``stream.readline()`` then blocked until newline or EOF and ignored
    ``timeout_sec``. That is the partial-line hang the POSIX reader avoids.
    Callers include the audio-recorder monitor. ``os.read`` the peeked count
    into the shared pending buffer. A deadline with no newline saves that
    partial and raises ``TimeoutExpired``. ``readline`` is only the fallback
    when this stream has no real pipe fd (``BytesIO`` / mocks).
    """
    try:
        fd = stream.fileno()
    except (AttributeError, OSError, ValueError):
        fd = None
    # MagicMock.fileno() returns another mock that coerces to int; PeekNamedPipe then
    # hits the console FD and raises errno 1. Match the POSIX isinstance(fd, int) gate.
    if not isinstance(fd, int):
        return _readline_blocking_with_pending(stream, max_bytes)

    deadline = time.monotonic() + max(0.0, timeout_sec)
    pending = _pop_json_line_pending(stream)
    while True:
        line = _line_from_pending(stream, pending, max_bytes)
        if line is not None:
            return line
        if time.monotonic() >= deadline:
            _save_json_line_pending(stream, pending)
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=timeout_sec)
        avail = _peek_pipe_bytes_available(fd)
        if avail is None:
            # Pipe closed. Return bytes already pulled; do not block in readline.
            try:
                _json_line_pending.pop(stream, None)
            except TypeError:
                pass
            return pending.decode("utf-8", errors="replace")
        if avail > 0:
            try:
                piece = os.read(fd, min(int(avail), 65536))
            except BlockingIOError:
                # Peek and read can race. Would-block is not EOF.
                time.sleep(max(0.0, min(0.001, deadline - time.monotonic())))
                continue
            except OSError:
                _save_json_line_pending(stream, pending)
                raise
            if not piece:
                # Empty read after a successful peek is a closed pipe.
                try:
                    _json_line_pending.pop(stream, None)
                except TypeError:
                    pass
                return pending.decode("utf-8", errors="replace")
            pending.extend(piece)
            continue
        # PeekNamedPipe can cross the deadline; sleep(negative) is ValueError.
        time.sleep(max(0.0, min(0.001, deadline - time.monotonic())))


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
    with _nonblocking(fd):
        try:
            return os.read(fd, 4096)
        except BlockingIOError:
            return None


def _readline_with_timeout_posix(stream: IO[str], fd: int, timeout_sec: float, max_bytes: int) -> str:
    """Read one line, returning when the deadline passes even without a newline."""
    deadline = time.monotonic() + max(0.0, float(timeout_sec))
    pending = _pop_json_line_pending(stream)
    while True:
        line = _line_from_pending(stream, pending, max_bytes)
        if line is not None:
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
        # A timed read may have saved a partial line on this stream. Skipping
        # the pending map parses the next chunk as a new object.
        return _readline_blocking_with_pending(stream, max_bytes)

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
