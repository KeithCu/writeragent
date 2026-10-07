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
- Periodic worker memory recycling (after max_tasks)
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
import json
import logging
import os
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import threading

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
from compute_service.worker_base import BaseProcessPool, BaseProcessWorker, PoolSingleton, remaining_sec, resolve_override
from plugin.scripting.config_limits import HOST_IPC_READ_GRACE_SEC

log = logging.getLogger("compute_service.formula")

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_WORKER_SCRIPT = os.path.join(_SCRIPT_DIR, "formula_worker.py")


@dataclass
class _Session:
    worker: BaseProcessWorker
    pid: int | None
    last_active: float


class _SessionDict(dict[str, _Session]):
    """Dict for _sessions that keeps a per-worker session index in sync."""

    _worker_sessions: dict[BaseProcessWorker, set[str]]

    def __init__(self, worker_sessions: dict[BaseProcessWorker, set[str]]) -> None:
        super().__init__()
        self._worker_sessions = worker_sessions

    def __setitem__(self, key: str, value: _Session) -> None:
        old = self.get(key)
        if old is not None and old.worker is not value.worker:
            old_set = self._worker_sessions.get(old.worker)
            if old_set is not None:
                old_set.discard(key)
                if not old_set:
                    self._worker_sessions.pop(old.worker, None)
        super().__setitem__(key, value)
        self._worker_sessions.setdefault(value.worker, set()).add(key)

    def __delitem__(self, key: str) -> None:
        old = self.get(key)
        super().__delitem__(key)
        if old is not None:
            old_set = self._worker_sessions.get(old.worker)
            if old_set is not None:
                old_set.discard(key)
                if not old_set:
                    self._worker_sessions.pop(old.worker, None)

    def pop(self, key: object, *args: Any) -> Any:
        if not isinstance(key, str):
            if args:
                return args[0]
            raise KeyError(key)
        old = self.get(key)
        res = super().pop(key, *args)
        if old is not None and key not in self:
            old_set = self._worker_sessions.get(old.worker)
            if old_set is not None:
                old_set.discard(key)
                if not old_set:
                    self._worker_sessions.pop(old.worker, None)
        return res

    def clear(self) -> None:
        super().clear()
        self._worker_sessions.clear()


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

        # Single source of truth for session tracking with per-worker index
        self._worker_sessions: dict[BaseProcessWorker, set[str]] = {}
        self._sessions: dict[str, _Session] = _SessionDict(self._worker_sessions)
        self._lost_sessions: OrderedDict[str, float] = OrderedDict()
        self._max_lost_sessions: int = 1000
        self.shared_kernel_ttl_sec = eff_shared_ttl
        super().__init__(script_path=_WORKER_SCRIPT, num_workers=eff_num_workers, default_timeout_sec=eff_timeout, max_tasks=eff_max_tasks, worker_name="Formula worker", idle_worker_ttl_sec=eff_idle_ttl, max_payload_bytes=COMPUTE_MAX_PAYLOAD_BYTES, on_process_exit=self._on_process_exit)
        self._session_reaper_thread: threading.Thread | None = None

        if self.shared_kernel_ttl_sec > 0:
            self._start_session_ttl_reaper()

    def _start_session_ttl_reaper(self) -> None:
        interval = max(0.02, min(self.shared_kernel_ttl_sec / 6.0, 300.0))
        self._session_reaper_thread = self._start_reaper(
            name="formula-session-reaper",
            interval=interval,
            fn=self._evict_stale_sessions,
        )

    def _evict_stale_sessions(self, ttl_sec: float | None = None) -> None:
        if self._is_shutdown:
            return
        ttl = self.shared_kernel_ttl_sec if ttl_sec is None else ttl_sec
        now = time.monotonic()
        stale: list[tuple[str, BaseProcessWorker]] = []
        with self._cond:
            for sid, s in list(self._sessions.items()):
                if now - s.last_active >= ttl:
                    stale.append((sid, s.worker))
        evicted: list[str] = []
        for sid, worker in stale:
            # Bound lease wait to 2.0s so the session reaper thread does not stall
            # if the worker is busy running a cell; it will retry on the next tick.
            # Lease before pop — see reset_session docstring for the TOCTOU race.
            with self.leased(worker, timeout_sec=2.0) as leased:
                if leased is None:
                    log.debug("TTL eviction skipped for %s: worker is currently leased", sid)
                    continue
                with self._cond:
                    sess = self._sessions.get(sid)
                    still_stale = sess is not None and sess.worker is worker and (time.monotonic() - sess.last_active) >= ttl
                if not still_stale:
                    continue
                res = self._reset_session_on_worker(leased, sid, timeout_sec=2.0)
                with self._cond:
                    sid_active = sid in self._sessions
                if res.get("status") == "ok" or not sid_active:
                    evicted.append(sid)
                    self._forget_session(sid, lost=True)
        if evicted:
            log.info("Session TTL reaper evicted %d idle session(s): %s", len(evicted), evicted)

    def _mark_session_lost_unlocked(self, session_id: str) -> None:
        self._lost_sessions[session_id] = time.monotonic()
        while len(self._lost_sessions) > self._max_lost_sessions:
            self._lost_sessions.popitem(last=False)

    def _mark_session_lost(self, session_id: str) -> None:
        with self._cond:
            self._mark_session_lost_unlocked(session_id)

    def _forget_session_unlocked(self, session_id: str, *, lost: bool = True) -> None:
        """Drop a session from the active session map and optionally record it as lost."""
        if lost:
            self._mark_session_lost_unlocked(session_id)
        self._drop_one_unlocked(session_id)

    def _forget_session(self, session_id: str, *, lost: bool = True) -> None:
        with self._cond:
            self._forget_session_unlocked(session_id, lost=lost)

    def _worker_session_count(self, worker: BaseProcessWorker) -> int:
        return len(self._worker_sessions.get(worker, ()))

    def _worker_has_sessions(self, worker: BaseProcessWorker) -> bool:
        return bool(self._worker_sessions.get(worker))

    def _worker_sessions_for(self, worker: BaseProcessWorker) -> list[str]:
        return list(self._worker_sessions.get(worker, ()))

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
            self._forget_session_unlocked(sid, lost=True)
        if stale:
            log.info("Dropped %d shared session(s) with exited pid=%s", len(stale), pid)

    def _drop_one_unlocked(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)

    def _reap_dead_sessions_unlocked(self) -> None:
        """Invalidate the session cache against the live pid.

        An external SIGKILL does not enter kill(); poll() reaps that pid
        and every session that named it is dropped. A wrapper that respawned
        under a new pid is not the old session.
        """
        for sid, s in list(self._sessions.items()):
            worker = s.worker
            proc = worker.process
            if proc is None or proc.poll() is not None:
                self._forget_session_unlocked(sid, lost=True)
            elif s.pid is not None and s.pid != proc.pid:
                self._forget_session_unlocked(sid, lost=True)

    def live_session_worker(self, session_id: str) -> BaseProcessWorker | None:
        """The process that still owns *session_id*, or None once that pid has exited."""
        with self._cond:
            self._reap_dead_sessions_unlocked()
            s = self._sessions.get(session_id)
            return s.worker if s is not None else None

    def _clear_worker_sessions_unlocked(self, worker: BaseProcessWorker) -> None:
        for sid in self._worker_sessions_for(worker):
            self._drop_one_unlocked(sid)

    def _skip_idle_evict(self, worker: BaseProcessWorker) -> bool:
        """Keep a shared kernel past the idle TTL.

        Idle TTL and session TTL are independent and both default to 3600s.
        Killing a worker that still holds sessions dropped every workbook on
        it and left ``_active_sessions`` pointing at a dead process. The next
        sticky call respawned an empty kernel that still looked live, so
        ``max_tasks`` recycle stayed blocked. Session TTL already resets
        those namespaces.
        """
        return self._worker_has_sessions(worker)

    def _drop_session_if_worker(self, session_id: str, worker: BaseProcessWorker) -> None:
        """Pop maps only when they still name *worker*."""
        with self._cond:
            s = self._sessions.get(session_id)
            if s is not None and s.worker is worker:
                self._drop_one_unlocked(session_id)

    def _drop_session_after_reset(self, session_id: str, worker: BaseProcessWorker, res: dict[str, Any]) -> bool:
        """Forget *session_id* only when the worker reset reports ``ok``.

        What was wrong: ``_evict_stale_sessions`` and ``reset_session`` called
        ``_drop_session_if_worker`` after every completed reset, including a
        response whose ``status`` was not ``ok``. How: the worker can still
        hold the namespace while the supervisor forgets the id, so the next
        sticky cell looks new on a kernel that is not empty. Why: drop the
        map only when ``res["status"] == "ok"``; a failed reset is logged
        and the map stays.
        """
        if res.get("status") == "ok":
            self._drop_session_if_worker(session_id, worker)
            return True
        log.error("reset_session failed for %s; keeping session map because the worker may still hold the namespace: %s", session_id, res)
        return False

    def _reset_session_on_worker(self, worker: BaseProcessWorker, session_id: str, timeout_sec: float = 5.0) -> dict[str, Any]:
        """Send reset_session action to leased worker and update session map on ok."""
        try:
            res = worker.execute({"action": "reset_session", "session_id": session_id}, timeout_sec=timeout_sec)
        except Exception:
            log.exception("reset_session IPC failed for %s; killing worker", session_id)
            worker.kill()
            with self._cond:
                self._clear_worker_sessions_unlocked(worker)
            return {"status": "error", "code": "WORKER_CRASHED", "error": "Worker failed during session reset"}
        self._drop_session_after_reset(session_id, worker, res)
        return res

    def should_recycle_worker(self, worker: BaseProcessWorker) -> bool:
        """Recycle worker if tasks_executed >= max_tasks, unless holding active shared sessions.

        Workers holding active shared sessions skip normal max_tasks recycling to preserve state.
        Sessions are held indefinitely while active and released after shared_kernel_ttl_sec of inactivity.
        """
        with self._cond:
            if not worker.is_alive():
                for sid in self._worker_sessions_for(worker):
                    self._forget_session_unlocked(sid, lost=True)
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
        for worker in list(self._idle):
            if not worker.is_alive():
                self._idle.discard(worker)
        if not self._idle:
            return None
        clean_workers = [w for w in self._idle if not self._worker_has_sessions(w)]
        if clean_workers:
            chosen = clean_workers[0]
        else:
            chosen = min(self._idle, key=lambda w: self._worker_session_count(w))
        self._idle.remove(chosen)
        return chosen

    def reset_session(self, session_id: str, timeout_sec: float = 5.0) -> dict[str, Any]:
        """Drop the shared sandbox + init companion for *session_id*.

        HTTP ``POST /v1/session/reset`` calls this (do not add a second reset
        path). Unknown / already-gone ids are idempotent ``ok``. The session
        map is dropped only when the worker reset returns ``status`` ``ok``.
        A failed reset is logged and the map stays, because that process may
        still hold the namespace. TTL eviction in ``_evict_stale_sessions``
        stays the safety net if reset is missed. Default timeout_sec=5.0
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
            # Explicit reset: discard from lost set so next call gets a clean kernel without session_reset: true
            self._lost_sessions.pop(session_id, None)

        if worker is None:
            return {"status": "ok"}

        with self.leased(worker, timeout_sec=timeout_sec) as leased:
            if leased is None:
                # Same status/code/error shape as execute pool-busy; HTTP maps to 503.
                return {"status": "error", "code": "WORKER_POOL_BUSY", "error": "Could not lease worker to reset session."}
            return self._reset_session_on_worker(leased, session_id, timeout_sec=timeout_sec)

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
        """
        with self._cond:
            self._reap_dead_sessions_unlocked()
            session_was_lost = bool(self._lost_sessions.pop(session_id, None))
            sess = self._sessions.get(session_id)
            if sess is not None and sess.worker not in self.workers:
                self._drop_one_unlocked(session_id)
                sess = None

            is_new_session = (sess is None)
            if sess is not None:
                target_worker = sess.worker
            elif self.workers:
                # Distribute new shared sessions across workers by choosing the worker
                # currently hosting the fewest active sessions. Prefer idle workers
                # among ties to distribute load evenly, using hash as final tie-breaker.
                target_worker = min(
                    self.workers,
                    key=lambda w: (
                        self._worker_session_count(w),
                        0 if w in self._idle else 1,
                        abs(hash((session_id, w.worker_id))),
                    ),
                )
                # Reserve session->worker at pick time inside the same with self._cond!
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

    def _run_execution(
        self,
        leased: BaseProcessWorker,
        payload: dict[str, Any],
        deadline: float,
        session_was_lost: bool,
        req_id: str | None,
        decode_result: bool,
    ) -> dict[str, Any]:
        """Send execution payload to leased worker and format response."""
        # The child used to get the original full timeout while this read
        # used only the time left. signal.alarm never won, so a normal
        # sleep became SIGKILL and dropped every shared session on that
        # process. Give the child the remaining budget and wait at least
        # alarm + grace so a cell finishing between them returns a clean
        # timeout instead of a SIGKILL.
        child_budget = remaining_sec(deadline)
        child_alarm = max(1, int(child_budget))
        payload["timeout_sec"] = child_alarm
        payload["session_reset"] = session_was_lost
        host_timeout = max(child_budget, float(child_alarm)) + HOST_IPC_READ_GRACE_SEC
        res = leased.execute(payload, timeout_sec=host_timeout)
        if session_was_lost and isinstance(res, dict):
            res["session_reset"] = True
        if req_id is not None and isinstance(res, dict):
            res["id"] = req_id
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
                proc = leased.process
                if mode == "shared" and session_id and leased.is_alive() and proc is not None and proc.poll() is None:
                    sess = self._sessions.get(session_id)
                    if sess is not None and sess.worker is leased:
                        sess.pid = proc.pid
                        sess.last_active = time.monotonic()
                    else:
                        # Bugfix: pop _lost_sessions[session_id] when the session is re-added (Bug 4).
                        # What was wrong: a concurrent dead-session reap could mark a newly starting
                        # session lost while running; re-adding it left the lost marker intact.
                        # Why this change: ensures subsequent requests are not falsely sent session_reset=True.
                        self._sessions[session_id] = _Session(worker=leased, pid=proc.pid, last_active=time.monotonic())
                    self._lost_sessions.pop(session_id, None)
                self._reap_dead_sessions_unlocked()
                if mode == "shared" and session_id and not leased.is_alive():
                    sess = self._sessions.get(session_id)
                    if sess is not None and sess.worker is leased:
                        self._forget_session_unlocked(session_id, lost=True)
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
            return {"id": req_id, "status": "error", "code": "SERVICE_SHUTDOWN", "error": "Formula compute pool is shutting down."}

        # One schema. A mode or wire the HTTP layer missed must not be
        # rewritten and run: that returned 200 for a kernel that kept no state.
        try:
            mode = canonical_execute_mode(mode)
            wire = require_execute_wire(wire)
            if session_id:
                validate_session_id(session_id)
        except ExecuteRequestError as exc:
            return {"id": req_id, "status": "error", "code": "INVALID_REQUEST", "error": str(exc)}

        eff_timeout = float(timeout_sec or self.default_timeout_sec)
        # The HTTP handler passes the accept-time deadline. Starting a fresh
        # clock here used to give the child the original full timeout after
        # the request had already waited in the queue.
        if deadline is None:
            deadline = time.monotonic() + eff_timeout
        if time.monotonic() >= deadline:
            return {"id": req_id, "status": "error", "code": "QUEUE_TIMEOUT", "error": "Request deadline expired before a worker lease."}

        try:
            payload = self._build_execute_payload(code=code, data=data, data_json=data_json, session_id=session_id, mode=mode, timeout_sec=max(1, int(eff_timeout)), init_script=init_script, req_id=req_id, wire=wire)
        except ExecuteRequestError as exc:
            return {"id": req_id, "status": "error", "code": "INVALID_REQUEST", "error": str(exc)}

        leased: BaseProcessWorker | None
        session_was_lost = False
        is_new_session = False
        if mode == "shared" and session_id:
            target_worker, session_was_lost, is_new_session = self._select_shared_worker(session_id)
            if target_worker is None:
                return {"id": req_id, "status": "error", "code": "SERVICE_SHUTDOWN", "error": "Formula compute pool is shutting down."}
            leased = self.lease_specific(target_worker, timeout_sec=remaining_sec(deadline))
            busy_err = "Sticky session worker is busy and request timed out waiting for worker lease."
        else:
            leased = self.lease_any(timeout_sec=remaining_sec(deadline))
            busy_err = "All formula workers are currently busy and request timed out waiting for worker lease."

        if leased is None:
            if session_id:
                with self._cond:
                    if is_new_session:
                        # Bugfix: drop newly reserved session when lease_specific fails (Bug 2).
                        # What was wrong: reservation stayed in _sessions if lease timed out,
                        # creating a phantom session preventing worker recycling/idle eviction.
                        # Why this change: clean up unleased reservations immediately.
                        self._drop_one_unlocked(session_id)
                    if session_was_lost:
                        self._mark_session_lost_unlocked(session_id)
            return {"id": req_id, "status": "error", "code": "WORKER_POOL_BUSY", "error": busy_err}

        try:
            return self._run_execution(
                leased,
                payload=payload,
                deadline=deadline,
                session_was_lost=session_was_lost,
                req_id=req_id,
                decode_result=decode_result,
            )
        finally:
            self._finalize_session(leased, session_id, mode)

    @staticmethod
    def _build_execute_payload(*, code: str, data: Any = None, data_json: bytes | None = None, session_id: str | None = None, mode: str = "isolated", timeout_sec: int = 30, init_script: str | None = None, req_id: str | None = None, wire: str = WIRE_JSON_FORWARD) -> dict[str, Any]:
        # Unknown wire used to be rewritten to json_forward and the cell ran.
        # ``pickle`` was a second payload (host_pack_data / split_grid) on the
        # same stdio envelope. It is rejected, not packed and not rewritten.
        eff_wire = require_execute_wire(wire)
        payload: dict[str, Any] = {"id": req_id, "code": code, "session_id": session_id, "mode": mode, "timeout_sec": timeout_sec, "init_script": init_script, "wire": eff_wire}
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


def shutdown_formula_pool() -> None:
    """Shut down the global formula process pool."""
    _POOL_SINGLETON.shutdown()
