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
import shutil
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, IO, Generator, Literal

from plugin.framework.config import get_config
from plugin.framework.thread_guard import background
from plugin.framework.constants import (
    EMBEDDINGS_HEARTBEAT_GRACE_S,
    HEARTBEAT_ABSOLUTE_CAP_SEC,
    WORKER_POOL_DEFAULT,
    WORKER_POOL_EMBEDDINGS,
)
from plugin.framework.child_runtime import Deadline
from plugin.framework.warm_child import ChildSpec, Death, Lease, WarmChild
from plugin.scripting.config_limits import (
    HOST_IPC_READ_GRACE_SEC,
    VENV_IPC_WRITE_TIMEOUT_SEC,
    WARM_WORKER_TIMEOUT_SEC,
    embeddings_worker_timeout_sec,
    python_exec_timeout,
)
from plugin.framework.send_cancellation import SendCancellation, is_cancelled
from plugin.framework.ipc import (
    DEFAULT_MAX_PAYLOAD_BYTES,
    EXEC_STARTED,
    FRAME_HEARTBEAT,
    FRAME_RESULT,
    IpcFrameError,
    IpcPartialFrameTimeout,
    pack_pickle_frame,
    read_frame_payload,
    unpack_pickle_frame,
    write_packed_frame_with_timeout,
)
from plugin.scripting.host_rpc import handle_tool_call_frame, resolve_allowed_tools
from plugin.scripting.session_key import SessionKey
from plugin.scripting.payload_codec import HOST_UNPACK_ERRORS, host_unpack_data
from plugin.framework.subprocess_env import scrub_subprocess_env
from plugin.scripting.sandbox import (
    detect_sandbox,
    resolve_libreoffice_python,
    resolve_venv_python,
    wrap_command_for_sandbox,
)

log = logging.getLogger(__name__)

_TIMEOUT_AFTER = " timed out after "

# Every caller waits up to _IO_LOCK_ACQUIRE_TIMEOUT_SEC for the pipe, then gets
# WORKER_REENTRY. Matches the scripting timeout ceiling so a second script can
# still run after a long one. The UI thread waits too: bailing leaves a wrong
# answer that does not heal (a stale #VALUE!, or a failed reset), the holder's
# own budget bounds the delay, and the only danger is a cycle, which the two
# refusals in _reentry_error cover. Refusal is only for the owner thread, or while
# the holder serves a host callback. Killing a worker does not wait on the caller
# either: the process tree is reaped in the background. The next spawn joins
# that reap first. A waiter also leaves within _IO_LOCK_POLL_SEC if the holder
# enters a host callback (tool RPC or on_heartbeat).
_IO_LOCK_ACQUIRE_TIMEOUT_SEC = 600.0
_IO_LOCK_POLL_SEC = 0.05

_WORKER_REENTRY_MESSAGE = (
    "This Python tool called back into the same worker and would deadlock the script pipe."
)
_WORKER_BUSY_MESSAGE = (
    "Python worker is busy with another script; try again when it finishes."
)


def _worker_error(code: str, message: str, *, details: dict[str, Any] | None = None) -> dict[str, Any]:
    """Host-constructed error dict. Child payloads still go through ``_normalize_response``."""
    return {
        "status": "error",
        "code": code,
        "message": message,
        "details": details or {},
    }


def parse_worker_stdout_frame(payload: bytes) -> dict[str, Any]:
    """Parse one Pickle5 frame from child stdout into a dictionary.

    Empty and non-dict payloads raise: empty bytes raise ValueError from
    unpack_pickle_frame, and a decoded object that is not a dict raises
    RuntimeError. A missing frame_type is still a valid dict (bare dict for
    exec_started, tool_call, llm_request, worker_event, or non-heartbeat
    terminal responses).
    """
    data = unpack_pickle_frame(payload)
    if not isinstance(data, dict):
        raise RuntimeError("Worker response must be a dict")
    return data


ReadFailureKind = Literal["cancel", "read_timeout", "ipc"]


@dataclass(frozen=True)
class Terminal:
    response: dict[str, Any]


@dataclass(frozen=True)
class RetryWithNewLease:
    """Leave the current lease and take a new one (at most one retry per request)."""

    error: dict[str, Any]


@dataclass(frozen=True)
class NotStarted:
    error: BaseException  # replayable; child has not started this id


@dataclass(frozen=True)
class FailedAfterStart:
    error: BaseException
    kind: ReadFailureKind  # never replay


_RETIRED_MESSAGE = "Python worker was replaced by a new venv path and will not be restarted"


_SHARED_WORKER_RESTART_HINT = " Shared Python process restarted (all workbooks)."


def _worker_error_message(exc: BaseException) -> str:
    """Build a short user-facing worker error without subprocess command paths."""
    if isinstance(exc, subprocess.TimeoutExpired):
        return f"Python worker failed: timed out after {exc.timeout} seconds"
    text = str(exc)
    if text.startswith("Command ") and _TIMEOUT_AFTER in text:
        return f"Python worker failed:{text[text.index(_TIMEOUT_AFTER):]}"
    return f"Python worker failed: {text}"


def host_script_session_id(session: SessionKey | None, pin: str | None) -> str | None:
    """Document id for host tool RPC.

    Pin (chat ctx.doc) wins; else the worker namespace id; else None.
    The child does not choose this value, and a pin is not written into the
    request, so Isolated mode still gets a fresh namespace.
    """
    if pin:
        return pin
    return session.wire() if session is not None else None


def _maybe_dispatch_intermediate_response(
    response: dict[str, Any],
    *,
    stdin_write: Callable[[bytes], None],
    allowed_tools: frozenset[str] | None = None,
    caller: str = "script",
    on_worker_event: Callable[[dict[str, Any]], None] | None = None,
    cancel: SendCancellation | None = None,
    script_session_id: str | None = None,
) -> bool:
    """Handle tool_call (any build). ppt-master llm_request / worker_event only for that caller."""
    # Tool RPC is the shared venv→LO path (Run Python Script, chat python, ppt-master).
    # Handle it here so LibrePy / WriterAgent-without-ppt_master still round-trip.
    if handle_tool_call_frame(
        response,
        stdin_write=stdin_write,
        allowed_tools=allowed_tools,
        caller=caller,
        script_session_id=script_session_id,
        cancel=cancel,
    ):
        return True
    # Only the ppt-master worker gets llm_request and worker_event dispatched.
    # Every other caller (scripts, =PY()) returns False here, so those scripts
    # cannot reach the host's LLM credentials.
    if caller != "ppt_master_venv":
        return False
    try:
        from plugin.ppt_master.venv.host_rpc import dispatch_worker_response
    except ModuleNotFoundError as exc:
        # Only "the ppt_master bundle is absent" is not-an-error. A missing
        # dependency *inside* host_rpc (e.g. numpy) must stay visible.
        name = exc.name or ""
        if name != "plugin.ppt_master" and not name.startswith("plugin.ppt_master."):
            raise
        return False
    return dispatch_worker_response(
        response,
        stdin_write=stdin_write,
        on_worker_event=on_worker_event,
        cancel=cancel,
    )


_HARNESS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "venv", "worker_harness.py")

_SCOPE_UNIT_PREFIX = "writeragent-worker-"
# Probe results keyed by (systemd-run path, XDG_RUNTIME_DIR) of the spawn env:
# the systemd-run path when a scope works there, else "". With one global result,
# a manager whose env lacks XDG_RUNTIME_DIR (tests pass env={}) would reuse a
# probe from a full env and run systemd-run without a user bus, which exits.
_systemd_run_for_worker: dict[tuple[str, str], str] = {}
# Set once a scoped spawn raised OSError; every later spawn runs plainly.
_systemd_scope_disabled = False
_systemd_scope_lock = threading.Lock()


def _probe_systemd_scope(env: dict[str, str]) -> str:
    """Return the systemd-run path when a transient user scope can be created, else ""."""
    if not sys.platform.startswith("linux") or detect_sandbox() == "flatpak":
        return ""
    exe = shutil.which("systemd-run", path=env.get("PATH"))
    runtime_dir = env.get("XDG_RUNTIME_DIR", "")
    if not exe or not runtime_dir or not os.path.exists(os.path.join(runtime_dir, "systemd", "private")):
        return ""
    probe = [exe, "--user", "--scope", "--quiet", "--collect", "--unit=" + _SCOPE_UNIT_PREFIX + "probe-" + uuid.uuid4().hex[:12], "--", "true"]
    try:
        done = subprocess.run(probe, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        log.info("systemd-run scope probe failed, Python worker runs without its own scope: %s", exc)
        return ""
    if done.returncode != 0:
        log.info("systemd-run scope probe exited %s, Python worker runs without its own scope: %s", done.returncode, done.stderr.decode("utf-8", "replace").strip()[:300])
        return ""
    log.debug("Python worker will run in its own systemd user scope via %s", exe)
    return exe


def _systemd_run_path(env: dict[str, str]) -> str:
    """Probe once per (systemd-run, XDG_RUNTIME_DIR) pair; later calls reuse it."""
    if not env.get("XDG_RUNTIME_DIR"):
        return ""
    key = (shutil.which("systemd-run", path=env.get("PATH")) or "", env["XDG_RUNTIME_DIR"])
    with _systemd_scope_lock:
        if _systemd_scope_disabled:
            return ""
        if key not in _systemd_run_for_worker:
            _systemd_run_for_worker[key] = _probe_systemd_scope(env)
        return _systemd_run_for_worker[key]


def wrap_worker_command(cmd: list[str], *, env: dict[str, str] | None = None, cwd: str | None = None) -> list[str]:
    """Run the venv worker in its own transient systemd user scope when available.

    A memory-cgroup OOM in the worker then stops only that scope. In the shared
    app scope systemd's default OOMPolicy=stop also tears down soffice. The scope
    execs in place, so the Popen pid, pipes and kill() are the interpreter's own.
    """
    exe = _systemd_run_path(env if env is not None else dict(os.environ))
    if not exe:
        return wrap_command_for_sandbox(cmd, env=env, cwd=cwd, watch_bus=True)
    unit = _SCOPE_UNIT_PREFIX + str(os.getpid()) + "-" + uuid.uuid4().hex[:12]
    return [exe, "--user", "--scope", "--quiet", "--collect", "--unit=" + unit, "--"] + cmd

_instances: dict[str, PythonWorkerManager] = {}
_registry_lock = threading.Lock()


@dataclass
class _TurnState:
    """Replay guards for one request; survives an exception out of the read loop."""

    execution_started: bool = False
    dispatched_intermediate: bool = False

    @property
    def may_have_run(self) -> bool:
        return self.execution_started or self.dispatched_intermediate


def _classify_read_failure(exc: BaseException, state: _TurnState) -> NotStarted | FailedAfterStart:
    if isinstance(exc, (subprocess.TimeoutExpired, IpcPartialFrameTimeout)):
        return FailedAfterStart(exc, "read_timeout")
    if isinstance(exc, ValueError):
        return FailedAfterStart(exc, "ipc")
    if isinstance(exc, (OSError, RuntimeError)):
        if state.may_have_run:
            return FailedAfterStart(exc, "ipc")
        return NotStarted(exc)
    raise exc


@dataclass(frozen=True, kw_only=True)
class ExecRequest:
    """One host execute. ``wire()`` is the child pickle; host-only fields stay off it.

    ``request_id`` is minted once so a pre-start retry writes the same ``id``.
    """

    code: str | None = None
    data: Any = None
    bindings: dict[str, Any] | None = None
    timeout_sec: int
    session: SessionKey | None = None
    action: str | None = None
    init_script: str | None = None
    init_session: SessionKey | None = None
    init_script_hash: str | None = None
    allow_heartbeat: bool = False
    on_heartbeat: Callable[[dict[str, Any]], None] | None = None
    on_worker_event: Callable[[dict[str, Any]], None] | None = None
    cancel: SendCancellation | None = None
    python_tool_domain: str | None = None
    caller: str = "script"
    pin: str | None = None
    sandbox_policy: dict[str, Any] | None = None
    request_id: str = field(default_factory=lambda: str(uuid.uuid4()))

    def wire(self) -> dict[str, Any]:
        # Key insertion order is the pickle order. Do not sort, do not
        # dataclasses.asdict (it would copy host fields and deepcopy ``data``).
        request: dict[str, Any] = {"id": self.request_id, "timeout_sec": self.timeout_sec}
        if self.session is not None:
            request["session_id"] = self.session.wire()
        if self.data is not None:
            request["data"] = self.data
        # allow_heartbeat belongs on every long job, not only the code branch.
        # Without it a run_trusted_action job (embeddings, folder index) sends no
        # heartbeat and the host kills it at the read timeout.
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
            if self.init_session is not None:
                request["init_session_id"] = self.init_session.wire()
            if self.init_script_hash:
                request["init_script_hash"] = self.init_script_hash
            if self.sandbox_policy is not None:
                request["sandbox_policy"] = self.sandbox_policy
        return request


class PythonWorkerManager:
    """One warm child process per (pool, Python executable path) pair."""

    exe: str
    env: dict[str, str]
    pool: str = WORKER_POOL_DEFAULT
    _primed: bool
    _primed_generation: int
    _retired: bool
    _warm: WarmChild | None

    def __init__(self, exe: str, env: dict[str, str], *, pool: str = WORKER_POOL_DEFAULT) -> None:
        self.exe = exe
        self.env = dict(env)
        self.env["WRITERAGENT_IS_WORKER"] = "1"
        self.pool = pool
        self._warm = None
        # Same-thread owner -> WORKER_REENTRY; host callback in flight (tool RPC or on_heartbeat) -> busy.
        # Every caller waits up to _IO_LOCK_ACQUIRE_TIMEOUT_SEC.
        # _serving_tool_call is a plain GIL-atomic bool on this manager, written
        # as True only inside serving_callback. It is read without the pipe lock:
        # the holder keeps that lock across host RPC, so a waiter has to see the
        # flag without acquiring it.
        self._serving_tool_call: bool = False
        self._primed = False
        self._primed_generation = -1
        self._retired = False

    def _on_worker_death(self, death: Death) -> None:
        self._clear_host_state_after_worker_death()

    def _create_warm_child(self) -> WarmChild:
        spec = ChildSpec(
            name=f"venv:{self.pool}",
            argv=[self.exe, _HARNESS_PATH],
            env=dict(self.env),
            command_wrapper=wrap_worker_command,
            ready_timeout_sec=float(WARM_WORKER_TIMEOUT_SEC),
            max_payload_bytes=DEFAULT_MAX_PAYLOAD_BYTES,
            # log_start_failure=False: a missing interpreter must stay OSError (systemd retry depends on it).
            log_start_failure=False,
        )
        return WarmChild(spec, on_death=self._on_worker_death)

    def _get_warm(self) -> WarmChild | None:
        if self._retired:
            return None
        if self._warm is None:
            self._warm = self._create_warm_child()
        return self._warm

    @staticmethod
    def _lease_error_to_worker(err: dict[str, Any]) -> dict[str, Any]:
        code = err.get("code")
        if code == "CANCELLED":
            return _worker_error("CANCELLED", "Python worker stopped by user")
        if code == "PIPE_LOCK_BUSY":
            return _worker_error("WORKER_REENTRY", _WORKER_BUSY_MESSAGE)
        if code == "SERVICE_SHUTDOWN":
            return _worker_error("WORKER_IPC_ERROR", _RETIRED_MESSAGE)
        message = err.get("error") or "Python worker unavailable"
        return _worker_error(str(code or "WORKER_IPC_ERROR"), str(message))

    def _refuse_lease(self) -> dict[str, Any] | None:
        reentry = self._reentry_error()
        if reentry is not None:
            return reentry
        if self._retired:
            return _worker_error("WORKER_IPC_ERROR", _RETIRED_MESSAGE)
        return None

    @contextlib.contextmanager
    def serving_callback(self) -> Generator[None, None, None]:
        """Set _serving_tool_call for one host callback and clear it on the way out.

        _refuse_lease reads the flag without the pipe lock. A waiter that sees
        it refuses with busy, because the callback may need the waiting thread
        (see _reentry_error). This is the only writer after __init__; nothing else
        clears the flag. A raise still clears the flag; this method does not
        swallow it. One enter at a time: the holder refuses reentry before another
        dispatch, and a nested enter would clear the flag early.
        """
        self._serving_tool_call = True
        try:
            yield
        finally:
            self._serving_tool_call = False

    @contextlib.contextmanager
    def _io_session(
        self,
        cancel: SendCancellation | None = None,
        *,
        spawn_deadline: Deadline | None = None,
        lease_wait_sec: float | None = None,
    ) -> Generator[Lease | dict[str, Any], None, None]:
        """Hold the warm-child pipe lock, or yield an error dict."""
        lock_budget = lease_wait_sec if lease_wait_sec is not None else _IO_LOCK_ACQUIRE_TIMEOUT_SEC
        acquire_end = time.monotonic() + lock_budget
        spawn_deadline = spawn_deadline or Deadline(float(WARM_WORKER_TIMEOUT_SEC))
        while True:
            refused: dict[str, Any] | None = None

            # Identity compare distinguishes our refusal from a WarmChild error dict with the same code.
            def _take_refusal() -> dict[str, Any] | None:
                nonlocal refused
                refused = self._refuse_lease()
                return refused

            warm = self._get_warm()
            if warm is None:
                yield _worker_error("WORKER_IPC_ERROR", _RETIRED_MESSAGE)
                return
            wait = max(0.0, acquire_end - time.monotonic())
            lease_acquired = False
            try:
                with warm.lease(
                    spawn_deadline,
                    wait=wait,
                    cancel=cancel,
                    refuse=_take_refusal,
                    poll_sec=_IO_LOCK_POLL_SEC,
                ) as leased:
                    if isinstance(leased, dict):
                        if leased is refused:
                            yield leased
                            return
                        yield self._lease_error_to_worker(leased)
                        return
                    lease_acquired = True
                    if is_cancelled(cancel):
                        yield _worker_error("CANCELLED", "Python worker stopped by user")
                        return
                    warm_err = self._ensure_warmed_unlocked(leased)
                    if warm_err is not None:
                        yield warm_err
                        return
                    yield leased
                return
            except OSError as exc:
                if lease_acquired:
                    raise
                global _systemd_scope_disabled
                if _systemd_scope_disabled or not _systemd_run_path(self.env):
                    yield self._err("WORKER_SPAWN_FAILED", f"Failed to spawn Python worker: {exc}")
                    return
                log.warning("Python worker failed to start under systemd-run, retrying without a scope: %s", exc)
                with _systemd_scope_lock:
                    _systemd_scope_disabled = True
                if self._warm is not None:
                    self._warm.retire()
                self._warm = self._create_warm_child()
                continue

    def _run_in_session(
        self,
        body: Callable[[Any], dict[str, Any] | RetryWithNewLease],
        *,
        cancel: SendCancellation | None = None,
        timeout_sec: int,
        lease_wait_sec: float | None = None,
    ) -> dict[str, Any]:
        """Run *body* while this manager holds the pipe, or return the session error."""
        spawn_deadline = Deadline(float(timeout_sec))
        for attempt in range(2):
            with self._io_session(cancel, spawn_deadline=spawn_deadline, lease_wait_sec=lease_wait_sec) as session:
                if isinstance(session, dict):
                    return session
                outcome = body(session)
                if not isinstance(outcome, RetryWithNewLease):
                    return outcome
                if attempt == 1:
                    return outcome.error
        # Unreachable: attempt 1 always returns on RetryWithNewLease or a plain dict.
        raise AssertionError("retry loop exited without a result")

    @classmethod
    def get(cls, exe: str, env: dict[str, str], *, pool: str = WORKER_POOL_DEFAULT) -> PythonWorkerManager:
        """Return the singleton worker for *pool* + *exe* (caller should pass a scrubbed env dict)."""
        key = f"{pool}:{exe}"
        prefix = f"{pool}:"
        stale: list[PythonWorkerManager] = []
        with _registry_lock:
            # Replacing pool:exe evicts other exe keys under the same pool so a Settings path change does not leave orphan worker children.
            for other_key in list(_instances):
                if other_key.startswith(prefix) and other_key != key:
                    old = _instances.pop(other_key, None)
                    if old is not None:
                        stale.append(old)
            mgr = _instances.get(key)
            if mgr is None:
                mgr = cls(exe, dict(env), pool=pool)
                _instances[key] = mgr
            else:
                # The live process keeps the env it was spawned with. The next
                # Popen (crash, or a path change) must see a later scrub, including
                # WRITERAGENT_DEBUG_LOG_PATH once logging is up.
                fresh = dict(env)
                fresh["WRITERAGENT_IS_WORKER"] = "1"
                mgr.env = fresh
            for old in stale:
                # In-flight execute holds the warm pipe lock. Do not retire here
                # under _registry_lock: a tool call may be waiting on the UI thread.
                old._retired = True
        for old in stale:
            # Settings notifyTermination may retire several pool keys in a row.
            # Each idle retire() can block up to ~2s (wait + stderr join) on this thread.
            warm = old._warm
            if warm is not None:
                warm.retire()
            old._warm = None
        return mgr

    def _is_worker_alive(self) -> bool:
        warm = self._warm
        return warm is not None and warm.is_alive()

    def _ensure_warmed_unlocked(self, leased: Any) -> dict[str, Any] | None:
        """Prime auto-imports once per warm-child generation. Returns error dict or None."""
        if self._primed and self._primed_generation == leased.generation and self._warm is not None and self._warm.is_alive():
            return None
        prime = self._execute_ipc_unlocked(
            ExecRequest(code="result = None", timeout_sec=WARM_WORKER_TIMEOUT_SEC),
            leased=leased,
        )
        if isinstance(prime, RetryWithNewLease):
            return prime.error
        if prime.get("status") != "ok":
            return prime
        self._primed = True
        self._primed_generation = leased.generation
        return None

    def _reentry_error(self) -> dict[str, Any] | None:
        """Refuse a nested execute that would deadlock this worker's pipe."""
        me = threading.get_ident()
        warm = self._warm
        # The holder can be inside a host callback (tool RPC or on_heartbeat) that
        # is waiting on the thread stuck in acquire (the UI pump, or whichever
        # worker must run the host callback), so the wait never ends. Refuse the
        # owner (true reentry), or anyone while a host callback is in progress.
        # Every other caller waits.
        # _serving_tool_call is per manager. =PY() during a chat script is a
        # different thread, not a callback into the same worker. The reentry
        # sentence is only true when this thread already holds the pipe.
        # Child._io_owner is a plain GIL-atomic int on the child, read here
        # without the pipe lock: the holder keeps that lock across host RPC.
        if warm is not None and warm.io_owner() == me:
            return _worker_error("WORKER_REENTRY", _WORKER_REENTRY_MESSAGE)
        if self._serving_tool_call:
            return _worker_error("WORKER_REENTRY", _WORKER_BUSY_MESSAGE)
        return None

    def warm(self) -> None:
        """Spawn the worker and trigger auto-imports (numpy etc.) so the next real execute is instant."""
        with self._io_session() as session:
            if isinstance(session, dict):
                log.warning("Python worker warm failed: %s", session.get("message"))

    def _clear_host_state_after_worker_death(self) -> None:
        """IPC is desynced after a kill; only the default pool feeds the Calc cache."""
        if self.pool != WORKER_POOL_DEFAULT:
            return
        # function.py imports run_code_in_user_venv from this module at load.
        from plugin.calc.python.function import clear_python_addin_cache

        clear_python_addin_cache()

    def _err(self, code: str, message: str, **extra: Any) -> dict[str, Any]:
        return _worker_error(code, message, details={"exe": self.exe, **extra})

    def _fail_no_replay(self, code: str, message: str, *, details: dict[str, Any] | None = None) -> dict[str, Any]:
        """Kill the desynced worker and return an error without resending the request."""
        self._terminate_worker()
        self._clear_host_state_after_worker_death()
        return self._err(code, message, **(details or {}))

    def _retry_after_kill(self, code: str, exc: BaseException) -> RetryWithNewLease:
        """Kill the desynced worker and ask _run_in_session for one more attempt on a new lease.

        Only for failures before the child can have started this request id, so the
        resend on the fresh child runs the script once.
        """
        self._terminate_worker()
        self._clear_host_state_after_worker_death()
        return RetryWithNewLease(self._err(code, _worker_error_message(exc), attempt=2))

    def _fail_ipc_no_replay(self, exc: BaseException) -> dict[str, Any]:
        """Terminal IPC failure after the request may have run. Same text at each site."""
        return self._fail_no_replay("WORKER_IPC_ERROR", f"Python worker failed: {exc}{_SHARED_WORKER_RESTART_HINT}")

    def _read_until_terminal(
        self,
        stdout: IO[bytes],
        stdin: IO[bytes],
        req: ExecRequest,
        wire: dict[str, Any],
        state: _TurnState,
        *,
        host_read_timeout_sec: float,
        write_timeout_sec: float,
        allowed_tools: frozenset[str] | None,
        resolved_script_session_id: str | None,
    ) -> Terminal | NotStarted | FailedAfterStart:
        """Serve intermediate frames until a terminal one; flags go into *state*.

        *state* is the caller's object so its except handlers still see
        exec_started / tool_call when this raises.
        """

        def _stdin_write(blob: bytes) -> None:
            state.dispatched_intermediate = True
            try:
                self._write_packed_frame_with_timeout(
                    stdin,
                    blob,
                    timeout_sec=write_timeout_sec,
                    label="host RPC response",
                )
            except subprocess.TimeoutExpired as exc:
                # The worker requested host work before this write. Retrying the
                # whole turn could duplicate UNO mutations already performed, so
                # the read loop's no-replay outcome kills the worker.
                raise RuntimeError(
                    f"host RPC response timed out after {write_timeout_sec:g} seconds"
                ) from exc

        def _closed_without_response() -> NotStarted | FailedAfterStart:
            stderr_out = self._drain_stderr()
            message = f"Worker closed stdout without a response{stderr_out}"
            if state.may_have_run:
                # Work may already have run. Resending this id would
                # run it again. A close before exec_started is a
                # failed start and falls through to the retry.
                return FailedAfterStart(RuntimeError(message), "ipc")
            return NotStarted(RuntimeError(message))

        try:
            while True:
                if req.allow_heartbeat:
                    # Already unpickled, so this branch skips parse_worker_stdout_frame:
                    # a second unpack of a multi-megabyte result frame is pure CPU.
                    response = self._read_response_with_heartbeats(
                        stdout,
                        host_read_timeout_sec,
                        EMBEDDINGS_HEARTBEAT_GRACE_S,
                        req.on_heartbeat,
                        cancel=req.cancel,
                    )
                    if isinstance(response, FailedAfterStart):
                        return response
                    if response is None:
                        return _closed_without_response()
                else:
                    response_bytes = self._read_response_bytes(
                        stdout, host_read_timeout_sec, cancel=req.cancel
                    )
                    if isinstance(response_bytes, FailedAfterStart):
                        return response_bytes
                    if not response_bytes:
                        return _closed_without_response()
                    response = parse_worker_stdout_frame(response_bytes)
                if response.get("type") == EXEC_STARTED:
                    state.execution_started = True
                    if response.get("id") != wire.get("id"):
                        # Hand the mismatched frame back; the caller's id check
                        # refuses it without replaying the request.
                        return Terminal(response)
                    continue

                # A tool_call (or ppt llm_request / worker_event) can change the document
                # before exec_started arrives. Mark the turn as may_have_run before dispatch,
                # so a tool that mutates and then raises is not replayed.
                if response.get("type") in ("tool_call", "llm_request", "worker_event"):
                    state.dispatched_intermediate = True
                with self.serving_callback():
                    is_intermediate = _maybe_dispatch_intermediate_response(
                        response,
                        stdin_write=_stdin_write,
                        allowed_tools=allowed_tools,
                        caller=req.caller,
                        on_worker_event=req.on_worker_event,
                        cancel=req.cancel,
                        script_session_id=resolved_script_session_id,
                    )
                if is_intermediate:
                    state.dispatched_intermediate = True
                    continue
                return Terminal(response)
        except (subprocess.TimeoutExpired, IpcPartialFrameTimeout, ValueError, OSError, RuntimeError) as exc:
            return _classify_read_failure(exc, state)

    def _execute_ipc_unlocked(self, req: ExecRequest, *, leased: Lease) -> dict[str, Any] | RetryWithNewLease:
        wire = req.wire()
        timeout_sec = req.timeout_sec
        stdin = leased.stdin
        stdout = leased.stdout
        # Host-only preparation. A failure here must not reach the worker-kill
        # handlers below: no request byte has been written yet.
        try:
            allowed_tools = resolve_allowed_tools(req.python_tool_domain)
            # Pin wins over the kernel session id. Chat passes doc:… and
            # leaves request session_id unset. RPS / =PY() / PPT-Master
            # omit the pin and keep the request id.
            resolved_script_session_id = host_script_session_id(req.session, req.pin)
        except Exception as exc:
            log.warning("Python worker request preparation failed: %s", exc)
            return self._err("WORKER_IPC_ERROR", f"Failed to prepare request: {exc}")
        try:
            if self._retired:
                log.warning("Python worker retired: %s", _RETIRED_MESSAGE)
                return self._err("WORKER_IPC_ERROR", _RETIRED_MESSAGE)
            write_timeout_sec = min(float(timeout_sec), float(VENV_IPC_WRITE_TIMEOUT_SEC))
            # The host read timeout includes a grace buffer so the child's in-process
            # signal/thread timeout fires first and returns a clean error frame without
            # terminating the warm subprocess (preserving shared workbook sessions).
            host_read_timeout_sec = float(timeout_sec) + float(HOST_IPC_READ_GRACE_SEC)
            try:
                self._write_frame_with_timeout(stdin, wire, timeout_sec=write_timeout_sec, label="request")
            except IpcFrameError as e:
                # An oversize request fails in pack_pickle_frame before any byte
                # reaches the pipe. Return here: the retry handler below would kill
                # the warm worker (once per attempt) and wipe every shared session
                # for a request the worker never saw.
                log.warning("Python worker request not sent: %s", e)
                return self._err("WORKER_IPC_ERROR", f"Failed to serialize request: {e}")
        except (subprocess.TimeoutExpired, ValueError, RuntimeError, OSError) as e:
            # The child has not started this id: the write failed or the pipe broke. A timeout can
            # leave a partial frame in the pipe, so the child is killed and the same id goes once to a fresh child.
            log.warning("Python worker failed on initial write: %s", e)
            code = "VENV_TIMEOUT" if isinstance(e, subprocess.TimeoutExpired) else "WORKER_IPC_ERROR"
            return self._retry_after_kill(code, e)

        state = _TurnState()
        outcome = self._read_until_terminal(
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

        if isinstance(outcome, NotStarted):
            # Nothing ran (no exec_started, no tool frame), so a resend runs the script once.
            log.warning("Python worker failed before start: %s", outcome.error)
            return self._retry_after_kill("WORKER_IPC_ERROR", outcome.error)

        if isinstance(outcome, FailedAfterStart):
            if outcome.kind == "cancel":
                log.info("Python worker stopped by user")
                return self._fail_no_replay("CANCELLED", "Python worker stopped by user")
            if outcome.kind == "read_timeout":
                log.warning("Python worker read timed out: %s", outcome.error)
                return self._fail_no_replay(
                    "VENV_TIMEOUT",
                    _worker_error_message(outcome.error) + _SHARED_WORKER_RESTART_HINT,
                    details={"timeout_sec": timeout_sec},
                )
            # kind "ipc": a rejected frame, a pipe error after the script may have run, or stdout
            # closed after start. Resending this id could run the script, or a tool it called, twice.
            log.warning("Python worker IPC failed (not replaying): %s", outcome.error)
            return self._fail_ipc_no_replay(outcome.error)

        # Only Terminal is left: the two failure outcomes returned above.
        response = outcome.response
        # Empty-id error before exec: the harness rejected the pickle and is
        # back at read(). Return its message and keep the worker. After
        # exec_started or a tool frame the same reply fails the id check below,
        # which restarts the worker.
        if (
            response.get("id") == ""
            and response.get("status") == "error"
            and not state.may_have_run
        ):
            message = response.get("message")
            text = message if isinstance(message, str) and message else "Invalid pickle request"
            log.warning("Python worker rejected the request frame (worker kept): %s", text)
            return self._err("WORKER_IPC_ERROR", text)
        req_id = wire.get("id")
        if response.get("id") != req_id:
            # A terminal frame must carry this request's id, or one cell would
            # consume another cell's result. A mismatch means the pipe is out of
            # step: kill the worker and do not replay. (Tool frames are checked by
            # exchange_tool_call in the child.)
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
            # host_unpack_data runs after the script has finished, so a rejected
            # result fails without replay: replaying would run a tool that already
            # changed the document a second time. HOST_UNPACK_ERRORS is broad because
            # bad shapes and non-finite ints raise different types.
            return self._normalize_response(response)
        except HOST_UNPACK_ERRORS as e:
            log.warning("Python worker result rejected (not replaying): %s", e)
            return self._fail_ipc_no_replay(e)

    def execute(self, request: ExecRequest, *, lease_wait_sec: float | None = None) -> dict[str, Any]:
        """Run *request* in the warm worker.

        Cold start under :data:`WARM_WORKER_TIMEOUT_SEC` is not charged
        against ``request.timeout_sec``; ``request.pin`` is host-only.
        """
        return self._run_in_session(
            lambda leased: self._execute_ipc_unlocked(request, leased=leased),
            cancel=request.cancel,
            timeout_sec=request.timeout_sec,
            lease_wait_sec=lease_wait_sec,
        )

    def execute_ppt_master_turn(
        self,
        payload: dict[str, Any],
        *,
        timeout_sec: int,
        on_worker_event: Callable[[dict[str, Any]], None] | None = None,
        cancel: SendCancellation | None = None,
    ) -> dict[str, Any]:
        """Run one PPT-Master sidebar turn in the venv worker (LLM + scripts + host UNO RPC).

        ``cancel`` is host-side only. ``data`` is pickled to the child,
        so the scope is passed beside it and attached when an llm_request frame
        comes back; it is not on ``wire()``.
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
        request = ExecRequest(
            data=payload,
            timeout_sec=timeout_sec,
            action="ppt_master_turn",
            session=SessionKey.from_host(session_id),
            on_worker_event=on_worker_event,
            cancel=cancel,
            caller="ppt_master_venv",
        )
        raw = self.execute(request)
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
        """Write a packed frame within *timeout_sec*.

        `TimeoutExpired` can leave a partial frame in the pipe; the caller kills
        the child (`_retry_after_kill` for the request frame, `_fail_no_replay`
        through the read loop for a host RPC response). A dead child raises
        `BrokenPipeError` from `is_alive`.
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
            raise subprocess.TimeoutExpired(cmd=self.exe, timeout=timeout_sec) from exc

    def _normalize_response(self, response: dict[str, Any]) -> dict[str, Any]:
        if response.get("status") == "ok":
            result = response.get("result")
            if result is not None:
                result = host_unpack_data(result)
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

    def _read_response_bytes(
        self,
        stdout: IO[bytes],
        timeout_sec: float | int,
        cancel: SendCancellation | None = None,
    ) -> bytes | FailedAfterStart:
        if not self._is_worker_alive():
            raise RuntimeError("worker terminated concurrently")
        return self._read_frame_bytes(stdout, timeout_sec, cancel=cancel)

    def _read_frame_bytes(
        self,
        stdout: IO[bytes],
        timeout_sec: float | int,
        cancel: SendCancellation | None = None,
        *,
        deadline: float | None = None,
    ) -> bytes | FailedAfterStart:
        """Read one frame payload via ipc, returning FailedAfterStart on Stop.

        ipc owns the wait; this method maps Stop to FailedAfterStart, passes
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
                cancel=cancel,
            )
        except (subprocess.TimeoutExpired, IpcPartialFrameTimeout):
            # ipc reports Stop as TimeoutExpired, including a mid-chunk stall
            # (IpcPartialFrameTimeout). Re-check cancel so Windows peek and
            # the POSIX loop both become CANCELLED.
            if is_cancelled(cancel):
                return FailedAfterStart(RuntimeError("Python worker stopped by user"), "cancel")
            raise
        return payload or b""

    def _read_response_with_heartbeats(
        self,
        stdout: IO[bytes],
        timeout_sec: float | int,
        grace_sec: float,
        on_heartbeat: Callable[[dict[str, Any]], None] | None,
        cancel: SendCancellation | None = None,
    ) -> dict[str, Any] | None | FailedAfterStart:
        # Heartbeats extend the wait only up to an absolute ceiling so endless heartbeat loops cannot hold the warm-child pipe lock forever.
        # _read_until_terminal starts a new ceiling after a non-heartbeat
        # frame. Trusted actions emit heartbeats until one result, so for
        # them this call is the whole job.
        cap = float(HEARTBEAT_ABSOLUTE_CAP_SEC)
        start = time.monotonic()
        ceiling = start + cap
        # First-byte wait is timeout_sec, not max(timeout, grace). Grace is only
        # the silence allowed after a heartbeat; taking the max would make a short
        # timeout wait for the full grace. After a heartbeat:
        # deadline = min(now + grace, ceiling).
        grace = float(grace_sec)
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
                stdout, timeout_label, cancel=cancel, deadline=deadline
            )
            if isinstance(frame_bytes, FailedAfterStart):
                return frame_bytes
            if not frame_bytes:
                return None
            data = parse_worker_stdout_frame(frame_bytes)
            frame_type = data.get("frame_type")
            if frame_type == FRAME_HEARTBEAT:
                payload = data.get("payload")
                if on_heartbeat is not None and isinstance(payload, dict):
                    with self.serving_callback():
                        try:
                            on_heartbeat(payload)
                        except Exception:
                            log.exception("Heartbeat callback failed (ignoring)")
                extended = time.monotonic() + grace
                if extended > ceiling:
                    deadline = ceiling
                    timeout_label = cap
                else:
                    deadline = extended
                    timeout_label = grace
                continue
            if frame_type == FRAME_RESULT or frame_type is None:
                return data
            # Unknown frames must not extend the deadline (protocol drift should
            # time out, not look like a stall). Log only the first unknown type, so
            # a child repeating it does not slow tight timeout tests with warnings.
            if not logged_unknown:
                log.warning("venv worker ignoring unknown frame type: %r", frame_type)
                logged_unknown = True

    def _drain_stderr(self) -> str:
        """Return bounded stderr captured by the live warm child (crash diagnostics)."""
        warm = self._warm
        if warm is None:
            return ""
        tail = warm.diagnostic_tail().strip()
        return f"\nWorker stderr:\n{tail}" if tail else ""

    def _terminate_worker(self) -> None:
        """Kill the warm child and forget priming."""
        self._primed = False
        self._primed_generation = -1
        warm = self._warm
        if warm is None:
            return
        warm.kill()


def shutdown_venv_workers_for_tests() -> None:
    """Retire venv managers and their warm children (pytest teardown)."""
    with _registry_lock:
        managers = list(_instances.values())
        for mgr in managers:
            mgr._retired = True
            warm = mgr._warm
            if warm is not None:
                try:
                    warm.retire()
                except Exception:
                    log.exception("Error retiring venv warm child in test teardown")
                mgr._warm = None
        _instances.clear()


# --- Public entrypoints ---


def _resolve_worker_python(
    *,
    pool: str = WORKER_POOL_DEFAULT,
) -> tuple[str | None, dict[str, Any] | None]:
    """Return (exe, error_response) for the configured venv / LO interpreter.

    Config is process-global (``get_config``). Callers that still have a
    UNO context pass it to ``run_code_in_user_venv``, not here.
    """
    venv_dir = str(get_config("scripting.python_venv_path") or "").strip()
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
            prefix = "Embeddings venv not configured or invalid" if embeddings else "No python executable found under configured venv"
            return None, _worker_error("VENV_NOT_FOUND", f"{prefix}: {venv_dir!r}")
        which = "embeddings venv" if embeddings else "venv"
        log.debug("run_venv_code: using %s interpreter under %s", which, venv_dir)
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
        return None, err
    child_env = scrub_subprocess_env(dict(os.environ))
    return PythonWorkerManager.get(exe, child_env, pool=pool), None


def run_code_in_user_venv(
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
    on_heartbeat: Callable[[dict[str, Any]], None] | None = None,
    action: str | None = None,
    cancel: SendCancellation | None = None,
) -> dict[str, Any]:
    """Execute *code* or handle *action* via :class:`PythonWorkerManager` (warm process).

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
        assert err is not None
        return err

    timeout = python_exec_timeout(timeout_sec)

    sandbox_policy = None
    if not action:
        allow_network = bool(get_config("scripting.python_allow_network"))
        allow_files = bool(get_config("scripting.python_allow_files"))
        allow_os = bool(get_config("scripting.python_allow_os"))
        extra_modules = get_config("scripting.python_extra_modules")
        sandbox_policy = {
            "network": allow_network,
            "files": allow_files or allow_os,
            "os": allow_os,
            "extra_modules": extra_modules,
        }

    return manager.execute(
        ExecRequest(
            code=code,
            data=data,
            bindings=bindings,
            timeout_sec=timeout,
            session=SessionKey.from_host(session_id),
            action=action,
            init_script=init_script,
            init_session=SessionKey.from_host(init_session_id),
            init_script_hash=init_script_hash,
            allow_heartbeat=allow_heartbeat,
            on_heartbeat=on_heartbeat,
            python_tool_domain=python_tool_domain,
            pin=script_session_id,
            cancel=cancel,
            sandbox_policy=sandbox_policy,
        )
    )


def reset_python_session(
    session_id: str,
    *,
    timeout_sec: int | None = None,
    lease_wait_sec: float | None = None,
) -> dict[str, Any]:
    """Drop the shared-kernel executor for *session_id* in the warm worker.

    A missing or exited child returns {"status": "ok"} and does not spawn.
    """
    if not (session_id or "").strip():
        return _worker_error("WORKER_IPC_ERROR", "No session_id provided.")

    manager, err = _get_worker_manager()
    if manager is None:
        assert err is not None
        return err

    # When no worker child is alive, return ok without execute() so Calc OnUnload does not spawn on the UNO thread and deadlock on SolarMutex during document teardown.
    if not manager._is_worker_alive():
        return {"status": "ok"}

    return manager.execute(
        ExecRequest(
            timeout_sec=python_exec_timeout(timeout_sec),
            session=SessionKey.from_host(session_id),
            action="reset_session",
        ),
        lease_wait_sec=lease_wait_sec,
    )


@background
def warm_venv_worker(pool: str = WORKER_POOL_DEFAULT) -> None:
    """Pre-warm a specific venv subprocess pool (spawn + trigger auto-imports + load embedding model if embeddings pool). Safe to call from a background thread."""
    manager, err = _get_worker_manager(pool=pool)
    if manager is None:
        log.warning("warm_venv_worker skipped for pool %s: %s", pool, (err or {}).get("message"))
        return
    manager.warm()

    # Pre-load the active embedding model inside the embeddings pool worker so first query executes instantly
    if pool == WORKER_POOL_EMBEDDINGS:
        try:
            # Lazy: the embeddings package is not in the LibrePy bundle.
            from plugin.embeddings.embedding_client import get_embedding_model

            model = get_embedding_model()
            if model:
                timeout_val = embeddings_worker_timeout_sec()
                res = manager.execute(
                    ExecRequest(
                        action="run_trusted_action",
                        data={
                            "domain": "embeddings_index",
                            "helper": "warm_embedder",
                            "params": {"model": model},
                        },
                        timeout_sec=timeout_val,
                        allow_heartbeat=True,
                    )
                )
                if res.get("status") != "ok":
                    log.warning("Embedding model pre-warm returned status %s: %s", res.get("status"), res.get("message"))
        except Exception:
            log.exception("Failed to warm embedding model")


__all__ = [
    "ExecRequest",
    "PythonWorkerManager",
    "parse_worker_stdout_frame",
    "reset_python_session",
    "run_code_in_user_venv",
    "warm_venv_worker",
]
