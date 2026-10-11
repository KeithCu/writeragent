# WriterAgent - Python Compute Service Base Worker & Pool Infrastructure
# Copyright (c) 2026 KeithCu
# SPDX-License-Identifier: GPL-3.0-or-later
"""
Process pool supervisor for a bounded set of child workers.

The child itself — spawn, handshake, framed request, and reap — lives in
``plugin.framework.process_worker``. Formula, vision, and Kokoro share that
class. This module keeps the multi-slot pool and the process-wide singleton.

Provides:
- Exclusive worker occupancy. Idle means the process completed a handshake or
  a response frame was consumed — a dead pid is not idle
- After max_tasks the slot is killed and left dead. The next lease respawns it
- ``tasks_executed`` counts a consumed response. The worker resets it on a
  ready handshake

Child stdio framing (``RestrictedUnpickler``, ``run_worker_stdio_loop``,
``set_pdeathsig``) is defined in ``worker_stdio`` and re-exported here.
``Deadline`` and ``error_dict`` are re-exported from ``process_worker``.
"""

from __future__ import annotations

import contextlib
import logging
import threading
import time
from collections import OrderedDict
from typing import TYPE_CHECKING, Generic, TypeVar

if TYPE_CHECKING:
    from collections.abc import Callable, Generator

from compute_service.worker_stdio import RestrictedUnpickler, run_compute_worker, run_worker_stdio_loop, set_pdeathsig, unpack_restricted_pickle_frame
from plugin.framework.process_worker import MIN_REQUEST_SEC, BaseProcessWorker, Deadline, _Deadline, _SPAWN_READY_TIMEOUT_SEC, _STDERR_LOG_CAP, error_dict
from plugin.framework.worker_pool import run_in_background
from plugin.scripting.ipc import DEFAULT_MAX_PAYLOAD_BYTES

__all__ = ["BaseProcessPool", "BaseProcessWorker", "Deadline", "MIN_REQUEST_SEC", "PoolSingleton", "RestrictedUnpickler", "_Deadline", "_SPAWN_READY_TIMEOUT_SEC", "_STDERR_LOG_CAP", "error_dict", "resolve_override", "run_compute_worker", "run_worker_stdio_loop", "set_pdeathsig", "unpack_restricted_pickle_frame"]

log = logging.getLogger("compute_service.worker")

_PoolT = TypeVar("_PoolT", bound="BaseProcessPool")
_ValT = TypeVar("_ValT")


def resolve_override(override: _ValT | None, default: _ValT) -> _ValT:
    """Return *override* if not None, else *default*."""
    return default if override is None else override


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
        of installing it. A non-permanent shutdown used to fail this caller
        with "shut down" even though the next ``get`` built a replacement.
        A shutdown that lands after publish and before this returns used to
        hand back the pool ``shutdown`` was already tearing down.
        """
        while True:
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
                # Shutdown can null ``_pool`` in the gap after publish.
                # ``_before_handout`` is where a test opens that gap.
                self._before_handout(created)
                with self._cv:
                    # Permanent shutdown sets ``_closed``. A non-permanent
                    # one only drops ``_pool``. Either way this object is
                    # the one ``shutdown`` is reaping, so it is not returned.
                    handed_out = self._pool is created and not self._closed
                    permanent = self._closed
                if handed_out:
                    return created
                if permanent:
                    raise RuntimeError("Compute pool is shut down.")
                continue
            if published is not None and not closed:
                return published
            if closed:
                raise RuntimeError("Compute pool is shut down.")
            # Non-permanent shutdown discarded this build. A waiter may
            # publish the replacement; otherwise this caller builds again.

    def _before_handout(self, pool: _PoolT) -> None:
        """Run after publish and before the closed re-check. Tests override this.

        Production returns. The pool is already installed. ``shutdown`` can
        drop it before ``get`` returns, and the re-check after this method
        refuses to hand that pool out.
        """
        del pool

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
    _unreaped: set[BaseProcessWorker]

    def __init__(
        self,
        script_path: str,
        num_workers: int = 1,
        default_timeout_sec: int = 30,
        max_tasks: int = 500,
        worker_name: str = "Worker",
        idle_worker_ttl_sec: float | None = None,
        max_payload_bytes: int = DEFAULT_MAX_PAYLOAD_BYTES,
        on_process_exit: Callable[[BaseProcessWorker, int], None] | None = None,
    ) -> None:
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
        # kill() returned while the child was still alive. Not idle, not
        # leased, and not cold-claimable until a retry reap finishes.
        self._unreaped = set()
        self._cond = threading.Condition(self._lock)
        self._reaper_stop_event = threading.Event()

        try:
            if self.num_workers > 0:
                # Workers spawn one at a time, each waiting on its ready handshake
                # (up to _SPAWN_READY_TIMEOUT_SEC). Spawning them in parallel would
                # cut startup roughly with the worker count. Left for later.
                for i in range(self.num_workers):
                    w = BaseProcessWorker(i + 1, script_path=script_path, worker_name=worker_name, max_payload_bytes=max_payload_bytes, on_process_exit=on_process_exit, unpacker=unpack_restricted_pickle_frame)
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
        except Exception:
            # A later worker, or the reaper start, failed after earlier
            # children were already up. Nothing outside __init__ holds those
            # Popen objects, so they have to be reaped here.
            self._discard_partial_pool()
            raise

    def _discard_partial_pool(self) -> None:
        """Reap children already started when ``__init__`` fails.

        ``shutdown`` returns immediately once ``_is_shutdown`` is set, so a
        failed constructor cannot use it: the flag would skip the kill and
        the live children would have no reference.
        """
        self._reaper_stop_event.set()
        workers = list(self.workers)
        self._is_shutdown = True
        self.workers.clear()
        self._idle.clear()
        self._leased.clear()
        self._unreaped.clear()
        for worker in workers:
            try:
                worker.request_shutdown()
            except Exception:
                log.exception("Failed to mark %s #%d shutting down during init abort", self.worker_name, worker.worker_id)
        for worker in workers:
            try:
                worker.kill()
            except Exception:
                log.exception("Failed to reap %s #%d during init abort", self.worker_name, worker.worker_id)

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
        self._start_reaper(name=f"{self.worker_name}-idle-reaper", interval=interval, fn=self._evict_idle_workers)

    def _evict_idle_workers(self) -> None:
        if self._is_shutdown:
            return
        with self._cond:
            idle_now = list(self._idle)
        # A child can append stderr for the whole idle gap. Cap outside
        # the pool lock; the next request also caps, but may not arrive
        # before the file has grown.
        for worker in idle_now:
            worker.cap_stderr_log()
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
            # A child that survived SIGKILL is not idle and not leased.
            # Cold claim skips a live process, so park it for another reap.
            self._park_unreaped(w)
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
            if worker in self._idle or worker in self._leased or worker in self._unreaped:
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
                # An unreaped child stays out until retry_reap finishes, even
                # after poll() would say it has died. Claiming it here would
                # respawn while that reap is still in kill().
                if not worker.is_alive() and worker not in self._leased and worker not in self._unreaped:
                    self._leased.add(worker)
                    return worker
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._cond.wait(remaining)

    @contextlib.contextmanager
    def leased(self, worker: BaseProcessWorker | None = None, *, timeout_sec: float | None = None) -> Generator[BaseProcessWorker | None, None, None]:
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

    def _worker_reap_incomplete(self, worker: BaseProcessWorker) -> bool:
        """True when *worker* survived ``kill``. Stand-ins omit the method.

        Test slots implement ``kill`` and ``is_alive`` only. Missing means
        the reap finished, which is what those tests already model.
        """
        flag = getattr(worker, "reap_incomplete", None)
        if not callable(flag):
            return False
        return bool(flag())

    def _park_unreaped(self, worker: BaseProcessWorker) -> None:
        """Retry ``kill`` for a child that is still alive, off the pool lock.

        The watcher calls ``retry_reap`` and does not hold ``_cond`` across
        it: ``on_process_exit`` takes ``_cond`` while the lifecycle lock is
        held. Cold claim skips ``_unreaped`` until that reap finishes, so it
        cannot respawn the slot underneath the watcher.
        """
        if not self._worker_reap_incomplete(worker):
            return
        with self._cond:
            if self._is_shutdown or worker in self._unreaped:
                return
            self._unreaped.add(worker)

        def _watch() -> None:
            try:
                worker.retry_reap(self._reaper_stop_event)
            except Exception:
                log.exception("Retry reap failed for %s #%s", self.worker_name, worker.worker_id)
            with self._cond:
                if not self._worker_reap_incomplete(worker) or self._is_shutdown:
                    self._unreaped.discard(worker)
                    self._cond.notify_all()

        run_in_background(_watch, name=f"{self.worker_name}-reap-{worker.worker_id}", dedicated=True)

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
                # A failed reap leaves the child alive on a desynced pipe.
                # Idling it would hand the next lease that process.
                if worker.is_alive() and not self._worker_reap_incomplete(worker):
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
        # Shutdown returned above. A child that survived kill stays out of
        # idle and leased until a retry reap. A finished reap is a no-op.
        self._park_unreaped(worker)

    def _clear_pool_state_unlocked(self) -> None:
        """Drop subclass maps. Caller holds ``_cond`` and has set ``_is_shutdown``.

        Base shutdown clears workers, idle, and leased. A subclass that keeps
        its own maps overrides this so a pool object kept after shutdown does
        not still advertise them.
        """
        return

    def shutdown(self) -> None:
        """Terminate all worker processes."""
        self._reaper_stop_event.set()
        with self._cond:
            if self._is_shutdown:
                return
            self._is_shutdown = True
            self._clear_pool_state_unlocked()
            log.info("Shutting down %s pool (%d workers)...", self.worker_name, len(self.workers))
            workers_to_kill = list(self.workers)
            # Drop the pool sets before releasing this lock so lease_any
            # cannot hand out a slot after shutdown has started.
            self.workers.clear()
            self._idle.clear()
            self._leased.clear()
            self._unreaped.clear()
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
