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
- A shared session dies with its child; a replaced or exited child drops that session
- After max_tasks the slot is killed and left dead; the next lease respawns it
- Active shared sessions are not capped: there is no per-host cap on active shared sessions
"""

from __future__ import annotations

from collections import OrderedDict
from contextlib import nullcontext
from dataclasses import dataclass
import json
import logging
import os
import threading
import time
from typing import Any

from compute_service.config import ComputeSettings
from compute_service.json_forward import COMPUTE_MAX_PAYLOAD_BYTES, WIRE_JSON_FORWARD, ExecuteRequestError, canonical_execute_mode, require_execute_wire, validate_session_id
from compute_service.worker_base import BaseProcessPool, BaseProcessWorker, Deadline, MIN_REQUEST_SEC, PoolSingleton, error_dict, resolve_override
from plugin.scripting.config_limits import HOST_IPC_READ_GRACE_SEC

log = logging.getLogger("compute_service.formula")

# The child never ran the cell. Dropping the lost-session marker on these
# codes would hide a reset kernel from the next sticky call.
_UNRUN_RESULT_CODES = frozenset({"QUEUE_TIMEOUT", "PAYLOAD_TOO_LARGE", "WORKER_SPAWN_FAILED", "WORKER_PIPE_BROKEN"})
SESSION_RESET_TIMEOUT_SEC = 5.0

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_WORKER_SCRIPT = os.path.join(_SCRIPT_DIR, "formula_worker.py")


@dataclass
class _Session:
    """One shared kernel.

    ``child`` is the live process that owned the namespace. Identity replaces
    the old pid: a respawn installs a different ``Child`` even when the kernel
    has not exited.
    """

    worker: BaseProcessWorker
    child: object


def decode_worker_result(res: dict[str, Any]) -> dict[str, Any]:
    """Materialize ``result_json`` for pool tests / callers that want a dict.

    The HTTP server must not use this on the success path — it forwards the
    raw bytes instead of re-dumping.
    """
    raw = res.get("result_json")
    if isinstance(raw, (bytes, bytearray)):
        parsed = json.loads(bytes(raw).decode("utf-8"))
        if isinstance(parsed, dict):
            return parsed
    return res


class FormulaProcessPool(BaseProcessPool):
    """Bounded pool of persistent worker subprocesses for formula calculations."""

    def __init__(self, settings: ComputeSettings | None = None, num_workers: int | None = None, default_timeout_sec: int | None = None, max_tasks: int | None = None, idle_worker_ttl_sec: float | None = None) -> None:
        cfg = settings or ComputeSettings()
        eff_num_workers = resolve_override(num_workers, cfg.workers)
        eff_timeout = resolve_override(default_timeout_sec, cfg.default_timeout_sec)
        eff_max_tasks = resolve_override(max_tasks, cfg.worker_max_tasks)
        eff_idle_ttl = resolve_override(idle_worker_ttl_sec, cfg.idle_worker_ttl_sec)

        # Session counts are derived from this map. A parallel index had to
        # stay in sync across set, delete, pop, and clear.
        self._sessions: dict[str, _Session] = {}
        # Consumed by the next sticky call that observes the id. The reaper
        # expires ids older than idle_worker_ttl_sec, so it does not grow forever.
        self._lost_sessions: OrderedDict[str, float] = OrderedDict()
        self._session_locks: dict[str, threading.Lock] = {}
        self._recycling: set[BaseProcessWorker] = set()
        super().__init__(script_path=_WORKER_SCRIPT, num_workers=eff_num_workers, default_timeout_sec=eff_timeout, max_tasks=eff_max_tasks, worker_name="Formula worker", idle_worker_ttl_sec=eff_idle_ttl, max_payload_bytes=COMPUTE_MAX_PAYLOAD_BYTES)

    def _clear_pool_state_unlocked(self) -> None:
        # Base shutdown drops workers and idle slots. These maps used to
        # survive, so a pool object kept after shutdown still advertised sessions.
        self._sessions.clear()
        self._lost_sessions.clear()
        self._session_locks.clear()
        self._recycling.clear()

    def _session_gate(self, session_id: str) -> threading.Lock:
        """Return the per-session placement lock for *session_id*.

        Held from lookup through worker release on shared execute/reset calls
        to prevent concurrent first-calls from selecting different workers.
        Caller must acquire the returned lock outside self._cond.
        """
        with self._cond:
            return self._session_locks.setdefault(session_id, threading.Lock())

    def _mark_session_lost_unlocked(self, session_id: str) -> None:
        """Remember that *session_id* lost its kernel. Caller holds ``self._cond``.

        ``session_reset`` is once, for the next sticky call that observes
        this set. A caller already blocked on the lease when the marker is
        set can run one cell without the flag. Worker death drops that
        worker's sessions on the exited child, and this set still reports once.
        """
        # Reinsert so dict order is age. The reaper pops expired ids off the front.
        self._lost_sessions.pop(session_id, None)
        self._lost_sessions[session_id] = time.monotonic()

    def _expire_lost_markers_unlocked(self) -> None:
        """Drop lost markers older than ``idle_worker_ttl_sec``. Caller holds ``self._cond``.

        A TTL of 0 keeps every marker: sessions never expire in that
        configuration either, so the markers are bounded the same way the
        session map is.
        """
        ttl = self.idle_worker_ttl_sec
        if ttl is None or ttl <= 0:
            return
        now = time.monotonic()
        while self._lost_sessions:
            sid, marked = next(iter(self._lost_sessions.items()))
            if now - marked < ttl:
                return
            del self._lost_sessions[sid]

    def _evict_idle_workers(self) -> None:
        with self._cond:
            self._expire_lost_markers_unlocked()
        super()._evict_idle_workers()

    def _drop_session(self, session_id: str, *, only_if_worker: BaseProcessWorker | None = None, lost: bool = True) -> None:
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

    def _session_child_live(self, child: object) -> bool:
        """True when *child* still has a running process.

        A stand-in with no ``managed`` stays live until the slot's ``child``
        pointer changes. ``is_alive`` reaps a zombie; that is a dead kernel.
        """
        managed = getattr(child, "managed", None)
        if managed is None:
            return True
        is_alive = getattr(managed, "is_alive", None)
        if not callable(is_alive):
            return True
        try:
            return bool(is_alive())
        except Exception:
            log.debug("session child poll failed", exc_info=True)
            return False

    def _reap_dead_sessions_unlocked(self) -> None:
        """Drop sessions whose process is gone or has been replaced.

        An external SIGKILL does not enter ``kill()``. ``poll()`` reaps that
        process. A session whose stored child is not the slot's current
        child has lost its kernel, including a child handed to the module
        reaper that has not exited yet.

        The child pointer is read without ``_lifecycle_lock``. This method
        already holds the pool condition. It must not take ``_lifecycle_lock``
        or call back into the worker.
        """
        for sid, s in list(self._sessions.items()):
            assert s.child is not None, "session child must not be None"
            worker = s.worker
            if s.child is not getattr(worker, "child", None) or not self._session_child_live(s.child):
                self._drop_session(sid)

    def live_session_worker(self, session_id: str) -> BaseProcessWorker | None:
        """The process that still owns *session_id*, or None once that child is gone.

        Test helper. Production code does not call this.
        """
        with self._cond:
            self._reap_dead_sessions_unlocked()
            s = self._sessions.get(session_id)
            return s.worker if s is not None else None

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

    def _reset_session_on_worker(self, worker: BaseProcessWorker, session_id: str, timeout_sec: float = SESSION_RESET_TIMEOUT_SEC, *, lost: bool = False) -> dict[str, Any]:
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
        """
        with self._cond:
            if not worker.is_alive():
                for sid in self._worker_sessions_for(worker):
                    self._drop_session(sid)
                self._recycling.discard(worker)
                return False
            if self._worker_has_sessions(worker):
                self._recycling.discard(worker)
                return False
            if worker.tasks_executed >= self.max_tasks:
                self._recycling.add(worker)
                return True
            self._recycling.discard(worker)
            return False

    def _clear_recycling_mark(self, worker: BaseProcessWorker) -> None:
        with self._cond:
            self._recycling.discard(worker)

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

    def reset_session(self, session_id: str, timeout_sec: float = SESSION_RESET_TIMEOUT_SEC) -> dict[str, Any]:
        """Drop the shared sandbox + init companion for *session_id*.

        HTTP ``POST /v1/session/reset`` calls this (do not add a second reset
        path). Unknown / already-gone ids are idempotent ``ok``. The session
        map is dropped only when the worker reset returns ``status`` ``ok``.
        A failed reset is logged and the map stays, because that process may
        still hold the namespace. Default timeout_sec=SESSION_RESET_TIMEOUT_SEC
        gives an in-progress calculation time to complete before failing the
        control-plane reset request.
        """
        try:
            validate_session_id(session_id)
        except ExecuteRequestError as exc:
            return {"status": "error", "code": "INVALID_REQUEST", "error": str(exc)}

        gate = self._session_gate(session_id)
        with gate:
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

        target_packages = ["numpy", "sympy", "pandas", "scipy"] if packages is None else packages
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

    def _pick_new_shared_worker(self) -> BaseProcessWorker | None:
        """Pick a worker for a new shared session under self._cond.

        Distribute new shared sessions across workers by choosing the worker
        currently hosting the fewest active sessions. Prefer idle workers
        among ties. worker_id is the last key so a restart does not reshuffle
        ties: hash() depends on PYTHONHASHSEED. Dead workers sort last so a new
        reservation gets a live child when any process is up. A dead-only pool
        still reserves one slot; execute respawns it.
        """
        if not self.workers:
            return None

        def _rank(w: BaseProcessWorker) -> tuple[int, int, int, int]:
            return (0 if w.is_alive() else 1, self._worker_session_count(w), 0 if w in self._idle else 1, w.worker_id)

        eligible = [w for w in self.workers if w not in self._recycling and not w.pending_exit]
        if not eligible:
            eligible = list(self.workers)

        return min(eligible, key=_rank)

    def _select_shared_worker(self, session_id: str) -> tuple[BaseProcessWorker | None, bool]:
        """Select target worker for a shared session under self._cond.

        Returns (target_worker, session_was_lost).
        ``session_was_lost`` is consumed here; the caller puts it back if the
        cell does not run. A loss that lands while this caller is already
        blocked on the lease is not seen.
        """
        with self._cond:
            self._reap_dead_sessions_unlocked()
            sess = self._sessions.get(session_id)
            if sess is not None and sess.worker not in self.workers:
                # execute() checks self.workers outside _cond. shutdown()
                # clears that list under _cond, so this can still fire.
                # The wrapper is gone. Do not mark lost: the caller treats
                # this as a new reservation on a live worker. An empty
                # workers list then returns no target.
                self._drop_session(session_id, lost=False)
                sess = None

            session_was_lost = session_id in self._lost_sessions
            if session_was_lost:
                self._lost_sessions.pop(session_id, None)

            target_worker: BaseProcessWorker | None
            if sess is not None:
                target_worker = sess.worker
            else:
                target_worker = self._pick_new_shared_worker()

            return target_worker, session_was_lost

    def _run_execution(self, leased: BaseProcessWorker, payload: dict[str, Any], clock: Deadline, session_was_lost: bool, req_id: str | None, decode_result: bool) -> dict[str, Any]:
        """Send execution payload to leased worker and format response."""
        # Give the child the remaining budget, and wait at least alarm +
        # grace. The original full timeout would let signal.alarm lose to
        # this read, so a normal sleep became SIGKILL and dropped every
        # shared session on that process.
        # usable() is the true remainder, or None when the request must
        # not start. A one-second request can already be under a second.
        # int() of that remainder is 0, and alarm(0) cancels the child's
        # alarm. The lift to one second is here, next to the alarm, so the
        # host wait is not shorter than the alarm.
        run_for = clock.usable()
        if run_for is None:
            return error_dict("QUEUE_TIMEOUT", "Request deadline expired before a worker lease.", req_id=req_id)
        if run_for < MIN_REQUEST_SEC:
            run_for = MIN_REQUEST_SEC
        child_alarm = int(run_for)
        payload["timeout_sec"] = child_alarm
        payload["session_reset"] = session_was_lost
        host_timeout = run_for + HOST_IPC_READ_GRACE_SEC
        res = leased.execute(payload, timeout_sec=host_timeout, req_id=req_id)
        if session_was_lost and isinstance(res, dict):
            res["session_reset"] = True
        if decode_result and isinstance(res, dict):
            try:
                decoded = decode_worker_result(res)
            except (json.JSONDecodeError, UnicodeDecodeError, RecursionError, MemoryError) as exc:
                # What was wrong: json.loads on result_json escaped execute, and the
                # catch that stopped that labeled the failure WORKER_CRASHED. The child
                # did not die.
                # How: deeply nested JSON raises RecursionError, and a huge value can
                # raise MemoryError. JSONDecodeError and UnicodeDecodeError are the
                # same path. The pickle frame was already consumed and tasks_executed
                # already counted.
                # Why: RESULT_DECODE_ERROR names a decode failure. The child stays up,
                # so a bad inner blob does not drop shared sessions. HTTP stays 500
                # via _HTTP_STATUS_BY_CODE.
                failed = error_dict("RESULT_DECODE_ERROR", f"Worker result_json could not be decoded: {exc}", req_id=req_id)
                if session_was_lost:
                    failed["session_reset"] = True
                return failed
            if session_was_lost and isinstance(decoded, dict):
                decoded["session_reset"] = True
            return decoded
        return res

    def _finalize_session(
        self,
        leased: BaseProcessWorker,
        session_id: str | None,
        mode: str,
        *,
        result: dict[str, Any] | None = None,
        existed: bool = False,
    ) -> None:
        """Update session mapping and release leased worker.

        Bugfix: what was wrong: session was inserted into _sessions before lease_specific
        returned, with child None when worker was recycling or dead.
        How it happened: _select_shared_worker inserted the row early to prevent another
        call picking a different worker.
        Why this change fixes it: the per-session gate (_session_gate) serializes placement
        for that session id. _finalize_session is now the only insert, recording the session
        only after a cell ran and the worker child is live.
        """
        try:
            with self._cond:
                # Unlocked child read: see _reap_dead_sessions_unlocked.
                # A respawn in this window means the kernel is already gone.
                current = leased.child
                result_code = result.get("code") if isinstance(result, dict) else None
                if (
                    mode == "shared"
                    and session_id
                    and result is not None
                    and result_code not in _UNRUN_RESULT_CODES
                    and leased.is_alive()
                    and current is not None
                    and self._session_child_live(current)
                ):
                    sess = self._sessions.get(session_id)
                    if sess is not None and sess.worker is leased:
                        sess.child = current
                    else:
                        self._sessions[session_id] = _Session(worker=leased, child=current)
                self._reap_dead_sessions_unlocked()
                if mode == "shared" and session_id and not leased.is_alive():
                    if existed:
                        self._drop_session(session_id, only_if_worker=leased, lost=True)
                    elif result_code not in _UNRUN_RESULT_CODES:
                        self._mark_session_lost_unlocked(session_id)
        finally:
            self.release_worker(leased)

    def execute(
        self,
        code: str,
        data: Any = None,
        session_id: str | None = None,
        timeout_sec: int | None = None,
        *,
        mode: str = "isolated",
        init_script: str | None = None,
        req_id: str | None = None,
        data_json: bytes | None = None,
        wire: str = WIRE_JSON_FORWARD,
        decode_result: bool = True,
        deadline: float | None = None,
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

        clock = Deadline.from_absolute(eff_timeout, deadline)
        # A budget under one second, or a longer one that has already fallen
        # under one second, does not lease. A 0.01s floor used to lease
        # anyway and then give the child a 1s alarm. A one-second request
        # still leases: the clock moves before this check. This is before
        # select so a miss does not reserve a session or consume the
        # lost-session marker.
        if clock.usable() is None:
            return error_dict("QUEUE_TIMEOUT", "Request deadline expired before a worker lease.", req_id=req_id)

        gate = self._session_gate(session_id) if mode == "shared" and session_id else nullcontext()
        with gate:
            leased: BaseProcessWorker | None = None
            session_was_lost = False
            existed = False
            if mode == "shared" and session_id:
                target_worker, session_was_lost = self._select_shared_worker(session_id)
                with self._cond:
                    existed = session_id in self._sessions
                if target_worker is None:
                    if session_was_lost:
                        with self._cond:
                            self._mark_session_lost_unlocked(session_id)
                    return error_dict("SERVICE_SHUTDOWN", "Formula compute pool is shutting down.", req_id=req_id)
                lease_budget = max(deadline - time.monotonic(), 0.0)
                leased = self.lease_specific(target_worker, timeout_sec=lease_budget)
                if leased is not None:
                    with self._cond:
                        row = self._sessions.get(session_id)
                        if row is not None:
                            assert row.worker is leased, "shared session row worker must match leased worker"
                busy_err = "Sticky session worker is busy and request timed out waiting for worker lease."
            else:
                lease_budget = max(deadline - time.monotonic(), 0.0)
                leased = self.lease_any(timeout_sec=lease_budget)
                busy_err = "All formula workers are currently busy and request timed out waiting for worker lease."

            if leased is None:
                if mode == "shared" and session_id and session_was_lost:
                    with self._cond:
                        self._mark_session_lost_unlocked(session_id)
                return error_dict("WORKER_POOL_BUSY", busy_err, req_id=req_id)

            result: dict[str, Any] | None = None
            try:
                # Time passes while waiting for the lease. Recheck the same clock.
                if clock.usable() is None:
                    result = error_dict("QUEUE_TIMEOUT", "Request deadline expired before a worker lease.", req_id=req_id)
                    return result
                result = self._run_execution(leased, payload=payload, clock=clock, session_was_lost=session_was_lost, req_id=req_id, decode_result=decode_result)
                return result
            finally:
                self._finalize_session(leased, session_id, mode, result=result, existed=existed)
                if (
                    mode == "shared"
                    and session_id
                    and session_was_lost
                    and isinstance(result, dict)
                    and result.get("code") in _UNRUN_RESULT_CODES
                ):
                    with self._cond:
                        self._mark_session_lost_unlocked(session_id)

    @staticmethod
    def _build_execute_payload(*, code: str, data: Any = None, data_json: bytes | None = None, session_id: str | None = None, mode: str = "isolated", init_script: str | None = None, req_id: str | None = None, wire: str = WIRE_JSON_FORWARD) -> dict[str, Any]:
        # execute() already called require_execute_wire. Repeating it here only
        # rejected direct callers. pickle is not a second payload. timeout_sec
        # is set later by _run_execution from the remaining budget.
        payload: dict[str, Any] = {"id": req_id, "code": code, "session_id": session_id, "mode": mode, "init_script": init_script, "wire": wire}
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
