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
- Exclusive occupancy so sticky and isolated jobs never share a worker concurrently
- Periodic worker memory recycling (after max_tasks)
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any

from compute_service.config import ComputeSettings
from compute_service.json_forward import COMPUTE_MAX_PAYLOAD_BYTES, WIRE_JSON_FORWARD, WIRE_PICKLE, decode_worker_result
from compute_service.worker_base import BaseProcessPool, BaseProcessWorker, PoolSingleton, remaining_sec, resolve_override
from plugin.scripting.config_limits import HOST_IPC_READ_GRACE_SEC

log = logging.getLogger("compute_service.formula")

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_WORKER_SCRIPT = os.path.join(_SCRIPT_DIR, "formula_worker.py")


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

        super().__init__(script_path=_WORKER_SCRIPT, num_workers=eff_num_workers, default_timeout_sec=eff_timeout, max_tasks=eff_max_tasks, worker_name="Formula worker", idle_worker_ttl_sec=eff_idle_ttl, max_payload_bytes=COMPUTE_MAX_PAYLOAD_BYTES)
        self._active_sessions: dict[str, BaseProcessWorker] = {}
        self._worker_sessions: dict[BaseProcessWorker, set[str]] = {}
        self._session_last_activity: dict[str, float] = {}
        self.shared_kernel_ttl_sec = eff_shared_ttl
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

    def _evict_stale_sessions(self) -> None:
        if self._is_shutdown:
            return
        now = time.monotonic()
        stale: list[tuple[str, BaseProcessWorker]] = []
        with self._cond:
            for sid, last_active in list(self._session_last_activity.items()):
                if now - last_active >= self.shared_kernel_ttl_sec:
                    worker = self._active_sessions.get(sid)
                    if worker is not None:
                        stale.append((sid, worker))
        evicted: list[str] = []
        for sid, worker in stale:
            # Bound lease wait to 2.0s so the session reaper thread does not stall
            # if the worker is busy running a cell; it will retry on the next tick.
            # Lease before pop — see reset_session docstring for the TOCTOU race.
            leased = self.lease_specific(worker, timeout_sec=2.0)
            if leased is None:
                log.debug("TTL eviction skipped for %s: worker is currently leased", sid)
                continue
            try:
                with self._cond:
                    refreshed = self._session_last_activity.get(sid)
                    mapped = self._active_sessions.get(sid)
                    still_stale = mapped is worker and refreshed is not None and (time.monotonic() - refreshed) >= self.shared_kernel_ttl_sec
                if not still_stale:
                    continue
                try:
                    leased.execute({"action": "reset_session", "session_id": sid}, timeout_sec=2.0)
                except Exception:
                    log.exception("TTL reset_session failed for %s; killing worker", sid)
                    leased.kill()
                    with self._cond:
                        self._clear_worker_sessions_unlocked(leased)
                self._drop_session_if_worker(sid, leased)
                evicted.append(sid)
            finally:
                self.release_worker(leased)
        if evicted:
            log.info("Session TTL reaper evicted %d idle session(s): %s", len(evicted), evicted)

    def _clear_worker_sessions_unlocked(self, worker: BaseProcessWorker) -> None:
        sessions = self._worker_sessions.pop(worker, set())
        for sid in sessions:
            self._active_sessions.pop(sid, None)
            self._session_last_activity.pop(sid, None)

    def _skip_idle_evict(self, worker: BaseProcessWorker) -> bool:
        """Keep a shared kernel past the idle TTL.

        Idle TTL and session TTL are independent and both default to 3600s.
        Killing a worker that still holds sessions dropped every workbook on
        it and left ``_active_sessions`` pointing at a dead process. The next
        sticky call respawned an empty kernel that still looked live, so
        ``max_tasks`` recycle stayed blocked. Session TTL already resets
        those namespaces.
        """
        return bool(self._worker_sessions.get(worker))

    def _drop_session_if_worker(self, session_id: str, worker: BaseProcessWorker) -> None:
        """Pop maps only when they still name *worker*."""
        with self._cond:
            if self._active_sessions.get(session_id) is not worker:
                return
            self._session_last_activity.pop(session_id, None)
            self._active_sessions.pop(session_id, None)
            sessions = self._worker_sessions.get(worker)
            if sessions is not None:
                sessions.discard(session_id)
                if not sessions:
                    del self._worker_sessions[worker]

    def should_recycle_worker(self, worker: BaseProcessWorker) -> bool:
        """Recycle worker if tasks_executed >= max_tasks, unless holding active shared sessions.

        Workers holding active shared sessions skip normal max_tasks recycling to preserve state.
        Sessions are held indefinitely while active and released after shared_kernel_ttl_sec of inactivity.

        NOTE: Must NOT be called with self._cond or self._lock held by the caller,
        as this method acquires self._lock internally (prevents lock inversion/deadlock).
        """
        with self._lock:
            if not worker.is_alive():
                self._clear_worker_sessions_unlocked(worker)
                return False
            has_sessions = bool(self._worker_sessions.get(worker))

        if has_sessions:
            return False

        return worker.tasks_executed >= self.max_tasks

    def _pick_idle_worker(self) -> BaseProcessWorker | None:
        """Pop an idle worker, preferring workers hosting zero active shared sessions.

        Isolates long-lived shared sessions so stateless/isolated workloads that
        hang or crash do not cause SIGKILL of a worker hosting user spreadsheet state.
        Caller holds self._cond.
        """
        if not self._idle:
            return None
        clean_workers = [w for w in self._idle if not self._worker_sessions.get(w)]
        if clean_workers:
            chosen = clean_workers[0]
            self._idle.remove(chosen)
            return chosen
        chosen = min(self._idle, key=lambda w: len(self._worker_sessions.get(w, ())))
        self._idle.remove(chosen)
        return chosen

    def reset_session(self, session_id: str, timeout_sec: float = 5.0) -> dict[str, Any]:
        """Drop the shared sandbox + init companion for *session_id*.

        HTTP ``POST /v1/session/reset`` calls this (do not add a second reset
        path). Unknown / already-gone ids are idempotent ``ok``. TTL eviction
        in ``_evict_stale_sessions`` stays the safety net if reset is missed.
        Default timeout_sec=5.0 gives an in-progress calculation time to complete
        before failing the control-plane reset request.
        """
        # Do not pop the map before the lease. The old order let a concurrent
        # execute re-register the session and run a cell, then this reset
        # wiped the kernel while the map still pointed at the worker. A lease
        # miss also left the map already gone, so max_tasks recycle could
        # kill the process anyway.
        with self._cond:
            worker = self._active_sessions.get(session_id)

        if worker is None:
            return {"status": "ok"}

        leased = self.lease_specific(worker, timeout_sec=timeout_sec)
        if leased is None:
            # Same status/code/error shape as execute pool-busy; HTTP maps to 503.
            return {"status": "error", "code": "WORKER_POOL_BUSY", "error": "Could not lease worker to reset session."}
        try:
            res = leased.execute({"action": "reset_session", "session_id": session_id}, timeout_sec=timeout_sec)
            self._drop_session_if_worker(session_id, leased)
            return res
        finally:
            self.release_worker(leased)

    def check_dependencies(self, packages: list[str] | None = None, timeout_sec: float = 10.0) -> tuple[bool, str | None]:
        """Ask an idle worker to verify required dependencies (e.g. numpy, sympy).

        Returns (success, error_message).
        """
        if self._is_shutdown or not self.workers:
            return False, "Formula compute pool is not running."

        target_packages = ["numpy", "sympy"] if packages is None else packages
        leased = self.lease_any(timeout_sec=timeout_sec)
        if leased is None:
            return False, "Failed to lease a formula worker subprocess for dependency check."

        try:
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
        finally:
            self.release_worker(leased)

    def execute(
        self, code: str, data: Any = None, session_id: str | None = None, timeout_sec: int | None = None, *, mode: str = "isolated", init_script: str | None = None, req_id: str | None = None, data_json: bytes | None = None, wire: str = WIRE_JSON_FORWARD, decode_result: bool = True
    ) -> dict[str, Any]:
        """Execute formula code on an appropriate worker subprocess.

        Default *wire* is JSON-forward (compute HTTP): raw ``data_json`` bytes
        go to the worker; the worker dumps the result once. Pass
        ``wire="pickle"`` for the LibrePy-style ``host_pack_data`` /
        ``split_grid`` path. The HTTP server sets ``decode_result=False`` so
        it can forward ``result_json`` without a second dumps.
        """
        if self._is_shutdown or not self.workers:
            return {"id": req_id, "status": "error", "code": "SERVICE_SHUTDOWN", "error": "Formula compute pool is shutting down."}

        eff_timeout = float(timeout_sec or self.default_timeout_sec)
        deadline = time.monotonic() + eff_timeout

        payload = self._build_execute_payload(code=code, data=data, data_json=data_json, session_id=session_id, mode=mode, timeout_sec=int(eff_timeout), init_script=init_script, req_id=req_id, wire=wire)

        leased: BaseProcessWorker | None
        # Snapshot workers under the pool lock to avoid a TOCTOU race with
        # concurrent shutdown() which calls self.workers.clear() under the same lock.
        # Without the snapshot, the IndexError window between len() and [] access
        # is real even on CPython when shutdown races execute on another thread.
        with self._lock:
            workers_snapshot = list(self.workers)
        busy_code = "WORKER_POOL_BUSY"
        if mode == "shared" and session_id and workers_snapshot:
            with self._cond:
                target_worker = self._active_sessions.get(session_id)
            if target_worker is not None and target_worker not in workers_snapshot:
                target_worker = None
            if target_worker is None:
                # Distribute new shared sessions across workers by choosing the worker
                # currently hosting the fewest active sessions, using hash as tie-breaker.
                with self._cond:
                    target_worker = min(
                        workers_snapshot,
                        key=lambda w: (len(self._worker_sessions.get(w, ())), abs(hash((session_id, w.worker_id)))),
                    )
            leased = self.lease_specific(target_worker, timeout_sec=remaining_sec(deadline))
            busy_err = "Sticky session worker is busy and request timed out waiting for worker lease."
        else:
            leased = self.lease_any(timeout_sec=remaining_sec(deadline))
            busy_err = "All formula workers are currently busy and request timed out waiting for worker lease."

        if leased is None:
            return {"id": req_id, "status": "error", "code": busy_code, "error": busy_err}

        try:
            # The child used to get the original full timeout while this read
            # used only the time left. signal.alarm never won, so a normal
            # sleep became SIGKILL and dropped every shared session on that
            # process. Give the child the remaining budget and wait the
            # LibrePy grace so the alarm returns an error and the process stays up.
            child_budget = remaining_sec(deadline)
            payload["timeout_sec"] = max(1, int(child_budget))
            res = leased.execute(payload, timeout_sec=child_budget + HOST_IPC_READ_GRACE_SEC)
            if req_id is not None and isinstance(res, dict):
                res["id"] = req_id
            if decode_result and isinstance(res, dict):
                return decode_worker_result(res)
            return res
        finally:
            # Register only after the child is alive. Doing it before execute
            # pinned every workbook to a wrapper whose process had already
            # died while idle. execute respawned a blank kernel, recycle saw
            # a live pid, and later cells refreshed the TTL on empty state.
            # Must register before release_worker(): once released, another
            # concurrent request could inspect _active_sessions before we update it.
            with self._cond:
                if leased.did_respawn:
                    self._clear_worker_sessions_unlocked(leased)
                if mode == "shared" and session_id and leased.is_alive():
                    self._active_sessions[session_id] = leased
                    self._worker_sessions.setdefault(leased, set()).add(session_id)
                    self._session_last_activity[session_id] = time.monotonic()
            self.release_worker(leased)

    @staticmethod
    def _build_execute_payload(*, code: str, data: Any = None, data_json: bytes | None = None, session_id: str | None = None, mode: str = "isolated", timeout_sec: int = 30, init_script: str | None = None, req_id: str | None = None, wire: str = WIRE_JSON_FORWARD) -> dict[str, Any]:
        if wire not in (WIRE_JSON_FORWARD, WIRE_PICKLE):
            log.warning("Unknown wire format %r; defaulting to %s", wire, WIRE_JSON_FORWARD)
            eff_wire = WIRE_JSON_FORWARD
        else:
            eff_wire = wire
        payload: dict[str, Any] = {"id": req_id, "code": code, "session_id": session_id, "mode": mode, "timeout_sec": timeout_sec, "init_script": init_script, "wire": eff_wire}
        if payload["wire"] == WIRE_JSON_FORWARD:
            blob = data_json
            if blob is None and data is not None:
                # Convenience for pool tests / in-process callers. The HTTP
                # path always supplies data_json so the host never dumps the grid.
                blob = json.dumps(data, allow_nan=False).encode("utf-8")
            if blob is not None:
                payload["data_json"] = bytes(blob)
            return payload

        # LibrePy-style fallback: host Cython/stdlib flatten → split_grid in pickle.
        wire_data = data
        if isinstance(data, list) and data:
            from plugin.scripting.payload_codec import host_pack_data

            try:
                wire_data = host_pack_data(data, min_cells=1000)
            except Exception:
                wire_data = data
        payload["data"] = wire_data
        return payload


# Global singleton per server process
_POOL_SINGLETON: PoolSingleton[FormulaProcessPool] = PoolSingleton()


def get_formula_pool(settings: ComputeSettings | None = None) -> FormulaProcessPool:
    """Retrieve or initialize the global formula process pool."""
    return _POOL_SINGLETON.get(lambda: FormulaProcessPool(settings=settings))


def shutdown_formula_pool() -> None:
    """Shut down the global formula process pool."""
    _POOL_SINGLETON.shutdown()
