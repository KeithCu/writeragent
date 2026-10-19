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
import queue
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, IO, Iterator, NoReturn

from plugin.framework.config import get_config_str
from plugin.framework.thread_guard import background, on_main_thread
from plugin.framework.constants import (
    EMBEDDINGS_HEARTBEAT_GRACE_S,
    HEARTBEAT_ABSOLUTE_CAP_SEC,
    WORKER_POOL_DEFAULT,
    WORKER_POOL_EMBEDDINGS,
)
from plugin.framework.worker_pool import (
    BackgroundHandle,
    StderrTail,
    run_in_background,
)
from plugin.framework.managed_subprocess import ManagedSubprocess, kill_process_tree
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
    DEFAULT_MAX_PAYLOAD_BYTES,
    EXEC_STARTED,
    IpcFrameError,
    IpcPartialFrameTimeout,
    pack_pickle_frame,
    read_frame_payload,
    unpack_pickle_frame,
    write_packed_frame_with_timeout,
)
from plugin.scripting.payload_codec import _HOST_UNPACK_ERRORS, host_unpack_data
from plugin.scripting.sandbox import (
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
    from plugin.calc.python.function import clear_python_addin_cache

    clear_python_addin_cache()


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
        # This is the chat SendCancellation; ToolContext.send_cancellation is what registers the HTTP call.
        send_cancellation=cancellation_scope,
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
            kill_process_tree(proc)
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


@dataclass
class _SpawnJob:
    done: threading.Event
    start: Callable[[], None]
    error: BaseException | None = None


@dataclass(frozen=True)
class _ExecRequest:
    """One host execute. ``wire()`` is the child pickle; host-only fields stay off it.

    ``request_id`` is minted once so a pre-start retry writes the same ``id``.
    """

    code: str | None = None
    data: Any = None
    bindings: dict[str, Any] | None = None
    timeout_sec: int | None = None
    session_id: str | None = None
    action: str | None = None
    init_script: str | None = None
    init_session_id: str | None = None
    init_script_hash: str | None = None
    allow_heartbeat: bool = False
    heartbeat_grace_sec: int | None = None
    on_heartbeat: Callable[[dict[str, Any]], None] | None = None
    on_worker_event: Callable[[dict[str, Any]], None] | None = None
    stop_checker: Callable[[], bool] | None = None
    cancellation_scope: Any | None = None
    python_tool_domain: str | None = None
    caller: str = "script"
    script_session_id: str | None = None
    request_id: str = field(default_factory=lambda: str(uuid.uuid4()))

    def wire(self) -> dict[str, Any]:
        # Key insertion order is the pickle order. Do not sort, do not
        # dataclasses.asdict (it would copy host fields and deepcopy ``data``).
        request: dict[str, Any] = {"id": self.request_id}
        if self.timeout_sec is not None:
            request["timeout_sec"] = self.timeout_sec
        if self.session_id:
            request["session_id"] = self.session_id
        if self.data is not None:
            request["data"] = self.data
        # allow_heartbeat belongs on every long job, not only the code branch.
        # run_trusted_action (embeddings, folder index) never received it, so
        # the child sent no heartbeat and the host killed a long job.
        if self.allow_heartbeat:
            request["allow_heartbeat"] = True
        if self.action:
            request["action"] = self.action
        else:
            request["code"] = self.code if self.code is not None else ""
            if self.bindings:
                request["bindings"] = self.bindings
            if self.init_script:
                request["init_script"] = self.init_script
            if self.init_session_id:
                request["init_session_id"] = self.init_session_id
            if self.init_script_hash:
                request["init_script_hash"] = self.init_script_hash
        return request


class PythonWorkerManager:
    """One warm child process per (pool, Python executable path) pair."""

    exe: str
    env: dict[str, str]
    _io_lock: threading.Lock
    _primed: bool
    _retired: bool
    _proc_lock: threading.Lock
    _reap_handles: list[BackgroundHandle]
    _spawn_handle: BackgroundHandle | None
    _spawn_queue: queue.Queue[_SpawnJob | None] | None
    _spawn_ident: int | None
    _spawn_ready: threading.Event | None

    def __init__(self, exe: str, env: dict[str, str]) -> None:
        self.exe = exe
        self.env = dict(env)
        self.env["WRITERAGENT_IS_WORKER"] = "1"
        self._proc: subprocess.Popen[Any] | None = None
        self._io_lock = threading.Lock()
        # Same-thread owner -> WORKER_REENTRY; tool RPC in flight or UI thread while
        # locked -> busy; UI never blocks (_acquire_io timeout=0). _io_owner and
        # _serving_tool_call are plain GIL-atomic attributes read without _io_lock on
        # purpose: the holder keeps it across host RPC. Readers load self._proc once
        # into a local: _terminate_worker stores None without taking _io_lock.
        self._io_owner: int | None = None
        self._serving_tool_call: bool = False
        self._primed = False
        self._retired = False
        self._proc_lock = threading.Lock()
        self._stderr_drain: StderrTail | None = None
        # In-flight background reaps. get() moves a replaced manager's handles
        # onto the replacement so the next spawn waits for the old tree.
        self._reap_handles = []
        self._spawn_handle = None
        self._spawn_queue = None
        self._spawn_ident = None
        self._spawn_ready = None

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
            old._stop_spawn_thread(wait=False)
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
            mgr._stop_spawn_thread(wait=True)

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
        # One load: _terminate_worker may store None concurrently.
        proc = self._proc
        return proc is not None and proc.poll() is None

    def _ensure_warmed_unlocked(self) -> dict[str, Any] | None:
        """Spawn worker and prime auto-imports. Returns error dict or None."""
        if self._primed and self._is_worker_alive():
            return None
        prime = self._execute_ipc_unlocked(
            _ExecRequest("result = None", timeout_sec=WARM_WORKER_TIMEOUT_SEC)
        )
        if prime.get("status") != "ok":
            return prime
        self._primed = True
        return None

    def _reentry_error(self) -> dict[str, Any] | None:
        """Refuse a nested execute that would deadlock this worker's pipe."""
        me = threading.get_ident()
        # Any other thread used to call Lock.acquire() with no timeout. The
        # holder can be inside a tool RPC that is waiting on the thread stuck
        # in acquire (the UI pump, or whichever worker must run the host
        # callback), so the wait never ends and the UI stays frozen. Refuse
        # the owner, anyone while a tool call is in progress, and the UI
        # thread while the pipe lock is held.
        # _serving_tool_call is per manager. =PY() during a chat script is a
        # different thread, not a callback into the same worker. The reentry
        # sentence is only true when this thread already holds the pipe.
        if self._io_owner == me:
            return _worker_error("WORKER_REENTRY", _WORKER_REENTRY_MESSAGE)
        if self._serving_tool_call:
            return _worker_error("WORKER_REENTRY", _WORKER_BUSY_MESSAGE)
        if on_main_thread() and self._io_lock.locked():
            return _worker_error("WORKER_REENTRY", _WORKER_BUSY_MESSAGE)
        return None

    def _acquire_io(self) -> dict[str, Any] | None:
        err = self._reentry_error()
        if err is not None:
            return err
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
                return _worker_error("WORKER_REENTRY", _WORKER_BUSY_MESSAGE)
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
        try:
            with self._io_session():
                return None
        except _IoSessionUnavailable as exc:
            return exc.args[0]

    def warm(self) -> None:
        """Spawn the worker and trigger auto-imports (numpy etc.) so the next real execute is instant."""
        err = self._ensure_warmed()
        if err is not None:
            log.warning("Python worker warm failed: %s", err.get("message"))

    def _fail_no_replay(self, code: str, message: str, *, details: dict[str, Any] | None = None) -> dict[str, Any]:
        """Kill the desynced worker and return an error without resending the request.

        The reap runs in the background so this return does not sit in
        ``proc.wait``. The next ``_ensure_running`` joins it before spawn.
        """
        self._terminate_worker(wait=False)
        _clear_host_state_after_worker_death()
        return _worker_error(code, message, details=details if details is not None else {"exe": self.exe})

    def _fail_ipc_no_replay(self, exc: BaseException) -> dict[str, Any]:
        """Terminal IPC failure after the request may have run. Same text at each site."""
        return self._fail_no_replay("WORKER_IPC_ERROR", f"Python worker failed: {exc}{_SHARED_WORKER_RESTART_HINT}")

    def _read_until_terminal(
        self,
        stdout: IO[bytes],
        stdin: IO[bytes],
        req: _ExecRequest,
        wire: dict[str, Any],
        state: _TurnState,
        *,
        host_read_timeout_sec: float,
        write_timeout_sec: float,
        allowed_tools: frozenset[str] | None,
        resolved_script_session_id: str | None,
    ) -> dict[str, Any]:
        """Serve intermediate frames until a terminal one; flags go into *state*.

        *state* is the caller's object so its except handlers still see
        exec_started / tool_call when this raises.
        """

        def _stdin_write(blob: bytes) -> None:
            try:
                self._write_packed_frame_with_timeout(
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
            if req.allow_heartbeat:
                grace = int(req.heartbeat_grace_sec if req.heartbeat_grace_sec is not None else EMBEDDINGS_HEARTBEAT_GRACE_S)
                # Already unpickled. A second unpack of a multi-megabyte
                # result frame was pure CPU on this path.
                response = self._read_response_with_heartbeats(
                    stdout,
                    host_read_timeout_sec,
                    grace,
                    req.on_heartbeat,
                    stop_checker=req.stop_checker,
                )
                if response is None:
                    _closed_without_response()
            else:
                response_bytes = self._read_response_bytes(stdout, host_read_timeout_sec, stop_checker=req.stop_checker)
                if not response_bytes:
                    _closed_without_response()
                response = unpack_pickle_frame(response_bytes)
                if not isinstance(response, dict):
                    raise RuntimeError("Worker response must be a dict")
            if response.get("type") == EXEC_STARTED:
                state.execution_started = True
                if response.get("id") != wire.get("id"):
                    # Hand the mismatched frame back; the caller's id check
                    # refuses it without replaying the request.
                    return response
                continue

            # A tool_call (or ppt llm_request / worker_event) can mutate the
            # document and then raise. The flag used to flip only after
            # dispatch returned, so a RuntimeError with no exec_started left
            # may_have_run false and the attempt loop sent the request again.
            if response.get("type") in ("tool_call", "llm_request", "worker_event"):
                state.dispatched_intermediate = True
            self._serving_tool_call = True
            try:
                is_intermediate = _maybe_dispatch_intermediate_response(
                    response,
                    stdin_write=_stdin_write,
                    allowed_tools=allowed_tools,
                    caller=req.caller,
                    on_worker_event=req.on_worker_event,
                    stop_checker=req.stop_checker,
                    cancellation_scope=req.cancellation_scope,
                    script_session_id=resolved_script_session_id,
                )
            finally:
                self._serving_tool_call = False
            if is_intermediate:
                state.dispatched_intermediate = True
                continue
            return response

    def _execute_ipc_unlocked(self, req: _ExecRequest) -> dict[str, Any]:
        wire = req.wire()
        timeout_sec = req.timeout_sec
        if timeout_sec is None:
            raise RuntimeError("timeout_sec is required")
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
                # Resolve tools and session id before writing the request. These are
                # host-only; if they fail after the write, the retry path would resend
                # the same id to a new child (may_have_run is still false until the
                # read starts).
                from plugin.scripting.host_rpc import resolve_allowed_tools

                allowed_tools = resolve_allowed_tools(req.python_tool_domain)
                # Pin wins over the kernel session id. Chat passes doc:… and
                # leaves request session_id unset. RPS / =PY() / PPT-Master
                # omit the pin and keep the request id.
                resolved_script_session_id = host_script_session_id(wire.get("session_id"), req.script_session_id)
                # The host read timeout includes a grace buffer so the child's in-process
                # signal/thread timeout fires first and returns a clean error frame without
                # terminating the warm subprocess (preserving shared workbook sessions).
                host_read_timeout_sec = float(timeout_sec) + float(HOST_IPC_READ_GRACE_SEC)
                try:
                    self._write_frame_with_timeout(stdin, wire, timeout_sec=write_timeout_sec, label="request")
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
                        req,
                        wire,
                        state,
                        host_read_timeout_sec=host_read_timeout_sec,
                        write_timeout_sec=write_timeout_sec,
                        allowed_tools=allowed_tools,
                        resolved_script_session_id=resolved_script_session_id,
                    )
                except (subprocess.TimeoutExpired, IpcPartialFrameTimeout) as e:
                    # Hung user code / C-extension: do not kill and replay (doubles the
                    # wait and side effects). Windows used to raise IpcPartialFrameTimeout
                    # (OSError) after buffering any byte, which missed this branch while
                    # may_have_run was still false, so the outer handler resent the script.
                    # Both platforms: partial chunk -> IpcPartialFrameTimeout; empty buffer
                    # -> TimeoutExpired. Catch both here so neither replays.
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
                    return self._fail_ipc_no_replay(e)
                except (OSError, RuntimeError) as e:
                    # After may_have_run, do not replay on RuntimeError (bad frame,
                    # closed pipe, or mid-turn host-response write timeout —
                    # _NonReplayableIpcWriteTimeout). Looking only at
                    # dispatched_intermediate used to re-raise into the attempt
                    # loop and run the script again. Before the turn starts, re-raise
                    # so the dedicated handler below can refuse the replay.
                    if not state.may_have_run:
                        raise
                    log.warning("Python worker failed after execution started (not replaying): %s", e)
                    return self._fail_ipc_no_replay(e)
                req_id = wire.get("id")
                # Empty-id error before exec: the harness rejected the pickle and is
                # back at read(). Return its message and keep the worker. Treating this
                # as an id mismatch used to kill a healthy child. After exec_started or
                # a tool frame, any other id mismatch still kills.
                if (
                    response.get("id") == ""
                    and response.get("status") == "error"
                    and not state.may_have_run
                ):
                    message = response.get("message")
                    text = message if isinstance(message, str) and message else "Invalid pickle request"
                    log.warning("Python worker rejected the request frame (worker kept): %s", text)
                    return _worker_error("WORKER_IPC_ERROR", text, details={"exe": self.exe})
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
                    return self._fail_ipc_no_replay(e)
            except _StopRequested:
                # Stop while waiting on the pipe: the child may be mid-script, so
                # it is killed (never replayed) and the caller sees CANCELLED.
                log.info("Python worker stopped by user")
                return self._fail_no_replay("CANCELLED", "Python worker stopped by user")
            except (_NoTerminalFrame, _NonReplayableIpcWriteTimeout) as e:
                log.warning("Python worker failed without replay: %s", e)
                return self._fail_ipc_no_replay(e)
            except _WorkerRetired as e:
                # The registry already dropped this manager. The generic handler
                # below would terminate it and clear the Calc add-in cache twice
                # even though no worker died.
                log.warning("Python worker retired: %s", e)
                return _worker_error("WORKER_IPC_ERROR", str(e), details={"exe": self.exe})
            except subprocess.TimeoutExpired as e:
                # Initial stdin write only. _write_packed_frame_with_timeout already
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
                _ExecRequest(
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
                _ExecRequest(
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
        """Pack, then bound the pipe write. ``IpcFrameError`` means no byte was written."""
        frame = pack_pickle_frame(message, max_payload_bytes=DEFAULT_MAX_PAYLOAD_BYTES)
        self._write_packed_frame_with_timeout(stdin, frame, timeout_sec=timeout_sec, label=label)

    def _write_packed_frame_with_timeout(
        self,
        stdin: IO[bytes],
        frame: bytes,
        *,
        timeout_sec: float,
        label: str,
    ) -> None:
        """Write a packed frame; on timeout kill the child (a partial frame may be in the pipe).

        A dead child raises ``BrokenPipeError`` from ``is_alive``; the caller handles it.
        """
        try:
            write_packed_frame_with_timeout(
                stdin,
                frame,
                timeout_sec,
                is_alive=self._is_worker_alive,
            )
        except subprocess.TimeoutExpired as exc:
            log.warning("%s write timed out after %ss; terminating Python worker", label, timeout_sec)
            self._terminate_worker(wait=False)
            _clear_host_state_after_worker_death()
            raise subprocess.TimeoutExpired(cmd=self.exe, timeout=timeout_sec) from exc

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

    def _ensure_spawn_thread(self) -> None:
        if self._retired:
            raise _WorkerRetired(
                "Python worker was replaced by a new venv path and will not be restarted"
            )
        if self._spawn_handle is not None and self._spawn_handle.is_alive():
            return
        spawn_queue: queue.Queue[_SpawnJob | None] = queue.Queue()
        ready = threading.Event()
        self._spawn_queue = spawn_queue
        self._spawn_ready = ready
        self._spawn_ident = None
        self._spawn_handle = run_in_background(
            self._spawn_loop,
            name="venv-worker-spawn",
            dedicated=True,
        )
        if not ready.wait(timeout=5.0):
            raise RuntimeError("venv worker spawn thread did not start")

    def _spawn_loop(self) -> None:
        self._spawn_ident = threading.get_ident()
        if self._spawn_ready is not None:
            self._spawn_ready.set()
        q = self._spawn_queue
        if q is None:
            return
        while True:
            job = q.get()
            if job is None:
                return
            try:
                if self._retired:
                    job.error = _WorkerRetired(
                        "Python worker was replaced by a new venv path and will not be restarted"
                    )
                else:
                    try:
                        job.start()
                    except BaseException as exc:
                        job.error = exc
            finally:
                job.done.set()

    def _start_child(self, managed: ManagedSubprocess) -> None:
        if threading.get_ident() == self._spawn_ident:
            managed.start()
            return
        self._ensure_spawn_thread()
        if self._spawn_queue is None:
            raise RuntimeError("venv worker spawn queue is missing")
        job = _SpawnJob(done=threading.Event(), start=managed.start)
        self._spawn_queue.put(job)
        job.done.wait()
        if job.error is not None:
            raise job.error

    def _ensure_running(self) -> None:
        if self._retired:
            raise _WorkerRetired(
                "Python worker was replaced by a new venv path and will not be restarted"
            )
        # A settings change reaps the previous interpreter off the UI thread.
        # Join that reap before reuse or spawn so the old tree is gone first.
        self._join_reaps()
        # One load: _terminate_worker may store None concurrently.
        proc = self._proc
        if proc is not None and proc.poll() is None:
            # Re-check _retired after _join_reaps: the flag can flip while that
            # wait blocks, and returning the still-live child would hand a retired
            # worker to the writer. Same _WorkerRetired as the spawn path. No lock
            # here — holding _proc_lock across the later write deadlocks a
            # concurrent retire.
            if self._retired:
                raise _WorkerRetired(
                    "Python worker was replaced by a new venv path and will not be restarted"
                )
            return
        # The join above ran before this reap existed. wait=False starts it
        # without blocking; the join below is the one that runs before Popen,
        # so the new child does not overlap the old tree.
        self._terminate_worker(wait=False)
        self._join_reaps()
        # The top-of-function check can pass, then shutdown_all (or get())
        # sets _retired, and this call would still Popen. Re-check immediately
        # before spawn so that retry cannot start an untracked child.
        with self._proc_lock:
            if self._retired:
                raise _WorkerRetired(
                    "Python worker was replaced by a new venv path and will not be restarted"
                )
            # preexec_fn=os.setsid runs between fork and exec and deadlocks a threaded host.
            # start_new_session is the C spawn path, same new process group, pgid == pid.
            # register=False because desktop shutdown_all would kill_tree this child.
            # notifyTermination does not call PythonWorkerManager.shutdown_all.
            # That reap joins on the UI thread. An idle child exits on stdin EOF.
            # Linux native stalls use PR_SET_PDEATHSIG in the harness. Windows and
            # macOS do not; quit does not wait on this process.
            # log_start_failure=False because a missing interpreter must stay OSError.
            managed = ManagedSubprocess(
                [self.exe, _HARNESS_PATH],
                base_env=self.env,
                command_wrapper=wrap_command_for_sandbox,
                optimize_pipes=True,
                stderr_name="venv-stderr",
                stderr_name_with_pid=True,
                register=False,
                log_start_failure=False,
                label="Python worker",
            )
            # Spawn on this manager's long-lived venv-worker-spawn thread, not
            # the caller. On Linux, PR_SET_PDEATHSIG kills the child when the
            # creating thread exits — warm/index/chat workers return after the
            # call, which dropped shared sessions while LibreOffice was still up.
            self._start_child(managed)
            # Publish the drain before _proc. A reader can observe _proc without
            # _proc_lock. The old order let that reader see a live proc with no drain.
            self._stderr_drain = managed.stderr_tail
            self._proc = managed.popen
            log.debug("Started Python worker pid=%s exe=%s", managed.pid, self.exe)

    def _read_response_bytes(self, stdout: IO[bytes], timeout_sec: float | int, stop_checker: Callable[[], bool] | None = None) -> bytes:
        if self._proc is None:
            raise RuntimeError("worker terminated concurrently")
        return self._read_frame_bytes(stdout, timeout_sec, stop_checker=stop_checker)

    def _read_frame_bytes(
        self,
        stdout: IO[bytes],
        timeout_sec: float | int,
        stop_checker: Callable[[], bool] | None = None,
        *,
        deadline: float | None = None,
    ) -> bytes:
        """Read one frame payload via ipc, mapping Stop to _StopRequested.

        ipc owns the wait; this method only maps Stop to _StopRequested, passes
        self._is_worker_alive, and forces DEFAULT_MAX_PAYLOAD_BYTES plus
        frame_label="venv worker frame".
        """
        try:
            payload = read_frame_payload(
                stdout,
                float(timeout_sec),
                deadline=deadline,
                max_payload_bytes=DEFAULT_MAX_PAYLOAD_BYTES,
                frame_label="venv worker frame",
                cmd=self.exe,
                is_alive=self._is_worker_alive,
                stop_checker=stop_checker,
            )
        except (subprocess.TimeoutExpired, IpcPartialFrameTimeout):
            # ipc reports Stop as TimeoutExpired, including a mid-chunk stall
            # (IpcPartialFrameTimeout). Re-check the checker so Windows peek and
            # the POSIX loop both become CANCELLED.
            if _stop_requested(stop_checker):
                raise _StopRequested() from None
            raise
        return payload or b""

    def _read_response_with_heartbeats(
        self,
        stdout: IO[bytes],
        timeout_sec: float | int,
        grace_sec: float,
        on_heartbeat: Callable[[dict[str, Any]], None] | None,
        stop_checker: Callable[[], bool] | None = None,
        absolute_cap_sec: float | None = None,
    ) -> dict[str, Any] | None:
        from plugin.scripting.ipc import FRAME_HEARTBEAT, FRAME_RESULT, parse_worker_stdout_frame

        # Each heartbeat used to set the deadline to now+grace with no end.
        # Trusted actions do not use the sandbox alarm, so a loop that kept
        # emitting held _io_lock until something else killed the child.
        # Heartbeats may move the short deadline forward, but never past an
        # absolute ceiling from the start of this call. They do not reset it.
        # _read_until_terminal starts a new ceiling after a non-heartbeat
        # frame. Trusted actions emit heartbeats until one result, so for
        # them this call is the whole job.
        cap = float(HEARTBEAT_ABSOLUTE_CAP_SEC if absolute_cap_sec is None else absolute_cap_sec)
        start = time.monotonic()
        ceiling = start + cap
        # First-byte wait is timeout_sec, not max(timeout, grace). Grace is only
        # silence allowed after a heartbeat; using the max made a short timeout
        # wait for grace instead. After a heartbeat:
        # deadline = min(now + grace, ceiling).
        window = float(timeout_sec)
        deadline = start + window
        if deadline > ceiling:
            deadline = ceiling
            timeout_label = cap
        else:
            timeout_label = window

        logged_unknown = False
        while True:
            now = time.monotonic()
            if now >= deadline:
                raise subprocess.TimeoutExpired(cmd=self.exe, timeout=timeout_label)
            frame_bytes = self._read_frame_bytes(
                stdout, timeout_label, stop_checker=stop_checker, deadline=deadline
            )
            if not frame_bytes:
                return None
            data = parse_worker_stdout_frame(frame_bytes)
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
                    deadline = ceiling
                    timeout_label = cap
                else:
                    deadline = extended
                    timeout_label = float(grace_sec)
                continue
            if frame_type == FRAME_RESULT or frame_type is None:
                return data
            # Unknown frames must not extend the deadline (protocol drift should
            # time out, not look like a stall). Log the first unknown type once —
            # per-frame warnings used to inflate wait time and flake short-timeout tests.
            if not logged_unknown:
                log.warning("venv worker ignoring unknown frame type: %r", frame_type)
                logged_unknown = True

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
            handles = list(self._reap_handles)
        for handle in handles:
            handle.join()
        with self._proc_lock:
            self._reap_handles = [handle for handle in self._reap_handles if handle.is_alive()]

    def _take_reap_handles(self) -> list[BackgroundHandle]:
        """Hand in-flight reaps to another manager. The caller must join them."""
        with self._proc_lock:
            handles = self._reap_handles
            self._reap_handles = []
            return handles

    def _inherit_reaps(self, handles: list[BackgroundHandle]) -> None:
        if not handles:
            return
        with self._proc_lock:
            self._reap_handles.extend(handles)

    def _stop_spawn_thread(self, *, wait: bool) -> None:
        handle: BackgroundHandle | None = getattr(self, "_spawn_handle", None)
        queue_: queue.Queue[_SpawnJob | None] | None = getattr(self, "_spawn_queue", None)
        spawn_ident: int | None = getattr(self, "_spawn_ident", None)
        if handle is None or not handle.is_alive():
            return
        if queue_ is not None:
            queue_.put(None)
        if not wait:
            return
        if threading.get_ident() != spawn_ident:
            handle.join()
        self._spawn_handle = None
        self._spawn_queue = None
        self._spawn_ident = None
        self._spawn_ready = None


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
    embeddings = pool == WORKER_POOL_EMBEDDINGS
    # Embeddings cannot fall through to LibreOffice's Python. An empty path
    # is its own error; a configured path shares the venv lookup below.
    if embeddings and not venv_dir:
        return None, _worker_error(
            "VENV_NOT_FOUND",
            "Embeddings require a configured Python venv (Settings → Python). "
            "LibreOffice embedded Python cannot run sentence-transformers or langgraph.",
        )
    if venv_dir:
        exe = resolve_venv_python(venv_dir)
        if not exe:
            if embeddings:
                message = f"Embeddings venv not configured or invalid: {venv_dir!r}"
            else:
                message = f"No python executable found under configured venv: {venv_dir!r}"
            return None, _worker_error("VENV_NOT_FOUND", message)
        if embeddings:
            log.debug("run_venv_code: using embeddings venv interpreter under %s", venv_dir)
        else:
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


def _get_worker_manager(
    *,
    pool: str = WORKER_POOL_DEFAULT,
) -> tuple[PythonWorkerManager | None, dict[str, Any] | None]:
    """Return the warm manager for *pool*, or ``(None, error)``.

    No UNO context: config is process-global. ``assert`` is not the guard.
    ``python -O`` strips asserts, and a missing interpreter must still return
    the error dict.
    """
    exe, err = _resolve_worker_python(pool=pool)
    if exe is None:
        return None, err or _worker_error("VENV_NOT_FOUND", "Could not resolve a Python interpreter.")
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

    manager, err = _get_worker_manager(pool=worker_pool)
    if manager is None:
        return err or _worker_error("WORKER_IPC_ERROR", "Python worker is unavailable.")

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

    manager, err = _get_worker_manager()
    if manager is None:
        return err or _worker_error("WORKER_IPC_ERROR", "Python worker is unavailable.")

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
    manager, err = _get_worker_manager(pool=pool)
    if manager is None:
        log.warning("warm_venv_worker skipped for pool %s: %s", pool, (err or {}).get("message"))
        return
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
