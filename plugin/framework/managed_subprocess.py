# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared managed-subprocess bottom layer.

Provides explicit start, environment merging, thread-guard refusal, bounded stderr,
tree kill, reaping, and registered shutdown. Stderr can be subprocess.DEVNULL (no drain),
and registry membership and start-error wrapping are optional. `raw_spawn=True` is a pass-through
Popen for short-lived children, and ManagedSubprocess.run is a pass-through subprocess.run.
Worker start, stderr drain, tree kill, and shutdown_all stay the default.
When `stderr_name_with_pid` is true and `stderr_name` is set, the drain thread name is `{stderr_name}-{pid}`.
"""

from __future__ import annotations

import logging
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from typing import IO, Any, Callable, cast

from plugin.framework.worker_pool import (
    StderrTail,
    get_subprocess_creationflags,
    start_stderr_drain,
    run_in_background,
)
from plugin.scripting.sandbox import optimize_popen_pipes, scrub_subprocess_env

log = logging.getLogger(__name__)

# Wait timeout before reaping an orphaned process.
_GRACE_KILL_TIMEOUT_SEC = 3.0
_REAP_CLOSE_WAIT_SEC = 5.0
_REAP_WAIT_SEC = 1.0
_REAPER_MAX_PASSES = 5
_REAPER_RETRY_PAUSE_SEC = 0.1
_RealPopen = subprocess.Popen


class ManagedSubprocess:
    """A managed child process providing explicit start, safe shutdown, and stderr tailing.

    The default constructor is the worker (pipes, drain, registry, close()).
    raw_spawn=True is the TTS path and does not do those things. Callers of raw mode
    own stop and reap. run does not create an instance.
    Callers own the stdout and stdin pipes (e.g. for JSON-RPC or custom framing).
    This layer handles environment merging, process-tree killing, and cleanup.
    These knobs default to the worker spawn (binary pipes, explicit merged env,
    POSIX start_new_session, stderr pipe + drain, registry, wrapped start errors,
    stderr name unchanged); `text=True` is line-buffered text pipes.
    `drain_stderr`, `register`, and `log_start_failure` default to the worker spawn
    (stderr drain, `shutdown_all` registry, start failures logged and wrapped in `RuntimeError`).
    """

    _cmd_line: list[str]
    _cwd: str | None
    _env: dict[str, str] | None
    _base_env: dict[str, str] | None
    _command_wrapper: Callable[..., list[str]] | None
    _thread_guard: Callable[[], None] | None
    _scrub_env: bool
    _optimize_pipes: bool
    _stderr_tail_chars: int | None
    _stderr_name: str | None
    _label: str
    _drain_stderr: bool
    _inherit_env: bool
    _new_session: bool
    _stderr: int
    _register: bool
    _log_start_failure: bool
    _text: bool
    _stderr_name_with_pid: bool
    _raw_spawn: bool
    _raw_stdin: Any
    _raw_stdout: Any
    _raw_stderr: Any
    _raw_text: bool
    _raw_encoding: str | None
    _raw_errors: str | None
    _proc: Any | None
    _stderr_drain: StderrTail | None
    _reaper_passes: int
    _stdio_closed: bool
    _lock: threading.Lock
    _closed: bool

    def __init__(
        self,
        cmd_line: list[str],
        *,
        env: dict[str, str] | None = None,
        base_env: dict[str, str] | None = None,
        cwd: str | None = None,
        command_wrapper: Callable[..., list[str]] | None = None,
        thread_guard: Callable[[], None] | None = None,
        scrub_env: bool = False,
        optimize_pipes: bool = False,
        stderr_tail_chars: int | None = None,
        stderr_name: str | None = None,
        stderr_name_with_pid: bool = False,
        label: str | None = None,
        drain_stderr: bool = True,
        inherit_env: bool = False,
        new_session: bool = True,
        stderr: Any = None,
        text: bool = False,
        register: bool = True,
        log_start_failure: bool = True,
        raw_spawn: bool = False,
        stdin: Any = None,
        stdout: Any = None,
        encoding: str | None = None,
        errors: str | None = None,
    ) -> None:
        if inherit_env and (env is not None or base_env is not None or scrub_env):
            raise ValueError("inherit_env cannot be combined with env, base_env, or scrub_env")
        if not drain_stderr and (stderr_tail_chars is not None or stderr_name is not None):
            raise ValueError(
                "drain_stderr=False does not take stderr_tail_chars or stderr_name"
            )
        if not raw_spawn:
            if stderr is None:
                worker_stderr = subprocess.PIPE
            elif stderr == subprocess.PIPE or stderr == subprocess.DEVNULL:
                worker_stderr = stderr
            else:
                raise ValueError("stderr must be subprocess.PIPE or subprocess.DEVNULL")
            self._stderr = worker_stderr
        else:
            self._stderr = subprocess.PIPE
        self._cmd_line = cmd_line
        self._cwd = cwd
        self._env = env
        self._base_env = base_env
        self._command_wrapper = command_wrapper
        self._thread_guard = thread_guard
        self._scrub_env = scrub_env
        self._optimize_pipes = optimize_pipes
        self._stderr_tail_chars = stderr_tail_chars
        self._stderr_name = stderr_name
        self._label = label if label else "Managed subprocess"
        self._drain_stderr = drain_stderr
        self._inherit_env = inherit_env
        self._new_session = new_session
        self._text = text
        self._register = register
        self._log_start_failure = log_start_failure
        self._stderr_name_with_pid = stderr_name_with_pid
        self._raw_spawn = raw_spawn
        self._raw_stdin = stdin
        self._raw_stdout = stdout
        self._raw_stderr = stderr
        self._raw_text = text
        self._raw_encoding = encoding
        self._raw_errors = errors
        self._reaper_passes = 0
        self._stdio_closed = False
        self._proc = None
        self._stderr_drain = None
        self._lock = threading.Lock()
        self._closed = False

    @property
    def popen(self) -> subprocess.Popen[bytes] | None:
        """Return the underlying subprocess.Popen instance or None if not started."""
        return cast("subprocess.Popen[bytes] | None", self._proc)

    @property
    def pid(self) -> Any:
        """``getattr(self._proc, "pid", None)`` so logs can print a double's pid. Not a kill guard."""
        return getattr(self._proc, "pid", None)

    @classmethod
    def wrap_existing(
        cls,
        proc: Any,
        *,
        label: str,
        stderr_tail: StderrTail | None = None,
    ) -> ManagedSubprocess:
        """Attach a process this object did not spawn. Does not Popen and does not register.

        ``start()`` then raises single-use because ``_proc`` is already set.
        Test doubles live here. ``kill_tree`` will not call ``kill_process_tree``
        unless ``proc`` is a real ``subprocess.Popen``. An integer pid on a double
        would be ``killpg`` of a live process group.
        """
        inst = cls(["managed-wrap"], label=label)
        inst._proc = proc
        inst._stderr_drain = stderr_tail
        return inst

    def kill_tree(self) -> None:
        """Immediate SIGKILL of the process group. No pipe close, no wait, no unregister.

        A double is not a Popen because kill_process_tree treats an int pid above 1 as a process group.
        close() is a different method because it waits out SIGTERM and closes stdout, which the compute
        io-owner forbids on the non-owner thread.
        """
        proc = self._proc
        if proc is None:
            return
        try:
            if isinstance(proc, _RealPopen):
                kill_process_tree(proc)
            else:
                poll_fn = getattr(proc, "poll", None)
                poll_val = poll_fn() if callable(poll_fn) else None
                if poll_val is None:
                    kill_fn = getattr(proc, "kill", None)
                    if callable(kill_fn):
                        kill_fn()
        except Exception:
            log.debug("kill_tree failed for %s pid=%s", self._label, self.pid, exc_info=True)

    def close_stdio(self) -> None:
        """Close stdin and stdout once. Leave stderr to the drain thread. A second call returns."""
        if self._stdio_closed:
            return
        self._stdio_closed = True
        proc = self._proc
        if proc is None:
            return
        for stream in (getattr(proc, "stdin", None), getattr(proc, "stdout", None)):
            if stream is None:
                continue
            try:
                stream.close()
            except Exception:
                log.debug("%s could not close a pipe", self._label, exc_info=True)

    def wait_once(self) -> bool:
        """``wait(_REAP_WAIT_SEC)`` even when ``poll`` has already reaped. True when ``poll()`` is not None.

        ``None`` proc returns True. Exceptions from ``wait`` or ``poll`` are debug-logged
        and a failed ``poll`` returns False. No timeout argument.
        """
        proc = self._proc
        if proc is None:
            return True
        try:
            proc.wait(timeout=_REAP_WAIT_SEC)
        except Exception:
            log.debug("%s wait failed", self._label, exc_info=True)
        try:
            return proc.poll() is not None
        except Exception:
            log.debug("%s poll failed", self._label, exc_info=True)
            return False

    def join_stderr(self, timeout: float = 1.0) -> str:
        """``finish_text`` when a drain exists. On exception, debug-log and return ``text()``. No drain returns ``""``."""
        drain = self._stderr_drain
        if drain is None:
            return ""
        try:
            return drain.finish_text(timeout=timeout)
        except Exception:
            log.debug("%s could not join stderr drain", self._label, exc_info=True)
            try:
                return drain.text()
            except Exception:
                return ""

    def stderr_text(self) -> str:
        """``text()`` with no join. No drain or an exception returns ``""``."""
        drain = self._stderr_drain
        if drain is None:
            return ""
        try:
            return drain.text()
        except Exception:
            return ""

    def abandon(self) -> None:
        """Enqueue ``("abandon", self)``. Does not close pipes."""
        _enqueue_reaper(("abandon", self))

    def reaper_pass(self) -> None:
        """One abandon pass. See binding decision 9. Not used by ``close()``."""
        proc = self._proc
        if proc is None:
            return
        try:
            if proc.poll() is not None:
                self.join_stderr(1.0)
                self.unregister()
                return
        except Exception:
            log.debug("%s abandoned pid poll failed", self._label, exc_info=True)
            return
        try:
            proc.wait(timeout=_REAP_WAIT_SEC)
        except Exception:
            log.debug("%s abandoned pid wait failed", self._label, exc_info=True)
        try:
            if proc.poll() is not None:
                self.join_stderr(1.0)
                self.unregister()
                return
        except Exception:
            log.debug("%s abandoned pid poll failed", self._label, exc_info=True)
            return
        self._reaper_passes += 1
        if self._reaper_passes >= _REAPER_MAX_PASSES:
            log.warning("%s pid=%s still alive; reaper giving up", self._label, self.pid)
            return
        time.sleep(_REAPER_RETRY_PAUSE_SEC)
        _enqueue_reaper(("abandon", self))

    @property
    def stdin(self) -> IO[bytes] | None:
        if self._proc:
            return self._proc.stdin
        return None

    @property
    def stdout(self) -> IO[bytes] | None:
        if self._proc:
            return self._proc.stdout
        return None

    @property
    def stderr_tail(self) -> StderrTail | None:
        return self._stderr_drain

    def is_alive(self) -> bool:
        """Return True if the subprocess is currently running."""
        return self._proc is not None and self._proc.poll() is None

    def start(self) -> None:
        """Spawn the child process.

        Worker path is unchanged (pipes, drain, registry). In raw_spawn mode,
        Popen runs with requested stdio and environment; OSError propagates.
        A second start raises RuntimeError.
        """
        with self._lock:
            # Single-use policy: a ManagedSubprocess cannot be respawned or restarted after exit/close.
            if self._closed or self._proc is not None:
                raise RuntimeError("ManagedSubprocess is single-use")

            if self._raw_spawn:
                self._start_raw_locked()
                return

            if self._thread_guard:
                self._thread_guard()

            if self._inherit_env:
                merged_env = None
            else:
                # Environment merge:
                # 1. merged = dict(os.environ) when base_env is None, else merged = dict(base_env).
                # 2. If scrub_env is true, merged = scrub_subprocess_env(merged).
                # 3. If self._env is non-empty, merged.update(self._env).
                if self._base_env is None:
                    merged_env = dict(os.environ)
                else:
                    merged_env = dict(self._base_env)
                if self._scrub_env:
                    merged_env = scrub_subprocess_env(merged_env)
                if self._env:
                    merged_env.update(self._env)

            popen_kw = get_subprocess_creationflags()
            if self._new_session and sys.platform != "win32":
                popen_kw["start_new_session"] = True

            cmd = (
                self._command_wrapper(self._cmd_line, env=merged_env, cwd=self._cwd)
                if self._command_wrapper is not None
                else list(self._cmd_line)
            )

            # What was wrong: if start_stderr_drain (or any step after Popen) raised,
            # self._proc was left assigned without being registered in _instances,
            # leaking the spawned process without a tree kill or cleanup.
            # How it happened: self._proc was assigned before start_stderr_drain ran.
            # Why this change fixes it: keep the Popen instance in a local variable,
            # clean up both the process and pipes on failure, and only assign self._proc
            # and register once all startup operations have succeeded.
            proc: subprocess.Popen[bytes] | None = None
            drain: StderrTail | None = None
            try:
                if self._text:
                    proc = cast(
                        "subprocess.Popen[bytes]",
                        subprocess.Popen(
                            cmd,
                            stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE,
                            stderr=self._stderr,
                            env=merged_env,
                            cwd=self._cwd,
                            bufsize=1,
                            text=True,
                            **popen_kw,
                        ),
                    )
                else:
                    proc = cast(
                        "subprocess.Popen[bytes]",
                        subprocess.Popen(
                            cmd,
                            stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE,
                            stderr=self._stderr,
                            env=merged_env,
                            cwd=self._cwd,
                            bufsize=0,
                            **popen_kw,
                        ),
                    )
                if self._stderr == subprocess.PIPE and self._drain_stderr:
                    if self._stderr_name is not None:
                        drain_name = (
                            f"{self._stderr_name}-{proc.pid}"
                            if self._stderr_name_with_pid
                            else self._stderr_name
                        )
                    else:
                        drain_name = f"managed-stderr-{proc.pid}"
                    drain_kwargs: dict[str, Any] = {"name": drain_name}
                    if self._stderr_tail_chars is not None:
                        drain_kwargs["max_tail_chars"] = self._stderr_tail_chars
                    drain = start_stderr_drain(
                        proc.stderr,
                        **drain_kwargs,
                    )
                else:
                    drain = None
                if self._optimize_pipes:
                    optimize_popen_pipes(proc)
                self._proc = proc
                self._stderr_drain = drain
            except Exception as e:
                if proc is not None:
                    try:
                        kill_process_tree(proc)
                    except Exception:
                        pass
                    try:
                        proc.wait(timeout=1.0)
                    except Exception:
                        pass
                    if proc.stdin:
                        try:
                            proc.stdin.close()
                        except Exception:
                            pass
                    if proc.stdout:
                        try:
                            proc.stdout.close()
                        except Exception:
                            pass
                    if proc.stderr:
                        try:
                            proc.stderr.close()
                        except Exception:
                            pass
                self._proc = None
                self._stderr_drain = None
                if self._log_start_failure:
                    log.exception("Failed to start managed subprocess")
                    raise RuntimeError(f"Failed to start managed subprocess: {e}") from e
                raise

            if self._register:
                _register_instance(self)

    def _start_raw_locked(self) -> None:
        # TTS stdio is DEVNULL or a pipe that communicate reads itself; a stderr
        # drain would swallow Kokoro and Piper stderr; stop is a direct-child
        # signal, not close(); start_new_session would detach players from the
        # LibreOffice process group.
        if self._command_wrapper is not None:
            raise ValueError("raw_spawn does not take a command_wrapper")
        if self._scrub_env:
            raise ValueError("raw_spawn does not scrub env")
        if self._optimize_pipes:
            raise ValueError("raw_spawn does not optimize pipes")
        if self._base_env is not None:
            raise ValueError("raw_spawn does not merge base_env")
        if (
            self._stderr_tail_chars is not None
            or self._stderr_name is not None
            or self._stderr_name_with_pid
            or not self._drain_stderr
        ):
            raise ValueError("raw_spawn does not drain stderr")

        if self._thread_guard:
            self._thread_guard()

        proc = subprocess.Popen(
            list(self._cmd_line),
            stdin=self._raw_stdin,
            stdout=self._raw_stdout,
            stderr=self._raw_stderr,
            text=self._raw_text,
            encoding=self._raw_encoding,
            errors=self._raw_errors,
            env=self._env,
            cwd=self._cwd,
        )
        self._proc = proc

    @staticmethod
    def run(
        args: list[str],
        *,
        check: bool = False,
        capture_output: bool = False,
        stdout: Any = None,
        stderr: Any = None,
        stdin: Any = None,
        timeout: float | None = None,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
        text: bool = False,
        encoding: str | None = None,
        errors: str | None = None,
    ) -> subprocess.CompletedProcess[Any]:
        """One-shot subprocess.run with no env merge, session, creation flags, drain, or registry.

        Timeouts and OSError propagate.
        """
        if capture_output:
            return subprocess.run(
                args,
                capture_output=True,
                env=env,
                cwd=cwd,
                text=text,
                encoding=encoding,
                errors=errors,
                timeout=timeout,
                check=check,
            )
        return subprocess.run(
            args,
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
            env=env,
            cwd=cwd,
            text=text,
            encoding=encoding,
            errors=errors,
            timeout=timeout,
            check=check,
        )

    def unregister(self) -> None:
        """Unregister this instance from shutdown_all without closing pipes or killing processes."""
        with self._lock:
            _unregister_instance(self)

    def close(self) -> None:
        """Idempotent shutdown: kill process tree, close pipes, unregister, and reap."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            proc = self._proc
            _unregister_instance(self)

        if proc is None:
            return

        # 1. Close stdin in its own try block so child observes EOF.
        if proc.stdin:
            try:
                proc.stdin.close()
            except OSError:
                log.debug("Error closing stdin for pid=%s", proc.pid, exc_info=True)

        # 2. Terminate gracefully if still running.
        if proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                log.debug("Error terminating pid=%s", proc.pid, exc_info=True)
            try:
                proc.wait(timeout=_GRACE_KILL_TIMEOUT_SEC)
            except subprocess.TimeoutExpired:
                pass
            except OSError:
                log.debug("Error waiting for pid=%s after terminate", proc.pid, exc_info=True)

        # 3. Always kill process tree.
        # What was wrong: previously close() only killed the process tree if wait() timed out.
        # If the leader process died within the grace period (or had already exited before close()),
        # the tree kill was skipped entirely, leaving orphaned grandchildren running in the background.
        # How it happened: early return on poll() or wait() success bypassed the process group kill.
        # Why this change fixes it: always invoking kill_process_tree ensures the entire process
        # group is signaled, cleaning up all descendant processes even when the leader is dead.
        kill_process_tree(proc)

        # 4. If the process is still alive after the tree kill, enqueue to reaper so caller isn't blocked.
        if proc.poll() is None:
            _enqueue_reaper(("close", self))

        # 5. Close stdout and stderr pipes in separate try blocks after the tree kill.
        # Stdout must stay open until after the tree kill so readers blocked on readline
        # see EOF rather than hanging on pipe locks.
        if proc.stdout:
            try:
                proc.stdout.close()
            except OSError:
                log.debug("Error closing stdout for pid=%s", proc.pid, exc_info=True)

        if proc.stderr:
            try:
                proc.stderr.close()
            except OSError:
                log.debug("Error closing stderr for pid=%s", proc.pid, exc_info=True)

        drain = self._stderr_drain
        if drain is not None:
            try:
                drain.finish_text(timeout=1.0)
            except Exception:
                log.debug("Error finishing stderr drain for pid=%s", proc.pid, exc_info=True)


def kill_process_tree(proc: subprocess.Popen[Any]) -> None:
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
    # A MagicMock pid is truthy, and int(MagicMock()) is 1. os.getpgid()
    # accepts that, so getpgid(1) is process group 1. killpg(1) is
    # kill(-1, SIGKILL): every process this user can signal, which ends
    # the desktop session. Only a real pid above 1 is safe to look up.
    if type(pid) is not int or pid <= 1:
        if proc.poll() is None:
            try:
                proc.kill()
            except (ProcessLookupError, PermissionError, OSError):
                pass
        return

    # What was wrong: in containerized environments (such as CI containers or restricted
    # sandboxes) or when dealing with recycled PIDs or foreign process groups, os.getpgid(pid)
    # or os.killpg(pgid, signal.SIGKILL) could fail with PermissionError [Errno 1] Operation not permitted.
    # Furthermore, if pgid != pid, signalling pgid would attempt to kill a process group the child
    # does not own.
    # How it happened: kill_process_tree only caught ProcessLookupError and lacked an ownership check
    # against pgid != pid.
    # Why this change fixes it: catches (PermissionError, OSError) when querying or signalling groups,
    # verifies ownership (pgid == pid) for session leaders, and safely falls back to killing only the direct child.
    try:
        pgid = os.getpgid(pid)
        fallback = False
    except ProcessLookupError:
        pgid = pid
        fallback = True
    except (PermissionError, OSError):
        if proc.poll() is None:
            try:
                proc.kill()
            except (ProcessLookupError, PermissionError, OSError):
                pass
        return

    try:
        if fallback and proc.poll() is None:
            try:
                proc.kill()
            except (ProcessLookupError, PermissionError, OSError):
                pass
        elif type(pgid) is not int or pgid <= 1 or pgid == os.getpgrp():
            # killpg on the host's own group (a reused pid, or a child without
            # its own session) would kill LibreOffice. killpg(0) signals that
            # same group. killpg(1) is kill(-1, SIGKILL). Kill only proc.
            if proc.poll() is None:
                try:
                    proc.kill()
                except (ProcessLookupError, PermissionError, OSError):
                    pass
        elif pgid != pid:
            # Avoid killpg on groups we don't own. Workers are spawned with
            # start_new_session=True, so their process group ID equals pid.
            # If pgid != pid, the process joined another group or the pid was
            # recycled into a foreign group. Kill only proc.
            if proc.poll() is None:
                try:
                    proc.kill()
                except (ProcessLookupError, PermissionError, OSError):
                    pass
        else:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                if proc.poll() is None:
                    try:
                        proc.kill()
                    except (ProcessLookupError, PermissionError, OSError):
                        pass
            except (PermissionError, OSError):
                # PermissionError [Errno 1] Operation not permitted in restricted
                # container environments or when group ownership prevents killpg.
                # Fall back to killing the direct child.
                if proc.poll() is None:
                    try:
                        proc.kill()
                    except (ProcessLookupError, PermissionError, OSError):
                        pass
    except (ProcessLookupError, PermissionError, OSError):
        if proc.poll() is None:
            try:
                proc.kill()
            except (ProcessLookupError, PermissionError, OSError):
                pass


def _kill_process_tree_win32(proc: subprocess.Popen[Any]) -> None:
    """Terminate the Windows process tree; ``TerminateProcess`` does not kill grandchildren."""
    pid = proc.pid
    if not pid:
        try:
            proc.kill()
        except (ProcessLookupError, PermissionError, OSError):
            pass
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
        try:
            proc.kill()
        except (ProcessLookupError, PermissionError, OSError):
            pass
        return
    if proc.poll() is None:
        try:
            proc.kill()
        except (ProcessLookupError, PermissionError, OSError):
            pass


_instances: set[ManagedSubprocess] = set()
_registry_lock = threading.Lock()


def _register_instance(inst: ManagedSubprocess) -> None:
    with _registry_lock:
        _instances.add(inst)


def _unregister_instance(inst: ManagedSubprocess) -> None:
    with _registry_lock:
        _instances.discard(inst)


def shutdown_all() -> None:
    """Kill all live managed processes on extension shutdown."""
    with _registry_lock:
        targets = list(_instances)
    for inst in targets:
        try:
            inst.close()
        except Exception:
            log.exception("Error closing managed subprocess instance during shutdown_all")


_reaper_queue: queue.Queue[tuple[str, ManagedSubprocess]] = queue.Queue()
_reaper_lock = threading.Lock()
_reaper_started = False


def _enqueue_reaper(item: tuple[str, ManagedSubprocess]) -> None:
    """Hand off a process that refused to die gracefully so the caller isn't blocked."""
    global _reaper_started
    with _reaper_lock:
        _reaper_queue.put(item)
        if not _reaper_started:
            _reaper_started = True
            run_in_background(
                _reaper_loop,
                daemon=True,
                name="managed-subprocess-reaper",
                dedicated=True,
            )


def _reaper_loop() -> None:
    while True:
        try:
            kind, inst = _reaper_queue.get()
        except Exception:
            log.exception("Unexpected error getting process from reaper queue")
            continue
        try:
            if kind == "close":
                _reap_close(inst)
            elif kind == "abandon":
                inst.reaper_pass()
            else:
                log.error("unknown reaper job %r", kind)
        except Exception:
            log.exception("abandoned child reap failed")
        finally:
            _reaper_queue.task_done()


def _reap_close(inst: ManagedSubprocess) -> None:
    proc = inst.popen
    if proc is None:
        return
    try:
        if proc.poll() is None:
            proc.wait(timeout=_REAP_CLOSE_WAIT_SEC)
    except subprocess.TimeoutExpired:
        log.warning(
            "Managed subprocess pid=%s still alive after SIGKILL; abandoning",
            getattr(proc, "pid", None),
        )
    except Exception:
        log.exception("Error reaping managed subprocess pid=%s", getattr(proc, "pid", None))
