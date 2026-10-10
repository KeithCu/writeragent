# WriterAgent - Python Compute Service Base Worker & Pool Infrastructure
# Copyright (c) 2026 KeithCu
# SPDX-License-Identifier: GPL-3.0-or-later
"""
Shared subprocess worker loop, worker process wrapper, and process pool supervisor.

Provides:
- High-speed length-prefixed Pickle 5 binary framing over stdio pipes
- Deadline-bounded pickle reads and stdin writes (header + payload)
- Child stderr is a log file, kept to the last 64 KiB after a request, on each
  idle-reaper scan, and when a reap leaves the child alive
- A read timeout, partial frame, or broken pipe kills the child. The next lease respawns it
- Exclusive worker occupancy. Idle means the process completed a handshake or
  a response frame was consumed — a dead pid is not idle
- After max_tasks the slot is killed and left dead. The next lease respawns it

Child stdio framing (``RestrictedUnpickler``, ``run_worker_stdio_loop``,
``set_pdeathsig``) is defined in ``worker_stdio`` and re-exported here.
"""

from __future__ import annotations

import contextlib
import logging
import os
import pickle
import subprocess
import sys
import tempfile
import threading
import time
from collections import OrderedDict
from typing import IO, TYPE_CHECKING, Any, Generic, TypeVar, cast

if TYPE_CHECKING:
    from collections.abc import Callable, Generator

from compute_service.worker_stdio import (
    RestrictedUnpickler,
    run_compute_worker,
    run_worker_stdio_loop,
    set_pdeathsig,
    unpack_restricted_pickle_frame,
)
from plugin.framework.worker_pool import get_subprocess_creationflags, run_in_background
from plugin.scripting.ipc import DEFAULT_MAX_PAYLOAD_BYTES, IpcFrameError, IpcPartialFrameTimeout, read_pickle_frame_with_timeout, write_pickle_frame_with_timeout
from plugin.scripting.sandbox import optimize_popen_pipes, scrub_subprocess_env

__all__ = [
    "BaseProcessPool",
    "BaseProcessWorker",
    "PoolSingleton",
    "RestrictedUnpickler",
    "error_dict",
    "resolve_override",
    "run_compute_worker",
    "run_worker_stdio_loop",
    "set_pdeathsig",
    "unpack_restricted_pickle_frame",
]

log = logging.getLogger("compute_service.worker")

_SPAWN_READY_TIMEOUT_SEC = 15.0
_STDERR_SNIPPET = 500
# One child appends for its whole life. The host keeps the tail; the
# snippet reader only uses the last _STDERR_SNIPPET characters.
_STDERR_LOG_CAP = 64 * 1024
# The child is already SIGKILL'd. wait() must not stall shutdown.
_REAP_WAIT_SEC = 1.0
# select must not be handed 0 near the end of a call that already started.
# A new spawn or lease is refused below _MIN_REQUEST_SEC instead.
_PIPE_WAIT_FLOOR = 0.01
_MIN_REQUEST_SEC = 1.0

_PoolT = TypeVar("_PoolT", bound="BaseProcessPool")
_ValT = TypeVar("_ValT")


def resolve_override(override: _ValT | None, default: _ValT) -> _ValT:
    """Return *override* if not None, else *default*."""
    return default if override is None else override


class _OmitId:
    """Marker so ``error_dict`` can tell a missing id from ``id=None``."""


_OMIT_ID = _OmitId()


def error_dict(code: str, error: str, *, req_id: Any = _OMIT_ID, message: str | None = None) -> dict[str, Any]:
    """Build ``{"status": "error", "code", "error"}``.

    ``id`` is set when the caller passes ``req_id``, including ``None``.
    ``message`` is set only when passed. Pool execute methods pass ``req_id``.
    The worker's ``_fail_request`` passes ``message`` for the two codes
    callers already read from both keys.
    """
    res: dict[str, Any] = {"status": "error", "code": code, "error": error}
    if req_id is not _OMIT_ID:
        res["id"] = req_id
    if message is not None:
        res["message"] = message
    return res


# count_task, mirror_message. ``kill`` stays a call-site choice:
# EXECUTION_TIMEOUT is kill=False when no child was started, and kill=True
# after a desynchronized pipe. Unknown codes count the task and do not
# copy the text onto ``message``.
_FAIL_POLICY: dict[str, tuple[bool, bool]] = {
    "SERVICE_SHUTDOWN": (False, False),
    "EXECUTION_TIMEOUT": (True, True),
    "WORKER_CRASHED": (True, True),
}
_DEFAULT_FAIL_POLICY: tuple[bool, bool] = (True, False)


def _fail_policy(code: str) -> tuple[bool, bool]:
    """Return ``(count_task, mirror_message)`` for a worker error *code*."""
    return _FAIL_POLICY.get(code, _DEFAULT_FAIL_POLICY)


class PoolSingleton(Generic[_PoolT]):
    """Thread-safe global singleton holder for a BaseProcessPool subclass."""

    _cv: threading.Condition
    _pool: _PoolT | None
    _closed: bool
    _building: bool
    _build_id: int
    _epoch: int

    def __init__(self) -> None:
        # A plain Lock, not the Condition default RLock. Nothing here re-enters.
        self._cv = threading.Condition(threading.Lock())
        self._pool = None
        self._closed = False
        self._building = False
        self._build_id = 0
        self._epoch = 0

    def get(self, factory: Callable[[], _PoolT]) -> _PoolT:
        """Return the pool. ``factory`` runs outside this lock.

        What was wrong: ``factory`` ran while the lock was held, and it
        spawns every child before returning. ``shutdown`` blocked on that
        lock for the whole spawn. One caller builds; the others wait.
        A shutdown that lands during the build discards that pool instead
        of installing it.
        """
        with self._cv:
            while self._building:
                if self._closed:
                    raise RuntimeError("Compute pool is shut down.")
                self._cv.wait()
            if self._closed:
                raise RuntimeError("Compute pool is shut down.")
            if self._pool is not None:
                return self._pool
            self._build_id += 1
            self._building = True
            epoch = self._epoch
        created: _PoolT | None = None
        stale = True
        published: _PoolT | None = None
        closed = False
        try:
            created = factory()
            with self._cv:
                # epoch moves on every shutdown, including a non-permanent
                # one, so this build cannot appear after shutdown returned.
                stale = self._closed or self._epoch != epoch or self._pool is not None
                if not stale:
                    self._pool = created
                published = self._pool
                closed = self._closed
            if stale:
                created.shutdown()
        finally:
            with self._cv:
                self._building = False
                self._cv.notify_all()
        if not stale and created is not None:
            return created
        if published is not None and not closed:
            return published
        raise RuntimeError("Compute pool is shut down.")

    def shutdown(self, *, permanent: bool = False) -> None:
        """Drop the pool. *permanent* makes a later ``get`` raise.

        Tests and a restarted process call shutdown without *permanent* so
        the next ``get`` builds a new pool. The server process passes
        *permanent* on the way out so an abandoned handler cannot spawn
        children after shutdown.

        The pool is reaped outside this lock. A build already inside
        ``factory`` is not installed; this waits until that build has
        discarded its children, and the wait is not held across the reap.
        """
        with self._cv:
            pool = self._pool
            self._pool = None
            self._epoch += 1
            self._closed = permanent
            inflight = self._build_id if self._building else None
            self._cv.notify_all()
        if pool is not None:
            pool.shutdown()
        if inflight is None:
            return
        with self._cv:
            while self._building and self._build_id == inflight:
                self._cv.wait()




class _Deadline:
    """Accept-time budget for a pool, or the worker clock started under its lock.

    ``__init__`` starts at now. ``BaseProcessWorker.execute`` builds that
    under ``self.lock`` so the wait for the lock is not part of the budget.
    ``from_absolute`` keeps the original requested seconds and an end the
    HTTP handler already chose. Formula and vision use that one.

    ``left()`` floors at ``_PIPE_WAIT_FLOOR`` so a select never sees 0.
    ``too_late_to_spawn()`` refuses a requested budget under one second,
    or a longer budget that has already fallen under one second. Flooring
    the whole budget used to turn a spent deadline into 0.01s, which
    spawned a child and SIGKILL'd it. A one-second request still starts:
    that is the minimum ``clamp_timeout_sec`` returns, and the clock moves
    before this check.
    """

    __slots__: tuple[str, ...] = ("budget_sec", "_end")
    budget_sec: float
    _end: float

    def __init__(self, timeout_sec: float) -> None:
        self.budget_sec = float(timeout_sec)
        self._end = time.monotonic() + self.budget_sec

    @classmethod
    def from_absolute(cls, requested_sec: float, end: float) -> _Deadline:
        """Clock whose end is already fixed and whose budget is the original request.

        ``requested_sec`` is not ``end - now``. A one-second request whose
        clock has already moved still starts; rebuilding the budget from
        the time left would refuse it.
        """
        clock = cls.__new__(cls)
        clock.budget_sec = float(requested_sec)
        clock._end = float(end)
        return clock

    def remaining(self) -> float:
        """Seconds until ``_end``. Negative once the clock has passed it."""
        return self._end - time.monotonic()

    def expired(self) -> bool:
        """True when the caller passed a spent budget, or the clock has passed it."""
        return self.budget_sec <= 0 or self.remaining() <= 0

    def too_late_to_spawn(self) -> bool:
        """True when a lease or a dead slot must not start.

        A one-second request may start: it is the minimum budget, and the
        clock has already moved. A longer request with under one second
        left must not. That handshake could not finish and SIGKILL'd the child.
        """
        remaining = self.remaining()
        if self.budget_sec < _MIN_REQUEST_SEC or remaining <= 0:
            return True
        if self.budget_sec <= _MIN_REQUEST_SEC:
            return False
        return remaining < _MIN_REQUEST_SEC

    def child_run_seconds(self) -> float:
        """Seconds for a child that already passed ``too_late_to_spawn``.

        A spent deadline is 0. Any time still left is at least one second.
        ``int`` of a one-second request is 0 once the clock has moved, and
        ``signal.alarm(0)`` cancels the child's alarm. Vision uses the same
        floor so a one-second OCR call is not given a sub-second read.
        """
        remaining = self.remaining()
        if remaining <= 0:
            return 0.0
        if remaining < _MIN_REQUEST_SEC:
            return _MIN_REQUEST_SEC
        return remaining

    def left(self) -> float:
        """Seconds still usable for a pipe wait, never below ``_PIPE_WAIT_FLOOR``."""
        if self.expired():
            return _PIPE_WAIT_FLOOR
        return max(_PIPE_WAIT_FLOOR, self.remaining())


class BaseProcessWorker:
    """Wrapper around one persistent child subprocess communicating via Pickle 5 frames."""

    worker_id: int
    script_path: str
    worker_name: str
    max_payload_bytes: int
    lock: threading.Lock
    _lifecycle_lock: threading.Lock
    tasks_executed: int
    on_process_exit: Callable[[BaseProcessWorker, int], None] | None
    _shutting_down: bool

    def __init__(self, worker_id: int, script_path: str, worker_name: str = "Worker", *, max_payload_bytes: int = DEFAULT_MAX_PAYLOAD_BYTES, on_process_exit: Callable[[BaseProcessWorker, int], None] | None = None) -> None:
        self.worker_id = worker_id
        self.script_path = script_path
        self.worker_name = worker_name
        self.max_payload_bytes = max_payload_bytes
        # The callback receives this wrapper and the reaped pid. It runs
        # only after wait() has reaped the child, so a pid that is still
        # alive is not reported as exited. Formula matches the wrapper:
        # is_alive()'s poll() can free the pid before this runs, and
        # another slot may already be using that number.
        self.on_process_exit = on_process_exit
        self.process: subprocess.Popen[bytes] | None = None
        self.lock = threading.Lock()
        # Serializes kill/reap. Distinct from ``lock``, which execute holds
        # across spawn, so reap must not take ``lock``.
        self._lifecycle_lock = threading.Lock()
        self.tasks_executed = 0
        # Path of the current child's stderr file. Unlinked when that child
        # is reaped. None between children.
        self._stderr_path: str | None = None
        # shutdown calls request_shutdown() before kill(). execute and respawn
        # read the flag without self.lock: kill takes _lifecycle_lock, not the
        # execute lock, so a flag under self.lock would stay invisible until
        # the cell finished. The write is under _lifecycle_lock so adopt either
        # sees shutdown or publishes a child that kill() still reaps.
        self._shutting_down = False
        self.respawn()

    def _open_stderr_log(self) -> IO[bytes]:
        """Open a stderr file for the child about to be spawned.

        A pipe would need a drain thread so the kernel buffer cannot fill
        and stall the handshake. The file is read only when spawn or a
        request fails, then unlinked when the child is reaped. ``_cap_stderr_log``
        keeps the last ``_STDERR_LOG_CAP`` bytes after a request, on each
        idle-reaper scan, and when a reap leaves the child alive.

        Always a new file. Reap clears ``_stderr_path`` before this runs,
        so there is no live path to reuse. Does not publish ``_stderr_path``.
        ``kill()`` unlinks only that published path, so a shutdown during
        ``Popen`` cannot delete this file before the child inherits the fd.
        ``respawn`` publishes it after ``Popen`` returns.
        """
        fd, path = tempfile.mkstemp(prefix=f"wa-compute-w{self.worker_id}-", suffix=".stderr")
        os.close(fd)
        return open(path, "ab", buffering=0)

    def _publish_stderr_path(self, path: str) -> bool:
        """Publish *path* unless shutdown already won.

        Call after ``Popen`` has inherited the fd. False means the caller
        still owns *path* and must unlink it.
        """
        with self._lifecycle_lock:
            if self._shutting_down:
                return False
            self._stderr_path = path
            return True

    def _safe_unlink(self, path: str) -> None:
        """Remove *path*. A missing file is normal when shutdown already unlinked it."""
        try:
            os.unlink(path)
        except OSError:
            log.debug("%s #%d could not remove stderr log %s", self.worker_name, self.worker_id, path, exc_info=True)

    def _unlink_stderr_file(self, path: str) -> None:
        """Remove a stderr file that was never published on ``_stderr_path``."""
        self._safe_unlink(path)

    def _close_stderr_log(self) -> None:
        """Unlink the current stderr file. Caller holds ``_lifecycle_lock``."""
        path = self._stderr_path
        self._stderr_path = None
        if path is None:
            return
        self._safe_unlink(path)

    def _cap_stderr_log(self) -> None:
        """Keep the last ``_STDERR_LOG_CAP`` bytes of this slot's stderr file.

        The child inherited an ``O_APPEND`` fd, so the next write goes to the
        new end after a truncate. A write that lands during the rewrite can
        drop a few bytes; this file is only a failure tail.
        """
        path = self._stderr_path
        if not path:
            return
        try:
            size = os.path.getsize(path)
            if size <= _STDERR_LOG_CAP:
                return
            with open(path, "rb") as handle:
                handle.seek(size - _STDERR_LOG_CAP)
                tail = handle.read(_STDERR_LOG_CAP)
            # r+b is not O_APPEND. Truncate, then write the tail at offset 0.
            # The child's separate append fd writes at the new end.
            with open(path, "r+b", buffering=0) as handle:
                handle.seek(0)
                handle.truncate(0)
                handle.write(tail)
        except OSError:
            log.debug("%s #%d could not cap stderr log %s", self.worker_name, self.worker_id, path, exc_info=True)

    def _stderr_snippet(self) -> str:
        """Last stderr bytes, decoded. Empty when this slot has no log file."""
        self._cap_stderr_log()
        path = self._stderr_path
        if not path:
            return ""
        try:
            with open(path, "rb") as handle:
                handle.seek(0, os.SEEK_END)
                size = handle.tell()
                # Extra bytes so a multibyte character is not cut in half
                # before the character trim below.
                handle.seek(max(0, size - _STDERR_SNIPPET * 4))
                raw = handle.read()
        except OSError:
            return ""
        text = raw.decode("utf-8", errors="replace").strip()
        if not text:
            return ""
        return text[-_STDERR_SNIPPET:]

    def _log_spawn_failure(self, message: str) -> None:
        """Log *message* with the child's stderr tail."""
        snippet = self._stderr_snippet()
        extra = f" stderr={snippet!r}" if snippet else " stderr=<empty>"
        log.error("%s%s", message, extra)

    def _log_spawn_outcome(self, message: str) -> None:
        """Log *message*. A shutdown-interrupted handshake is not a failed spawn."""
        if self._shutting_down:
            log.info("%s", message)
            return
        self._log_spawn_failure(message)

    def _reap_previous_process(self) -> bool:
        """Wait on the Popen ``respawn`` is about to replace.

        Returns True when the slot is empty or ``wait()`` reaped the child.
        Returns False when the child is still alive after the wait: the slot
        stays on that ``Popen``, and ``on_process_exit`` does not run.

        ``poll()`` reaps an already-dead child so the next ``respawn`` does
        not leave a zombie. Kill first only when it is still running, so a
        reused pid is not signaled.
        """
        with self._lifecycle_lock:
            previous = self.process
            if previous is None:
                self._close_stderr_log()
                return True
            pid = previous.pid
            # What was wrong: wait()'s timeout was swallowed, self.process was
            # already None, and on_process_exit still ran. The formula pool
            # dropped every session on a pid whose SIGKILL had not taken
            # effect (uninterruptible sleep).
            if not self._kill_and_wait(previous, pid, context=""):
                log.warning("%s #%d pid=%s still alive after reap; not reporting exit", self.worker_name, self.worker_id, pid)
                # The file stays with this child. The idle reaper does not
                # see a slot that is neither idle nor leased.
                self._cap_stderr_log()
                return False
            self.process = None
            # After wait(), the pid is reaped. Report this wrapper with it.
            # Matching the pid alone dropped another slot's session when
            # poll() had already freed this pid and the kernel reused it.
            if pid is not None and self.on_process_exit is not None:
                try:
                    self.on_process_exit(self, pid)
                except Exception:
                    log.exception("%s #%d process-exit callback failed for pid=%s", self.worker_name, self.worker_id, pid)
            # Snippet readers run before kill(). Unlink only after the child
            # is reaped so a failure log still sees the tail.
            self._close_stderr_log()
            return True

    def respawn(self, timeout_sec: float = _SPAWN_READY_TIMEOUT_SEC, *, deadline: _Deadline | None = None) -> None:
        """Spawn worker subprocess and await readiness handshake.

        Returns without a child when the pool is stopping, when the previous
        child is still alive after the reap wait, or when *deadline* is
        already too late to start one. Recycle calls this after kill(); the
        flag is what stops that spawn. The check after reap covers a shutdown
        that arrives while the previous child is reaped. The new ``Popen`` is
        published under ``_lifecycle_lock``, which ``kill()`` also holds, so a
        shutdown during spawn still reaps it.

        *deadline* is the caller's request clock. The reap wait is inside that
        clock: budgeting the handshake from the time left before
        ``_reap_previous_process`` let a call run about a second past its
        timeout. ``None`` is startup, which has no request clock.
        """
        if self._shutting_down:
            return
        if not self._reap_previous_process():
            return
        if self._shutting_down:
            return
        # What was wrong: spawn_budget was fixed before this wait, and
        # _REAP_WAIT_SEC can block for a second. A longer request with under
        # a second left still started a child. The pipe-wait floor is not a
        # handshake budget. The handshake timeout is computed after Popen,
        # which can itself spend the clock.
        if deadline is not None and deadline.too_late_to_spawn():
            return
        cmd = [sys.executable, self.script_path]
        stderr_log: IO[bytes] | None = None
        published_stderr = False
        try:
            # Scrub matches the venv host: drop PYTHONHOME / credential-like
            # names so the child does not inherit the parent's secret env.
            # **creationflags kwargs make the type checker treat this as Popen[str].
            # stderr is a file, not a pipe: nothing has to drain it.
            # The path stays unpublished until Popen returns, so kill() during
            # Popen cannot unlink it before the child inherits the fd.
            # A grandchild that inherits this stdout pipe holds the host read
            # open until the request deadline; that timeout kills the child.
            stderr_log = self._open_stderr_log()
            proc = cast(
                "subprocess.Popen[bytes]",
                subprocess.Popen(
                    cmd,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=stderr_log,
                    bufsize=0,
                    text=False,
                    env=scrub_subprocess_env(dict(os.environ)),
                    **get_subprocess_creationflags(),
                ),
            )
            # Do not hold _lifecycle_lock across Popen or the handshake read.
            # kill() takes that lock, and the handshake can block for the
            # spawn budget. Publishing here is after the inherit.
            if not self._publish_stderr_path(stderr_log.name):
                self._discard_unadopted_process(proc)
                if self._stderr_path != stderr_log.name:
                    self._unlink_stderr_file(stderr_log.name)
                return
            published_stderr = True
            if not self._adopt_spawned_process(proc):
                self._discard_unadopted_process(proc)
                # Reap already holds this lock. This path does not.
                with self._lifecycle_lock:
                    self._close_stderr_log()
                return
            optimize_popen_pipes(proc)
            # Popen itself is not on the clock. If it ran past the deadline,
            # kill the child here. A handshake read would use the 0.01s floor
            # and then SIGKILL.
            if deadline is not None and deadline.too_late_to_spawn():
                self.kill()
                return
            if deadline is not None:
                timeout_sec = min(_SPAWN_READY_TIMEOUT_SEC, deadline.left())
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
                self._log_spawn_outcome(f"{self.worker_name} #{self.worker_id} spawn handshake was not ready (status={status!r})")
                self.kill()
                return
            log.info("%s #%d spawned (pid=%s, status=%s)", self.worker_name, self.worker_id, ready_data.get("pid", proc.pid), ready_data.get("status"))
            self.tasks_executed = 0
        except (subprocess.TimeoutExpired, IpcPartialFrameTimeout):
            # No-byte hang is TimeoutExpired. A deadline after the first
            # handshake byte is IpcPartialFrameTimeout (a ConnectionError).
            # That used to be logged as "Failed to spawn". The child is
            # killed either way; the next lease respawns the slot.
            spawned = self.process
            rc = spawned.poll() if spawned is not None else None
            self._log_spawn_outcome(f"{self.worker_name} #{self.worker_id} spawn handshake timed out (returncode={rc})")
            self.kill()
        except Exception as exc:
            self._log_spawn_outcome(f"Failed to spawn {self.worker_name} #{self.worker_id}: {exc}")
            # Popen failed before the path was published. kill() only
            # unlinks _stderr_path, so this file would otherwise leak.
            if stderr_log is not None and not published_stderr and self._stderr_path != stderr_log.name:
                self._unlink_stderr_file(stderr_log.name)
            self.kill()
        finally:
            if stderr_log is not None:
                stderr_log.close()


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

    def _kill_and_wait(self, proc: subprocess.Popen[bytes], pid: int | None, *, context: str) -> bool:
        """SIGKILL *proc* only while ``poll()`` says it is running, then wait.

        Returns True when ``poll()`` is not None after the wait. A timeout
        leaves False: SIGKILL has not taken effect, and the pid must not be
        reported as exited.

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
        return proc.poll() is not None

    def _discard_unadopted_process(self, proc: subprocess.Popen[bytes]) -> None:
        """Kill a child that was never assigned to ``self.process``."""
        self._kill_and_wait(proc, proc.pid, context="unadopted ")

    def is_alive(self) -> bool:
        # kill() assigns None under _lifecycle_lock while execute's select
        # loop calls this. A second read of self.process could be None and
        # raise AttributeError, which the write handler reported as
        # REQUEST_NOT_SERIALIZABLE. Snapshot once. Do not take
        # _lifecycle_lock here: kill() holds it across wait().
        proc = self.process
        # poll() reaps a zombie and does not run on_process_exit. A later
        # reap still reports that pid, which the kernel may already have
        # reused; the callback carries this wrapper so the formula pool
        # does not drop another slot's session. Idle prune and session
        # finalize call this while holding the pool condition; the callback
        # takes that condition, so it cannot run here. Formula drops those
        # sessions in _reap_dead_sessions_unlocked.
        return proc is not None and proc.poll() is None

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

    def _fail_request(
        self,
        code: str,
        msg: str,
        *,
        pid: int | None,
        kill: bool = True,
    ) -> dict[str, Any]:
        """Build an error dict, then kill the child or leave it.

        ``kill`` is a timeout, a desynchronized pipe, a crash, or an empty
        response. The next lease respawns that slot. False leaves the child:
        shutdown, or a process that never became live. An oversized frame
        never gets here.
        """
        count_task, mirror_message = _fail_policy(code)
        if count_task:
            # Timeouts and failed attempts count so a slot that keeps dying
            # still reaches max_tasks. Shutdown is not a task. An oversized
            # frame never gets here: no bytes were written, and the child stays.
            self.tasks_executed += 1
        snippet = self._stderr_snippet()
        if snippet:
            msg = f"{msg}\n{snippet}"
        if kill:
            log.warning("%s request failed (%s) on worker #%d; terminating pid=%s", self.worker_name, code, self.worker_id, pid)
            self.kill()
        # Callers of the mirrored codes read the text from either key.
        message = msg if mirror_message else None
        return error_dict(code, msg, message=message)

    def _timeout_message(self, deadline: _Deadline) -> str:
        """Error text for a spent budget. Sub-second budgets are refused, not reported as 0.

        ``int`` of a 1.9s budget used to report "1 seconds".
        """
        seconds = deadline.budget_sec
        if seconds < 1:
            return "Execution timeout must be at least 1 second."
        whole = int(seconds)
        shown: int | float = whole if seconds == whole else seconds
        return f"Execution exceeded maximum timeout of {shown} seconds."

    def _fail_timeout(self, deadline: _Deadline, pid: int | None, *, kill: bool = True) -> dict[str, Any]:
        """``EXECUTION_TIMEOUT`` for *deadline*. ``kill`` is false when no child should die."""
        return self._fail_request(
            "EXECUTION_TIMEOUT",
            self._timeout_message(deadline),
            pid=pid,
            kill=kill,
        )

    def _ensure_live_process(self, deadline: _Deadline) -> tuple[subprocess.Popen[bytes], IO[bytes], IO[bytes]] | dict[str, Any]:
        """Return a live child and its stdio pipes, or an error dict.

        Caller holds ``self.lock``. ``kill()`` during shutdown sets
        ``self.process`` to None; reading ``.pid`` or ``.stdin`` on that
        would be AttributeError. A dead child during shutdown is
        ``SERVICE_SHUTDOWN``, not a new process. The handshake uses the
        remaining request budget, not a fresh spawn timeout. Under one second
        left, do not spawn: the pipe-wait floor is not a handshake budget.
        Reap and ``Popen`` are inside that same clock. A budget that is gone
        when they return is ``EXECUTION_TIMEOUT``, not ``WORKER_SPAWN_FAILED``.
        """
        proc = self.process

        def _pid() -> int | None:
            return proc.pid if proc is not None else None

        if proc is None or proc.poll() is not None:
            if self._shutting_down:
                return self._fail_request("SERVICE_SHUTDOWN", f"{self.worker_name} #{self.worker_id} is shutting down.", pid=_pid(), kill=False)
            if deadline.too_late_to_spawn():
                return self._fail_timeout(deadline, _pid(), kill=False)
            # respawn recomputes the handshake timeout from deadline.left()
            # after Popen. A budget taken before the reap wait can disagree
            # with that floor, and the argument is unused when deadline is set.
            self.respawn(deadline=deadline)
            proc = self.process
            if proc is None or proc.poll() is not None:
                if self._shutting_down:
                    return self._fail_request("SERVICE_SHUTDOWN", f"{self.worker_name} #{self.worker_id} is shutting down.", pid=_pid(), kill=False)
                # Reap or Popen spent the budget. The caller's deadline is
                # gone, so this is not a worker that failed while time remained.
                if deadline.too_late_to_spawn():
                    return self._fail_timeout(deadline, _pid(), kill=False)
                return self._fail_request("WORKER_SPAWN_FAILED", f"{self.worker_name} #{self.worker_id} could not be started.", pid=_pid(), kill=False)

        stdin = proc.stdin
        stdout = proc.stdout
        if stdin is None or stdout is None:
            return self._fail_request("WORKER_SPAWN_FAILED", f"{self.worker_name} #{self.worker_id} has no stdio pipes.", pid=_pid(), kill=False)
        return proc, stdin, stdout

    def execute(self, payload: dict[str, Any], timeout_sec: float, *, req_id: Any = None) -> dict[str, Any]:
        """Send request to worker process and await response with timeout.

        ``req_id`` is copied onto the returned dict when it is not None.
        Pools used to patch ``id`` afterward, so an error frame could leave
        without it. ``None`` still omits the key.
        """
        res = self._execute(payload, timeout_sec)
        if req_id is not None and isinstance(res, dict):
            res["id"] = req_id
        return res

    def _execute(self, payload: dict[str, Any], timeout_sec: float) -> dict[str, Any]:
        """Run one request. ``execute`` stamps ``id`` on the way out."""
        # One deadline covers spawn, the stdin write, and the stdout read.
        # It is created under self.lock so waiting for that lock is not
        # part of the budget. Do not floor timeout_sec before this: a
        # caller that already passed 0 would still spawn.
        with self.lock:
            deadline = _Deadline(timeout_sec)
            if deadline.too_late_to_spawn():
                current = self.process
                return self._fail_timeout(deadline, current.pid if current is not None else None, kill=False)
            ensured = self._ensure_live_process(deadline)
            if isinstance(ensured, dict):
                return ensured
            proc, stdin, stdout = ensured
            # proc is live here. The nullable pid helper stays in
            # _ensure_live_process, where the slot may still be empty.
            pid = proc.pid

            try:
                # The write shares the request deadline. It holds self.lock, so a
                # child that stopped reading stdin would never return and the
                # slot would stay leased. A partial frame is desynchronized,
                # so the child is killed.
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
                return error_dict("PAYLOAD_TOO_LARGE", str(exc))
            except (pickle.PicklingError, TypeError, ValueError, RecursionError) as exc:
                # pack_pickle_frame pickles before the first write. These used
                # to escape execute. ValueError and RecursionError from
                # pickle.dumps did too, and the HTTP handler turned them into
                # an unhandled 500. The child is still frame-aligned, so it
                # stays. AttributeError is not included: a shutdown race on
                # self.process used to land here and skip the kill.
                # MemoryError is not included: the process may be out of memory.
                return error_dict("REQUEST_NOT_SERIALIZABLE", str(exc))
            except subprocess.TimeoutExpired:
                return self._fail_timeout(deadline, pid)
            except OSError as exc:
                return self._fail_request("WORKER_PIPE_BROKEN", f"Failed to send request to {self.worker_name} #{self.worker_id}: {exc}", pid=pid)

            try:
                resp = read_pickle_frame_with_timeout(
                    stdout,
                    deadline.left(),
                    is_alive=self.is_alive,
                    max_payload_bytes=self.max_payload_bytes,
                    unpacker=unpack_restricted_pickle_frame,
                )
            except subprocess.TimeoutExpired:
                # The frame never arrived. Kill rather than read it later:
                # the next caller would consume this response as its own.
                return self._fail_timeout(deadline, pid)
            except IpcPartialFrameTimeout:
                # A deadline after the first byte is a timeout, not a crash.
                # The next read would consume the rest of this frame as a
                # new response, so the child is killed.
                return self._fail_timeout(deadline, pid)
            except Exception as exc:
                return self._fail_request("WORKER_CRASHED", f"{self.worker_name} error: {exc}", pid=pid)
            if resp is None or not isinstance(resp, dict):
                # Shutdown kills the child while this read is in flight. EOF
                # used to be EMPTY_RESPONSE, which counts a task and looks
                # like a crash. The pool is stopping, so this is
                # SERVICE_SHUTDOWN and the child is already being reaped.
                if self._shutting_down:
                    return self._fail_request(
                        "SERVICE_SHUTDOWN",
                        f"{self.worker_name} #{self.worker_id} is shutting down.",
                        pid=pid,
                        kill=False,
                    )
                return self._fail_request("EMPTY_RESPONSE", f"No response returned from {self.worker_name}.", pid=pid)
            self.tasks_executed += 1
            self._cap_stderr_log()
            return resp


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

    def __init__(self, script_path: str, num_workers: int = 1, default_timeout_sec: int = 30, max_tasks: int = 500, worker_name: str = "Worker", idle_worker_ttl_sec: float | None = None, max_payload_bytes: int = DEFAULT_MAX_PAYLOAD_BYTES, on_process_exit: Callable[[BaseProcessWorker, int], None] | None = None) -> None:
        self.script_path = script_path
        self.num_workers = max(0, num_workers)
        self.default_timeout_sec = default_timeout_sec
        # 0 used to recycle on the first release: tasks_executed >= 0 is
        # always true. Config already rejects < 1. A direct caller gets
        # the same floor.
        self.max_tasks = max(1, max_tasks)
        self.worker_name = worker_name
        self.idle_worker_ttl_sec = idle_worker_ttl_sec
        self.max_payload_bytes = max_payload_bytes
        self.workers: list[BaseProcessWorker] = []
        self._is_shutdown = False
        self._lock = threading.RLock()
        # Oldest idle worker is first. The value is the monotonic time of
        # the last successful handshake or consumed response. Release moves
        # a worker to the end and writes a new stamp. Leasing pops the entry,
        # so the stamp cannot outlive the idle slot.
        self._idle: OrderedDict[BaseProcessWorker, float] = OrderedDict()
        # Leased or cold-claimed. A dead pid nobody holds is not idle;
        # the next lease respawns it.
        self._leased: set[BaseProcessWorker] = set()
        self._cond = threading.Condition(self._lock)
        self._reaper_stop_event = threading.Event()

        if self.num_workers > 0:
            # Workers spawn one at a time, each waiting on its ready handshake
            # (up to _SPAWN_READY_TIMEOUT_SEC). Spawning them in parallel would
            # cut startup roughly with the worker count. Left for later.
            for i in range(self.num_workers):
                w = BaseProcessWorker(i + 1, script_path=script_path, worker_name=worker_name, max_payload_bytes=max_payload_bytes, on_process_exit=on_process_exit)
                self.workers.append(w)
                # Idle only after the ready handshake. A failed spawn stays
                # out of the idle set; the next lease respawns that slot.
                if w.is_alive():
                    # Stamp after spawn. A timestamp taken before this loop made a
                    # slow handshake look already idle, so a short idle TTL killed
                    # the child as soon as the reaper ran.
                    self._idle[w] = time.monotonic()

        # 0 and None mean this pool does not evict for idle time. A subclass
        # can still start the thread for its own TTL (shared session expiry).
        period = self._reaper_period_sec()
        if period is not None:
            self._start_idle_reaper(period)

    def _start_reaper(self, name: str, interval: float, fn: Callable[[], None]) -> None:
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
        run_in_background(_loop, name=name, dedicated=True)

    def _reaper_period_sec(self) -> float | None:
        """Seconds the idle reaper uses for its interval, or None to stay off.

        0 and None both mean this pool does not evict for idle time. A zero
        interval would spin, and treating 0 as already expired would kill
        workers that just spawned. Subclasses add their own TTL so a session
        timer still runs when idle eviction is off.
        """
        ttl = self.idle_worker_ttl_sec
        if ttl is None or ttl <= 0:
            return None
        return float(ttl)

    def _start_idle_reaper(self, period: float) -> None:
        interval = max(0.02, min(period / 6.0, 300.0))
        self._start_reaper(
            name=f"{self.worker_name}-idle-reaper",
            interval=interval,
            fn=self._evict_idle_workers,
        )

    def _evict_idle_workers(self) -> None:
        if self._is_shutdown:
            return
        with self._cond:
            idle_now = list(self._idle)
        # A child can append stderr for the whole idle gap. Cap outside
        # the pool lock; the next request also caps, but may not arrive
        # before the file has grown.
        for worker in idle_now:
            worker._cap_stderr_log()
        now = time.monotonic()
        # Split the reason. One "idle for" line blamed the idle timer when
        # the kill was a worker whose sessions were already past session TTL.
        idle_hits: list[BaseProcessWorker] = []
        abandoned_hits: list[BaseProcessWorker] = []
        idle_ttl = self.idle_worker_ttl_sec
        with self._cond:
            # Drop dead pids before the TTL pass so lease_any cannot pop them.
            # A dead pid is not idle: the next lease performs the handshake.
            self._prune_dead_idle_unlocked()
            for w in list(self._idle):
                if self._skip_idle_evict(w):
                    continue
                last_active = self._idle.get(w, now)
                # 0 and None do not expire. A zero TTL used to compare as
                # already elapsed and kill every idle child on the first tick.
                idle_expired = idle_ttl is not None and idle_ttl > 0 and now - last_active >= idle_ttl
                # Past idle TTL stays in the idle bucket even if the sessions
                # are also stale: that sentence is still true. The other
                # bucket is only the kill that happened before idle TTL.
                # The base pool has no session map, so abandoned is false there.
                if idle_expired:
                    idle_hits.append(w)
                elif self._abandoned_sessions(w):
                    abandoned_hits.append(w)
            to_kill = idle_hits + abandoned_hits
            for w in to_kill:
                self._drop_evicted_unlocked(w)
        for w in to_kill:
            w.kill()
        # Do not put the killed process back in idle. Idle is a successful
        # handshake or a consumed response frame. lease_any claims the cold
        # slot and respawns it.
        if idle_hits or abandoned_hits:
            with self._cond:
                self._cond.notify_all()
        if idle_hits and idle_ttl is not None:
            log.info("Idle worker reaper terminated %d %s(s) idle for >%.1fs", len(idle_hits), self.worker_name, idle_ttl)
        if abandoned_hits:
            log.info("Idle worker reaper terminated %d %s(s) whose shared sessions were all past the session TTL", len(abandoned_hits), self.worker_name)

    def _drop_evicted_unlocked(self, worker: BaseProcessWorker) -> None:
        """Remove *worker* from idle. Caller holds ``self._cond``.

        The last-active stamp is the idle value, so this pop drops it too.
        The next idle writes a new stamp.
        """
        self._idle.pop(worker, None)

    def _skip_idle_evict(self, _worker: BaseProcessWorker) -> bool:
        """Return true to leave *_worker* running past the idle TTL.

        Formula sessions override this. The base pool has no session map.
        """
        return False

    def _abandoned_sessions(self, _worker: BaseProcessWorker) -> bool:
        """True when *_worker* should be killed before idle TTL elapses.

        Formula overrides this for a process whose shared sessions are all
        past the session TTL. The base pool has no session map.
        """
        return False

    def is_enabled(self) -> bool:
        return self.num_workers > 0 and not self._is_shutdown

    def _prune_dead_idle_unlocked(self) -> None:
        """Drop dead pids from idle. Caller holds self._cond.

        A dead pid is not idle. The next lease claims that slot as cold
        and respawns it. The last-active stamp goes with the idle entry;
        the next successful idle writes a new one.

        ``is_alive`` may reap the pid without ``on_process_exit``. Formula
        session rows are dropped by ``_reap_dead_sessions_unlocked``.
        """
        for worker in list(self._idle):
            if not worker.is_alive():
                self._idle.pop(worker, None)

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
        """Return a live worker to idle. Kill a slot that has reached max_tasks.

        A timeout already killed the child, so this release finds it dead
        and does not re-idle it. max_tasks does the same kill and leaves the
        slot dead. The next lease cold-claims it and execute respawns.
        """
        self._finish_release(worker)

    def _drop_lease(self, worker: BaseProcessWorker) -> None:
        """Remove *worker* from the leased set and wake waiters.

        Shutdown calls this before ``kill()``. ``lease_any`` and
        ``lease_specific`` return ``None`` once ``_is_shutdown`` is set, so
        the slot cannot be cold-claimed during the reap. Recycle must not
        drop first: ``lease_any`` would claim a process this release is
        about to SIGKILL.
        """
        with self._cond:
            self._leased.discard(worker)
            self._cond.notify_all()

    def _finish_release(self, worker: BaseProcessWorker) -> None:
        if self._is_shutdown:
            self._drop_lease(worker)
            worker.kill()
            return

        recycle = self.should_recycle_worker(worker)
        kill_for_shutdown = False
        with self._cond:
            if self._is_shutdown:
                kill_for_shutdown = True
            elif recycle:
                # Keep the lease until kill returns. Dropping it first lets
                # lease_any cold-claim a process this release is about to
                # SIGKILL. After kill the slot is dead and not idle.
                pass
            else:
                self._leased.discard(worker)
                if worker.is_alive():
                    # The response frame was consumed and worker is alive: return to idle.
                    self._idle[worker] = time.monotonic()
                    self._idle.move_to_end(worker)
                self._cond.notify_all()

        if kill_for_shutdown:
            self._drop_lease(worker)
            worker.kill()
            return

        if recycle:
            log.info("Retiring %s #%d after %d tasks", self.worker_name, worker.worker_id, worker.tasks_executed)
            worker.kill()
            self._drop_lease(worker)

    def shutdown(self) -> None:
        """Terminate all worker processes."""
        self._reaper_stop_event.set()
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
            self._cond.notify_all()
        # request_shutdown takes _lifecycle_lock. Reap's on_process_exit
        # takes _cond while that lock is held, so this stays outside _cond.
        # Adopt then either sees the flag or publishes a child kill() reaps.
        for w in workers_to_kill:
            w.request_shutdown()
        # Reaping worker processes can take seconds (kill + wait).
        # Perform outside the pool lock so waiting threads or release callbacks
        # do not block on child process termination.
        for w in workers_to_kill:
            w.kill()
