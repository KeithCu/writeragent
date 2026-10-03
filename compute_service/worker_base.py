# WriterAgent - Python Compute Service Base Worker & Pool Infrastructure
# Copyright (c) 2026 KeithCu
# SPDX-License-Identifier: GPL-3.0-or-later
"""
Shared subprocess worker loop, worker process wrapper, and process pool supervisor.

Provides:
- High-speed length-prefixed Pickle 5 binary framing over stdio pipes
- Deadline-bounded pickle reads (header + payload)
- Live stderr drain (start_stderr_drain) so piped stderr cannot deadlock
- Hard SIGKILL only when a child never writes its response frame (vision drains
  one late frame first; formula waits for the in-process error frame)
- Exclusive worker occupancy. Idle means the process completed a handshake or
  a response frame was consumed — a dead pid is not idle
- Automatic crash recovery and worker recycling after max_tasks
"""

from __future__ import annotations

import enum
import logging
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from typing import Any, Generic, TypeVar, cast

from plugin.framework.worker_pool import StderrTail, get_subprocess_creationflags, start_stderr_drain
from plugin.scripting.ipc import DEFAULT_MAX_PAYLOAD_BYTES, IpcFrameError, read_pickle_frame, read_pickle_frame_with_timeout, write_pickle_frame
from plugin.scripting.sandbox import optimize_popen_pipes, scrub_subprocess_env

log = logging.getLogger("compute_service.worker")

_SPAWN_READY_TIMEOUT_SEC = 15.0
_STDERR_SNIPPET = 500

_PoolT = TypeVar("_PoolT", bound="BaseProcessPool")
_ValT = TypeVar("_ValT")


def resolve_override(override: _ValT | None, default: _ValT) -> _ValT:
    """Return *override* if not None, else *default*."""
    return default if override is None else override


class PoolSingleton(Generic[_PoolT]):
    """Thread-safe global singleton holder for a BaseProcessPool subclass."""

    _lock: threading.Lock
    _pool: _PoolT | None

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pool = None

    def get(self, factory: Callable[[], _PoolT]) -> _PoolT:
        with self._lock:
            if self._pool is None:
                self._pool = factory()
            return self._pool

    def shutdown(self) -> None:
        with self._lock:
            if self._pool is not None:
                self._pool.shutdown()
                self._pool = None


def run_worker_stdio_loop(handler: Callable[[dict[str, Any]], dict[str, Any]], *, max_payload_bytes: int = DEFAULT_MAX_PAYLOAD_BYTES) -> int:
    """Standard binary Pickle 5 stdio worker loop for child subprocesses."""
    stdin_bin = sys.stdin.buffer
    stdout_bin = sys.stdout.buffer

    # Signal readiness to supervisor
    write_pickle_frame(stdout_bin, {"status": "ready", "pid": os.getpid()})

    while True:
        try:
            req = read_pickle_frame(stdin_bin, max_payload_bytes=max_payload_bytes)
            if req is None:
                break
            if not isinstance(req, dict):
                res = {"status": "error", "error": "Request must be a dict"}
            else:
                res = handler(req)
                if not isinstance(res, dict):
                    res = {"status": "error", "error": "Handler returned non-dict"}
        except Exception as exc:
            res = {"status": "error", "error": f"Invalid IPC frame or unhandled error: {exc}"}

        try:
            write_pickle_frame(stdout_bin, res, max_payload_bytes=max_payload_bytes)
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
    did_respawn: bool
    recover_on_timeout: bool
    on_process_exit: Callable[[int], None] | None
    _drain_state: _DrainState
    _drain_lock: threading.Lock

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
        self.did_respawn = False
        self._stderr_drain: StderrTail | None = None
        # idle: no timeout drain. draining: late frame still on the pipe.
        # release_wait: release_worker already ran and must re-idle after the
        # frame. drained: frame consumed before release_worker ran.
        self._drain_state = _DrainState.IDLE
        self._drain_lock = threading.Lock()
        self._release_cb: Callable[[], None] | None = None
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
                    pass
            if previous is not None:
                try:
                    previous.wait(timeout=1.0)
                except Exception:
                    pass
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

    def respawn(self) -> None:
        """Spawn worker subprocess and await readiness handshake."""
        self._reap_previous_process()
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
            self.process = proc
            optimize_popen_pipes(proc)
            self._stderr_drain = start_stderr_drain(proc.stderr, name=f"{self.worker_name}-stderr-{self.worker_id}")
            ready_data: Any = None
            if proc.stdout is not None:
                ready_data = read_pickle_frame_with_timeout(proc.stdout, _SPAWN_READY_TIMEOUT_SEC, is_alive=self.is_alive, max_payload_bytes=self.max_payload_bytes, require_dict=True)
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


    def is_alive(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def kill(self) -> None:
        """Terminate the child using the same rules as spawn's reap.

        ``kill()`` used to signal even after ``poll()`` had reaped the pid,
        and it left ``_stderr_drain`` set so the next ``respawn`` dropped that
        thread. ``_reap_previous_process`` already avoided both.
        """
        self._reap_previous_process()

    def execute(self, payload: dict[str, Any], timeout_sec: float) -> dict[str, Any]:
        """Send request to worker process and await response with timeout."""
        timeout_sec = max(0.01, float(timeout_sec))
        with self.lock:
            # The pool reads this after the call. A respawn replaces the
            # kernel; session maps that still name this wrapper are stale.
            self.did_respawn = False
            if not self.is_alive():
                self.respawn()
                self.did_respawn = True
                if not self.is_alive():
                    return {"status": "error", "code": "WORKER_SPAWN_FAILED", "error": f"{self.worker_name} #{self.worker_id} could not be started."}

            assert self.process is not None
            assert self.process.stdin is not None
            assert self.process.stdout is not None

            try:
                write_pickle_frame(self.process.stdin, payload, max_payload_bytes=self.max_payload_bytes)
            except IpcFrameError as exc:
                # Raised before any byte is written. The child is still the
                # same kernel; killing it would drop every shared session.
                return {"status": "error", "code": "PAYLOAD_TOO_LARGE", "error": str(exc)}
            except (BrokenPipeError, OSError) as exc:
                snippet = self._stderr_snippet()
                self.kill()
                err = f"Failed to send request to {self.worker_name} #{self.worker_id}: {exc}"
                if snippet:
                    err = f"{err}\n{snippet}"
                return {"status": "error", "code": "WORKER_PIPE_BROKEN", "error": err}

            try:
                resp = read_pickle_frame_with_timeout(self.process.stdout, timeout_sec, is_alive=self.is_alive, max_payload_bytes=self.max_payload_bytes)
            except subprocess.TimeoutExpired:
                snippet = self._stderr_snippet()
                msg = f"Execution exceeded maximum timeout of {int(timeout_sec)} seconds."
                if snippet:
                    msg = f"{msg}\n{snippet}"
                if self.recover_on_timeout:
                    # The child is still going to write one frame. Killing it
                    # drops a loaded OCR model, and releasing the pipe now would
                    # make the next request read that frame. Drain it aside.
                    log.warning("%s execution timed out after %.1fs on worker #%d; draining late frame from pid=%s", self.worker_name, timeout_sec, self.worker_id, self.process.pid)
                    self._start_late_drain(timeout_sec)
                else:
                    # The in-process alarm did not return a frame inside the
                    # grace period (stuck in C, or the host clock was shorter
                    # than the child). SIGKILL is the last resort. Reap drops
                    # every shared session on this pid before the slot is reused.
                    log.warning("%s execution timed out after %.1fs on worker #%d; terminating pid=%s", self.worker_name, timeout_sec, self.worker_id, self.process.pid)
                    self.kill()
                return {"status": "error", "code": "EXECUTION_TIMEOUT", "error": msg, "message": msg}
            except Exception as exc:
                snippet = self._stderr_snippet()
                self.kill()
                err = f"{self.worker_name} error: {exc}"
                if snippet:
                    err = f"{err}\n{snippet}"
                return {"status": "error", "code": "WORKER_CRASHED", "error": err, "message": err}
            if resp is None or not isinstance(resp, dict):
                snippet = self._stderr_snippet()
                self.kill()
                err = f"No response returned from {self.worker_name}."
                if snippet:
                    err = f"{err}\n{snippet}"
                return {"status": "error", "code": "EMPTY_RESPONSE", "error": err}
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
                resp = read_pickle_frame_with_timeout(stdout, timeout_sec, is_alive=self.is_alive, max_payload_bytes=self.max_payload_bytes)
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
        self._idle: set[BaseProcessWorker] = set()
        # Leased, cold-claimed, or mid-recycle. A dead pid nobody holds is
        # not idle; the next lease respawns it. Recycle stays in this set
        # until respawn finishes, or lease_any treats that dead pid as free.
        self._leased: set[BaseProcessWorker] = set()
        self._worker_last_active: dict[BaseProcessWorker, float] = {}
        self._cond = threading.Condition(self._lock)
        self._reaper_stop_event = threading.Event()
        self._idle_reaper_thread: threading.Thread | None = None

        if self.num_workers > 0:
            for i in range(self.num_workers):
                w = BaseProcessWorker(i + 1, script_path=script_path, worker_name=worker_name, max_payload_bytes=max_payload_bytes, recover_on_timeout=recover_on_timeout, on_process_exit=on_process_exit)
                self.workers.append(w)
                # Idle only after the ready handshake. A failed spawn stays
                # out of the idle set; the next lease respawns that slot.
                if w.is_alive():
                    self._idle.add(w)
                    # Stamp after spawn. A timestamp taken before this loop made a
                    # slow handshake look already idle, so a short idle TTL killed
                    # the child as soon as the reaper ran.
                    self._worker_last_active[w] = time.monotonic()

        if self.idle_worker_ttl_sec is not None and self.idle_worker_ttl_sec > 0:
            self._start_idle_reaper()

    def _start_reaper(self, name: str, interval: float, fn: Callable[[], None]) -> threading.Thread:
        def _loop() -> None:
            # Wait on stop event instead of time.sleep so pool shutdown terminates immediately
            while not self._reaper_stop_event.wait(interval):
                if not self._is_shutdown:
                    fn()

        t = threading.Thread(target=_loop, name=name, daemon=True)
        t.start()
        return t

    def _start_idle_reaper(self) -> None:
        ttl = cast(float, self.idle_worker_ttl_sec)
        interval = max(0.02, min(ttl / 6.0, 300.0))
        self._idle_reaper_thread = self._start_reaper(
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
            for w in list(self._idle):
                if not w.is_alive():
                    continue
                if self._skip_idle_evict(w):
                    continue
                last_active = self._worker_last_active.get(w, now)
                if now - last_active >= self.idle_worker_ttl_sec:
                    stale.append(w)
            # Remove from idle set while holding the lock so lease_any()
            # cannot pop a worker we are about to kill.
            for w in list(self._idle):
                if not w.is_alive():
                    # Already exited. poll() inside is_alive reaped it. It is
                    # not idle: the next lease performs the handshake.
                    self._idle.discard(w)
            for w in stale:
                self._idle.discard(w)
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
            worker = self._idle.pop()
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

        The dead slot is not idle. ``execute`` performs the handshake and
        sets ``did_respawn``; release idles the worker only after that
        handshake or after a response frame is consumed.
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
                    self._idle.discard(worker)
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
                    self._idle.add(worker)
                    self._worker_last_active[worker] = time.monotonic()
                self._cond.notify_all()

        if kill_worker:
            worker.kill()
            return

        if recycle:
            log.info("Recycling %s #%d after %d tasks to refresh memory", self.worker_name, worker.worker_id, worker.tasks_executed)
            worker.kill()
            # Re-spawn so the next lease does not pay spawn latency inside execute().
            worker.respawn()
            with self._cond:
                # Drop the recycle lease before idle. The other order lets a
                # concurrent lease pop idle while this wrapper is still leased,
                # then this discard clears the lease that pop just took.
                self._leased.discard(worker)
                if not self._is_shutdown and worker.is_alive():
                    self._idle.add(worker)
                    self._worker_last_active[worker] = time.monotonic()
                elif self._is_shutdown:
                    kill_worker = True
                self._cond.notify_all()
            if kill_worker:
                worker.kill()

    def shutdown(self) -> None:
        """Terminate all worker processes."""
        self._reaper_stop_event.set()
        with self._cond:
            if self._is_shutdown:
                return
            self._is_shutdown = True
            log.info("Shutting down %s pool (%d workers)...", self.worker_name, len(self.workers))
            workers_to_kill = list(self.workers)
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
