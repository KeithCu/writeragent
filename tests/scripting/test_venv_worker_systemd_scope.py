"""Venv worker launch in its own systemd user scope (mocked which/subprocess)."""
from __future__ import annotations

import contextlib
import subprocess
import sys
from unittest import mock

import pytest

from plugin.scripting import venv_worker as vw

CMD = ["/venv/bin/python", "/x/worker_harness.py"]


@pytest.fixture(autouse=True)
def _reset_cache(tmp_path):
    vw._systemd_run_for_worker.clear()
    vw._systemd_scope_disabled = False
    (tmp_path / "systemd").mkdir()
    (tmp_path / "systemd" / "private").touch()
    yield
    vw._systemd_run_for_worker.clear()
    vw._systemd_scope_disabled = False


def _env(tmp_path):
    return {"PATH": "/usr/bin", "XDG_RUNTIME_DIR": str(tmp_path)}


def _patches(which="/usr/bin/systemd-run", platform="linux", sandbox=None, rc=0):
    run = mock.Mock(return_value=subprocess.CompletedProcess([], rc, b"", b"Failed to connect to bus"))
    return run, [
        mock.patch.object(vw.shutil, "which", return_value=which),
        mock.patch.object(vw.sys, "platform", platform),
        mock.patch.object(vw, "detect_sandbox", return_value=sandbox),
        mock.patch.object(vw.subprocess, "run", run),
    ]


def _wrap(tmp_path, **kw):
    run, ps = _patches(**kw)
    for p in ps:
        p.start()
    try:
        return run, vw.wrap_worker_command(list(CMD), env=_env(tmp_path))
    finally:
        for p in ps:
            p.stop()


def test_wraps_in_scope_and_probes_once(tmp_path):
    run, ps = _patches()
    for p in ps:
        p.start()
    try:
        first = vw.wrap_worker_command(list(CMD), env=_env(tmp_path))
        second = vw.wrap_worker_command(list(CMD), env=_env(tmp_path))
    finally:
        for p in ps:
            p.stop()
    assert run.call_count == 1
    for out in (first, second):
        assert out[:5] == ["/usr/bin/systemd-run", "--user", "--scope", "--quiet", "--collect"]
        assert out[5].startswith("--unit=writeragent-worker-")
        assert out[6] == "--" and out[7:] == CMD
        assert not any(a.startswith(("-pMemory", "MemoryMax")) for a in out)
    assert first[5] != second[5]


@pytest.mark.parametrize(
    "kw",
    [{"which": None}, {"platform": "darwin"}, {"platform": "win32"}, {"sandbox": "flatpak"}, {"rc": 1}],
)
def test_falls_back_to_plain_command(tmp_path, kw):
    with mock.patch.object(vw, "wrap_command_for_sandbox", side_effect=lambda c, **_: ["SANDBOX"] + c) as sb:
        _run, out = _wrap(tmp_path, **kw)
    assert out == ["SANDBOX"] + CMD
    sb.assert_called_once()
    assert set(vw._systemd_run_for_worker.values()) <= {""}


def test_no_user_manager_socket_skips_probe(tmp_path):
    (tmp_path / "systemd" / "private").unlink()
    run, out = _wrap(tmp_path)
    assert out == CMD
    run.assert_not_called()


def test_probe_oserror_falls_back(tmp_path):
    run, ps = _patches()
    run.side_effect = OSError("boom")
    for p in ps:
        p.start()
    try:
        out = vw.wrap_worker_command(list(CMD), env=_env(tmp_path))
    finally:
        for p in ps:
            p.stop()
    assert out == CMD


@pytest.mark.skipif(sys.platform != "linux", reason="systemd scope spawn retry is Linux-only")
def test_spawn_retries_plain_when_scoped_start_raises(monkeypatch):
    mgr = vw.PythonWorkerManager(sys.executable, {"XDG_RUNTIME_DIR": "/run/user/1000", "PATH": "/usr/bin"})
    vw._systemd_run_for_worker[("/usr/bin/systemd-run", "/run/user/1000")] = "/usr/bin/systemd-run"
    attempts = {"n": 0}

    @contextlib.contextmanager
    def lease_stub(self, spawn_deadline, wait=0.0):
        del spawn_deadline, wait
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise OSError("systemd-run vanished")
        proc = mock.Mock()
        proc.stdin = mock.Mock()
        proc.stdout = mock.Mock()
        proc.poll.return_value = None
        from plugin.framework.warm_child import Lease

        yield Lease(stdin=proc.stdin, stdout=proc.stdout, pid=1, generation=0, warm=self)

    monkeypatch.setattr(vw.WarmChild, "lease", lease_stub)
    monkeypatch.setattr(mgr, "_ensure_warmed_unlocked", lambda leased: None)
    monkeypatch.setattr(mgr, "_execute_ipc_unlocked", lambda req, *, leased: {"status": "ok", "result": 1})
    result = mgr.execute("result = 1", timeout_sec=1)
    assert result["status"] == "ok"
    assert attempts["n"] == 2
    assert vw._systemd_scope_disabled
    assert vw.wrap_worker_command(list(CMD), env={"XDG_RUNTIME_DIR": "/run/user/1000"}) == CMD


def test_env_without_runtime_dir_ignores_earlier_scoped_probe(tmp_path):
    """CI: a full-env probe succeeded, then a manager with env={} got systemd-run
    without XDG_RUNTIME_DIR and the worker died with 'not defined'."""
    run, first = _wrap(tmp_path)
    assert first[0] == "/usr/bin/systemd-run"
    run2, ps = _patches()
    for p in ps:
        p.start()
    try:
        second = vw.wrap_worker_command(list(CMD), env={"PATH": "/usr/bin"})
    finally:
        for p in ps:
            p.stop()
    assert second == CMD
    assert run2.call_count == 0
