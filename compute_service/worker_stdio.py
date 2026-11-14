# WriterAgent - Python Compute Service Worker Stdio
# Copyright (c) 2026 KeithCu
# SPDX-License-Identifier: GPL-3.0-or-later
"""Child stdio loop and the compute restricted unpickler.

The parent pool lives in ``worker_base``. Formula and vision children call
``run_compute_worker`` from here.
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

from compute_service import error_codes as ec
from compute_service.config import COMPUTE_MAX_PAYLOAD_BYTES
from plugin.framework.ipc import DEFAULT_MAX_PAYLOAD_BYTES, AllowlistUnpickler, IpcFrameError, _PICKLE_LOAD_ERRORS, claim_ipc_channel, read_pickle_frame, write_pickle_frame
from plugin.framework.child_runtime import announce_ready, die_with_parent, error_dict

log = logging.getLogger("compute_service.worker")

# The repo root in a checkout, /app in the runner image.
_SERVICE_ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))


def install_image_pkgs(project_root: str) -> None:
    """Put the runner image's ``<root>/pkgs`` on sys.path by path; the scrubbed child env never carries ``PYTHONPATH``."""
    pkgs = os.path.join(project_root, "pkgs")
    if os.path.isdir(pkgs) and pkgs not in sys.path:
        sys.path.insert(0, pkgs)

# Allowed builtins for child->host and child stdin compute frames (strictly primitives/scalars/containers).
_RESTRICTED_PICKLE_BUILTINS = frozenset({"dict", "list", "tuple", "set", "frozenset", "bytes", "bytearray", "str", "int", "float", "complex", "bool"})


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


COMPUTE_THREAD_ENV_VARS: tuple[str, ...] = (
    "OPENBLAS_NUM_THREADS",
    "OMP_NUM_THREADS",
    "MKL_NUM_THREADS",
    "VECLIB_MAXIMUM_THREADS",
    "NUMEXPR_NUM_THREADS",
)


def configure_compute_worker_env() -> None:
    """Set default single-threaded limits for child compute worker subprocesses.

    Multi-process compute pools (such as formula and vision workers) run multiple
    worker processes concurrently. By default, numerical libraries (OpenBLAS,
    OpenMP, MKL, NumExpr) detect all host CPU cores and allocate thread pools
    sized to the entire machine in each process. When multiple worker processes run
    in parallel, this causes severe CPU core over-subscription and cache thrashing.

    Limiting each worker process to 1 thread eliminates this contention and allows
    clean linear scaling across worker processes. We use setdefault() so that any
    explicit user or administrator override in the environment is preserved.
    """
    for var_name in COMPUTE_THREAD_ENV_VARS:
        os.environ.setdefault(var_name, "1")


def run_compute_worker(handler: Callable[[dict[str, Any]], dict[str, Any]]) -> int:
    """Stdio loop for a formula or vision child: marked as a compute worker (no office, no tool pipe), framed at the pool's payload cap.

    The child's path setup lives here: the pool scrubs PYTHONPATH from the
    child env (plugin/framework/subprocess_env.py), so the image's pkgs
    (numpy, pandas, ...) is added by path. It is added before the loop because
    the handlers import those packages lazily, per request.
    """
    install_image_pkgs(_SERVICE_ROOT)
    os.environ["WRITERAGENT_IS_WORKER"] = "1"
    os.environ["WRITERAGENT_COMPUTE_WORKER"] = "1"
    configure_compute_worker_env()
    return run_worker_stdio_loop(handler, max_payload_bytes=COMPUTE_MAX_PAYLOAD_BYTES)


def run_worker_stdio_loop(handler: Callable[[dict[str, Any]], dict[str, Any]], *, max_payload_bytes: int = DEFAULT_MAX_PAYLOAD_BYTES) -> int:
    """Standard binary Pickle 5 stdio worker loop for child subprocesses."""
    die_with_parent()

    stdin_bin = sys.stdin.buffer

    # claim_ipc_channel duplicates fd 1 for the frame channel and points fd 1
    # at stderr, so a stray print() cannot corrupt the pickle stream.
    stdout_bin = claim_ipc_channel()

    # Signal readiness to supervisor
    announce_ready(stdout_bin)

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

    try:
        # optional vendored executor; the loop runs without it
        import plugin.contrib.smolagents.local_python_executor as _lpe
    except ImportError:
        _lpe = None  # type: ignore[assignment]

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
            res = handler(req)
        except BaseException as exc:
            if _shutdown_requested:
                break
            res = error_dict(ec.WORKER_EXECUTION_ERROR, f"Unhandled error: {exc}", req_id=req.get("id"))

        # After an abandoned timeout the frame carries "exiting" and os._exit(1) follows the write.
        should_exit = _lpe is not None and getattr(_lpe, "abandoned_timeout", False)
        if should_exit:
            assert _lpe is not None
            _lpe.abandoned_timeout = False
            res["exiting"] = True

        wrote = False
        try:
            write_pickle_frame(stdout_bin, res, max_payload_bytes=max_payload_bytes)
            wrote = True
        except IpcFrameError as exc:
            out = error_dict(ec.RESULT_TOO_LARGE, f"Result exceeds maximum payload size: {exc}", req_id=req.get("id"))
            if should_exit:
                out["exiting"] = True
            try:
                write_pickle_frame(stdout_bin, out, max_payload_bytes=max_payload_bytes)
                wrote = True
            except Exception:
                pass
        except Exception:
            pass

        if not wrote:
            break
        if should_exit:
            os._exit(1)

    return 0
