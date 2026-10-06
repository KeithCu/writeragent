# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""Host-side venv worker: warm subprocess IPC and run_code_in_user_venv."""

from __future__ import annotations

import contextlib
import logging
import os
import select
import signal
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Dict, IO, Iterator

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
    EXEC_STARTED,
    IpcFrameError,
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

# Non-UI callers wait this long for the pipe, then get WORKER_REENTRY.
# Matches the scripting timeout ceiling so a second background script can
# still run after a long one. The UI thread never waits. A waiter also
# leaves within _IO_LOCK_POLL_SEC if the holder enters a tool RPC.
_IO_LOCK_ACQUIRE_TIMEOUT_SEC = 600.0
_IO_LOCK_POLL_SEC = 0.05
_STDERR_FALLBACK_READ_SEC = 0.2

_WORKER_REENTRY_MESSAGE = (
    "This Python tool called back into the same worker and would deadlock the script pipe."
)
_WORKER_BUSY_MESSAGE = (
    "Python worker is busy; waiting on its pipe would block this thread."
)


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


class _StopRequested(Exception):
    """The user pressed Stop while the host waited on the worker pipe."""


class _NoTerminalFrame(Exception):
    """The request was written and the child died before a terminal frame.

    Not an OSError/RuntimeError: those still retry a failed initial write.
    This one must not, or the same request id runs again.
    """


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
    cancellation_scope: Any | None = None,
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
        cancellation_scope=cancellation_scope,
    )


def host_script_session_id(request_session_id: Any, script_session_id: str | None) -> str | None:
    """Document id for host tool RPC.

    An explicit pin (chat ``ctx.doc``) wins. Otherwise the worker namespace
    id on the request is used (Run Python Script, ``=PY()``, PPT-Master).
    The child does not choose this value, and a pin is not written into the
    request, so Isolated mode still gets a fresh namespace.
    """
    explicit = script_session_id.strip() if isinstance(script_session_id, str) else ""
    if explicit:
        return explicit
    if isinstance(request_session_id, str):
        raw = request_session_id.strip()
        if raw:
            return raw
    return None


def _maybe_dispatch_intermediate_response(
    response: dict[str, Any],
    *,
    stdin_write: Callable[[bytes], None],
    allowed_tools: frozenset[str] | None = None,
    caller: str = "script",
    on_worker_event: Callable[[dict[str, Any]], None] | None = None,
    stop_checker: Callable[[], bool] | None = None,
    cancellation_scope: Any | None = None,
    script_session_id: str | None = None,
) -> bool:
    """Handle tool_call (any build). ppt-master llm_request / worker_event only for that caller."""
    from plugin.scripting.host_rpc import handle_tool_call_frame

    # Tool RPC is the shared venv→LO path (Run Python Script, chat python, ppt-master).
    # Handle it here so LibrePy / WriterAgent-without-ppt_master still round-trip.
    if handle_tool_call_frame(
        response,
        stdin_write=stdin_write,
        allowed_tools=allowed_tools,
        caller=caller,
        script_session_id=script_session_id,
        stop_checker=stop_checker,
    ):
        return True
    # Bugfix: every caller used to fall through into the ppt-master dispatcher,
    # and that dispatcher runs llm_request with the host's API credentials.
    # A non-PPT worker (caller "script", including =PY()) could emit
    # llm_request and make the host perform that call. Only the ppt-master
    # worker is allowed to ask for it. tool_call above stays open to every caller.
    if caller != "ppt_master_venv":
        return False
    return _maybe_dispatch_ppt_master_response(
        response,
        stdin_write=stdin_write,
        on_worker_event=on_worker_event,
        stop_checker=stop_checker,
        cancellation_scope=cancellation_scope,
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
    if sys.platform == "win32":
        # Bugfix: returning when poll() is not None skipped taskkill /T, so
        # grandchildren of an already-exited worker were left running.
        _kill_process_tree_win32(proc)
        return
    # Bugfix: the same early return skipped the process group on POSIX.
    # The worker is a session leader (start_new_session, so pgid == pid).
    # If it has already exited, poll() has reaped it and getpgid(pid) raises
    # ProcessLookupError, but grandchildren can still be in that group.
    # killpg(pid) reaches them. ProcessLookupError means the group is gone.
    pid = proc.pid
    if not pid:
        if proc.poll() is None:
            proc.kill()
        return
    try:
        pgid = os.getpgid(pid)
        fallback = False
    except ProcessLookupError:
        pgid = pid
        fallback = True

    try:
        if fallback and proc.poll() is None:
            proc.kill()
        elif pgid == os.getpgrp():
            # Bugfix: killpg on the host's own group (a reused pid, or a child
            # without its own session) would kill LibreOffice. Kill only proc.
            if proc.poll() is None:
                proc.kill()
        else:
            os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        if proc.poll() is None:
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


class _IoSessionUnavailable(Exception):
    """``_io_session`` could not start; ``args[0]`` is the error dict to return."""


@dataclass
class _TurnState:
    """Replay guards for one request; survives an exception out of the read loop."""

    execution_started: bool = False
    dispatched_intermediate: bool = False

    @property
    def may_have_run(self) -> bool:
        return self.execution_started or self.dispatched_intermediate


class PythonWorkerManager:
    """One warm child process per (pool, Python executable path) pair."""

    exe: str
    env: dict[str, str]
    _io_lock: threading.Lock
    _primed: bool
    _retired: bool
    _proc_lock: threading.Lock

    def __init__(self, exe: str, env: dict[str, str]) -> None:
        self.exe = exe
        self.env = dict(env)
        self.env["WRITERAGENT_IS_WORKER"] = "1"
        self._proc: subprocess.Popen[Any] | None = None
        self._io_lock = threading.Lock()
        # Owner of _io_lock. A script tool that calls this pool again on the
        # same thread, or any thread while a tool RPC is in progress, deadlocks
        # the pipe: the holder is waiting for that thread. RLock would
        # interleave frames, so we refuse. The UI thread also refuses when the
        # lock is already held, instead of blocking the UI.
        self._io_owner: int | None = None
        self._serving_tool_call: bool = False
        self._primed = False
        self._retired = False
        self._proc_lock = threading.Lock()
        self._stderr_drain: StderrTail | None = None
        self._stdin_writer_thread: threading.Thread | None = None

    @contextlib.contextmanager
    def _io_session(self) -> Iterator[None]:
        """Hold the IO lock with a warm worker; raise ``_IoSessionUnavailable`` otherwise."""
        reentry = self._acquire_io()
        if reentry is not None:
            raise _IoSessionUnavailable(reentry)
        try:
            warm_err = self._ensure_warmed_unlocked()
            if warm_err is not None:
                raise _IoSessionUnavailable(warm_err)
            yield
        finally:
            self._release_io()

    @classmethod
    def get(cls, exe: str, env: dict[str, str], *, pool: str = WORKER_POOL_DEFAULT) -> PythonWorkerManager:
        """Return the singleton worker for *pool* + *exe* (caller should pass a scrubbed env dict)."""
        key = _worker_registry_key(exe, pool)
        prefix = f"{pool}:"
        stale: list[PythonWorkerManager] = []
        with _registry_lock:
            # Bugfix: the registry key is pool:exe. A new Settings path used to
            # leave the previous child running until LibreOffice exited.
            for other_key in list(_instances):
                if other_key.startswith(prefix) and other_key != key:
                    old = _instances.pop(other_key, None)
                    if old is not None:
                        stale.append(old)
            mgr = _instances.get(key)
            if mgr is None:
                mgr = cls(exe, dict(env))
                _instances[key] = mgr
            else:
                # The live process keeps the env it was spawned with. The next
                # Popen (crash, or a path change) must see a later scrub, including
                # WRITERAGENT_DEBUG_LOG_PATH once logging is up.
                fresh = dict(env)
                fresh["WRITERAGENT_IS_WORKER"] = "1"
                mgr.env = fresh
            for old in stale:
                # In-flight execute holds _io_lock. Do not take it here: a tool
                # call is waiting on the UI thread. The flag stops the retry
                # from Popen-ing a child this registry no longer owns.
                old._retired = True
        for old in stale:
            old._terminate_worker()
        return mgr

    @classmethod
    def shutdown_all(cls) -> None:
        """Terminate all workers (tests / extension teardown)."""
        # Bugfix: shutdown_all used to skip _retired, and the retry spawned an
        # untracked warm process. get() already sets the flag before terminate
        # so an in-flight execute cannot Popen a child the registry dropped.
        # Drop the lock before terminate: an in-flight tool call may need the
        # UI thread, and terminate must not hold _registry_lock across that.
        with _registry_lock:
            managers = list(_instances.values())
            for mgr in managers:
                mgr._retired = True
            _instances.clear()
        for mgr in managers:
            mgr._terminate_worker()

    @classmethod
    def pool_is_running(cls, pool: str = WORKER_POOL_DEFAULT) -> bool:
        """True when *pool* already has a live child. Does not spawn one."""
        prefix = f"{pool}:"
        with _registry_lock:
            for key, mgr in _instances.items():
                if key.startswith(prefix) and mgr._is_worker_alive():
                    return True
        return False

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

    def _reentry_error(self) -> dict[str, Any] | None:
        """Refuse a nested execute that would deadlock this worker's pipe."""
        from plugin.framework.thread_guard import on_main_thread

        me = threading.get_ident()
        # Bugfix: this used to refuse only the owner, or the UI thread while
        # _serving_tool_call was already set. Every other thread then called
        # Lock.acquire() with no timeout. The holder can be inside a tool RPC
        # that is waiting on the thread stuck in acquire (the UI pump, or
        # whichever worker must run the host callback), so the wait never
        # ends and the UI stays frozen.
        if self._io_owner == me or self._serving_tool_call:
            return _worker_error("WORKER_REENTRY", _WORKER_REENTRY_MESSAGE)
        if on_main_thread() and self._io_lock.locked():
            return _worker_error("WORKER_REENTRY", _WORKER_BUSY_MESSAGE)
        return None

    def _acquire_io(self) -> dict[str, Any] | None:
        err = self._reentry_error()
        if err is not None:
            return err
        from plugin.framework.thread_guard import on_main_thread

        # The locked() check above can pass, then another thread takes the
        # pipe and blocks on the UI thread. A non-blocking acquire closes
        # that race. Other threads wait, but leave if a tool RPC starts.
        if on_main_thread():
            if not self._io_lock.acquire(timeout=0):
                return _worker_error("WORKER_REENTRY", _WORKER_BUSY_MESSAGE)
            self._io_owner = threading.get_ident()
            return None

        deadline = time.monotonic() + _IO_LOCK_ACQUIRE_TIMEOUT_SEC
        while True:
            if self._serving_tool_call:
                return _worker_error("WORKER_REENTRY", _WORKER_REENTRY_MESSAGE)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return _worker_error("WORKER_REENTRY", _WORKER_BUSY_MESSAGE)
            if self._io_lock.acquire(timeout=min(_IO_LOCK_POLL_SEC, remaining)):
                self._io_owner = threading.get_ident()
                return None

    def _release_io(self) -> None:
        self._serving_tool_call = False
        self._io_owner = None
        self._io_lock.release()

    def _ensure_warmed(self) -> dict[str, Any] | None:
        err = self._acquire_io()
        if err is not None:
            return err
        try:
            return self._ensure_warmed_unlocked()
        finally:
            self._release_io()

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
        if session_id:
            request["session_id"] = session_id
        if data is not None:
            request["data"] = data
        # Bugfix: allow_heartbeat used to be set only on the code branch.
        # run_trusted_action (embeddings, folder index) never received it, so
        # the child sent no heartbeat and the host killed a long job.
        if allow_heartbeat:
            request["allow_heartbeat"] = True
        if action:
            request["action"] = action
        else:
            request["code"] = code if code is not None else ""
            if bindings:
                request["bindings"] = bindings
            if init_script:
                request["init_script"] = init_script
            if init_session_id:
                request["init_session_id"] = init_session_id
            if init_script_hash:
                request["init_script_hash"] = init_script_hash
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
        cancellation_scope: Any | None = None,
        python_tool_domain: str | None = None,
        caller: str = "script",
        script_session_id: str | None = None,
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
        return self._execute_ipc_attempts(
            request,
            timeout_sec=timeout_sec,
            allow_heartbeat=allow_heartbeat,
            heartbeat_grace_sec=heartbeat_grace_sec,
            on_heartbeat=on_heartbeat,
            on_worker_event=on_worker_event,
            stop_checker=stop_checker,
            cancellation_scope=cancellation_scope,
            python_tool_domain=python_tool_domain,
            caller=caller,
            script_session_id=script_session_id,
        )

    def _fail_no_replay(self, code: str, message: str, *, details: dict[str, Any] | None = None) -> dict[str, Any]:
        """Kill the desynced worker and return an error without resending the request."""
        self._terminate_worker()
        _clear_host_state_after_worker_death()
        return _worker_error(code, message, details=details if details is not None else {"exe": self.exe})

    def _read_until_terminal(
        self,
        stdout: IO[bytes],
        stdin: IO[bytes],
        request: dict[str, Any],
        state: _TurnState,
        *,
        host_read_timeout_sec: float,
        write_timeout_sec: float,
        allow_heartbeat: bool,
        heartbeat_grace_sec: int | None,
        on_heartbeat: Callable[[dict[str, Any]], None] | None,
        on_worker_event: Callable[[dict[str, Any]], None] | None,
        stop_checker: Callable[[], bool] | None,
        cancellation_scope: Any | None,
        allowed_tools: frozenset[str] | None,
        caller: str,
        resolved_script_session_id: str | None,
    ) -> dict[str, Any]:
        """Serve intermediate frames until a terminal one; flags go into *state*.

        *state* is the caller's object so its except handlers still see
        exec_started / tool_call when this raises.
        """

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

        while True:
            if allow_heartbeat:
                from plugin.framework.constants import EMBEDDINGS_HEARTBEAT_GRACE_S

                grace = int(heartbeat_grace_sec if heartbeat_grace_sec is not None else EMBEDDINGS_HEARTBEAT_GRACE_S)
                response_bytes = self._read_response_with_heartbeats(
                    stdout,
                    host_read_timeout_sec,
                    grace,
                    on_heartbeat,
                    stop_checker=stop_checker,
                )
            else:
                response_bytes = self._read_response_bytes(stdout, host_read_timeout_sec, stop_checker=stop_checker)
            if not response_bytes:
                stderr_out = self._drain_stderr()
                message = f"Worker closed stdout without a response{stderr_out}"
                if state.may_have_run:
                    # Work may already have run. Resending this id would
                    # run it again. A close before exec_started is a
                    # failed start and falls through to the retry.
                    raise _NoTerminalFrame(message)
                raise RuntimeError(message)
            response = unpack_pickle_frame(response_bytes)
            if not isinstance(response, dict):
                raise RuntimeError("Worker response must be a dict")
            if response.get("type") == EXEC_STARTED:
                state.execution_started = True
                if response.get("id") != request.get("id"):
                    # Hand the mismatched frame back; the caller's id check
                    # refuses it without replaying the request.
                    return response
                continue

            self._serving_tool_call = True
            try:
                is_intermediate = _maybe_dispatch_intermediate_response(
                    response,
                    stdin_write=_stdin_write,
                    allowed_tools=allowed_tools,
                    caller=caller,
                    on_worker_event=on_worker_event,
                    stop_checker=stop_checker,
                    cancellation_scope=cancellation_scope,
                    script_session_id=resolved_script_session_id,
                )
            finally:
                self._serving_tool_call = False
            if is_intermediate:
                state.dispatched_intermediate = True
                continue
            return response

    def _execute_ipc_attempts(
        self,
        request: dict[str, Any],
        *,
        timeout_sec: int,
        allow_heartbeat: bool,
        heartbeat_grace_sec: int | None,
        on_heartbeat: Callable[[dict[str, Any]], None] | None,
        on_worker_event: Callable[[dict[str, Any]], None] | None,
        stop_checker: Callable[[], bool] | None,
        cancellation_scope: Any | None,
        python_tool_domain: str | None,
        caller: str,
        script_session_id: str | None = None,
    ) -> dict[str, Any]:
        for attempt in range(2):
            try:
                self._ensure_running()
                proc = self._proc
                # shutdown_all can terminate without _io_lock; an assert here
                # escaped execute() as AttributeError (or vanished under -O).
                if proc is None or proc.stdin is None or proc.stdout is None:
                    raise RuntimeError("worker terminated concurrently")
                stdin = proc.stdin
                stdout = proc.stdout
                write_timeout_sec = min(float(timeout_sec), float(VENV_IPC_WRITE_TIMEOUT_SEC))
                try:
                    self._write_frame_with_timeout(stdin, request, timeout_sec=write_timeout_sec, label="request")
                except IpcFrameError as e:
                    # Bugfix: an oversize request failed in pack_pickle_frame, before
                    # any byte reached the pipe, yet hit the retry handler, which
                    # killed the warm worker twice and wiped every shared session.
                    log.warning("Python worker request not sent: %s", e)
                    return _worker_error(
                        "WORKER_IPC_ERROR",
                        f"Failed to serialize request: {e}",
                        details={"exe": self.exe},
                    )

                # The host read timeout includes a grace buffer so the child's in-process
                # signal/thread timeout fires first and returns a clean error frame without
                # terminating the warm subprocess (preserving shared workbook sessions).
                host_read_timeout_sec = float(timeout_sec) + float(HOST_IPC_READ_GRACE_SEC)
                from plugin.scripting.host_rpc import resolve_allowed_tools

                allowed_tools = resolve_allowed_tools(python_tool_domain)
                # Pin wins over the kernel session id. Chat passes doc:… and
                # leaves request session_id unset. RPS / =PY() / PPT-Master
                # omit the pin and keep the request id.
                resolved_script_session_id = host_script_session_id(request.get("session_id"), script_session_id)

                # A tool_call frame can already have mutated the document. A later
                # pipe error must not resend the original script (the write-timeout
                # path below is the same rule). exec_started is the same rule for
                # side effects that never sent a tool_call frame. A death before
                # that marker has not run the request, so the outer loop may retry.
                state = _TurnState()
                try:
                    response = self._read_until_terminal(
                        stdout,
                        stdin,
                        request,
                        state,
                        host_read_timeout_sec=host_read_timeout_sec,
                        write_timeout_sec=write_timeout_sec,
                        allow_heartbeat=allow_heartbeat,
                        heartbeat_grace_sec=heartbeat_grace_sec,
                        on_heartbeat=on_heartbeat,
                        on_worker_event=on_worker_event,
                        stop_checker=stop_checker,
                        cancellation_scope=cancellation_scope,
                        allowed_tools=allowed_tools,
                        caller=caller,
                        resolved_script_session_id=resolved_script_session_id,
                    )
                except subprocess.TimeoutExpired as e:
                    # User code / C-extension hung: killing and replaying would double the wait.
                    log.warning("Python worker read timed out: %s", e)
                    return self._fail_no_replay(
                        "VENV_TIMEOUT",
                        _worker_error_message(e) + _SHARED_WORKER_RESTART_HINT,
                        details={"timeout_sec": timeout_sec, "exe": self.exe},
                    )
                except ValueError as e:
                    # Bugfix: a bad pickle used to kill the child and resend the
                    # same request. Side effects that already ran (DuckDB writes,
                    # a trusted update) ran twice. Id mismatch already refuses
                    # that replay; unpickle does too.
                    log.warning("Python worker frame rejected (not replaying): %s", e)
                    return self._fail_no_replay("WORKER_IPC_ERROR", f"Python worker failed: {e}{_SHARED_WORKER_RESTART_HINT}")
                except (OSError, RuntimeError) as e:
                    # Bugfix: RuntimeError used to look only at dispatched_intermediate,
                    # so a later RuntimeError (bad frame, closed pipe) after
                    # exec_started re-raised into the attempt loop and ran the
                    # same script on a new child.
                    if not state.may_have_run:
                        raise
                    log.warning("Python worker failed after execution started (not replaying): %s", e)
                    return self._fail_no_replay("WORKER_IPC_ERROR", f"Python worker failed: {e}{_SHARED_WORKER_RESTART_HINT}")
                req_id = request.get("id")
                if response.get("id") != req_id:
                    # exchange_tool_call already checks ids. A mismatched terminal
                    # frame used to be accepted, so one cell could receive another's result.
                    log.warning(
                        "Python worker response id %r does not match request %r",
                        response.get("id"),
                        req_id,
                    )
                    return self._fail_no_replay(
                        "WORKER_IPC_ERROR",
                        f"Python worker response id mismatch.{_SHARED_WORKER_RESTART_HINT}",
                    )
                try:
                    # host_unpack_data runs after the script has finished. A bad
                    # envelope used to hit the outer handler and replay the
                    # request, so a tool call that already mutated the document
                    # ran twice.
                    return self._normalize_response(response)
                except ValueError as e:
                    log.warning("Python worker result rejected (not replaying): %s", e)
                    return self._fail_no_replay("WORKER_IPC_ERROR", f"Python worker failed: {e}{_SHARED_WORKER_RESTART_HINT}")
            except _StopRequested:
                # Stop while waiting on the pipe: the child may be mid-script, so
                # it is killed (never replayed) and the caller sees CANCELLED.
                log.info("Python worker stopped by user")
                return self._fail_no_replay("CANCELLED", "Python worker stopped by user")
            except (_NoTerminalFrame, _NonReplayableIpcWriteTimeout) as e:
                log.warning("Python worker failed without replay: %s", e)
                return self._fail_no_replay("WORKER_IPC_ERROR", f"Python worker failed: {e}{_SHARED_WORKER_RESTART_HINT}")
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
        script_session_id: str | None = None,
        stop_checker: Callable[[], bool] | None = None,
        cancellation_scope: Any | None = None,
    ) -> dict[str, Any]:
        """Run *code* in the warm worker, or handle *action* (e.g. reset_session).

        Without *session_id*, each execute uses a fresh namespace in the child. With
        *session_id*, the child reuses one LocalPythonExecutor per id.

        *script_session_id* is host-only. Tool RPC resolves the document from it
        ahead of *session_id*. It is not sent to the child.

        Cold start: spawn + auto-imports run first under :data:`WARM_WORKER_TIMEOUT_SEC`
        and are not charged against *timeout_sec*.
        """
        if timeout_sec is None:
            timeout_sec = python_exec_timeout_default()

        try:
            with self._io_session():
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
                    script_session_id=script_session_id,
                    stop_checker=stop_checker,
                    cancellation_scope=cancellation_scope,
                )
        except _IoSessionUnavailable as e:
            return e.args[0]

    def execute_ppt_master_turn(
        self,
        payload: dict[str, Any],
        *,
        timeout_sec: int,
        on_worker_event: Callable[[dict[str, Any]], None] | None = None,
        stop_checker: Callable[[], bool] | None = None,
        cancellation_scope: Any | None = None,
    ) -> dict[str, Any]:
        """Run one PPT-Master sidebar turn in the venv worker (LLM + scripts + host UNO RPC).

        ``cancellation_scope`` is host-side only. ``data`` is pickled to the child,
        so the scope is passed beside it and attached when an llm_request frame
        comes back, the same way as ``stop_checker``.
        """
        try:
            import plugin.ppt_master  # noqa: F401  # pyright: ignore[reportUnusedImport]
        except ImportError:
            return _worker_error(
                "WORKER_IPC_ERROR",
                "PPT-Master is not available in this extension build.",
            )
        # The child reads session_id from payload (skill cache). The request
        # field is host-only: tool frames resolve the frame document from it.
        # ppt_master_turn does not use it as a Python namespace.
        raw_session = payload.get("session_id")
        session_id = raw_session.strip() if isinstance(raw_session, str) else ""
        try:
            with self._io_session():
                raw = self._execute_ipc_unlocked(
                    None,
                    data=payload,
                    timeout_sec=timeout_sec,
                    action="ppt_master_turn",
                    session_id=session_id or None,
                    on_worker_event=on_worker_event,
                    stop_checker=stop_checker,
                    cancellation_scope=cancellation_scope,
                    caller="ppt_master_venv",
                )
        except _IoSessionUnavailable as e:
            return e.args[0]
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
                # Bugfix: bufsize=0 makes stdin a raw FileIO. write() can return a short count.
                # Ignoring short writes corrupts the length-prefixed frame protocol.
                view = memoryview(payload)
                written = 0
                while written < len(view):
                    n = stdin.write(view[written:])
                    if not n:
                        raise OSError("zero bytes written to pipe")
                    written += n
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
        if not writer.is_alive():
            # Join finished. A completed writer thread keeps its stack until
            # the next write replaces this reference.
            self._stdin_writer_thread = None
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
        if self._retired:
            raise RuntimeError(
                "Python worker was replaced by a new venv path and will not be restarted"
            )
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
            # Bugfix: preexec_fn=os.setsid runs in the child between fork and
            # exec. In a threaded host only the forking thread is cloned, so
            # a lock held by another thread (malloc, logging) deadlocks the
            # child before exec. start_new_session asks the C spawn path to
            # call setsid, which is the same new process group without that
            # Python callback. pgid stays equal to pid for _kill_process_tree.
            popen_kw["start_new_session"] = True
        # The top-of-function check can pass, then shutdown_all (or get())
        # sets _retired, and this call would still Popen. Re-check immediately
        # before spawn so that retry cannot start an untracked child.
        with self._proc_lock:
            if self._retired:
                raise RuntimeError(
                    "Python worker was replaced by a new venv path and will not be restarted"
                )
            self._proc = subprocess.Popen(wrap_command_for_sandbox([self.exe, _HARNESS_PATH]), **popen_kw)
            optimize_popen_pipes(self._proc)
            # Live stderr drain: prevent 64KB pipe deadlock while parent blocks on stdin/stdout.
            self._stderr_drain = start_stderr_drain(
                self._proc.stderr,
                name=f"venv-stderr-{self._proc.pid}",
            )
            log.debug("Started Python worker pid=%s exe=%s", self._proc.pid, self.exe)

    def _read_response_bytes(self, stdout: IO[bytes], timeout_sec: float | int, stop_checker: Callable[[], bool] | None = None) -> bytes:
        if self._proc is None:
            raise RuntimeError("worker terminated concurrently")
        # Do not merge this with ipc.read_pickle_frame_with_timeout: the worker
        # path also poll()-short-circuits a dead child and (on the heartbeat
        # path) resets the deadline. Unifying those is a hang-regression risk
        # for =PY(). Windows select.select() only supports sockets, not pipes
        # (WinError 10038); PeekNamedPipe there instead of a ReadFile thread.
        if sys.platform == "win32":
            return self._read_response_bytes_threaded(stdout, timeout_sec, stop_checker=stop_checker)
        return self._read_response_bytes_select(stdout, timeout_sec, stop_checker=stop_checker)

    def _read_response_bytes_select(self, stdout: IO[bytes], timeout_sec: float | int, stop_checker: Callable[[], bool] | None = None) -> bytes:
        """POSIX path: use select() to poll the pipe with a timeout."""
        # monotonic: a wall-clock step used to stretch the wait or kill the warm worker.
        deadline = time.monotonic() + timeout_sec

        def _read_exact(n: int) -> bytes:
            return self._read_exact_before_deadline(stdout, n, deadline, stop_checker, timeout_label=timeout_sec)

        return self._read_frame_bytes(stdout, _read_exact)

    def _read_response_bytes_threaded(self, stdout: IO[bytes], timeout_sec: float | int, stop_checker: Callable[[], bool] | None = None) -> bytes:
        """Windows path: PeekNamedPipe, not a daemon thread blocked in ReadFile.

        Closing the pipe while a thread was inside ReadFile crashed the xdist
        worker (CI 33453184665). Real pipe fds use ipc's peek helper. Streams
        without a pipe fd (BytesIO / tests) still use a join-timeout thread;
        that read is not a Windows pipe ReadFile.
        """
        deadline = time.monotonic() + float(timeout_sec)

        def _read_exact(n: int) -> bytes:
            if stop_checker and stop_checker():
                raise _StopRequested()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(cmd=self.exe, timeout=timeout_sec)
            return self._read_exact_win32(stdout, n, remaining, timeout_sec, stop_checker)

        return (
            read_frame_payload(
                stdout,
                read_exact=_read_exact,
                max_payload_bytes=DEFAULT_MAX_PAYLOAD_BYTES,
                frame_label="venv worker frame",
            )
            or b""
        )

    def _read_exact_win32(self, stdout: IO[bytes], nbytes: int, remaining: float, timeout_sec: float | int, stop_checker: Callable[[], bool] | None = None) -> bytes:
        """One exact read on Windows: peek a real pipe, else a join-timeout thread."""
        if sys.platform == "win32":
            try:
                fd = stdout.fileno()
            except (AttributeError, OSError, ValueError):
                fd = None
            if isinstance(fd, int) and fd >= 0:
                from plugin.scripting.ipc import _read_bytes_with_timeout_win32

                try:
                    return _read_bytes_with_timeout_win32(stdout, nbytes, remaining, cmd=self.exe, stop_checker=stop_checker)
                except subprocess.TimeoutExpired:
                    # ipc reports Stop as a timeout; the caller needs CANCELLED.
                    if stop_checker and stop_checker():
                        raise _StopRequested() from None
                    raise

        result: list[bytes] = [b""]
        error: list[BaseException | None] = [None]

        def _reader() -> None:
            try:
                result[0] = stdout.read(nbytes)
            except Exception as exc:
                error[0] = exc

        t = threading.Thread(target=_reader, daemon=True)
        t.start()
        t.join(timeout=max(0.0, remaining))
        if t.is_alive():
            raise subprocess.TimeoutExpired(cmd=self.exe, timeout=timeout_sec)
        if error[0] is not None:
            raise error[0]
        return result[0] or b""

    def _read_exact_before_deadline(self, stdout: IO[bytes], nbytes: int, deadline: float, stop_checker: Callable[[], bool] | None = None, timeout_label: float | int | None = None) -> bytes:
        remaining = deadline - time.monotonic()
        # Bugfix: the label was computed from the time left, so an expired wait
        # reported "timed out after 1 seconds" whatever the real window was.
        label = timeout_label if timeout_label is not None else max(1, int(remaining))

        if sys.platform == "win32":
            if remaining <= 0:
                raise subprocess.TimeoutExpired(cmd=self.exe, timeout=label)
            return self._read_exact_win32(stdout, nbytes, remaining, label, stop_checker)

        buf = bytearray()
        while len(buf) < nbytes:
            if stop_checker and stop_checker():
                raise _StopRequested()
            if time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(cmd=self.exe, timeout=label)
            remaining = deadline - time.monotonic()
            ready, _unused, _unused2 = select.select([stdout], [], [], min(0.2, remaining) if stop_checker else min(1.0, remaining))
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
        stop_checker: Callable[[], bool] | None = None,
    ) -> bytes:
        from plugin.scripting.venv.worker_heartbeat import FRAME_HEARTBEAT, FRAME_RESULT, parse_frame

        # Each heartbeat pushes the deadline out by grace_sec; window is the
        # current wait, used as the timeout label.
        window = float(max(timeout_sec, grace_sec))
        deadline_holder = [time.monotonic() + window, window]

        def _read_exact(n: int) -> bytes:
            if stop_checker and stop_checker():
                raise _StopRequested()
            return self._read_exact_before_deadline(stdout, n, deadline_holder[0], stop_checker, timeout_label=deadline_holder[1])

        while True:
            frame_bytes = self._read_frame_bytes(stdout, _read_exact)
            if not frame_bytes:
                return b""
            data = parse_frame(frame_bytes)
            frame_type = data.get("frame_type")
            if frame_type == FRAME_HEARTBEAT:
                payload = data.get("payload")
                if on_heartbeat is not None and isinstance(payload, dict):
                    try:
                        on_heartbeat(payload)
                    except Exception:
                        log.exception("Heartbeat callback failed (ignoring)")
                deadline_holder[0] = time.monotonic() + grace_sec
                deadline_holder[1] = float(grace_sec)
                continue
            if frame_type == FRAME_RESULT or frame_type is None:
                return frame_bytes
            if data.get("status") in ("ok", "error"):
                return frame_bytes
            log.debug("venv worker ignoring unknown frame type: %r", frame_type)

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
            text = drain.finish_text().strip()
            return f"\nWorker stderr:\n{text}" if text else ""
        # Fallback if spawn raced before the drain was attached.
        proc = self._proc
        if proc is None or proc.stderr is None:
            return ""
        try:
            proc.wait(timeout=2)
        except Exception:
            pass
        stderr_bytes = self._read_stderr_fallback(proc.stderr)
        if not stderr_bytes:
            return ""
        text = stderr_bytes.decode("utf-8", errors="replace").strip()
        return f"\nWorker stderr:\n{text}"

    def _read_stderr_fallback(self, stderr: IO[bytes]) -> bytes:
        """Stderr bytes already queued. Does not wait for EOF.

        What was wrong: ``stderr.read()`` blocks until EOF. This path runs
        when the drain thread was not attached. A grandchild still holding
        the write end means EOF never comes, so the host hung before it
        could kill the tree. POSIX ``select`` plus one short read, and a
        Windows peek of the queued count, return only what is already there.
        No reader thread: a join-timeout ``ReadFile`` thread leaked on timeout.
        """
        if sys.platform == "win32":
            return self._read_stderr_fallback_win32(stderr)
        try:
            ready, _unused, _unused2 = select.select([stderr], [], [], _STDERR_FALLBACK_READ_SEC)
        except (OSError, TypeError, ValueError):
            return b""
        if not ready:
            return b""
        try:
            chunk = stderr.read(65536)
        except OSError:
            return b""
        return chunk or b""

    def _read_stderr_fallback_win32(self, stderr: IO[bytes]) -> bytes:
        try:
            fd = stderr.fileno()
        except (AttributeError, OSError, ValueError):
            return b""
        if not isinstance(fd, int) or fd < 0:
            return b""
        from plugin.scripting.ipc import _peek_pipe_bytes_available

        try:
            avail = _peek_pipe_bytes_available(fd)
        except OSError:
            return b""
        if not avail:
            return b""
        try:
            chunk = stderr.read(min(int(avail), 65536))
        except OSError:
            return b""
        return chunk or b""

    def _terminate_worker(self) -> None:
        with self._proc_lock:
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
    script_session_id: str | None = None,
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
    stop_checker: Callable[[], bool] | None = None,
    cancellation_scope: Any | None = None,
) -> Dict[str, Any]:
    """Execute *code* or handle *action* via :class:`PythonWorkerManager` (warm process).

    Without *session_id*, each call uses an isolated namespace in the child. With
    *session_id*, the child reuses one namespace per workbook (shared kernel).

    *worker_pool* selects which warm child to use (e.g. embeddings vs Calc/chat default).

    *active_domain* is unused (chat specialized domain). *python_tool_domain*
    scopes venv→LO tool RPC: ``None`` = all tools, ``""`` = disabled (``=PY()``),
    a domain name = that domain's proxies. See ``plugin.scripting.host_rpc``.

    *script_session_id* is host-only. Tool RPC resolves the document from it
    ahead of *session_id*. Chat passes a ``doc:`` pin so ``wa.*`` uses
    ``ctx.doc`` without putting that id on the child namespace.
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
        script_session_id=script_session_id,
        stop_checker=stop_checker,
        cancellation_scope=cancellation_scope,
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
    manager, err = _worker_manager_for_ctx(uno_ctx, pool=pool)
    if err is not None:
        log.warning("warm_venv_worker skipped for pool %s: %s", pool, err.get("message"))
        return
    assert manager is not None
    manager.warm()

    # Pre-load the active embedding model inside the embeddings pool worker so first query executes instantly
    if pool == WORKER_POOL_EMBEDDINGS:
        try:
            from plugin.embeddings.embedding_client import get_embedding_model
            from plugin.scripting.config_limits import embeddings_worker_timeout_sec

            model = get_embedding_model()
            if model:
                timeout_val = embeddings_worker_timeout_sec(uno_ctx)
                res = manager.execute(
                    action="run_trusted_action",
                    data={
                        "domain": "embeddings_index",
                        "helper": "warm_embedder",
                        "params": {"model": model},
                    },
                    timeout_sec=timeout_val,
                    allow_heartbeat=True,
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
