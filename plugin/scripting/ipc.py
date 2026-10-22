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
import enum
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

# select(0) returns immediately. Callers refuse a spent deadline before
# they reach these waits, so this only lifts a remainder that is already
# positive off zero.
_SELECT_WAIT_FLOOR = 0.01


def _select_slice(remaining: float, cap: float) -> float:
    """Wait slice for a positive remainder, never zero and never above *cap*."""
    return min(cap, max(_SELECT_WAIT_FLOOR, remaining))

PICKLE_PROTOCOL = 5
FRAME_HEADER_SIZE = 4

# Child writes this before user code, trusted actions, or a ppt turn. The host
# must not replay the request after seeing it: in-process side effects may
# already have run without a tool_call frame. A death before this frame is
# still a failed start and may be retried.
EXEC_STARTED = "exec_started"

# frame_type values on venv-worker stdout when the request set allow_heartbeat.
# Absent frame_type is a bare dict: exec_started, tool_call, llm_request,
# worker_event, or a non-heartbeat terminal reply.
FRAME_HEARTBEAT = "heartbeat"
FRAME_RESULT = "result"

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
        log.debug("claim_ipc_channel: stdout flush failed", exc_info=True)
    try:
        sys.stderr.flush()
    except Exception:
        log.debug("claim_ipc_channel: stderr flush failed", exc_info=True)
    ipc_fd = os.dup(1)
    os.dup2(2, 1)
    _child_ipc_stream = cast("BinaryIO", os.fdopen(ipc_fd, "wb", buffering=0))
    return _child_ipc_stream


def get_child_ipc_stream() -> BinaryIO:
    """Return the claimed child IPC stream, or sys.stdout's buffer.

    What was wrong: a fileno check ran before the claim. After
    ``claim_ipc_channel``, fd 1 is dup2'd onto stderr, but ``sys.stdout``
    still reports fileno 1, so that check did not see the claim. It only
    fired when something else had replaced ``sys.stdout``, and then the
    claimed pipe was ignored.
    How it happened: the check was copied from ``claim_ipc_channel``, where
    fileno != 1 means "do not dup". Here it means the opposite.
    Why this change: return the claim first. Tool writes, the audio
    recorder, and the heartbeat all go through this function.
    """
    if _child_ipc_stream is not None:
        return _child_ipc_stream
    return cast("BinaryIO", getattr(sys.stdout, "buffer", sys.stdout))


_child_ipc_stdin: BinaryIO | None = None


def claim_ipc_stdin() -> BinaryIO:
    """Claim stdin for child IPC by detaching sys.stdin.buffer if it is a BufferedReader.

    What was wrong: Windows id-mismatch drain uses PeekNamedPipe, which does not
    see bytes BufferedReader.read already pulled into its 8KiB buffer, so the
    next exchange_tool_call stays one frame behind.
    How it happened: claim_ipc_channel only replaced stdout, and -u does not
    unbuffer stdin.
    Why this change: detach to the raw FileIO before the first read so the
    kernel queue is the whole tail.
    """
    global _child_ipc_stdin
    if _child_ipc_stdin is not None:
        return _child_ipc_stdin
    buf = getattr(sys.stdin, "buffer", None)
    if isinstance(buf, io.BufferedReader):
        raw = buf.detach()
        _child_ipc_stdin = cast("BinaryIO", cast("object", raw))
        return _child_ipc_stdin
    return cast("BinaryIO", buf if buf is not None else sys.stdin)


def get_child_ipc_stdin() -> BinaryIO:
    """Return the claimed child IPC stdin, or sys.stdin's buffer."""
    if _child_ipc_stdin is not None:
        return _child_ipc_stdin
    return cast("BinaryIO", getattr(sys.stdin, "buffer", sys.stdin))


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
# ("numpy", "ndarray") stays. A contiguous array uses _frombuffer and does
# not GLOBAL ndarray. A view ([:, ::2], [::-1]) passes numpy.ndarray to
# _reconstruct as the subtype. Dropping the pair breaks those frames.
# payload_codec also requires type(obj).__module__ == "numpy", so a
# stand-in subclass cannot replace the real class.
_NUMPY_RECONSTRUCT_PAIRS = frozenset({
    ("numpy._core.numeric", "_frombuffer"),
    ("numpy.core.numeric", "_frombuffer"),
    ("numpy._core.multiarray", "_reconstruct"),
    ("numpy.core.multiarray", "_reconstruct"),
    ("numpy._core.multiarray", "scalar"),
    ("numpy.core.multiarray", "scalar"),
    # A dtype, including a subarray dtype such as dtype(("u1", (10**9,))),
    # records the shape and does not allocate the array; _reconstruct and a
    # direct ndarray call are the allocations the REDUCE guards already reject.
    ("numpy", "dtype"),
    ("numpy", "ndarray"),
})


def _numpy_reconstruct_nbytes(shape: object, dtype: object) -> int:
    """Bytes a ``_reconstruct(subtype, shape, dtype)`` would allocate.

    NumPy's own pickles call this with shape ``(0,)`` and then BUILD the
    buffer that is already inside the frame. A hostile shape is a few
    pickle bytes and still allocates ``product(shape) * itemsize``.
    """
    if not isinstance(shape, tuple):
        raise pickle.UnpicklingError("numpy reconstruct shape is not a tuple")
    n = 1
    cap = DEFAULT_MAX_PAYLOAD_BYTES
    for dim in shape:
        if isinstance(dim, bool) or not isinstance(dim, int) or dim < 0:
            raise pickle.UnpicklingError("numpy reconstruct shape is not a tuple of sizes")
        if dim > cap or (dim != 0 and n > cap // dim):
            raise pickle.UnpicklingError(
                f"numpy reconstruct allocates more than {cap} bytes"
            )
        n *= dim
    if n == 0:
        # shape (0,) is what NumPy's own view pickles pass. The bytes arrive
        # later, inside the frame, via BUILD.
        return 0
    # What was wrong: a dtype string, bytes spec, or tuple spec has no
    # itemsize, and the bytes branch forced 1. _reconstruct then allocated
    # product(shape) * the real element size from a few pickle bytes.
    # How: getattr(dtype, "itemsize", 1) and isinstance(dtype, (bytes, bytearray)).
    # Why: numpy.dtype is PyArray_DescrConverter, which _reconstruct uses,
    # and a subarray dtype records its shape without allocating the array.
    try:
        import numpy as np

        descr = np.dtype(cast("Any", dtype))
        itemsize = int(descr.itemsize)
    except (TypeError, ValueError, OverflowError) as exc:
        raise pickle.UnpicklingError("numpy reconstruct dtype is not a dtype") from exc
    if itemsize < 1:
        # Unsized Unicode is rebuilt as one UCS-4 character. Other unsized
        # kinds are at most one byte (void allocates nothing).
        itemsize = 4 if descr.kind == "U" else 1
    if itemsize > cap or n > cap // itemsize:
        raise pickle.UnpicklingError(
            f"numpy reconstruct allocates more than {cap} bytes"
        )
    return n * itemsize


def _is_numpy_ndarray(obj: object) -> bool:
    """True for the real ``numpy.ndarray`` class, not a lookalike."""
    return getattr(obj, "__name__", "") == "ndarray" and getattr(obj, "__module__", "") == "numpy"


def _reject_ndarray_constructor(obj: object) -> None:
    """Refuse a call that would run ``numpy.ndarray`` as a constructor.

    Views pass this class to ``_reconstruct`` as the subtype. That is a
    GLOBAL, not a call. Calling it allocates the shape, and the shape is
    only a few pickle bytes.
    """
    if _is_numpy_ndarray(obj):
        raise pickle.UnpicklingError("numpy.ndarray constructor is not allowed")


def _reject_unbounded_numpy_reduce(func: object, args: object) -> None:
    """Refuse NumPy REDUCE targets that allocate past the frame cap.

    What was wrong: find_class returned the real ``numpy.ndarray`` class,
    which views need as a subtype. REDUCE then called ``ndarray(shape)``
    or ``_reconstruct(ndarray, huge_shape, dtype)``. The frame cap never
    saw that allocation. The shape is a few bytes.
    How it happened: the allowlist is a name list. GLOBAL and REDUCE share
    find_class, and the C unpickler has no other hook.
    Why this change: ``load_reduce`` runs before the call. NumPy's own
    pickles never REDUCE ``ndarray``. They do REDUCE ``_reconstruct`` with
    an empty shape. A direct ``ndarray`` call is rejected. A reconstruct
    whose nbytes exceed ``DEFAULT_MAX_PAYLOAD_BYTES`` is rejected.
    ``_frombuffer`` only reshapes the buffer already in the frame.
    """
    _reject_ndarray_constructor(func)
    name = getattr(func, "__name__", "")
    module = getattr(func, "__module__", "") or ""
    if name != "_reconstruct" or not module.endswith("multiarray"):
        return
    if not isinstance(args, tuple) or len(args) < 3:
        raise pickle.UnpicklingError("numpy _reconstruct arguments are not a shape and a dtype")
    _numpy_reconstruct_nbytes(args[1], args[2])


class AllowlistUnpickler(pickle._Unpickler):
    """Host unpickler: REDUCE may only call names listed on the subclass.

    Two policies share this check. ``_SafeUnpickler`` (scripting / editor
    frames) allows ``complex`` and the NumPy reconstruct entry points, and
    may import a reconstruct module that is not loaded yet. Compute's
    ``RestrictedUnpickler`` allows builtin containers and scalars only, and
    never imports. A name off the list never becomes ``getattr`` on an
    arbitrary module, so a child cannot REDUCE into ``os.system``.

    The base is ``pickle._Unpickler``, not the C ``pickle.Unpickler``. The
    C class only runs ``find_class``. ``_guarded_load_reduce`` has to see
    ``ndarray`` and ``_reconstruct`` before they allocate. ``dispatch`` is
    copied below so that function is the REDUCE opcode. NEWOBJ and
    NEWOBJ_EX are replaced the same way. OBJ and INST call
    ``_instantiate``, which this class overrides.
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

    def load(self) -> Any:
        try:
            return super().load()
        except KeyError as exc:
            # pickle._Unpickler looks the opcode up in dispatch. An unknown
            # byte is KeyError. The C unpickler raises UnpicklingError
            # ("invalid load key, 'n'."). unpack_pickle_frame only maps
            # UnpicklingError, so b"notpickl" escaped read_message as KeyError.
            # A string key from inside an opcode handler is not this case.
            key = exc.args[0] if exc.args else None
            if isinstance(key, int) and 0 <= key <= 255:
                shown = chr(key) if 32 <= key < 127 else "?"
                raise pickle.UnpicklingError(f"invalid load key, '{shown}'.") from exc
            raise
        except EOFError as exc:
            # pickle._Unpickler raises EOFError() with no text. The C
            # unpickler says "Ran out of input". unpack_pickle_frame
            # stringifies this. A truncated frame used to carry that text.
            if exc.args:
                raise
            raise EOFError("Ran out of input") from exc

    def _instantiate(self, klass: Any, args: Any) -> None:
        """OBJ and INST. ``ndarray(shape)`` never reaches the REDUCE guard.

        What was wrong: protocol 0 builds the array with OBJ or INST after
        GLOBAL of the allowlisted class. Those opcodes call
        ``_instantiate``, not REDUCE, so ``ndarray((n,))`` allocated.
        How it happened: views need that class on the allowlist so
        ``_reconstruct`` can take it as the subtype. GLOBAL succeeds, and
        OBJ/INST then call the class.
        Why this change: reject ``numpy.ndarray`` before the original
        ``_instantiate``. NumPy's own pickles use REDUCE and BUILD, not
        OBJ or INST. Any other class still uses the original.
        """
        _reject_ndarray_constructor(klass)
        instantiate = getattr(pickle._Unpickler, "_instantiate")
        instantiate(self, klass, args)


def _guarded_load_reduce(unpickler: pickle.Unpickler, /) -> None:
    """REDUCE opcode. Refuse NumPy calls that allocate past the frame cap.

    A method on AllowlistUnpickler does not match the opcode table, which
    stores ``(Unpickler) -> None``. The pure-Python unpickler's ``stack``
    is not on the C type the checkers know about.
    """
    stack = cast("list[Any]", getattr(unpickler, "stack"))
    args = stack.pop()
    func = stack[-1]
    _reject_unbounded_numpy_reduce(func, args)
    if not isinstance(args, tuple):
        raise pickle.UnpicklingError("REDUCE arguments are not a tuple")
    if not callable(func):
        raise pickle.UnpicklingError("REDUCE target is not callable")
    stack[-1] = func(*args)


def _guarded_load_newobj(unpickler: pickle.Unpickler, /) -> None:
    """NEWOBJ opcode. ``ndarray.__new__`` is the same allocation as REDUCE.

    What was wrong: the guard only replaced REDUCE. GLOBAL of the
    allowlisted ``numpy.ndarray`` plus NEWOBJ calls ``__new__`` and skips
    it. A few bytes then allocate the shape.
    How it happened: views need the class on the stack so ``_reconstruct``
    can take it as the subtype. NumPy's own pickles never NEWOBJ it.
    Why this change: reject that class before ``load_newobj``. Other
    classes still use the original opcode.
    """
    stack = cast("list[Any]", getattr(unpickler, "stack"))
    # load_newobj pops the args tuple, then the class under it.
    if len(stack) >= 2:
        _reject_ndarray_constructor(stack[-2])
    load_newobj = getattr(pickle._Unpickler, "load_newobj")
    load_newobj(unpickler)


def _guarded_load_newobj_ex(unpickler: pickle.Unpickler, /) -> None:
    """NEWOBJ_EX opcode. Same ``ndarray.__new__`` hole, with kwargs.

    What was wrong: NEWOBJ_EX also calls ``cls.__new__(cls, *args, **kwargs)``
    and never entered ``_guarded_load_reduce``.
    How it happened: the class is allowlisted for views, and this opcode
    is a separate slot in ``dispatch``.
    Why this change: reject ``numpy.ndarray`` before ``load_newobj_ex``.
    The class sits under the args tuple and the kwargs dict.
    """
    stack = cast("list[Any]", getattr(unpickler, "stack"))
    # load_newobj_ex pops kwargs, then args, then the class.
    if len(stack) >= 3:
        _reject_ndarray_constructor(stack[-3])
    load_newobj_ex = getattr(pickle._Unpickler, "load_newobj_ex")
    load_newobj_ex(unpickler)


# The opcode table is filled on pickle._Unpickler. A subclass method does
# not replace it. Copy, then point REDUCE, NEWOBJ, and NEWOBJ_EX at the
# guards. OBJ and INST call _instantiate, which the class overrides.
# RestrictedUnpickler does not define dispatch, so it uses this table too.
AllowlistUnpickler.dispatch = pickle._Unpickler.dispatch.copy()
AllowlistUnpickler.dispatch[pickle.REDUCE[0]] = _guarded_load_reduce
AllowlistUnpickler.dispatch[pickle.NEWOBJ[0]] = _guarded_load_newobj
AllowlistUnpickler.dispatch[pickle.NEWOBJ_EX[0]] = _guarded_load_newobj_ex


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


class IpcTruncatedFrame(IpcFrameReadError):
    """EOF after at least one byte of this frame and before the declared end.

    Zero bytes at a frame boundary is clean EOF (None). A deadline with the
    pipe still open is IpcPartialFrameTimeout, raised by the timed reader.
    """


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
# MemoryError stays in this tuple. A REDUCE that still allocates (the host is
# already short on memory) is not a ValueError. Mapping it is what lets the
# worker kill the child instead of dying inside the LibreOffice process.
# An opcode handler (func(*args) in _guarded_load_reduce, or BUILD / __setstate__
# on an allowlisted numpy object) can raise KeyError; AllowlistUnpickler.load
# rewrites only an int key in 0..255; any other KeyError used to leave
# unpack_pickle_frame and skip the harness except ValueError that writes a
# bad-request frame.
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
    KeyError,
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

    The cap lives on ``pack_pickle_frame``. Pass None only to opt out.
    """
    _write_all(stream, pack_pickle_frame(message, max_payload_bytes=max_payload_bytes))


def _write_bytes_until_joined(
    stream: IO[bytes],
    payload: bytes,
    timeout_sec: float,
    *,
    is_alive: Callable[[], bool] | None = None,
) -> None:
    """Bound a blocking write by joining a daemon thread.

    Windows pipes are not selectable. ``TimeoutExpired`` leaves this thread
    blocked in ``write`` until the caller kills the child and the pipe breaks.
    The helper does not kill. A dead child raises ``BrokenPipeError`` instead
    of waiting out *timeout_sec*; the caller's kill unblocks the write.
    """
    errors: list[Exception] = []

    def _writer() -> None:
        try:
            _write_all(stream, payload)
        except Exception as exc:
            errors.append(exc)

    writer = threading.Thread(target=_writer, name="ipc-stdin-write", daemon=True)
    writer.start()
    deadline = time.monotonic() + max(0.0, timeout_sec)
    while writer.is_alive():
        if is_alive is not None and not is_alive():
            raise BrokenPipeError("IPC frame write aborted: child exited")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(cmd="IPC frame", timeout=timeout_sec)
        writer.join(timeout=_select_slice(remaining, 0.05))
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

    A short ``os.write`` resumes in this loop (``offset += written``).
    The function raises on timeout, a non-positive count, or a dead child.
    It does not return a partial write for the caller to finish later.
    ``TimeoutExpired`` can mean a partial frame is already in the pipe;
    the caller kills that child.
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
                _ready_read, writable, _ready_err = select.select([], [fd], [], _select_slice(remaining, 1.0))
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
    the child is still aligned. The packed bytes go through
    ``write_packed_frame_with_timeout``.
    """
    frame = pack_pickle_frame(message, max_payload_bytes=max_payload_bytes)
    write_packed_frame_with_timeout(stream, frame, timeout_sec, is_alive=is_alive)


def write_packed_frame_with_timeout(
    stream: IO[bytes],
    frame: bytes,
    timeout_sec: float,
    *,
    is_alive: Callable[[], bool] | None = None,
) -> None:
    """Write an already-packed frame, bounding the pipe write with *timeout_sec*.

    POSIX uses non-blocking ``os.write`` plus ``select``. Windows pipes are
    not selectable, so a daemon thread is joined for the deadline. Both paths
    consult ``is_alive`` and raise ``BrokenPipeError`` when the child is
    already dead. ``TimeoutExpired`` can mean a partial frame is already in
    the pipe; the caller kills that child.
    A stream with no real fileno (``BytesIO``) is written without a deadline.
    """
    timeout_sec = max(0.0, float(timeout_sec))
    fd = _stream_fileno(stream)
    if fd is None:
        # Popen pipes always have an int fileno. This is BytesIO and mocks.
        # _write_all has no deadline, so a real pipe that landed here would
        # wedge the caller that holds the worker lock.
        log.debug("write_packed_frame_with_timeout: no int fileno; writing without a deadline")
        _write_all(stream, frame)
        return
    if sys.platform == "win32":
        _write_bytes_until_joined(stream, frame, timeout_sec, is_alive=is_alive)
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


def _stream_fileno(stream: object) -> int | None:
    """Real pipe fd, or None for BytesIO, a closed fd, and MagicMock.

    Text and binary streams both have fileno. ``IO[bytes]`` and ``IO[str]``
    are invariant, so the parameter is the object that was passed in.
    """
    try:
        fd = cast("Any", stream).fileno()
    except (AttributeError, OSError, ValueError, io.UnsupportedOperation):
        return None
    if isinstance(fd, int):
        return fd
    return None


def _unread_pipe_bytes(stream: IO[bytes], n: int = 512) -> bytes:
    """Best-effort leftover bytes after a bad length prefix (non-blocking on real pipes)."""
    fd = _stream_fileno(stream)
    if fd is not None:
        # os.set_blocking is POSIX-only; skip the non-blocking peek on win32.
        # BytesIO/mocks still fall through to stream.read (no real fileno).
        if sys.platform == "win32" or not hasattr(os, "set_blocking"):
            # Do not stream.read() here — that can block on a live pipe.
            return b""
        # Restore blocking when the peek ends. The blocking mode is restored
        # because a later read on a fd left non-blocking returns None or
        # BlockingIOError, and the frame reader treats that as EOF. The peek
        # only fills the error text. A bad length prefix has already desynced
        # the stream; Kokoro and the compute worker exit instead of reading
        # another frame.
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


# PeekNamedPipe is rebuilt on every poll (up to once a millisecond) if this
# stays inside the function. One WinDLL is enough for the process.
_peek_named_pipe: Any = None
# PeekNamedPipe failures for a gone peer; ERROR_NO_DATA is 232 and is not
# one of them; an empty pipe is a successful peek of 0.
_WIN_ERROR_BROKEN_PIPE = 109
_WIN_ERROR_PIPE_NOT_CONNECTED = 233


def _peek_pipe_bytes_available(fd: int) -> int | None:
    """Return queued byte count for a Windows pipe fd, or None when the pipe is closed."""
    if sys.platform != "win32":
        return None
    import ctypes
    import msvcrt
    from ctypes import wintypes

    global _peek_named_pipe
    peek_named_pipe = _peek_named_pipe
    if peek_named_pipe is None:
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
        _peek_named_pipe = peek_named_pipe

    avail = wintypes.DWORD(0)
    handle = msvcrt.get_osfhandle(fd)
    if peek_named_pipe(handle, None, 0, None, ctypes.byref(avail), None):
        return int(avail.value)
    if ctypes.get_last_error() in (_WIN_ERROR_BROKEN_PIPE, _WIN_ERROR_PIPE_NOT_CONNECTED):
        return None
    raise OSError(ctypes.get_last_error(), ctypes.FormatError(ctypes.get_last_error()))


class _PipeWait(enum.Enum):
    READY = "ready"    # select fired, or Windows avail > 0
    EMPTY = "empty"    # not ready, still waiting
    DEAD = "dead"      # not ready and is_alive() is false; caller stops
    CLOSED = "closed"  # Windows peek returned None (broken / not connected)


def _poll_pipe(
    select_target: Any,
    fd: int | None,
    deadline: float,
    *,
    timeout_label: float,
    cmd: str,
    stop_checker: Callable[[], bool] | None = None,
    is_alive: Callable[[], bool] | None = None,
    select_cap: float = 1.0,
    partial: bool = False,
    use_peek: bool = False,
) -> tuple[_PipeWait, int]:
    """Wait one slice for bytes on a pipe and say what happened.

    Raises ``TimeoutExpired`` on Stop or deadline. With *partial* (bytes of
    this frame already taken) a deadline is ``IpcPartialFrameTimeout``
    instead, since the pipe is now out of sync. A clean timeout stays
    ``TimeoutExpired`` so a vision late-drain can still read the frame.
    *use_peek* is the Windows path: pipes are not selectable there.
    The second value is the peeked byte count (0 for select).
    """
    if _stop_requested(stop_checker):
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=timeout_label)
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        if partial:
            raise IpcPartialFrameTimeout(f"{cmd} stream desynchronized: timeout mid-frame")
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=timeout_label)
    if use_peek:
        avail = _peek_pipe_bytes_available(fd) if fd is not None else None
        if avail is None:
            return _PipeWait.CLOSED, 0
        if avail > 0:
            return _PipeWait.READY, avail
        # Peek before is_alive: a child that wrote a frame and exited still
        # has those bytes queued. Only an empty pipe plus a dead child is EOF.
        if is_alive is not None and not is_alive():
            return _PipeWait.DEAD, 0
        # Re-read the clock: PeekNamedPipe can cross the deadline, and
        # sleep(negative) is ValueError.
        time.sleep(max(0.0, min(0.001, deadline - time.monotonic())))
        return _PipeWait.EMPTY, 0
    ready, _unused, _unused2 = select.select([select_target], [], [], _select_slice(remaining, select_cap))
    if ready:
        return _PipeWait.READY, 0
    if is_alive is not None and not is_alive():
        return _PipeWait.DEAD, 0
    return _PipeWait.EMPTY, 0


def _read_bytes_with_timeout_win32(
    stream: IO[bytes],
    n: int,
    deadline: float,
    timeout_sec: float,
    *,
    cmd: str,
    stop_checker: Callable[[], bool] | None = None,
    is_alive: Callable[[], bool] | None = None,
) -> bytes:
    """Read *n* bytes from a Windows pipe without a stuck ReadFile thread.

    Polls ``PeekNamedPipe`` until bytes are queued, then reads only what is
    available. Raises ``TimeoutExpired`` on deadline.
    A dead child is EOF only when the peek is empty; queued bytes are read
    first.
    """
    fd = _stream_fileno(stream)
    buf = bytearray()
    while len(buf) < n:
        state, avail = _poll_pipe(
            stream,
            fd,
            deadline,
            timeout_label=timeout_sec,
            cmd=cmd,
            stop_checker=stop_checker,
            is_alive=is_alive,
            partial=len(buf) > 0,
            use_peek=True,
        )
        if state is _PipeWait.DEAD:
            break
        if state in (_PipeWait.CLOSED, _PipeWait.READY):
            chunk = stream.read(min(n - len(buf), avail) if state is _PipeWait.READY else n - len(buf))
            if not chunk:
                return bytes(buf)
            buf.extend(chunk)
    return bytes(buf)


def read_frame_payload(
    stream: IO[bytes],
    timeout_sec: float | None = None,
    *,
    max_payload_bytes: int | None = DEFAULT_MAX_PAYLOAD_BYTES,
    frame_label: str = "IPC frame",
    is_alive: Callable[[], bool] | None = None,
    stop_checker: Callable[[], bool] | None = None,
    deadline: float | None = None,
    timeout_label: float | None = None,
    cmd: str | None = None,
) -> bytes | None:
    """Read one length-prefixed payload. Return None on clean EOF.

    With no *timeout_sec* this blocks (child side). With one, header and
    payload share that deadline: POSIX uses ``select``, Windows polls
    ``PeekNamedPipe``, and streams without a real fd (BytesIO, mocks) wait
    on a daemon thread. A ``stop_checker`` raises ``TimeoutExpired`` (the
    caller maps that to cancel); ``deadline`` is an absolute
    ``time.monotonic()`` stamp; ``timeout_label`` is only the number stored
    on ``TimeoutExpired``. A truncated frame raises ``IpcTruncatedFrame``.

    The timed path needs an unbuffered stream (``bufsize=0``): after
    ``select``, raw ``FileIO.read`` returns what is there, but a
    ``BufferedReader`` waits for all *n* bytes.
    """
    if timeout_sec is None:
        # One short read is not EOF; a raw pipe can return part of the
        # 4-byte header. Loop until n bytes or an empty read.
        def _read_exact(n: int) -> bytes:
            buf = bytearray()
            while len(buf) < n:
                chunk = stream.read(n - len(buf))
                if not chunk:
                    return bytes(buf)
                buf.extend(chunk)
            return bytes(buf)
    else:
        timeout_sec = max(0.0, float(timeout_sec))
        abs_deadline = time.monotonic() + timeout_sec if deadline is None else float(deadline)
        label = float(timeout_sec if timeout_label is None else timeout_label)
        cmd_name = frame_label if cmd is None else cmd
        fd = _stream_fileno(stream)

        if fd is None:
            def _read_exact(n: int) -> bytes:
                if _stop_requested(stop_checker):
                    raise subprocess.TimeoutExpired(cmd=cmd_name, timeout=label)
                if is_alive is not None and not is_alive():
                    return b""
                remaining = abs_deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(cmd=cmd_name, timeout=label)
                result: list[bytes] = [b""]
                error: list[BaseException | None] = [None]

                def _reader() -> None:
                    try:
                        result[0] = stream.read(n)
                    except Exception as exc:
                        error[0] = exc

                t = threading.Thread(target=_reader, daemon=True)
                t.start()
                t.join(timeout=max(0.0, remaining))
                if t.is_alive():
                    raise subprocess.TimeoutExpired(cmd=cmd_name, timeout=label)
                if error[0] is not None:
                    raise error[0]
                return result[0] or b""

        elif sys.platform == "win32":
            # PeekNamedPipe, not a daemon thread blocked in ReadFile. Closing the
            # pipe while that thread was still in ReadFile crashed the xdist
            # worker (CI 33453184665).
            def _read_exact(n: int) -> bytes:
                return _read_bytes_with_timeout_win32(
                    stream,
                    n,
                    abs_deadline,
                    label,
                    cmd=cmd_name,
                    stop_checker=stop_checker,
                    is_alive=is_alive,
                )

        else:
            def _read_exact(n: int) -> bytes:
                buf = bytearray()
                while len(buf) < n:
                    state, _avail = _poll_pipe(
                        stream,
                        fd,
                        abs_deadline,
                        timeout_label=label,
                        cmd=cmd_name,
                        stop_checker=stop_checker,
                        is_alive=is_alive,
                        select_cap=0.2 if stop_checker is not None else 1.0,
                        partial=len(buf) > 0,
                        use_peek=False,
                    )
                    if state is _PipeWait.DEAD:
                        break
                    if state is _PipeWait.READY:
                        chunk = stream.read(n - len(buf))
                        if not chunk:
                            return bytes(buf)
                        buf.extend(chunk)
                return bytes(buf)

    header = _read_exact(FRAME_HEADER_SIZE)
    if not header:
        return None
    if len(header) < FRAME_HEADER_SIZE:
        # Some header bytes, then EOF: the child died mid-frame. That is not a
        # clean EOF, so raise and let the caller tear the stream down.
        raise IpcTruncatedFrame(
            f"{frame_label}: truncated header (expected {FRAME_HEADER_SIZE} bytes, got {len(header)})"
        )
    size = struct.unpack("!I", header)[0]
    try:
        _validate_frame_size(size, max_payload_bytes=max_payload_bytes, frame_label=frame_label)
    except IpcFrameError as exc:
        rest = _unread_pipe_bytes(stream)
        msg = f"{exc} stdout_rest={rest!r}"
        log.error("%s", msg)
        raise IpcFrameReadError(msg) from None
    payload = _read_exact(size)
    if len(payload) < size:
        # The payload ended before its declared size: the pipe is out of sync.
        raise IpcTruncatedFrame(
            f"{frame_label}: truncated payload (expected {size} bytes, got {len(payload)})"
        )
    return payload


def unpack_pickle_frame(payload: bytes) -> Any:
    """Decode one Pickle5 payload from a child or the editor.

    Allows builtin containers and scalars, ``complex``, and the NumPy
    reconstruct entry points a real array pickle calls. A direct
    ``numpy.ndarray`` constructor is rejected. A name off that list never
    becomes ``getattr`` on an arbitrary module.
    """
    try:
        return _SafeUnpickler(io.BytesIO(payload)).load()
    except _PICKLE_LOAD_ERRORS as exc:
        # A length-valid truncated payload (protocol header, no body) raises
        # EOFError or KeyError, not only UnpicklingError. That escaped
        # PythonWorkerManager.execute and left the child on the pipe. Map it
        # to the same ValueError the worker already turns into
        # WORKER_IPC_ERROR, which kills that child instead of replaying.
        raise ValueError(str(exc)) from exc


def parse_worker_stdout_frame(payload: bytes) -> dict[str, Any]:
    """Parse one Pickle5 frame from child stdout into a dictionary.

    Empty and non-dict payloads raise: empty bytes raise ValueError from
    unpack_pickle_frame, and a decoded object that is not a dict raises
    RuntimeError. A missing frame_type is still a valid dict (bare dict for
    exec_started, tool_call, llm_request, worker_event, or non-heartbeat
    terminal responses).
    """
    data = unpack_pickle_frame(payload)
    if not isinstance(data, dict):
        raise RuntimeError("Worker response must be a dict")
    return data


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
    timeout_sec: float | None = None,
    *,
    max_payload_bytes: int | None = DEFAULT_MAX_PAYLOAD_BYTES,
    frame_label: str = "IPC frame",
    require_dict: bool = False,
    is_alive: Callable[[], bool] | None = None,
    unpacker: Callable[[bytes], Any] | None = None,
) -> Any | None:
    """Read and unpickle one length-prefixed message. Return None on clean EOF."""
    payload = read_frame_payload(
        stream,
        timeout_sec,
        max_payload_bytes=max_payload_bytes,
        frame_label=frame_label,
        is_alive=is_alive,
    )
    return _decode_pickle_payload(payload, frame_label=frame_label, require_dict=require_dict, unpacker=unpacker)


# One lock for every venv → host tool_call on this pipe, as well as heartbeat
# and result frames written by write_child_ipc_frame. The LibrePy named-script
# fallback used to write a frame with no lock, so two calls could interleave.
_tool_call_lock = threading.Lock()


def write_child_ipc_frame(
    stream: IO[bytes],
    message: Any,
    *,
    max_payload_bytes: int | None = DEFAULT_MAX_PAYLOAD_BYTES,
) -> None:
    """Write one Pickle5 length-prefixed frame to child stdout holding _tool_call_lock."""
    with _tool_call_lock:
        write_pickle_frame(stream, message, max_payload_bytes=max_payload_bytes)


def _pause_script_alarm() -> tuple[float, float, float] | None:
    """Turn off SIGALRM for the tool_call read. Return (delay, interval, start).

    The script alarm used to fire inside this stdin read. The harness then wrote
    an error frame while the host was still writing the tool reply, and the next
    cell read that reply as a request.

    What was wrong: ``signal.alarm(0)`` returns whole seconds, and resume
    rounded up with ``int(left + 0.999)`` and a floor of 1. A tool call gave
    the cell up to about a second it had already spent.
    How it happened: the executor arms the timer with ``signal.alarm``, which
    only has second resolution on the way in. The pause threw away the
    fraction that was still left.
    Why this change: ``setitimer(ITIMER_REAL, 0)`` is the same timer and
    returns the fraction. Resume puts that fraction back. Platforms without
    ``setitimer`` keep ``alarm``. The timer is process-wide, so this still
    disarms a timer armed on the main thread.
    """
    if sys.platform == "win32":
        return None
    try:
        import signal

        if hasattr(signal, "setitimer"):
            delay, interval = signal.setitimer(signal.ITIMER_REAL, 0.0)
            delay_sec = float(delay)
            interval_sec = float(interval)
        else:
            delay_sec = float(signal.alarm(0))
            interval_sec = 0.0
    except (AttributeError, ValueError, OSError):
        return None
    if delay_sec <= 0 and interval_sec <= 0:
        return None
    return delay_sec, interval_sec, time.monotonic()


# Upper bound for discarding a desynced tool_call tail. Queued bytes return
# immediately; the deadline only stops a peer that keeps the pipe full.
_TOOL_CALL_MISMATCH_DRAIN_SEC = 0.05


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
    The harness stdin is the raw stream from ``claim_ipc_stdin``, so the
    peek is the full queue; do not ``stream.read`` on a zero peek; do not
    turn on ``os.set_blocking`` for win32.
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
            # The deadline expired while the peer was still writing. True
            # means this fd was handled. Leftover bytes stay queued. Do not
            # fall through to PeekNamedPipe. This function already returns
            # False on Windows before the loop, and on POSIX that peek
            # helper returns immediately.
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


def _resume_script_alarm(paused: tuple[float, float, float] | None) -> bool:
    """Restore the script alarm. Return True when the budget was already spent.

    The tool reply has been consumed by then, so the timeout is a normal script
    error instead of a desynced pipe. The fraction from ``setitimer`` is put
    back as-is. A one-second floor would hand the cell time it had spent
    inside the tool call.
    """
    if paused is None:
        return False
    delay, interval, started = paused
    left = delay - (time.monotonic() - started)
    if sys.platform == "win32":
        return left <= 0
    try:
        import signal

        if left <= 0:
            # This shot is spent. A repeating interval, if one was set, is
            # not the script budget. Put it back so pause did not clear it.
            if interval > 0 and hasattr(signal, "setitimer"):
                signal.setitimer(signal.ITIMER_REAL, interval, interval)
            return True
        if hasattr(signal, "setitimer"):
            signal.setitimer(signal.ITIMER_REAL, left, interval)
        else:
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
    response = None
    mismatched = False
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
            stdin = get_child_ipc_stdin()
            response = read_pickle_frame(
                stdin,
                require_dict=True,
                max_payload_bytes=DEFAULT_MAX_PAYLOAD_BYTES,
                frame_label="tool_call response",
            )
            # Drain while _tool_call_lock is still held. Releasing it first
            # lets the next exchange read the tail this call already pulled.
            # A drain failure must not replace the id mismatch error.
            # The same flag is what the raise below checks. A spent alarm
            # budget is still raised first: the reply was consumed, and the
            # cell's timeout is the result the script already had coming.
            mismatched = isinstance(response, dict) and response.get("id") != call_id
            if mismatched:
                try:
                    _drain_queued_pipe_bytes(stdin)
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
    if mismatched:
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


def _read_json_tail_from_fd(stream: IO[str], fd: int, pending: bytearray, max_bytes: int) -> str:
    """Append fd bytes onto *pending* until a newline, then decode once.

    What was wrong: the handoff decoded *pending* with ``errors="replace"``
    and then appended ``readline()`` text. A timed ``os.read`` can stop on
    the first byte of a UTF-8 character. That byte became U+FFFD, and the
    text wrapper decoded the continuation as a second character.
    How it happened: the timed reader keeps a bytearray. This path turned
    it into text before the tail was attached.
    Why this change: read the tail with ``os.read`` into the same
    bytearray and decode the line once. The timed loop already does that
    for a line that finishes before the deadline.
    """
    while True:
        if len(pending) > max_bytes:
            # The line is already over the cap. Saving it makes the next
            # read raise again instead of starting mid-line. The caller
            # drops the child, the same as an oversize pickle frame.
            _save_json_line_pending(stream, pending)
            raise ValueError(f"JSON line exceeds {max_bytes} bytes")
        nl = pending.find(b"\n")
        if nl >= 0:
            line_b = bytes(pending[: nl + 1])
            rest = bytearray(pending[nl + 1 :])
            _save_json_line_pending(stream, rest)
            return line_b.decode("utf-8", errors="replace")
        try:
            chunk = os.read(fd, 4096)
        except OSError:
            _save_json_line_pending(stream, pending)
            raise
        if not chunk:
            return pending.decode("utf-8", errors="replace")
        pending.extend(chunk)


def _finish_saved_json_line(stream: IO[str], pending: bytearray, max_bytes: int) -> str:
    """Blocking read of the tail after a timed read saved *pending*.

    The partial was already taken off the pipe. An untimed
    ``read_json_line`` used to ignore this buffer and parse from the
    middle of the line.

    An fd is read as bytes. ``readline`` is only the no-fd path
    (``StringIO``). Its size is a character count and ``max_bytes`` is a
    byte count. The encode check is what enforces the cap there. A line
    longer than the character limit leaves the rest in the stream, and
    this saves the oversize prefix. The next read raises again. The pipe
    is mid-line. The caller drops the child.
    """
    fd = _stream_fileno(stream)
    if fd is not None:
        return _read_json_tail_from_fd(stream, fd, pending, max_bytes)
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

    ``avail > 0`` only means some bytes are queued, not a full line, so
    ``readline()`` here would block past ``timeout_sec``. ``os.read`` the
    peeked count into the shared pending buffer instead. A deadline with
    no newline saves that partial and raises ``TimeoutExpired``.
    ``readline`` is only used when the stream has no real pipe fd.
    """
    # MagicMock.fileno() returns a mock that coerces to int; _stream_fileno
    # only accepts a real int.
    fd = _stream_fileno(stream)
    if fd is None:
        return _readline_blocking_with_pending(stream, max_bytes)

    deadline = time.monotonic() + max(0.0, float(timeout_sec))
    pending = _pop_json_line_pending(stream)
    while True:
        line = _line_from_pending(stream, pending, max_bytes)
        if line is not None:
            return line
        try:
            state, avail = _poll_pipe(
                stream,
                fd,
                deadline,
                timeout_label=timeout_sec,
                cmd=cmd,
                select_cap=1.0,
                partial=False,
                use_peek=True,
            )
        except subprocess.TimeoutExpired:
            # Keep the partial line for the next read of this stream.
            _save_json_line_pending(stream, pending)
            raise
        if state is _PipeWait.CLOSED:
            # Pipe closed: return what was already pulled, do not block.
            return pending.decode("utf-8", errors="replace")
        if state is _PipeWait.READY:
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
                return pending.decode("utf-8", errors="replace")
            pending.extend(piece)


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
        try:
            state, _avail = _poll_pipe(
                fd,
                fd,
                deadline,
                timeout_label=timeout_sec,
                cmd="IPC JSON line",
                select_cap=1.0,
                partial=False,
                use_peek=False,
            )
        except subprocess.TimeoutExpired:
            _save_json_line_pending(stream, pending)
            raise
        if state is _PipeWait.READY:
            piece = _read_available_line_bytes(fd)
            if piece is None:
                continue
            if piece == b"":
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

    fd = _stream_fileno(stream)
    if fd is not None:
        return _readline_with_timeout_posix(stream, fd, timeout_sec, max_bytes)

    # No real fd, so there is no select deadline. A timed read may already
    # have saved a partial line on this stream. readline() alone starts
    # from the middle of that line.
    return _readline_blocking_with_pending(stream, max_bytes)


def read_json_line(
    stream: IO[str],
    *,
    timeout_sec: float | None = None,
    max_bytes: int = DEFAULT_MAX_PAYLOAD_BYTES,
) -> dict[str, Any] | None:
    """Read one newline-delimited JSON object. Return None on clean EOF.

    Use one mode on a stream. A timed read takes bytes with ``os.read``
    on the fd. An untimed read uses ``readline`` on the text wrapper,
    which may already hold later lines. Switching modes skips those
    lines. The pending buffer is only a partial line saved by a timed
    read, not that wrapper buffer.
    """
    line = _readline_with_timeout(stream, timeout_sec, max_bytes)
    if not line:
        return None
    encoded = line.encode("utf-8", errors="replace")
    if len(encoded) > max_bytes:
        raise ValueError(f"JSON line exceeds {max_bytes} bytes")
    try:
        payload = json.loads(line.strip())
    except json.JSONDecodeError as exc:
        # A finished line that is not JSON says so. A partial with no newline
        # is the pipe closing mid-object. Calling that "Invalid JSON" hid the
        # truncation. A final object with no newline still parses above.
        if not line.endswith("\n"):
            raise ValueError(f"JSON line truncated at EOF: {line!r}") from exc
        raise ValueError(f"Invalid JSON line: {line!r}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"JSON line must contain an object: {payload!r}")
    return payload
