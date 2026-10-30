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
from plugin.framework.process_worker import Deadline, error_dict
from plugin.framework.worker_pool import run_in_background
from plugin.scripting.ipc import (
    IpcPartialFrameTimeout,
    pack_pickle_frame,
    read_pickle_frame,
    write_packed_frame_with_timeout,
)
from plugin.scripting.sandbox import resolve_venv_python, scrub_subprocess_env

log = logging.getLogger(__name__)

_SCRIPT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "kokoro_worker.py")
_SPAWN_READY_TIMEOUT_SEC = 20.0
_MIN_SPAWN_REMAINING_SEC = 0.25
_DEFAULT_JOB_TIMEOUT_SEC = 120.0
# Long enough to cover a chat reply, short enough that an abandoned CPU model
# does not sit in RAM for the rest of the LibreOffice session.
_DEFAULT_IDLE_TTL_SEC = 300.0
_SEEN_WARNINGS_MAX = 32
WORKER_CANCELLED = "WORKER_CANCELLED"


def _child_env() -> dict[str, str]:
    """Scrub secrets and LibreOffice PYTHONPATH, then point at the extension."""
    env = scrub_subprocess_env(dict(os.environ))
    # scrub_subprocess_env strips PYTHONPATH on purpose (LO's bundled stdlib
    # breaks venv imports). Put it back as only the extension root.
    env["PYTHONPATH"] = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir, os.pardir))
    return env


def _error_with_tail(code: str, msg: str, tail: str, *, with_message: bool = False) -> dict[str, Any]:
    """``error_dict`` whose ``error`` ends with the child's stderr tail (also under ``stderr``)."""
    res = error_dict(code, f"{msg}\n{tail}" if tail else msg, message=msg if with_message else None)
    if tail:
        res["stderr"] = tail
    return res


class _KokoroWorker:
    """Subprocess worker executing Kokoro jobs over stdio Pickle 5 framing."""

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
        self._ready_timeout_sec = ready_timeout_sec
        self._managed = ManagedSubprocess(
            [python_executable, script_path],
            base_env=env,
            command_wrapper=None,
            optimize_pipes=True,
            stderr_name="wa-kokoro",
            stderr_name_with_pid=True,
            label="Kokoro",
        )
        self._shutting_down = False
        self._killed = False

    def stderr_tail(self) -> str:
        text = self._managed.stderr_text()
        if not text.strip() and not self.is_alive():
            text = self._managed.join_stderr(0.5)
        return text.strip()[-500:]

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
            ready_data = read_pickle_frame(
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
        except BaseException:
            # Bugfix: an interrupt used to skip kill() because it is not an Exception;
            # the worker stayed published and the next job consumed its unread frame.
            # kill() sets _killed, so is_alive() is false and the next job spawns.
            self.kill()
            raise

    def execute(self, payload: dict[str, Any], timeout_sec: float) -> dict[str, Any]:
        stdin = self._managed.stdin
        stdout = self._managed.stdout
        if stdin is None or stdout is None or not self.is_alive():
            self.kill()
            return _error_with_tail("WORKER_CRASHED", "Kokoro worker has no stdio pipes or is not alive.", self.stderr_tail(), with_message=True)

        start_time = time.monotonic()
        try:
            frame = pack_pickle_frame(payload)
        except Exception as exc:
            return error_dict("REQUEST_NOT_SERIALIZABLE", str(exc))

        try:
            write_packed_frame_with_timeout(stdin, frame, timeout_sec, is_alive=self.is_alive)
        except subprocess.TimeoutExpired:
            self.kill()
            return _error_with_tail("EXECUTION_TIMEOUT", f"Execution exceeded maximum timeout of {timeout_sec} seconds.", self.stderr_tail(), with_message=True)
        except Exception as exc:
            self.kill()
            return _error_with_tail("WORKER_PIPE_BROKEN", f"Failed to send request to Kokoro: {exc}", self.stderr_tail())

        elapsed = time.monotonic() - start_time
        remaining = timeout_sec - elapsed
        if remaining <= 0:
            self.kill()
            return _error_with_tail("EXECUTION_TIMEOUT", f"Execution exceeded maximum timeout of {timeout_sec} seconds.", self.stderr_tail(), with_message=True)

        try:
            resp = read_pickle_frame(stdout, remaining, is_alive=self.is_alive, require_dict=True)
            if not isinstance(resp, dict):
                self.kill()
                return _error_with_tail("EMPTY_RESPONSE", "No response returned from Kokoro.", self.stderr_tail())
            return resp
        except (subprocess.TimeoutExpired, IpcPartialFrameTimeout):
            self.kill()
            return _error_with_tail("EXECUTION_TIMEOUT", f"Execution exceeded maximum timeout of {timeout_sec} seconds.", self.stderr_tail(), with_message=True)
        except Exception as exc:
            self.kill()
            return _error_with_tail("WORKER_CRASHED", f"Kokoro error: {exc}", self.stderr_tail(), with_message=True)
        except BaseException:
            self.kill()
            raise

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
        if not bool(self._managed.is_alive()):
            self._managed.close_stdio()
            if self._managed.wait_once():
                self._managed.unregister()
                return True
        self._managed.abandon()
        return True

    def signal(self) -> None:
        """Signal the child process directly without waiting or closing stdin.

        What was wrong: Stop thread closed managed.stdin while execute could be in
        write_packed_frame_with_timeout / os.write on a captured fd.
        How it happened: the closed fd number can be reused by a later open before
        that loop finishes, causing writes to corrupt unrelated files.
        Why this change fixes it: signal only (SIGKILL on POSIX, terminate on
        Windows); the job thread unblocks on EPIPE/EOF and leaves the pipe; kill
        still does not close pipes.
        """
        proc = self._managed.popen
        if proc is None:
            return
        action = proc.terminate if sys.platform == "win32" else proc.kill
        best_effort(action, "Kokoro child signal failed")


class KokoroProcessPool:
    """Single warm worker plus an idle reaper."""

    python_executable: str
    script_path: str
    idle_worker_ttl_sec: float | None
    job_timeout_sec: float
    _lock: threading.Lock
    _job_lock: threading.Lock
    _shutdown: bool
    _last_active: float
    _reaper_stop: threading.Event
    _seen_warnings: dict[str, None]

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
        self._worker: _KokoroWorker | None = None
        self._lock = threading.Lock()
        self._job_lock = threading.Lock()
        # Identifies this execute() call. cancel() must not clear a newer
        # call's token, and the older call's finally must not clear the newer
        # one. In-flight means the token is not None.
        self._exec_token: object | None = None
        self._shutdown = False
        self._last_active = time.monotonic()
        self._reaper_stop = threading.Event()
        self._seen_warnings = {}
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
                    return error_dict("WORKER_SHUTDOWN", "Kokoro pool is shut down.")

            if cancel_check is not None and cancel_check():
                # What was wrong: _inflight was set first, then cancel_inflight killed
                # the idle _worker even before a frame was sent.
                # How it happened: Stop after _kokoro_warm_to_file's playback check and
                # before any frame made the next sentence look in-flight.
                # Why this fixes it: a true cancel_check returns WORKER_CANCELLED
                # without publishing the token, leaving the warm idle worker in place.
                return error_dict(WORKER_CANCELLED, "Kokoro worker was cancelled.")

            token = object()
            with self._lock:
                if self._shutdown:
                    return error_dict("WORKER_SHUTDOWN", "Kokoro pool is shut down.")
                self._exec_token = token
                existing = self._worker
                # A dead slot is not warm. kill() has already dropped a child it
                # could not reap, so is_alive() is false and this job may spawn.
                reuse = existing is not None and existing.is_alive() and not existing.is_shutting_down()

            deadline = Deadline(eff_timeout)
            token_stolen = False
            result: dict[str, Any] = {}
            spawned_worker: _KokoroWorker | None = None
            try:
                if existing is not None and not reuse:
                    self._retire_worker(existing)
                    with self._lock:
                        if self._worker is existing:
                            self._worker = None

                if reuse:
                    if deadline.left() <= 0:
                        msg = f"Execution exceeded maximum timeout of {eff_timeout} seconds."
                        result = error_dict("EXECUTION_TIMEOUT", msg, message=msg)
                        worker = None
                    else:
                        worker = existing
                else:
                    worker = None

                if worker is None and not result:
                    if deadline.left() < _MIN_SPAWN_REMAINING_SEC:
                        msg = f"Execution exceeded maximum timeout of {eff_timeout} seconds."
                        result = error_dict("EXECUTION_TIMEOUT", msg, message=msg)
                    else:
                        ready_timeout = min(_SPAWN_READY_TIMEOUT_SEC, deadline.left())
                        spawned = _KokoroWorker(
                            self.python_executable,
                            self.script_path,
                            env=_child_env(),
                            ready_timeout_sec=ready_timeout,
                        )
                        spawned_worker = spawned
                        spawn_aborted = False
                        with self._lock:
                            # Cancel during the gap before Popen: do not start the child.
                            if self._shutdown or self._exec_token is not token:
                                spawn_aborted = True
                            else:
                                # Visible to cancel_inflight before the ready handshake blocks.
                                self._worker = spawned
                        if spawn_aborted:
                            result = error_dict(WORKER_CANCELLED, "Kokoro worker spawn was cancelled.")
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
                                    result = error_dict(WORKER_CANCELLED, "Kokoro worker spawn was cancelled.")
                                elif deadline.left() < _MIN_SPAWN_REMAINING_SEC:
                                    result = _error_with_tail("EXECUTION_TIMEOUT", f"Execution exceeded maximum timeout of {eff_timeout} seconds.", spawned.stderr_tail(), with_message=True)
                                else:
                                    result = _error_with_tail("WORKER_SPAWN_FAILED", "Kokoro worker could not be started.", spawned.stderr_tail())
                            else:
                                if deadline.left() <= 0:
                                    self._retire_worker(spawned)
                                    with self._lock:
                                        if self._worker is spawned:
                                            self._worker = None
                                    msg = f"Execution exceeded maximum timeout of {eff_timeout} seconds."
                                    result = error_dict("EXECUTION_TIMEOUT", msg, message=msg)
                                else:
                                    worker = spawned

                if worker is not None:
                    result = worker.execute(payload, deadline.left())
            finally:
                with self._lock:
                    if (
                        spawned_worker is not None
                        and self._worker is spawned_worker
                        and not spawned_worker.is_alive()
                    ):
                        self._worker = None
                    # Cancel drops the token; this finally must not clear a
                    # token cancel_inflight already cleared. A second execute
                    # waits on _job_lock until this finally releases it, so it
                    # cannot write the same stdin.
                    if self._exec_token is token:
                        self._exec_token = None
                        self._last_active = time.monotonic()
                    else:
                        token_stolen = True

        if token_stolen:
            return error_dict(WORKER_CANCELLED, "Kokoro worker was cancelled.")
        raw_warning = result.get("warning")
        if isinstance(raw_warning, str):
            text = raw_warning.strip()
            if text:
                first = False
                with self._lock:
                    if text not in self._seen_warnings:
                        if len(self._seen_warnings) >= _SEEN_WARNINGS_MAX:
                            del self._seen_warnings[next(iter(self._seen_warnings))]
                        self._seen_warnings[text] = None
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
            if self._exec_token is None:
                return
            if token is not None and self._exec_token is not token:
                return
            worker = self._worker
            self._worker = None
            self._exec_token = None
        if worker is not None:
            log.info("Cancelling in-flight Kokoro synthesis")
            worker.request_shutdown()
            worker.signal()
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
            if self._exec_token is not None or self._worker is None or not self._worker.is_alive():
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
            return self._exec_token

    def shutdown(self) -> None:
        self._reaper_stop.set()
        with self._lock:
            self._shutdown = True
            worker = self._worker
            self._worker = None
            # In-flight execute treats a dropped token as cancel, so shutdown
            # does not fall through to a one-shot Kokoro clip.
            self._exec_token = None
        if worker is not None:
            self._retire_worker(worker)


_POOL: KokoroProcessPool | None = None
_POOL_LOCK = threading.Lock()
_POOL_CONFIG_VENV: str | None = None


def resolve_kokoro_python() -> str | None:
    """Venv interpreter that has kokoro-onnx, or None when Python is not configured."""
    from plugin.framework.config import get_config_str

    venv_dir = get_config_str("scripting.python_venv_path").strip()
    if not venv_dir:
        return None
    return resolve_venv_python(venv_dir)


def get_kokoro_pool() -> KokoroProcessPool | None:
    """Lazy singleton. None when no venv Python is configured."""
    global _POOL, _POOL_CONFIG_VENV
    from plugin.framework.config import get_config_str

    venv_str = get_config_str("scripting.python_venv_path").strip()
    with _POOL_LOCK:
        # Same configured path: reuse without re-resolving the interpreter.
        if venv_str and _POOL is not None and _POOL_CONFIG_VENV == venv_str:
            return _POOL
        py = resolve_venv_python(venv_str) if venv_str else None
        if _POOL is not None and _POOL.python_executable != py:
            _POOL.shutdown()
            _POOL = None
        if not py:
            _POOL_CONFIG_VENV = None
            return None
        if _POOL is None:
            _POOL = KokoroProcessPool(py)
        _POOL_CONFIG_VENV = venv_str
        return _POOL


def shutdown_kokoro_pool() -> None:
    """Tests and interpreter shutdown. Safe to call when the pool was never used."""
    global _POOL, _POOL_CONFIG_VENV
    with _POOL_LOCK:
        pool = _POOL
        _POOL = None
        _POOL_CONFIG_VENV = None
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
