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

Child stdio framing (``RestrictedUnpickler``, ``run_worker_stdio_loop``,
``set_pdeathsig``) is defined in ``worker_stdio`` and re-exported here.
"""

from __future__ import annotations

import contextlib
import enum
import logging
import os
import queue
import subprocess
import sys
import threading
import time
from collections import OrderedDict
from typing import IO, TYPE_CHECKING, Any, Generic, TypeVar, cast

if TYPE_CHECKING:
    from collections.abc import Callable, Generator

from compute_service.worker_stdio import (
    RestrictedUnpickler,
    run_worker_stdio_loop,
    set_pdeathsig,
    unpack_restricted_pickle_frame,
)
from plugin.framework.worker_pool import BackgroundHandle, StderrTail, get_subprocess_creationflags, run_in_background, start_stderr_drain
from plugin.scripting.ipc import DEFAULT_MAX_PAYLOAD_BYTES, IpcFrameError, IpcPartialFrameTimeout, read_pickle_frame_with_timeout, write_pickle_frame_with_timeout
from plugin.scripting.sandbox import optimize_popen_pipes, scrub_subprocess_env

__all__ = [
    "BaseProcessPool",
    "BaseProcessWorker",
    "PoolSingleton",
    "RestrictedUnpickler",
    "remaining_sec",
    "resolve_override",
    "run_worker_stdio_loop",
    "set_pdeathsig",
    "unpack_restricted_pickle_frame",
]

log = logging.getLogger("compute_service.worker")

_SPAWN_READY_TIMEOUT_SEC = 15.0
_STDERR_SNIPPET = 500
# Reap wait and the stderr-drain join are short: the child is already
# SIGKILL'd, and a drain whose pipe never reaches EOF must not stall shutdown.
_REAP_WAIT_SEC = 1.0
_STDERR_DRAIN_JOIN_SEC = 0.2
# int() of a budget under one second is 0, so timeouts and leftover drain
# budgets never report or wait on a zero deadline.
_MIN_BUDGET_SEC = 0.01

_PoolT = TypeVar("_PoolT", bound="BaseProcessPool")
_ValT = TypeVar("_ValT")


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




def remaining_sec(deadline: float, *, floor: float = _MIN_BUDGET_SEC) -> float:
    """Return remaining seconds until *deadline*, bounded below by *floor*."""
    return max(floor, deadline - time.monotonic())


class _Deadline:
    """One clock for spawn, the stdin write, and the stdout read.

    ``left()`` floors at ``_MIN_BUDGET_SEC`` so a select or read never sees
    0. ``expired()`` is the raw budget. Flooring before this object existed
    made a deadline that had already passed look like 0.01s, which spawned
    a child and SIGKILL'd it when the handshake could not finish.
    """

    __slots__: tuple[str, ...] = ("budget_sec", "_end")
    budget_sec: float
    _end: float

    def __init__(self, timeout_sec: float) -> None:
        self.budget_sec = float(timeout_sec)
        self._end = time.monotonic() + self.budget_sec

    def expired(self) -> bool:
        """True when the caller passed a spent budget, or the clock has passed it."""
        return self.budget_sec <= 0 or time.monotonic() >= self._end

    def left(self) -> float:
        """Seconds still usable for a pipe wait, never below ``_MIN_BUDGET_SEC``."""
        if self.expired():
            return _MIN_BUDGET_SEC
        return max(_MIN_BUDGET_SEC, self._end - time.monotonic())


class _DrainState(enum.Enum):
    """Late-frame drain. ``_start_late_drain`` writes it; ``defer_release`` consumes it.

    DRAINING is set before the drain thread starts. ``defer_release`` turns
    DRAINING into RELEASE_WAIT and stores the release callback.
    ``_complete_drain`` runs that callback and sets IDLE, or sets DRAINED
    when release has not asked yet. ``defer_release`` turns DRAINED into
    IDLE and returns false so the caller releases immediately. A thread
    that never starts finishes the drain itself: DRAINING becomes DRAINED,
    and RELEASE_WAIT runs the stored callback then becomes IDLE. Otherwise
    the slot would wait on a drain that cannot finish.
    """

    IDLE = "idle"
    DRAINING = "draining"
    RELEASE_WAIT = "release_wait"
    DRAINED = "drained"


class _LateAction(enum.Enum):
    """What to do with the child after a failed request.

    DRAIN is a clean response-read timeout: vision reads one late frame,
    formula kills. KILL is a desynchronized pipe (stdin timeout, partial
    frame, crash, empty response). NONE is shutdown, or a child that
    never became live.
    """

    DRAIN = "drain"
    KILL = "kill"
    NONE = "none"


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

    def __init__(self, worker_id: int, script_path: str, worker_name: str = "Worker", *, max_payload_bytes: int = DEFAULT_MAX_PAYLOAD_BYTES, recover_on_timeout: bool = False, on_process_exit: Callable[[int], None] | None = None) -> None:
        self.worker_id = worker_id
        self.script_path = script_path
        self.worker_name = worker_name
        self.max_payload_bytes = max_payload_bytes
        self.recover_on_timeout = recover_on_timeout
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
        # shutdown calls request_shutdown() before kill(). execute and respawn
        # read the flag without self.lock: kill takes _lifecycle_lock, not the
        # execute lock, so a flag under self.lock would stay invisible until
        # the cell finished. The write is under _lifecycle_lock so adopt either
        # sees shutdown or publishes a child that kill() still reaps.
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

    def _log_spawn_failure(self, message: str) -> None:
        """Log *message* with the child's stderr tail."""
        snippet = self._stderr_snippet()
        extra = f" stderr={snippet!r}" if snippet else " stderr=<empty>"
        log.error("%s%s", message, extra)

    def _reap_previous_process(self, *, only: subprocess.Popen[bytes] | None = None) -> None:
        """Wait on the Popen ``respawn`` is about to replace.

        ``poll()`` reaps an already-dead child so the next ``respawn`` does
        not leave a zombie. Kill first only when it is still running, so a
        reused pid is not signaled.

        *only* is the child a late drain snapshotted. When ``self.process``
        is already a replacement, return without signaling it. ``None``
        reaps whatever is published now.
        """
        with self._lifecycle_lock:
            previous = self.process
            if only is not None and previous is not only:
                return
            drain = self._stderr_drain
            pid = previous.pid if previous is not None else None
            self.process = None
            self._stderr_drain = None
            if previous is not None:
                self._kill_and_wait(previous, pid, context="")
            # After wait(), the pid is reaped. Tell the pool before the next
            # Popen can reuse it, or a lookup can still treat the session as live.
            if pid is not None and self.on_process_exit is not None:
                try:
                    self.on_process_exit(pid)
                except Exception:
                    log.exception("%s #%d process-exit callback failed for pid=%s", self.worker_name, self.worker_id, pid)
        if drain is not None:
            drain.join(timeout=_STDERR_DRAIN_JOIN_SEC)

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
            # The drain install takes the lock again. A kill in the gap after
            # adopt has already cleared this child; attaching a drain then
            # would point _stderr_drain at a process this worker no longer owns.
            if not self._adopt_spawned_process(proc):
                self._discard_unadopted_process(proc)
                return
            optimize_popen_pipes(proc)
            if not self._install_stderr_drain(proc):
                self.kill()
                return
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
            # Only status "ready" counts. EOF or any other dict would mark the
            # worker idle, and the next execute would wait out the full
            # timeout before EMPTY_RESPONSE.
            if not isinstance(ready_data, dict) or ready_data.get("status") != "ready":
                status = ready_data.get("status") if isinstance(ready_data, dict) else None
                self._log_spawn_failure(f"{self.worker_name} #{self.worker_id} spawn handshake was not ready (status={status!r})")
                self.kill()
                return
            log.info("%s #%d spawned (pid=%s, status=%s)", self.worker_name, self.worker_id, ready_data.get("pid", proc.pid), ready_data.get("status"))
            self.tasks_executed = 0
        except subprocess.TimeoutExpired:
            # Handshake hang: child may still be importing, or stdout was not pickle.
            spawned = self.process
            rc = spawned.poll() if spawned is not None else None
            self._log_spawn_failure(f"{self.worker_name} #{self.worker_id} spawn handshake timed out (returncode={rc})")
            self.kill()
        except Exception as exc:
            self._log_spawn_failure(f"Failed to spawn {self.worker_name} #{self.worker_id}: {exc}")
            self.kill()


    def _adopt_spawned_process(self, proc: subprocess.Popen[bytes]) -> bool:
        """Publish *proc*, or refuse when shutdown already won.

        Assignment and ``_shutting_down`` share ``_lifecycle_lock``, which
        ``kill()`` holds. Shutdown either sees this child, or this method
        refuses and the caller kills the unpublished ``Popen``.
        """
        with self._lifecycle_lock:
            if self._shutting_down:
                return False
            self.process = proc
            return True

    def _install_stderr_drain(self, proc: subprocess.Popen[bytes]) -> bool:
        """Start the stderr drain only while *proc* is still the published child.

        ``start_stderr_drain`` runs under ``_lifecycle_lock`` after the same
        checks as adopt. Shutdown or a reap in the gap after adopt either
        still owns this child (and this method refuses it) or has already
        cleared ``self.process``.
        """
        with self._lifecycle_lock:
            if self._shutting_down or self.process is not proc:
                return False
            self._stderr_drain = start_stderr_drain(proc.stderr, name=f"{self.worker_name}-stderr-{self.worker_id}")
            return True

    def _kill_and_wait(self, proc: subprocess.Popen[bytes], pid: int | None, *, context: str) -> None:
        """SIGKILL *proc* only while ``poll()`` says it is running, then wait.

        After ``wait()`` the pid can be reused and must not be signaled.
        *context* is ``""`` for a published child and ``"unadopted "`` for
        one shutdown refused before ``self.process`` was assigned.
        """
        if proc.poll() is None:
            try:
                proc.kill()
            except Exception:
                log.debug("%s #%d kill of %spid=%s failed", self.worker_name, self.worker_id, context, pid, exc_info=True)
        try:
            proc.wait(timeout=_REAP_WAIT_SEC)
        except Exception:
            log.debug("%s #%d wait for %spid=%s failed", self.worker_name, self.worker_id, context, pid, exc_info=True)

    def _discard_unadopted_process(self, proc: subprocess.Popen[bytes]) -> None:
        """Kill a child that was never assigned to ``self.process``."""
        self._kill_and_wait(proc, proc.pid, context="unadopted ")

    def is_alive(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def request_shutdown(self) -> None:
        """Publish shutdown so execute and respawn will not start a child.

        The flag shares ``_lifecycle_lock`` with process publish. Callers
        must not hold the pool condition: reap's ``on_process_exit`` takes
        that condition while this lock is held.
        """
        with self._lifecycle_lock:
            self._shutting_down = True

    def kill(self) -> None:
        """Terminate the child using the same rules as spawn's reap."""
        self._reap_previous_process()

    def _kill_snapshotted(self, proc: subprocess.Popen[bytes] | None) -> None:
        """Reap *proc* only while it is still this worker's child.

        A late drain follows the Popen it snapshotted, not whatever
        ``self.process`` is when the read finishes. Killing the current
        child would SIGKILL a replacement if one was published after the
        snapshot. ``None`` means there was no child to reap.
        """
        if proc is None:
            return
        self._reap_previous_process(only=proc)

    def _fail_request(
        self,
        code: str,
        msg: str,
        *,
        budget_sec: float,
        pid: int | None,
        action: _LateAction = _LateAction.KILL,
        drain_timeout_sec: float | None = None,
    ) -> dict[str, Any]:
        """Build an error dict, then drain, kill, or leave the child.

        ``DRAIN`` is a response-read timeout: vision reads one late frame,
        formula kills. A stdin write timeout and a partial frame are
        ``KILL``: the pipe is desynchronized, so the next read would not
        be that frame. ``NONE`` is shutdown or a child that never became live.
        """
        if code != "SERVICE_SHUTDOWN":
            # Timeouts and failed attempts count so a slot that keeps dying
            # still reaches max_tasks. Shutdown is not a task. An oversized
            # frame never gets here: no bytes were written, and the child stays.
            self.tasks_executed += 1
        snippet = self._stderr_snippet()
        if snippet:
            msg = f"{msg}\n{snippet}"
        if action is _LateAction.DRAIN:
            if self.recover_on_timeout:
                # Late drain gets its own budget. A leftover of ~0.01s after
                # queue wait would SIGKILL a worker that was about to answer.
                # When the caller omits it, use the original request budget,
                # not time left after spawn.
                eff_drain = budget_sec if drain_timeout_sec is None else max(_MIN_BUDGET_SEC, float(drain_timeout_sec))
                log.warning("%s execution timed out after %.1fs on worker #%d; draining late frame from pid=%s", self.worker_name, budget_sec, self.worker_id, pid)
                self._start_late_drain(eff_drain)
            else:
                log.warning("%s execution timed out after %.1fs on worker #%d; terminating pid=%s", self.worker_name, budget_sec, self.worker_id, pid)
                self.kill()
        elif action is _LateAction.KILL:
            self.kill()
        res: dict[str, Any] = {"status": "error", "code": code, "error": msg}
        if code in ("EXECUTION_TIMEOUT", "WORKER_CRASHED"):
            res["message"] = msg
        return res

    def _timeout_message(self, deadline: _Deadline) -> str:
        """Error text for a spent budget. Two decimals: ``int()`` of a sub-second budget is 0."""
        return f"Execution exceeded maximum timeout of {max(0.0, deadline.budget_sec):.2f} seconds."

    def _ensure_live_process(self, deadline: _Deadline) -> tuple[subprocess.Popen[bytes], IO[bytes], IO[bytes]] | dict[str, Any]:
        """Return a live child and its stdio pipes, or an error dict.

        Caller holds ``self.lock``. ``kill()`` during shutdown sets
        ``self.process`` to None; reading ``.pid`` or ``.stdin`` on that
        would be AttributeError. A dead child during shutdown is
        ``SERVICE_SHUTDOWN``, not a new process. The handshake uses the
        remaining request budget, not a fresh spawn timeout. A budget that
        is already spent does not spawn: the 0.01s floor is only for pipe
        waits, and a handshake cannot finish in that long.
        """
        proc = self.process

        def _pid() -> int | None:
            return proc.pid if proc is not None else None

        if proc is None or proc.poll() is not None:
            if self._shutting_down:
                return self._fail_request("SERVICE_SHUTDOWN", f"{self.worker_name} #{self.worker_id} is shutting down.", budget_sec=deadline.budget_sec, pid=_pid(), action=_LateAction.NONE)
            if deadline.expired():
                return self._fail_request("EXECUTION_TIMEOUT", self._timeout_message(deadline), budget_sec=deadline.budget_sec, pid=_pid(), action=_LateAction.NONE)
            spawn_budget = min(_SPAWN_READY_TIMEOUT_SEC, deadline.left())
            self.respawn(timeout_sec=spawn_budget)
            proc = self.process
            if proc is None or proc.poll() is not None:
                return self._fail_request("WORKER_SPAWN_FAILED", f"{self.worker_name} #{self.worker_id} could not be started.", budget_sec=deadline.budget_sec, pid=_pid(), action=_LateAction.NONE)

        stdin = proc.stdin
        stdout = proc.stdout
        if stdin is None or stdout is None:
            return self._fail_request("WORKER_SPAWN_FAILED", f"{self.worker_name} #{self.worker_id} has no stdio pipes.", budget_sec=deadline.budget_sec, pid=_pid(), action=_LateAction.NONE)
        return proc, stdin, stdout

    def execute(self, payload: dict[str, Any], timeout_sec: float, drain_timeout_sec: float | None = None) -> dict[str, Any]:
        """Send request to worker process and await response with timeout."""
        # One deadline covers spawn, the stdin write, and the stdout read.
        # It is created under self.lock so waiting for that lock is not
        # part of the budget. Do not floor timeout_sec before this: a
        # caller that already passed 0 would still spawn.
        with self.lock:
            deadline = _Deadline(timeout_sec)
            if deadline.expired():
                current = self.process
                return self._fail_request(
                    "EXECUTION_TIMEOUT",
                    self._timeout_message(deadline),
                    budget_sec=deadline.budget_sec,
                    pid=current.pid if current is not None else None,
                    action=_LateAction.NONE,
                )
            ensured = self._ensure_live_process(deadline)
            if isinstance(ensured, dict):
                return ensured
            proc, stdin, stdout = ensured

            def _pid() -> int | None:
                return proc.pid

            try:
                # The write shares the request deadline. It holds self.lock, so a
                # child that stopped reading stdin would never return and the
                # slot would stay leased. A partial frame is desynchronized,
                # so the child is killed instead of late-drained.
                write_pickle_frame_with_timeout(
                    stdin,
                    payload,
                    deadline.left(),
                    max_payload_bytes=self.max_payload_bytes,
                    is_alive=self.is_alive,
                )
            except IpcFrameError as exc:
                # Raised before any byte is written. The child is still the
                # same kernel; killing it would drop every shared session.
                return {"status": "error", "code": "PAYLOAD_TOO_LARGE", "error": str(exc)}
            except subprocess.TimeoutExpired:
                return self._fail_request(
                    "EXECUTION_TIMEOUT",
                    self._timeout_message(deadline),
                    budget_sec=deadline.budget_sec,
                    pid=_pid(),
                    action=_LateAction.KILL,
                )
            except (BrokenPipeError, OSError) as exc:
                return self._fail_request("WORKER_PIPE_BROKEN", f"Failed to send request to {self.worker_name} #{self.worker_id}: {exc}", budget_sec=deadline.budget_sec, pid=_pid())

            try:
                resp = read_pickle_frame_with_timeout(
                    stdout,
                    deadline.left(),
                    is_alive=self.is_alive,
                    max_payload_bytes=self.max_payload_bytes,
                    unpacker=unpack_restricted_pickle_frame,
                )
            except subprocess.TimeoutExpired:
                return self._fail_request(
                    "EXECUTION_TIMEOUT",
                    self._timeout_message(deadline),
                    budget_sec=deadline.budget_sec,
                    pid=_pid(),
                    action=_LateAction.DRAIN,
                    drain_timeout_sec=drain_timeout_sec,
                )
            except IpcPartialFrameTimeout:
                # A deadline after the first byte is a timeout, not a crash.
                # KILL instead of DRAIN: the next read would consume the rest
                # of this frame as a new response.
                return self._fail_request(
                    "EXECUTION_TIMEOUT",
                    self._timeout_message(deadline),
                    budget_sec=deadline.budget_sec,
                    pid=_pid(),
                    action=_LateAction.KILL,
                )
            except Exception as exc:
                return self._fail_request("WORKER_CRASHED", f"{self.worker_name} error: {exc}", budget_sec=deadline.budget_sec, pid=_pid())
            if resp is None or not isinstance(resp, dict):
                return self._fail_request("EMPTY_RESPONSE", f"No response returned from {self.worker_name}.", budget_sec=deadline.budget_sec, pid=_pid())
            self.tasks_executed += 1
            return resp

    def _start_late_drain(self, timeout_sec: float) -> None:
        """Read the one frame a timed-out vision call will still write.

        The same budget applies again. A call that finishes shortly after the
        client gave up keeps the process. A call that never returns is killed
        so the slot can respawn.
        """
        # Snapshot process and its stdout while self.lock is still held by execute.
        # The drain thread must reap this Popen, not a later replacement.
        proc = self.process
        stdout = proc.stdout if proc is not None else None
        with self._drain_lock:
            self._drain_state = _DrainState.DRAINING
            self._release_cb = None
        # A long stdout read that release_worker waits on. dedicated=True so
        # it does not occupy a slot in the shared background pool.
        # Thread.start() raises RuntimeError ("can't start new thread") after
        # the state is already DRAINING. Leaving it there makes defer_release
        # park the slot on a drain that never runs. KeyboardInterrupt is not
        # an Exception; it still has to abandon, then propagate.
        try:
            run_in_background(
                self._drain_late_response,
                proc,
                stdout,
                timeout_sec,
                name=f"{self.worker_name}-drain-{self.worker_id}",
                dedicated=True,
            )
        except BaseException as exc:
            if isinstance(exc, Exception):
                log.exception("%s could not start late-frame drain on worker #%d", self.worker_name, self.worker_id)
            self._abandon_unstarted_drain(proc)
            if not isinstance(exc, Exception):
                raise

    def _abandon_unstarted_drain(self, proc: subprocess.Popen[bytes] | None) -> None:
        """Mark a drain finished when its thread never started, and reap *proc*.

        What was wrong: DRAINING was published before ``Thread.start()``.
        ``RuntimeError`` from start left ``_release_cb`` unset, so
        ``defer_release`` moved the slot to RELEASE_WAIT and waited forever.
        DRAINED lets the caller release now. If release has already parked
        on RELEASE_WAIT, that stored callback still has to run or the slot
        stays leased. The unread frame is still on the pipe, so this child
        is killed instead of re-idled. Kill before the callback, matching
        ``_complete_drain``: the slot must not be released while this child
        is still the published process.
        """
        with self._drain_lock:
            callback: Callable[[], None] | None = None
            if self._drain_state == _DrainState.DRAINING:
                self._drain_state = _DrainState.DRAINED
                self._release_cb = None
            elif self._drain_state == _DrainState.RELEASE_WAIT:
                callback = self._release_cb
                self._release_cb = None
                self._drain_state = _DrainState.IDLE
        self._kill_snapshotted(proc)
        if callback is not None:
            callback()

    def _drain_late_response(self, proc: subprocess.Popen[bytes] | None, stdout: Any, timeout_sec: float) -> None:
        try:
            resp: Any = None
            # Liveness is the snapshotted child. self.is_alive() follows
            # self.process, so a replacement would look alive and then be
            # killed when this read failed.
            if stdout is not None and proc is not None and self.process is proc and proc.poll() is None:
                child = proc

                def _still_this_child() -> bool:
                    return self.process is child and child.poll() is None

                resp = read_pickle_frame_with_timeout(
                    stdout,
                    timeout_sec,
                    is_alive=_still_this_child,
                    max_payload_bytes=self.max_payload_bytes,
                    unpacker=unpack_restricted_pickle_frame,
                )
            if not isinstance(resp, dict):
                # EOF or a non-frame: the pipe cannot take another request.
                self._kill_snapshotted(proc)
        except subprocess.TimeoutExpired:
            log.warning("%s late frame exceeded %.1fs on worker #%d; terminating pid=%s", self.worker_name, timeout_sec, self.worker_id, proc.pid if proc is not None else None)
            self._kill_snapshotted(proc)
        except Exception:
            log.exception("%s late-frame drain failed on worker #%d", self.worker_name, self.worker_id)
            self._kill_snapshotted(proc)
        except BaseException:
            # KeyboardInterrupt / SystemExit skip Exception. An interrupted
            # read may be a partial frame, so the child is killed instead of
            # re-idled. _complete_drain still runs: a RELEASE_WAIT callback
            # is what returns the slot.
            self._kill_snapshotted(proc)
            raise
        finally:
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
    _reaper_handles: list[BackgroundHandle]

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
        self._reaper_handles = []
        self._recycle_queue: queue.Queue[BaseProcessWorker | None] = queue.Queue()
        # dedicated=True: this loop runs until shutdown and must not occupy
        # a slot in the shared background pool. It is a daemon and is not joined.
        self._recycle_thread: BackgroundHandle = run_in_background(
            self._recycle_loop,
            name=f"{self.worker_name}-recycle-loop",
            dedicated=True,
        )

        if self.num_workers > 0:
            # Workers spawn one at a time, each waiting on its ready handshake
            # (up to _SPAWN_READY_TIMEOUT_SEC). Spawning them in parallel would
            # cut startup roughly with the worker count. Left for later.
            for i in range(self.num_workers):
                w = BaseProcessWorker(i + 1, script_path=script_path, worker_name=worker_name, max_payload_bytes=max_payload_bytes, recover_on_timeout=recover_on_timeout, on_process_exit=on_process_exit)
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

    def _start_reaper(self, name: str, interval: float, fn: Callable[[], None]) -> BackgroundHandle:
        def _loop() -> None:
            # Wait on the stop event so pool shutdown returns immediately.
            # One failed tick must not kill this daemon, or idle workers and
            # shared sessions are never evicted again. The handler stays
            # inside the loop: run_in_background would otherwise log the
            # exception and end the thread.
            while not self._reaper_stop_event.wait(interval):
                if self._is_shutdown:
                    continue
                try:
                    fn()
                except Exception:
                    log.exception("Reaper %s tick failed", name)

        # dedicated=True: this loop runs until shutdown and must not occupy
        # a slot in the shared background pool. Daemon, and not joined;
        # shutdown sets the stop event, which wakes the wait. A bare
        # threading.Thread is not tagged for the UNO thread guard.
        handle = run_in_background(_loop, name=name, dedicated=True)
        self._reaper_handles.append(handle)
        return handle

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
            # Drop dead pids before the TTL pass so lease_any cannot pop them.
            # A dead pid is not idle: the next lease performs the handshake.
            self._prune_dead_idle_unlocked()
            for w in list(self._idle):
                if self._skip_idle_evict(w):
                    continue
                last_active = self._worker_last_active.get(w, now)
                if now - last_active >= self.idle_worker_ttl_sec:
                    stale.append(w)
            for w in stale:
                self._idle.pop(w, None)
                # Same as _prune_dead_idle_unlocked. The next idle writes a
                # new stamp; leaving the old one would look already expired
                # if this slot were re-idled without that write.
                self._worker_last_active.pop(w, None)
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

    def _prune_dead_idle_unlocked(self) -> None:
        """Drop dead pids from idle. Caller holds self._cond.

        A dead pid is not idle. The next lease claims that slot as cold
        and respawns it. The last-active stamp goes with the idle entry;
        the next successful idle writes a new one.
        """
        for worker in list(self._idle):
            if not worker.is_alive():
                self._idle.pop(worker, None)
                self._worker_last_active.pop(worker, None)

    def _pick_idle_worker(self) -> BaseProcessWorker | None:
        """Pop one protocol-ready idle worker. Caller must hold self._cond.

        A dead pid is removed from idle rather than returned. The caller
        claims that slot as cold and respawns it (handshake, then leased).
        """
        self._prune_dead_idle_unlocked()
        if not self._idle:
            return None
        worker, _unused = self._idle.popitem(last=False)
        return worker

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
        """Persistent worker thread for recycling child processes without thread-exit PDEATHSIG races.

        One failed recycle must not kill this daemon, or later items stay
        in ``_leased`` and ``lease_any`` waits forever. ``kill`` / ``respawn``
        failures still release that one slot in ``_recycle_worker_async``'s
        ``finally``.
        """
        while True:
            worker = self._recycle_queue.get()
            if worker is None:
                break
            try:
                self._recycle_worker_async(worker)
            except Exception:
                log.exception("Recycle of %s #%s failed", self.worker_name, worker.worker_id)

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
            # Drop the pool sets before releasing this lock so lease_any
            # cannot hand out a slot after shutdown has started.
            self.workers.clear()
            self._idle.clear()
            self._leased.clear()
            self._worker_last_active.clear()
            self._cond.notify_all()
        # request_shutdown takes _lifecycle_lock. Reap's on_process_exit
        # takes _cond while that lock is held, so this stays outside _cond.
        # Adopt then either sees the flag or publishes a child kill() reaps.
        for w in workers_to_kill:
            w.request_shutdown()
        # Reaping/killing worker processes can take seconds (wait + drain join).
        # Perform outside the pool lock so waiting threads or release callbacks
        # do not block on child process termination.
        for w in workers_to_kill:
            w.kill()
