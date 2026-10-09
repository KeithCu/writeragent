# WriterAgent - Python Compute Service Base Worker & Pool Infrastructure
# Copyright (c) 2026 KeithCu
# SPDX-License-Identifier: GPL-3.0-or-later
"""
Shared subprocess worker loop, worker process wrapper, and process pool supervisor.

Provides:
- High-speed length-prefixed Pickle 5 binary framing over stdio pipes
- Deadline-bounded pickle reads and stdin writes (header + payload)
- Live stderr drain (start_stderr_drain) so piped stderr cannot deadlock
- Hard SIGKILL only when a child never writes its response frame (vision drains
  one late frame first; formula waits for the in-process error frame)
- Exclusive worker occupancy. Idle means the process completed a handshake or
  a response frame was consumed — a dead pid is not idle
- Automatic crash recovery and worker recycling after max_tasks
"""

from __future__ import annotations

import builtins
import contextlib
import enum
import io
import logging
import os
import pickle
import queue
import signal
import subprocess
import sys
import threading
import time
from collections import OrderedDict
from typing import TYPE_CHECKING, Any, Generic, TypeVar, cast

if TYPE_CHECKING:
    from collections.abc import Callable, Generator

from plugin.framework.worker_pool import StderrTail, get_subprocess_creationflags, start_stderr_drain
from plugin.scripting.ipc import DEFAULT_MAX_PAYLOAD_BYTES, IpcFrameError, IpcPartialFrameTimeout, claim_ipc_channel, read_pickle_frame, read_pickle_frame_with_timeout, write_pickle_frame, write_pickle_frame_with_timeout
from plugin.scripting.sandbox import optimize_popen_pipes, scrub_subprocess_env

log = logging.getLogger("compute_service.worker")

_SPAWN_READY_TIMEOUT_SEC = 15.0
_STDERR_SNIPPET = 500

_PoolT = TypeVar("_PoolT", bound="BaseProcessPool")
_ValT = TypeVar("_ValT")

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


class RestrictedUnpickler(pickle.Unpickler):
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

    def find_class(self, module: str, name: str) -> Any:
        if module in ("builtins", "__builtin__") and name in _RESTRICTED_PICKLE_BUILTINS:
            return getattr(builtins, name)
        raise pickle.UnpicklingError(f"global {module}.{name} is forbidden in compute child frames")


def unpack_restricted_pickle_frame(payload: bytes) -> Any:
    """Decode one child IPC payload using RestrictedUnpickler."""
    try:
        return RestrictedUnpickler(io.BytesIO(payload)).load()
    except _PICKLE_LOAD_ERRORS as exc:
        raise ValueError(str(exc)) from exc


def resolve_override(override: _ValT | None, default: _ValT) -> _ValT:
    """Return *override* if not None, else *default*."""
    return default if override is None else override


class PoolSingleton(Generic[_PoolT]):
    """Thread-safe global singleton holder for a BaseProcessPool subclass."""

    _lock: threading.Lock
    _pool: _PoolT | None
    _closed: bool

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pool = None
        self._closed = False

    def get(self, factory: Callable[[], _PoolT]) -> _PoolT:
        with self._lock:
            if self._closed:
                raise RuntimeError("Compute pool is shut down.")
            if self._pool is None:
                self._pool = factory()
            return self._pool

    def shutdown(self, *, permanent: bool = False) -> None:
        """Drop the pool. *permanent* makes a later ``get`` raise.

        Tests and a restarted process call shutdown without *permanent* so
        the next ``get`` builds a new pool. The server process passes
        *permanent* on the way out so an abandoned handler cannot spawn
        children after shutdown.
        """
        with self._lock:
            if self._pool is not None:
                self._pool.shutdown()
                self._pool = None
            self._closed = permanent


def set_pdeathsig(sig: int | None = None) -> bool:
    """Set parent death signal on Linux via prctl so child worker terminates on hard host exit.

    Guarded so non-Linux platforms (macOS/Windows) safely return False without error.
    Default signal is SIGKILL, looked up only on Linux.
    """
    if sys.platform != "linux":
        return False
    # What was wrong: the default was signal.SIGKILL. Defaults are evaluated at
    # import on every platform, and Windows has no SIGKILL, so ty (and a real
    # import) failed before this guard. Why this works: the lookup runs only
    # after the Linux return, same as venv_worker process-group kill.
    if sig is None:
        sig = signal.SIGKILL
    try:
        import ctypes
        import ctypes.util

        # What was wrong: CDLL("libc.so.6") is glibc's soname. On musl that
        # file is absent, prctl never ran, and a hard SIGKILL of the parent
        # left workers alive. The failure returned False with no log.
        # Why this works: CDLL(None) uses the libc this interpreter is already
        # linked to (glibc or musl). find_library and libc.so.6 cover a
        # process that does not export prctl from the global namespace.
        # The Debian image has libc.so.6; Alpine does not.
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


def remaining_sec(deadline: float, *, floor: float = 0.01) -> float:
    """Return remaining seconds until *deadline*, bounded below by *floor*."""
    return max(floor, deadline - time.monotonic())


class _DrainState(enum.Enum):
    """Lifecycle states for draining late IPC responses after timeout."""

    IDLE = "idle"
    DRAINING = "draining"
    RELEASE_WAIT = "release_wait"
    DRAINED = "drained"


class BaseProcessWorker:
    """Wrapper around one persistent child subprocess communicating via Pickle 5 frames."""

    worker_id: int
    script_path: str
    worker_name: str
    max_payload_bytes: int
    lock: threading.Lock
    _lifecycle_lock: threading.Lock
    tasks_executed: int
    recover_on_timeout: bool
    on_process_exit: Callable[[int], None] | None
    _drain_state: _DrainState
    _drain_lock: threading.Lock
    _shutting_down: bool
    default_timeout_sec: float

    def __init__(self, worker_id: int, script_path: str, worker_name: str = "Worker", *, max_payload_bytes: int = DEFAULT_MAX_PAYLOAD_BYTES, recover_on_timeout: bool = False, on_process_exit: Callable[[int], None] | None = None, default_timeout_sec: float = 30.0) -> None:
        self.worker_id = worker_id
        self.script_path = script_path
        self.worker_name = worker_name
        self.max_payload_bytes = max_payload_bytes
        self.recover_on_timeout = recover_on_timeout
        self.default_timeout_sec = float(default_timeout_sec)
        # Formula sessions key off this pid. The callback runs once the child
        # is being reaped so the supervisor can drop every session on it
        # before a replacement process is started.
        self.on_process_exit = on_process_exit
        self.process: subprocess.Popen[bytes] | None = None
        self.lock = threading.Lock()
        # Serializes kill/reap. Distinct from ``lock``, which execute holds
        # across spawn, so reap must not take ``lock``.
        self._lifecycle_lock = threading.Lock()
        self.tasks_executed = 0
        self._stderr_drain: StderrTail | None = None
        # idle: no timeout drain. draining: late frame still on the pipe.
        # release_wait: release_worker already ran and must re-idle after the
        # frame. drained: frame consumed before release_worker ran.
        self._drain_state = _DrainState.IDLE
        self._drain_lock = threading.Lock()
        self._release_cb: Callable[[], None] | None = None
        # Pool.shutdown sets this before kill(). execute and respawn read it
        # without self.lock: kill takes _lifecycle_lock, not the execute lock,
        # so a flag under self.lock would be invisible until the cell finished.
        self._shutting_down = False
        self.respawn()

    def _stderr_snippet(self) -> str:
        drain = self._stderr_drain
        if drain is None:
            return ""
        text = drain.text().strip()
        if not text:
            return ""
        return text[-_STDERR_SNIPPET:]

    def _reap_previous_process(self) -> None:
        """Wait on the Popen ``respawn`` is about to replace.

        A child that exited outside ``kill()`` used to stay a zombie: the next
        ``respawn`` assigned a new ``Popen`` and nothing called ``wait()``.
        ``poll()`` reaps an already-dead child. Kill first only when it is
        still running, so a reused pid is not signaled.
        """
        with self._lifecycle_lock:
            previous = self.process
            drain = self._stderr_drain
            pid = previous.pid if previous is not None else None
            self.process = None
            self._stderr_drain = None
            # Signal only a child poll() still reports as running. After
            # wait() the pid can be reused; a second kill() used to signal
            # that new process.
            if previous is not None and previous.poll() is None:
                try:
                    previous.kill()
                except Exception:
                    log.debug("%s #%d kill of pid=%s failed", self.worker_name, self.worker_id, pid, exc_info=True)
            if previous is not None:
                try:
                    previous.wait(timeout=1.0)
                except Exception:
                    log.debug("%s #%d wait for pid=%s failed", self.worker_name, self.worker_id, pid, exc_info=True)
            # After wait(), the pid is reaped. Tell the pool before the next
            # Popen can reuse it. A callback that runs while poll() still
            # said "running" used to race a lookup that treated the session
            # as live.
            if pid is not None and self.on_process_exit is not None:
                try:
                    self.on_process_exit(pid)
                except Exception:
                    log.exception("%s #%d process-exit callback failed for pid=%s", self.worker_name, self.worker_id, pid)
        if drain is not None:
            drain.join(timeout=0.2)

    def respawn(self, timeout_sec: float = _SPAWN_READY_TIMEOUT_SEC) -> None:
        """Spawn worker subprocess and await readiness handshake.

        Returns without a child when the pool is stopping. Recycle calls this
        after kill(); the flag is what stops that spawn. The check after reap
        covers a shutdown that arrives while the previous child is reaped.
        The new ``Popen`` is published under ``_lifecycle_lock``, which
        ``kill()`` also holds, so a shutdown during spawn still reaps it.
        """
        if self._shutting_down:
            return
        self._reap_previous_process()
        if self._shutting_down:
            return
        cmd = [sys.executable, self.script_path]
        try:
            # Scrub matches the venv host: drop PYTHONHOME / credential-like
            # names so the child does not inherit the parent's secret env.
            # **creationflags kwargs make the type checker treat this as Popen[str].
            proc = cast(
                "subprocess.Popen[bytes]",
                subprocess.Popen(
                    cmd,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    bufsize=0,
                    text=False,
                    env=scrub_subprocess_env(dict(os.environ)),
                    **get_subprocess_creationflags(),
                ),
            )
            # Do not hold _lifecycle_lock across the handshake read: kill()
            # takes that lock, and the read can block for the spawn budget.
            if not self._adopt_spawned_process(proc):
                self._discard_unadopted_process(proc)
                return
            optimize_popen_pipes(proc)
            self._stderr_drain = start_stderr_drain(proc.stderr, name=f"{self.worker_name}-stderr-{self.worker_id}")
            ready_data: Any = None
            if proc.stdout is not None:
                ready_data = read_pickle_frame_with_timeout(
                    proc.stdout,
                    timeout_sec,
                    is_alive=self.is_alive,
                    max_payload_bytes=self.max_payload_bytes,
                    require_dict=True,
                    unpacker=unpack_restricted_pickle_frame,
                )
            # EOF (None) and any dict whose status is not "ready" used to
            # count as success. The worker was marked idle, and the next
            # execute burned the full call timeout before EMPTY_RESPONSE.
            if not isinstance(ready_data, dict) or ready_data.get("status") != "ready":
                snippet = self._stderr_snippet()
                extra = f" stderr={snippet!r}" if snippet else " stderr=<empty>"
                status = ready_data.get("status") if isinstance(ready_data, dict) else None
                log.error("%s #%d spawn handshake was not ready (status=%r)%s", self.worker_name, self.worker_id, status, extra)
                self.kill()
                return
            log.info("%s #%d spawned (pid=%s, status=%s)", self.worker_name, self.worker_id, ready_data.get("pid", proc.pid), ready_data.get("status"))
            self.tasks_executed = 0
        except subprocess.TimeoutExpired:
            # Handshake hang: child may still be importing, or stdout was not pickle.
            # Execute-path timeouts already attach _stderr_snippet(); spawn must too.
            snippet = self._stderr_snippet()
            spawned = self.process
            rc = spawned.poll() if spawned is not None else None
            extra = f" stderr={snippet!r}" if snippet else " stderr=<empty>"
            log.error("%s #%d spawn handshake timed out (returncode=%s)%s", self.worker_name, self.worker_id, rc, extra)
            self.kill()
        except Exception as exc:
            snippet = self._stderr_snippet()
            extra = f" stderr={snippet!r}" if snippet else " stderr=<empty>"
            log.error("Failed to spawn %s #%d: %s%s", self.worker_name, self.worker_id, exc, extra)
            self.kill()


    def _adopt_spawned_process(self, proc: subprocess.Popen[bytes]) -> bool:
        """Publish *proc*, or refuse when shutdown already won.

        What was wrong: ``self.process = proc`` ran after the unlocked
        ``_shutting_down`` check. ``kill()`` in that window saw ``None``,
        reaped nothing, and the new child lived until the parent exited.
        Why this change: assignment and the flag share ``_lifecycle_lock``,
        which ``kill()`` holds. Shutdown either sees this child, or this
        method refuses and the caller kills the unpublished ``Popen``.
        """
        with self._lifecycle_lock:
            if self._shutting_down:
                return False
            self.process = proc
            return True

    def _discard_unadopted_process(self, proc: subprocess.Popen[bytes]) -> None:
        """Kill a child that was never assigned to ``self.process``."""
        pid = proc.pid
        try:
            if proc.poll() is None:
                proc.kill()
        except Exception:
            log.debug("%s #%d kill of unadopted pid=%s failed", self.worker_name, self.worker_id, pid, exc_info=True)
        try:
            proc.wait(timeout=1.0)
        except Exception:
            log.debug("%s #%d wait for unadopted pid=%s failed", self.worker_name, self.worker_id, pid, exc_info=True)

    def is_alive(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def kill(self) -> None:
        """Terminate the child using the same rules as spawn's reap.

        ``kill()`` used to signal even after ``poll()`` had reaped the pid,
        and it left ``_stderr_drain`` set so the next ``respawn`` dropped that
        thread. ``_reap_previous_process`` already avoided both.
        """
        self._reap_previous_process()

    def _fail_request(
        self,
        code: str,
        msg: str,
        *,
        budget_sec: float,
        pid: int | None,
        kill: bool = True,
        timeout: bool = False,
        drain_timeout_sec: float | None = None,
    ) -> dict[str, Any]:
        """Build an error dict, optionally killing or draining one late frame.

        *timeout* is a response-read timeout: vision drains the late frame,
        formula kills. A stdin write timeout passes ``kill=True`` and
        ``timeout=False``. A partial frame is not a late response.
        """
        snippet = self._stderr_snippet()
        if snippet:
            msg = f"{msg}\n{snippet}"
        if timeout:
            if self.recover_on_timeout:
                # Bugfix: give the late-drain step its own timeout budget (Bug 3).
                # What was wrong: leftover request budget (e.g. 0.01s after queue wait) was passed
                # to late-drain, causing premature timeout and SIGKILL of healthy workers.
                # Why this change: support drain_timeout_sec so callers can give late-drain a full budget.
                # When the caller omits it, use the original request budget, not time left after spawn.
                eff_drain = budget_sec if drain_timeout_sec is None else max(0.01, float(drain_timeout_sec))
                log.warning("%s execution timed out after %.1fs on worker #%d; draining late frame from pid=%s", self.worker_name, budget_sec, self.worker_id, pid)
                self._start_late_drain(eff_drain)
            else:
                log.warning("%s execution timed out after %.1fs on worker #%d; terminating pid=%s", self.worker_name, budget_sec, self.worker_id, pid)
                self.kill()
        elif kill:
            self.kill()
        res: dict[str, Any] = {"status": "error", "code": code, "error": msg}
        if code in ("EXECUTION_TIMEOUT", "WORKER_CRASHED"):
            res["message"] = msg
        return res

    def execute(self, payload: dict[str, Any], timeout_sec: float, drain_timeout_sec: float | None = None) -> dict[str, Any]:
        """Send request to worker process and await response with timeout."""
        # One deadline covers spawn, the stdin write, and the stdout read.
        # The error text keeps this original value so a handshake that used
        # the whole budget does not report "exceeded 0 seconds".
        budget_sec = max(0.01, float(timeout_sec))
        with self.lock:
            deadline = time.monotonic() + budget_sec

            def _budget_left() -> float:
                return max(0.01, deadline - time.monotonic())

            # kill() during shutdown sets self.process to None. Reading it
            # again for .pid or .stdin raised AttributeError and the request
            # became 500 INTERNAL_ERROR. This snapshot is the child we talk to.
            proc = self.process

            def _pid() -> int | None:
                return proc.pid if proc is not None else None

            if proc is None or proc.poll() is not None:
                # What was wrong: shutdown killed the child, then this branch
                # saw proc is None and called respawn(). The new interpreter
                # ran the cell and was killed on the way out.
                # Why this change: the pool sets _shutting_down before kill().
                # A dead child during shutdown is SERVICE_SHUTDOWN, not a new process.
                if self._shutting_down:
                    return self._fail_request("SERVICE_SHUTDOWN", f"{self.worker_name} #{self.worker_id} is shutting down.", budget_sec=budget_sec, pid=_pid(), kill=False)
                # Respect request deadline: do not allow spawn handshake to exceed
                # the remaining request budget.
                spawn_budget = min(_SPAWN_READY_TIMEOUT_SEC, _budget_left())
                self.respawn(timeout_sec=spawn_budget)
                proc = self.process
                if proc is None or proc.poll() is not None:
                    return self._fail_request("WORKER_SPAWN_FAILED", f"{self.worker_name} #{self.worker_id} could not be started.", budget_sec=budget_sec, pid=_pid(), kill=False)

            if proc.stdin is None or proc.stdout is None:
                return self._fail_request("WORKER_SPAWN_FAILED", f"{self.worker_name} #{self.worker_id} has no stdio pipes.", budget_sec=budget_sec, pid=_pid(), kill=False)

            try:
                # What was wrong: this write blocked while self.lock was held.
                # A child that stopped reading stdin never returned, so
                # release_worker never ran and the slot stayed leased.
                # Why this change: the write shares the request deadline.
                # A partial frame is desynchronized, so the child is killed
                # instead of late-drained.
                write_pickle_frame_with_timeout(
                    proc.stdin,
                    payload,
                    _budget_left(),
                    max_payload_bytes=self.max_payload_bytes,
                    is_alive=self.is_alive,
                )
            except IpcFrameError as exc:
                # Raised before any byte is written. The child is still the
                # same kernel; killing it would drop every shared session.
                return {"status": "error", "code": "PAYLOAD_TOO_LARGE", "error": str(exc)}
            except subprocess.TimeoutExpired:
                self.tasks_executed += 1
                return self._fail_request(
                    "EXECUTION_TIMEOUT",
                    f"Execution exceeded maximum timeout of {int(budget_sec)} seconds.",
                    budget_sec=budget_sec,
                    pid=_pid(),
                    kill=True,
                    timeout=False,
                )
            except (BrokenPipeError, OSError) as exc:
                return self._fail_request("WORKER_PIPE_BROKEN", f"Failed to send request to {self.worker_name} #{self.worker_id}: {exc}", budget_sec=budget_sec, pid=_pid())

            try:
                resp = read_pickle_frame_with_timeout(
                    proc.stdout,
                    _budget_left(),
                    is_alive=self.is_alive,
                    max_payload_bytes=self.max_payload_bytes,
                    unpacker=unpack_restricted_pickle_frame,
                )
            except subprocess.TimeoutExpired:
                # Count timeouts toward worker tasks executed so hanging workers eventually recycle
                self.tasks_executed += 1
                return self._fail_request(
                    "EXECUTION_TIMEOUT",
                    f"Execution exceeded maximum timeout of {int(budget_sec)} seconds.",
                    budget_sec=budget_sec,
                    pid=_pid(),
                    timeout=True,
                    drain_timeout_sec=drain_timeout_sec,
                )
            except IpcPartialFrameTimeout:
                # What was wrong: a deadline after the first byte raised
                # ConnectionError, and the generic handler below reported
                # WORKER_CRASHED (HTTP 500). Vision then skipped this kill
                # and would have drained a pipe that is no longer aligned.
                # Why this change: the cell timed out. timeout=False kills
                # instead of late-draining, because the next read would
                # consume the rest of this frame as a new response.
                self.tasks_executed += 1
                return self._fail_request(
                    "EXECUTION_TIMEOUT",
                    f"Execution exceeded maximum timeout of {int(budget_sec)} seconds.",
                    budget_sec=budget_sec,
                    pid=_pid(),
                    timeout=False,
                    kill=True,
                )
            except Exception as exc:
                return self._fail_request("WORKER_CRASHED", f"{self.worker_name} error: {exc}", budget_sec=budget_sec, pid=_pid())
            if resp is None or not isinstance(resp, dict):
                return self._fail_request("EMPTY_RESPONSE", f"No response returned from {self.worker_name}.", budget_sec=budget_sec, pid=_pid())
            self.tasks_executed += 1
            return resp

    def _start_late_drain(self, timeout_sec: float) -> None:
        """Read the one frame a timed-out vision call will still write.

        The same budget applies again. A call that finishes shortly after the
        client gave up keeps the process. A call that never returns is killed
        so the slot can respawn.
        """
        # Snapshot process and its stdout while self.lock is still held by run_task,
        # preventing race conditions with concurrent respawn() or kill().
        proc = self.process
        stdout = proc.stdout if proc is not None else None
        with self._drain_lock:
            self._drain_state = _DrainState.DRAINING
            self._release_cb = None
        threading.Thread(
            target=self._drain_late_response,
            args=(proc, stdout, timeout_sec),
            name=f"{self.worker_name}-drain-{self.worker_id}",
            daemon=True,
        ).start()

    def _drain_late_response(self, proc: subprocess.Popen[bytes] | None, stdout: Any, timeout_sec: float) -> None:
        try:
            resp: Any = None
            if stdout is not None and self.is_alive():
                resp = read_pickle_frame_with_timeout(
                    stdout,
                    timeout_sec,
                    is_alive=self.is_alive,
                    max_payload_bytes=self.max_payload_bytes,
                    unpacker=unpack_restricted_pickle_frame,
                )
            if not isinstance(resp, dict):
                # EOF or a non-frame: the pipe cannot take another request.
                self.kill()
        except subprocess.TimeoutExpired:
            log.warning("%s late frame exceeded %.1fs on worker #%d; terminating pid=%s", self.worker_name, timeout_sec, self.worker_id, proc.pid if proc is not None else None)
            self.kill()
        except Exception:
            log.exception("%s late-frame drain failed on worker #%d", self.worker_name, self.worker_id)
            self.kill()
        self._complete_drain()

    def _complete_drain(self) -> None:
        with self._drain_lock:
            if self._drain_state == _DrainState.RELEASE_WAIT:
                callback = self._release_cb
                self._release_cb = None
                self._drain_state = _DrainState.IDLE
            else:
                callback = None
                self._drain_state = _DrainState.DRAINED
        if callback is not None:
            callback()

    def defer_release(self, callback: Callable[[], None]) -> bool:
        """Run *callback* only after a late-frame drain, if one is in progress.

        Returns True when the caller must not release yet. The stdio protocol
        is one request then one response; idling the worker before the late
        frame arrives hands that frame to the next caller.
        """
        with self._drain_lock:
            if self._drain_state == _DrainState.DRAINING:
                self._drain_state = _DrainState.RELEASE_WAIT
                self._release_cb = callback
                return True
            if self._drain_state == _DrainState.DRAINED:
                self._drain_state = _DrainState.IDLE
                return False
            return False


class BaseProcessPool:
    """Base supervisor for a bounded pool of child worker subprocesses."""

    script_path: str
    num_workers: int
    default_timeout_sec: int
    max_tasks: int
    worker_name: str
    idle_worker_ttl_sec: float | None
    max_payload_bytes: int
    _is_shutdown: bool
    _lock: threading.RLock
    _cond: threading.Condition
    _reaper_stop_event: threading.Event

    def __init__(self, script_path: str, num_workers: int = 1, default_timeout_sec: int = 30, max_tasks: int = 500, worker_name: str = "Worker", idle_worker_ttl_sec: float | None = None, max_payload_bytes: int = DEFAULT_MAX_PAYLOAD_BYTES, recover_on_timeout: bool = False, on_process_exit: Callable[[int], None] | None = None) -> None:
        self.script_path = script_path
        self.num_workers = max(0, num_workers)
        self.default_timeout_sec = default_timeout_sec
        self.max_tasks = max_tasks
        self.worker_name = worker_name
        self.idle_worker_ttl_sec = idle_worker_ttl_sec
        self.max_payload_bytes = max_payload_bytes
        self.workers: list[BaseProcessWorker] = []
        self._is_shutdown = False
        self._lock = threading.RLock()
        # Oldest idle worker is first. Release moves a worker to the end.
        self._idle: OrderedDict[BaseProcessWorker, None] = OrderedDict()
        # Leased, cold-claimed, or mid-recycle. A dead pid nobody holds is
        # not idle; the next lease respawns it. Recycle stays in this set
        # until respawn finishes, or lease_any treats that dead pid as free.
        self._leased: set[BaseProcessWorker] = set()
        self._worker_last_active: dict[BaseProcessWorker, float] = {}
        self._cond = threading.Condition(self._lock)
        self._reaper_stop_event = threading.Event()
        self._recycle_queue: queue.Queue[BaseProcessWorker | None] = queue.Queue()
        self._recycle_thread: threading.Thread = threading.Thread(
            target=self._recycle_loop,
            name=f"{self.worker_name}-recycle-loop",
            daemon=True,
        )
        self._recycle_thread.start()

        if self.num_workers > 0:
            # Workers spawn one at a time, each waiting on its ready handshake
            # (up to _SPAWN_READY_TIMEOUT_SEC). Spawning them in parallel would
            # cut startup roughly with the worker count. Left for later.
            for i in range(self.num_workers):
                w = BaseProcessWorker(i + 1, script_path=script_path, worker_name=worker_name, max_payload_bytes=max_payload_bytes, recover_on_timeout=recover_on_timeout, on_process_exit=on_process_exit, default_timeout_sec=default_timeout_sec)
                self.workers.append(w)
                # Idle only after the ready handshake. A failed spawn stays
                # out of the idle set; the next lease respawns that slot.
                if w.is_alive():
                    self._idle[w] = None
                    # Stamp after spawn. A timestamp taken before this loop made a
                    # slow handshake look already idle, so a short idle TTL killed
                    # the child as soon as the reaper ran.
                    self._worker_last_active[w] = time.monotonic()

        # 0 disables the reaper, same as None. A zero interval would spin, and
        # treating 0 as "evict immediately" would kill workers that just spawned.
        if self.idle_worker_ttl_sec is not None and self.idle_worker_ttl_sec > 0:
            self._start_idle_reaper()

    def _start_reaper(self, name: str, interval: float, fn: Callable[[], None]) -> threading.Thread:
        def _loop() -> None:
            # Wait on stop event instead of time.sleep so pool shutdown terminates immediately.
            # What was wrong: fn() had no handler. One OSError from is_alive()
            # or join killed this daemon, and idle workers and shared sessions
            # were never evicted again for the process lifetime.
            # Why this change: log the tick and keep waiting on the stop event.
            while not self._reaper_stop_event.wait(interval):
                if self._is_shutdown:
                    continue
                try:
                    fn()
                except Exception:
                    log.exception("Reaper %s tick failed", name)

        t = threading.Thread(target=_loop, name=name, daemon=True)
        t.start()
        return t

    def _start_idle_reaper(self) -> None:
        ttl = cast("float", self.idle_worker_ttl_sec)
        interval = max(0.02, min(ttl / 6.0, 300.0))
        self._start_reaper(
            name=f"{self.worker_name}-idle-reaper",
            interval=interval,
            fn=self._evict_idle_workers,
        )

    def _evict_idle_workers(self) -> None:
        if self._is_shutdown or self.idle_worker_ttl_sec is None:
            return
        now = time.monotonic()
        stale: list[BaseProcessWorker] = []
        with self._cond:
            # One pass: drop dead pids, collect workers past the idle TTL.
            # Removal happens before kill so lease_any cannot pop them.
            for w in list(self._idle):
                if not w.is_alive():
                    # Already exited. poll() inside is_alive reaped it. It is
                    # not idle: the next lease performs the handshake.
                    self._idle.pop(w, None)
                    continue
                if self._skip_idle_evict(w):
                    continue
                last_active = self._worker_last_active.get(w, now)
                if now - last_active >= self.idle_worker_ttl_sec:
                    stale.append(w)
            for w in stale:
                self._idle.pop(w, None)
        for w in stale:
            w.kill()
        # Do not put the killed process back in idle. Idle is a successful
        # handshake or a consumed response frame. lease_any claims the cold
        # slot and respawns it.
        if stale:
            with self._cond:
                self._cond.notify_all()
            log.info("Idle worker reaper terminated %d %s(s) idle for >%.1fs", len(stale), self.worker_name, self.idle_worker_ttl_sec)

    def _skip_idle_evict(self, worker: BaseProcessWorker) -> bool:
        """Return true to leave *worker* running past the idle TTL.

        Formula sessions override this. The base pool has no session map.
        """
        del worker
        return False

    def is_enabled(self) -> bool:
        return self.num_workers > 0 and not self._is_shutdown

    def _pick_idle_worker(self) -> BaseProcessWorker | None:
        """Pop one protocol-ready idle worker. Caller must hold self._cond.

        A dead pid is removed from idle rather than returned. The caller
        claims that slot as cold and respawns it (handshake, then leased).
        """
        while self._idle:
            worker, _unused = self._idle.popitem(last=False)
            if worker.is_alive():
                return worker
        return None

    def _claim_cold_unlocked(self) -> BaseProcessWorker | None:
        """Return a pool slot whose process has exited and nobody holds.

        Caller holds self._cond. The slot is not idle: idle is only a
        successful handshake or a consumed response frame.
        """
        for worker in self.workers:
            if worker in self._idle or worker in self._leased:
                continue
            if not worker.is_alive():
                return worker
        return None

    def lease_any(self, timeout_sec: float) -> BaseProcessWorker | None:
        """Acquire a protocol-ready worker, or a dead slot the caller will respawn.

        The dead slot is not idle. ``execute`` performs the handshake;
        release idles the worker only after that handshake or after a
        response frame is consumed.
        """
        deadline = time.monotonic() + max(0.0, float(timeout_sec))
        while True:
            with self._cond:
                if self._is_shutdown:
                    return None
                claimed = self._pick_idle_worker()
                if claimed is None:
                    claimed = self._claim_cold_unlocked()
                if claimed is not None:
                    self._leased.add(claimed)
                    return claimed
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._cond.wait(remaining)

    def lease_specific(self, worker: BaseProcessWorker, timeout_sec: float) -> BaseProcessWorker | None:
        """Acquire *worker* when it is idle, or once its process has exited."""
        deadline = time.monotonic() + max(0.0, float(timeout_sec))
        while True:
            with self._cond:
                if self._is_shutdown:
                    return None
                if worker in self._idle:
                    self._idle.pop(worker, None)
                    self._leased.add(worker)
                    return worker
                # Process exit: not idle, but the slot can be respawned by execute.
                if not worker.is_alive() and worker not in self._leased:
                    self._leased.add(worker)
                    return worker
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._cond.wait(remaining)

    @contextlib.contextmanager
    def leased(
        self,
        worker: BaseProcessWorker | None = None,
        *,
        timeout_sec: float | None = None,
    ) -> Generator[BaseProcessWorker | None, None, None]:
        """Context manager leasing a worker and releasing on exit.

        Leases *worker* if given, else any available worker.
        Always releases the leased worker upon exiting the block.
        """
        timeout = float(self.default_timeout_sec) if timeout_sec is None else float(timeout_sec)
        w = self.lease_specific(worker, timeout_sec=timeout) if worker is not None else self.lease_any(timeout_sec=timeout)
        try:
            yield w
        finally:
            if w is not None:
                self.release_worker(w)

    def should_recycle_worker(self, worker: BaseProcessWorker) -> bool:
        """Predicate to determine if worker should be recycled on release."""
        return worker.tasks_executed >= self.max_tasks

    def release_worker(self, worker: BaseProcessWorker) -> None:
        """Return worker to idle set, recycling if max_tasks reached.

        A vision timeout returns before the child writes its frame. Re-idling
        immediately would desync the next request, so that release waits until
        the drain thread has consumed the frame.
        """
        if worker.defer_release(lambda: self._finish_release(worker)):
            return
        self._finish_release(worker)

    def _finish_release(self, worker: BaseProcessWorker) -> None:
        if self._is_shutdown:
            worker.kill()
            with self._cond:
                self._leased.discard(worker)
                self._cond.notify_all()
            return

        recycle = self.should_recycle_worker(worker)
        kill_worker = False
        with self._cond:
            if self._is_shutdown:
                self._leased.discard(worker)
                kill_worker = True
                self._cond.notify_all()
            elif recycle:
                # Stay leased across kill/respawn. Those calls run outside
                # this lock because kill re-enters it from on_process_exit.
                # Dropping the lease first made is_alive() false look like a
                # free slot: lease_any claimed this same wrapper, the in-flight
                # call failed (pipe broken, empty response, or spawn failed),
                # and the wrapper ended in both _idle and _leased.
                pass
            else:
                self._leased.discard(worker)
                if worker.is_alive():
                    # The response frame was consumed and worker is alive: return to idle.
                    self._idle[worker] = None
                    self._idle.move_to_end(worker)
                    self._worker_last_active[worker] = time.monotonic()
                self._cond.notify_all()

        if kill_worker:
            worker.kill()
            return

        if recycle:
            log.info("Recycling %s #%d after %d tasks to refresh memory", self.worker_name, worker.worker_id, worker.tasks_executed)
            self._recycle_queue.put(worker)

    def _recycle_loop(self) -> None:
        """Persistent worker thread for recycling child processes without thread-exit PDEATHSIG races."""
        while True:
            worker = self._recycle_queue.get()
            if worker is None:
                break
            self._recycle_worker_async(worker)

    def _recycle_worker_async(self, worker: BaseProcessWorker) -> None:
        """Kill and respawn recycled worker off the request path."""
        kill_worker = False
        try:
            worker.kill()
            if not self._is_shutdown:
                worker.respawn()
        finally:
            with self._cond:
                # Drop the recycle lease before idle. The other order lets a
                # concurrent lease pop idle while this wrapper is still leased,
                # then this discard clears the lease that pop just took.
                self._leased.discard(worker)
                if not self._is_shutdown and worker.is_alive():
                    self._idle[worker] = None
                    self._idle.move_to_end(worker)
                    self._worker_last_active[worker] = time.monotonic()
                elif self._is_shutdown:
                    kill_worker = True
                self._cond.notify_all()
            if kill_worker:
                worker.kill()

    def shutdown(self) -> None:
        """Terminate all worker processes."""
        self._reaper_stop_event.set()
        self._recycle_queue.put(None)
        with self._cond:
            if self._is_shutdown:
                return
            self._is_shutdown = True
            log.info("Shutting down %s pool (%d workers)...", self.worker_name, len(self.workers))
            workers_to_kill = list(self.workers)
            # Before kill(), and before dropping this lock. An in-flight
            # execute that finds a dead child must not respawn.
            for w in workers_to_kill:
                w._shutting_down = True
            self.workers.clear()
            self._idle.clear()
            self._leased.clear()
            self._worker_last_active.clear()
            self._cond.notify_all()
        # Reaping/killing worker processes can take seconds (wait + drain join).
        # Perform outside the pool lock so waiting threads or release callbacks
        # do not block on child process termination.
        for w in workers_to_kill:
            w.kill()
