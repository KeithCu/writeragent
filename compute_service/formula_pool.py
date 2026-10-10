# WriterAgent - Python Compute Service Formula Process Pool
# Copyright (c) 2026 KeithCu
# SPDX-License-Identifier: GPL-3.0-or-later
"""
Process pool supervisor for formula and general sandboxed Python execution.

Maintains a bounded pool of warm subprocesses. Provides:
- 100% crash isolation (master HTTP server never crashes)
- Hard SIGKILL termination for hangs/timeouts
- Multi-core linear CPU scaling (bypasses single-interpreter GIL)
- Sticky session affinity for stateful sessions (mode="shared")
- Clean workers preferred for isolated work; falls back to fewest-sessions if all idle workers hold sessions
- A shared session dies with its process; SIGKILL drops every session on that pid
- After max_tasks the slot is killed and left dead; the next lease respawns it
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
import json
import logging
import os
import time
from typing import Any

from compute_service.config import ComputeSettings
from compute_service.json_forward import (
    COMPUTE_MAX_PAYLOAD_BYTES,
    WIRE_JSON_FORWARD,
    ExecuteRequestError,
    canonical_execute_mode,
    decode_worker_result,
    require_execute_wire,
    validate_session_id,
)
from compute_service.worker_base import BaseProcessPool, BaseProcessWorker, PoolSingleton, _Deadline, error_dict, resolve_override
from plugin.scripting.config_limits import HOST_IPC_READ_GRACE_SEC

log = logging.getLogger("compute_service.formula")

# The child never ran the cell. Dropping the lost-session marker on these
# codes would hide a reset kernel from the next sticky call.
_UNRUN_RESULT_CODES = frozenset({
    "QUEUE_TIMEOUT",
    "PAYLOAD_TOO_LARGE",
    "WORKER_SPAWN_FAILED",
    "WORKER_PIPE_BROKEN",
})

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_WORKER_SCRIPT = os.path.join(_SCRIPT_DIR, "formula_worker.py")


@dataclass
class _Session:
    worker: BaseProcessWorker
    pid: int | None
    last_active: float


class FormulaProcessPool(BaseProcessPool):
    """Bounded pool of persistent worker subprocesses for formula calculations."""

    shared_kernel_ttl_sec: float

    def __init__(self, settings: ComputeSettings | None = None, num_workers: int | None = None, default_timeout_sec: int | None = None, max_tasks: int | None = None, shared_kernel_ttl_sec: float | None = None, idle_worker_ttl_sec: float | None = None) -> None:
        cfg = settings or ComputeSettings()
        eff_num_workers = resolve_override(num_workers, cfg.workers)
        eff_timeout = resolve_override(default_timeout_sec, cfg.default_timeout_sec)
        eff_max_tasks = resolve_override(max_tasks, cfg.worker_max_tasks)
        eff_shared_ttl = resolve_override(shared_kernel_ttl_sec, cfg.shared_kernel_ttl_sec)
        eff_idle_ttl = resolve_override(idle_worker_ttl_sec, cfg.idle_worker_ttl_sec)

        # Session counts are derived from this map. A parallel index had to
        # stay in sync across set, delete, pop, and clear.
        self._sessions: dict[str, _Session] = {}
        # Consumed by the next sticky call that observes the id. Capped so a
        # long-lived process does not keep every workbook it has ever reset.
        self._lost_sessions: OrderedDict[str, float] = OrderedDict()
        self._max_lost_sessions: int = 1000
        self.shared_kernel_ttl_sec = eff_shared_ttl
        super().__init__(script_path=_WORKER_SCRIPT, num_workers=eff_num_workers, default_timeout_sec=eff_timeout, max_tasks=eff_max_tasks, worker_name="Formula worker", idle_worker_ttl_sec=eff_idle_ttl, max_payload_bytes=COMPUTE_MAX_PAYLOAD_BYTES, on_process_exit=self._on_process_exit)

    def _reaper_period_sec(self) -> float | None:
        """Idle TTL, session TTL, or the shorter of the two. Both 0 stays off.

        The idle reaper is the only pass that kills a worker whose sessions
        are all past ``shared_kernel_ttl_sec``. Gating that thread on idle
        TTL alone left those kernels up when idle eviction was disabled.
        ``shared_kernel_ttl_sec`` is set before ``BaseProcessPool.__init__``
        asks for this period.
        """
        periods: list[float] = []
        idle = super()._reaper_period_sec()
        if idle is not None:
            periods.append(idle)
        if self.shared_kernel_ttl_sec > 0:
            periods.append(float(self.shared_kernel_ttl_sec))
        if not periods:
            return None
        return min(periods)

    def _mark_session_lost_unlocked(self, session_id: str) -> None:
        """Remember that *session_id* lost its kernel. Caller holds ``self._cond``.

        ``session_reset`` is once, for the next sticky call that observes
        this set. A caller already blocked on the lease when the marker is
        set can run one cell without the flag. Worker death drops every
        session on that pid, and this set still reports once.
        """
        # Reinsert so dict order is recency. The cap drops the oldest id.
        self._lost_sessions.pop(session_id, None)
        self._lost_sessions[session_id] = time.monotonic()
        while len(self._lost_sessions) > self._max_lost_sessions:
            self._lost_sessions.popitem(last=False)

    def _drop_session(
        self,
        session_id: str,
        *,
        only_if_worker: BaseProcessWorker | None = None,
        lost: bool = True,
    ) -> None:
        """Drop *session_id* from the active map.

        The pool lock is re-entrant, so callers that already hold ``self._cond``
        can use this. *only_if_worker* leaves the entry when it names a different
        process. *lost* records the id so the next sticky call reports
        ``session_reset``.
        """
        with self._cond:
            current = self._sessions.get(session_id)
            if only_if_worker is not None and (current is None or current.worker is not only_if_worker):
                return
            if lost:
                self._mark_session_lost_unlocked(session_id)
            if current is not None:
                self._sessions.pop(session_id, None)

    def _worker_session_count(self, worker: BaseProcessWorker) -> int:
        return sum(1 for s in self._sessions.values() if s.worker is worker)

    def _worker_has_sessions(self, worker: BaseProcessWorker) -> bool:
        return any(s.worker is worker for s in self._sessions.values())

    def _worker_sessions_for(self, worker: BaseProcessWorker) -> list[str]:
        return [sid for sid, s in self._sessions.items() if s.worker is worker]

    def _on_process_exit(self, pid: int) -> None:
        """Drop every shared session that named this pid.

        SIGKILL and crash both come through the worker reap. The drop is
        under the pool lock so a lookup cannot observe the session on a
        process that has already exited.
        """
        with self._cond:
            self._drop_pid_unlocked(pid)

    def _drop_pid_unlocked(self, pid: int) -> None:
        stale = [sid for sid, s in self._sessions.items() if s.pid == pid]
        for sid in stale:
            self._drop_session(sid)
        if stale:
            log.info("Dropped %d shared session(s) with exited pid=%s", len(stale), pid)

    def _reap_dead_sessions_unlocked(self) -> None:
        """Invalidate the session cache against the live pid.

        An external SIGKILL does not enter kill(); poll() reaps that pid
        and every session that named it is dropped. A wrapper that respawned
        under a new pid is not the old session.

        ``pid is None`` is only a reservation that has not been leased yet.
        Dropping it here let a second request bind the same id to another worker.

        ``worker.process`` is read without ``_lifecycle_lock``. Reap holds
        that lock across ``on_process_exit``, which takes this pool's
        condition, and this method already holds the condition. Taking the
        lifecycle lock here would deadlock. A respawn clears ``process``
        before the new child is adopted; the old kernel is gone, so dropping
        the session (``session_reset``) is the right outcome.
        """
        for sid, s in list(self._sessions.items()):
            if s.pid is None:
                continue
            worker = s.worker
            proc = worker.process
            if proc is None or proc.poll() is not None:
                self._drop_session(sid)
            elif s.pid != proc.pid:
                self._drop_session(sid)

    def live_session_worker(self, session_id: str) -> BaseProcessWorker | None:
        """The process that still owns *session_id*, or None once that pid has exited.

        Test helper. Production code does not call this.
        """
        with self._cond:
            self._reap_dead_sessions_unlocked()
            s = self._sessions.get(session_id)
            return s.worker if s is not None else None

    def _session_past_ttl(self, sess: _Session, now: float | None = None) -> bool:
        """True when *sess* has been idle past ``shared_kernel_ttl_sec``.

        A TTL of 0 never expires. That used to mean "do not start the
        reaper"; expiry is now checked on the sticky call and by the idle
        reaper, and 0 still means keep the kernel.
        """
        if self.shared_kernel_ttl_sec <= 0:
            return False
        current = time.monotonic() if now is None else now
        return (current - sess.last_active) >= self.shared_kernel_ttl_sec

    def _worker_has_fresh_session(self, worker: BaseProcessWorker, now: float | None = None) -> bool:
        current = time.monotonic() if now is None else now
        return any(s.worker is worker and not self._session_past_ttl(s, current) for s in self._sessions.values())

    def _skip_idle_evict(self, worker: BaseProcessWorker) -> bool:
        """Keep a shared kernel that is still inside the session TTL.

        Idle TTL and session TTL are independent and both default to 3600s.
        Killing a worker that still holds a live session drops that workbook.
        A session TTL of 0 never expires, so any session pins the process.
        """
        return self._worker_has_fresh_session(worker)

    def _abandoned_sessions(self, worker: BaseProcessWorker) -> bool:
        """True when every session on *worker* is past the session TTL.

        The idle reaper kills that process without waiting for
        ``idle_worker_ttl_sec``. That thread still starts when this TTL is
        positive and idle eviction is off. Process exit marks the ids lost.
        A TTL of 0 disables this. Caller holds ``self._cond``.
        """
        if self.shared_kernel_ttl_sec <= 0:
            return False
        saw = False
        for sess in self._sessions.values():
            if sess.worker is not worker:
                continue
            saw = True
            if not self._session_past_ttl(sess):
                return False
        return saw

    def _drop_session_after_reset(self, session_id: str, worker: BaseProcessWorker, res: dict[str, Any], *, lost: bool = False) -> bool:
        """Forget *session_id* only when the worker reset reports ``ok``.

        A non-ok response can mean the worker still holds the namespace.
        Dropping the id would make the next sticky cell look new on a
        kernel that is not empty. *lost* is true for TTL eviction so the
        mark and the drop share this lock hold. Explicit reset passes false.
        """
        if res.get("status") == "ok":
            self._drop_session(session_id, only_if_worker=worker, lost=lost)
            return True
        log.error("reset_session failed for %s; keeping session map because the worker may still hold the namespace: %s", session_id, res)
        return False

    def _reset_session_on_worker(self, worker: BaseProcessWorker, session_id: str, timeout_sec: float = 5.0, *, lost: bool = False) -> dict[str, Any]:
        """Send reset_session action to leased worker and update session map on ok.

        ``execute`` returns an error dict and does not raise. *lost* marks
        the id when the reset succeeds.
        """
        res = worker.execute({"action": "reset_session", "session_id": session_id}, timeout_sec=timeout_sec)
        self._drop_session_after_reset(session_id, worker, res, lost=lost)
        return res

    def should_recycle_worker(self, worker: BaseProcessWorker) -> bool:
        """Recycle worker if tasks_executed >= max_tasks, unless holding active shared sessions.

        Workers holding active shared sessions skip normal max_tasks recycling to preserve state.
        A session past shared_kernel_ttl_sec is reset on the next sticky call.
        """
        with self._cond:
            if not worker.is_alive():
                for sid in self._worker_sessions_for(worker):
                    self._drop_session(sid)
                return False
            if self._worker_has_sessions(worker):
                return False
            return worker.tasks_executed >= self.max_tasks

    def _pick_idle_worker(self) -> BaseProcessWorker | None:
        """Pop an idle worker, preferring clean workers without shared sessions.

        When all idle workers hold shared sessions, falls back to the worker
        with the fewest sessions so isolated work (=PY() / =PROMPT()) does not
        sit in WORKER_POOL_BUSY until shared session TTL.
        Caller holds self._cond.
        """
        if not self._idle:
            return None
        self._reap_dead_sessions_unlocked()
        self._prune_dead_idle_unlocked()
        if not self._idle:
            return None
        # Iteration order is oldest-idle first.
        clean_workers = [w for w in self._idle if not self._worker_has_sessions(w)]
        if clean_workers:
            chosen = clean_workers[0]
        else:
            chosen = min(self._idle, key=lambda w: self._worker_session_count(w))
        self._idle.pop(chosen, None)
        return chosen

    def reset_session(self, session_id: str, timeout_sec: float = 5.0) -> dict[str, Any]:
        """Drop the shared sandbox + init companion for *session_id*.

        HTTP ``POST /v1/session/reset`` calls this (do not add a second reset
        path). Unknown / already-gone ids are idempotent ``ok``. The session
        map is dropped only when the worker reset returns ``status`` ``ok``.
        A failed reset is logged and the map stays, because that process may
        still hold the namespace. A session past ``shared_kernel_ttl_sec`` is
        reset on the next sticky call. The idle reaper kills a worker whose
        sessions are all past that TTL. Default timeout_sec=5.0
        gives an in-progress calculation time to complete before failing the
        control-plane reset request.
        """
        try:
            validate_session_id(session_id)
        except ExecuteRequestError as exc:
            return {"status": "error", "code": "INVALID_REQUEST", "error": str(exc)}

        with self._cond:
            self._reap_dead_sessions_unlocked()
            s = self._sessions.get(session_id)
            worker = s.worker if s is not None else None

        if worker is None:
            # Already gone. Explicit reset clears the lost marker so the next
            # cell is not told session_reset.
            with self._cond:
                self._lost_sessions.pop(session_id, None)
            return {"status": "ok"}

        with self.leased(worker, timeout_sec=timeout_sec) as leased:
            if leased is None:
                # Pop the lost marker only after a successful reset. Lease
                # failure leaves the kernel untouched, so the next sticky
                # call must still report session_reset.
                return {"status": "error", "code": "WORKER_POOL_BUSY", "error": "Could not lease worker to reset session."}
            result = self._reset_session_on_worker(leased, session_id, timeout_sec=timeout_sec)
            if result.get("status") == "ok":
                # Explicit reset: discard from lost set so next call gets a clean kernel without session_reset: true
                with self._cond:
                    self._lost_sessions.pop(session_id, None)
            return result

    def check_dependencies(self, packages: list[str] | None = None, timeout_sec: float = 10.0) -> tuple[bool, str | None]:
        """Ask an idle worker to verify required dependencies (e.g. numpy, sympy).

        Returns (success, error_message).
        """
        if self._is_shutdown or not self.workers:
            return False, "Formula compute pool is not running."

        target_packages = ["numpy", "sympy"] if packages is None else packages
        with self.leased(timeout_sec=timeout_sec) as leased:
            if leased is None:
                return False, "Failed to lease a formula worker subprocess for dependency check."

            payload = {"action": "check_dependencies", "packages": target_packages}
            res = leased.execute(payload, timeout_sec=timeout_sec)
            if res.get("status") == "ok":
                return True, None
            missing = res.get("missing")
            if missing and isinstance(missing, list):
                missing_str = ", ".join(str(m) for m in missing)
                return False, (f"Error: {missing_str} is not installed in the current Python environment.\nPlease start the server using './compute_service/start.sh' or activate the correct virtual environment.")
            err = res.get("error") or "Unknown error during worker dependency check."
            return False, str(err)

    def _select_shared_worker(self, session_id: str) -> tuple[BaseProcessWorker | None, bool, bool]:
        """Select or reserve target worker for a shared session under self._cond.

        Returns (target_worker, session_was_lost, is_new_session).
        ``session_was_lost`` is consumed here. A loss that lands while
        this caller is already blocked on the lease is not seen.
        """
        with self._cond:
            self._reap_dead_sessions_unlocked()
            session_was_lost = session_id in self._lost_sessions
            if session_was_lost:
                self._lost_sessions.pop(session_id, None)
            sess = self._sessions.get(session_id)
            if sess is not None and sess.worker not in self.workers:
                # execute() checks self.workers outside _cond. shutdown()
                # clears that list under _cond, so this can still fire.
                # The wrapper is gone. Do not mark lost: the caller treats
                # this as a new reservation on a live worker. An empty
                # workers list then returns no target.
                self._drop_session(session_id, lost=False)
                sess = None

            is_new_session = (sess is None)
            if sess is not None:
                target_worker = sess.worker
            elif self.workers:
                # Distribute new shared sessions across workers by choosing the worker
                # currently hosting the fewest active sessions. Prefer idle workers
                # among ties. worker_id is the last key so a restart does not
                # reshuffle ties: hash() depends on PYTHONHASHSEED.
                # Dead workers sort last so a new reservation gets a live pid
                # when any process is up. A dead-only pool still reserves one
                # slot; execute respawns it.
                target_worker = min(
                    self.workers,
                    key=lambda w: (
                        0 if w.is_alive() else 1,
                        self._worker_session_count(w),
                        0 if w in self._idle else 1,
                        w.worker_id,
                    ),
                )
                # Reserve session->worker at pick time inside the same with self._cond!
                # Unlocked process read: see _reap_dead_sessions_unlocked. This
                # holds _cond, and reap's on_process_exit takes _cond under
                # _lifecycle_lock.
                proc = target_worker.process
                pid = proc.pid if (proc is not None and proc.poll() is None) else None
                self._sessions[session_id] = _Session(
                    worker=target_worker,
                    pid=pid,
                    last_active=time.monotonic(),
                )
            else:
                target_worker = None

            return target_worker, session_was_lost, is_new_session

    def _expire_leased_session(self, worker: BaseProcessWorker, session_id: str, deadline: float) -> bool:
        """Reset *session_id* when it has been idle past the session TTL.

        Caller holds the lease on *worker*. The map entry stays: dropping
        it let a concurrent sticky call reserve the id on another process
        before this cell finished. This call reports ``session_reset``.
        The id is not marked lost, so the following cell does not report
        it again. A failed reset leaves the namespace and returns False.
        """
        if self.shared_kernel_ttl_sec <= 0:
            return False
        with self._cond:
            sess = self._sessions.get(session_id)
            stale = sess is not None and sess.worker is worker and self._session_past_ttl(sess)
        if not stale:
            return False
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        res = worker.execute({"action": "reset_session", "session_id": session_id}, timeout_sec=remaining)
        if res.get("status") != "ok":
            log.error("reset_session failed for %s; keeping session map because the worker may still hold the namespace: %s", session_id, res)
            return False
        return True

    def _run_execution(
        self,
        leased: BaseProcessWorker,
        payload: dict[str, Any],
        clock: _Deadline,
        session_was_lost: bool,
        req_id: str | None,
        decode_result: bool,
    ) -> dict[str, Any]:
        """Send execution payload to leased worker and format response."""
        # Give the child the remaining budget, and wait at least alarm +
        # grace. The original full timeout would let signal.alarm lose to
        # this read, so a normal sleep became SIGKILL and dropped every
        # shared session on that process.
        # child_run_seconds lifts a 1s request that has already slipped
        # under a second. int() of that remainder is 0, and alarm(0)
        # cancels the child's alarm. A deadline that is already spent
        # does not run. The host wait is that floored budget: a raw
        # remainder under a second used to be shorter than the alarm, so
        # the host SIGKILLed a cell that was about to answer.
        run_for = clock.child_run_seconds()
        if run_for <= 0:
            return error_dict("QUEUE_TIMEOUT", "Request deadline expired before a worker lease.", req_id=req_id)
        child_alarm = int(run_for)
        payload["timeout_sec"] = child_alarm
        payload["session_reset"] = session_was_lost
        host_timeout = run_for + HOST_IPC_READ_GRACE_SEC
        res = leased.execute(payload, timeout_sec=host_timeout, req_id=req_id)
        if session_was_lost and isinstance(res, dict):
            res["session_reset"] = True
        if decode_result and isinstance(res, dict):
            decoded = decode_worker_result(res)
            if session_was_lost and isinstance(decoded, dict):
                decoded["session_reset"] = True
            return decoded
        return res

    def _finalize_session(self, leased: BaseProcessWorker, session_id: str | None, mode: str) -> None:
        """Update session mapping and release leased worker."""
        try:
            with self._cond:
                # Unlocked process read: see _reap_dead_sessions_unlocked.
                # A respawn in this window means the kernel is already gone.
                proc = leased.process
                if mode == "shared" and session_id and leased.is_alive() and proc is not None and proc.poll() is None:
                    sess = self._sessions.get(session_id)
                    if sess is not None and sess.worker is leased:
                        sess.pid = proc.pid
                        sess.last_active = time.monotonic()
                    else:
                        self._sessions[session_id] = _Session(
                            worker=leased,
                            pid=proc.pid,
                            last_active=time.monotonic(),
                        )
                    # Do not clear _lost_sessions here. Select already consumed
                    # a marker it observed. A reset that lands while this call
                    # is blocked on the lease sets the marker after that, and
                    # the next sticky call is the one that reports it.
                self._reap_dead_sessions_unlocked()
                # A reservation whose pid is still None (spawn failed before a
                # pid was recorded) is kept by _reap_dead_sessions_unlocked.
                # Drop it here when this lease's process is not alive.
                if mode == "shared" and session_id and not leased.is_alive():
                    self._drop_session(session_id, only_if_worker=leased)
        finally:
            self.release_worker(leased)

    def execute(
        self, code: str, data: Any = None, session_id: str | None = None, timeout_sec: int | None = None, *, mode: str = "isolated", init_script: str | None = None, req_id: str | None = None, data_json: bytes | None = None, wire: str = WIRE_JSON_FORWARD, decode_result: bool = True, deadline: float | None = None
    ) -> dict[str, Any]:
        """Execute formula code on an appropriate worker subprocess.

        The one payload is raw ``data_json`` bytes. The worker dumps the
        result once. The HTTP server sets ``decode_result=False`` so it can
        forward ``result_json`` without a second dumps. A ``data`` object is
        only a convenience for in-process callers: it is JSON-encoded into
        ``data_json`` here, not packed as a second wire.
        """
        if self._is_shutdown or not self.workers:
            return error_dict("SERVICE_SHUTDOWN", "Formula compute pool is shutting down.", req_id=req_id)

        # One schema. A mode or wire the HTTP layer missed must not be
        # rewritten and run: that returned 200 for a kernel that kept no state.
        try:
            mode = canonical_execute_mode(mode)
            wire = require_execute_wire(wire)
            # HTTP rejects shared with no session id. Reject it here too, or
            # the pool leases a worker and the child treats a missing id as isolated.
            if mode == "shared" and (not isinstance(session_id, str) or not session_id.strip()):
                raise ExecuteRequestError("mode='shared' requires a non-empty session_id.")
            if session_id:
                validate_session_id(session_id)
        except ExecuteRequestError as exc:
            return error_dict("INVALID_REQUEST", str(exc), req_id=req_id)

        # Only None means "use the default". Zero is an explicit timeout.
        eff_timeout = float(self.default_timeout_sec if timeout_sec is None else timeout_sec)
        # The HTTP handler passes the accept-time deadline. A fresh clock
        # here would give the child the original full timeout after the
        # request had already waited in the queue.
        if deadline is None:
            deadline = time.monotonic() + eff_timeout
        if time.monotonic() >= deadline:
            return error_dict("QUEUE_TIMEOUT", "Request deadline expired before a worker lease.", req_id=req_id)

        try:
            payload = self._build_execute_payload(code=code, data=data, data_json=data_json, session_id=session_id, mode=mode, init_script=init_script, req_id=req_id, wire=wire)
        except ExecuteRequestError as exc:
            return error_dict("INVALID_REQUEST", str(exc), req_id=req_id)

        clock = _Deadline.from_absolute(eff_timeout, deadline)
        # A budget under one second, or a longer one that has already fallen
        # under one second, does not lease. A 0.01s floor used to lease
        # anyway and then give the child a 1s alarm. A one-second request
        # still leases: the clock moves before this check. This is before
        # select so a miss does not reserve a session or consume the
        # lost-session marker.
        if clock.too_late_to_spawn():
            return error_dict("QUEUE_TIMEOUT", "Request deadline expired before a worker lease.", req_id=req_id)

        leased: BaseProcessWorker | None = None
        session_was_lost = False
        is_new_session = False
        if mode == "shared" and session_id:
            target_worker, session_was_lost, is_new_session = self._select_shared_worker(session_id)
            if target_worker is None:
                if session_was_lost:
                    with self._cond:
                        self._mark_session_lost_unlocked(session_id)
                return error_dict("SERVICE_SHUTDOWN", "Formula compute pool is shutting down.", req_id=req_id)
            lease_budget = max(deadline - time.monotonic(), 0.0)
            leased = self.lease_specific(target_worker, timeout_sec=lease_budget)
            busy_err = "Sticky session worker is busy and request timed out waiting for worker lease."
        else:
            lease_budget = max(deadline - time.monotonic(), 0.0)
            leased = self.lease_any(timeout_sec=lease_budget)
            busy_err = "All formula workers are currently busy and request timed out waiting for worker lease."

        if leased is None:
            if session_id:
                with self._cond:
                    if is_new_session:
                        # Drop a reservation that never got a lease. Leaving it in
                        # _sessions would block recycle and idle eviction.
                        self._drop_session(session_id, lost=False)
                    if session_was_lost:
                        self._mark_session_lost_unlocked(session_id)
            return error_dict("WORKER_POOL_BUSY", busy_err, req_id=req_id)

        result: dict[str, Any] | None = None
        try:
            # Time passes while waiting for the lease. Recheck the same clock.
            if clock.too_late_to_spawn():
                result = error_dict("QUEUE_TIMEOUT", "Request deadline expired before a worker lease.", req_id=req_id)
                return result
            if mode == "shared" and session_id and self._expire_leased_session(leased, session_id, deadline):
                session_was_lost = True
            result = self._run_execution(
                leased,
                payload=payload,
                clock=clock,
                session_was_lost=session_was_lost,
                req_id=req_id,
                decode_result=decode_result,
            )
            return result
        finally:
            self._finalize_session(leased, session_id, mode)
            # Put the lost-session marker back when the cell did not run.
            # Select already consumed it. A payload that never reached the
            # child would otherwise look like a live kernel.
            if (
                session_was_lost
                and session_id
                and isinstance(result, dict)
                and result.get("code") in _UNRUN_RESULT_CODES
            ):
                with self._cond:
                    self._mark_session_lost_unlocked(session_id)

    @staticmethod
    def _build_execute_payload(*, code: str, data: Any = None, data_json: bytes | None = None, session_id: str | None = None, mode: str = "isolated", init_script: str | None = None, req_id: str | None = None, wire: str = WIRE_JSON_FORWARD) -> dict[str, Any]:
        # Unknown wire is rejected, not rewritten to json_forward. ``pickle``
        # is not a second payload on this envelope. timeout_sec is not set
        # here: _run_execution overwrites it with the remaining budget.
        eff_wire = require_execute_wire(wire)
        payload: dict[str, Any] = {"id": req_id, "code": code, "session_id": session_id, "mode": mode, "init_script": init_script, "wire": eff_wire}
        blob = data_json
        if blob is None and data is not None:
            # Convenience for pool tests / in-process callers. The HTTP
            # path always supplies data_json so the host never dumps the grid.
            # allow_nan=False raises ValueError for NaN/Inf and TypeError for
            # a non-JSON object. That escaped execute() before any lease.
            # execute() already turns ExecuteRequestError into an error
            # payload. Do not validate data_json: those bytes are the wire.
            try:
                blob = json.dumps(data, allow_nan=False).encode("utf-8")
            except (TypeError, ValueError) as exc:
                raise ExecuteRequestError(f"data is not JSON-serializable: {exc}") from exc
        if blob is not None:
            payload["data_json"] = bytes(blob)
        return payload


# Global singleton per server process
_POOL_SINGLETON: PoolSingleton[FormulaProcessPool] = PoolSingleton()


def get_formula_pool(settings: ComputeSettings | None = None) -> FormulaProcessPool:
    """Retrieve or initialize the global formula process pool."""
    return _POOL_SINGLETON.get(lambda: FormulaProcessPool(settings=settings))


def shutdown_formula_pool(*, permanent: bool = False) -> None:
    """Shut down the global formula process pool.

    *permanent* is the server-exit path. A later ``get_formula_pool`` raises
    instead of spawning a new pool.
    """
    _POOL_SINGLETON.shutdown(permanent=permanent)
