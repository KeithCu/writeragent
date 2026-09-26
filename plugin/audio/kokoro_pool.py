# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""One warm Kokoro worker process, with a Vision-style idle reaper.

Mirrors ``compute_service.worker_base.BaseProcessWorker`` / ``BaseProcessPool``
and ``vision_pool.VisionProcessPool`` (num_workers=1, idle TTL, Pickle 5
handshake). The classes live here, not in ``compute_service``, because that
package is not part of the WriterAgent OXT. The child is the configured venv
Python: Kokoro and soundfile are installed there, not in LibreOffice's runtime.

The process stays up across sentences so CPU Kokoro does not reload ONNX and
the voices file every clip. ``cancel_inflight`` kills it only while a job is
inside ``execute`` — ``Kokoro.create`` cannot be interrupted any other way.
An idle worker is left alone so the Send button's ``stop_speech`` (which runs
before the reply exists) does not throw away a warm model. The reaper drops
the process after ``idle_worker_ttl_sec`` of quiet so the RAM comes back.
"""

from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
from typing import Any, cast

from plugin.framework.worker_pool import (
    StderrTail,
    get_subprocess_creationflags,
    run_in_background,
    start_stderr_drain,
)
from plugin.scripting.ipc import (
    DEFAULT_MAX_PAYLOAD_BYTES,
    read_pickle_frame_with_timeout,
    write_pickle_frame,
)
from plugin.scripting.sandbox import optimize_popen_pipes, resolve_venv_python, scrub_subprocess_env

log = logging.getLogger(__name__)

_SCRIPT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "kokoro_worker.py")
_SPAWN_READY_TIMEOUT_SEC = 20.0
_DEFAULT_JOB_TIMEOUT_SEC = 120.0
# Long enough to cover a chat reply, short enough that an abandoned CPU model
# does not sit in RAM for the rest of the LibreOffice session.
_DEFAULT_IDLE_TTL_SEC = 300.0
_STDERR_SNIPPET = 500


def _extension_root() -> str:
    return os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir, os.pardir))


def _child_env() -> dict[str, str]:
    """Scrub secrets and LibreOffice PYTHONPATH, then point at the extension."""
    env = scrub_subprocess_env(dict(os.environ))
    root = _extension_root()
    # scrub_subprocess_env strips PYTHONPATH on purpose (LO's bundled stdlib
    # breaks venv imports). Put it back as only the extension root.
    env["PYTHONPATH"] = root
    return env


class _KokoroProcess:
    """One persistent child. ``execute`` is serialized; ``kill`` is not."""

    def __init__(self, python_executable: str, script_path: str) -> None:
        self.python_executable = python_executable
        self.script_path = script_path
        self.process: subprocess.Popen[bytes] | None = None
        self._stderr_drain: StderrTail | None = None
        self._lock = threading.Lock()
        # kill() retires the object so a cancelled execute cannot respawn it
        # and finish the sentence the user just stopped.
        self._retired = False
        self._spawn()

    def _stderr_snippet(self) -> str:
        drain = self._stderr_drain
        if drain is None:
            return ""
        text = drain.text().strip()
        if not text:
            return ""
        return text[-_STDERR_SNIPPET:]

    def is_alive(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def _spawn(self) -> None:
        cmd = [self.python_executable, self.script_path]
        try:
            proc = cast(
                "subprocess.Popen[bytes]",
                subprocess.Popen(
                    cmd,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    bufsize=0,
                    text=False,
                    env=_child_env(),
                    **get_subprocess_creationflags(),
                ),
            )
            self.process = proc
            optimize_popen_pipes(proc)
            self._stderr_drain = start_stderr_drain(proc.stderr, name="kokoro-worker-stderr")
            if proc.stdout is None:
                raise RuntimeError("Kokoro worker stdout is not a pipe")
            ready = read_pickle_frame_with_timeout(
                proc.stdout,
                _SPAWN_READY_TIMEOUT_SEC,
                is_alive=self.is_alive,
                max_payload_bytes=DEFAULT_MAX_PAYLOAD_BYTES,
                require_dict=True,
            )
            if not isinstance(ready, dict) or ready.get("status") != "ready":
                snippet = self._stderr_snippet()
                extra = f" stderr={snippet!r}" if snippet else ""
                log.error("Kokoro worker ready frame was %r.%s", ready, extra)
                self.kill()
                return
            log.info("Kokoro worker spawned (pid=%s)", ready.get("pid", proc.pid))
        except subprocess.TimeoutExpired:
            snippet = self._stderr_snippet()
            extra = f" stderr={snippet!r}" if snippet else " stderr=<empty>"
            log.error("Kokoro worker spawn handshake timed out.%s", extra)
            self.kill()
        except Exception as exc:
            snippet = self._stderr_snippet()
            extra = f" stderr={snippet!r}" if snippet else ""
            log.error("Failed to spawn Kokoro worker: %s%s", exc, extra)
            self.kill()

    def kill(self) -> None:
        """Kill the child without taking ``_lock`` so a blocked ``execute`` can abort."""
        self._retired = True
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
        timeout_sec = max(0.01, float(timeout_sec))
        with self._lock:
            if self._retired:
                return {
                    "status": "error",
                    "code": "WORKER_CANCELLED",
                    "error": "Kokoro worker was cancelled.",
                }
            if not self.is_alive():
                self._spawn()
                if self._retired or not self.is_alive():
                    return {
                        "status": "error",
                        "code": "WORKER_SPAWN_FAILED",
                        "error": "Kokoro worker could not be started.",
                    }
            assert self.process is not None
            assert self.process.stdin is not None
            assert self.process.stdout is not None
            try:
                write_pickle_frame(
                    self.process.stdin,
                    payload,
                    max_payload_bytes=DEFAULT_MAX_PAYLOAD_BYTES,
                )
            except (BrokenPipeError, OSError) as exc:
                snippet = self._stderr_snippet()
                self.kill()
                err = f"Failed to send Kokoro job: {exc}"
                if snippet:
                    err = f"{err}\n{snippet}"
                return {"status": "error", "code": "WORKER_PIPE_BROKEN", "error": err}
            try:
                resp = read_pickle_frame_with_timeout(
                    self.process.stdout,
                    timeout_sec,
                    is_alive=self.is_alive,
                    max_payload_bytes=DEFAULT_MAX_PAYLOAD_BYTES,
                )
            except subprocess.TimeoutExpired:
                snippet = self._stderr_snippet()
                self.kill()
                msg = f"Kokoro job exceeded {int(timeout_sec)} seconds."
                if snippet:
                    msg = f"{msg}\n{snippet}"
                return {"status": "error", "code": "EXECUTION_TIMEOUT", "error": msg}
            except Exception as exc:
                snippet = self._stderr_snippet()
                self.kill()
                err = f"Kokoro worker error: {exc}"
                if snippet:
                    err = f"{err}\n{snippet}"
                return {"status": "error", "code": "WORKER_CRASHED", "error": err}
            if not isinstance(resp, dict):
                snippet = self._stderr_snippet()
                self.kill()
                err = "No response returned from Kokoro worker."
                if snippet:
                    err = f"{err}\n{snippet}"
                return {"status": "error", "code": "EMPTY_RESPONSE", "error": err}
            warning = resp.get("warning")
            if isinstance(warning, str) and warning.strip():
                log.warning("Kokoro: %s", warning.strip())
            return resp


class KokoroProcessPool:
    """Single warm worker plus an idle reaper. ``num_workers`` is always 1."""

    def __init__(
        self,
        python_executable: str,
        *,
        script_path: str | None = None,
        idle_worker_ttl_sec: float | None = _DEFAULT_IDLE_TTL_SEC,
        job_timeout_sec: float = _DEFAULT_JOB_TIMEOUT_SEC,
    ) -> None:
        self.python_executable = python_executable
        self.script_path = script_path or _SCRIPT_PATH
        self.idle_worker_ttl_sec = idle_worker_ttl_sec
        self.job_timeout_sec = job_timeout_sec
        self.num_workers = 1
        self._worker: _KokoroProcess | None = None
        self._lock = threading.Lock()
        self._inflight = False
        # Identifies the execute() call that set ``_inflight``. cancel() must
        # not clear a newer call's flag, and the older call's finally must not
        # clear the newer one.
        self._exec_token: object | None = None
        self._shutdown = False
        self._last_active = time.monotonic()
        self._reaper_stop = threading.Event()
        if idle_worker_ttl_sec is not None and idle_worker_ttl_sec > 0:
            self._start_reaper()

    def _start_reaper(self) -> None:
        ttl = float(self.idle_worker_ttl_sec or 0.0)
        interval = max(0.05, min(ttl / 6.0, 30.0))

        def _loop() -> None:
            while not self._reaper_stop.is_set():
                if self._reaper_stop.wait(interval):
                    return
                self._evict_idle_worker()

        # dedicated: the reaper lives until shutdown and must not occupy a
        # pooled slot (see plugin.framework.worker_pool).
        run_in_background(_loop, name="kokoro-idle-reaper", dedicated=True)

    def execute(self, payload: dict[str, Any], timeout_sec: float | None = None) -> dict[str, Any]:
        """Run one Kokoro job. Spawns the child on first use and after a crash.

        The pool lock is not held across spawn or the job itself: ``stop_speech``
        runs on the UI thread and must be able to ``cancel_inflight`` immediately.
        """
        eff_timeout = float(self.job_timeout_sec if timeout_sec is None else timeout_sec)
        token = object()
        with self._lock:
            if self._shutdown:
                return {"status": "error", "code": "WORKER_SHUTDOWN", "error": "Kokoro pool is shut down."}
            self._inflight = True
            self._exec_token = token
            self._last_active = time.monotonic()
            existing = self._worker
            reuse = existing is not None and existing.is_alive() and not existing._retired
        if existing is not None and not reuse:
            existing.kill()
        worker = existing if reuse else None
        if worker is None:
            spawned = _KokoroProcess(self.python_executable, self.script_path)
            with self._lock:
                if self._shutdown or self._exec_token is not token:
                    spawned.kill()
                    if self._exec_token is token:
                        self._inflight = False
                    return {
                        "status": "error",
                        "code": "WORKER_SPAWN_FAILED",
                        "error": "Kokoro worker spawn was cancelled.",
                    }
                if not spawned.is_alive():
                    self._worker = None
                    self._inflight = False
                    self._exec_token = None
                    return {
                        "status": "error",
                        "code": "WORKER_SPAWN_FAILED",
                        "error": "Kokoro worker could not be started.",
                    }
                self._worker = spawned
            worker = spawned
        try:
            return worker.execute(payload, eff_timeout)
        finally:
            with self._lock:
                # A newer execute() may already own ``_inflight``.
                if self._exec_token is token:
                    self._inflight = False
                    self._exec_token = None
                    self._last_active = time.monotonic()

    def cancel_inflight(self) -> None:
        """Kill the child only when a job is running. Idle warm processes stay up."""
        with self._lock:
            if not self._inflight:
                return
            worker = self._worker
            self._worker = None
        if worker is not None:
            log.info("Cancelling in-flight Kokoro synthesis")
            worker.kill()

    def _evict_idle_worker(self) -> None:
        ttl = self.idle_worker_ttl_sec
        if ttl is None or self._shutdown:
            return
        with self._lock:
            if self._inflight or self._worker is None or not self._worker.is_alive():
                return
            if time.monotonic() - self._last_active < ttl:
                return
            worker = self._worker
            self._worker = None
        log.info("Kokoro worker idle for >%.0fs; releasing the ONNX model", ttl)
        worker.kill()

    def shutdown(self) -> None:
        self._reaper_stop.set()
        with self._lock:
            self._shutdown = True
            worker = self._worker
            self._worker = None
            self._inflight = False
        if worker is not None:
            worker.kill()


_POOL: KokoroProcessPool | None = None
_POOL_LOCK = threading.Lock()
_POOL_PYTHON: str | None = None


def resolve_kokoro_python() -> str | None:
    """Venv interpreter that has kokoro-onnx, or None when Python is not configured."""
    from plugin.framework.config import get_config_str

    venv_dir = get_config_str("scripting.python_venv_path").strip()
    if not venv_dir:
        return None
    return resolve_venv_python(venv_dir)


def get_kokoro_pool() -> KokoroProcessPool | None:
    """Lazy singleton. None when no venv Python is configured."""
    global _POOL, _POOL_PYTHON
    py = resolve_kokoro_python()
    if not py:
        return None
    with _POOL_LOCK:
        if _POOL is not None and _POOL_PYTHON != py:
            _POOL.shutdown()
            _POOL = None
        if _POOL is None:
            _POOL = KokoroProcessPool(py)
            _POOL_PYTHON = py
        return _POOL


def shutdown_kokoro_pool() -> None:
    """Tests and interpreter shutdown. Safe to call when the pool was never used."""
    global _POOL, _POOL_PYTHON
    with _POOL_LOCK:
        pool = _POOL
        _POOL = None
        _POOL_PYTHON = None
    if pool is not None:
        pool.shutdown()


def cancel_kokoro_inflight() -> None:
    """Abort the current ONNX job without dropping an idle warm worker."""
    with _POOL_LOCK:
        pool = _POOL
    if pool is not None:
        pool.cancel_inflight()
