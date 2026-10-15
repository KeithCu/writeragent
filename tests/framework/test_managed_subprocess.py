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
    monkeypatch.setattr(ms, "_enqueue_reaper", lambda item: enqueued.append(item))

    managed = ms.ManagedSubprocess(["dummy"])
    managed._proc = mock_proc
    ms._register_instance(managed)

    managed.close()

    assert killed == [mock_proc]
    assert enqueued == [("close", managed)]
    mock_proc.stdout.close.assert_not_called()
    mock_proc.stderr.close.assert_called_once()

    # Second close should not re-enqueue
    managed.close()
    assert len(enqueued) == 1


def test_close_skips_stdout_and_drained_stderr(monkeypatch: pytest.MonkeyPatch) -> None:
    import plugin.framework.managed_subprocess as ms

    mock_proc = MagicMock()
    mock_proc.pid = 9998
    mock_proc.stdin = MagicMock()
    mock_proc.stdout = MagicMock()
    mock_proc.stderr = MagicMock()
    mock_proc.poll.return_value = None
    mock_proc.wait.side_effect = subprocess.TimeoutExpired(cmd="test", timeout=0.01)

    mock_drain = MagicMock()

    killed: list[Any] = []
    monkeypatch.setattr(ms, "kill_process_tree", lambda p: killed.append(p))

    enqueued: list[Any] = []
    monkeypatch.setattr(ms, "_enqueue_reaper", lambda item: enqueued.append(item))

    managed = ms.ManagedSubprocess(["dummy"])
    managed._proc = mock_proc
    managed._stderr_drain = mock_drain
    ms._register_instance(managed)

    managed.close()

    mock_proc.stdin.close.assert_called_once()
    mock_proc.stdout.close.assert_not_called()
    mock_proc.stderr.close.assert_not_called()
    mock_drain.finish_text.assert_called_once_with(timeout=1.0)
    assert killed == [mock_proc]
    assert enqueued == [("close", managed)]
    assert managed not in ms._instances

    # Second close() does not call finish_text again
    managed.close()
    assert mock_drain.finish_text.call_count == 1


def test_close_closes_stderr_when_no_drain(monkeypatch: pytest.MonkeyPatch) -> None:
    import plugin.framework.managed_subprocess as ms

    mock_proc = MagicMock()
    mock_proc.pid = 9997
    mock_proc.stdin = MagicMock()
    mock_proc.stdout = MagicMock()
    mock_proc.stderr = MagicMock()
    mock_proc.poll.return_value = 0

    killed: list[Any] = []
    monkeypatch.setattr(ms, "kill_process_tree", lambda p: killed.append(p))

    managed = ms.ManagedSubprocess(["dummy"])
    managed._proc = mock_proc
    managed._stderr_drain = None
    ms._register_instance(managed)

    managed.close()

    mock_proc.stderr.close.assert_called_once()
    mock_proc.stdout.close.assert_not_called()


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
        command_wrapper=lambda cmd, **_kwargs: ["wrapped", *cmd],
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
    managed.close()

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
        proc.close()


def test_optimize_pipes_is_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    import plugin.framework.managed_subprocess as ms

    mock_proc = MagicMock()
    mock_proc.pid = 123
    mock_proc.stdin = MagicMock()
    mock_proc.stdout = MagicMock()
    mock_proc.stderr = MagicMock()
    monkeypatch.setattr(ms.subprocess, "Popen", lambda *a, **kw: mock_proc)
    monkeypatch.setattr(ms, "start_stderr_drain", lambda *a, **kw: MagicMock())

    opt_calls: list[object] = []
    monkeypatch.setattr(ms, "optimize_popen_pipes", lambda p: opt_calls.append(p))

    # Default does not call it
    proc_default = ms.ManagedSubprocess(["dummy"])
    proc_default.start()
    try:
        assert opt_calls == []
    finally:
        proc_default.close()

    # optimize_pipes=True calls it once
    proc_opt = ms.ManagedSubprocess(["dummy"], optimize_pipes=True)
    proc_opt.start()
    try:
        assert opt_calls == [mock_proc]
    finally:
        proc_opt.close()


def test_stderr_name_and_cap_are_forwarded(monkeypatch: pytest.MonkeyPatch) -> None:
    import plugin.framework.managed_subprocess as ms

    mock_proc = MagicMock()
    mock_proc.pid = 5432
    mock_proc.stdin = MagicMock()
    mock_proc.stdout = MagicMock()
    mock_proc.stderr = MagicMock()
    monkeypatch.setattr(ms.subprocess, "Popen", lambda *a, **kw: mock_proc)

    drain_mock = MagicMock()
    monkeypatch.setattr(ms, "start_stderr_drain", drain_mock)

    # Default instance
    proc_default = ms.ManagedSubprocess(["dummy"])
    proc_default.start()
    try:
        drain_mock.assert_called_once_with(mock_proc.stderr, name="managed-stderr-5432")
    finally:
        proc_default.close()

    drain_mock.reset_mock()

    # Custom name and tail chars
    proc_custom = ms.ManagedSubprocess(
        ["dummy"],
        stderr_name="wa-formula-worker-w2-stderr",
        stderr_tail_chars=65536,
    )
    proc_custom.start()
    try:
        drain_mock.assert_called_once_with(
            mock_proc.stderr,
            name="wa-formula-worker-w2-stderr",
            max_tail_chars=65536,
        )
    finally:
        proc_custom.close()

    drain_mock.reset_mock()

    proc_pid = ms.ManagedSubprocess(
        ["dummy"],
        stderr_name="venv-stderr",
        stderr_name_with_pid=True,
        register=False,
    )
    proc_pid.start()
    try:
        drain_mock.assert_called_once_with(
            mock_proc.stderr,
            name="venv-stderr-5432",
        )
    finally:
        proc_pid.close()


def test_abandoned_reaper_pauses_before_requeue(monkeypatch: pytest.MonkeyPatch) -> None:
    import plugin.framework.managed_subprocess as ms

    sleeps: list[float] = []
    enqueued: list[tuple[str, object]] = []
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
    assert enqueued == [("abandon", managed)]


def test_reaper_stops_after_max_passes(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import logging
    import plugin.framework.managed_subprocess as ms

    sleeps: list[float] = []
    enqueued: list[tuple[str, object]] = []
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
        assert enqueued == [("abandon", managed)] * (ms._REAPER_MAX_PASSES - 1)
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
        with pytest.raises(ValueError, match=r"^inherit_env cannot be combined with env, base_env, or scrub_env$"):
            ManagedSubprocess(["x"], inherit_env=True, **kwargs)


def test_stderr_rejects_non_pipe_non_devnull() -> None:
    for val in (subprocess.STDOUT, 1):
        with pytest.raises(ValueError, match=r"^stderr must be subprocess.PIPE or subprocess.DEVNULL$"):
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
            sub3.close()
    else:
        for ns in (True, False):
            popen_calls.clear()
            sub_win = ms.ManagedSubprocess(["echo"], new_session=ns)
            sub_win.start()
            try:
                assert "start_new_session" not in popen_calls[0][1]
                assert popen_calls[0][1].get("creationflags") == subprocess.CREATE_NO_WINDOW
            finally:
                sub_win.close()


def test_register_false_skipped_by_shutdown_all(monkeypatch: pytest.MonkeyPatch) -> None:
    import plugin.framework.managed_subprocess as ms

    mock_proc = MagicMock()
    mock_proc.pid = 2222
    mock_proc.stdin = MagicMock()
    mock_proc.stdout = MagicMock()
    mock_proc.stderr = None
    mock_proc.poll.return_value = 0

    monkeypatch.setattr(ms.subprocess, "Popen", lambda *a, **kw: mock_proc)

    inst_unregistered = ms.ManagedSubprocess(["echo"], stderr=subprocess.DEVNULL, register=False)
    inst_unregistered.start()
    assert inst_unregistered not in ms._instances

    close_mock = MagicMock()
    monkeypatch.setattr(inst_unregistered, "close", close_mock)
    ms.shutdown_all()
    close_mock.assert_not_called()

    inst_registered = ms.ManagedSubprocess(["echo"], stderr=subprocess.DEVNULL)
    inst_registered.start()
    assert inst_registered in ms._instances
    inst_registered.close()
    assert inst_registered not in ms._instances


def test_log_start_failure_false_reraises_original(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import plugin.framework.managed_subprocess as ms

    def fail_popen(*a: Any, **kw: Any) -> None:
        raise OSError("exec failed")

    monkeypatch.setattr(ms.subprocess, "Popen", fail_popen)

    sub_quiet = ms.ManagedSubprocess(["fail"], log_start_failure=False, stderr=subprocess.DEVNULL)
    with caplog.at_level("ERROR", logger="plugin.framework.managed_subprocess"):
        with pytest.raises(OSError, match=r"^exec failed$"):
            sub_quiet.start()
    assert sub_quiet.popen is None
    assert "Failed to start managed subprocess" not in caplog.text

    caplog.clear()
    sub_loud = ms.ManagedSubprocess(["fail"], stderr=subprocess.DEVNULL)
    with caplog.at_level("ERROR", logger="plugin.framework.managed_subprocess"):
        with pytest.raises(RuntimeError, match=r"^Failed to start managed subprocess: exec failed$"):
            sub_loud.start()
    assert "Failed to start managed subprocess" in caplog.text

    mock_proc = MagicMock()
    mock_proc.pid = 3333
    mock_proc.stdin = MagicMock()
    mock_proc.stdout = MagicMock()
    mock_proc.stderr = None
    monkeypatch.setattr(ms.subprocess, "Popen", lambda *a, **kw: mock_proc)

    sub_twice = ms.ManagedSubprocess(["ok"], log_start_failure=False, stderr=subprocess.DEVNULL, register=False)
    sub_twice.start()
    with pytest.raises(RuntimeError, match=r"ManagedSubprocess is single-use"):
        sub_twice.start()


def test_raw_spawn_passes_stdio_env_and_skips_worker_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import plugin.framework.managed_subprocess as ms

    captured_cmds: list[list[str]] = []
    captured_kwargs: list[dict[str, Any]] = []
    fake_proc = MagicMock()
    fake_proc.pid = 4242
    fake_proc.poll.return_value = None

    def fake_popen(cmd: list[str], **kwargs: Any) -> MagicMock:
        captured_cmds.append(list(cmd))
        captured_kwargs.append(dict(kwargs))
        return fake_proc

    monkeypatch.setattr(ms.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(
        ms, "start_stderr_drain", lambda *args, **kwargs: pytest.fail("drain should not be called")
    )

    env = {"WA_AUDIO_PATH": "clip.wav", "PATH": "/bin"}
    subp = ms.ManagedSubprocess(
        ["ffplay", "clip.wav"],
        raw_spawn=True,
        stdin=None,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=False,
        encoding=None,
        errors=None,
        env=env,
    )
    subp.start()

    assert captured_cmds[0] == ["ffplay", "clip.wav"]
    kwargs = captured_kwargs[0]
    assert kwargs == {
        "stdin": None,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "text": False,
        "encoding": None,
        "errors": None,
        "env": env,
        "cwd": None,
    }
    assert kwargs["env"] is env
    assert "start_new_session" not in kwargs
    assert "creationflags" not in kwargs
    assert "bufsize" not in kwargs
    assert subp not in ms._instances
    assert subp.stderr_tail is None
    assert subp.popen is fake_proc


def test_raw_spawn_text_pipes_for_communicate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import plugin.framework.managed_subprocess as ms

    captured_cmds: list[list[str]] = []
    captured_kwargs: list[dict[str, Any]] = []
    fake_proc = MagicMock()
    fake_proc.pid = 4242
    fake_proc.poll.return_value = None

    def fake_popen(cmd: list[str], **kwargs: Any) -> MagicMock:
        captured_cmds.append(list(cmd))
        captured_kwargs.append(dict(kwargs))
        return fake_proc

    monkeypatch.setattr(ms.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(
        ms, "start_stderr_drain", lambda *args, **kwargs: pytest.fail("drain should not be called")
    )

    subp = ms.ManagedSubprocess(
        ["piper", "--model", "m"],
        raw_spawn=True,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=None,
    )
    subp.start()

    assert captured_cmds[0] == ["piper", "--model", "m"]
    kwargs = captured_kwargs[0]
    assert kwargs == {
        "stdin": subprocess.PIPE,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "env": None,
        "cwd": None,
    }
    assert kwargs["env"] is None
    assert kwargs["cwd"] is None
    assert subp not in ms._instances


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

    bad_cases: list[tuple[dict[str, Any], str]] = [
        ({"command_wrapper": lambda cmd, **_k: cmd}, "raw_spawn does not take a command_wrapper"),
        ({"scrub_env": True}, "raw_spawn does not scrub env"),
        ({"optimize_pipes": True}, "raw_spawn does not optimize pipes"),
        ({"base_env": {}}, "raw_spawn does not merge base_env"),
        ({"stderr_tail_chars": 100}, "raw_spawn does not drain stderr"),
        ({"stderr_name": "x"}, "raw_spawn does not drain stderr"),
        ({"stderr_name_with_pid": True}, "raw_spawn does not drain stderr"),
    ]

    for extra_kwargs, expected_msg in bad_cases:
        subp = ms.ManagedSubprocess(["echo"], raw_spawn=True, **extra_kwargs)
        with pytest.raises(ValueError) as excinfo:
            subp.start()
        assert str(excinfo.value) == expected_msg


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


def test_worker_start_ignores_raw_stdio_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import plugin.framework.managed_subprocess as ms

    captured_kwargs: list[dict[str, Any]] = []
    fake_proc = MagicMock()
    fake_proc.pid = 4242
    fake_proc.poll.return_value = None

    def fake_popen(cmd: list[str], **kwargs: Any) -> MagicMock:
        captured_kwargs.append(dict(kwargs))
        return fake_proc

    drain_called = False

    def fake_drain(*args: Any, **kwargs: Any) -> MagicMock:
        nonlocal drain_called
        drain_called = True
        return MagicMock()

    monkeypatch.setattr(ms.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(ms, "start_stderr_drain", fake_drain)

    subp = ms.ManagedSubprocess(
        ["echo"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
    )
    try:
        subp.start()
        assert drain_called
        kwargs = captured_kwargs[0]
        assert kwargs["stdin"] == subprocess.PIPE
        assert kwargs["stdout"] == subprocess.PIPE
        assert kwargs["stderr"] == subprocess.PIPE
        assert kwargs["bufsize"] == 0
        if sys.platform == "win32":
            assert "creationflags" in kwargs
            assert "start_new_session" not in kwargs
        else:
            assert kwargs["start_new_session"] is True
            assert "creationflags" not in kwargs
    finally:
        subp.close()


def test_managed_subprocess_run_is_subprocess_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import plugin.framework.managed_subprocess as ms
    from plugin.framework.worker_pool import get_subprocess_creationflags

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
    expected = {
        "stdin": None,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "env": None,
        "cwd": None,
        "text": False,
        "encoding": None,
        "errors": None,
        "timeout": 2,
        "check": False,
    }
    expected.update(get_subprocess_creationflags())
    assert captured_kwargs[0] == expected
    assert "start_new_session" not in captured_kwargs[0]


def test_default_start_stays_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    import plugin.framework.managed_subprocess as ms

    captured_kwargs: list[dict[str, Any]] = []
    mock_proc = MagicMock()
    mock_proc.pid = 4242
    mock_proc.stdin = MagicMock()
    mock_proc.stdout = MagicMock()
    mock_proc.stderr = MagicMock()
    mock_proc.poll.return_value = 0

    monkeypatch.setattr(ms.subprocess, "Popen", lambda *a, **kw: captured_kwargs.append(dict(kw)) or mock_proc)
    monkeypatch.setattr(ms, "start_stderr_drain", lambda *a, **kw: MagicMock())

    inst = ms.ManagedSubprocess(["dummy"])
    inst.start()
    try:
        kwargs = captured_kwargs[0]
        assert kwargs["bufsize"] == 0
        assert "text" not in kwargs
        assert "encoding" not in kwargs
        assert "errors" not in kwargs
        assert "universal_newlines" not in kwargs
        assert kwargs["stdin"] == subprocess.PIPE
        assert kwargs["stdout"] == subprocess.PIPE
        assert kwargs["stderr"] == subprocess.PIPE
    finally:
        inst.close()


def test_text_mode_is_line_buffered_and_unregistered(monkeypatch: pytest.MonkeyPatch) -> None:
    import plugin.framework.managed_subprocess as ms

    captured_kwargs: list[dict[str, Any]] = []
    mock_proc = MagicMock()
    mock_proc.pid = 4242
    mock_proc.stdin = MagicMock()
    mock_proc.stdout = MagicMock()
    mock_proc.stderr = MagicMock()

    monkeypatch.setattr(ms.subprocess, "Popen", lambda *a, **kw: captured_kwargs.append(dict(kw)) or mock_proc)
    monkeypatch.setattr(ms, "start_stderr_drain", lambda *a, **kw: MagicMock())

    inst = ms.ManagedSubprocess(["dummy"], text=True, register=False)
    inst.start()
    kwargs = captured_kwargs[0]
    assert kwargs["text"] is True
    assert kwargs["bufsize"] == 1
    assert "encoding" not in kwargs
    assert "errors" not in kwargs
    assert "universal_newlines" not in kwargs
    assert inst not in ms._instances


def test_register_false_omits_shutdown_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    import plugin.framework.managed_subprocess as ms

    mock_proc = MagicMock()
    mock_proc.pid = 4242
    mock_proc.stdin = MagicMock()
    mock_proc.stdout = MagicMock()
    mock_proc.stderr = MagicMock()

    monkeypatch.setattr(ms.subprocess, "Popen", lambda *a, **kw: mock_proc)
    monkeypatch.setattr(ms, "start_stderr_drain", lambda *a, **kw: MagicMock())

    inst = ms.ManagedSubprocess(["dummy"], register=False)
    inst.start()
    assert inst not in ms._instances
    ms.shutdown_all()
    assert inst._closed is False
    assert mock_proc.terminate.call_count == 0


def test_log_start_failure_false_popen_oserror(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import logging
    import plugin.framework.managed_subprocess as ms

    def fake_popen(*a: Any, **kw: Any) -> Any:
        raise FileNotFoundError(2, "No such file", "missing")

    monkeypatch.setattr(ms.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(ms, "start_stderr_drain", lambda *a, **kw: MagicMock())

    inst = ms.ManagedSubprocess(["dummy"], log_start_failure=False)
    with caplog.at_level(logging.ERROR, logger="plugin.framework.managed_subprocess"):
        with pytest.raises(FileNotFoundError):
            inst.start()
    assert inst._proc is None
    assert inst not in ms._instances
    assert "Failed to start managed subprocess" not in caplog.text


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






def test_worker_flag_defaults() -> None:
    from plugin.framework.managed_subprocess import ManagedSubprocess

    proc = ManagedSubprocess(["echo"])
    assert proc._drain_stderr is True
    assert proc._register is True
    assert proc._log_start_failure is True


def test_drain_stderr_false_skips_drain_and_register(monkeypatch: pytest.MonkeyPatch) -> None:
    import plugin.framework.managed_subprocess as ms

    recorded_cmd: list[str] = []
    recorded_kwargs: dict[str, Any] = {}

    mock_popen = MagicMock()
    mock_popen.poll.return_value = None
    mock_popen.stderr = MagicMock()

    def fake_popen(cmd: list[str], **kwargs: Any) -> MagicMock:
        recorded_cmd.extend(cmd)
        recorded_kwargs.update(kwargs)
        return mock_popen

    monkeypatch.setattr(ms.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(ms, "start_stderr_drain", MagicMock(side_effect=AssertionError("drain")))

    base_env = {"PATH": "/bin", "LD_LIBRARY_PATH": "/lo"}
    managed = ms.ManagedSubprocess(
        ["python", "editor_main.py"],
        base_env=base_env,
        drain_stderr=False,
        register=False,
        log_start_failure=False,
        label="monaco-editor",
    )
    managed.start()

    assert recorded_cmd == ["python", "editor_main.py"]
    assert recorded_kwargs["stdin"] == subprocess.PIPE
    assert recorded_kwargs["stdout"] == subprocess.PIPE
    assert recorded_kwargs["stderr"] == subprocess.PIPE
    assert recorded_kwargs["bufsize"] == 0
    assert recorded_kwargs["cwd"] is None
    assert recorded_kwargs["env"] == base_env
    assert recorded_kwargs["env"] is not base_env
    assert "text" not in recorded_kwargs
    assert "preexec_fn" not in recorded_kwargs

    if sys.platform == "win32":
        assert recorded_kwargs.get("creationflags") == subprocess.CREATE_NO_WINDOW
        assert "start_new_session" not in recorded_kwargs
    else:
        assert recorded_kwargs.get("start_new_session") is True
        assert "creationflags" not in recorded_kwargs

    assert managed.stderr_tail is None
    assert managed not in ms._instances

    monkeypatch.setattr(managed, "close", MagicMock(side_effect=AssertionError))
    ms.shutdown_all()


def test_log_start_failure_false_editor_host(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import plugin.framework.managed_subprocess as ms

    monkeypatch.setattr(ms.subprocess, "Popen", MagicMock(side_effect=OSError("exec failed")))

    managed = ms.ManagedSubprocess(
        ["python", "editor_main.py"],
        drain_stderr=False,
        log_start_failure=False,
        register=False,
    )
    with caplog.at_level("ERROR", logger="plugin.framework.managed_subprocess"):
        with pytest.raises(OSError, match=r"^exec failed$"):
            managed.start()

    assert managed.popen is None
    assert managed not in ms._instances
    assert not any(
        "Failed to start managed subprocess" in r.getMessage()
        for r in caplog.records
        if r.name == "plugin.framework.managed_subprocess"
    )

    # A second start() on that same instance, with Popen now returning a mock, succeeds (popen is that mock).
    mock_proc = MagicMock()
    mock_proc.poll.return_value = None
    monkeypatch.setattr(ms.subprocess, "Popen", MagicMock(return_value=mock_proc))
    managed.start()
    assert managed.popen is mock_proc

    # A different instance with default log_start_failure (and drain_stderr=False) raises RuntimeError
    # matching ^Failed to start managed subprocess: exec failed$, and that log line is present.
    monkeypatch.setattr(ms.subprocess, "Popen", MagicMock(side_effect=OSError("exec failed")))
    default_managed = ms.ManagedSubprocess(
        ["python", "editor_main.py"],
        drain_stderr=False,
        register=False,
    )
    caplog.clear()
    with caplog.at_level("ERROR", logger="plugin.framework.managed_subprocess"):
        with pytest.raises(RuntimeError, match=r"^Failed to start managed subprocess: exec failed$"):
            default_managed.start()
    assert any(
        "Failed to start managed subprocess" in r.getMessage()
        for r in caplog.records
        if r.name == "plugin.framework.managed_subprocess"
    )

    # Single-use still raises RuntimeError matching ManagedSubprocess is single-use
    # when log_start_failure=False and start() is called twice after a successful first start.
    with pytest.raises(RuntimeError, match=r"^ManagedSubprocess is single-use$"):
        managed.start()


def test_drain_stderr_false_rejects_tail_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    import plugin.framework.managed_subprocess as ms

    monkeypatch.setattr(ms.subprocess, "Popen", MagicMock(side_effect=AssertionError("Popen called")))

    settings_to_test = [
        {"stderr_tail_chars": 100},
        {"stderr_name": "editor-stderr"},
        {"stderr_tail_chars": 100, "stderr_name": "editor-stderr"},
    ]
    for kwargs in settings_to_test:
        with pytest.raises(
            ValueError,
            match=r"^drain_stderr=False does not take stderr_tail_chars or stderr_name$",
        ):
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
    assert type(recorded_kwargs["timeout"]) is int
    assert recorded_kwargs["env"] is env
    assert recorded_kwargs["text"] is True

    absent_keys = (
        "input",
        "shell",
        "start_new_session",
        "bufsize",
        "preexec_fn",
        "stdin",
        "stdout",
        "stderr",
    )
    for key in absent_keys:
        assert key not in recorded_kwargs

    from plugin.framework.worker_pool import get_subprocess_creationflags

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
