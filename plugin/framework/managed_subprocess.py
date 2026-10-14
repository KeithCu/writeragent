# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared managed-subprocess bottom layer.

Provides explicit start, environment merging, thread-guard refusal, bounded stderr,
tree kill, reaping, and registered shutdown.
"""

from __future__ import annotations

import logging
import os
import queue
import signal
import subprocess
import sys
import threading
from typing import IO, Any, Callable, cast

from plugin.framework.worker_pool import (
    StderrTail,
    get_subprocess_creationflags,
    start_stderr_drain,
    run_in_background,
)

log = logging.getLogger(__name__)

# Wait timeout before reaping an orphaned process.
_GRACE_KILL_TIMEOUT_SEC = 3.0


class ManagedSubprocess:
    """A managed child process providing explicit start, safe shutdown, and stderr tailing.

    Callers own the stdout and stdin pipes (e.g. for JSON-RPC or custom framing).
    This layer handles environment merging, process-tree killing, and cleanup.
    """

    _cmd_line: list[str]
    _cwd: str | None
    _env: dict[str, str] | None
    _base_env: dict[str, str] | None
    _command_wrapper: Callable[[list[str]], list[str]] | None
    _thread_guard: Callable[[], None] | None
    _proc: subprocess.Popen[bytes] | None
    _stderr_drain: StderrTail | None
    _lock: threading.Lock
    _closed: bool

    def __init__(
        self,
        cmd_line: list[str],
        *,
        env: dict[str, str] | None = None,
        base_env: dict[str, str] | None = None,
        cwd: str | None = None,
        command_wrapper: Callable[[list[str]], list[str]] | None = None,
        thread_guard: Callable[[], None] | None = None,
    ) -> None:
        self._cmd_line = cmd_line
        self._cwd = cwd
        self._env = env
        self._base_env = base_env
        self._command_wrapper = command_wrapper
        self._thread_guard = thread_guard
        self._proc = None
        self._stderr_drain = None
        self._lock = threading.Lock()
        self._closed = False

    @property
    def popen(self) -> subprocess.Popen[bytes] | None:
        """Return the underlying subprocess.Popen instance or None if not started."""
        return self._proc

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
        """Spawn the child process. Raises RuntimeError if already running, closed, or reused."""
        with self._lock:
            # Single-use policy: a ManagedSubprocess cannot be respawned or restarted after exit/close.
            if self._closed or self._proc is not None:
                raise RuntimeError("ManagedSubprocess is single-use")

            if self._thread_guard:
                self._thread_guard()

            # Environment merge:
            # - base_env is None: copy os.environ, then overlay env (ACP).
            # - base_env is dict (even {}): copy that dict only, then overlay env (Kokoro/scrubbed env).
            if self._base_env is None:
                merged_env = dict(os.environ)
            else:
                merged_env = dict(self._base_env)
            if self._env:
                merged_env.update(self._env)

            popen_kw = get_subprocess_creationflags()
            if sys.platform != "win32":
                popen_kw["start_new_session"] = True

            cmd = (
                self._command_wrapper(self._cmd_line)
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
                proc = cast(
                    "subprocess.Popen[bytes]",
                    subprocess.Popen(
                        cmd,
                        stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        env=merged_env,
                        cwd=self._cwd,
                        bufsize=0,
                        **popen_kw,
                    ),
                )
                drain = start_stderr_drain(
                    proc.stderr,
                    name=f"managed-stderr-{proc.pid}",
                )
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
                log.exception("Failed to start managed subprocess")
                raise RuntimeError(f"Failed to start managed subprocess: {e}") from e

            _register_instance(self)

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
            _enqueue_reaper(proc)

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
        elif type(pgid) is not int or pgid <= 1 or pgid == os.getpgrp():
            # killpg on the host's own group (a reused pid, or a child without
            # its own session) would kill LibreOffice. killpg(0) signals that
            # same group. killpg(1) is kill(-1, SIGKILL). Kill only proc.
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


_reaper_queue: queue.Queue[subprocess.Popen[Any]] = queue.Queue()
_reaper_lock = threading.Lock()
_reaper_started = False


def _enqueue_reaper(proc: subprocess.Popen[Any]) -> None:
    """Hand off a process that refused to die gracefully so the caller isn't blocked."""
    global _reaper_started
    with _reaper_lock:
        _reaper_queue.put(proc)
        if not _reaper_started:
            _reaper_started = True
            run_in_background(
                _reaper_loop,
                daemon=True,
                name="managed-subprocess-reaper",
                dedicated=True,
            )


def _reaper_loop() -> None:
    """Wait on orphaned children in the background to prevent zombies."""
    while True:
        try:
            proc = _reaper_queue.get()
        except Exception:
            log.exception("Unexpected error getting process from reaper queue")
            continue

        try:
            if proc.poll() is None:
                proc.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            log.warning("Managed subprocess pid=%s still alive after SIGKILL; abandoning", getattr(proc, "pid", None))
        except Exception:
            log.exception("Error reaping managed subprocess pid=%s", getattr(proc, "pid", None))
        finally:
            _reaper_queue.task_done()
