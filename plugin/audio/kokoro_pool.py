# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""One warm Kokoro worker on the process-wide warm-child Supervisor.

The child speaks Pickle 5 frames via ``kokoro_worker.run_kokoro_stdio_loop``.
``kokoro_worker`` emits ready before importing ``kokoro_onnx``, so the spawn
timeout covers process start only and model load counts against the first job.
``ChildSpec.idle_ttl_sec`` (default 300s) evicts idle ONNX via the supervisor
reaper. Stop on an utterance registers ``WarmChild.kill()`` on ``SendCancellation``.

Kokoro and soundfile live in the configured venv, not in LibreOffice's runtime.
Quit is ``supervisor.shutdown_all()`` (``main.py``); this module does not register
a separate shutdown hook.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import TYPE_CHECKING, Any

from plugin.framework.process_worker import Deadline, error_dict
from plugin.framework.subprocess_env import scrub_subprocess_env
from plugin.framework.warm_child import ChildSpec, Supervisor, WarmChild, get_supervisor
from plugin.scripting.sandbox import resolve_venv_python

if TYPE_CHECKING:
    from plugin.framework.send_cancellation import SendCancellation

log = logging.getLogger(__name__)

_SCRIPT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "kokoro_worker.py")
_SPAWN_READY_TIMEOUT_SEC = 20.0
_DEFAULT_JOB_TIMEOUT_SEC = 120.0
# Long enough to cover a chat reply; short enough an abandoned CPU model does not sit in RAM.
_DEFAULT_IDLE_TTL_SEC = 300.0
_SEEN_WARNINGS_MAX = 32
WORKER_CANCELLED = "WORKER_CANCELLED"


def _child_env() -> dict[str, str]:
    """Scrub secrets and LibreOffice PYTHONPATH, then point at the extension."""
    env = scrub_subprocess_env(dict(os.environ))
    # scrub_subprocess_env strips PYTHONPATH on purpose (LO's bundled stdlib breaks venv imports).
    env["PYTHONPATH"] = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir, os.pardir))
    return env


class KokoroProcessPool:
    """Single warm Kokoro worker registered with the warm-child Supervisor."""

    def __init__(
        self,
        python_executable: str,
        *,
        script_path: str | None = None,
        idle_worker_ttl_sec: float | None = _DEFAULT_IDLE_TTL_SEC,
        supervisor: Supervisor | None = None,
    ) -> None:
        self.python_executable: str = python_executable
        self.script_path: str = script_path or _SCRIPT_PATH
        self.job_timeout_sec: float = _DEFAULT_JOB_TIMEOUT_SEC
        ttl = idle_worker_ttl_sec
        if ttl is not None and ttl <= 0:
            ttl = None
        spec = ChildSpec(
            name="kokoro",
            argv=[python_executable, self.script_path],
            env=_child_env(),
            ready_timeout_sec=_SPAWN_READY_TIMEOUT_SEC,
            idle_ttl_sec=ttl,
        )
        sup = supervisor if supervisor is not None else get_supervisor()
        self._warm: WarmChild = WarmChild(spec, supervisor=sup)
        self._job_lock: threading.Lock = threading.Lock()
        self._state_lock: threading.Lock = threading.Lock()
        self._shutdown: bool = False
        self._job_live: bool = False
        self._abort: bool = False
        self._seen_warnings: dict[str, None] = {}

    def execute(
        self,
        payload: dict[str, Any],
        timeout_sec: float | None = None,
        cancel: SendCancellation | None = None,
    ) -> dict[str, Any]:
        """Run one Kokoro job. Spawns the child on first use and after a crash."""
        eff_timeout = float(self.job_timeout_sec if timeout_sec is None else timeout_sec)
        with self._job_lock:
            if self._shutdown:
                return error_dict("WORKER_SHUTDOWN", "Kokoro pool is shut down.")

            if cancel is not None and cancel.is_cancelled():
                return error_dict(WORKER_CANCELLED, "Kokoro worker was cancelled.")

            with self._state_lock:
                self._job_live = True
                self._abort = False
            if cancel is not None:
                cancel.register_on_cancel(self._on_cancel)

            deadline = Deadline(eff_timeout)
            result: dict[str, Any] = {}
            try:
                result = self._warm.request(payload, deadline)
            finally:
                if cancel is not None:
                    cancel.unregister_on_cancel(self._on_cancel)
                with self._state_lock:
                    self._job_live = False

            with self._state_lock:
                abort = self._abort
            if abort:
                return error_dict(WORKER_CANCELLED, "Kokoro worker was cancelled.")

            if self._shutdown:
                return error_dict("WORKER_SHUTDOWN", "Kokoro pool is shut down.")

            if result.get("status") == "error":
                return result

        raw_warning = result.get("warning")
        if isinstance(raw_warning, str):
            text = raw_warning.strip()
            if text:
                first = False
                with self._job_lock:
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

    def _on_cancel(self) -> None:
        # Pool-wide hook: kills whatever job is live. Safe because utterance scopes are
        # cancelled under _speech_lock before the next utterance can start.
        with self._state_lock:
            if not self._job_live:
                return
            self._abort = True
            self._job_live = False
        log.info("Cancelling in-flight Kokoro synthesis")
        self._warm.kill()

    def shutdown(self) -> None:
        """Retire the warm child (tests and venv-path changes)."""
        with self._job_lock:
            self._shutdown = True
            with self._state_lock:
                self._abort = True
                self._job_live = False
        self._warm.retire()

    def is_warm_alive(self) -> bool:
        return self._warm.is_alive()


_POOL: KokoroProcessPool | None = None
_POOL_LOCK = threading.Lock()
_POOL_CONFIG_VENV: str | None = None


def resolve_kokoro_python() -> str | None:
    """Venv interpreter that has kokoro-onnx, or None when Python is not configured."""
    from plugin.framework.config import get_config

    venv_dir = str(get_config("scripting.python_venv_path") or "").strip()
    if not venv_dir:
        return None
    return resolve_venv_python(venv_dir)


def get_kokoro_pool() -> KokoroProcessPool | None:
    """Lazy singleton. None when no venv Python is configured."""
    global _POOL, _POOL_CONFIG_VENV
    from plugin.framework.config import get_config

    venv_str = str(get_config("scripting.python_venv_path") or "").strip()
    with _POOL_LOCK:
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
