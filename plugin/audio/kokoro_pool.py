# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""One warm Kokoro worker process, with an idle reaper.

The child is a ``ManagedSubprocess`` plus the Pickle 5 handshake in
``_KokoroWorker``. The 20 second ready timeout covers process start and
the import of ``plugin.scripting.ipc`` only. ``run_kokoro_stdio_loop``
writes ``{"status": "ready"}`` before ``_get_kokoro`` imports ``kokoro_onnx``;
the model load is the first job's timeout. ``cancel_inflight`` signals on
the caller thread and reaps with ``worker.kill`` on a background thread;
the signal does not close stdin and does not call ``ManagedSubprocess.close``.
``execute`` holds ``_job_lock`` across spawn and the pipe so two callers
cannot split a frame. ``stop_speech`` can call ``cancel_inflight`` without
waiting for ``ManagedSubprocess.close``. This pool keeps the cancel token
and the single lazy slot: Stop during the handshake must see the object
before ``respawn`` blocks. Kokoro and soundfile live in the configured venv,
not in LibreOffice's runtime.

The process stays up across sentences so CPU Kokoro does not reload ONNX and
the voices file every clip. ``cancel_inflight`` kills it while a job is
inside ``execute``, including the spawn handshake before the ready frame —
``Kokoro.create`` cannot be interrupted any other way. An idle worker is left
alone so the Send button's ``stop_speech`` (which runs before the reply
exists) does not throw away a warm model. A ``cancel_check`` which returns
true before a frame is sent returns ``WORKER_CANCELLED`` and does not call
``cancel_inflight``. The reaper drops the process after
``idle_worker_ttl_sec`` of quiet so the RAM comes back.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time
from typing import Any, Callable

from plugin.framework.errors import best_effort
from plugin.framework.managed_subprocess import ManagedSubprocess
from plugin.framework.worker_pool import run_in_background
from plugin.scripting.ipc import (
    IpcPartialFrameTimeout,
    pack_pickle_frame,
    read_pickle_frame_with_timeout,
    write_packed_frame_with_timeout,
)
from plugin.scripting.sandbox import resolve_venv_python, scrub_subprocess_env

log = logging.getLogger(__name__)

_SCRIPT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "kokoro_worker.py")
_SPAWN_READY_TIMEOUT_SEC = 20.0
_DEFAULT_JOB_TIMEOUT_SEC = 120.0
# Long enough to cover a chat reply, short enough that an abandoned CPU model
# does not sit in RAM for the rest of the LibreOffice session.
_DEFAULT_IDLE_TTL_SEC = 300.0


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


class _KokoroWorker:
    """Subprocess worker executing Kokoro jobs over stdio Pickle 5 framing."""

    python_executable: str
    script_path: str
    _env: dict[str, str] | None
    _ready_timeout_sec: float
    _managed: ManagedSubprocess
    _shutting_down: bool
    _killed: bool

    def __init__(
        self,
        python_executable: str,
        script_path: str,
        *,
        env: dict[str, str] | None = None,
        ready_timeout_sec: float = _SPAWN_READY_TIMEOUT_SEC,
    ) -> None:
        self.python_executable = python_executable
        self.script_path = script_path
        self._env = env
        self._ready_timeout_sec = ready_timeout_sec
        self._managed = ManagedSubprocess(
            [python_executable, script_path],
            base_env=env,
            command_wrapper=None,
            optimize_pipes=True,
            label="Kokoro",
        )
        self._shutting_down = False
        self._killed = False

    def respawn(self) -> None:
        if self._shutting_down:
            return
        try:
            self._managed.start()
        except Exception as exc:
            log.error("Failed to start Kokoro worker: %s", exc)
            self.kill()
            return

        if self._shutting_down:
            self.kill()
            return

        proc = self._managed.popen

        stdout = self._managed.stdout
        if stdout is None:
            self.kill()
            return

        try:
            ready_data = read_pickle_frame_with_timeout(
                stdout,
                self._ready_timeout_sec,
                is_alive=self.is_alive,
                require_dict=True,
            )
            if not isinstance(ready_data, dict) or ready_data.get("status") != "ready":
                log.error("Kokoro spawn handshake was not ready: %r", ready_data)
                self.kill()
                return
            log.info(
                "Kokoro worker spawned (pid=%s, status=%s)",
                ready_data.get("pid", getattr(proc, "pid", None)),
                ready_data.get("status"),
            )
        except Exception as exc:
            log.error("Kokoro spawn handshake timed out or failed: %s", exc)
            self.kill()

    def execute(self, payload: dict[str, Any], timeout_sec: float) -> dict[str, Any]:
        stdin = self._managed.stdin
        stdout = self._managed.stdout
        if stdin is None or stdout is None or not self.is_alive():
            self.kill()
            return {
                "status": "error",
                "code": "WORKER_CRASHED",
                "error": "Kokoro worker has no stdio pipes or is not alive.",
                "message": "Kokoro worker has no stdio pipes or is not alive.",
            }

        start_time = time.monotonic()
        try:
            frame = pack_pickle_frame(payload)
        except Exception as exc:
            return {
                "status": "error",
                "code": "REQUEST_NOT_SERIALIZABLE",
                "error": str(exc),
            }

        try:
            write_packed_frame_with_timeout(stdin, frame, timeout_sec, is_alive=self.is_alive)
        except subprocess.TimeoutExpired:
            self.kill()
            msg = f"Execution exceeded maximum timeout of {timeout_sec} seconds."
            return {"status": "error", "code": "EXECUTION_TIMEOUT", "error": msg, "message": msg}
        except Exception as exc:
            self.kill()
            msg = f"Failed to send request to Kokoro: {exc}"
            return {"status": "error", "code": "WORKER_PIPE_BROKEN", "error": msg}

        elapsed = time.monotonic() - start_time
        remaining = timeout_sec - elapsed
        if remaining <= 0:
            self.kill()
            msg = f"Execution exceeded maximum timeout of {timeout_sec} seconds."
            return {"status": "error", "code": "EXECUTION_TIMEOUT", "error": msg, "message": msg}

        try:
            resp = read_pickle_frame_with_timeout(stdout, remaining, is_alive=self.is_alive, require_dict=True)
            if not isinstance(resp, dict):
                self.kill()
                msg = "No response returned from Kokoro."
                return {"status": "error", "code": "EMPTY_RESPONSE", "error": msg}
            return resp
        except (subprocess.TimeoutExpired, IpcPartialFrameTimeout):
            self.kill()
            msg = f"Execution exceeded maximum timeout of {timeout_sec} seconds."
            return {"status": "error", "code": "EXECUTION_TIMEOUT", "error": msg, "message": msg}
        except Exception as exc:
            self.kill()
            msg = f"Kokoro error: {exc}"
            return {"status": "error", "code": "WORKER_CRASHED", "error": msg, "message": msg}

    def is_alive(self) -> bool:
        return not self._killed and self._managed.is_alive()

    def is_shutting_down(self) -> bool:
        return self._shutting_down

    def request_shutdown(self) -> None:
        self._shutting_down = True

    def kill(self) -> bool:
        # What was wrong: when spawn failed (missing script), respawn() called
        # kill(), which signaled and abandoned the process to the background
        # reaper without waiting for it to be reaped. is_alive() returned True
        # while proc.poll() was still None, leading execute() to treat spawn
        # as successful and fail on pipe write with WORKER_PIPE_BROKEN.
        # How it happened: kill() switched from close() to kill_tree() + abandon(),
        # but _KokoroWorker did not track killed state.
        # Why this change fixes it: mark _killed = True so is_alive() returns False
        # immediately, surfacing spawn failures as WORKER_SPAWN_FAILED.
        self._killed = True
        self._managed.kill_tree()
        self._managed.abandon()
        return True


def _signal_kokoro_child(worker: Any) -> None:
    """Signal the child process directly without waiting or closing stdin.

    What was wrong: Stop thread closed managed.stdin while execute could be in
    write_packed_frame_with_timeout / os.write on a captured fd.
    How it happened: the closed fd number can be reused by a later open before
    that loop finishes, causing writes to corrupt unrelated files.
    Why this change fixes it: signal only (SIGKILL on POSIX, terminate on
    Windows); the job thread unblocks on EPIPE/EOF and leaves the pipe; kill
    still does not close pipes.
    """
    managed = getattr(worker, "_managed", None)
    if managed is None:
        return
    proc = getattr(managed, "popen", None)
    if proc is None:
        return
    action = proc.terminate if sys.platform == "win32" else proc.kill
    best_effort(action, "Kokoro child signal failed")


class KokoroProcessPool:
    """Single warm worker plus an idle reaper. ``num_workers`` is always 1."""

    python_executable: str
    script_path: str
    idle_worker_ttl_sec: float | None
    job_timeout_sec: float
    num_workers: int
    _lock: threading.Lock
    _job_lock: threading.Lock
    _inflight: bool
    _shutdown: bool
    _last_active: float
    _reaper_stop: threading.Event
    _seen_warnings: set[str]

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
        self._worker: _KokoroWorker | None = None
        self._lock = threading.Lock()
        self._job_lock = threading.Lock()
        self._inflight = False
        # Identifies the execute() call that set ``_inflight``. cancel() must
        # not clear a newer call's flag, and the older call's finally must not
        # clear the newer one.
        self._exec_token: object | None = None
        self._shutdown = False
        self._last_active = time.monotonic()
        self._reaper_stop = threading.Event()
        self._seen_warnings = set()
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

    def execute(
        self,
        payload: dict[str, Any],
        timeout_sec: float | None = None,
        cancel_check: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        """Run one Kokoro job. Spawns the child on first use and after a crash.

        The pool lock is not held across spawn or the job, and cancel_check is
        not called under it (_playback_blocked takes the speech lock).
        cancel_check runs before the job is marked in-flight and a true result
        leaves _worker in place. _job_lock is the pipe lock. cancel_inflight
        does not take _job_lock.
        """
        eff_timeout = float(self.job_timeout_sec if timeout_sec is None else timeout_sec)
        with self._job_lock:
            with self._lock:
                if self._shutdown:
                    return {"status": "error", "code": "WORKER_SHUTDOWN", "error": "Kokoro pool is shut down."}

            if cancel_check is not None and cancel_check():
                # What was wrong: _inflight was set first, then cancel_inflight killed
                # the idle _worker even before a frame was sent.
                # How it happened: Stop after _kokoro_warm_to_file's playback check and
                # before any frame made the next sentence look in-flight.
                # Why this fixes it: a true cancel_check returns WORKER_CANCELLED
                # without publishing the token or setting _inflight, leaving the warm
                # idle worker in place.
                return {
                    "status": "error",
                    "code": "WORKER_CANCELLED",
                    "error": "Kokoro worker was cancelled.",
                }

            token = object()
            with self._lock:
                if self._shutdown:
                    return {"status": "error", "code": "WORKER_SHUTDOWN", "error": "Kokoro pool is shut down."}
                self._inflight = True
                self._exec_token = token
                self._last_active = time.monotonic()
                existing = self._worker
                # A dead slot is not warm. kill() has already dropped a child it
                # could not reap, so is_alive() is false and this job may spawn.
                reuse = existing is not None and existing.is_alive() and not existing.is_shutting_down()

            token_stolen = False
            result: dict[str, Any] = {}
            try:
                if existing is not None and not reuse:
                    self._retire_worker(existing)
                    with self._lock:
                        if self._worker is existing:
                            self._worker = None
                worker = existing if reuse else None
                if worker is None:
                    spawned = _KokoroWorker(
                        self.python_executable,
                        self.script_path,
                        env=_child_env(),
                        ready_timeout_sec=_SPAWN_READY_TIMEOUT_SEC,
                    )
                    spawn_aborted = False
                    with self._lock:
                        # Cancel during the gap before Popen: do not start the child.
                        if self._shutdown or self._exec_token is not token:
                            spawn_aborted = True
                        else:
                            # Visible to cancel_inflight before the ready handshake blocks.
                            self._worker = spawned
                    if spawn_aborted:
                        result = {
                            "status": "error",
                            "code": "WORKER_CANCELLED",
                            "error": "Kokoro worker spawn was cancelled.",
                        }
                    else:
                        spawned.respawn()
                        spawn_cancelled = False
                        failed = False
                        with self._lock:
                            # Token drop means Stop or shutdown. A crashed handshake
                            # kills the child without setting ``_shutting_down``, so
                            # that flag is not "cancelled": a real spawn failure must
                            # stay WORKER_SPAWN_FAILED and fall back to the one-shot clip.
                            spawn_cancelled = self._shutdown or self._exec_token is not token
                            # A handshake that could not reap has already dropped the
                            # child, so is_alive() is false and this stays a failed spawn.
                            failed = spawn_cancelled or not spawned.is_alive()
                            if failed and self._worker is spawned:
                                self._worker = None
                        if failed:
                            self._retire_worker(spawned)
                            if spawn_cancelled:
                                result = {
                                    "status": "error",
                                    "code": "WORKER_CANCELLED",
                                    "error": "Kokoro worker spawn was cancelled.",
                                }
                            else:
                                result = {
                                    "status": "error",
                                    "code": "WORKER_SPAWN_FAILED",
                                    "error": "Kokoro worker could not be started.",
                                }
                        else:
                            worker = spawned
                if worker is not None:
                    result = worker.execute(payload, eff_timeout)
            finally:
                with self._lock:
                    # Cancel drops the token; this finally must not clear a
                    # token cancel_inflight already cleared. A second execute
                    # waits on _job_lock until this finally releases it, so it
                    # cannot write the same stdin.
                    if self._exec_token is token:
                        self._inflight = False
                        self._exec_token = None
                        self._last_active = time.monotonic()
                    else:
                        token_stolen = True

        if token_stolen:
            return {
                "status": "error",
                "code": "WORKER_CANCELLED",
                "error": "Kokoro worker was cancelled.",
            }
        raw_warning = result.get("warning")
        if isinstance(raw_warning, str):
            text = raw_warning.strip()
            if text:
                first = False
                with self._lock:
                    if text not in self._seen_warnings:
                        self._seen_warnings.add(text)
                        first = True
                if first:
                    log.warning("Kokoro: %s", text)
                else:
                    log.debug("Kokoro: %s", text)
        return result

    def cancel_inflight(self, token: object | None = None) -> None:
        """Kill the child only when a job is running. Idle warm processes stay up.

        Drop ``_exec_token`` so the spawner cannot adopt the child, and kill
        the process if it has already been published. ``request_shutdown``
        makes an in-flight ``respawn`` discard the child instead of
        publishing it. The worker is stored before ``respawn`` so Stop
        during the ready handshake still finds it.
        """
        with self._lock:
            if not self._inflight:
                return
            if token is not None and self._exec_token is not token:
                return
            worker = self._worker
            self._worker = None
            self._exec_token = None
            self._inflight = False
        if worker is not None:
            log.info("Cancelling in-flight Kokoro synthesis")
            worker.request_shutdown()
            _signal_kokoro_child(worker)
            # close() waits up to 3 seconds and then up to 1 second on the
            # stderr drain; stop_speech runs on the UI thread and already reaps
            # Piper and the players in the background. The signal is synchronous
            # so the ONNX process is killed before this function returns.
            try:
                run_in_background(worker.kill, name="kokoro-cancel-reap")
            except Exception:
                worker.kill()

    def _evict_idle_worker(self) -> None:
        ttl = self.idle_worker_ttl_sec
        if ttl is None or self._shutdown:
            return
        with self._lock:
            if self._inflight or self._worker is None or not self._worker.is_alive():
                return
            worker = self._worker
            expired = time.monotonic() - self._last_active >= ttl
            if expired:
                self._worker = None
        if not expired:
            return
        log.info("Kokoro worker idle for >%.0fs; releasing the ONNX model", ttl)
        self._retire_worker(worker)

    def _retire_worker(self, worker: _KokoroWorker) -> None:
        """request_shutdown plus kill calls kill_tree then abandon (group SIGKILL, no pipe close, no wait on this thread; the abandon reaper does the five 1s passes). It is not close() and it is not the compute handshake."""
        worker.request_shutdown()
        worker.kill()

    def token_if_inflight(self) -> object | None:
        """Return the active job's token when a job is inflight, else None."""
        with self._lock:
            if self._inflight:
                return self._exec_token
            return None

    def shutdown(self) -> None:
        self._reaper_stop.set()
        with self._lock:
            self._shutdown = True
            worker = self._worker
            self._worker = None
            self._inflight = False
            # In-flight execute treats a dropped token as cancel, so shutdown
            # does not fall through to a one-shot Kokoro clip.
            self._exec_token = None
        if worker is not None:
            worker.request_shutdown()
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


def get_kokoro_inflight_token() -> object | None:
    """Return an opaque token for the active job, or None."""
    with _POOL_LOCK:
        pool = _POOL
    if pool is not None:
        return pool.token_if_inflight()
    return None


def cancel_kokoro_inflight(token: object | None = None) -> None:
    """Abort the current ONNX job without dropping an idle warm worker.

    If token is provided, only aborts if that specific job is still running.
    """
    with _POOL_LOCK:
        pool = _POOL
    if pool is not None:
        pool.cancel_inflight(token)
