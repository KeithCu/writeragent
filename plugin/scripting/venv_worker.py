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
import select
import signal
import subprocess
import sys
import threading
import time
import uuid
from typing import Any, Callable, Dict, IO

from plugin.framework.config import get_config_str
from plugin.framework.thread_guard import background
from plugin.framework.constants import WORKER_POOL_DEFAULT, WORKER_POOL_EMBEDDINGS
from plugin.framework.worker_pool import StderrTail, get_subprocess_creationflags, start_stderr_drain
from plugin.scripting.config_limits import (
    HOST_IPC_READ_GRACE_SEC,
    VENV_IPC_WRITE_TIMEOUT_SEC,
    WARM_WORKER_TIMEOUT_SEC,
    configured_python_exec_timeout,
    python_exec_timeout_default,
    resolve_python_exec_timeout,
)
from plugin.scripting.ipc import (
    DEFAULT_MAX_PAYLOAD_BYTES,
    pack_pickle_frame,
    read_frame_payload,
    unpack_pickle_frame,
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


def _worker_error(code: str, message: str, *, details: dict[str, Any] | None = None) -> dict[str, Any]:
    """Host-constructed error dict. Child payloads still go through ``_normalize_response``."""
    return {
        "status": "error",
        "code": code,
        "message": message,
        "details": details or {},
    }


class _NonReplayableIpcWriteTimeout(RuntimeError):
    """A mid-turn host response timed out after side effects may have occurred."""


_SHARED_WORKER_RESTART_HINT = " Shared Python process restarted (all workbooks)."


def _clear_host_state_after_worker_death() -> None:
    """IPC is desynced after a kill; drop add-in scalar cache so the next turn is cold.

    Do **not** clear ``_RECORDED_CALC_SESSION_IDS``. Off-main Shared ``=PY()``
    needs that single id (leftover after cap-hit / worker restart otherwise
    sees ``recorded=0`` and Isolated ``x_geo_live`` undefined). The new
    worker is a fresh namespace; the host still knows which workbook it is.
    """
    try:
        from plugin.calc.python.function import clear_python_addin_cache

        clear_python_addin_cache()
    except Exception:
        log.debug("venv_worker: clear_python_addin_cache after death failed", exc_info=True)


def _worker_error_message(exc: BaseException) -> str:
    """Build a short user-facing worker error without subprocess command paths."""
    if isinstance(exc, subprocess.TimeoutExpired):
        return f"Python worker failed: timed out after {exc.timeout} seconds"
    text = str(exc)
    if text.startswith("Command ") and _TIMEOUT_AFTER in text:
        return f"Python worker failed:{text[text.index(_TIMEOUT_AFTER):]}"
    return f"Python worker failed: {text}"


def _maybe_dispatch_ppt_master_response(
    response: dict[str, Any],
    *,
    stdin_write: Callable[[bytes], None],
    on_worker_event: Callable[[dict[str, Any]], None] | None = None,
    stop_checker: Callable[[], bool] | None = None,
) -> bool:
    """Handle ppt-master intermediate worker frames; no-op when ppt_master is not bundled."""
    try:
        from plugin.ppt_master.venv.host_rpc import dispatch_worker_response
    except ImportError:
        return False
    return dispatch_worker_response(
        response,
        stdin_write=stdin_write,
        on_worker_event=on_worker_event,
        stop_checker=stop_checker,
    )


def _maybe_dispatch_intermediate_response(
    response: dict[str, Any],
    *,
    stdin_write: Callable[[bytes], None],
    allowed_tools: frozenset[str] | None = None,
    caller: str = "script",
    on_worker_event: Callable[[dict[str, Any]], None] | None = None,
    stop_checker: Callable[[], bool] | None = None,
) -> bool:
    """Handle tool_call (any build) then ppt-master llm_request / worker_event frames."""
    from plugin.scripting.host_rpc import handle_tool_call_frame

    # Tool RPC is the shared venv→LO path (Run Python Script, chat python, ppt-master).
    # Handle it here so LibrePy / WriterAgent-without-ppt_master still round-trip.
    if handle_tool_call_frame(
        response,
        stdin_write=stdin_write,
        allowed_tools=allowed_tools,
        caller=caller,
    ):
        return True
    return _maybe_dispatch_ppt_master_response(
        response,
        stdin_write=stdin_write,
        on_worker_event=on_worker_event,
        stop_checker=stop_checker,
    )


_HARNESS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "venv", "worker_harness.py")
_instances: dict[str, PythonWorkerManager] = {}
_registry_lock = threading.Lock()


def _worker_registry_key(exe: str, pool: str) -> str:
    return f"{pool}:{exe}"


def pid_is_alive(pid: int) -> bool:
    """True if *pid* still names a live process.

    ``os.kill(pid, 0)`` is POSIX. On Windows signal 0 is WinError 87, so a
    naive helper treated every live grandchild as dead (CI 33453184665).
    """
    if pid <= 0:
        return False
    if sys.platform == "win32":
        return _pid_is_alive_win32(int(pid))
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _pid_is_alive_win32(pid: int) -> bool:
    if sys.platform != "win32":
        return False
    import ctypes

    # PROCESS_QUERY_LIMITED_INFORMATION: exists-check without PROCESS_ALL_ACCESS.
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.OpenProcess(0x1000, False, pid)
    if handle:
        kernel32.CloseHandle(handle)
        return True
    # ACCESS_DENIED (5): process exists but this token cannot query it.
    return ctypes.get_last_error() == 5


def _kill_process_tree(proc: subprocess.Popen[Any]) -> None:
    """Kill *proc* and its descendants (POSIX process group, Windows ``taskkill /T``)."""
    if proc.poll() is not None:
        return
    if sys.platform == "win32":
        _kill_process_tree_win32(proc)
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except ProcessLookupError:
        proc.kill()


def _kill_process_tree_win32(proc: subprocess.Popen[Any]) -> None:
    """Terminate the Windows process tree; ``TerminateProcess`` does not kill grandchildren."""
    pid = proc.pid
    if not pid:
        proc.kill()
        return
    try:
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
            **get_subprocess_creationflags(),
        )
    except (OSError, subprocess.TimeoutExpired):
        proc.kill()
        return
    if proc.poll() is None:
        proc.kill()


class PythonWorkerManager:
    """One warm child process per (pool, Python executable path) pair."""

    def __init__(self, exe: str, env: dict[str, str]) -> None:
        self.exe = exe
        self.env = dict(env)
        self.env["WRITERAGENT_IS_WORKER"] = "1"
        self._proc: subprocess.Popen[Any] | None = None
        self._io_lock = threading.Lock()
        self._primed = False
        self._stderr_drain: StderrTail | None = None
        self._stdin_writer_thread: threading.Thread | None = None

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
        bindings: dict[str, Any] | None = None,
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
        }
        if timeout_sec is not None:
            request["timeout_sec"] = timeout_sec
        # Later: action and code branches both set data/session_id — fold common
        # keys if editing this function.
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
            if bindings:
                request["bindings"] = bindings
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
        bindings: dict[str, Any] | None = None,
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
        python_tool_domain: str | None = None,
        caller: str = "script",
    ) -> dict[str, Any]:
        request = self._build_request(
            code,
            data=data,
            bindings=bindings,
            session_id=session_id,
            action=action,
            init_script=init_script,
            init_session_id=init_session_id,
            init_script_hash=init_script_hash,
            allow_heartbeat=allow_heartbeat,
            timeout_sec=timeout_sec,
        )
        for attempt in range(2):
            try:
                self._ensure_running()
                assert self._proc is not None and self._proc.stdin is not None and self._proc.stdout is not None
                stdin = self._proc.stdin
                stdout = self._proc.stdout
                write_timeout_sec = min(float(timeout_sec), float(VENV_IPC_WRITE_TIMEOUT_SEC))
                self._write_frame_with_timeout(stdin, request, timeout_sec=write_timeout_sec, label="request")

                # The host read timeout includes a grace buffer so the child's in-process
                # signal/thread timeout fires first and returns a clean error frame without
                # terminating the warm subprocess (preserving shared workbook sessions).
                host_read_timeout_sec = float(timeout_sec) + float(HOST_IPC_READ_GRACE_SEC)
                from plugin.scripting.host_rpc import resolve_allowed_tools

                allowed_tools = resolve_allowed_tools(python_tool_domain)

                try:
                    while True:
                        if allow_heartbeat:
                            from plugin.framework.constants import EMBEDDINGS_HEARTBEAT_GRACE_S

                            grace = int(heartbeat_grace_sec if heartbeat_grace_sec is not None else EMBEDDINGS_HEARTBEAT_GRACE_S)
                            response_bytes = self._read_response_with_heartbeats(
                                stdout,
                                host_read_timeout_sec,
                                grace,
                                on_heartbeat,
                            )
                        else:
                            response_bytes = self._read_response_bytes(stdout, host_read_timeout_sec)
                        if not response_bytes:
                            stderr_out = self._drain_stderr()
                            raise RuntimeError(f"Worker closed stdout without a response{stderr_out}")
                        response = unpack_pickle_frame(response_bytes)
                        if not isinstance(response, dict):
                            raise RuntimeError("Worker response must be a dict")
                        if isinstance(response, dict):
                            def _stdin_write(blob: bytes) -> None:
                                try:
                                    self._write_bytes_with_timeout(
                                        stdin,
                                        blob,
                                        timeout_sec=write_timeout_sec,
                                        label="host RPC response",
                                    )
                                except subprocess.TimeoutExpired as exc:
                                    # The worker requested host work before this write. Retrying the
                                    # whole turn could duplicate UNO mutations already performed.
                                    raise _NonReplayableIpcWriteTimeout(
                                        f"host RPC response timed out after {write_timeout_sec:g} seconds"
                                    ) from exc

                            if _maybe_dispatch_intermediate_response(
                                response,
                                stdin_write=_stdin_write,
                                allowed_tools=allowed_tools,
                                caller=caller,
                                on_worker_event=on_worker_event,
                                stop_checker=stop_checker,
                            ):
                                continue
                        break
                except subprocess.TimeoutExpired as e:
                    # User code / C-extension hung: killing and replaying would double the wait.
                    log.warning("Python worker read timed out: %s", e)
                    self._terminate_worker()
                    _clear_host_state_after_worker_death()
                    return _worker_error(
                        "VENV_TIMEOUT",
                        _worker_error_message(e) + _SHARED_WORKER_RESTART_HINT,
                        details={"timeout_sec": timeout_sec, "exe": self.exe},
                    )
                return self._normalize_response(response)
            except _NonReplayableIpcWriteTimeout as e:
                log.warning("Python worker failed without replay: %s", e)
                self._terminate_worker()
                _clear_host_state_after_worker_death()
                return _worker_error(
                    "WORKER_IPC_ERROR",
                    f"Python worker failed: {e}{_SHARED_WORKER_RESTART_HINT}",
                    details={"exe": self.exe},
                )
            except (BrokenPipeError, ValueError, RuntimeError, subprocess.TimeoutExpired, OSError) as e:
                # TimeoutExpired here is an initial stdin write timeout only; retry once on a
                # fresh worker. Host read timeouts return above without replay.
                log.warning("Python worker failed (attempt %s): %s", attempt + 1, e)
                self._terminate_worker()
                _clear_host_state_after_worker_death()
                if attempt == 1:
                    code_val = "VENV_TIMEOUT" if isinstance(e, subprocess.TimeoutExpired) else "WORKER_IPC_ERROR"
                    return _worker_error(
                        code_val,
                        _worker_error_message(e),
                        details={"exe": self.exe, "attempt": attempt + 1},
                    )
        return _worker_error("WORKER_IPC_ERROR", "Python worker failed", details={"exe": self.exe})


    def execute(
        self,
        code: str | None = None,
        *,
        data: Any = None,
        bindings: dict[str, Any] | None = None,
        timeout_sec: int | None = None,
        session_id: str | None = None,
        action: str | None = None,
        init_script: str | None = None,
        init_session_id: str | None = None,
        init_script_hash: str | None = None,
        allow_heartbeat: bool = False,
        heartbeat_grace_sec: int | None = None,
        on_heartbeat: Callable[[dict[str, Any]], None] | None = None,
        python_tool_domain: str | None = None,
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
                bindings=bindings,
                timeout_sec=timeout_sec,
                session_id=session_id,
                action=action,
                init_script=init_script,
                init_session_id=init_session_id,
                init_script_hash=init_script_hash,
                allow_heartbeat=allow_heartbeat,
                heartbeat_grace_sec=heartbeat_grace_sec,
                on_heartbeat=on_heartbeat,
                python_tool_domain=python_tool_domain,
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
        try:
            import plugin.ppt_master  # noqa: F401  # pyright: ignore[reportUnusedImport]
        except ImportError:
            return _worker_error(
                "WORKER_IPC_ERROR",
                "PPT-Master is not available in this extension build.",
            )
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
                caller="ppt_master_venv",
            )
        if raw.get("status") == "error":
            return raw
        inner = raw.get("result")
        if isinstance(inner, dict):
            return inner
        return {"status": "ok", "result": str(inner) if inner is not None else ""}

    def _write_frame_with_timeout(
        self,
        stdin: IO[bytes],
        message: Any,
        *,
        timeout_sec: float,
        label: str,
    ) -> None:
        """Serialize one frame, then bound only the potentially blocking pipe write."""
        frame = pack_pickle_frame(message, max_payload_bytes=DEFAULT_MAX_PAYLOAD_BYTES)
        self._write_bytes_with_timeout(stdin, frame, timeout_sec=timeout_sec, label=label)

    def _write_bytes_with_timeout(
        self,
        stdin: IO[bytes],
        payload: bytes,
        *,
        timeout_sec: float,
        label: str,
    ) -> None:
        """Write and flush bytes without allowing a stalled child to hold ``_io_lock`` forever.

        Skip a reusable writer thread: this per-write daemon is how a stalled child
        cannot hold ``_io_lock``. Thread creation is cheap vs a venv round-trip;
        a pooled writer adds shutdown races on Windows pipes. Measure before changing.
        """
        errors: list[Exception] = []

        def _writer() -> None:
            try:
                stdin.write(payload)
                stdin.flush()
            except Exception as exc:
                errors.append(exc)

        writer = threading.Thread(
            target=_writer,
            name=f"venv-stdin-{label.replace(' ', '-').lower()}",
            daemon=True,
        )
        self._stdin_writer_thread = writer
        writer.start()
        writer.join(timeout=max(0.01, timeout_sec))
        if writer.is_alive():
            # Previously a child that stopped reading stdin left this thread and the
            # caller blocked in write()/flush() while _io_lock serialized the whole
            # pool. Killing the child closes the pipe reader and unblocks the writer.
            log.warning("%s write timed out after %ss; terminating Python worker", label, timeout_sec)
            self._terminate_worker()
            _clear_host_state_after_worker_death()
            writer.join(timeout=5)
            if writer.is_alive():
                log.error("%s writer thread remained blocked after worker termination", label)
            raise subprocess.TimeoutExpired(cmd=self.exe, timeout=timeout_sec)
        if errors:
            raise errors[0]

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
        out = _worker_error(
            response.get("code") or "VENV_EXEC_ERROR",
            str(msg),
            details=response.get("details") or {},
        )
        out["stdout"] = (response.get("stdout") or "").strip()
        out["traceback"] = str(tb or "")
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
        # Live stderr drain: prevent 64KB pipe deadlock while parent blocks on stdin/stdout.
        self._stderr_drain = start_stderr_drain(
            self._proc.stderr,
            name=f"venv-stderr-{self._proc.pid}",
        )
        log.debug("Started Python worker pid=%s exe=%s", self._proc.pid, self.exe)

    def _read_response_bytes(self, stdout: IO[bytes], timeout_sec: float | int) -> bytes:
        assert self._proc is not None
        # Do not merge this with ipc.read_pickle_frame_with_timeout: the worker
        # path also poll()-short-circuits a dead child and (on the heartbeat
        # path) resets the deadline. Unifying those is a hang-regression risk
        # for =PY(). Windows select.select() only supports sockets, not pipes
        # (WinError 10038); use a thread-based blocking read there instead.
        if sys.platform == "win32":
            return self._read_response_bytes_threaded(stdout, timeout_sec)
        return self._read_response_bytes_select(stdout, timeout_sec)

    def _read_response_bytes_select(self, stdout: IO[bytes], timeout_sec: float | int) -> bytes:
        """POSIX path: use select() to poll the pipe with a timeout."""
        assert self._proc is not None
        end = time.time() + timeout_sec

        def _read_exact(n: int) -> bytes:
            buf = bytearray()
            while len(buf) < n:
                if time.time() >= end:
                    raise subprocess.TimeoutExpired(cmd=self.exe, timeout=timeout_sec)
                remaining = end - time.time()
                ready, _unused, _unused2 = select.select([stdout], [], [], min(1.0, remaining))
                if ready:
                    chunk = stdout.read(n - len(buf))
                    if not chunk:
                        return bytes()
                    buf.extend(chunk)
                if self._proc is not None and self._proc.poll() is not None and not ready:
                    break
            return bytes(buf)

        return (
            read_frame_payload(
                stdout,
                read_exact=_read_exact,
                max_payload_bytes=DEFAULT_MAX_PAYLOAD_BYTES,
                frame_label="venv worker frame",
            )
            or b""
        )

    def _read_response_bytes_threaded(self, stdout: IO[bytes], timeout_sec: float | int) -> bytes:
        """Windows path: blocking read in a daemon thread with join-timeout."""
        result: list[bytes] = [b""]
        error: list[BaseException | None] = [None]

        def _reader() -> None:
            try:
                result[0] = (
                    read_frame_payload(
                        stdout,
                        max_payload_bytes=DEFAULT_MAX_PAYLOAD_BYTES,
                        frame_label="venv worker frame",
                    )
                    or b""
                )
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
            ready, _unused, _unused2 = select.select([stdout], [], [], min(1.0, remaining))
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
        timeout_sec: float | int,
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
        return (
            read_frame_payload(
                stdout,
                read_exact=read_exact,
                max_payload_bytes=DEFAULT_MAX_PAYLOAD_BYTES,
                frame_label="venv worker frame",
            )
            or b""
        )

    def _drain_stderr(self) -> str:
        """Return bounded stderr captured by the live drain thread (crash diagnostics)."""
        drain = self._stderr_drain
        if drain is not None:
            text = drain.text().strip()
            return f"\nWorker stderr:\n{text}" if text else ""
        # Fallback if spawn raced before the drain was attached.
        if self._proc is None or self._proc.stderr is None:
            return ""
        try:
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
        stderr_drain = self._stderr_drain
        self._proc = None
        self._primed = False
        self._stderr_drain = None
        if proc is None:
            if stderr_drain is not None:
                stderr_drain.join(timeout=1)
            return
        try:
            _kill_process_tree(proc)
            proc.wait(timeout=5)
        except (subprocess.TimeoutExpired, ProcessLookupError, OSError):
            try:
                proc.kill()
                proc.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                pass
        finally:
            if stderr_drain is not None:
                stderr_drain.join(timeout=2)
                if stderr_drain.is_alive:
                    log.debug("Python worker stderr drain still exiting after process termination")


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
            return None, _worker_error(
                "VENV_NOT_FOUND",
                "Embeddings require a configured Python venv (Settings → Python). "
                "LibreOffice embedded Python cannot run sentence-transformers or langgraph.",
            )
        exe = resolve_venv_python(venv_dir)
        if not exe:
            return None, _worker_error(
                "VENV_NOT_FOUND",
                f"Embeddings venv not configured or invalid: {venv_dir!r}",
            )
        log.debug("run_venv_code: using embeddings venv interpreter under %s", venv_dir)
        return exe, None

    if venv_dir:
        exe = resolve_venv_python(venv_dir)
        if not exe:
            return None, _worker_error(
                "VENV_NOT_FOUND",
                f"No python executable found under configured venv: {venv_dir!r}",
            )
        log.debug("run_venv_code: using venv interpreter under %s", venv_dir)
        return exe, None
    exe = resolve_libreoffice_python()
    if not exe:
        return None, _worker_error(
            "VENV_NOT_FOUND",
            "Could not resolve a Python interpreter (sys.executable missing, not a file, or not executable). "
            "Set scripting.python_venv_path in Settings → Python for a dedicated venv, or fix the LibreOffice install.",
        )

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
    code: str | None = None,
    *,
    data: Any = None,
    bindings: dict[str, Any] | None = None,
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
    action: str | None = None,
) -> Dict[str, Any]:
    """Execute *code* or handle *action* via :class:`PythonWorkerManager` (warm process).

    Without *session_id*, each call uses an isolated namespace in the child. With
    *session_id*, the child reuses one namespace per workbook (shared kernel).

    *worker_pool* selects which warm child to use (e.g. embeddings vs Calc/chat default).

    *active_domain* is unused (chat specialized domain). *python_tool_domain*
    scopes venv→LO tool RPC: ``None`` = all tools, ``""`` = disabled (``=PY()``),
    a domain name = that domain's proxies. See ``plugin.scripting.host_rpc``.
    """
    del active_domain  # chat specialized domain is not the tool-RPC allowlist
    if not action and not (code or "").strip():
        return _worker_error("WORKER_IPC_ERROR", "No code provided.")

    manager, err = _worker_manager_for_ctx(uno_ctx, pool=worker_pool)
    if err is not None:
        return err
    assert manager is not None

    configured = configured_python_exec_timeout(uno_ctx)
    timeout_sec = resolve_python_exec_timeout(timeout_sec, configured=configured)

    return manager.execute(
        code,
        data=data,
        bindings=bindings,
        timeout_sec=timeout_sec,
        session_id=session_id,
        init_script=init_script,
        init_session_id=init_session_id,
        init_script_hash=init_script_hash,
        allow_heartbeat=allow_heartbeat,
        heartbeat_grace_sec=heartbeat_grace_sec,
        on_heartbeat=on_heartbeat,
        action=action,
        python_tool_domain=python_tool_domain,
    )


def reset_python_session(uno_ctx: Any, session_id: str, *, timeout_sec: int | None = None) -> Dict[str, Any]:
    """Drop the shared-kernel executor for *session_id* in the warm worker."""
    if not (session_id or "").strip():
        return _worker_error("WORKER_IPC_ERROR", "No session_id provided.")

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
    child_env["WRITERAGENT_IS_WORKER"] = "1"

    manager = PythonWorkerManager.get(exe, child_env, pool=pool)
    manager.warm()

    # Pre-load the active embedding model inside the embeddings pool worker so first query executes instantly
    if pool == WORKER_POOL_EMBEDDINGS:
        try:
            from plugin.framework.client.embedding_client import get_embedding_model

            model = get_embedding_model()
            if model:
                res = manager.execute(
                    action="run_trusted_action",
                    data={
                        "domain": "embeddings_index",
                        "helper": "warm_embedder",
                        "params": {"model": model},
                    },
                )
                if res.get("status") != "ok":
                    log.warning("Embedding model pre-warm returned status %s: %s", res.get("status"), res.get("message"))
        except Exception:
            log.exception("Failed to warm embedding model")


__all__ = [
    "PythonWorkerManager",
    "reset_python_session",
    "resolve_libreoffice_python",
    "resolve_venv_python",
    "run_code_in_user_venv",
    "scrub_subprocess_env",
    "warm_venv_worker",
    "wrap_command_for_sandbox",
]
