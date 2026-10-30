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

from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
import json
import logging
import math
import os
import threading
import time
from typing import Any, Iterator

from compute_service.config import ComputeSettings
from compute_service.json_forward import COMPUTE_MAX_PAYLOAD_BYTES, ExecuteRequestError, canonical_execute_mode, validate_session_id
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

    Bugfix: what was wrong: a stale row was dropped once it had been stale
    for ``idle_worker_ttl_sec``, so a sticky call after one or two reaper
    passes got a fresh namespace without ``session_reset``.
    How it happened: the reaper also expired stale rows, a marker TTL the
    README says does not exist.
    Why this change fixes it: stale rows now stay until a sticky call or a
    reset consumes them, so a lost kernel is always reported once.
    """

    worker: BaseProcessWorker
    child: object | None
    last_used: float = field(default_factory=time.monotonic)


@dataclass
class _SessionGate:
    """Placement gate for one shared session id (reference-counted)."""

    lock: threading.Lock
    users: int = 0


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
        # Base shutdown drops workers and idle slots. These maps used to
        # survive, so a pool object kept after shutdown still advertised sessions.
        self._sessions.clear()
        self._session_locks.clear()
        self._recycling.clear()

    @contextmanager
    def _session_gate(self, session_id: str) -> Iterator[None]:
        """Placement gate for *session_id* (reference-counted).

        Held from lookup through worker release on shared execute/reset calls
        to prevent concurrent first-calls from selecting different workers.
        The lock entry in ``_session_locks`` is pruned as soon as no caller holds
        or waits on it.

        Bugfix: what was wrong: _session_locks accumulated an entry per shared session
        id that was never pruned (only cleared at shutdown), leaking a Lock for the life
        of the service.
        How it happened: _session_gate used setdefault to create Lock instances without
        tracking when callers finished using them.
        Why this change fixes it: reference-counts active callers [lock, users] under
        self._cond, acquiring the lock outside self._cond, and deletes the entry in finally
        when users drops to 0.
        """
        with self._cond:
            entry = self._session_locks.get(session_id)
            if entry is None:
                entry = _SessionGate(lock=threading.Lock())
                self._session_locks[session_id] = entry
            entry.users += 1
            lock = entry.lock

        try:
            with lock:
                yield
        finally:
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
        control-plane reset request.
        """
        try:
            validate_session_id(session_id)
        except ExecuteRequestError as exc:
            return {"status": "error", "code": "INVALID_REQUEST", "error": str(exc)}

        with self._session_gate(session_id):
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
                    return {"status": "error", "code": "WORKER_POOL_BUSY", "error": "Could not lease worker to reset session."}
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
                    # The wrapper is gone. Do not mark lost: the caller treats
                    # this as a new reservation on a live worker.
                    self._drop_session(session_id)
                    sess = None
                else:
                    sess.last_used = time.monotonic()

            if sess is not None:
                return sess.worker
            return self._pick_new_shared_worker()

    def _run_execution(self, leased: BaseProcessWorker, payload: dict[str, Any], clock: Deadline, req_id: str | None, decode_result: bool, expect_namespace: bool = False) -> dict[str, Any]:
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
        # Bugfix: what was wrong: int(run_for) floored the budget, so a
        # 2000 ms request got a 1 s alarm and the 30 s default got 29 s.
        # How it happened: usable() is already a little under the request
        # timeout when the lease is granted, and int() drops the fraction.
        # Why this change fixes it: ceil keeps the full remaining second, the
        # same rounding the sandbox uses, and the host wait is measured from
        # that alarm so the alarm still fires before the host read gives up.
        child_alarm = math.ceil(run_for)
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
                    else:
                        self._sessions[session_id] = _Session(
                            worker=leased,
                            child=leased.child,
                            last_used=now,
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

        # One schema. A mode the HTTP layer missed must not be
        # rewritten and run: that returned 200 for a kernel that kept no state.
        try:
            mode = canonical_execute_mode(mode)
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
            payload = self._build_execute_payload(code=code, data=data, data_json=data_json, session_id=session_id, mode=mode, init_script=init_script, req_id=req_id)
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
            had_binding = False
            if mode == "shared" and session_id:
                with self._cond:
                    had_binding = session_id in self._sessions
                target_worker = self._select_shared_worker(session_id)
                if target_worker is None:
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
                return error_dict("WORKER_POOL_BUSY", busy_err, req_id=req_id)

            result: dict[str, Any] | None = None
            try:
                # Time passes while waiting for the lease. Recheck the same clock.
                if clock.usable() is None:
                    result = error_dict("QUEUE_TIMEOUT", "Request deadline expired before a worker lease.", req_id=req_id)
                    return result
                result = self._run_execution(
                    leased,
                    payload=payload,
                    clock=clock,
                    req_id=req_id,
                    decode_result=decode_result,
                    expect_namespace=(mode == "shared" and had_binding),
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
