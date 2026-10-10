# WriterAgent - Python Compute Service Worker Stdio
# Copyright (c) 2026 KeithCu
# SPDX-License-Identifier: GPL-3.0-or-later
"""Child stdio loop and the compute restricted unpickler.

The parent pool lives in ``worker_base``, which re-exports the names here.
Formula and vision children call ``run_compute_worker`` from ``worker_base``.
"""

from __future__ import annotations

import io
import logging
import os
import signal
import sys
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

from compute_service.json_forward import COMPUTE_MAX_PAYLOAD_BYTES
from plugin.scripting.ipc import (
    DEFAULT_MAX_PAYLOAD_BYTES,
    AllowlistUnpickler,
    IpcFrameError,
    _PICKLE_LOAD_ERRORS,
    claim_ipc_channel,
    read_pickle_frame,
    write_pickle_frame,
)

log = logging.getLogger("compute_service.worker")

# Allowed builtins for child->host and child stdin compute frames (strictly primitives/scalars/containers).
_RESTRICTED_PICKLE_BUILTINS = frozenset({
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

class RestrictedUnpickler(AllowlistUnpickler):
    """Restricted unpickler for child->host IPC frames.

    Only allows standard builtin container and scalar types (dict, list, tuple,
    str, int, float, bytes, bool, None). Forbids all module imports and callable
    reconstructors (including NumPy) so child processes running untrusted user
    code cannot execute arbitrary code on the host via pickle globals.

    Remaining risk:
    Pickle parsing can still be vulnerable to resource consumption attacks
    (deeply nested structures causing recursion or memory exhaustion), though
    bounded by max_payload_bytes and Python's recursion limit. A future plain
    encoding (JSON metadata + raw byte frames) would eliminate pickle entirely.
    """

    _builtins_allow: frozenset[str] = _RESTRICTED_PICKLE_BUILTINS
    _deny_tail: str = "is forbidden in compute child frames"


def unpack_restricted_pickle_frame(payload: bytes) -> Any:
    """Decode one child IPC payload using RestrictedUnpickler."""
    try:
        return RestrictedUnpickler(io.BytesIO(payload)).load()
    except _PICKLE_LOAD_ERRORS as exc:
        raise ValueError(str(exc)) from exc


def set_pdeathsig(sig: int | None = None) -> bool:
    """Set parent death signal on Linux via prctl so child worker terminates on hard host exit.

    Guarded so non-Linux platforms (macOS/Windows) safely return False without error.
    Default signal is SIGKILL, looked up only on Linux.
    """
    if sys.platform != "linux":
        return False
    # Looked up here, not as a default argument. Defaults are evaluated at
    # import on every platform, and Windows has no SIGKILL.
    if sig is None:
        sig = signal.SIGKILL
    try:
        import ctypes
        import ctypes.util

        # CDLL(None) is the libc this interpreter is already linked to
        # (glibc or musl). find_library and libc.so.6 cover a process that
        # does not export prctl. Debian has libc.so.6; Alpine does not.
        names: list[str | None] = [None]
        found = ctypes.util.find_library("c")
        if isinstance(found, str) and found not in names:
            names.append(found)
        if "libc.so.6" not in names:
            names.append("libc.so.6")
        pr_set_pdeathsig = 1
        for name in names:
            try:
                libc = ctypes.CDLL(name, use_errno=True)
                res = libc.prctl(pr_set_pdeathsig, ctypes.c_ulong(sig), 0, 0, 0)
                return res == 0
            except (OSError, AttributeError):
                continue
        return False
    except Exception:
        return False


def run_compute_worker(handler: Callable[[dict[str, Any]], dict[str, Any]]) -> int:
    """Stdio loop for a formula or vision child.

    Before the loop. writeragent_api treats a missing ``WRITERAGENT_IS_WORKER``
    as the LibreOffice host and calls ``execute_tool`` → ``get_ctx()``. This
    process has no office and no tool-call pipe. ``WRITERAGENT_COMPUTE_WORKER``
    makes that call fail before either path.

    The parent pool reads and writes ``COMPUTE_MAX_PAYLOAD_BYTES`` (33 MiB).
    The stdio default is 16 MiB, so a request the parent had accepted failed
    in the child, and a result over 16 MiB broke this loop (host saw
    ``EMPTY_RESPONSE``).
    """
    os.environ["WRITERAGENT_IS_WORKER"] = "1"
    os.environ["WRITERAGENT_COMPUTE_WORKER"] = "1"
    return run_worker_stdio_loop(handler, max_payload_bytes=COMPUTE_MAX_PAYLOAD_BYTES)


def run_worker_stdio_loop(handler: Callable[[dict[str, Any]], dict[str, Any]], *, max_payload_bytes: int = DEFAULT_MAX_PAYLOAD_BYTES) -> int:
    """Standard binary Pickle 5 stdio worker loop for child subprocesses."""
    # Ensure worker subprocess terminates immediately if master HTTP process dies abruptly
    set_pdeathsig()

    stdin_bin = sys.stdin.buffer

    # claim_ipc_channel duplicates fd 1 for the frame channel and points fd 1
    # at stderr, so a stray print() cannot corrupt the pickle stream. The same
    # helper is the venv child's stdout claim. When stdout is not a real fd 1
    # (tests), it returns the existing buffer and does not redirect.
    try:
        is_real_fd1 = hasattr(sys.stdout, "fileno") and sys.stdout.fileno() == 1
    except (io.UnsupportedOperation, AttributeError, OSError):
        is_real_fd1 = False

    stdout_bin = claim_ipc_channel()
    if is_real_fd1:
        sys.stdout = sys.stderr

    # Signal readiness to supervisor
    write_pickle_frame(stdout_bin, {"status": "ready", "pid": os.getpid()})

    _shutdown_requested = False

    def _sig_shutdown(signum: int, _frame: Any) -> None:
        # sys.exit raises SystemExit. The request handler catches BaseException,
        # so the flag is what turns that into a clean loop break.
        nonlocal _shutdown_requested
        _shutdown_requested = True
        sys.exit(0)

    try:
        signal.signal(signal.SIGTERM, _sig_shutdown)
        signal.signal(signal.SIGINT, _sig_shutdown)
    except (ValueError, AttributeError):
        pass

    while True:
        try:
            req = read_pickle_frame(stdin_bin, max_payload_bytes=max_payload_bytes, unpacker=unpack_restricted_pickle_frame)
        except Exception as exc:
            # If reading/decoding the frame from stdin fails, the stream is desynced.
            # Attempting to continue reading frames from an offset stream corrupts subsequent requests.
            # Break so the worker process terminates and the pool supervisor respawns it.
            log.error("Fatal: failed to read/decode IPC frame from stdin: %s", exc)
            break

        if req is None or _shutdown_requested:
            break

        res: dict[str, Any]
        try:
            if not isinstance(req, dict):
                res = {"status": "error", "error": "Request must be a dict"}
            else:
                res = handler(req)
                if not isinstance(res, dict):
                    res = {"status": "error", "error": "Handler returned non-dict"}
        except BaseException as exc:
            if _shutdown_requested:
                break
            req_id = req.get("id") if isinstance(req, dict) else None
            res = {"id": req_id, "status": "error", "code": "WORKER_EXECUTION_ERROR", "error": f"Unhandled error: {exc}"}

        try:
            write_pickle_frame(stdout_bin, res, max_payload_bytes=max_payload_bytes)
        except IpcFrameError as exc:
            req_id = req.get("id") if isinstance(req, dict) else None
            err_frame = {
                "id": req_id,
                "status": "error",
                "code": "RESULT_TOO_LARGE",
                "error": f"Result exceeds maximum payload size: {exc}",
            }
            try:
                write_pickle_frame(stdout_bin, err_frame, max_payload_bytes=max_payload_bytes)
            except Exception:
                break
        except Exception:
            break

    return 0
