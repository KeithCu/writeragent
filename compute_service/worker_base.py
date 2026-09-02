# WriterAgent - Python Compute Service Base Worker & Pool Infrastructure
# Copyright (c) 2026 KeithCu
# SPDX-License-Identifier: GPL-3.0-or-later
"""
Shared subprocess worker loop, worker process wrapper, and process pool supervisor.

Provides:
- High-speed length-prefixed Pickle 5 binary framing over stdio pipes
- Deadline-bounded pickle reads (header + payload)
- Live stderr drain (start_stderr_drain) so piped stderr cannot deadlock
- Hard SIGKILL watchdog timers on hangs/timeouts
- Exclusive worker occupancy (idle set + Condition) so sticky and isolated jobs
  never share a process concurrently
- Automatic crash recovery and worker recycling after max_tasks
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from collections.abc import Callable

from plugin.framework.worker_pool import StderrTail, get_subprocess_creationflags, start_stderr_drain
from plugin.scripting.ipc import (
    DEFAULT_MAX_PAYLOAD_BYTES,
    read_pickle_frame,
    read_pickle_frame_with_timeout,
    write_pickle_frame,
)
from plugin.scripting.sandbox import optimize_popen_pipes

log = logging.getLogger("compute_service.worker")

_SPAWN_READY_TIMEOUT_SEC = 15.0
_STDERR_SNIPPET = 500


def run_worker_stdio_loop(handler: Callable[[dict[str, Any]], dict[str, Any]]) -> int:
    """Standard binary Pickle 5 stdio worker loop for child subprocesses."""
    stdin_bin = sys.stdin.buffer
    stdout_bin = sys.stdout.buffer

    # Signal readiness to supervisor
    write_pickle_frame(stdout_bin, {"status": "ready", "pid": os.getpid()})

    while True:
        try:
            req = read_pickle_frame(stdin_bin, max_payload_bytes=DEFAULT_MAX_PAYLOAD_BYTES)
            if req is None:
                break
            if not isinstance(req, dict):
                res = {"status": "error", "error": "Request must be a dict"}
            else:
                res = handler(req)
        except Exception as exc:
            res = {"status": "error", "error": f"Invalid IPC frame or unhandled error: {exc}"}

        try:
            write_pickle_frame(stdout_bin, res)
        except Exception:
            break

    return 0


class BaseProcessWorker:
    """Wrapper around one persistent child subprocess communicating via Pickle 5 frames."""

    def __init__(self, worker_id: int, script_path: str, worker_name: str = "Worker") -> None:
        self.worker_id = worker_id
        self.script_path = script_path
        self.worker_name = worker_name
        self.process: subprocess.Popen[bytes] | None = None
        self.lock = threading.Lock()
        self.tasks_executed = 0
        self._stderr_drain: StderrTail | None = None
        self._spawn()

    def _stderr_snippet(self) -> str:
        drain = self._stderr_drain
        if drain is None:
            return ""
        text = drain.text().strip()
        if not text:
            return ""
        return text[-_STDERR_SNIPPET:]

    def _spawn(self) -> None:
        """Spawn worker subprocess and await readiness handshake."""
        cmd = [sys.executable, self.script_path]
        try:
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
                    **get_subprocess_creationflags(),
                ),
            )
            self.process = proc
            optimize_popen_pipes(proc)
            self._stderr_drain = start_stderr_drain(
                proc.stderr,
                name=f"{self.worker_name}-stderr-{self.worker_id}",
            )
            if proc.stdout is not None:
                ready_data = read_pickle_frame_with_timeout(
                    proc.stdout,
                    _SPAWN_READY_TIMEOUT_SEC,
                    is_alive=self.is_alive,
                    max_payload_bytes=DEFAULT_MAX_PAYLOAD_BYTES,
                    require_dict=True,
                )
                if isinstance(ready_data, dict):
                    log.info(
                        "%s #%d spawned (pid=%s, status=%s)",
                        self.worker_name,
                        self.worker_id,
                        ready_data.get("pid", proc.pid),
                        ready_data.get("status"),
                    )
            self.tasks_executed = 0
        except subprocess.TimeoutExpired:
            # Handshake hang: child may still be importing, or stdout was not pickle.
            # Execute-path timeouts already attach _stderr_snippet(); spawn must too.
            snippet = self._stderr_snippet()
            spawned = self.process
            rc = spawned.poll() if spawned is not None else None
            extra = f" stderr={snippet!r}" if snippet else " stderr=<empty>"
            log.error(
                "%s #%d spawn handshake timed out (returncode=%s)%s",
                self.worker_name,
                self.worker_id,
                rc,
                extra,
            )
            self.kill()
        except Exception as exc:
            snippet = self._stderr_snippet()
            extra = f" stderr={snippet!r}" if snippet else " stderr=<empty>"
            log.error(
                "Failed to spawn %s #%d: %s%s",
                self.worker_name,
                self.worker_id,
                exc,
                extra,
            )
            self.kill()

    def is_alive(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def kill(self) -> None:
        """Forcefully terminate worker process."""
        proc = self.process
        self.process = None
        if proc is not None:
            try:
                proc.kill()
                proc.wait(timeout=1.0)
            except Exception:
                pass
        drain = self._stderr_drain
        if drain is not None:
            drain.join(timeout=0.2)

    def execute(self, payload: dict[str, Any], timeout_sec: float) -> dict[str, Any]:
        """Send request to worker process and await response with timeout."""
        timeout_sec = max(0.01, float(timeout_sec))
        with self.lock:
            if not self.is_alive():
                self._spawn()
                if not self.is_alive():
                    return {
                        "status": "error",
                        "code": "WORKER_SPAWN_FAILED",
                        "error": f"{self.worker_name} #{self.worker_id} could not be started.",
                    }

            assert self.process is not None
            assert self.process.stdin is not None
            assert self.process.stdout is not None

            try:
                write_pickle_frame(self.process.stdin, payload)
            except (BrokenPipeError, OSError) as exc:
                snippet = self._stderr_snippet()
                self.kill()
                err = f"Failed to send request to {self.worker_name} #{self.worker_id}: {exc}"
                if snippet:
                    err = f"{err}\n{snippet}"
                return {
                    "status": "error",
                    "code": "WORKER_PIPE_BROKEN",
                    "error": err,
                }

            try:
                resp = read_pickle_frame_with_timeout(
                    self.process.stdout,
                    timeout_sec,
                    is_alive=self.is_alive,
                )
            except subprocess.TimeoutExpired:
                log.warning(
                    "%s execution timed out after %.1fs on worker #%d; terminating pid=%s",
                    self.worker_name,
                    timeout_sec,
                    self.worker_id,
                    self.process.pid,
                )
                snippet = self._stderr_snippet()
                self.kill()
                msg = f"Execution exceeded maximum timeout of {int(timeout_sec)} seconds."
                if snippet:
                    msg = f"{msg}\n{snippet}"
                return {
                    "status": "error",
                    "code": "EXECUTION_TIMEOUT",
                    "error": msg,
                    "message": msg,
                }
            except Exception as exc:
                snippet = self._stderr_snippet()
                self.kill()
                err = f"{self.worker_name} error: {exc}"
                if snippet:
                    err = f"{err}\n{snippet}"
                return {
                    "status": "error",
                    "code": "WORKER_CRASHED",
                    "error": err,
                    "message": err,
                }
            if resp is None or not isinstance(resp, dict):
                snippet = self._stderr_snippet()
                self.kill()
                err = f"No response returned from {self.worker_name}."
                if snippet:
                    err = f"{err}\n{snippet}"
                return {
                    "status": "error",
                    "code": "EMPTY_RESPONSE",
                    "error": err,
                }
            self.tasks_executed += 1
            return resp


class BaseProcessPool:
    """Base supervisor for a bounded pool of child worker subprocesses."""

    def __init__(
        self,
        script_path: str,
        num_workers: int = 1,
        default_timeout_sec: int = 30,
        max_tasks: int = 500,
        worker_name: str = "Worker",
        idle_worker_ttl_sec: float | None = None,
    ) -> None:
        self.script_path = script_path
        self.num_workers = max(0, num_workers)
        self.default_timeout_sec = default_timeout_sec
        self.max_tasks = max_tasks
        self.worker_name = worker_name
        self.idle_worker_ttl_sec = idle_worker_ttl_sec
        self.workers: list[BaseProcessWorker] = []
        self._is_shutdown = False
        self._lock = threading.Lock()
        self._idle: set[BaseProcessWorker] = set()
        self._worker_last_active: dict[BaseProcessWorker, float] = {}
        self._cond = threading.Condition(self._lock)
        self._idle_reaper_thread: threading.Thread | None = None

        if self.num_workers > 0:
            now = time.monotonic()
            for i in range(self.num_workers):
                w = BaseProcessWorker(i + 1, script_path=script_path, worker_name=worker_name)
                self.workers.append(w)
                self._idle.add(w)
                self._worker_last_active[w] = now

        if self.idle_worker_ttl_sec is not None and self.idle_worker_ttl_sec > 0:
            self._start_idle_reaper()

    def _start_idle_reaper(self) -> None:
        ttl = cast(float, self.idle_worker_ttl_sec)
        interval = max(0.02, min(ttl / 6.0, 300.0))

        def _reap_loop() -> None:
            while not self._is_shutdown:
                time.sleep(interval)
                self._evict_idle_workers()

        t = threading.Thread(target=_reap_loop, name=f"{self.worker_name}-idle-reaper", daemon=True)
        t.start()
        self._idle_reaper_thread = t

    def _evict_idle_workers(self) -> None:
        if self._is_shutdown or self.idle_worker_ttl_sec is None:
            return
        now = time.monotonic()
        stale: list[BaseProcessWorker] = []
        with self._cond:
            for w in list(self._idle):
                if not w.is_alive():
                    continue
                last_active = self._worker_last_active.get(w, now)
                if now - last_active >= self.idle_worker_ttl_sec:
                    stale.append(w)
            # Remove from idle set while holding the lock so lease_any()
            # cannot pop a worker we are about to kill.
            for w in stale:
                self._idle.discard(w)
        for w in stale:
            w.kill()
        # Re-add dead workers to idle so future lease_any() can lazy-respawn.
        if stale:
            with self._cond:
                for w in stale:
                    if not self._is_shutdown:
                        self._idle.add(w)
                self._cond.notify_all()
        if stale:
            log.info(
                "Idle worker reaper terminated %d %s(s) idle for >%.1fs",
                len(stale),
                self.worker_name,
                self.idle_worker_ttl_sec,
            )

    def is_enabled(self) -> bool:
        return self.num_workers > 0 and not self._is_shutdown

    def lease_any(self, timeout_sec: float) -> BaseProcessWorker | None:
        """Acquire any idle worker, or None on timeout / shutdown."""
        deadline = time.monotonic() + max(0.0, float(timeout_sec))
        with self._cond:
            while True:
                if self._is_shutdown:
                    return None
                if self._idle:
                    return self._idle.pop()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._cond.wait(remaining)

    def lease_specific(self, worker: BaseProcessWorker, timeout_sec: float) -> BaseProcessWorker | None:
        """Acquire *worker* when it is idle, or None on timeout / shutdown."""
        deadline = time.monotonic() + max(0.0, float(timeout_sec))
        with self._cond:
            while True:
                if self._is_shutdown:
                    return None
                if worker in self._idle:
                    self._idle.discard(worker)
                    return worker
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._cond.wait(remaining)

    def lease_worker(self, timeout_sec: float) -> BaseProcessWorker | None:
        """Back-compat alias for :meth:`lease_any`."""
        return self.lease_any(timeout_sec)

    def should_recycle_worker(self, worker: BaseProcessWorker) -> bool:
        """Predicate to determine if worker should be recycled on release."""
        return worker.tasks_executed >= self.max_tasks

    def release_worker(self, worker: BaseProcessWorker) -> None:
        """Return worker to idle set, recycling if max_tasks reached."""
        if self.should_recycle_worker(worker):
            log.info(
                "Recycling %s #%d after %d tasks to refresh memory",
                self.worker_name,
                worker.worker_id,
                worker.tasks_executed,
            )
            worker.kill()
            # Re-spawn so the next lease does not pay spawn latency inside execute().
            # Affinity hashing uses this wrapper list, not process liveness.
            worker._spawn()
        with self._cond:
            if self._is_shutdown:
                # Recycle may have started a child after shutdown()'s kill loop.
                worker.kill()
            else:
                self._idle.add(worker)
                self._worker_last_active[worker] = time.monotonic()
            self._cond.notify_all()

    def shutdown(self) -> None:
        """Terminate all worker processes."""
        with self._cond:
            if self._is_shutdown:
                return
            self._is_shutdown = True
            log.info("Shutting down %s pool (%d workers)...", self.worker_name, len(self.workers))
            for w in self.workers:
                w.kill()
            self.workers.clear()
            self._idle.clear()
            self._worker_last_active.clear()
            self._cond.notify_all()
