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
- Active shared sessions are not capped per host.
"""

from __future__ import annotations

from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
import json
import logging
import math
import os
import threading
import time
from typing import Any, Generator

from compute_service import error_codes as ec
from compute_service.config import ComputeSettings
from compute_service.json_forward import COMPUTE_MAX_PAYLOAD_BYTES, ExecuteRequestError, derive_execute_mode, validate_session_id
from compute_service.worker_base import BaseProcessPool, BaseProcessWorker, PoolSingleton, bind_pool_singleton, resolve_override
from plugin.framework.child_runtime import Deadline
from plugin.framework.child_runtime import error_dict
from plugin.scripting.config_limits import HOST_IPC_READ_GRACE_SEC

log = logging.getLogger("compute_service.formula")

# The child never ran the cell. Dropping the lost-session marker on these
# codes would hide a reset kernel from the next sticky call.
_UNRUN_RESULT_CODES = frozenset({ec.QUEUE_TIMEOUT, ec.PAYLOAD_TOO_LARGE, ec.WORKER_SPAWN_FAILED, ec.WORKER_PIPE_BROKEN})
SESSION_RESET_TIMEOUT_SEC = 5.0
_SESSION_GATE_BUSY = "Sticky session is busy and request timed out waiting for the session gate."

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
    child: object | None
    last_used: float = field(default_factory=time.monotonic)
    stale_since: float | None = None


@dataclass
class _SessionGate:
    """Placement gate for one shared session id (reference-counted)."""

    lock: threading.Lock
    users: int = 0


@dataclass
class _SharedAdmission:
    """Admission record for a shared session request."""

    worker: BaseProcessWorker | None
    had_binding: bool


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
        self._session_locks: dict[str, _SessionGate] = {}
        self._recycling: set[BaseProcessWorker] = set()
        super().__init__(script_path=_WORKER_SCRIPT, num_workers=eff_num_workers, default_timeout_sec=eff_timeout, max_tasks=eff_max_tasks, worker_name="Formula worker", idle_worker_ttl_sec=eff_idle_ttl, max_payload_bytes=COMPUTE_MAX_PAYLOAD_BYTES)

    def _clear_pool_state_unlocked(self) -> None:
        # A pool kept after shutdown advertises no sessions.
        self._sessions.clear()
        self._session_locks.clear()
        self._recycling.clear()

    @contextmanager
    def _session_gate(self, session_id: str, timeout_sec: float) -> Generator[bool, None, None]:
        """Serialize placement for *session_id* from lookup through worker release.

        Each ``_SessionGate`` counts its holders and waiters under ``self._cond``;
        the lock itself is taken outside ``self._cond``, and the entry is removed
        when ``users`` reaches 0. Yields True only while this caller holds entry.lock.
        Yields False when acquire times out. users is still incremented before the
        wait and decremented in finally, so the holder's entry stays until the holder leaves.
        """
        with self._cond:
            entry = self._session_locks.get(session_id)
            if entry is None:
                entry = _SessionGate(lock=threading.Lock())
                self._session_locks[session_id] = entry
            entry.users += 1
            lock = entry.lock

        # What was wrong: an unbounded Lock.acquire() waited out the holder's whole
        # cell while the waiter already held a listener thread and an admission permit.
        # How it happened: the gate wraps the whole cell (up to max_timeout_sec +
        # IPC grace), so concurrent requests for a busy sticky session tied up
        # server resources.
        # Why this change fixes it: acquire uses this request's clock (timeout_sec);
        # lock is released only when acquired is true, and finally still drops this
        # waiter from users so the holder's entry stays until the holder leaves.
        acquired = lock.acquire(timeout=max(float(timeout_sec), 0.0))
        try:
            yield acquired
        finally:
            if acquired:
                lock.release()
            with self._cond:
                entry.users -= 1
                if entry.users <= 0 and self._session_locks.get(session_id) is entry:
                    self._session_locks.pop(session_id, None)

    def _is_session_live(self, s: _Session) -> bool:
        return (
            s.child is not None
            and s.child is getattr(s.worker, "child", None)
            and self._session_child_live(s.child)
        )

    def _expire_stale_sessions_unlocked(self) -> None:
        """Drop stale session rows that have been stale for longer than idle_worker_ttl_sec.

        A session row is stale when its child is no longer that slot's live child.
        Stale rows are kept so the next call reports session_reset, but must expire
        on idle_worker_ttl_sec so they do not accumulate indefinitely.
        Live rows are unaffected. Caller holds self._cond.
        """
        ttl = self.idle_worker_ttl_sec
        if ttl is None or ttl <= 0:
            return
        now = time.monotonic()
        for sid, s in list(self._sessions.items()):
            if not self._is_session_live(s):
                if s.stale_since is None:
                    s.stale_since = now
                elif now - s.stale_since >= ttl:
                    self._sessions.pop(sid, None)
            else:
                s.stale_since = None

    def _evict_idle_workers(self) -> None:
        with self._cond:
            self._expire_stale_sessions_unlocked()
        super()._evict_idle_workers()

    def _drop_session(self, session_id: str, *, only_if_worker: BaseProcessWorker | None = None) -> None:
        """Drop *session_id* from the active map.

        The pool lock is re-entrant, so callers that already hold ``self._cond``
        can use this. *only_if_worker* leaves the entry when it names a different
        process.
        """
        with self._cond:
            current = self._sessions.get(session_id)
            if current is not None and (only_if_worker is None or current.worker is only_if_worker):
                self._sessions.pop(session_id, None)

    def _worker_session_count(self, worker: BaseProcessWorker) -> int:
        return sum(1 for s in self._sessions.values() if s.worker is worker and self._is_session_live(s))

    def _worker_has_sessions(self, worker: BaseProcessWorker) -> bool:
        return any(s.worker is worker and self._is_session_live(s) for s in self._sessions.values())

    def _worker_sessions_for(self, worker: BaseProcessWorker) -> list[str]:
        return [sid for sid, s in self._sessions.items() if s.worker is worker and self._is_session_live(s)]

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

    def live_session_worker(self, session_id: str) -> BaseProcessWorker | None:
        """The process that still owns *session_id*, or None once that child is gone.

        Test helper. Production code does not call this.
        """
        with self._cond:
            s = self._sessions.get(session_id)
            if s is not None and self._is_session_live(s):
                return s.worker
            return None

    def should_recycle_worker(self, worker: BaseProcessWorker) -> bool:
        """Recycle worker if tasks_executed >= max_tasks, unless holding active shared sessions.

        Workers holding active shared sessions skip normal max_tasks recycling to preserve state.
        """
        with self._cond:
            recycle = worker.is_alive() and not self._worker_has_sessions(worker) and worker.tasks_executed >= self.max_tasks
            if recycle:
                self._recycling.add(worker)
            else:
                self._recycling.discard(worker)
            return recycle

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
        control-plane reset request. The gate wait uses timeout_sec, then the lease
        uses timeout_sec again.
        """
        try:
            validate_session_id(session_id)
        except ExecuteRequestError as exc:
            return error_dict(ec.INVALID_REQUEST, str(exc))

        with self._session_gate(session_id, timeout_sec) as entered:
            if not entered:
                return error_dict(ec.WORKER_POOL_BUSY, _SESSION_GATE_BUSY)
            with self._cond:
                s = self._sessions.get(session_id)
                if s is None:
                    return {"status": "ok"}
                if not self._is_session_live(s):
                    self._drop_session(session_id)
                    return {"status": "ok"}
                worker = s.worker

            with self.leased(worker, timeout_sec=timeout_sec) as leased:
                if leased is None:
                    return error_dict(ec.WORKER_POOL_BUSY, "Could not lease worker to reset session.")
                res = leased.execute({"action": "reset_session", "session_id": session_id}, timeout_sec=timeout_sec)
                # A non-ok reset can mean the worker still holds the namespace.
                if res.get("status") == "ok":
                    self._drop_session(session_id, only_if_worker=leased)
                else:
                    log.error("reset_session failed for %s; keeping session map because the worker may still hold the namespace: %s", session_id, res)
                return res

    def check_dependencies(self, packages: list[str] | None = None, timeout_sec: float = 10.0) -> tuple[bool, str | None]:
        """Ask an idle worker to verify required dependencies (e.g. numpy, sympy).

        Returns (success, error_message).
        """
        if self._is_shutdown or not self.workers:
            return False, "Formula compute pool is not running."

        # No matplotlib: importing it can take up to 5 seconds at startup, and a broken install shows up when plotting.
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

    def _select_shared_worker(self, session_id: str) -> BaseProcessWorker | None:
        """Select target worker for a shared session under self._cond."""
        with self._cond:
            sess = self._sessions.get(session_id)
            if sess is not None:
                if sess.worker not in self.workers:
                    # execute() checks self.workers outside _cond. shutdown()
                    # clears that list under _cond, so this can still fire.
                    # Pop the row; do not set stale_since: the caller reserves again on a live worker.
                    self._drop_session(session_id)
                    sess = None
                else:
                    sess.last_used = time.monotonic()

            if sess is not None:
                return sess.worker
            return self._pick_new_shared_worker()

    def _admit_shared_session(self, session_id: str) -> _SharedAdmission:
        """Admit a shared session request and record admission state."""
        with self._cond:
            had_binding = session_id in self._sessions
            target_worker = self._select_shared_worker(session_id)
        return _SharedAdmission(worker=target_worker, had_binding=had_binding)

    def _run_execution(self, leased: BaseProcessWorker, payload: dict[str, Any], clock: Deadline, req_id: str | None, decode_result: bool, expect_namespace: bool = False) -> dict[str, Any]:
        """Send execution payload to leased worker and format response.

        The caller already checked *clock* after the lease. The child alarm is
        the remaining budget rounded up, and the host waits alarm + grace so
        the alarm, not a SIGKILL, ends a long cell.
        """
        child_alarm = math.ceil(clock.run_for())
        payload["timeout_sec"] = child_alarm
        if expect_namespace:
            payload["expect_namespace"] = True
        host_timeout = child_alarm + HOST_IPC_READ_GRACE_SEC
        res = leased.execute(payload, timeout_sec=host_timeout, req_id=req_id)
        created = bool(res.get("namespace_created")) if isinstance(res, dict) else False
        if expect_namespace and created and isinstance(res, dict):
            if "result_json" not in res:
                res["session_reset"] = True
        if decode_result and isinstance(res, dict):
            try:
                decoded = decode_worker_result(res)
            except (json.JSONDecodeError, UnicodeDecodeError, RecursionError, MemoryError) as exc:
                # A bad result blob is a decode failure, not a crash: the child stays up.
                failed = error_dict(ec.RESULT_DECODE_ERROR, f"Worker result_json could not be decoded: {exc}", req_id=req_id)
                if expect_namespace and created:
                    failed["session_reset"] = True
                return failed
            if expect_namespace and created and isinstance(decoded, dict):
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
    ) -> None:
        """Record the session (the only insert into ``_sessions``) after a cell ran, then release *leased*."""
        try:
            with self._cond:
                result_code = result.get("code") if isinstance(result, dict) else None
                if (
                    mode == "shared"
                    and session_id
                    and result is not None
                    and result_code not in _UNRUN_RESULT_CODES
                ):
                    now = time.monotonic()
                    sess = self._sessions.get(session_id)
                    # If child is None, the session is registered as stale so the next call reports session_reset.
                    if sess is not None:
                        sess.worker = leased
                        sess.child = leased.child
                        sess.last_used = now
                        if leased.child is None:
                            if sess.stale_since is None:
                                sess.stale_since = now
                        else:
                            sess.stale_since = None
                    else:
                        self._sessions[session_id] = _Session(
                            worker=leased,
                            child=leased.child,
                            last_used=now,
                            stale_since=now if leased.child is None else None,
                        )
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
        decode_result: bool = True,
        deadline: Deadline | None = None,
    ) -> dict[str, Any]:
        """Execute formula code on an appropriate worker subprocess.

        *deadline* is the HTTP admission clock. Without one the clock is
        *timeout_sec* (or the default) from now. It is checked once before
        the lease and once after.

        The one payload is raw ``data_json`` bytes. The worker dumps the
        result once. The HTTP server sets ``decode_result=False`` so it can
        forward ``result_json`` without a second dumps. A ``data`` object is
        only a convenience for in-process callers: it is JSON-encoded into
        ``data_json`` here, not packed as a second wire.
        """
        if self._is_shutdown or not self.workers:
            return error_dict(ec.SERVICE_SHUTDOWN, "Formula compute pool is shutting down.", req_id=req_id)

        try:
            if session_id:
                session_id = validate_session_id(session_id)
            mode = derive_execute_mode(session_id)
        except ExecuteRequestError as exc:
            return error_dict(ec.INVALID_REQUEST, str(exc), req_id=req_id)

        # Only None means "use the default". Zero is an explicit timeout.
        clock = deadline if deadline is not None else Deadline(float(self.default_timeout_sec if timeout_sec is None else timeout_sec))
        # Before select, so a miss does not reserve a session or consume the
        # lost-session marker.
        if clock.usable() is None:
            return error_dict(ec.QUEUE_TIMEOUT, "Request deadline expired before a worker lease.", req_id=req_id)

        try:
            payload = self._build_execute_payload(code=code, data=data, data_json=data_json, session_id=session_id, mode=mode, init_script=init_script, req_id=req_id)
        except ExecuteRequestError as exc:
            return error_dict(ec.INVALID_REQUEST, str(exc), req_id=req_id)

        gate = (
            self._session_gate(session_id, max(clock.left(), 0.0))
            if mode == "shared" and session_id
            else nullcontext(True)  # None would look like a gate miss
        )
        with gate as entered:
            if not entered:
                return error_dict(ec.WORKER_POOL_BUSY, _SESSION_GATE_BUSY, req_id=req_id)
            leased: BaseProcessWorker | None = None
            admission: _SharedAdmission | None = None
            if mode == "shared" and session_id:
                admission = self._admit_shared_session(session_id)
                if admission.worker is None:
                    return error_dict(ec.SERVICE_SHUTDOWN, "Formula compute pool is shutting down.", req_id=req_id)
                lease_budget = max(clock.left(), 0.0)
                leased = self.lease_specific(admission.worker, timeout_sec=lease_budget)
                if leased is not None:
                    with self._cond:
                        row = self._sessions.get(session_id)
                        if row is not None:
                            assert row.worker is leased, "shared session row worker must match leased worker"
                busy_err = "Sticky session worker is busy and request timed out waiting for worker lease."
            else:
                lease_budget = max(clock.left(), 0.0)
                leased = self.lease_any(timeout_sec=lease_budget)
                busy_err = "All formula workers are currently busy and request timed out waiting for worker lease."

            if leased is None:
                return error_dict(ec.WORKER_POOL_BUSY, busy_err, req_id=req_id)

            result: dict[str, Any] | None = None
            try:
                # The lease wait spent part of the clock.
                if clock.usable() is None:
                    result = error_dict(ec.QUEUE_TIMEOUT, "Request deadline expired before a worker lease.", req_id=req_id)
                    return result
                result = self._run_execution(
                    leased,
                    payload=payload,
                    clock=clock,
                    req_id=req_id,
                    decode_result=decode_result,
                    expect_namespace=(mode == "shared" and admission is not None and admission.had_binding),
                )
                return result
            finally:
                self._finalize_session(leased, session_id, mode, result=result)

    @staticmethod
    def _build_execute_payload(*, code: str, data: Any = None, data_json: bytes | None = None, session_id: str | None = None, mode: str = "isolated", init_script: str | None = None, req_id: str | None = None) -> dict[str, Any]:
        # pickle is not a second payload. timeout_sec is set later by
        # _run_execution from the remaining budget.
        payload: dict[str, Any] = {"id": req_id, "code": code, "session_id": session_id, "mode": mode, "init_script": init_script}
        blob = data_json
        if blob is None and data is not None:
            # In-process callers only; HTTP supplies data_json, which is forwarded unvalidated.
            try:
                blob = json.dumps(data, allow_nan=False).encode("utf-8")
            except (TypeError, ValueError) as exc:
                raise ExecuteRequestError(f"data is not JSON-serializable: {exc}") from exc
        if blob is not None:
            payload["data_json"] = bytes(blob)
        return payload


# Global singleton per server process
_POOL_SINGLETON: PoolSingleton[FormulaProcessPool] = PoolSingleton()


def _new_formula_pool(settings: ComputeSettings | None = None) -> FormulaProcessPool:
    return FormulaProcessPool(settings=settings)


get_formula_pool, shutdown_formula_pool = bind_pool_singleton(_POOL_SINGLETON, _new_formula_pool)
