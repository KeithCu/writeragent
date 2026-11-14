# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
import types
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from plugin.framework.managed_subprocess import (
    ManagedSubprocess,
    _instances,
    optimize_pipe,
    optimize_popen_pipes,
    shutdown_all,
)


@pytest.fixture(autouse=True)
def _fake_fcntl_on_non_posix(monkeypatch: pytest.MonkeyPatch) -> None:
    if "fcntl" not in sys.modules:
        fake_fcntl = types.ModuleType("fcntl")
        fake_fcntl.F_SETPIPE_SZ = 1031
        fake_fcntl.fcntl = lambda *args, **kwargs: None
        monkeypatch.setitem(sys.modules, "fcntl", fake_fcntl)
    elif not hasattr(sys.modules["fcntl"], "F_SETPIPE_SZ"):
        monkeypatch.setattr(sys.modules["fcntl"], "F_SETPIPE_SZ", 1031, raising=False)


def pid_is_alive(pid: int) -> bool:
    """True if *pid* still names a live process."""
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

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.OpenProcess(0x1000, False, pid)
    if handle:
        kernel32.CloseHandle(handle)
        return True
    return ctypes.get_last_error() == 5


def _reap_for_test(managed: ManagedSubprocess) -> None:
    managed.kill_tree()
    proc = managed.popen
    if proc is not None:
        try:
            proc.wait(timeout=5)
        except Exception:
            pass
    managed.join_stderr(1.0)
    managed.unregister()


def test_managed_subprocess_explicit_start_and_cwd(tmp_path):
    cwd = str(tmp_path)
    env = {"TEST_VAR": "hello"}

    # Child waits for stdin to close, so it is still alive when we check (fast Macs finished first).
    cmd = [sys.executable, "-c", "import os, sys; print(os.getcwd()); print(os.environ.get('TEST_VAR')); sys.stdin.read()"]

    proc = ManagedSubprocess(cmd, env=env, cwd=cwd)
    assert not proc.is_alive()

    proc.start()
    assert proc.is_alive()
    assert proc.stdin is not None
    assert proc.stdout is not None
    assert proc.stderr_tail is not None

    proc.stdin.close()
    stdout = proc.stdout.read()
    proc.stdout.close()
    output = stdout.decode("utf-8").strip().splitlines()

    assert output[0].endswith(os.path.basename(cwd))
    assert output[1] == "hello"

    _reap_for_test(proc)
    assert not proc.is_alive()


def test_managed_subprocess_thread_guard():
    refused = False

    def guard():
        nonlocal refused
        refused = True
        raise RuntimeError("Forbidden thread")

    cmd = [sys.executable, "-c", "print('ok')"]
    proc = ManagedSubprocess(cmd, thread_guard=guard)

    with pytest.raises(RuntimeError, match="Forbidden thread"):
        proc.start()

    assert refused
    assert not proc.is_alive()
    assert proc._proc is None


def test_managed_subprocess_stderr_tail():
    code = "import sys; sys.stderr.write('crash text'); sys.stderr.flush()"
    cmd = [sys.executable, "-c", code]

    proc = ManagedSubprocess(cmd)
    proc.start()

    assert proc.stdout is not None
    proc.stdout.read()  # Synchronize on child exit / EOF
    tail = proc.stderr_tail
    assert tail is not None
    assert "crash text" in tail.finish_text(timeout=1.0)
    _reap_for_test(proc)


def test_managed_subprocess_shutdown_registry():
    import plugin.framework.managed_subprocess as ms

    cmd1 = [sys.executable, "-c", "import time; time.sleep(10)"]
    cmd2 = [sys.executable, "-c", "import time; time.sleep(10)"]

    proc1 = ManagedSubprocess(cmd1)
    proc1.start()

    proc2 = ManagedSubprocess(cmd2)
    proc2.start()

    assert proc1 in _instances
    assert proc2 in _instances
    assert proc1.is_alive()
    assert proc2.is_alive()

    shutdown_all()
    ms._reaper_queue.join()

    assert not proc1.is_alive()
    assert not proc2.is_alive()
    assert proc1 not in _instances
    assert proc2 not in _instances


def test_shutdown_all_kills_without_close() -> None:
    import plugin.framework.managed_subprocess as ms

    inst = MagicMock()
    ms._instances.add(inst)
    try:
        ms.shutdown_all()
        inst.kill_tree.assert_called_once()
        inst.abandon.assert_called_once()
        inst.close.assert_not_called()
    finally:
        ms._instances.discard(inst)


def test_managed_subprocess_start_failure_after_popen(monkeypatch):
    import plugin.framework.managed_subprocess as ms

    mock_proc = MagicMock()
    mock_proc.pid = 7777
    mock_proc.stdin = MagicMock()
    mock_proc.stdout = MagicMock()
    mock_proc.stderr = MagicMock()

    killed = []
    monkeypatch.setattr(ms, "kill_process_tree", lambda p: killed.append(p))
    monkeypatch.setattr(ms.subprocess, "Popen", lambda *a, **kw: mock_proc)
    monkeypatch.setattr(ms, "start_stderr_drain", MagicMock(side_effect=RuntimeError("drain failed")))

    managed = ms.ManagedSubprocess(["dummy"])
    with pytest.raises(RuntimeError, match="drain failed"):
        managed.start()

    assert killed == [mock_proc]
    assert managed._proc is None
    assert managed._stderr_drain is None
    assert managed not in ms._instances
    mock_proc.stdin.close.assert_called_once()
    mock_proc.stdout.close.assert_called_once()


def test_managed_subprocess_single_use_after_exit():
    managed = ManagedSubprocess([sys.executable, "-c", "import sys; sys.exit(0)"])
    managed.start()
    assert managed.stdout is not None
    managed.stdout.read()
    assert managed.popen is not None
    managed.popen.wait(timeout=5)
    assert not managed.is_alive()

    first_proc = managed._proc
    first_drain = managed.stderr_tail

    with pytest.raises(RuntimeError, match="single-use"):
        managed.start()

    assert managed._proc is first_proc
    assert managed.stderr_tail is first_drain

    _reap_for_test(managed)
    assert managed not in _instances


def test_managed_subprocess_env_and_wrapper(monkeypatch):
    import plugin.framework.managed_subprocess as ms

    captured_kw: dict[str, Any] = {}
    captured_cmd: list[str] = []

    def fake_popen(cmd, **kwargs):
        captured_cmd.clear()
        captured_cmd.extend(cmd)
        captured_kw.clear()
        captured_kw.update(kwargs)
        mock = MagicMock()
        mock.pid = 1111
        mock.poll.return_value = 0
        mock.stderr = MagicMock()
        return mock

    monkeypatch.setattr(ms.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(ms, "start_stderr_drain", lambda *a, **kw: MagicMock())

    # 1. base_env dict + env overlay
    monkeypatch.setenv("SENTINEL_VAR", "should_not_appear")
    managed1 = ms.ManagedSubprocess(
        ["echo", "1"],
        base_env={"A": "1", "B": "base"},
        env={"B": "over", "C": "3"},
    )
    managed1.start()
    _reap_for_test(managed1)
    env1 = captured_kw["env"]
    assert env1 == {"A": "1", "B": "over", "C": "3"}
    assert "SENTINEL_VAR" not in env1

    # 2. base_env is None -> os.environ + env overlay (ACP)
    managed2 = ms.ManagedSubprocess(
        ["echo", "2"],
        base_env=None,
        env={"TEST_VAR": "hello"},
    )
    managed2.start()
    _reap_for_test(managed2)
    env2 = captured_kw["env"]
    assert env2["TEST_VAR"] == "hello"
    assert "PATH" in env2
    assert env2.get("SENTINEL_VAR") == "should_not_appear"

    # 3. base_env is {} -> only {} + env overlay (Kokoro)
    managed3 = ms.ManagedSubprocess(
        ["echo", "3"],
        base_env={},
        env={"ONLY": "1"},
    )
    managed3.start()
    _reap_for_test(managed3)
    env3 = captured_kw["env"]
    assert env3 == {"ONLY": "1"}

    # 4. command_wrapper
    managed4 = ms.ManagedSubprocess(
        ["orig", "arg"],
        command_wrapper=lambda cmd, **_kwargs: ["wrapped", *cmd],
    )
    managed4.start()
    _reap_for_test(managed4)
    assert captured_cmd[:3] == ["wrapped", "orig", "arg"]

    # 5. command_wrapper is None -> no wrapping and does not call wrap_command_for_sandbox
    monkeypatch.setattr(
        "plugin.scripting.sandbox.wrap_command_for_sandbox",
        MagicMock(side_effect=RuntimeError("must not be called")),
    )
    managed5 = ms.ManagedSubprocess(
        ["orig", "arg2"],
        command_wrapper=None,
    )
    managed5.start()
    _reap_for_test(managed5)
    assert captured_cmd == ["orig", "arg2"]


def test_command_wrapper_receives_env_and_cwd(monkeypatch):
    import plugin.framework.managed_subprocess as ms

    recorded: list[tuple[list[str], dict[str, str] | None, str | None]] = []

    def mock_wrapper(cmd: list[str], *, env: dict[str, str] | None = None, cwd: str | None = None) -> list[str]:
        recorded.append((cmd, env, cwd))
        return cmd

    proc = MagicMock()
    proc.poll.return_value = None
    proc.stdin = MagicMock()
    proc.stdout = MagicMock()
    proc.stderr = MagicMock()
    popen = MagicMock(return_value=proc)
    monkeypatch.setattr(ms.subprocess, "Popen", popen)
    monkeypatch.setattr(ms, "start_stderr_drain", lambda *a, **k: None)

    managed = ms.ManagedSubprocess(
        ["orig", "arg"],
        command_wrapper=mock_wrapper,
        env={"ONLY": "1"},
        base_env={},
        cwd="/tmp",
    )
    managed.start()
    _reap_for_test(managed)

    assert len(recorded) == 1
    cmd, env, cwd = recorded[0]
    assert cmd == ["orig", "arg"]
    assert env == {"ONLY": "1"}
    assert cwd == "/tmp"


def test_kill_process_tree_win32_uses_taskkill(monkeypatch):
    """Windows must kill grandchildren; TerminateProcess on the worker PID is not enough."""
    import plugin.framework.managed_subprocess as managed_subprocess

    run = MagicMock()
    monkeypatch.setattr(managed_subprocess.subprocess, "run", run)
    monkeypatch.setattr(managed_subprocess.sys, "platform", "win32")
    monkeypatch.setattr("plugin.framework.worker_pool.subprocess.CREATE_NO_WINDOW", 0x08000000, raising=False)

    proc = MagicMock()
    proc.poll.return_value = 1
    proc.pid = 4242
    managed_subprocess.kill_process_tree(proc)
    run.assert_called_once()
    assert run.call_args[0][0] == ["taskkill", "/F", "/T", "/PID", "4242"]
    proc.kill.assert_not_called()


def test_kill_process_tree_signals_group_after_leader_exits(monkeypatch):
    """Descendants must still be signaled when the direct child has already exited."""
    import plugin.framework.managed_subprocess as managed_subprocess

    proc = MagicMock()
    proc.pid = 4242
    proc.poll.return_value = 0
    if sys.platform == "win32":
        run = MagicMock()
        monkeypatch.setattr(managed_subprocess.subprocess, "run", run)
        managed_subprocess.kill_process_tree(proc)
        run.assert_called_once()
        assert run.call_args[0][0] == ["taskkill", "/F", "/T", "/PID", "4242"]
        proc.kill.assert_not_called()
        return

    killed: dict[str, int] = {}

    def _getpgid(pid: int) -> int:
        raise ProcessLookupError(pid)

    def _killpg(pgid: int, sig: int) -> None:
        killed["pgid"] = pgid
        killed["sig"] = sig

    monkeypatch.setattr(managed_subprocess.os, "getpgid", _getpgid)
    monkeypatch.setattr(managed_subprocess.os, "killpg", _killpg)

    # Leader already exited, proc.poll() returns 0.
    proc.poll.return_value = 0
    managed_subprocess.kill_process_tree(proc)
    assert killed == {"pgid": 4242, "sig": signal.SIGKILL}
    proc.kill.assert_not_called()


def test_kill_process_tree_no_pgid_but_alive(monkeypatch):
    """If getpgid fails and the leader is still alive, we fallback to proc.kill()."""
    import plugin.framework.managed_subprocess as managed_subprocess

    proc = MagicMock()
    proc.pid = 4242
    proc.poll.return_value = None
    if sys.platform == "win32":
        return

    killed: dict[str, int] = {}

    def _getpgid(pid: int) -> int:
        raise ProcessLookupError(pid)

    def _killpg(pgid: int, sig: int) -> None:
        killed["pgid"] = pgid
        killed["sig"] = sig

    monkeypatch.setattr(managed_subprocess.os, "getpgid", _getpgid)
    monkeypatch.setattr(managed_subprocess.os, "killpg", _killpg)

    managed_subprocess.kill_process_tree(proc)
    assert not killed  # os.killpg not called
    proc.kill.assert_called_once()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups")
def test_kill_process_tree_reaps_grandchild_after_leader_exits():
    """Real session: leader exit must not leave the grandchild running."""
    import plugin.framework.managed_subprocess as managed_subprocess

    code = (
        "import os, sys, time\n"
        "pid = os.fork()\n"
        "if pid == 0:\n"
        "    time.sleep(120)\n"
        "    os._exit(0)\n"
        "sys.stderr.write('GRANDCHILD %s\\n' % pid)\n"
        "sys.stderr.flush()\n"
        "os._exit(0)\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", code],
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    gpid = None
    try:
        assert proc.stderr is not None
        line = proc.stderr.readline()
        text = line.decode()
        assert text.startswith("GRANDCHILD "), text
        gpid = int(text.split()[1])
        proc.wait(timeout=5)
        assert proc.poll() is not None
        assert pid_is_alive(gpid)
        managed_subprocess.kill_process_tree(proc)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and pid_is_alive(gpid):
            time.sleep(0.05)
        assert not pid_is_alive(gpid), f"grandchild pid {gpid} survived group kill"
    finally:
        if gpid is not None and pid_is_alive(gpid):
            try:
                os.kill(gpid, signal.SIGKILL)
            except OSError:
                pass
        if proc.poll() is None:
            proc.kill()
        try:
            proc.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            pass
        if proc.stderr is not None:
            proc.stderr.close()


def test_kill_process_tree_host_pgid_protection(monkeypatch):
    """Never killpg the host's own group; fall back to killing just the child."""
    import plugin.framework.managed_subprocess as managed_subprocess

    if sys.platform == "win32":
        pytest.skip("POSIX process groups")
    proc = MagicMock()
    proc.pid = 12345
    proc.poll.return_value = None
    killpg = MagicMock()
    monkeypatch.setattr(managed_subprocess.os, "getpgid", lambda pid: 9999)
    monkeypatch.setattr(managed_subprocess.os, "getpgrp", lambda: 9999)
    monkeypatch.setattr(managed_subprocess.os, "killpg", killpg)

    managed_subprocess.kill_process_tree(proc)

    killpg.assert_not_called()
    proc.kill.assert_called_once()


def test_kill_process_tree_refuses_process_group_one(monkeypatch):
    """killpg(1) is kill(-1, SIGKILL) and tears down the login session.

    unittest.mock.MagicMock converts to int 1, and os.getpgid() accepts that.
    A test double passed to ACPConnection.stop() used to log the desktop out.
    """
    import plugin.framework.managed_subprocess as managed_subprocess

    if sys.platform == "win32":
        pytest.skip("POSIX process groups")
    killpg = MagicMock()
    monkeypatch.setattr(managed_subprocess.os, "killpg", killpg)

    unset = MagicMock()
    managed_subprocess.kill_process_tree(unset)
    killpg.assert_not_called()
    unset.kill.assert_not_called()

    init_pid = MagicMock()
    init_pid.pid = 1
    init_pid.poll.return_value = None
    managed_subprocess.kill_process_tree(init_pid)
    killpg.assert_not_called()
    init_pid.kill.assert_called_once()

    reused = MagicMock()
    reused.pid = 4242
    reused.poll.return_value = None
    monkeypatch.setattr(managed_subprocess.os, "getpgid", lambda _pid: 1)
    monkeypatch.setattr(managed_subprocess.os, "getpgrp", lambda: 9999)
    managed_subprocess.kill_process_tree(reused)
    killpg.assert_not_called()
    reused.kill.assert_called_once()


def test_kill_process_tree_avoids_foreign_group(monkeypatch):
    """Never killpg a process group that does not match the session leader's pid."""
    import plugin.framework.managed_subprocess as managed_subprocess

    if sys.platform == "win32":
        pytest.skip("POSIX process groups")
    proc = MagicMock()
    proc.pid = 4242
    proc.poll.return_value = None
    killpg = MagicMock()
    # Process has joined a foreign group (e.g. pgid=5555 != pid 4242)
    monkeypatch.setattr(managed_subprocess.os, "getpgid", lambda _pid: 5555)
    monkeypatch.setattr(managed_subprocess.os, "getpgrp", lambda: 9999)
    monkeypatch.setattr(managed_subprocess.os, "killpg", killpg)

    managed_subprocess.kill_process_tree(proc)

    killpg.assert_not_called()
    proc.kill.assert_called_once()


def test_kill_process_tree_handles_permission_error_on_getpgid(monkeypatch):
    """Handle PermissionError during getpgid (e.g. container sandboxes) without raising."""
    import plugin.framework.managed_subprocess as managed_subprocess

    if sys.platform == "win32":
        pytest.skip("POSIX process groups")
    proc = MagicMock()
    proc.pid = 4242
    proc.poll.return_value = None
    killpg = MagicMock()

    def _getpgid(pid: int) -> int:
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(managed_subprocess.os, "getpgid", _getpgid)
    monkeypatch.setattr(managed_subprocess.os, "killpg", killpg)

    managed_subprocess.kill_process_tree(proc)

    killpg.assert_not_called()
    proc.kill.assert_called_once()


def test_kill_process_tree_handles_permission_error_on_killpg(monkeypatch):
    """Handle PermissionError during killpg (e.g. group owned by another user) without raising."""
    import plugin.framework.managed_subprocess as managed_subprocess

    if sys.platform == "win32":
        pytest.skip("POSIX process groups")
    proc = MagicMock()
    proc.pid = 4242
    proc.poll.return_value = None

    def _killpg(pgid: int, sig: int) -> None:
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(managed_subprocess.os, "getpgid", lambda _pid: 4242)
    monkeypatch.setattr(managed_subprocess.os, "getpgrp", lambda: 9999)
    monkeypatch.setattr(managed_subprocess.os, "killpg", _killpg)

    # Must not raise PermissionError; falls back to direct child kill.
    managed_subprocess.kill_process_tree(proc)
    proc.kill.assert_called_once()


def test_scrub_env_runs_before_overlay(monkeypatch: pytest.MonkeyPatch) -> None:
    import plugin.framework.managed_subprocess as ms

    recorded_env: dict[str, str] | None = None

    def fake_popen(*_args: object, **kwargs: object) -> MagicMock:
        nonlocal recorded_env
        recorded_env = kwargs.get("env")  # type: ignore[assignment]
        mock_p = MagicMock()
        mock_p.pid = 123
        mock_p.stdin = MagicMock()
        mock_p.stdout = MagicMock()
        mock_p.stderr = MagicMock()
        return mock_p

    monkeypatch.setattr(ms.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(ms, "start_stderr_drain", lambda *a, **kw: MagicMock())

    base = {"OPENAI_API_KEY": "x", "PATH": "/bin", "PYTHONPATH": "/leak"}
    overlay = {"PYTHONPATH": "/ext"}
    proc = ms.ManagedSubprocess(["dummy"], base_env=base, env=overlay, scrub_env=True)
    proc.start()
    try:
        assert recorded_env is not None
        assert "OPENAI_API_KEY" not in recorded_env
        assert recorded_env.get("PATH") == "/bin"
        assert recorded_env.get("PYTHONPATH") == "/ext"
        assert recorded_env.get("PYTHONUTF8") == "1"
    finally:
        _reap_for_test(proc)


@patch("plugin.framework.managed_subprocess.sys.platform", "linux")
@patch("fcntl.fcntl", side_effect=OSError("cap denied"))
def test_optimize_pipe_swallows_oserror(_mock_fcntl: MagicMock) -> None:
    optimize_pipe(3)


@patch("plugin.framework.managed_subprocess.sys.platform", "win32")
@patch("fcntl.fcntl")
def test_optimize_pipe_noop_on_windows(mock_fcntl: MagicMock) -> None:
    optimize_pipe(5)
    mock_fcntl.assert_not_called()


@patch("plugin.framework.managed_subprocess.sys.platform", "darwin")
@patch("fcntl.fcntl")
def test_optimize_pipe_noop_on_macos(mock_fcntl: MagicMock) -> None:
    optimize_pipe(5)
    mock_fcntl.assert_not_called()


def _linux_pipe_size_cmds() -> tuple[int, int]:
    import fcntl

    get_cmd = getattr(fcntl, "F_GETPIPE_SZ", None)
    set_cmd = getattr(fcntl, "F_SETPIPE_SZ", None)
    if get_cmd is None or set_cmd is None:
        pytest.skip("fcntl F_GETPIPE_SZ / F_SETPIPE_SZ unavailable")
    return int(get_cmd), int(set_cmd)


@pytest.mark.skipif(sys.platform != "linux", reason="F_SETPIPE_SZ is Linux-only")
def test_optimize_pipe_grows_linux_pipe_buffer() -> None:
    import fcntl

    get_cmd, _set_cmd = _linux_pipe_size_cmds()
    r_fd, w_fd = os.pipe()
    try:
        before = fcntl.fcntl(w_fd, get_cmd)
        optimize_pipe(w_fd)
        after = fcntl.fcntl(w_fd, get_cmd)
        assert after > before
    finally:
        os.close(r_fd)
        os.close(w_fd)


@pytest.mark.skipif(sys.platform != "linux", reason="F_SETPIPE_SZ is Linux-only")
def test_optimize_popen_pipes_skips_none_streams() -> None:
    import fcntl

    get_cmd, _set_cmd = _linux_pipe_size_cmds()
    proc = subprocess.Popen(
        [sys.executable, "-c", "pass"],
        stdout=subprocess.PIPE,
    )
    try:
        assert proc.stdin is None
        assert proc.stderr is None
        assert proc.stdout is not None
        before = fcntl.fcntl(proc.stdout.fileno(), get_cmd)
        optimize_popen_pipes(proc)
        after = fcntl.fcntl(proc.stdout.fileno(), get_cmd)
        assert after > before
    finally:
        if proc.stdout is not None:
            proc.stdout.close()
        proc.wait(timeout=5)


@pytest.mark.skipif(sys.platform != "linux", reason="F_SETPIPE_SZ is Linux-only")
def test_optimize_pipes_is_opt_in() -> None:
    import fcntl

    get_cmd, _set_cmd = _linux_pipe_size_cmds()
    r_fd, w_fd = os.pipe()
    try:
        default_pipe_size = fcntl.fcntl(w_fd, get_cmd)
    finally:
        os.close(r_fd)
        os.close(w_fd)

    cmd = [sys.executable, "-c", "import sys; sys.stdin.read()"]
    proc_default = ManagedSubprocess(cmd)
    proc_default.start()
    try:
        assert proc_default.popen is not None
        assert proc_default.popen.stdout is not None
        assert fcntl.fcntl(proc_default.popen.stdout.fileno(), get_cmd) == default_pipe_size
    finally:
        _reap_for_test(proc_default)

    proc_opt = ManagedSubprocess(cmd, optimize_pipes=True)
    proc_opt.start()
    try:
        assert proc_opt.popen is not None
        assert proc_opt.popen.stdout is not None
        assert fcntl.fcntl(proc_opt.popen.stdout.fileno(), get_cmd) > default_pipe_size
    finally:
        _reap_for_test(proc_opt)


def test_abandoned_reaper_pauses_before_requeue(monkeypatch: pytest.MonkeyPatch) -> None:
    import plugin.framework.managed_subprocess as ms

    sleeps: list[float] = []
    enqueued: list[object] = []
    monkeypatch.setattr(ms.time, "sleep", lambda sec: sleeps.append(sec))
    monkeypatch.setattr(ms, "_enqueue_reaper", lambda item: enqueued.append(item))

    class _Stuck:
        def poll(self) -> None:
            return None

        def wait(self, timeout: float | None = None) -> int:
            del timeout
            raise subprocess.TimeoutExpired(cmd="stuck", timeout=1)

    managed = ms.ManagedSubprocess.wrap_existing(_Stuck(), label="Worker")  # type: ignore[arg-type]
    managed.reaper_pass()
    assert sleeps == [0.1]
    assert enqueued == [managed]


def test_reaper_stops_after_max_passes(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import logging
    import plugin.framework.managed_subprocess as ms

    sleeps: list[float] = []
    enqueued: list[object] = []
    monkeypatch.setattr(ms.time, "sleep", lambda sec: sleeps.append(sec))
    monkeypatch.setattr(ms, "_enqueue_reaper", lambda item: enqueued.append(item))

    class _Stuck:
        pid = 41

        def poll(self) -> None:
            return None

        def wait(self, timeout: float | None = None) -> int:
            del timeout
            raise subprocess.TimeoutExpired(cmd="stuck", timeout=1)

    managed = ms.ManagedSubprocess.wrap_existing(_Stuck(), label="Worker #1")  # type: ignore[arg-type]
    ms._register_instance(managed)
    try:
        with caplog.at_level(logging.WARNING, logger="plugin.framework.managed_subprocess"):
            for _idx in range(ms._REAPER_MAX_PASSES):
                managed.reaper_pass()
        assert managed not in ms._instances
        assert managed in ms._reaper_given_up
        assert sleeps == [0.1] * (ms._REAPER_MAX_PASSES - 1)
        assert len(enqueued) == ms._REAPER_MAX_PASSES - 1
        assert enqueued == [managed] * (ms._REAPER_MAX_PASSES - 1)
        giving_up = [record for record in caplog.records if "reaper giving up" in record.getMessage()]
        assert len(giving_up) == 1
        assert "Worker #1" in giving_up[0].getMessage()
        assert "pid=41" in giving_up[0].getMessage()
    finally:
        if managed in ms._reaper_given_up:
            ms._reaper_given_up.remove(managed)
        ms._instances.discard(managed)


def test_abandoned_reaper_thread_is_a_daemon(monkeypatch: pytest.MonkeyPatch) -> None:
    import queue
    import plugin.framework.managed_subprocess as ms

    calls: list[dict[str, object]] = []

    def _spy(*_args: object, **kwargs: object) -> None:
        calls.append(dict(kwargs))

    monkeypatch.setattr(ms, "_reaper_started", False)
    monkeypatch.setattr(ms, "run_in_background", _spy)
    managed = ms.ManagedSubprocess.wrap_existing(object(), label="Worker")  # type: ignore[arg-type]
    try:
        managed.abandon()
        managed.abandon()
        assert calls == [{"name": "managed-subprocess-reaper", "dedicated": True, "daemon": True}]
    finally:
        while True:
            try:
                ms._reaper_queue.get_nowait()
            except queue.Empty:
                break


def test_inherit_env_rejects_conflicts() -> None:
    for kwargs in (
        {"env": {"A": "b"}},
        {"env": {}},
        {"base_env": {}},
        {"base_env": {"A": "b"}},
        {"scrub_env": True},
        {"env": {"A": "b"}, "base_env": {"A": "b"}, "scrub_env": True},
    ):
        with pytest.raises(ValueError):
            ManagedSubprocess(["x"], inherit_env=True, **kwargs)


def test_stderr_rejects_non_pipe_non_devnull() -> None:
    for val in (subprocess.STDOUT, 1):
        with pytest.raises(ValueError):
            ManagedSubprocess(["x"], stderr=val)


def test_inherit_env_popen_and_wrapper_see_none(monkeypatch: pytest.MonkeyPatch) -> None:
    import plugin.framework.managed_subprocess as ms
    from plugin.scripting.sandbox import wrap_command_for_sandbox
    import plugin.scripting.sandbox as sandbox_mod

    wrapper_calls: list[tuple[list[str], dict[str, str] | None, str | None]] = []

    def recording_wrapper(cmd: list[str], env: dict[str, str] | None = None, cwd: str | None = None) -> list[str]:
        wrapper_calls.append((list(cmd), env, cwd))
        return wrap_command_for_sandbox(cmd, env=env, cwd=cwd)

    popen_calls: list[tuple[Any, dict[str, Any]]] = []
    mock_drain = MagicMock()
    monkeypatch.setattr(ms, "start_stderr_drain", mock_drain)

    mock_proc = MagicMock()
    mock_proc.pid = 1234
    mock_proc.stdin = MagicMock()
    mock_proc.stdout = MagicMock()
    mock_proc.stderr = MagicMock()  # truthy MagicMock

    def fake_popen(cmd: Any, **kwargs: Any) -> MagicMock:
        popen_calls.append((cmd, kwargs))
        return mock_proc

    monkeypatch.setattr(ms.subprocess, "Popen", fake_popen)

    sub = ms.ManagedSubprocess(
        ["/bin/harper-ls", "--stdio"],
        command_wrapper=recording_wrapper,
        inherit_env=True,
        new_session=False,
        stderr=subprocess.DEVNULL,
        register=False,
        log_start_failure=False,
    )
    sub.start()

    assert len(wrapper_calls) == 1
    assert wrapper_calls[0][1] is None
    assert wrapper_calls[0][2] is None

    assert len(popen_calls) == 1
    _cmd, kwargs = popen_calls[0]
    assert kwargs.get("env") is None
    assert kwargs.get("cwd") is None
    assert kwargs.get("stdin") is subprocess.PIPE
    assert kwargs.get("stdout") is subprocess.PIPE
    assert kwargs.get("stderr") is subprocess.DEVNULL
    assert kwargs.get("bufsize") == 0
    assert "start_new_session" not in kwargs
    mock_drain.assert_not_called()
    assert sub not in ms._instances

    monkeypatch.setattr(sandbox_mod, "detect_sandbox", lambda: "flatpak")
    popen_calls.clear()
    sub2 = ms.ManagedSubprocess(
        ["/bin/harper-ls", "--stdio"],
        command_wrapper=recording_wrapper,
        inherit_env=True,
        new_session=False,
        stderr=subprocess.DEVNULL,
        register=False,
        log_start_failure=False,
    )
    sub2.start()
    assert len(popen_calls) == 1
    cmd2, _kwargs2 = popen_calls[0]
    assert cmd2 == ["flatpak-spawn", "--host", "/bin/harper-ls", "--stdio"]
    assert "--clear-env" not in cmd2
    assert not any(arg.startswith("--env=") for arg in cmd2)

    popen_calls.clear()
    if sys.platform != "win32":
        sub3 = ms.ManagedSubprocess(["echo"])
        sub3.start()
        try:
            assert popen_calls[0][1].get("start_new_session") is True
            env_val = popen_calls[0][1].get("env")
            assert isinstance(env_val, dict)
            assert "PATH" in env_val
        finally:
            _reap_for_test(sub3)
    else:
        for ns in (True, False):
            popen_calls.clear()
            sub_win = ms.ManagedSubprocess(["echo"], new_session=ns)
            sub_win.start()
            try:
                assert "start_new_session" not in popen_calls[0][1]
                assert popen_calls[0][1].get("creationflags") == subprocess.CREATE_NO_WINDOW
            finally:
                _reap_for_test(sub_win)


@pytest.mark.parametrize(
    ("ctor_kwargs", "stub_drain"),
    [
        pytest.param({"stderr": subprocess.DEVNULL}, False, id="devnull"),
        pytest.param({}, True, id="default_stderr"),
    ],
)
def test_register_false_skipped_by_shutdown_all(
    monkeypatch: pytest.MonkeyPatch, ctor_kwargs: dict[str, Any], stub_drain: bool
) -> None:
    import plugin.framework.managed_subprocess as ms

    def _make_mock_proc(pid: int) -> MagicMock:
        proc = MagicMock()
        proc.pid = pid
        proc.stdin = MagicMock()
        proc.stdout = MagicMock()
        proc.stderr = MagicMock() if stub_drain else None
        proc.poll.return_value = None if stub_drain else 0
        return proc

    unreg_proc = _make_mock_proc(2222)
    reg_proc = _make_mock_proc(2223)
    procs = iter([unreg_proc, reg_proc])

    monkeypatch.setattr(ms.subprocess, "Popen", lambda *a, **kw: next(procs))
    if stub_drain:
        monkeypatch.setattr(ms, "start_stderr_drain", lambda *a, **kw: MagicMock())

    inst_unregistered = ms.ManagedSubprocess(["echo"], register=False, **ctor_kwargs)
    inst_unregistered.start()
    assert inst_unregistered not in ms._instances
    ms.shutdown_all()
    unreg_proc.kill.assert_not_called()

    inst_registered = ms.ManagedSubprocess(["echo"], **ctor_kwargs)
    inst_registered.start()
    assert inst_registered in ms._instances
    _reap_for_test(inst_registered)
    assert inst_registered not in ms._instances


@pytest.mark.parametrize(
    ("cmd", "ctor_kwargs", "exc_cls", "exc_args"),
    [
        pytest.param(["fail"], {"stderr": subprocess.DEVNULL}, OSError, ("exec failed",), id="stderr_devnull"),
        pytest.param(["python", "editor_main.py"], {"drain_stderr": False}, OSError, ("exec failed",), id="drain_stderr_false"),
        pytest.param(["dummy"], {}, FileNotFoundError, (2, "No such file", "missing"), id="default_stderr_file_not_found"),
    ],
)
def test_log_start_failure_false_reraises_original(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    cmd: list[str],
    ctor_kwargs: dict[str, Any],
    exc_cls: type[OSError],
    exc_args: tuple[Any, ...],
) -> None:
    import re
    import plugin.framework.managed_subprocess as ms

    err = exc_cls(*exc_args)
    err_re = re.escape(str(err))

    def fail_popen(*a: Any, **kw: Any) -> None:
        raise err

    monkeypatch.setattr(ms.subprocess, "Popen", fail_popen)
    if "stderr" not in ctor_kwargs and ctor_kwargs.get("drain_stderr", True):
        monkeypatch.setattr(ms, "start_stderr_drain", lambda *a, **kw: MagicMock())

    sub_quiet = ms.ManagedSubprocess(cmd, log_start_failure=False, **ctor_kwargs)
    with caplog.at_level("ERROR", logger="plugin.framework.managed_subprocess"):
        with pytest.raises(exc_cls, match=f"^{err_re}$"):
            sub_quiet.start()
    assert sub_quiet.popen is None
    assert sub_quiet not in ms._instances
    assert "Failed to start managed subprocess" not in caplog.text

    caplog.clear()
    sub_loud = ms.ManagedSubprocess(cmd, **ctor_kwargs)
    with caplog.at_level("ERROR", logger="plugin.framework.managed_subprocess"):
        with pytest.raises(RuntimeError, match=f"^Failed to start managed subprocess: {err_re}$"):
            sub_loud.start()
    assert sub_loud not in ms._instances
    assert "Failed to start managed subprocess" in caplog.text

    mock_proc = MagicMock()
    mock_proc.pid = 3333
    mock_proc.stdin = MagicMock()
    mock_proc.stdout = MagicMock()
    mock_proc.stderr = MagicMock() if "stderr" not in ctor_kwargs else None
    mock_proc.poll.return_value = None
    monkeypatch.setattr(ms.subprocess, "Popen", lambda *a, **kw: mock_proc)

    try:
        sub_quiet.start()
        assert sub_quiet.popen is mock_proc
        with pytest.raises(RuntimeError, match=r"^ManagedSubprocess is single-use$"):
            sub_quiet.start()
    finally:
        _reap_for_test(sub_quiet)


@pytest.mark.parametrize(
    "cmd, text, encoding, errors, stdin, stdout, stderr, env",
    [
        pytest.param(
            ["ffplay", "clip.wav"],
            False,
            None,
            None,
            None,
            subprocess.DEVNULL,
            subprocess.DEVNULL,
            {"WA_AUDIO_PATH": "clip.wav", "PATH": "/bin"},
            id="binary_devnull",
        ),
        pytest.param(
            ["piper", "--model", "m"],
            True,
            "utf-8",
            "replace",
            subprocess.PIPE,
            subprocess.PIPE,
            subprocess.PIPE,
            None,
            id="text_pipes",
        ),
    ],
)
def test_raw_spawn_forwards_contract_and_skips_worker_defaults(
    monkeypatch: pytest.MonkeyPatch,
    cmd: list[str],
    text: bool,
    encoding: str | None,
    errors: str | None,
    stdin: Any,
    stdout: Any,
    stderr: Any,
    env: dict[str, str] | None,
) -> None:
    import plugin.framework.managed_subprocess as ms

    captured_cmds: list[list[str]] = []
    captured_kwargs: list[dict[str, Any]] = []
    fake_proc = MagicMock()
    fake_proc.pid = 4242
    fake_proc.poll.return_value = None

    def fake_popen(popen_cmd: list[str], **kwargs: Any) -> MagicMock:
        captured_cmds.append(list(popen_cmd))
        captured_kwargs.append(dict(kwargs))
        return fake_proc

    monkeypatch.setattr(ms.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(
        ms, "start_stderr_drain", lambda *args, **kwargs: pytest.fail("drain should not be called")
    )

    subp = ms.ManagedSubprocess(
        cmd,
        raw_spawn=True,
        stdin=stdin,
        stdout=stdout,
        stderr=stderr,
        text=text,
        encoding=encoding,
        errors=errors,
        env=env,
    )
    subp.start()

    assert captured_cmds[0] == cmd
    kwargs = captured_kwargs[0]
    assert kwargs["stdin"] is stdin
    assert kwargs["stdout"] is stdout
    assert kwargs["stderr"] is stderr
    assert kwargs["text"] is text
    assert kwargs["encoding"] == encoding
    assert kwargs["errors"] == errors
    assert kwargs["env"] is env
    assert kwargs["cwd"] is None
    assert "start_new_session" not in kwargs
    assert "bufsize" not in kwargs
    for flag_key, flag_val in ms.get_subprocess_creationflags().items():
        assert kwargs[flag_key] == flag_val
    assert subp not in ms._instances
    assert subp.stderr_tail is None
    assert subp.popen is fake_proc


def test_raw_spawn_popen_oserror_propagates_and_is_reusable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import plugin.framework.managed_subprocess as ms

    fake_proc = MagicMock()
    fake_proc.pid = 4242
    fake_proc.poll.return_value = None
    calls = 0

    def fake_popen(cmd: list[str], **kwargs: Any) -> MagicMock:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise FileNotFoundError(2, "No such file", "missing-bin")
        return fake_proc

    monkeypatch.setattr(ms.subprocess, "Popen", fake_popen)

    subp = ms.ManagedSubprocess(["missing-bin"], raw_spawn=True)
    with pytest.raises(FileNotFoundError):
        subp.start()

    assert subp.popen is None
    assert subp not in ms._instances

    # Second start returns and popen is the mock. No RuntimeError.
    subp.start()
    assert subp.popen is fake_proc


def test_raw_spawn_single_use_after_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import plugin.framework.managed_subprocess as ms

    fake_proc = MagicMock()
    fake_proc.pid = 4242
    fake_proc.poll.return_value = None
    popen_calls = 0

    def fake_popen(cmd: list[str], **kwargs: Any) -> MagicMock:
        nonlocal popen_calls
        popen_calls += 1
        return fake_proc

    monkeypatch.setattr(ms.subprocess, "Popen", fake_popen)

    subp = ms.ManagedSubprocess(["echo"], raw_spawn=True)
    subp.start()
    with pytest.raises(RuntimeError, match="ManagedSubprocess is single-use"):
        subp.start()

    assert popen_calls == 1


def test_raw_spawn_rejects_worker_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import plugin.framework.managed_subprocess as ms

    def fail_popen(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Popen should not be called")

    monkeypatch.setattr(ms.subprocess, "Popen", fail_popen)

    bad_cases: list[dict[str, Any]] = [
        {"command_wrapper": lambda cmd, **_k: cmd},
        {"scrub_env": True},
        {"optimize_pipes": True},
        {"base_env": {}},
        {"stderr_tail_chars": 100},
        {"stderr_name": "x"},
        {"stderr_name_with_pid": True},
    ]

    for extra_kwargs in bad_cases:
        subp = ms.ManagedSubprocess(["echo"], raw_spawn=True, **extra_kwargs)
        with pytest.raises(ValueError):
            subp.start()


def test_raw_spawn_drain_stderr_false_starts(monkeypatch: pytest.MonkeyPatch) -> None:
    import plugin.framework.managed_subprocess as ms

    fake_proc = MagicMock()
    fake_proc.pid = 1
    fake_proc.poll.return_value = None

    monkeypatch.setattr(ms.subprocess, "Popen", lambda *args, **kwargs: fake_proc)
    monkeypatch.setattr(ms, "start_stderr_drain", MagicMock(side_effect=AssertionError("should not drain")))

    managed = ms.ManagedSubprocess(["echo"], raw_spawn=True, drain_stderr=False)
    managed.start()

    assert managed.popen is fake_proc
    assert managed.stderr_tail is None
    assert managed not in ms._instances


@pytest.mark.parametrize(
    ("ctor_kwargs", "expect_drain", "expect_registered", "expected_kwargs"),
    [
        pytest.param(
            {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL},
            True,
            True,
            {"stdin": subprocess.DEVNULL, "stdout": subprocess.PIPE, "stderr": subprocess.PIPE, "bufsize": 0},
            id="honours_stdin_ignores_stdout",
        ),
        pytest.param(
            {},
            True,
            True,
            {"stdin": subprocess.PIPE, "stdout": subprocess.PIPE, "stderr": subprocess.PIPE, "text": None},
            id="default_binary",
        ),
        pytest.param(
            {"text": True, "register": False},
            True,
            False,
            {"text": True, "bufsize": 1},
            id="text_line_buffered_unregistered",
        ),
        pytest.param(
            {
                "base_env": {"PATH": "/bin", "LD_LIBRARY_PATH": "/lo"},
                "drain_stderr": False,
                "register": False,
                "log_start_failure": False,
                "label": "monaco-editor",
            },
            False,
            False,
            {
                "stdin": subprocess.PIPE,
                "stdout": subprocess.PIPE,
                "stderr": subprocess.PIPE,
                "bufsize": 0,
                "cwd": None,
                "env": {"PATH": "/bin", "LD_LIBRARY_PATH": "/lo"},
            },
            id="drain_stderr_false_skips_drain_and_register",
        ),
    ],
)
def test_worker_start_popen_kwargs_and_registration(
    monkeypatch: pytest.MonkeyPatch,
    ctor_kwargs: dict[str, Any],
    expect_drain: bool,
    expect_registered: bool,
    expected_kwargs: dict[str, Any],
) -> None:
    import plugin.framework.managed_subprocess as ms

    captured_kwargs: list[dict[str, Any]] = []
    drain_calls: list[bool] = []
    fake_proc = MagicMock()
    fake_proc.pid = 4242
    fake_proc.poll.return_value = None

    monkeypatch.setattr(ms.subprocess, "Popen", lambda _cmd, **kw: captured_kwargs.append(dict(kw)) or fake_proc)
    monkeypatch.setattr(ms, "start_stderr_drain", lambda *_a, **_kw: drain_calls.append(True) or MagicMock())

    subp = ms.ManagedSubprocess(["dummy"], **ctor_kwargs)
    try:
        subp.start()
        assert len(drain_calls) == int(expect_drain)
        assert (subp in ms._instances) is expect_registered

        kwargs = captured_kwargs[0]
        for key, val in expected_kwargs.items():
            assert kwargs.get(key) == val
        if sys.platform == "win32":
            assert "creationflags" in kwargs
            assert "start_new_session" not in kwargs
        else:
            assert kwargs.get("start_new_session") is True
            assert "creationflags" not in kwargs
    finally:
        _reap_for_test(subp)


def test_worker_start_copies_base_env(monkeypatch: pytest.MonkeyPatch) -> None:
    import plugin.framework.managed_subprocess as ms

    captured_kwargs: list[dict[str, Any]] = []
    fake_proc = MagicMock()
    fake_proc.pid = 4242
    fake_proc.poll.return_value = None
    monkeypatch.setattr(ms.subprocess, "Popen", lambda _cmd, **kw: captured_kwargs.append(dict(kw)) or fake_proc)
    monkeypatch.setattr(ms, "start_stderr_drain", lambda *_a, **_kw: MagicMock())

    base_env = {"PATH": "/bin"}
    subp = ms.ManagedSubprocess(["dummy"], base_env=base_env, register=False)
    subp.start()
    assert captured_kwargs[0]["env"] == base_env
    assert captured_kwargs[0]["env"] is not base_env


def test_managed_subprocess_run_forwards_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import plugin.framework.managed_subprocess as ms
    from plugin.framework.subprocess_env import get_subprocess_creationflags

    captured_args: list[Any] = []
    captured_kwargs: list[dict[str, Any]] = []

    def fake_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[Any]:
        captured_args.append(args)
        captured_kwargs.append(dict(kwargs))
        return subprocess.CompletedProcess(args[0], 0)

    monkeypatch.setattr(ms.subprocess, "run", fake_run)

    res = ms.ManagedSubprocess.run(
        ["spd-say", "-S"],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=2,
    )
    assert res.returncode == 0
    assert captured_args[0] == (["spd-say", "-S"],)
    kwargs = captured_kwargs[0]
    assert kwargs["stdout"] == subprocess.DEVNULL
    assert kwargs["stderr"] == subprocess.DEVNULL
    assert kwargs["timeout"] == 2
    assert kwargs["check"] is False
    for flag_key, flag_val in get_subprocess_creationflags().items():
        assert kwargs[flag_key] == flag_val
    assert "start_new_session" not in kwargs


def test_log_start_failure_false_after_popen_reraises_original(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import logging
    import plugin.framework.managed_subprocess as ms

    mock_proc = MagicMock()
    mock_proc.pid = 4242
    mock_proc.stdin = MagicMock()
    mock_proc.stdout = MagicMock()
    mock_proc.stderr = MagicMock()

    monkeypatch.setattr(ms.subprocess, "Popen", lambda *a, **kw: mock_proc)

    def raise_drain(*a: Any, **kw: Any) -> Any:
        raise RuntimeError("drain failed")

    monkeypatch.setattr(ms, "start_stderr_drain", raise_drain)
    killed: list[Any] = []
    monkeypatch.setattr(ms, "kill_process_tree", lambda proc: killed.append(proc))

    inst = ms.ManagedSubprocess(["dummy"], log_start_failure=False)
    with caplog.at_level(logging.ERROR, logger="plugin.framework.managed_subprocess"):
        with pytest.raises(RuntimeError, match="^drain failed$"):
            inst.start()
    assert killed == [mock_proc]
    mock_proc.stdin.close.assert_called()
    mock_proc.stdout.close.assert_called()
    assert inst not in ms._instances
    assert "Failed to start managed subprocess" not in caplog.text


def test_stderr_name_with_pid_appends_pid(monkeypatch: pytest.MonkeyPatch) -> None:
    import plugin.framework.managed_subprocess as ms

    mock_proc = MagicMock()
    mock_proc.pid = 99
    mock_proc.stdin = MagicMock()
    mock_proc.stdout = MagicMock()
    mock_proc.stderr = MagicMock()

    drain_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(ms.subprocess, "Popen", lambda *a, **kw: mock_proc)
    monkeypatch.setattr(
        ms, "start_stderr_drain", lambda stream, **kw: drain_calls.append(dict(kw)) or MagicMock()
    )

    inst1 = ms.ManagedSubprocess(
        ["dummy"],
        stderr_name="audio-rec-stderr",
        stderr_name_with_pid=True,
        register=False,
    )
    inst1.start()
    assert drain_calls[0]["name"] == "audio-rec-stderr-99"

    inst2 = ms.ManagedSubprocess(
        ["dummy"],
        stderr_name="fixed",
        stderr_name_with_pid=False,
        register=False,
    )
    inst2.start()
    assert drain_calls[1]["name"] == "fixed"


def test_drain_stderr_false_rejects_tail_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    import plugin.framework.managed_subprocess as ms

    monkeypatch.setattr(ms.subprocess, "Popen", MagicMock(side_effect=AssertionError("Popen called")))

    settings_to_test = [
        {"stderr_tail_chars": 100},
        {"stderr_name": "editor-stderr"},
        {"stderr_tail_chars": 100, "stderr_name": "editor-stderr"},
    ]
    for kwargs in settings_to_test:
        with pytest.raises(ValueError):
            ms.ManagedSubprocess(["echo"], drain_stderr=False, **kwargs)


def test_managed_subprocess_run_capture_output(monkeypatch: pytest.MonkeyPatch) -> None:
    import plugin.framework.managed_subprocess as ms

    recorded_args: list[Any] = []
    recorded_kwargs: dict[str, Any] = {}

    def fake_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[Any]:
        recorded_args.append(args)
        recorded_kwargs.update(kwargs)
        return subprocess.CompletedProcess(args[0], 0, stdout="ok", stderr="")

    monkeypatch.setattr(ms.subprocess, "run", fake_run)
    monkeypatch.setattr(ms, "start_stderr_drain", MagicMock(side_effect=AssertionError("drain")))
    monkeypatch.setattr(ms.subprocess, "Popen", MagicMock(side_effect=AssertionError("Popen")))

    instances_snapshot = set(ms._instances)

    env = {"PATH": "/bin"}
    cmd = ["/venv/bin/python", "-c", "import webview"]
    res = ms.ManagedSubprocess.run(
        cmd,
        capture_output=True,
        timeout=30,
        env=env,
        text=True,
    )
    assert res.returncode == 0
    assert res.stdout == "ok"
    assert recorded_args == [(cmd,)]
    assert recorded_kwargs["capture_output"] is True
    assert recorded_kwargs["timeout"] == 30
    assert recorded_kwargs["env"] is env
    assert recorded_kwargs["text"] is True
    assert recorded_kwargs["stdin"] is None
    assert recorded_kwargs["stdout"] is None
    assert recorded_kwargs["stderr"] is None

    from plugin.framework.subprocess_env import get_subprocess_creationflags

    assert recorded_kwargs.get("creationflags") == get_subprocess_creationflags().get("creationflags")

    assert ms._instances == instances_snapshot

    # A second call whose patched subprocess.run raises subprocess.TimeoutExpired(cmd=["x"], timeout=30)
    # propagates that type (not RuntimeError).
    monkeypatch.setattr(
        ms.subprocess,
        "run",
        MagicMock(side_effect=subprocess.TimeoutExpired(cmd=["x"], timeout=30)),
    )
    with pytest.raises(subprocess.TimeoutExpired) as exc_info:
        ms.ManagedSubprocess.run(["x"], timeout=30)
    assert not isinstance(exc_info.value, RuntimeError)


def test_reaper_start_failure_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    import queue
    import plugin.framework.managed_subprocess as ms

    monkeypatch.setattr(ms, "_reaper_started", False)

    def fail_run(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("start reaper failed")

    monkeypatch.setattr(ms, "run_in_background", fail_run)

    managed = ms.ManagedSubprocess.wrap_existing(object(), label="retry-reaper")
    try:
        with pytest.raises(RuntimeError, match="start reaper failed"):
            managed.abandon()

        assert ms._reaper_started is False
        assert ms._reaper_queue.qsize() == 1

        calls: list[dict[str, object]] = []

        def spy_run(*_args: object, **kwargs: object) -> None:
            calls.append(dict(kwargs))

        monkeypatch.setattr(ms, "run_in_background", spy_run)

        managed.abandon()
        assert len(calls) == 1
        assert calls[0] == {"name": "managed-subprocess-reaper", "dedicated": True, "daemon": True}
        assert ms._reaper_started is True
    finally:
        while True:
            try:
                ms._reaper_queue.get_nowait()
            except queue.Empty:
                break


def test_managed_subprocess_run_capture_output_passes_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    import plugin.framework.managed_subprocess as ms

    recorded_args: list[Any] = []
    recorded_kwargs: dict[str, Any] = {}

    def fake_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[Any]:
        recorded_args.append(args)
        recorded_kwargs.update(kwargs)
        return subprocess.CompletedProcess(args[0], 0, stdout="ok", stderr="")

    monkeypatch.setattr(ms.subprocess, "run", fake_run)

    cmd = ["/venv/bin/python", "-c", "import sys"]
    res = ms.ManagedSubprocess.run(
        cmd,
        capture_output=True,
        stdin=subprocess.DEVNULL,
    )
    assert res.returncode == 0
    assert recorded_kwargs["capture_output"] is True
    assert recorded_kwargs["stdin"] is subprocess.DEVNULL


def test_managed_subprocess_run_capture_output_rejects_stdout_and_stderr(monkeypatch: pytest.MonkeyPatch) -> None:
    import plugin.framework.managed_subprocess as ms

    def fail_run(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("subprocess.run should not be called")

    monkeypatch.setattr(ms.subprocess, "run", fail_run)

    with pytest.raises(ValueError):
        ms.ManagedSubprocess.run(["x"], capture_output=True, stdout=subprocess.PIPE)

    with pytest.raises(ValueError):
        ms.ManagedSubprocess.run(["x"], capture_output=True, stderr=subprocess.PIPE)


def _pid_is_alive(pid: int) -> bool:
    from pathlib import Path

    stat_path = Path(f"/proc/{pid}/stat")
    if stat_path.is_file():
        try:
            stat = stat_path.read_text()
            close_paren = stat.rfind(")")
            if close_paren >= 0 and close_paren + 2 < len(stat) and stat[close_paren + 2] == "Z":
                return False
        except OSError:
            pass
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def test_managed_subprocess_run_kill_tree_timeout_calls_kill_process_tree(monkeypatch):
    import plugin.framework.managed_subprocess as ms

    proc = MagicMock()
    proc.poll.return_value = None
    proc.__enter__ = MagicMock(return_value=proc)
    proc.__exit__ = MagicMock(return_value=None)
    killed: list[object] = []
    monkeypatch.setattr(ms.subprocess, "Popen", lambda *a, **k: proc)
    monkeypatch.setattr(ms, "kill_process_tree", lambda p: killed.append(p))
    proc.communicate.side_effect = [
        subprocess.TimeoutExpired(cmd=["x"], timeout=1.0),
        ("", ""),
    ]

    with pytest.raises(subprocess.TimeoutExpired):
        ms.ManagedSubprocess.run(["x"], capture_output=True, timeout=1.0, kill_tree=True)

    assert killed == [proc]


@pytest.mark.skipif(sys.platform != "linux", reason="POSIX process group integration")
def test_managed_subprocess_run_kill_tree_timeout_reaps_grandchild(tmp_path):
    """Timeout must not leave a forked grandchild running (soffice.bin pattern)."""

    import plugin.framework.managed_subprocess as ms

    pidfile = tmp_path / "grandchild.pid"
    code = (
        "import os, sys, time\n"
        f"pidfile = {str(pidfile)!r}\n"
        "pid = os.fork()\n"
        "if pid == 0:\n"
        "    time.sleep(300)\n"
        "    os._exit(0)\n"
        "with open(pidfile, 'w') as f:\n"
        "    f.write(str(pid))\n"
        "sys.stdout.write(str(pid) + '\\n')\n"
        "sys.stdout.flush()\n"
        "time.sleep(300)\n"
    )
    gpid: int | None = None
    try:
        with pytest.raises(subprocess.TimeoutExpired) as exc_info:
            ms.ManagedSubprocess.run(
                [sys.executable, "-c", code],
                capture_output=True,
                text=True,
                timeout=3.0,
                kill_tree=True,
            )
        out = exc_info.value.output or ""
        assert out.strip(), f"expected grandchild pid on stdout before timeout, got {out!r}"
        gpid = int(out.strip().splitlines()[0])
        assert pidfile.read_text().strip() == str(gpid)
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and _pid_is_alive(gpid):
            time.sleep(0.05)
        assert not _pid_is_alive(gpid), f"grandchild {gpid} survived convert-style timeout"
    finally:
        if gpid is not None and _pid_is_alive(gpid):
            try:
                os.kill(gpid, signal.SIGKILL)
            except OSError:
                pass


def test_managed_subprocess_die_with_parent(monkeypatch: pytest.MonkeyPatch) -> None:
    from plugin.framework.warm_child import supervisor

    with pytest.raises(ValueError, match="raw_spawn does not support die_with_parent"):
        ManagedSubprocess(["dummy"], raw_spawn=True, die_with_parent=True)

    recorded_kwargs: dict[str, Any] = {}

    class FakePopen:
        def __init__(self, cmd: list[str], **kwargs: Any) -> None:
            recorded_kwargs.update(kwargs)
            self.pid = 9999
            self.stdin = None
            self.stdout = None
            self.stderr = None

        def poll(self) -> int | None:
            return 0

        def wait(self, timeout: float | None = None) -> int:
            return 0

    monkeypatch.setattr(subprocess, "Popen", FakePopen)
    monkeypatch.setattr(sys, "platform", "linux")

    # start() from a non-spawner thread routes through supervisor.spawn on Linux
    spawn_calls: list[ManagedSubprocess] = []
    orig_spawn = supervisor.spawn

    def tracked_spawn(managed: ManagedSubprocess, timeout_sec: float | None = None) -> None:
        spawn_calls.append(managed)
        return orig_spawn(managed, timeout_sec)

    monkeypatch.setattr(supervisor, "spawn", tracked_spawn)
    proc_routed = ManagedSubprocess(
        [sys.executable, "-c", "pass"],
        die_with_parent=True,
        drain_stderr=False,
        register=False,
    )
    proc_routed.start()
    assert len(spawn_calls) == 1
    assert proc_routed.is_alive() is False
    assert "preexec_fn" in recorded_kwargs

    # When libc cannot be loaded, start() still spawns the child without preexec_fn
    recorded_kwargs.clear()
    spawn_calls.clear()
    import ctypes
    import plugin.framework.child_runtime as cr

    def _raise_cdll(*unused_args: object, **unused_kwargs: object) -> None:
        raise OSError("libc.so.6 not found")

    monkeypatch.setattr(cr, "_prctl", None)
    monkeypatch.setattr(ctypes, "CDLL", _raise_cdll)
    proc_cdll = ManagedSubprocess(["dummy"], die_with_parent=True, drain_stderr=False, register=False)
    proc_cdll.start()
    assert "preexec_fn" not in recorded_kwargs

    # Non-Linux spawns directly without supervisor.spawn or preexec_fn
    recorded_kwargs.clear()

    def forbid_spawn(*args: object, **kwargs: object) -> None:
        raise AssertionError("supervisor.spawn must not run off Linux")

    monkeypatch.setattr(supervisor, "spawn", forbid_spawn)
    monkeypatch.setattr(subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    monkeypatch.setattr(sys, "platform", "win32")
    proc_win = ManagedSubprocess(["dummy"], die_with_parent=True, drain_stderr=False, register=False)
    proc_win.start()
    assert "preexec_fn" not in recorded_kwargs

