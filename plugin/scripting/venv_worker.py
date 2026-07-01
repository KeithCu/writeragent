# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""Host-side venv worker: warm subprocess IPC and run_code_in_user_venv."""

from __future__ import annotations

import logging
import os
import pickle
import select
import signal
import struct
import subprocess
import sys
import threading
import time
import uuid
from typing import Any, Callable, Dict, IO

from plugin.framework.config import get_config_str, get_config_bool_safe
from plugin.framework.i18n import get_active_locale
from plugin.framework.thread_guard import background
from plugin.framework.constants import WORKER_POOL_DEFAULT, WORKER_POOL_EMBEDDINGS
from plugin.scripting.config_limits import (
    WARM_WORKER_TIMEOUT_SEC,
    configured_python_exec_timeout,
    python_exec_timeout_default,
    resolve_python_exec_timeout,
)
from plugin.scripting.payload_codec import host_unpack_data
from plugin.scripting.sandbox import (
    optimize_popen_pipes,
    resolve_libreoffice_python,
    resolve_venv_python,
    scrub_subprocess_env,
    wrap_command_for_sandbox,
)

log = logging.getLogger(__name__)

_TIMEOUT_AFTER = " timed out after "


def _worker_error_message(exc: BaseException) -> str:
    """Build a short user-facing worker error without subprocess command paths."""
    if isinstance(exc, subprocess.TimeoutExpired):
        return f"Python worker failed: timed out after {exc.timeout} seconds"
    text = str(exc)
    if text.startswith("Command ") and _TIMEOUT_AFTER in text:
        return f"Python worker failed:{text[text.index(_TIMEOUT_AFTER):]}"
    return f"Python worker failed: {text}"


_HARNESS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "venv", "worker_harness.py")
_instances: dict[str, PythonWorkerManager] = {}
_registry_lock = threading.Lock()


def _worker_registry_key(exe: str, pool: str) -> str:
    return f"{pool}:{exe}"


class PythonWorkerManager:
    """One warm child process per (pool, Python executable path) pair."""

    def __init__(self, exe: str, env: dict[str, str]) -> None:
        self.exe = exe
        self.env = dict(env)
        self._proc: subprocess.Popen[Any] | None = None
        self._io_lock = threading.Lock()
        self._primed = False

    @classmethod
    def get(cls, exe: str, env: dict[str, str], *, pool: str = WORKER_POOL_DEFAULT) -> PythonWorkerManager:
        """Return the singleton worker for *pool* + *exe* (caller should pass a scrubbed env dict)."""
        key = _worker_registry_key(exe, pool)
        with _registry_lock:
            mgr = _instances.get(key)
            if mgr is None:
                mgr = cls(exe, dict(env))
                _instances[key] = mgr
            return mgr

    @classmethod
    def shutdown_all(cls) -> None:
        """Terminate all workers (tests / extension teardown)."""
        with _registry_lock:
            for mgr in list(_instances.values()):
                mgr._terminate_worker()
            _instances.clear()

    def _is_worker_alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def _ensure_warmed_unlocked(self) -> dict[str, Any] | None:
        """Spawn worker and prime auto-imports. Returns error dict or None."""
        if self._primed and self._is_worker_alive():
            return None
        prime = self._execute_ipc_unlocked("result = None", timeout_sec=WARM_WORKER_TIMEOUT_SEC)
        if prime.get("status") != "ok":
            return prime
        self._primed = True
        return None

    def _ensure_warmed(self) -> dict[str, Any] | None:
        with self._io_lock:
            return self._ensure_warmed_unlocked()

    def warm(self) -> None:
        """Spawn the worker and trigger auto-imports (numpy etc.) so the next real execute is instant."""
        self._ensure_warmed()

    def _build_request(
        self,
        code: str | None = None,
        *,
        data: Any = None,
        session_id: str | None = None,
        action: str | None = None,
        init_script: str | None = None,
        init_session_id: str | None = None,
        init_script_hash: str | None = None,
        allow_heartbeat: bool = False,
        timeout_sec: int | None = None,
    ) -> dict[str, Any]:
        request: dict[str, Any] = {
            "id": str(uuid.uuid4()),
            "locale": get_active_locale(),
            "convert_datetime": get_config_bool_safe("scripting.python_convert_datetime"),
        }
        if timeout_sec is not None:
            request["timeout_sec"] = timeout_sec
        if action:
            request["action"] = action
            if session_id:
                request["session_id"] = session_id
            if data is not None:
                request["data"] = data
        else:
            request["code"] = code if code is not None else ""
            if data is not None:
                request["data"] = data
            if session_id:
                request["session_id"] = session_id
            if init_script:
                request["init_script"] = init_script
            if init_session_id:
                request["init_session_id"] = init_session_id
            if init_script_hash:
                request["init_script_hash"] = init_script_hash
            if allow_heartbeat:
                request["allow_heartbeat"] = True
        return request

    def _execute_ipc_unlocked(
        self,
        code: str | None = None,
        *,
        data: Any = None,
        timeout_sec: int,
        session_id: str | None = None,
        action: str | None = None,
        init_script: str | None = None,
        init_session_id: str | None = None,
        init_script_hash: str | None = None,
        allow_heartbeat: bool = False,
        heartbeat_grace_sec: int | None = None,
        on_heartbeat: Callable[[dict[str, Any]], None] | None = None,
        on_worker_event: Callable[[dict[str, Any]], None] | None = None,
        stop_checker: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        request = self._build_request(
            code,
            data=data,
            session_id=session_id,
            action=action,
            init_script=init_script,
            init_session_id=init_session_id,
            init_script_hash=init_script_hash,
            allow_heartbeat=allow_heartbeat,
            timeout_sec=timeout_sec,
        )
        payload = pickle.dumps(request, protocol=5)
        header = struct.pack("!I", len(payload))

        for attempt in range(2):
            try:
                self._ensure_running()
                assert self._proc is not None and self._proc.stdin is not None and self._proc.stdout is not None
                stdin = self._proc.stdin
                stdout = self._proc.stdout
                stdin.write(header)
                stdin.write(payload)
                stdin.flush()

                while True:
                    if allow_heartbeat:
                        from plugin.framework.constants import EMBEDDINGS_HEARTBEAT_GRACE_S

                        grace = int(heartbeat_grace_sec if heartbeat_grace_sec is not None else EMBEDDINGS_HEARTBEAT_GRACE_S)
                        response_bytes = self._read_response_with_heartbeats(
                            stdout,
                            timeout_sec,
                            grace,
                            on_heartbeat,
                        )
                    else:
                        response_bytes = self._read_response_bytes(stdout, timeout_sec)
                    if not response_bytes:
                        stderr_out = self._drain_stderr()
                        raise RuntimeError(f"Worker closed stdout without a response{stderr_out}")
                    # Trusted IPC: bytes from our own worker_harness child over a private pipe.
                    response = pickle.loads(response_bytes)  # nosec B301
                    if isinstance(response, dict):
                        from plugin.ppt_master.venv.host_rpc import dispatch_worker_response

                        def _stdin_write(blob: bytes) -> None:
                            stdin.write(blob)
                            stdin.flush()

                        if dispatch_worker_response(
                            response,
                            stdin_write=_stdin_write,
                            on_worker_event=on_worker_event,
                            stop_checker=stop_checker,
                        ):
                            continue
                    break
                return self._normalize_response(response)
            except (BrokenPipeError, pickle.UnpicklingError, RuntimeError, subprocess.TimeoutExpired, OSError) as e:
                log.warning("Python worker failed (attempt %s): %s", attempt + 1, e)
                self._terminate_worker()
                if attempt == 1:
                    return {"status": "error", "message": _worker_error_message(e)}
        return {"status": "error", "message": "Python worker failed"}

    def execute(
        self,
        code: str | None = None,
        *,
        data: Any = None,
        timeout_sec: int | None = None,
        session_id: str | None = None,
        action: str | None = None,
        init_script: str | None = None,
        init_session_id: str | None = None,
        init_script_hash: str | None = None,
        allow_heartbeat: bool = False,
        heartbeat_grace_sec: int | None = None,
        on_heartbeat: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        """Run *code* in the warm worker, or handle *action* (e.g. reset_session).

        Without *session_id*, each execute uses a fresh namespace in the child. With
        *session_id*, the child reuses one LocalPythonExecutor per id.

        Cold start: spawn + auto-imports run first under :data:`WARM_WORKER_TIMEOUT_SEC`
        and are not charged against *timeout_sec*.
        """
        if timeout_sec is None:
            timeout_sec = python_exec_timeout_default()

        with self._io_lock:
            warm_err = self._ensure_warmed_unlocked()
            if warm_err is not None:
                return warm_err
            return self._execute_ipc_unlocked(
                code,
                data=data,
                timeout_sec=timeout_sec,
                session_id=session_id,
                action=action,
                init_script=init_script,
                init_session_id=init_session_id,
                init_script_hash=init_script_hash,
                allow_heartbeat=allow_heartbeat,
                heartbeat_grace_sec=heartbeat_grace_sec,
                on_heartbeat=on_heartbeat,
            )

    def execute_ppt_master_turn(
        self,
        payload: dict[str, Any],
        *,
        timeout_sec: int,
        on_worker_event: Callable[[dict[str, Any]], None] | None = None,
        stop_checker: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        """Run one PPT-Master sidebar turn in the venv worker (LLM + scripts + host UNO RPC)."""
        with self._io_lock:
            warm_err = self._ensure_warmed_unlocked()
            if warm_err is not None:
                return warm_err
            raw = self._execute_ipc_unlocked(
                None,
                data=payload,
                timeout_sec=timeout_sec,
                action="ppt_master_turn",
                on_worker_event=on_worker_event,
                stop_checker=stop_checker,
            )
        if raw.get("status") == "error":
            return raw
        inner = raw.get("result")
        if isinstance(inner, dict):
            return inner
        return {"status": "ok", "result": str(inner) if inner is not None else ""}

    def _normalize_response(self, response: dict[str, Any]) -> dict[str, Any]:
        if response.get("status") == "ok":
            result = response.get("result")
            if result is not None:
                result = host_unpack_data(result, as_nested_list=True)
            return {
                "status": "ok",
                "result": result,
                "stdout": (response.get("stdout") or "").strip(),
                "stderr": "",
            }
        msg = response.get("message") or response.get("error") or "Unknown worker error"
        tb = response.get("traceback")
        if tb and isinstance(tb, str):
            msg = f"{msg}\n{tb.strip()}"
        out: dict[str, Any] = {
            "status": "error",
            "message": str(msg),
            "stdout": (response.get("stdout") or "").strip(),
        }
        return out

    def _ensure_running(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            return
        self._terminate_worker()
        popen_kw: dict[str, Any] = {
            "stdin": subprocess.PIPE,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "env": self.env,
            "text": False,
            "bufsize": 0,
        }
        if sys.platform == "win32":
            popen_kw["creationflags"] = subprocess.CREATE_NO_WINDOW
        else:
            popen_kw["preexec_fn"] = os.setsid
        self._proc = subprocess.Popen(wrap_command_for_sandbox([self.exe, _HARNESS_PATH]), **popen_kw)
        optimize_popen_pipes(self._proc)
        log.debug("Started Python worker pid=%s exe=%s", self._proc.pid, self.exe)

    def _read_response_bytes(self, stdout: IO[bytes], timeout_sec: int) -> bytes:
        assert self._proc is not None
        # Windows select.select() only supports sockets, not pipes (raises
        # WinError 10038).  Use a thread-based blocking read there instead.
        if sys.platform == "win32":
            return self._read_response_bytes_threaded(stdout, timeout_sec)
        return self._read_response_bytes_select(stdout, timeout_sec)

    def _read_response_bytes_select(self, stdout: IO[bytes], timeout_sec: int) -> bytes:
        """POSIX path: use select() to poll the pipe with a timeout."""
        assert self._proc is not None
        end = time.time() + timeout_sec

        def _read_exact(n: int) -> bytes:
            buf = bytearray()
            while len(buf) < n:
                if time.time() >= end:
                    raise subprocess.TimeoutExpired(cmd=self.exe, timeout=timeout_sec)
                remaining = end - time.time()
                ready, _, _ = select.select([stdout], [], [], min(1.0, remaining))
                if ready:
                    chunk = stdout.read(n - len(buf))
                    if not chunk:
                        return bytes()
                    buf.extend(chunk)
                if self._proc is not None and self._proc.poll() is not None and not ready:
                    break
            return bytes(buf)

        header = _read_exact(4)
        if len(header) < 4:
            return b""

        size = struct.unpack("!I", header)[0]
        payload = _read_exact(size)
        if len(payload) < size:
            return b""

        return payload

    def _read_response_bytes_threaded(self, stdout: IO[bytes], timeout_sec: int) -> bytes:
        """Windows path: blocking read in a daemon thread with join-timeout."""
        result: list[bytes] = [b""]
        error: list[BaseException | None] = [None]

        def _reader() -> None:
            try:
                header = stdout.read(4)
                if not header or len(header) < 4:
                    return
                size = struct.unpack("!I", header)[0]
                payload = stdout.read(size)
                if len(payload) < size:
                    return
                result[0] = payload
            except Exception as exc:
                error[0] = exc

        t = threading.Thread(target=_reader, daemon=True)
        t.start()
        t.join(timeout=timeout_sec)
        if t.is_alive():
            raise subprocess.TimeoutExpired(cmd=self.exe, timeout=timeout_sec)
        if error[0] is not None:
            raise error[0]
        return result[0]

    def _read_exact_before_deadline(self, stdout: IO[bytes], nbytes: int, deadline: float) -> bytes:
        if sys.platform == "win32":
            result: list[bytes] = [b""]

            def _reader() -> None:
                result[0] = stdout.read(nbytes)

            t = threading.Thread(target=_reader, daemon=True)
            t.start()
            t.join(timeout=max(0.1, deadline - time.time()))
            if t.is_alive():
                raise subprocess.TimeoutExpired(cmd=self.exe, timeout=max(1, int(deadline - time.time())))
            return result[0] or b""

        buf = bytearray()
        while len(buf) < nbytes:
            if time.time() >= deadline:
                raise subprocess.TimeoutExpired(cmd=self.exe, timeout=max(1, int(deadline - time.time())))
            remaining = deadline - time.time()
            ready, _, _ = select.select([stdout], [], [], min(1.0, remaining))
            if ready:
                chunk = stdout.read(nbytes - len(buf))
                if not chunk:
                    break
                buf.extend(chunk)
            if self._proc is not None and self._proc.poll() is not None and not ready:
                break
        return bytes(buf)

    def _read_response_with_heartbeats(
        self,
        stdout: IO[bytes],
        timeout_sec: int,
        grace_sec: int,
        on_heartbeat: Callable[[dict[str, Any]], None] | None,
    ) -> bytes:
        from plugin.scripting.venv.worker_heartbeat import FRAME_HEARTBEAT, FRAME_RESULT, parse_frame

        deadline_holder = [time.time() + max(timeout_sec, grace_sec)]

        def _read_exact(n: int) -> bytes:
            return self._read_exact_before_deadline(stdout, n, deadline_holder[0])

        while True:
            frame_bytes = self._read_frame_bytes(stdout, _read_exact)
            if not frame_bytes:
                return b""
            data = parse_frame(frame_bytes)
            frame_type = data.get("frame_type")
            if frame_type == FRAME_HEARTBEAT:
                payload = data.get("payload")
                if on_heartbeat is not None and isinstance(payload, dict):
                    on_heartbeat(payload)
                deadline_holder[0] = time.time() + grace_sec
                continue
            if frame_type == FRAME_RESULT or frame_type is None:
                return frame_bytes
            if data.get("status") in ("ok", "error"):
                return frame_bytes

    def _read_frame_bytes(self, stdout: IO[bytes], read_exact: Callable[[int], bytes]) -> bytes:
        header = read_exact(4)
        if len(header) < 4:
            return b""
        size = struct.unpack("!I", header)[0]
        payload = read_exact(size)
        if len(payload) < size:
            return b""
        return payload

    def _drain_stderr(self) -> str:
        """Read any pending stderr from the crashed worker for diagnostics."""
        if self._proc is None or self._proc.stderr is None:
            return ""
        try:
            # Worker already exited (stdout closed); wait briefly then read stderr.
            self._proc.wait(timeout=2)
        except Exception:
            pass
        try:
            stderr_bytes = self._proc.stderr.read()
        except Exception:
            return ""
        if not stderr_bytes:
            return ""
        text = stderr_bytes.decode("utf-8", errors="replace").strip()
        return f"\nWorker stderr:\n{text}"

    def _terminate_worker(self) -> None:
        proc = self._proc
        self._proc = None
        self._primed = False
        if proc is None:
            return
        try:
            if proc.poll() is None:
                if sys.platform == "win32":
                    proc.kill()
                else:
                    try:
                        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                    except ProcessLookupError:
                        proc.kill()
            proc.wait(timeout=5)
        except (subprocess.TimeoutExpired, ProcessLookupError, OSError):
            try:
                proc.kill()
            except OSError:
                pass


# --- Public entrypoints ---


def _resolve_worker_python(
    uno_ctx: Any,
    *,
    pool: str = WORKER_POOL_DEFAULT,
) -> tuple[str | None, dict[str, Any] | None]:
    """Return (exe, error_response) for the configured venv / LO interpreter."""
    venv_dir = get_config_str("scripting.python_venv_path").strip()

    if pool == WORKER_POOL_EMBEDDINGS:
        if not venv_dir:
            return None, {
                "status": "error",
                "message": (
                    "Embeddings require a configured Python venv (Settings → Python). "
                    "LibreOffice embedded Python cannot run sentence-transformers or langgraph."
                ),
            }
        exe = resolve_venv_python(venv_dir)
        if not exe:
            return None, {
                "status": "error",
                "message": f"Embeddings venv not configured or invalid: {venv_dir!r}",
            }
        log.debug("run_venv_code: using embeddings venv interpreter under %s", venv_dir)
        return exe, None

    if venv_dir:
        exe = resolve_venv_python(venv_dir)
        if not exe:
            return None, {
                "status": "error",
                "message": f"No python executable found under configured venv: {venv_dir!r}",
            }
        log.debug("run_venv_code: using venv interpreter under %s", venv_dir)
        return exe, None
    exe = resolve_libreoffice_python()
    if not exe:
        return None, {
            "status": "error",
            "message": (
                "Could not resolve a Python interpreter (sys.executable missing, not a file, or not executable). "
                "Set scripting.python_venv_path in Settings → Python for a dedicated venv, or fix the LibreOffice install."
            ),
        }
    log.debug("run_venv_code: using process interpreter %s (no venv path set)", exe)
    return exe, None


def _worker_manager_for_ctx(
    uno_ctx: Any,
    *,
    pool: str = WORKER_POOL_DEFAULT,
) -> tuple[PythonWorkerManager | None, dict[str, Any] | None]:
    exe, err = _resolve_worker_python(uno_ctx, pool=pool)
    if err is not None:
        return None, err
    assert exe is not None
    child_env = scrub_subprocess_env(dict(os.environ))
    child_env["WRITERAGENT_IS_WORKER"] = "1"
    return PythonWorkerManager.get(exe, child_env, pool=pool), None


def run_code_in_user_venv(
    uno_ctx: Any,
    code: str,
    *,
    data: Any = None,
    timeout_sec: int | None = None,
    session_id: str | None = None,
    init_script: str | None = None,
    init_session_id: str | None = None,
    init_script_hash: str | None = None,
    active_domain: str | None = None,
    python_tool_domain: str | None = None,
    worker_pool: str = WORKER_POOL_DEFAULT,
    allow_heartbeat: bool = False,
    heartbeat_grace_sec: int | None = None,
    on_heartbeat: Callable[[dict[str, Any]], None] | None = None,
) -> Dict[str, Any]:
    """Execute *code* via :class:`PythonWorkerManager` (warm process).

    Without *session_id*, each call uses an isolated namespace in the child. With
    *session_id*, the child reuses one namespace per workbook (shared kernel).

    *worker_pool* selects which warm child to use (e.g. embeddings vs Calc/chat default).

    *active_domain* / *python_tool_domain* are reserved for future venv→LO tool RPC (not wired yet).
    """
    del active_domain, python_tool_domain  # deferred — see docs/enabling_numpy_in_libreoffice.md §7
    if not (code or "").strip():
        return {"status": "error", "message": "No code provided."}

    manager, err = _worker_manager_for_ctx(uno_ctx, pool=worker_pool)
    if err is not None:
        return err
    assert manager is not None

    configured = configured_python_exec_timeout(uno_ctx)
    timeout_sec = resolve_python_exec_timeout(timeout_sec, configured=configured)

    return manager.execute(
        code,
        data=data,
        timeout_sec=timeout_sec,
        session_id=session_id,
        init_script=init_script,
        init_session_id=init_session_id,
        init_script_hash=init_script_hash,
        allow_heartbeat=allow_heartbeat,
        heartbeat_grace_sec=heartbeat_grace_sec,
        on_heartbeat=on_heartbeat,
    )


def reset_python_session(uno_ctx: Any, session_id: str, *, timeout_sec: int | None = None) -> Dict[str, Any]:
    """Drop the shared-kernel executor for *session_id* in the warm worker."""
    if not (session_id or "").strip():
        return {"status": "error", "message": "No session_id provided."}

    manager, err = _worker_manager_for_ctx(uno_ctx)
    if err is not None:
        return err
    assert manager is not None

    configured = configured_python_exec_timeout(uno_ctx)
    timeout_sec = resolve_python_exec_timeout(timeout_sec, configured=configured)

    return manager.execute(
        None,
        timeout_sec=timeout_sec,
        session_id=session_id,
        action="reset_session",
    )


@background
def warm_venv_worker(uno_ctx: Any, pool: str = WORKER_POOL_DEFAULT) -> None:
    """Pre-warm a specific venv subprocess pool (spawn + trigger auto-imports + load embedding model if embeddings pool). Safe to call from a background thread."""
    exe, err = _resolve_worker_python(uno_ctx, pool=pool)
    if err is not None:
        log.warning("warm_venv_worker skipped for pool %s: %s", pool, err.get("message"))
        return
    assert exe is not None
    child_env = scrub_subprocess_env(dict(os.environ))

    manager = PythonWorkerManager.get(exe, child_env, pool=pool)
    manager.warm()

    # Pre-load the active embedding model inside the embeddings pool worker so first query executes instantly
    if pool == WORKER_POOL_EMBEDDINGS:
        try:
            from plugin.framework.client.embedding_client import get_embedding_model
            model = get_embedding_model()
            if model:
                code = (
                    f"from plugin.embeddings.venv.embeddings_index import _get_embedder\n"
                    f"_get_embedder({model!r})\n"
                )
                res = manager.execute(code, data={"model": model})
                if res.get("status") != "ok":
                    log.warning("Embedding model pre-warm returned status %s: %s", res.get("status"), res.get("message"))
        except Exception:
            log.exception("Failed to warm embedding model")


# Backward-compatible re-exports (diagnostics + sandbox helpers used via venv_worker today).
from plugin.scripting.sandbox import detect_sandbox  # noqa: E402
from plugin.scripting.venv_diagnostics import (  # noqa: E402
    probe_venv_path,
    probe_venv_path_with_progress,
    run_venv_self_check,
    run_venv_self_check_with_progress,
)

__all__ = [
    "PythonWorkerManager",
    "detect_sandbox",
    "probe_venv_path",
    "probe_venv_path_with_progress",
    "reset_python_session",
    "resolve_libreoffice_python",
    "resolve_venv_python",
    "run_code_in_user_venv",
    "run_venv_self_check",
    "run_venv_self_check_with_progress",
    "scrub_subprocess_env",
    "warm_venv_worker",
    "wrap_command_for_sandbox",
]

# Re-export path/env helpers for callers that still import from venv_worker.
