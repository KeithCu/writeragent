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

import logging
import os
import threading
import time
from typing import Any

from compute_service.config import ComputeSettings
from compute_service.worker_base import BaseProcessPool, BaseProcessWorker

log = logging.getLogger("compute_service.formula")

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_WORKER_SCRIPT = os.path.join(_SCRIPT_DIR, "formula_worker.py")


def _remaining_sec(deadline: float, *, floor: float = 0.01) -> float:
    return max(floor, deadline - time.monotonic())


class FormulaProcessPool(BaseProcessPool):
    """Bounded pool of persistent worker subprocesses for formula calculations."""

    def __init__(
        self,
        num_workers: int = 1,
        default_timeout_sec: int = 30,
        max_tasks: int = 500,
        shared_kernel_ttl_sec: float = 3600.0,
        idle_worker_ttl_sec: float | None = 3600.0,
    ) -> None:
        super().__init__(
            script_path=_WORKER_SCRIPT,
            num_workers=num_workers,
            default_timeout_sec=default_timeout_sec,
            max_tasks=max_tasks,
            worker_name="Formula worker",
            idle_worker_ttl_sec=idle_worker_ttl_sec,
        )
        self._active_sessions: dict[str, BaseProcessWorker] = {}
        self._worker_sessions: dict[BaseProcessWorker, set[str]] = {}
        self._session_last_activity: dict[str, float] = {}
        self.shared_kernel_ttl_sec = shared_kernel_ttl_sec
        self._session_reaper_thread: threading.Thread | None = None

        if self.shared_kernel_ttl_sec > 0:
            self._start_session_ttl_reaper()

    def _start_session_ttl_reaper(self) -> None:
        interval = max(0.02, min(self.shared_kernel_ttl_sec / 6.0, 300.0))

        def _reap_loop() -> None:
            while not self._is_shutdown:
                time.sleep(interval)
                self._evict_stale_sessions()

        t = threading.Thread(target=_reap_loop, name="formula-session-reaper", daemon=True)
        t.start()
        self._session_reaper_thread = t

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
        for sid, worker in stale:
            # Reset the Python namespace *before* dropping maps so the same
            # session_id cannot hash back onto leftover globals.
            leased = self.lease_specific(worker, timeout_sec=2.0)
            if leased is not None:
                try:
                    leased.execute({"action": "reset_session", "session_id": sid}, timeout_sec=2.0)
                except Exception:
                    log.exception("TTL reset_session failed for %s; killing worker", sid)
                    leased.kill()
                finally:
                    self.release_worker(leased)
                with self._cond:
                    self._session_last_activity.pop(sid, None)
                    mapped = self._active_sessions.pop(sid, None)
                    if mapped and mapped in self._worker_sessions:
                        self._worker_sessions[mapped].discard(sid)
                        if not self._worker_sessions[mapped]:
                            del self._worker_sessions[mapped]
            else:
                # Worker is currently busy executing a task; skip eviction and retry on next cycle
                log.debug("TTL eviction skipped for %s: worker is currently leased", sid)
        if stale:
            log.info("Session TTL reaper evicted %d idle session(s): %s", len(stale), [s for s, _w in stale])

    def _clear_worker_sessions_unlocked(self, worker: BaseProcessWorker) -> None:
        sessions = self._worker_sessions.pop(worker, set())
        for sid in sessions:
            self._active_sessions.pop(sid, None)
            self._session_last_activity.pop(sid, None)

    def should_recycle_worker(self, worker: BaseProcessWorker) -> bool:
        """Recycle worker if tasks_executed >= max_tasks, unless holding active shared sessions.

        Workers holding active shared sessions skip normal max_tasks recycling to preserve state.
        Sessions are held indefinitely while active and released after shared_kernel_ttl_sec of inactivity.
        """
        with self._lock:
            if not worker.is_alive():
                self._clear_worker_sessions_unlocked(worker)
                return False
            has_sessions = bool(self._worker_sessions.get(worker))

        if has_sessions:
            return False

        return worker.tasks_executed >= self.max_tasks

    def reset_session(self, session_id: str, timeout_sec: float = 5.0) -> dict[str, Any]:
        """Reset shared sandbox session and update active session tracking."""
        with self._cond:
            self._session_last_activity.pop(session_id, None)
            worker = self._active_sessions.pop(session_id, None)
            if worker and worker in self._worker_sessions:
                self._worker_sessions[worker].discard(session_id)
                if not self._worker_sessions[worker]:
                    del self._worker_sessions[worker]

        if worker is None:
            return {"status": "ok"}

        leased = self.lease_specific(worker, timeout_sec=timeout_sec)
        if leased is None:
            return {"status": "error", "error": "Could not lease worker to reset session."}
        try:
            res = leased.execute({"action": "reset_session", "session_id": session_id}, timeout_sec=timeout_sec)
            return res
        finally:
            self.release_worker(leased)

    def check_dependencies(
        self,
        packages: list[str] | None = None,
        timeout_sec: float = 10.0,
    ) -> tuple[bool, str | None]:
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
            payload = {
                "action": "check_dependencies",
                "packages": target_packages,
            }
            res = leased.execute(payload, timeout_sec=timeout_sec)
            if res.get("status") == "ok":
                return True, None
            missing = res.get("missing")
            if missing and isinstance(missing, list):
                missing_str = ", ".join(str(m) for m in missing)
                return False, (
                    f"Error: {missing_str} is not installed in the current Python environment.\n"
                    "Please start the server using './compute_service/start.sh' or activate the correct virtual environment."
                )
            err = res.get("error") or "Unknown error during worker dependency check."
            return False, str(err)
        finally:
            self.release_worker(leased)

    def probe_cython_status(self, timeout_sec: float = 10.0) -> str | None:
        """Ask one idle formula worker for Cython load status (once per call).

        The HTTP host logs this as a single ``Formula worker Cython Accelerator``
        line after the pool is up. Do not call per worker or on every request.
        """
        if self._is_shutdown or not self.workers:
            return None

        leased = self.lease_any(timeout_sec=timeout_sec)
        if leased is None:
            return None

        try:
            res = leased.execute({"action": "cython_status"}, timeout_sec=timeout_sec)
            line = res.get("cython_status")
            if res.get("status") == "ok" and isinstance(line, str) and line:
                return line
            return None
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
    ) -> dict[str, Any]:
        """Execute formula code on an appropriate worker subprocess."""
        if self._is_shutdown or not self.workers:
            return {
                "id": req_id,
                "status": "error",
                "code": "SERVICE_SHUTDOWN",
                "error": "Formula compute pool is shutting down.",
            }

        eff_timeout = float(timeout_sec or self.default_timeout_sec)
        deadline = time.monotonic() + eff_timeout

        # Optimize large matrix data using zero-copy split_grid binary envelope
        wire_data = data
        if isinstance(data, list) and data:
            from plugin.scripting.payload_codec import host_pack_data

            try:
                wire_data = host_pack_data(data, min_cells=1000)
            except Exception:
                wire_data = data

        payload = {
            "id": req_id,
            "code": code,
            "data": wire_data,
            "session_id": session_id,
            "mode": mode,
            "timeout_sec": int(eff_timeout),
            "init_script": init_script,
        }

        leased: BaseProcessWorker | None
        # Snapshot workers under the pool lock to avoid a TOCTOU race with
        # concurrent shutdown() which calls self.workers.clear() under the same lock.
        # Without the snapshot, the IndexError window between len() and [] access
        # is real even on CPython when shutdown races execute on another thread.
        with self._lock:
            workers_snapshot = list(self.workers)
        if mode == "shared" and session_id and workers_snapshot:
            worker_idx = abs(hash(session_id)) % len(workers_snapshot)
            target_worker = workers_snapshot[worker_idx]
            leased = self.lease_specific(target_worker, timeout_sec=_remaining_sec(deadline))
            busy_code = "WORKER_POOL_BUSY"
            busy_err = "Sticky session worker is busy and request timed out waiting for worker lease."
        else:
            leased = self.lease_any(timeout_sec=_remaining_sec(deadline))
            busy_code = "WORKER_POOL_BUSY"
            busy_err = "All formula workers are currently busy and request timed out waiting for worker lease."

        if leased is None:
            return {
                "id": req_id,
                "status": "error",
                "code": busy_code,
                "error": busy_err,
            }

        if mode == "shared" and session_id:
            with self._cond:
                self._active_sessions[session_id] = leased
                if leased not in self._worker_sessions:
                    self._worker_sessions[leased] = set()
                self._worker_sessions[leased].add(session_id)
                self._session_last_activity[session_id] = time.monotonic()

        try:
            res = leased.execute(payload, timeout_sec=_remaining_sec(deadline))
            if req_id is not None and isinstance(res, dict):
                res["id"] = req_id
            return res
        finally:
            if mode == "shared" and session_id:
                with self._cond:
                    self._session_last_activity[session_id] = time.monotonic()
            self.release_worker(leased)


# Global singleton per server process
_GLOBAL_FORMULA_POOL: FormulaProcessPool | None = None
_GLOBAL_FORMULA_POOL_LOCK = threading.Lock()


def get_formula_pool(settings: ComputeSettings | None = None) -> FormulaProcessPool:
    """Retrieve or initialize the global formula process pool."""
    global _GLOBAL_FORMULA_POOL
    with _GLOBAL_FORMULA_POOL_LOCK:
        if _GLOBAL_FORMULA_POOL is None:
            if settings is not None:
                num_w = getattr(settings, "workers", None) or getattr(settings, "max_workers", None) or 2
                _GLOBAL_FORMULA_POOL = FormulaProcessPool(
                    num_workers=num_w,
                    default_timeout_sec=settings.default_timeout_sec,
                    max_tasks=settings.worker_max_tasks,
                    shared_kernel_ttl_sec=settings.shared_kernel_ttl_sec,
                    idle_worker_ttl_sec=settings.idle_worker_ttl_sec,
                )
            else:
                _GLOBAL_FORMULA_POOL = FormulaProcessPool(num_workers=1)
        return _GLOBAL_FORMULA_POOL


def shutdown_formula_pool() -> None:
    """Shut down the global formula process pool."""
    global _GLOBAL_FORMULA_POOL
    with _GLOBAL_FORMULA_POOL_LOCK:
        if _GLOBAL_FORMULA_POOL is not None:
            _GLOBAL_FORMULA_POOL.shutdown()
            _GLOBAL_FORMULA_POOL = None
