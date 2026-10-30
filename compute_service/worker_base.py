# WriterAgent - Python Compute Service Base Worker & Pool Infrastructure
# Copyright (c) 2026 KeithCu
# SPDX-License-Identifier: GPL-3.0-or-later
"""
Process pool supervisor for a bounded set of child workers.

The child itself — spawn, handshake, framed request, and reap — lives in
``plugin.framework.process_worker``. Formula and vision share
``BaseProcessWorker``. Kokoro's warm process is ``ManagedSubprocess`` in
``plugin/audio/kokoro_pool.py``, not this class. This module keeps the
multi-slot pool and the process-wide singleton.

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
import os
import sys
import threading
import time
from collections import OrderedDict
from typing import TYPE_CHECKING, Generic, TypeVar

if TYPE_CHECKING:
    from collections.abc import Callable, Generator

from compute_service.worker_stdio import COMPUTE_THREAD_ENV_VARS, RestrictedUnpickler, configure_compute_worker_env, run_compute_worker, run_worker_stdio_loop, set_pdeathsig, unpack_restricted_pickle_frame
from plugin.framework.process_worker import MIN_REQUEST_SEC, BaseProcessWorker, Child, Deadline, error_dict
from plugin.framework.worker_pool import run_in_background
from plugin.scripting.ipc import DEFAULT_MAX_PAYLOAD_BYTES


def install_image_pkgs(project_root: str) -> None:
    """Add the image wheel directory when it exists.

    What was wrong: the runner image installs numpy and the rest into
    ``/app/pkgs`` and publishes that directory with ``PYTHONPATH``. Formula
    workers call ``scrub_subprocess_env``, which drops ``PYTHONPATH`` so a
    LibreOffice interpreter cannot leak its own stdlib into the child. The
    container variable never arrived, ``check_dependencies`` reported numpy
    and sympy missing, and the server exited immediately after the ready
    handshake. Re-add the known image directory by path. Do not copy
    ``PYTHONPATH`` through: that is the variable the scrub exists to remove.
    Desktop runs have no ``pkgs`` directory, so this is a no-op there.
    """
    pkgs = os.path.join(project_root, "pkgs")
    if os.path.isdir(pkgs) and pkgs not in sys.path:
        sys.path.insert(0, pkgs)


install_image_pkgs(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

__all__ = ["BaseProcessPool", "BaseProcessWorker", "COMPUTE_THREAD_ENV_VARS", "Child", "Deadline", "MIN_REQUEST_SEC", "PoolSingleton", "RestrictedUnpickler", "configure_compute_worker_env", "error_dict", "install_image_pkgs", "resolve_override", "run_compute_worker", "run_worker_stdio_loop", "set_pdeathsig", "unpack_restricted_pickle_frame"]

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

    def __init__(self) -> None:
        # A plain Lock, not the Condition default RLock. Nothing here re-enters.
        self._cv = threading.Condition(threading.Lock())
        self._pool = None
        self._closed = False
        self._building = False

    def get(self, factory: Callable[[], _PoolT]) -> _PoolT:
        """Return the pool. ``factory`` runs outside this lock."""
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
                self._building = True
            try:
                created = factory()
                with self._cv:
                    closed = self._closed
                    if not closed:
                        self._pool = created
                if closed:
                    created.shutdown()
                    raise RuntimeError("Compute pool is shut down.")
                return created
            finally:
                with self._cv:
                    self._building = False
                    self._cv.notify_all()

    def shutdown(self, *, permanent: bool = False) -> None:
        """Drop the pool. *permanent* makes a later ``get`` raise.

        Tests and a restarted process call shutdown without *permanent* so
        the next ``get`` builds a new pool. The server process passes
        *permanent* on the way out so an abandoned handler cannot spawn
        children after shutdown.

        The pool is reaped outside this lock.
        """
        with self._cv:
            pool = self._pool
            self._pool = None
            self._closed = permanent
            self._cv.notify_all()
        if pool is not None:
            pool.shutdown()


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

    def __init__(
        self,
        script_path: str,
        num_workers: int = 1,
        default_timeout_sec: int = 30,
        max_tasks: int = 500,
        worker_name: str = "Worker",
        idle_worker_ttl_sec: float | None = None,
        max_payload_bytes: int = DEFAULT_MAX_PAYLOAD_BYTES,
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
        self._cond = threading.Condition(self._lock)
        self._reaper_stop_event = threading.Event()

        try:
            if self.num_workers > 0:
                # Workers spawn one at a time, each waiting on its ready handshake.
                # Spawning them in parallel would cut startup roughly with the
                # worker count. Left for later.
                for i in range(self.num_workers):
                    w = BaseProcessWorker(i + 1, script_path=script_path, worker_name=worker_name, max_payload_bytes=max_payload_bytes, unpacker=unpack_restricted_pickle_frame)
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
                interval = max(0.02, min(period / 6.0, 300.0))
                self._start_reaper(name=f"{self.worker_name}-idle-reaper", interval=interval, fn=self._evict_idle_workers)
        except Exception:
            # A later worker, or the reaper start, failed after earlier
            # children were already up. Nothing outside __init__ holds those
            # Popen objects, so they have to be reaped here.
            self.shutdown()
            raise

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

    def _evict_idle_workers(self) -> None:
        if self._is_shutdown:
            return
        now = time.monotonic()
        idle_hits: list[BaseProcessWorker] = []
        idle_ttl = self.idle_worker_ttl_sec
        with self._cond:
            # Drop dead pids before the TTL pass so lease_any cannot pop them.
            # A dead pid is not idle: the next lease performs the handshake.
            self._prune_dead_idle_unlocked()
            for w in list(self._idle):
                last_active = self._idle.get(w, now)
                # 0 and None do not expire. A zero TTL used to compare as
                # already elapsed and kill every idle child on the first tick.
                if idle_ttl is not None and idle_ttl > 0 and now - last_active >= idle_ttl:
                    idle_hits.append(w)
            for w in idle_hits:
                self._idle.pop(w, None)
        for w in idle_hits:
            # A child that survives SIGKILL is abandoned inside kill().
            # The slot is then not alive, and the next lease can respawn it.
            w.kill()
        # Do not put the killed process back in idle. Idle is a successful
        # handshake or a consumed response frame. lease_any claims the cold
        # slot and respawns it.
        if idle_hits:
            with self._cond:
                self._cond.notify_all()
        if idle_hits and idle_ttl is not None:
            log.info("Idle worker reaper terminated %d %s(s) idle for >%.1fs", len(idle_hits), self.worker_name, idle_ttl)

    def is_enabled(self) -> bool:
        return self.num_workers > 0 and not self._is_shutdown

    def _prune_dead_idle_unlocked(self) -> None:
        """Drop dead pids from idle. Caller holds self._cond.

        A dead pid is not idle. The next lease claims that slot as cold
        and respawns it. The last-active stamp goes with the idle entry;
        the next successful idle writes a new one.

        ``is_alive`` may reap a zombie with ``poll()``. Stale formula session rows
        expire on idle TTL when the stored child is no longer the slot's child.
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

    def _lease(self, pick: Callable[[], BaseProcessWorker | None], timeout_sec: float) -> BaseProcessWorker | None:
        deadline = time.monotonic() + max(0.0, float(timeout_sec))
        while True:
            with self._cond:
                if self._is_shutdown:
                    return None
                claimed = pick()
                if claimed is not None:
                    self._leased.add(claimed)
                    return claimed
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._cond.wait(remaining)

    def lease_any(self, timeout_sec: float) -> BaseProcessWorker | None:
        """Acquire a protocol-ready worker, or a dead slot the caller will respawn.

        The dead slot is not idle. ``execute`` performs the handshake;
        release idles the worker only after that handshake or after a
        response frame is consumed.
        """
        def _pick() -> BaseProcessWorker | None:
            claimed = self._pick_idle_worker()
            if claimed is not None:
                return claimed
            # Process has exited and nobody holds the slot. Idle is only a
            # successful handshake or a consumed response frame.
            for worker in self.workers:
                if worker in self._idle or worker in self._leased:
                    continue
                if not worker.is_alive():
                    return worker
            return None

        return self._lease(_pick, timeout_sec)

    def lease_specific(self, worker: BaseProcessWorker, timeout_sec: float) -> BaseProcessWorker | None:
        """Acquire *worker* when it is idle, or once its process has exited."""
        def _pick() -> BaseProcessWorker | None:
            if worker in self._idle:
                self._idle.pop(worker, None)
                return worker
            # Process exit: not idle, but the slot can be respawned by execute.
            # A child kill() could not reap is no longer this slot's child.
            if not worker.is_alive() and worker not in self._leased:
                return worker
            return None

        return self._lease(_pick, timeout_sec)

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

    def _reap(self, worker: BaseProcessWorker, why: str) -> bool:
        try:
            worker.kill()
            return True
        except Exception:
            log.exception("Failed to reap %s #%d during %s", self.worker_name, worker.worker_id, why)
            return False

    def release_worker(self, worker: BaseProcessWorker) -> None:
        """Return a live worker to idle. Kill a slot that has reached max_tasks.

        A timeout already killed the child, so this release finds it dead
        and does not re-idle it. max_tasks does the same kill and leaves the
        slot dead. The next lease cold-claims it and execute respawns.
        """
        # An "exiting" frame means the child os._exit()s next. Never idle it,
        # even when it holds sessions.
        exiting = worker.pending_exit
        recycle = exiting or self.should_recycle_worker(worker)
        kill_for_shutdown = False
        with self._cond:
            if self._is_shutdown:
                kill_for_shutdown = True
            else:
                if recycle and not exiting:
                    # Re-check in this hold: a shared session reserved since the
                    # first check cancels the recycle and clears the mark.
                    recycle = self.should_recycle_worker(worker)
                if not recycle:
                    self._leased.discard(worker)
                    # A child kill() could not reap is no longer alive: the slot
                    # dropped it. Idling a dead slot would hand out a process
                    # execute has to respawn anyway.
                    if worker.is_alive():
                        # The response frame was consumed and worker is alive: return to idle.
                        self._idle[worker] = time.monotonic()
                        self._idle.move_to_end(worker)
                    self._cond.notify_all()

        if kill_for_shutdown:
            self._drop_lease(worker)
            self._reap(worker, "shutdown")
            return

        if recycle:
            if exiting:
                log.info("Retiring %s #%d after abandoned timeout", self.worker_name, worker.worker_id)
            else:
                log.info("Retiring %s #%d after %d tasks", self.worker_name, worker.worker_id, worker.tasks_executed)
            # What was wrong: kill() sat before _drop_lease with no handler.
            # How: an exception from kill() skipped _drop_lease, so the slot
            # stayed in _leased and no later lease could claim it.
            # Why: the same try/except as init abort logs the reap failure,
            # then _drop_lease still runs. The lease stays held across kill()
            # so lease_any cannot cold-claim the process being retired.
            if self._reap(worker, "recycle"):
                worker.pending_exit = False
                self._clear_recycling_mark(worker)
            self._drop_lease(worker)

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

    def _clear_recycling_mark(self, worker: BaseProcessWorker) -> None:
        """Clear any recycling reservation mark. Subclasses may override."""
        return

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
            self._cond.notify_all()
        # request_shutdown takes _lifecycle_lock. Keep that outside _cond so
        # a lease waiting on the condition is not stuck behind the flag.
        # Adopt then either sees the flag or publishes a child kill() signals.
        for w in workers_to_kill:
            w.request_shutdown()
        # Reaping worker processes can take seconds (kill + wait).
        # Perform outside the pool lock so waiting threads or release callbacks
        # do not block on child process termination.
        # What was wrong: one kill() exception aborted the rest of the loop.
        # How: unlike init abort, shutdown called kill() with no handler.
        # Why: each reap is logged and the loop continues, so one child
        # cannot leave the others running.
        for w in workers_to_kill:
            self._reap(w, "shutdown")
