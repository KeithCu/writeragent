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
from typing import Any, Callable, IO, Iterator, NoReturn

from plugin.framework.config import get_config_str
from plugin.framework.thread_guard import background
from plugin.framework.constants import WORKER_POOL_DEFAULT, WORKER_POOL_EMBEDDINGS
from plugin.framework.worker_pool import (
    BackgroundHandle,
    StderrTail,
    get_subprocess_creationflags,
    run_in_background,
    start_stderr_drain,
)
from plugin.scripting.config_limits import (
    HOST_IPC_READ_GRACE_SEC,
    VENV_IPC_WRITE_TIMEOUT_SEC,
    WARM_WORKER_TIMEOUT_SEC,
    configured_python_exec_timeout,
    python_exec_timeout_default,
    resolve_python_exec_timeout,
)
from plugin.scripting.ipc import (
    _stop_requested,
    _write_all,
    DEFAULT_MAX_PAYLOAD_BYTES,
    EXEC_STARTED,
    IpcFrameError,
    IpcPartialFrameTimeout,
    pack_pickle_frame,
    read_frame_payload,
    unpack_pickle_frame,
)
from plugin.scripting.payload_codec import _HOST_UNPACK_ERRORS, host_unpack_data
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
# still run after a long one. The UI thread does not wait to acquire
# _io_lock. Killing a worker does not wait on the caller either: the
# process tree is reaped in the background. The next spawn joins that
# reap first. A waiter also leaves within _IO_LOCK_POLL_SEC if the
# holder enters a tool RPC.
_IO_LOCK_ACQUIRE_TIMEOUT_SEC = 600.0
_IO_LOCK_POLL_SEC = 0.05

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


class _WorkerRetired(RuntimeError):
    """This manager was replaced by a new venv path and must not be restarted."""


class _StopRequested(Exception):
    """The user pressed Stop while the host waited on the worker pipe."""


class _NoTerminalFrame(Exception):
    """The request was written and the child died before a terminal frame.

    Not an OSError/RuntimeError: those still retry a failed initial write.
    This one must not, or the same request id runs again.
    """


_SHARED_WORKER_RESTART_HINT = " Shared Python process restarted (all workbooks)."


def _clear_host_state_after_worker_death() -> None:
    """IPC is desynced after a kill; drop add-in scalar cache so the next turn is cold."""
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
    # Only the ppt-master worker may emit llm_request. Every other caller
    # (including "script" and =PY()) used to fall through into that dispatcher,
    # which runs llm_request with the host's API credentials. tool_call above
    # stays open to every caller.
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


def _kill_process_tree(proc: subprocess.Popen[Any]) -> None:
    """Kill *proc* and its descendants (POSIX process group, Windows ``taskkill /T``)."""
    if sys.platform == "win32":
        # Returning when poll() is not None skipped taskkill /T, so
        # grandchildren of an already-exited worker were left running.
        _kill_process_tree_win32(proc)
        return
    # The same early return skipped the process group on POSIX. The worker is
    # a session leader (start_new_session, so pgid == pid). If it has already
    # exited, poll() has reaped it and getpgid(pid) raises ProcessLookupError,
    # but grandchildren can still be in that group. killpg(pid) reaches them.
    # ProcessLookupError means the group is gone.
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
            # killpg on the host's own group (a reused pid, or a child without
            # its own session) would kill LibreOffice. Kill only proc.
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


def _reap_worker_process(proc: subprocess.Popen[Any] | None, stderr_drain: StderrTail | None) -> None:
    """Kill a detached child and join its stderr drain. Runs off the caller thread.

    ``_terminate_worker`` used to wait on this thread for the kill, a second
    ``wait``, and the stderr join (about 12s when the tree ignores the first
    signal). ``PythonWorkerManager.get`` did that on the caller, which is the
    UI thread when Settings changes the venv path. The caller has already
    dropped ``_proc`` under ``_proc_lock``. This thread only reaps that
    snapshot. The next spawn joins it first, so the new child does not
    overlap the old process tree.
    """
    if proc is not None:
        try:
            _kill_process_tree(proc)
            proc.wait(timeout=5)
        except (subprocess.TimeoutExpired, ProcessLookupError, OSError):
            try:
                proc.kill()
                proc.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                pass
    if stderr_drain is not None:
        stderr_drain.join(timeout=2 if proc is not None else 1)
        if stderr_drain.is_alive:
            log.debug("Python worker stderr drain still exiting after process termination")


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
    _reap_handles: list[BackgroundHandle]

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
        # In-flight background reaps. get() moves a replaced manager's handles
        # onto the replacement so the next spawn waits for the old tree.
        self._reap_handles = []

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

    def _run_in_session(self, body: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        """Run *body* while this manager holds the pipe, or return the session error."""
        try:
            with self._io_session():
                return body()
        except _IoSessionUnavailable as e:
            return e.args[0]

    @classmethod
    def get(cls, exe: str, env: dict[str, str], *, pool: str = WORKER_POOL_DEFAULT) -> PythonWorkerManager:
        """Return the singleton worker for *pool* + *exe* (caller should pass a scrubbed env dict)."""
        key = _worker_registry_key(exe, pool)
        prefix = f"{pool}:"
        stale: list[PythonWorkerManager] = []
        with _registry_lock:
            # The registry key is pool:exe. A new Settings path used to leave
            # the previous child running until LibreOffice exited.
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
        inherited: list[BackgroundHandle] = []
        for old in stale:
            # Do not wait: Settings calls get() on the UI thread. The
            # replacement joins these handles before its next Popen.
            old._terminate_worker(wait=False)
            inherited.extend(old._take_reap_handles())
        if inherited:
            mgr._inherit_reaps(inherited)
        return mgr

    @classmethod
    def shutdown_all(cls) -> None:
        """Terminate all workers (tests / extension teardown)."""
        # shutdown_all must not skip _retired: the retry spawned an untracked
        # warm process. get() already sets the flag before terminate so an
        # in-flight execute cannot Popen a child the registry dropped. Drop
        # the lock before terminate: an in-flight tool call may need the UI
        # thread, and terminate must not hold _registry_lock across that.
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
        # Any other thread used to call Lock.acquire() with no timeout. The
        # holder can be inside a tool RPC that is waiting on the thread stuck
        # in acquire (the UI pump, or whichever worker must run the host
        # callback), so the wait never ends and the UI stays frozen. Refuse
        # the owner, anyone while a tool call is in progress, and the UI
        # thread while the pipe lock is held.
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
        err = self._ensure_warmed()
        if err is not None:
            log.warning("Python worker warm failed: %s", err.get("message"))

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
        # allow_heartbeat belongs on every long job, not only the code branch.
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
        """Kill the desynced worker and return an error without resending the request.

        The reap runs in the background so this return does not sit in
        ``proc.wait``. The next ``_ensure_running`` joins it before spawn.
        """
        self._terminate_worker(wait=False)
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

        def _closed_without_response() -> NoReturn:
            stderr_out = self._drain_stderr()
            message = f"Worker closed stdout without a response{stderr_out}"
            if state.may_have_run:
                # Work may already have run. Resending this id would
                # run it again. A close before exec_started is a
                # failed start and falls through to the retry.
                raise _NoTerminalFrame(message)
            raise RuntimeError(message)

        while True:
            if allow_heartbeat:
                from plugin.framework.constants import EMBEDDINGS_HEARTBEAT_GRACE_S

                grace = int(heartbeat_grace_sec if heartbeat_grace_sec is not None else EMBEDDINGS_HEARTBEAT_GRACE_S)
                # Already unpickled. A second unpack of a multi-megabyte
                # result frame was pure CPU on this path.
                response = self._read_response_with_heartbeats(
                    stdout,
                    host_read_timeout_sec,
                    grace,
                    on_heartbeat,
                    stop_checker=stop_checker,
                )
                if response is None:
                    _closed_without_response()
            else:
                response_bytes = self._read_response_bytes(stdout, host_read_timeout_sec, stop_checker=stop_checker)
                if not response_bytes:
                    _closed_without_response()
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
                    # An oversize request fails in pack_pickle_frame before any byte
                    # reaches the pipe. That must not hit the retry handler,
                    # which killed the warm worker twice and wiped every shared
                    # session.
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
                except (subprocess.TimeoutExpired, IpcPartialFrameTimeout) as e:
                    # User code / C-extension hung: killing and replaying would double the wait.
                    # Windows raises IpcPartialFrameTimeout (an OSError) once any
                    # byte of a frame is buffered. That missed this branch, and
                    # with exec_started only partly read may_have_run was still
                    # false, so the outer handler resent the script. The child
                    # writes exec_started and then runs user code immediately, so
                    # file and DuckDB side effects ran twice. POSIX already
                    # raises TimeoutExpired for that stall.
                    log.warning("Python worker read timed out: %s", e)
                    return self._fail_no_replay(
                        "VENV_TIMEOUT",
                        _worker_error_message(e) + _SHARED_WORKER_RESTART_HINT,
                        details={"timeout_sec": timeout_sec, "exe": self.exe},
                    )
                except ValueError as e:
                    # A bad pickle must not kill the child and resend the same request.
                    # Side effects that already ran (DuckDB writes, a trusted
                    # update) would run twice. Id mismatch already refuses that
                    # replay; unpickle does too.
                    log.warning("Python worker frame rejected (not replaying): %s", e)
                    return self._fail_no_replay("WORKER_IPC_ERROR", f"Python worker failed: {e}{_SHARED_WORKER_RESTART_HINT}")
                except (OSError, RuntimeError) as e:
                    # RuntimeError must not look only at dispatched_intermediate. A
                    # later RuntimeError (bad frame, closed pipe) after
                    # exec_started re-raised into the attempt loop and ran the
                    # same script on a new child. _NonReplayableIpcWriteTimeout
                    # is a RuntimeError, so a mid-turn host-response write
                    # timeout is caught here first. Re-raising when the turn
                    # has not started lets the dedicated handler below refuse
                    # the replay; after exec_started this branch already does.
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
                    # ran twice. The catch is the unpack contract, not only
                    # ValueError: int(inf) on an int column is OverflowError,
                    # and a bad shape can be TypeError, AttributeError, or
                    # KeyError. Those miss the outer replay tuple and used to
                    # escape as an uncaught exception.
                    return self._normalize_response(response)
                except _HOST_UNPACK_ERRORS as e:
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
            except _WorkerRetired as e:
                # The registry already dropped this manager. The generic handler
                # below would terminate it and clear the Calc add-in cache twice
                # even though no worker died.
                log.warning("Python worker retired: %s", e)
                return _worker_error("WORKER_IPC_ERROR", str(e), details={"exe": self.exe})
            except subprocess.TimeoutExpired as e:
                # Initial stdin write only. _write_bytes_with_timeout already
                # detached the child and started the reap. Terminating again
                # made it unclear who kills the worker, and a second wait
                # would block on that reap. Host read timeouts return above.
                log.warning("Python worker failed (attempt %s): %s", attempt + 1, e)
                if self._proc is not None:
                    self._terminate_worker(wait=False)
                    _clear_host_state_after_worker_death()
                if attempt == 0:
                    continue
                return _worker_error(
                    "VENV_TIMEOUT",
                    _worker_error_message(e),
                    details={"exe": self.exe, "attempt": attempt + 1},
                )
            except (BrokenPipeError, ValueError, RuntimeError, OSError) as e:
                log.warning("Python worker failed (attempt %s): %s", attempt + 1, e)
                # Do not join the reap on this thread. A retry calls
                # _ensure_running, which joins before Popen. The last attempt
                # returns the error while the reap continues.
                self._terminate_worker(wait=False)
                _clear_host_state_after_worker_death()
                if attempt == 0:
                    continue
                return _worker_error(
                    "WORKER_IPC_ERROR",
                    _worker_error_message(e),
                    details={"exe": self.exe, "attempt": attempt + 1},
                )
        raise AssertionError("worker attempt loop exited without a result")


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

        return self._run_in_session(
            lambda: self._execute_ipc_unlocked(
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
        )

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
        raw = self._run_in_session(
            lambda: self._execute_ipc_unlocked(
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
                # Short writes on a raw pipe (bufsize=0) desync the length prefix.
                # _write_all loops; this thread only bounds how long that can block.
                _write_all(stdin, payload)
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
            # The writer is a daemon on the old pipe. Do not join it here: that
            # waited another 5s on the caller after the kill. The reap closes the
            # pipe; the next spawn joins the reap first.
            log.warning("%s write timed out after %ss; terminating Python worker", label, timeout_sec)
            self._terminate_worker(wait=False)
            _clear_host_state_after_worker_death()
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
            raise _WorkerRetired(
                "Python worker was replaced by a new venv path and will not be restarted"
            )
        # A settings change reaps the previous interpreter off the UI thread.
        # Join that reap before reuse or spawn so the old tree is gone first.
        self._join_reaps()
        if self._proc is not None and self._proc.poll() is None:
            return
        # The join above ran before this reap existed. wait=False starts it
        # without blocking; the join below is the one that runs before Popen,
        # so the new child does not overlap the old tree.
        self._terminate_worker(wait=False)
        self._join_reaps()
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
            # preexec_fn=os.setsid runs in the child between fork and exec. In a
            # threaded host only the forking thread is cloned, so a lock held
            # by another thread (malloc, logging) deadlocks the child before
            # exec. start_new_session asks the C spawn path to call setsid,
            # which is the same new process group without that Python callback.
            # pgid stays equal to pid for _kill_process_tree.
            popen_kw["start_new_session"] = True
        # The top-of-function check can pass, then shutdown_all (or get())
        # sets _retired, and this call would still Popen. Re-check immediately
        # before spawn so that retry cannot start an untracked child.
        with self._proc_lock:
            if self._retired:
                raise _WorkerRetired(
                    "Python worker was replaced by a new venv path and will not be restarted"
                )
            proc = subprocess.Popen(wrap_command_for_sandbox([self.exe, _HARNESS_PATH]), **popen_kw)
            optimize_popen_pipes(proc)
            # Live stderr drain: prevent 64KB pipe deadlock while parent blocks on stdin/stdout.
            # Publish the drain before _proc. The old order let a reader observe
            # a live proc with no drain. stderr=PIPE always gets a tail, so
            # _drain_stderr does not need a second read of the pipe.
            drain = start_stderr_drain(
                proc.stderr,
                name=f"venv-stderr-{proc.pid}",
            )
            self._stderr_drain = drain
            self._proc = proc
            log.debug("Started Python worker pid=%s exe=%s", proc.pid, self.exe)

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
            if _stop_requested(stop_checker):
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
                    return _read_bytes_with_timeout_win32(stdout, nbytes, time.monotonic() + remaining, timeout_sec, cmd=self.exe, stop_checker=stop_checker)
                except (subprocess.TimeoutExpired, IpcPartialFrameTimeout):
                    # ipc reports Stop as a timeout. A mid-frame stall is
                    # IpcPartialFrameTimeout, not TimeoutExpired; both must
                    # become CANCELLED when the user already pressed Stop.
                    if _stop_requested(stop_checker):
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
        # Report the configured window, not the time left. An expired wait
        # otherwise says "timed out after 1 seconds" whatever the real window was.
        label = timeout_label if timeout_label is not None else max(1, int(remaining))

        if sys.platform == "win32":
            if remaining <= 0:
                raise subprocess.TimeoutExpired(cmd=self.exe, timeout=label)
            return self._read_exact_win32(stdout, nbytes, remaining, label, stop_checker)

        buf = bytearray()
        while len(buf) < nbytes:
            if _stop_requested(stop_checker):
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
        grace_sec: float,
        on_heartbeat: Callable[[dict[str, Any]], None] | None,
        stop_checker: Callable[[], bool] | None = None,
        absolute_cap_sec: float | None = None,
    ) -> dict[str, Any] | None:
        from plugin.framework.constants import HEARTBEAT_ABSOLUTE_CAP_SEC
        from plugin.scripting.venv.worker_heartbeat import FRAME_HEARTBEAT, FRAME_RESULT, parse_frame

        # Each heartbeat used to set the deadline to now+grace with no end.
        # Trusted actions do not use the sandbox alarm, so a loop that kept
        # emitting held _io_lock until something else killed the child.
        # Heartbeats may move the short deadline forward, but never past an
        # absolute ceiling from the start of this read.
        cap = float(HEARTBEAT_ABSOLUTE_CAP_SEC if absolute_cap_sec is None else absolute_cap_sec)
        start = time.monotonic()
        ceiling = start + cap
        window = float(max(timeout_sec, grace_sec))
        first = start + window
        if first > ceiling:
            first = ceiling
            label = cap
        else:
            label = window
        deadline_holder = [first, label]

        def _read_exact(n: int) -> bytes:
            if _stop_requested(stop_checker):
                raise _StopRequested()
            return self._read_exact_before_deadline(stdout, n, deadline_holder[0], stop_checker, timeout_label=deadline_holder[1])

        while True:
            now = time.monotonic()
            if now >= deadline_holder[0]:
                raise subprocess.TimeoutExpired(cmd=self.exe, timeout=deadline_holder[1])
            frame_bytes = self._read_frame_bytes(stdout, _read_exact)
            if not frame_bytes:
                return None
            data = parse_frame(frame_bytes)
            frame_type = data.get("frame_type")
            if frame_type == FRAME_HEARTBEAT:
                payload = data.get("payload")
                if on_heartbeat is not None and isinstance(payload, dict):
                    try:
                        on_heartbeat(payload)
                    except Exception:
                        log.exception("Heartbeat callback failed (ignoring)")
                extended = time.monotonic() + float(grace_sec)
                if extended > ceiling:
                    deadline_holder[0] = ceiling
                    deadline_holder[1] = cap
                else:
                    deadline_holder[0] = extended
                    deadline_holder[1] = float(grace_sec)
                continue
            if frame_type == FRAME_RESULT or frame_type is None:
                if not data:
                    # parse_frame turns a non-dict into {}. Re-read only that
                    # empty case so a bad payload stays a read error. A real
                    # result is the dict already parsed above.
                    raw = unpack_pickle_frame(frame_bytes)
                    if not isinstance(raw, dict):
                        raise RuntimeError("Worker response must be a dict")
                    return raw
                return data
            # Unknown frames must not extend the deadline. A protocol drift
            # then fails as a timeout instead of looking like a silent stall.
            log.warning("venv worker ignoring unknown frame type: %r", frame_type)

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
        if drain is None:
            return ""
        text = drain.finish_text().strip()
        return f"\nWorker stderr:\n{text}" if text else ""

    def _terminate_worker(self, *, wait: bool = True) -> None:
        """Detach the child and reap it off this thread.

        *wait* joins the reap. ``get``, a failed turn, a stdin write
        timeout, and the generic retry handler pass ``wait=False`` so the
        caller is not stuck in ``proc.wait``. ``_ensure_running`` detaches a
        dead child the same way and joins that reap once, immediately before
        ``Popen``. ``shutdown_all`` waits.
        """
        with self._proc_lock:
            proc = self._proc
            stderr_drain = self._stderr_drain
            self._proc = None
            self._primed = False
            self._stderr_drain = None
            if not hasattr(self, "_reap_handles"):
                self._reap_handles = []
        if proc is None and stderr_drain is None:
            if wait:
                self._join_reaps()
            return
        handle = run_in_background(
            _reap_worker_process,
            proc,
            stderr_drain,
            name="venv-worker-reap",
            dedicated=True,
        )
        with self._proc_lock:
            self._reap_handles.append(handle)
        if wait:
            self._join_reaps()

    def _join_reaps(self) -> None:
        """Wait until every reap started for this manager has finished."""
        with self._proc_lock:
            if not hasattr(self, "_reap_handles"):
                self._reap_handles = []
            handles = list(self._reap_handles)
        for handle in handles:
            handle.join()
        with self._proc_lock:
            self._reap_handles = [handle for handle in self._reap_handles if handle.is_alive()]

    def _take_reap_handles(self) -> list[BackgroundHandle]:
        """Hand in-flight reaps to another manager. The caller must join them."""
        with self._proc_lock:
            if not hasattr(self, "_reap_handles"):
                self._reap_handles = []
            handles = self._reap_handles
            self._reap_handles = []
            return handles

    def _inherit_reaps(self, handles: list[BackgroundHandle]) -> None:
        if not handles:
            return
        with self._proc_lock:
            if not hasattr(self, "_reap_handles"):
                self._reap_handles = []
            self._reap_handles.extend(handles)


# --- Public entrypoints ---


def _resolve_worker_python(
    *,
    pool: str = WORKER_POOL_DEFAULT,
) -> tuple[str | None, dict[str, Any] | None]:
    """Return (exe, error_response) for the configured venv / LO interpreter.

    Config is process-global (``get_config_str``). Callers that still have a
    UNO context pass it to ``run_code_in_user_venv``, not here.
    """
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
    *,
    pool: str = WORKER_POOL_DEFAULT,
) -> tuple[PythonWorkerManager | None, dict[str, Any] | None]:
    exe, err = _resolve_worker_python(pool=pool)
    if err is not None:
        return None, err
    assert exe is not None
    child_env = scrub_subprocess_env(dict(os.environ))
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
    python_tool_domain: str | None = None,
    worker_pool: str = WORKER_POOL_DEFAULT,
    allow_heartbeat: bool = False,
    heartbeat_grace_sec: int | None = None,
    on_heartbeat: Callable[[dict[str, Any]], None] | None = None,
    action: str | None = None,
    stop_checker: Callable[[], bool] | None = None,
    cancellation_scope: Any | None = None,
) -> dict[str, Any]:
    """Execute *code* or handle *action* via :class:`PythonWorkerManager` (warm process).

    *uno_ctx* is unused. Config is process-global; the argument stays so
    existing callers can keep passing a context.

    Without *session_id*, each call uses an isolated namespace in the child. With
    *session_id*, the child reuses one namespace per workbook (shared kernel).

    *worker_pool* selects which warm child to use (e.g. embeddings vs Calc/chat default).

    *python_tool_domain* scopes venv→LO tool RPC: ``None`` = all tools, ``""`` =
    disabled (``=PY()``), a domain name = that domain's proxies. See
    ``plugin.scripting.host_rpc``.

    *script_session_id* is host-only. Tool RPC resolves the document from it
    ahead of *session_id*. Chat passes a ``doc:`` pin so ``wa.*`` uses
    ``ctx.doc`` without putting that id on the child namespace.
    """
    if not action and not (code or "").strip():
        return _worker_error("WORKER_IPC_ERROR", "No code provided.")

    manager, err = _worker_manager_for_ctx(pool=worker_pool)
    if err is not None:
        return err
    assert manager is not None

    configured = configured_python_exec_timeout()
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


def reset_python_session(uno_ctx: Any, session_id: str, *, timeout_sec: int | None = None) -> dict[str, Any]:
    """Drop the shared-kernel executor for *session_id* in the warm worker.

    *uno_ctx* is unused. Config is process-global; the argument stays so
    existing callers can keep passing a context.
    """
    if not (session_id or "").strip():
        return _worker_error("WORKER_IPC_ERROR", "No session_id provided.")

    manager, err = _worker_manager_for_ctx()
    if err is not None:
        return err
    assert manager is not None

    configured = configured_python_exec_timeout()
    timeout_sec = resolve_python_exec_timeout(timeout_sec, configured=configured)

    return manager.execute(
        None,
        timeout_sec=timeout_sec,
        session_id=session_id,
        action="reset_session",
    )


@background
def warm_venv_worker(uno_ctx: Any, pool: str = WORKER_POOL_DEFAULT) -> None:
    """Pre-warm a specific venv subprocess pool (spawn + trigger auto-imports + load embedding model if embeddings pool). Safe to call from a background thread.

    *uno_ctx* is unused. Config is process-global; the argument stays so
    existing callers can keep passing a context.
    """
    manager, err = _worker_manager_for_ctx(pool=pool)
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
                timeout_val = embeddings_worker_timeout_sec()
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
    "run_code_in_user_venv",
    "warm_venv_worker",
]
