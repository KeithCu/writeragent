# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Warm Kokoro pool on ``WarmChild`` and the shared supervisor."""

from __future__ import annotations

from tests.harness.strip_bundle import is_release_build
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from typing import Any

from unittest.mock import MagicMock

import pytest

from plugin.audio.kokoro_pool import KokoroProcessPool, _child_env, shutdown_kokoro_pool
from plugin.framework.send_cancellation import SendCancellation
from plugin.framework.warm_child import Supervisor, WarmChild


def _repo_root() -> str:
    return os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def _fake_worker_script(directory) -> str:
    root = _repo_root()
    path = directory / "fake_kokoro_worker.py"
    path.write_text(
        "import os, sys, time\n"
        f"sys.path.insert(0, {root!r})\n"
        "from plugin.audio.kokoro_worker import run_kokoro_stdio_loop\n"
        "\n"
        "def handler(req):\n"
        "    text = req.get('text')\n"
        "    if text == 'block':\n"
        "        time.sleep(30)\n"
        "    t0 = time.monotonic()\n"
        "    if text == 'slow':\n"
        "        time.sleep(0.4)\n"
        "    t1 = time.monotonic()\n"
        "    out = req.get('out_path')\n"
        "    if not isinstance(out, str) or not out:\n"
        "        return {'status': 'error', 'error': 'missing out_path'}\n"
        "    with open(out, 'wb') as handle:\n"
        "        handle.write(b'wav')\n"
        "    return {'status': 'ok', 'path': out, 'echo': text, 'pid': os.getpid(), 'start': t0, 'end': t1}\n"
        "\n"
        "raise SystemExit(run_kokoro_stdio_loop(handler))\n",
        encoding="utf-8",
    )
    return str(path)


def _delayed_ready_script(directory, flag) -> str:
    root = _repo_root()
    path = directory / "slow_kokoro_worker.py"
    path.write_text(
        "import os, sys, time\n"
        f"sys.path.insert(0, {root!r})\n"
        f"_flag = {str(flag)!r}\n"
        "if os.path.exists(_flag):\n"
        "    time.sleep(30)\n"
        "from plugin.audio.kokoro_worker import run_kokoro_stdio_loop\n"
        "\n"
        "def handler(req):\n"
        "    out = req.get('out_path')\n"
        "    if not isinstance(out, str) or not out:\n"
        "        return {'status': 'error', 'error': 'missing out_path'}\n"
        "    with open(out, 'wb') as handle:\n"
        "        handle.write(b'wav')\n"
        "    return {'status': 'ok', 'path': out, 'echo': req.get('text'), 'pid': os.getpid()}\n"
        "\n"
        "raise SystemExit(run_kokoro_stdio_loop(handler))\n",
        encoding="utf-8",
    )
    return str(path)


def _warm_popen(pool: KokoroProcessPool) -> subprocess.Popen[bytes] | None:
    child = pool._warm._child
    if child is None:
        return None
    return child.managed.popen


def _wait_until(deadline: float, predicate, *, interval: float = 0.02) -> bool:
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


@pytest.fixture
def supervisor() -> Any:
    sup = Supervisor()
    yield sup
    sup.shutdown_all()


def test_pool_reuses_one_child_and_returns_wav_path(tmp_path, supervisor):
    script = _fake_worker_script(tmp_path)
    pool = KokoroProcessPool(sys.executable, script_path=script, idle_worker_ttl_sec=None, supervisor=supervisor)
    try:
        first_out = tmp_path / "one.wav"
        second_out = tmp_path / "two.wav"
        first = pool.execute({"text": "Hi.", "out_path": str(first_out)})
        second = pool.execute({"text": "There.", "out_path": str(second_out)})
        assert first["status"] == "ok"
        assert second["status"] == "ok"
        assert first["pid"] == second["pid"]
        assert first_out.read_bytes() == b"wav"
        assert second["echo"] == "There."
        assert pool.is_warm_alive()
    finally:
        pool.shutdown()


def test_scope_cancel_after_job_keeps_warm_worker(tmp_path, supervisor):
    script = _fake_worker_script(tmp_path)
    pool = KokoroProcessPool(sys.executable, script_path=script, idle_worker_ttl_sec=None, supervisor=supervisor)
    scope = SendCancellation()
    try:
        out = tmp_path / "done.wav"
        res = pool.execute({"text": "Hi.", "out_path": str(out)}, cancel=scope)
        assert res["status"] == "ok"
        assert pool.is_warm_alive()
        scope.cancel()
        assert scope._hooks == []
        assert pool.is_warm_alive()
    finally:
        pool.shutdown()


def test_pool_cancel_inflight_kills_child_and_next_job_respawns(tmp_path, supervisor):
    script = _fake_worker_script(tmp_path)
    pool = KokoroProcessPool(sys.executable, script_path=script, idle_worker_ttl_sec=None, supervisor=supervisor)
    try:
        blocked = tmp_path / "blocked.wav"
        result: dict[str, object] = {}
        cancel = SendCancellation()

        def _run() -> None:
            result.update(pool.execute({"text": "block", "out_path": str(blocked)}, timeout_sec=10, cancel=cancel))

        thread = threading.Thread(target=_run)
        thread.start()
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and not pool.is_warm_alive():
            time.sleep(0.02)
        assert pool.is_warm_alive()
        time.sleep(0.15)
        cancel.cancel()
        thread.join(3)
        assert result.get("code") == "WORKER_CANCELLED"
        recovered = pool.execute({"text": "after", "out_path": str(tmp_path / "after.wav")})
        assert recovered["status"] == "ok"
        assert recovered["echo"] == "after"
    finally:
        pool.shutdown()


def test_pool_idle_reaper_drops_warm_worker(tmp_path, supervisor):
    script = _fake_worker_script(tmp_path)
    pool = KokoroProcessPool(sys.executable, script_path=script, idle_worker_ttl_sec=0.2, supervisor=supervisor)
    try:
        out = pool.execute({"text": "Hi.", "out_path": str(tmp_path / "idle.wav")})
        assert out["status"] == "ok"
        assert pool.is_warm_alive()
        deadline = time.monotonic() + 2.0
        assert _wait_until(deadline, lambda: not pool.is_warm_alive(), interval=0.05)
    finally:
        pool.shutdown()


def test_pool_cancel_before_child_published(tmp_path, supervisor, monkeypatch):
    script = _fake_worker_script(tmp_path)
    pool = KokoroProcessPool(sys.executable, script_path=script, idle_worker_ttl_sec=None, supervisor=supervisor)
    from plugin.framework import managed_subprocess as ms

    orig_start = ms.ManagedSubprocess.start

    def slow_start(self: ms.ManagedSubprocess) -> None:
        orig_start(self)
        time.sleep(1.5)

    monkeypatch.setattr(ms.ManagedSubprocess, "start", slow_start)
    result: dict[str, object] = {}
    cancel = SendCancellation()
    out = tmp_path / "never.wav"

    def _run() -> None:
        result.update(pool.execute({"text": "Hi.", "out_path": str(out)}, cancel=cancel))

    thread = threading.Thread(target=_run)
    thread.start()
    try:
        deadline = time.monotonic() + 2.0
        assert _wait_until(deadline, thread.is_alive)
        assert _warm_popen(pool) is None
        cancel.cancel()
        thread.join(5)
        assert result.get("code") == "WORKER_CANCELLED"
        assert not out.is_file()
    finally:
        pool.shutdown()
        thread.join(3)


def test_pool_cancel_during_spawn_handshake_kills_child(tmp_path, supervisor):
    flag = tmp_path / "delay-ready"
    flag.write_text("1", encoding="utf-8")
    script = _delayed_ready_script(tmp_path, flag)
    pool = KokoroProcessPool(sys.executable, script_path=script, idle_worker_ttl_sec=None, supervisor=supervisor)
    result: dict[str, object] = {}
    cancel = SendCancellation()

    def _run() -> None:
        result.update(pool.execute({"text": "Hi.", "out_path": str(tmp_path / "slow.wav")}, cancel=cancel))

    thread = threading.Thread(target=_run)
    thread.start()
    try:
        proc = None
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            proc = _warm_popen(pool)
            if proc is not None and proc.pid:
                break
            time.sleep(0.02)
        assert proc is not None and proc.pid
        pid = proc.pid
        cancel.cancel()
        join_deadline = time.monotonic() + 8.0
        assert _wait_until(join_deadline, lambda: not thread.is_alive(), interval=0.05)
        assert result.get("code") == "WORKER_CANCELLED"
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            raise AssertionError("Kokoro child %s was still alive after spawn cancel" % pid) from None
        flag.unlink()
        recovered = pool.execute({"text": "after", "out_path": str(tmp_path / "after.wav")})
        assert recovered["status"] == "ok"
        assert recovered["echo"] == "after"
    finally:
        pool.shutdown()
        thread.join(3)


def test_pool_missing_script_is_spawn_failed_not_cancelled(tmp_path, supervisor):
    pool = KokoroProcessPool(
        sys.executable,
        script_path=str(tmp_path / "missing.py"),
        idle_worker_ttl_sec=None,
        supervisor=supervisor,
    )
    try:
        result = pool.execute({"text": "Hi.", "out_path": str(tmp_path / "missing.wav")})
        assert result["status"] == "error"
        assert result["code"] == "WORKER_SPAWN_FAILED"
    finally:
        pool.shutdown()


def test_cancel_check_true_keeps_warm_worker(tmp_path, supervisor):
    script = _fake_worker_script(tmp_path)
    pool = KokoroProcessPool(sys.executable, script_path=script, idle_worker_ttl_sec=None, supervisor=supervisor)
    try:
        cancel = SendCancellation()
        cancel.cancel()
        res = pool.execute({"text": "hi", "out_path": str(tmp_path / "x.wav")}, cancel=cancel)
        assert res.get("code") == "WORKER_CANCELLED"
        assert not pool.is_warm_alive()
        res2 = pool.execute({"text": "next", "out_path": str(tmp_path / "y.wav")})
        assert res2.get("status") == "ok"
        assert pool.is_warm_alive()
    finally:
        pool.shutdown()


def test_cancel_check_true_does_not_spawn(tmp_path, supervisor) -> None:
    pool = KokoroProcessPool(
        sys.executable,
        script_path=str(tmp_path / "missing.py"),
        idle_worker_ttl_sec=None,
        supervisor=supervisor,
    )
    try:
        assert not pool.is_warm_alive()
        cancel = SendCancellation()
        cancel.cancel()
        res = pool.execute({"text": "hi", "out_path": str(tmp_path / "x.wav")}, cancel=cancel)
        assert res.get("code") == "WORKER_CANCELLED"
        assert not pool.is_warm_alive()
    finally:
        pool.shutdown()


def test_execute_serializes_callers(tmp_path, supervisor) -> None:
    script = _fake_worker_script(tmp_path)
    pool = KokoroProcessPool(sys.executable, script_path=script, idle_worker_ttl_sec=None, supervisor=supervisor)
    barrier = threading.Barrier(2)
    results: list[dict[str, Any]] = []
    lock = threading.Lock()

    def _call(name: str) -> None:
        out = tmp_path / f"{name}.wav"
        barrier.wait()
        res = pool.execute({"text": "slow", "out_path": str(out)})
        with lock:
            results.append(res)

    t1 = threading.Thread(target=_call, args=("one",))
    t2 = threading.Thread(target=_call, args=("two",))
    try:
        t1.start()
        t2.start()
        t1.join(5)
        t2.join(5)
        assert len(results) == 2
        r1, r2 = sorted(results, key=lambda r: r["start"])
        assert r1["status"] == "ok"
        assert r2["status"] == "ok"
        assert r1["pid"] == r2["pid"]
        assert r1["end"] <= r2["start"]
    finally:
        pool.shutdown()


def test_execute_logs_each_warning_once(caplog, supervisor) -> None:
    warning_msg = "Kokoro fell back to espeak-ng for ja; non-English text may be misread"

    class _StubWarm:
        def __init__(self) -> None:
            self.warning = warning_msg

        def is_alive(self) -> bool:
            return True

        def request(self, payload: dict[str, Any], deadline: Any) -> dict[str, Any]:
            del payload, deadline
            return {"status": "ok", "warning": self.warning}

        def kill(self) -> None:
            return None

        def retire(self) -> None:
            return None

    caplog.set_level(logging.DEBUG, logger="plugin.audio.kokoro_pool")
    pool = KokoroProcessPool(sys.executable, script_path="unused.py", idle_worker_ttl_sec=None, supervisor=supervisor)
    stub = _StubWarm()
    pool._warm = stub  # type: ignore[assignment]
    try:
        pool.execute({"text": "one"})
        pool.execute({"text": "two"})

        warning_records = [
            r
            for r in caplog.records
            if r.name == "plugin.audio.kokoro_pool" and r.levelname == "WARNING" and r.getMessage().startswith("Kokoro:")
        ]
        debug_records = [
            r
            for r in caplog.records
            if r.name == "plugin.audio.kokoro_pool" and r.levelname == "DEBUG" and r.getMessage().startswith("Kokoro:")
        ]
        assert len(warning_records) == 1
        assert warning_records[0].getMessage() == f"Kokoro: {warning_msg}"
        if not is_release_build():
            assert len(debug_records) == 1

        stub.warning = "Different warning message"
        pool.execute({"text": "three"})

        warning_records2 = [
            r
            for r in caplog.records
            if r.name == "plugin.audio.kokoro_pool" and r.levelname == "WARNING" and r.getMessage().startswith("Kokoro:")
        ]
        assert len(warning_records2) == 2

        for idx in range(4, 36):
            stub.warning = f"Warning {idx}"
            pool.execute({"text": f"msg {idx}"})
        assert len(pool._seen_warnings) <= 32
    finally:
        pool.shutdown()


def test_kokoro_pool_spawn_failed_includes_stderr(tmp_path, supervisor):
    script = tmp_path / "broken_worker.py"
    script.write_text(
        "import sys\n"
        "sys.stderr.write('ModuleNotFoundError: kokoro_onnx\\n')\n"
        "sys.stderr.flush()\n"
        "raise SystemExit(1)\n"
    )
    pool = KokoroProcessPool(sys.executable, script_path=str(script), idle_worker_ttl_sec=None, supervisor=supervisor)
    try:
        result = pool.execute({"text": "Hello", "out_path": str(tmp_path / "out.wav")})
        assert result.get("status") == "error"
        assert result.get("code") == "WORKER_SPAWN_FAILED"
        assert "ModuleNotFoundError: kokoro_onnx" in str(result.get("error", ""))
    finally:
        pool.shutdown()


def test_delayed_ready_script_timeout_kills_child(tmp_path, supervisor):
    flag = tmp_path / "delay-ready"
    flag.write_text("1", encoding="utf-8")
    script = _delayed_ready_script(tmp_path, flag)
    pool = KokoroProcessPool(sys.executable, script_path=script, idle_worker_ttl_sec=None, supervisor=supervisor)
    result = pool.execute({"text": "Hi.", "out_path": str(tmp_path / "out.wav")}, timeout_sec=0.5)
    assert result.get("code") in ("EXECUTION_TIMEOUT", "WORKER_SPAWN_FAILED")
    assert not pool.is_warm_alive()
    pool.shutdown()


def test_alive_worker_execute_shares_clock(monkeypatch, supervisor):
    recorded_timeout: list[float] = []

    def tracked_request(self, payload, deadline):
        recorded_timeout.append(deadline.left())
        return {"status": "ok"}

    monkeypatch.setattr(WarmChild, "request", tracked_request)
    pool = KokoroProcessPool(sys.executable, script_path="unused.py", idle_worker_ttl_sec=None, supervisor=supervisor)
    try:
        res = pool.execute({"text": "Hi."}, timeout_sec=0.3)
        assert res.get("status") == "ok"
        assert len(recorded_timeout) == 1
        assert recorded_timeout[0] < 0.5
    finally:
        pool.shutdown()


def test_crash_mid_job_returns_worker_crashed(tmp_path, supervisor):
    root = _repo_root()
    script = tmp_path / "crash_worker.py"
    script.write_text(
        "import os, sys\n"
        f"sys.path.insert(0, {root!r})\n"
        "from plugin.audio.kokoro_worker import run_kokoro_stdio_loop\n"
        "\n"
        "def handler(req):\n"
        "    os._exit(1)\n"
        "\n"
        "raise SystemExit(run_kokoro_stdio_loop(handler))\n",
        encoding="utf-8",
    )
    pool = KokoroProcessPool(sys.executable, script_path=str(script), idle_worker_ttl_sec=None, supervisor=supervisor)
    try:
        result = pool.execute({"text": "Hi.", "out_path": str(tmp_path / "out.wav")})
        assert result.get("status") == "error"
        assert result.get("code") in ("WORKER_CRASHED", "EMPTY_RESPONSE")
        assert not pool.is_warm_alive()
    finally:
        pool.shutdown()


def test_get_kokoro_pool_caches_for_configured_venv(monkeypatch):
    import plugin.audio.kokoro_pool as kp

    shutdowns: list[Any] = []

    class DummyPool:
        def __init__(self, py_exe: str):
            self.py_exe = py_exe
            self.python_executable = py_exe

        def shutdown(self):
            shutdowns.append(self)

    config_val = {"path": "/venv1"}
    monkeypatch.setattr("plugin.framework.config.get_config", lambda key: config_val["path"])
    resolve_calls: list[str] = []

    def fake_resolve(path: str) -> str:
        resolve_calls.append(path)
        return path + "/bin/python"

    monkeypatch.setattr("plugin.audio.kokoro_pool.resolve_venv_python", fake_resolve)
    monkeypatch.setattr("plugin.audio.kokoro_pool.KokoroProcessPool", DummyPool)

    try:
        p1 = kp.get_kokoro_pool()
        assert p1 is not None
        assert len(resolve_calls) == 1

        p2 = kp.get_kokoro_pool()
        assert p2 is p1
        assert len(resolve_calls) == 1

        config_val["path"] = "/venv2"
        p3 = kp.get_kokoro_pool()
        assert p3 is not None
        assert p3 is not p1
        assert len(resolve_calls) == 2
        assert p1 in shutdowns

        config_val["path"] = ""
        p4 = kp.get_kokoro_pool()
        assert p4 is None
        assert p3 in shutdowns
    finally:
        with kp._POOL_LOCK:
            kp._POOL = None
            kp._POOL_CONFIG_VENV = None


def test_kokoro_child_spec_env_and_spawn(monkeypatch, supervisor):
    from unittest.mock import MagicMock

    monkeypatch.setenv("OPENAI_API_KEY", "super-secret-key")
    captured: dict[str, Any] = {}

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        proc = MagicMock()
        proc.stderr = MagicMock()
        proc.pid = 1234
        proc.stdin = MagicMock()
        proc.stdout = MagicMock()
        return proc

    monkeypatch.setattr("plugin.framework.managed_subprocess.subprocess.Popen", fake_popen)
    monkeypatch.setattr("plugin.framework.managed_subprocess.start_stderr_drain", lambda *_a, **_k: MagicMock())
    monkeypatch.setattr("plugin.framework.managed_subprocess.optimize_popen_pipes", lambda _proc: None)
    monkeypatch.setattr(
        "plugin.framework.process_worker.read_pickle_frame",
        lambda *_a, **_k: {"status": "ready", "pid": 1234},
    )

    pool = KokoroProcessPool(sys.executable, script_path="dummy_script.py", idle_worker_ttl_sec=None, supervisor=supervisor)
    try:
        spec = pool._warm.spec
        assert spec.name == "kokoro"
        assert spec.argv == [sys.executable, "dummy_script.py"]
        assert spec.env == _child_env()
        pool.execute({"text": "Hi.", "out_path": "/tmp/x.wav"})
        assert captured["cmd"] == [sys.executable, "dummy_script.py"]
        popen_env = captured["kwargs"]["env"]
        assert "OPENAI_API_KEY" not in popen_env
        assert popen_env.get("PYTHONUTF8") == "1"
        assert "PYTHONPATH" in popen_env
    finally:
        pool.shutdown()


def test_venv_path_change_retires_warm_child(monkeypatch):
    import plugin.audio.kokoro_pool as kp

    retire_calls: list[Any] = []

    class TrackingPool(KokoroProcessPool):
        def shutdown(self) -> None:
            retire_calls.append(self)
            super().shutdown()

    monkeypatch.setattr(kp, "KokoroProcessPool", TrackingPool)
    config = {"path": "/venv/a"}
    monkeypatch.setattr("plugin.framework.config.get_config", lambda _k: config["path"])
    monkeypatch.setattr(
        "plugin.audio.kokoro_pool.resolve_venv_python",
        lambda p: f"{p}/bin/python",
    )
    with kp._POOL_LOCK:
        kp._POOL = None
        kp._POOL_CONFIG_VENV = None
    try:
        p1 = kp.get_kokoro_pool()
        assert p1 is not None
        p2 = kp.get_kokoro_pool()
        assert p2 is p1
        assert len(retire_calls) == 0

        config["path"] = "/venv/b"
        p3 = kp.get_kokoro_pool()
        assert p3 is not p1
        assert p1 in retire_calls
    finally:
        with kp._POOL_LOCK:
            if kp._POOL is not None:
                kp._POOL.shutdown()
            kp._POOL = None
            kp._POOL_CONFIG_VENV = None


def test_cancel_inflight_returns_without_blocking_on_reap(tmp_path, supervisor):
    script = _fake_worker_script(tmp_path)
    pool = KokoroProcessPool(sys.executable, script_path=script, idle_worker_ttl_sec=None, supervisor=supervisor)
    cancel = SendCancellation()
    result: dict[str, object] = {}

    def _run() -> None:
        result.update(pool.execute({"text": "block", "out_path": str(tmp_path / "b.wav")}, cancel=cancel))

    thread = threading.Thread(target=_run)
    thread.start()
    deadline = time.monotonic() + 2
    assert _wait_until(deadline, pool.is_warm_alive)
    cancel.cancel()
    join_deadline = time.monotonic() + 1.0
    assert _wait_until(join_deadline, lambda: not thread.is_alive())
    assert result.get("code") == "WORKER_CANCELLED"
    pool.shutdown()


@pytest.mark.slow
@pytest.mark.skipif(sys.platform != "linux", reason="orphan PDEATHSIG check is Linux-only")
def test_orphan_kokoro_child_dies_with_parent(tmp_path):
    root = _repo_root()
    worker_script = _fake_worker_script(tmp_path)
    parent_script = tmp_path / "parent_kokoro.py"
    parent_script.write_text(
        "\n".join(
            [
                "import os, sys, threading, time",
                f"sys.path.insert(0, {root!r})",
                "from plugin.audio.kokoro_pool import KokoroProcessPool",
                f"script = {worker_script!r}",
                "pool = KokoroProcessPool(sys.executable, script_path=script, idle_worker_ttl_sec=None)",
                "out = sys.argv[1]",
                "pid_path = sys.argv[2]",
                "def job():",
                "    pool.execute({'text': 'block', 'out_path': out})",
                "threading.Thread(target=job, daemon=True).start()",
                "deadline = time.monotonic() + 15",
                "while time.monotonic() < deadline:",
                "    child = pool._warm._child",
                "    proc = child.managed.popen if child else None",
                "    if proc is not None and proc.pid:",
                "        with open(pid_path, 'w', encoding='utf-8') as f:",
                "            f.write(str(proc.pid))",
                "        break",
                "    time.sleep(0.02)",
                "else:",
                "    raise SystemExit(2)",
                "time.sleep(120)",
            ]
        ),
        encoding="utf-8",
    )
    out_wav = tmp_path / "block.wav"
    pid_file = tmp_path / "child.pid"
    parent = subprocess.Popen([sys.executable, str(parent_script), str(out_wav), str(pid_file)])
    try:
        deadline = time.monotonic() + 20.0
        assert _wait_until(deadline, pid_file.is_file, interval=0.05)
        assert pid_file.is_file(), "parent never recorded child pid"
        child_pid = int(pid_file.read_text(encoding="utf-8").strip())
        os.kill(parent.pid, signal.SIGKILL)
        parent.wait(timeout=5)
        gone_by = time.monotonic() + 2.0
        if _wait_until(gone_by, lambda: _process_gone(child_pid), interval=0.05):
            return
        pytest.fail(f"Kokoro warm child {child_pid} outlived SIGKILL parent")
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=5)


def _process_gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return True
    return False


def test_base_exception_during_request_propagates(tmp_path, monkeypatch, supervisor):
    orig_request = WarmChild.request
    first = True

    def mock_request(self, payload, deadline):
        nonlocal first
        if first:
            first = False
            raise KeyboardInterrupt()
        return orig_request(self, payload, deadline)

    monkeypatch.setattr(WarmChild, "request", mock_request)
    script = _fake_worker_script(tmp_path)
    pool = KokoroProcessPool(sys.executable, script_path=script, idle_worker_ttl_sec=None, supervisor=supervisor)
    try:
        with pytest.raises(KeyboardInterrupt):
            pool.execute({"text": "Hi.", "out_path": str(tmp_path / "out1.wav")})
        res = pool.execute({"text": "Hi.", "out_path": str(tmp_path / "out2.wav")})
        assert res.get("status") == "ok"
    finally:
        pool.shutdown()


def test_shutdown_kokoro_pool_drops_singleton(monkeypatch) -> None:
    import plugin.audio.kokoro_pool as kokoro_pool

    mock_pool = MagicMock()
    monkeypatch.setattr(kokoro_pool, "_POOL", mock_pool)
    monkeypatch.setattr(kokoro_pool, "_POOL_CONFIG_VENV", "/fake/venv")
    shutdown_kokoro_pool()
    mock_pool.shutdown.assert_called_once()
    assert kokoro_pool._POOL is None
    assert kokoro_pool._POOL_CONFIG_VENV is None
