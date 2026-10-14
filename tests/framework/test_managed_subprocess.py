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
from typing import Any
from unittest.mock import MagicMock

import pytest

from plugin.framework.managed_subprocess import (
    ManagedSubprocess,
    shutdown_all,
    _instances,
)


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


def test_managed_subprocess_explicit_start_and_cwd(tmp_path):
    cwd = str(tmp_path)
    env = {"TEST_VAR": "hello"}

    cmd = [sys.executable, "-c", "import os; print(os.getcwd()); print(os.environ.get('TEST_VAR'))"]

    proc = ManagedSubprocess(cmd, env=env, cwd=cwd)
    assert not proc.is_alive()

    proc.start()
    assert proc.is_alive()
    assert proc.stdin is not None
    assert proc.stdout is not None
    assert proc.stderr_tail is not None

    stdout = proc.stdout.read()
    proc.stdout.close()
    output = stdout.decode("utf-8").strip().splitlines()

    assert output[0].endswith(os.path.basename(cwd))
    assert output[1] == "hello"

    proc.close()
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
    proc.close()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups")
def test_managed_subprocess_tree_kill_grandchild():
    """ManagedSubprocess.close() must kill descendants even when leader already exited."""
    code = (
        "import os, sys, time\n"
        "pid = os.fork()\n"
        "if pid == 0:\n"
        "    time.sleep(120)\n"
        "    os._exit(0)\n"
        "sys.stdout.write('GRANDCHILD %s\\n' % pid)\n"
        "sys.stdout.flush()\n"
        "os._exit(0)\n"
    )
    proc = ManagedSubprocess([sys.executable, "-c", code])
    proc.start()
    gpid = None
    try:
        assert proc.stdout is not None
        line = proc.stdout.readline()
        text = line.decode()
        assert text.startswith("GRANDCHILD "), text
        gpid = int(text.split()[1])

        # Wait for the direct child (leader) to exit
        assert proc.popen is not None
        proc.popen.wait(timeout=5)
        assert proc.popen.poll() is not None
        assert pid_is_alive(gpid)

        # Bugfix 1: grace wait previously skipped tree kill when leader exited early.
        # close() must always kill the process tree.
        proc.close()

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and pid_is_alive(gpid):
            time.sleep(0.05)
        assert not pid_is_alive(gpid), f"grandchild pid {gpid} survived ManagedSubprocess.close()"
    finally:
        if gpid is not None and pid_is_alive(gpid):
            try:
                os.kill(gpid, signal.SIGKILL)
            except OSError:
                pass
        proc.close()


def test_managed_subprocess_idempotent_close():
    cmd = [sys.executable, "-c", "import time; time.sleep(10)"]
    proc = ManagedSubprocess(cmd)
    proc.start()

    assert proc.is_alive()
    assert proc in _instances

    proc.close()
    assert not proc.is_alive()
    assert proc not in _instances

    # Second close should be a no-op and not raise any errors
    proc.close()


def test_managed_subprocess_shutdown_registry():
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

    assert not proc1.is_alive()
    assert not proc2.is_alive()
    assert proc1 not in _instances
    assert proc2 not in _instances


def test_managed_subprocess_timeout_escalate_and_reap(monkeypatch):
    import plugin.framework.managed_subprocess as ms

    monkeypatch.setattr(ms, "_GRACE_KILL_TIMEOUT_SEC", 0.01)

    mock_proc = MagicMock()
    mock_proc.pid = 9999
    mock_proc.stdin = MagicMock()
    mock_proc.stdout = MagicMock()
    mock_proc.stderr = MagicMock()
    mock_proc.poll.return_value = None
    mock_proc.wait.side_effect = subprocess.TimeoutExpired(cmd="test", timeout=0.01)

    killed = []
    monkeypatch.setattr(ms, "kill_process_tree", lambda p: killed.append(p))

    enqueued = []
    monkeypatch.setattr(ms, "_enqueue_reaper", lambda p: enqueued.append(p))

    managed = ms.ManagedSubprocess(["dummy"])
    managed._proc = mock_proc
    ms._register_instance(managed)

    managed.close()

    assert killed == [mock_proc]
    assert enqueued == [mock_proc]

    # Second close should not re-enqueue
    managed.close()
    assert len(enqueued) == 1


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

    managed.close()
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
    managed1.close()
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
    managed2.close()
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
    managed3.close()
    env3 = captured_kw["env"]
    assert env3 == {"ONLY": "1"}

    # 4. command_wrapper
    managed4 = ms.ManagedSubprocess(
        ["orig", "arg"],
        command_wrapper=lambda cmd: ["wrapped", *cmd],
    )
    managed4.start()
    managed4.close()
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
    managed5.close()
    assert captured_cmd == ["orig", "arg2"]


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
