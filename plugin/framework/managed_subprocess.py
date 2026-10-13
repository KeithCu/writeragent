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
        thread_guard: Callable[[], None] | None = None,
    ) -> None:
        self._cmd_line = cmd_line
        self._cwd = cwd
        self._env = env
        self._base_env = base_env
        self._thread_guard = thread_guard
        self._proc = None
        self._stderr_drain = None
        self._lock = threading.Lock()
        self._closed = False

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
        """Spawn the child process. Raises RuntimeError if already running or closed."""
        with self._lock:
            if self._closed:
                raise RuntimeError("ManagedSubprocess is closed")
            if self.is_alive():
                raise RuntimeError("ManagedSubprocess is already running")

            if self._thread_guard:
                self._thread_guard()

            from plugin.scripting.sandbox import wrap_command_for_sandbox

            # Environment merge: base_env (or os.environ) + env
            merged_env = dict(self._base_env if self._base_env is not None else os.environ)
            if self._env:
                merged_env.update(self._env)

            popen_kw = get_subprocess_creationflags()
            if sys.platform != "win32":
                popen_kw["start_new_session"] = True

            cmd = wrap_command_for_sandbox(self._cmd_line)

            try:
                self._proc = cast(
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
                self._stderr_drain = start_stderr_drain(self._proc.stderr, name=f"managed-stderr-{self._proc.pid}")
            except Exception as e:
                log.exception("Failed to start managed subprocess")
                raise RuntimeError(f"Failed to start managed subprocess: {e}") from e

            _register_instance(self)

    def close(self) -> None:
        """Idempotent shutdown: kill process tree, close pipes, unregister, and reap."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            proc = self._proc
            self._proc = None
            _unregister_instance(self)

        if proc is None:
            return

        try:
            if proc.stdin:
                proc.stdin.close()
            if proc.stdout:
                proc.stdout.close()
        except Exception:
            pass

        if proc.poll() is None:
            try:
                proc.terminate()
            except Exception:
                pass

            try:
                proc.wait(timeout=_GRACE_KILL_TIMEOUT_SEC)
            except subprocess.TimeoutExpired:
                _kill_process_tree(proc)
                _enqueue_reaper(proc)


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
            pass

_reaper_queue: list[subprocess.Popen[Any]] = []
_reaper_lock = threading.Lock()
_reaper_started = False

def _enqueue_reaper(proc: subprocess.Popen[Any]) -> None:
    """Hand off a process that refused to die gracefully so the caller isn't blocked."""
    global _reaper_started
    with _reaper_lock:
        _reaper_queue.append(proc)
        if not _reaper_started:
            _reaper_started = True
            run_in_background(_reaper_loop, daemon=True, name="managed-subprocess-reaper", dedicated=True)

def _reaper_loop() -> None:
    """Wait on orphaned children in the background to prevent zombies."""
    import time
    while True:
        with _reaper_lock:
            if not _reaper_queue:
                global _reaper_started
                _reaper_started = False
                return
            proc = _reaper_queue.pop(0)

        try:
            if proc.poll() is None:
                proc.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            log.warning("Managed subprocess pid=%s still alive after SIGKILL; abandoning", proc.pid)
            continue
        except Exception:
            pass

        # Small sleep so we don't spin heavily if wait throws immediately.
        time.sleep(0.1)
