# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Warm Kokoro pool against a fake child that speaks the Pickle 5 stdio protocol."""

from __future__ import annotations

from tests.harness.strip_bundle import is_release_build
import os
import subprocess
import sys
import threading
import time
from typing import Any, cast

import pytest

from plugin.audio.kokoro_pool import KokoroProcessPool


def _fake_worker_script(directory) -> str:
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
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


def test_pool_reuses_one_child_and_returns_wav_path(tmp_path):
    script = _fake_worker_script(tmp_path)
    pool = KokoroProcessPool(sys.executable, script_path=script, idle_worker_ttl_sec=None)
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
        assert pool._worker is not None and pool._worker.is_alive()
    finally:
        pool.shutdown()


def test_pool_cancel_inflight_kills_child_and_next_job_respawns(tmp_path):
    script = _fake_worker_script(tmp_path)
    pool = KokoroProcessPool(sys.executable, script_path=script, idle_worker_ttl_sec=None)
    try:
        blocked = tmp_path / "blocked.wav"
        result: dict[str, object] = {}

        def _run() -> None:
            result.update(pool.execute({"text": "block", "out_path": str(blocked)}, timeout_sec=10))

        thread = threading.Thread(target=_run)
        thread.start()
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and (pool._worker is None or not pool._worker.is_alive()):
            time.sleep(0.02)
        assert pool._worker is not None and pool._worker.is_alive()
        time.sleep(0.15)
        pool.cancel_inflight()
        thread.join(3)
        assert result.get("status") == "error"
        recovered = pool.execute({"text": "after", "out_path": str(tmp_path / "after.wav")})
        assert recovered["status"] == "ok"
        assert recovered["echo"] == "after"
    finally:
        pool.shutdown()


def test_idle_reaper_leaves_a_warm_child_before_ttl():
    """A tick before the TTL does not kill. An in-flight job is left alone.

    Stderr is a bounded drain. The idle reaper does not visit it.
    """

    class _Warm:
        def __init__(self) -> None:
            self.kills = 0

        def is_alive(self) -> bool:
            return True

        def request_shutdown(self) -> None:
            return None

        def kill(self) -> bool:
            self.kills += 1
            return True

    # None keeps the background reaper off. The TTL is set after init so
    # this call is the only tick.
    pool = KokoroProcessPool(sys.executable, script_path="unused.py", idle_worker_ttl_sec=None)
    pool.idle_worker_ttl_sec = 300.0
    warm = _Warm()
    pool._worker = cast("Any", warm)
    pool._last_active = time.monotonic()
    try:
        pool._evict_idle_worker()
        assert warm.kills == 0
        assert pool._worker is warm
        pool._inflight = True
        pool._last_active = 0.0
        pool._evict_idle_worker()
        assert warm.kills == 0
        assert pool._worker is warm
    finally:
        pool.shutdown()


def test_pool_idle_reaper_drops_warm_worker(tmp_path):
    script = _fake_worker_script(tmp_path)
    pool = KokoroProcessPool(sys.executable, script_path=script, idle_worker_ttl_sec=0)
    try:
        out = pool.execute({"text": "Hi.", "out_path": str(tmp_path / "idle.wav")})
        assert out["status"] == "ok"
        assert pool._worker is not None and pool._worker.is_alive()
        pool._evict_idle_worker()
        assert pool._worker is None
    finally:
        pool.shutdown()


def _delayed_ready_script(directory, flag) -> str:
    """Child sleeps before the ready frame while *flag* exists."""
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
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


def test_pool_cancel_during_spawn_handshake_kills_child(tmp_path):
    """Stop during the ready frame must not adopt the child or one-shot the clip."""
    flag = tmp_path / "delay-ready"
    flag.write_text("1", encoding="utf-8")
    script = _delayed_ready_script(tmp_path, flag)
    pool = KokoroProcessPool(sys.executable, script_path=script, idle_worker_ttl_sec=None)
    result: dict[str, object] = {}

    def _run() -> None:
        result.update(pool.execute({"text": "Hi.", "out_path": str(tmp_path / "slow.wav")}))

    thread = threading.Thread(target=_run)
    thread.start()
    try:
        pid = 0
        proc = None
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            worker = pool._worker
            proc = worker._managed.popen if worker is not None else None
            if proc is not None and proc.pid:
                pid = proc.pid
                break
            time.sleep(0.02)
        assert pid > 0
        t0 = time.monotonic()
        pool.cancel_inflight()
        thread.join(8)
        assert not thread.is_alive()
        assert time.monotonic() - t0 < 8
        assert result.get("code") == "WORKER_CANCELLED"
        # Popen.wait, not os.kill(pid, 0): on Windows signal 0 is CTRL_C_EVENT,
        # not a liveness probe.
        assert proc is not None
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


def test_pool_missing_script_is_spawn_failed_not_cancelled(tmp_path):
    """A handshake crash still falls back. Only a dropped token is WORKER_CANCELLED."""
    pool = KokoroProcessPool(sys.executable, script_path=str(tmp_path / "missing.py"), idle_worker_ttl_sec=None)
    try:
        result = pool.execute({"text": "Hi.", "out_path": str(tmp_path / "missing.wav")})
        assert result["status"] == "error"
        assert result["code"] == "WORKER_SPAWN_FAILED"
    finally:
        pool.shutdown()


def test_cancel_clears_the_slot_so_the_next_job_can_spawn(monkeypatch):
    """Cancel retires the child. The next job may spawn.

    A child that survives SIGKILL is abandoned inside kill(). The slot
    is empty, so this job does not wait for that process.
    """

    class _Stuck:
        def __init__(self) -> None:
            self.kills = 0
            self.shutdowns = 0

        def request_shutdown(self) -> None:
            self.shutdowns += 1

        def kill(self) -> bool:
            self.kills += 1
            return True

        def is_alive(self) -> bool:
            return False

        def is_shutting_down(self) -> bool:
            return False

    class _Replacement:
        def __init__(self, *_args, **_kwargs) -> None:
            return None

        def is_alive(self) -> bool:
            return True

        def is_shutting_down(self) -> bool:
            return False

        def respawn(self) -> None:
            return None

        def execute(self, payload, timeout_sec):
            del timeout_sec
            return {"status": "ok", "echo": payload.get("text")}

        def request_shutdown(self) -> None:
            return None

        def kill(self) -> bool:
            return True

    monkeypatch.setattr("plugin.audio.kokoro_pool._KokoroWorker", _Replacement)
    pool = KokoroProcessPool(sys.executable, script_path="unused.py", idle_worker_ttl_sec=None)
    stuck = _Stuck()
    token = object()
    pool._worker = cast("Any", stuck)
    pool._inflight = True
    pool._exec_token = token
    try:
        pool.cancel_inflight(token)
        assert pool._worker is None
        assert stuck.shutdowns == 1
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and stuck.kills == 0:
            time.sleep(0.01)
        assert stuck.kills == 1
        recovered = pool.execute({"text": "after"})
        assert recovered["status"] == "ok"
        assert recovered["echo"] == "after"
        assert isinstance(pool._worker, _Replacement)
    finally:
        pool.shutdown()


def test_failed_handshake_is_not_executed_and_a_later_job_may_spawn(monkeypatch) -> None:
    """A handshake that leaves the worker not alive is a failed spawn.

    What was wrong: is_alive() was still true after a kill that could not
    reap, so the pool called execute on a child that never said ready.
    That child is dropped. The next job may construct a new worker, and
    neither one is executed while spawn stays failed.
    """
    made: list = []

    class _DeadSpawn:
        def __init__(self, *_args, **_kwargs) -> None:
            self.executes = 0
            made.append(self)

        def is_alive(self) -> bool:
            return False

        def is_shutting_down(self) -> bool:
            return False

        def respawn(self) -> None:
            return None

        def stderr_tail(self) -> str:
            return ""

        def execute(self, payload, timeout_sec):
            del payload, timeout_sec
            self.executes += 1
            return {"status": "ok"}

        def request_shutdown(self) -> None:
            return None

        def kill(self) -> bool:
            return True

    monkeypatch.setattr("plugin.audio.kokoro_pool._KokoroWorker", _DeadSpawn)
    pool = KokoroProcessPool(sys.executable, script_path="unused.py", idle_worker_ttl_sec=None)
    try:
        result = pool.execute({"text": "hi"})
        assert result["code"] == "WORKER_SPAWN_FAILED"
        assert pool._worker is None
        assert len(made) == 1
        assert made[0].executes == 0
        again = pool.execute({"text": "next"})
        assert again["code"] == "WORKER_SPAWN_FAILED"
        assert len(made) == 2
        assert made[0] is not made[1]
        assert made[0].executes == 0
        assert made[1].executes == 0
    finally:
        pool.shutdown()


def test_dead_warm_child_is_retired_and_replaced(monkeypatch) -> None:
    """A warm child that is not alive is retired. The replacement runs the job.

    is_alive True is a healthy reuse. This stand-in is dead, so execute
    must not write to it.
    """

    class _Dead:
        def __init__(self) -> None:
            self.executes = 0
            self.kills = 0

        def is_alive(self) -> bool:
            return False

        def is_shutting_down(self) -> bool:
            return False

        def execute(self, payload, timeout_sec):
            del payload, timeout_sec
            self.executes += 1
            return {"status": "ok"}

        def request_shutdown(self) -> None:
            return None

        def kill(self) -> bool:
            self.kills += 1
            return True

    class _Replacement:
        def __init__(self, *_args, **_kwargs) -> None:
            return None

        def is_alive(self) -> bool:
            return True

        def is_shutting_down(self) -> bool:
            return False

        def respawn(self) -> None:
            return None

        def execute(self, payload, timeout_sec):
            del timeout_sec
            return {"status": "ok", "echo": payload.get("text")}

        def request_shutdown(self) -> None:
            return None

        def kill(self) -> bool:
            return True

    monkeypatch.setattr("plugin.audio.kokoro_pool._KokoroWorker", _Replacement)
    pool = KokoroProcessPool(sys.executable, script_path="unused.py", idle_worker_ttl_sec=None)
    stuck = _Dead()
    pool._worker = cast("Any", stuck)
    try:
        result = pool.execute({"text": "hi"})
        assert result["status"] == "ok"
        assert result["echo"] == "hi"
        assert stuck.executes == 0
        assert stuck.kills == 1
        assert isinstance(pool._worker, _Replacement)
        assert pool._worker is not stuck
    finally:
        pool.shutdown()


def test_kokoro_worker_managed_subprocess_env_and_wrapper(monkeypatch):
    from unittest.mock import MagicMock
    from plugin.audio.kokoro_pool import _KokoroWorker, _child_env

    # Plant a secret in os.environ that scrub_subprocess_env removes
    monkeypatch.setenv("OPENAI_API_KEY", "super-secret-key")

    captured: dict[str, Any] = {}

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        proc = MagicMock()
        proc.stderr = MagicMock()
        proc.pid = 1234
        return proc

    monkeypatch.setattr("plugin.framework.managed_subprocess.subprocess.Popen", fake_popen)
    monkeypatch.setattr("plugin.framework.managed_subprocess.start_stderr_drain", lambda *_a, **_k: MagicMock())
    monkeypatch.setattr("plugin.framework.managed_subprocess.optimize_popen_pipes", lambda _proc: None)

    worker = _KokoroWorker(sys.executable, "dummy_script.py", env=_child_env())
    # Command wrapper must be None
    assert worker._managed._command_wrapper is None
    assert worker._managed._optimize_pipes is True
    assert worker._managed._scrub_env is False
    assert worker._managed._stderr_name == "wa-kokoro"
    assert worker._managed._stderr_name_with_pid is True

    worker._managed.start()
    worker.kill()

    # Argv is [sys.executable, "dummy_script.py"] with no flatpak-spawn or wrapper prefix
    assert captured["cmd"] == [sys.executable, "dummy_script.py"]

    # Environment passed to Popen must not contain the scrubbed secret
    popen_env = captured["kwargs"]["env"]
    assert "OPENAI_API_KEY" not in popen_env
    assert popen_env.get("PYTHONUTF8") == "1"
    assert "PYTHONPATH" in popen_env


def test_worker_kill_signals_and_abandons_without_close() -> None:
    from unittest.mock import MagicMock
    from plugin.audio.kokoro_pool import _KokoroWorker

    worker = _KokoroWorker(sys.executable, "unused.py")
    mock_managed = MagicMock()
    worker._managed = mock_managed

    assert worker.kill() is True
    assert worker.is_alive() is False
    mock_managed.kill_tree.assert_called_once()
    mock_managed.abandon.assert_called_once()
    mock_managed.close.assert_not_called()


def test_cancel_inflight_returns_while_reap_blocks() -> None:
    class _SlowKill:
        def __init__(self) -> None:
            self.kills = 0
            self.shutdowns = 0

        def request_shutdown(self) -> None:
            self.shutdowns += 1

        def kill(self) -> bool:
            time.sleep(2)
            self.kills += 1
            return True

        def is_alive(self) -> bool:
            return True

        def is_shutting_down(self) -> bool:
            return False

    pool = KokoroProcessPool(sys.executable, script_path="unused.py", idle_worker_ttl_sec=None)
    slow = _SlowKill()
    token = object()
    pool._worker = cast("Any", slow)
    pool._inflight = True
    pool._exec_token = token
    try:
        t0 = time.monotonic()
        pool.cancel_inflight(token)
        elapsed = time.monotonic() - t0
        assert elapsed < 0.3
        assert pool._worker is None
        assert slow.shutdowns == 1
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and slow.kills == 0:
            time.sleep(0.02)
        assert slow.kills == 1
    finally:
        pool.shutdown()


def test_raising_cancel_check_releases_inflight(tmp_path) -> None:
    pool = KokoroProcessPool(sys.executable, script_path=str(tmp_path / "missing.py"), idle_worker_ttl_sec=None)

    def _boom() -> bool:
        raise RuntimeError("boom")

    try:
        with pytest.raises(RuntimeError, match="boom"):
            pool.execute({"text": "hi"}, cancel_check=_boom)
        assert not pool._inflight
        assert pool._exec_token is None
        result = pool.execute({"text": "hi"})
        assert result["code"] == "WORKER_SPAWN_FAILED"
    finally:
        pool.shutdown()


def test_execute_serializes_callers(tmp_path) -> None:
    script = _fake_worker_script(tmp_path)
    pool = KokoroProcessPool(sys.executable, script_path=script, idle_worker_ttl_sec=None)
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


def test_signal_kokoro_child_does_not_close_stdin() -> None:
    from unittest.mock import MagicMock
    from plugin.audio.kokoro_pool import _KokoroWorker, _signal_kokoro_child

    worker = _KokoroWorker(sys.executable, "unused.py")
    mock_managed = MagicMock()
    mock_popen = MagicMock()
    mock_stdin = MagicMock()
    mock_managed.popen = mock_popen
    mock_managed.stdin = mock_stdin
    worker._managed = mock_managed

    _signal_kokoro_child(worker)

    mock_stdin.close.assert_not_called()
    assert not hasattr(mock_managed, "close_stdio") or mock_managed.close_stdio.call_count == 0
    if sys.platform == "win32":
        mock_popen.terminate.assert_called_once()
        mock_popen.kill.assert_not_called()
    else:
        mock_popen.kill.assert_called_once()
        mock_popen.terminate.assert_not_called()


def test_cancel_check_true_keeps_warm_worker() -> None:
    class _Stub:
        def __init__(self) -> None:
            self.kills = 0
            self.executes = 0

        def is_alive(self) -> bool:
            return True

        def is_shutting_down(self) -> bool:
            return False

        def request_shutdown(self) -> None:
            return None

        def kill(self) -> bool:
            self.kills += 1
            return True

        def execute(self, payload: dict[str, Any], timeout_sec: float) -> dict[str, Any]:
            del timeout_sec
            self.executes += 1
            return {"status": "ok", "echo": payload.get("text")}

    pool = KokoroProcessPool(sys.executable, script_path="unused.py", idle_worker_ttl_sec=None)
    stub = _Stub()
    pool._worker = cast("Any", stub)
    try:
        res = pool.execute({"text": "hi"}, cancel_check=lambda: True)
        assert res.get("code") == "WORKER_CANCELLED"
        assert stub.kills == 0
        assert stub.executes == 0
        assert pool._worker is stub
        assert not pool._inflight
        assert pool._exec_token is None

        res2 = pool.execute({"text": "next"})
        assert res2.get("status") == "ok"
        assert res2.get("echo") == "next"
        assert stub.executes == 1
        assert stub.kills == 0
        assert pool._worker is stub
    finally:
        pool.shutdown()


def test_cancel_check_true_does_not_spawn(tmp_path) -> None:
    pool = KokoroProcessPool(
        sys.executable,
        script_path=str(tmp_path / "missing.py"),
        idle_worker_ttl_sec=None,
    )
    try:
        assert pool._worker is None
        res = pool.execute({"text": "hi"}, cancel_check=lambda: True)
        assert res.get("code") == "WORKER_CANCELLED"
        assert pool._worker is None
    finally:
        pool.shutdown()


def test_execute_logs_each_warning_once(caplog) -> None:
    import logging

    warning_msg = "Kokoro fell back to espeak-ng for ja; non-English text may be misread"

    class _Stub:
        def __init__(self) -> None:
            self.warning = warning_msg

        def is_alive(self) -> bool:
            return True

        def is_shutting_down(self) -> bool:
            return False

        def request_shutdown(self) -> None:
            return None

        def kill(self) -> bool:
            return True

        def execute(self, payload: dict[str, Any], timeout_sec: float) -> dict[str, Any]:
            del payload, timeout_sec
            return {"status": "ok", "warning": self.warning}

    caplog.set_level(logging.DEBUG, logger="plugin.audio.kokoro_pool")
    pool = KokoroProcessPool(sys.executable, script_path="unused.py", idle_worker_ttl_sec=None)
    stub = _Stub()
    pool._worker = cast("Any", stub)
    try:
        pool.execute({"text": "one"})
        pool.execute({"text": "two"})

        warning_records = [
            r for r in caplog.records
            if r.name == "plugin.audio.kokoro_pool" and r.levelname == "WARNING" and r.getMessage().startswith("Kokoro:")
        ]
        debug_records = [
            r for r in caplog.records
            if r.name == "plugin.audio.kokoro_pool" and r.levelname == "DEBUG" and r.getMessage().startswith("Kokoro:")
        ]
        assert len(warning_records) == 1
        assert warning_records[0].getMessage() == f"Kokoro: {warning_msg}"
        if not is_release_build():  # log.debug call site is stripped in release bundles
            assert len(debug_records) == 1
            assert debug_records[0].getMessage() == f"Kokoro: {warning_msg}"

        stub.warning = "Different warning message"
        pool.execute({"text": "three"})

        warning_records2 = [
            r for r in caplog.records
            if r.name == "plugin.audio.kokoro_pool" and r.levelname == "WARNING" and r.getMessage().startswith("Kokoro:")
        ]
        assert len(warning_records2) == 2
        assert warning_records2[1].getMessage() == "Kokoro: Different warning message"

        # Emit up to 34 distinct warnings total
        for idx in range(4, 36):
            stub.warning = f"Warning {idx}"
            pool.execute({"text": f"msg {idx}"})
        assert len(pool._seen_warnings) <= 32
    finally:
        pool.shutdown()


def test_kokoro_pool_spawn_failed_includes_stderr(tmp_path):
    script = tmp_path / "broken_worker.py"
    script.write_text(
        "import sys\n"
        "sys.stderr.write('ModuleNotFoundError: kokoro_onnx\\n')\n"
        "sys.stderr.flush()\n"
        "raise SystemExit(1)\n"
    )
    pool = KokoroProcessPool(sys.executable, script_path=str(script), idle_worker_ttl_sec=None)
    try:
        result = pool.execute({"text": "Hello", "out_path": str(tmp_path / "out.wav")})
        assert result.get("status") == "error"
        assert result.get("code") == "WORKER_SPAWN_FAILED"
        assert (
            "ModuleNotFoundError: kokoro_onnx" in result.get("stderr", "")
            or "ModuleNotFoundError: kokoro_onnx" in result.get("error", "")
        )
    finally:
        pool.shutdown()


def test_delayed_ready_script_timeout_kills_child(tmp_path):
    flag = tmp_path / "delay-ready"
    flag.write_text("1", encoding="utf-8")
    script = _delayed_ready_script(tmp_path, flag)
    pool = KokoroProcessPool(sys.executable, script_path=script, idle_worker_ttl_sec=None)
    t0 = time.monotonic()
    result = pool.execute({"text": "Hi.", "out_path": str(tmp_path / "out.wav")}, timeout_sec=0.5)
    elapsed = time.monotonic() - t0
    assert elapsed < 1.0
    assert result.get("code") in ("EXECUTION_TIMEOUT", "WORKER_SPAWN_FAILED")
    assert pool._worker is None or not pool._worker.is_alive()


def test_alive_worker_execute_shares_clock(monkeypatch):
    recorded_timeout: list[float] = []

    class _AliveWorker:
        def is_alive(self):
            return True

        def is_shutting_down(self):
            return False

        def execute(self, payload, timeout_sec):
            recorded_timeout.append(timeout_sec)
            return {"status": "ok"}

    pool = KokoroProcessPool(sys.executable, script_path="unused.py", idle_worker_ttl_sec=None)
    pool._worker = cast("Any", _AliveWorker())
    res = pool.execute({"text": "Hi."}, timeout_sec=0.3)
    assert res.get("status") == "ok"
    assert len(recorded_timeout) == 1
    assert recorded_timeout[0] < 0.5


def test_respawn_uses_clock_and_expires(monkeypatch):
    kills: list[bool] = []
    executes: list[bool] = []

    class _SlowSpawnWorker:
        def __init__(self, *args, **kwargs):
            self._alive = False

        def respawn(self):
            time.sleep(0.4)
            self._alive = True

        def is_alive(self):
            return self._alive

        def is_shutting_down(self):
            return False

        def request_shutdown(self):
            pass

        def kill(self):
            kills.append(True)
            self._alive = False
            return True

        def execute(self, payload, timeout_sec):
            executes.append(True)
            return {"status": "ok"}

        def stderr_tail(self):
            return ""

    monkeypatch.setattr("plugin.audio.kokoro_pool._KokoroWorker", _SlowSpawnWorker)
    pool = KokoroProcessPool(sys.executable, script_path="unused.py", idle_worker_ttl_sec=None)
    res = pool.execute({"text": "Hi."}, timeout_sec=0.3)
    assert res.get("code") == "EXECUTION_TIMEOUT"
    assert not executes
    assert kills


def test_base_exception_during_handshake_or_read_drops_worker(tmp_path, monkeypatch):
    import plugin.audio.kokoro_pool as kp
    orig_read = kp.read_pickle_frame
    first_call = True

    def mock_read(*args, **kwargs):
        nonlocal first_call
        if first_call:
            first_call = False
            raise KeyboardInterrupt()
        return orig_read(*args, **kwargs)

    monkeypatch.setattr(kp, "read_pickle_frame", mock_read)
    script = _fake_worker_script(tmp_path)
    pool = KokoroProcessPool(sys.executable, script_path=script, idle_worker_ttl_sec=None)
    try:
        with pytest.raises(KeyboardInterrupt):
            pool.execute({"text": "Hi.", "out_path": str(tmp_path / "out1.wav")})

        assert pool._worker is None or not pool._worker.is_alive()

        res = pool.execute({"text": "Hi.", "out_path": str(tmp_path / "out2.wav")})
        assert res.get("status") == "ok"
    finally:
        pool.shutdown()


def test_get_kokoro_pool_caches_for_configured_venv(monkeypatch):
    from plugin.audio.kokoro_pool import get_kokoro_pool, shutdown_kokoro_pool

    created_pools: list[Any] = []
    shutdowns: list[Any] = []

    class DummyPool:
        def __init__(self, py_exe: str):
            self.py_exe = py_exe
            self.configured_venv = None
            created_pools.append(self)

        def shutdown(self):
            shutdowns.append(self)

    config_val = {"path": "/venv1"}
    monkeypatch.setattr("plugin.framework.config.get_config_str", lambda key: config_val["path"])
    resolve_calls: list[str] = []

    def fake_resolve(path: str) -> str:
        resolve_calls.append(path)
        return path + "/bin/python"

    monkeypatch.setattr("plugin.audio.kokoro_pool.resolve_venv_python", fake_resolve)
    monkeypatch.setattr("plugin.audio.kokoro_pool.KokoroProcessPool", DummyPool)

    try:
        p1 = get_kokoro_pool()
        assert p1 is not None
        assert len(resolve_calls) == 1

        p2 = get_kokoro_pool()
        assert p2 is p1
        assert len(resolve_calls) == 1

        # Change venv path
        config_val["path"] = "/venv2"
        p3 = get_kokoro_pool()
        assert p3 is not None
        assert p3 is not p1
        assert len(resolve_calls) == 2
        assert p1 in shutdowns

        # Empty string
        config_val["path"] = ""
        p4 = get_kokoro_pool()
        assert p4 is None
        assert p3 in shutdowns
    finally:
        shutdown_kokoro_pool()


def test_worker_kill_dead_child_closes_and_unregisters():
    from plugin.audio.kokoro_pool import _KokoroWorker

    class PlainManaged:
        def __init__(self, wait_result: bool):
            self.wait_result = wait_result
            self.calls: list[str] = []

        def is_alive(self) -> bool:
            return False

        def kill_tree(self) -> None:
            self.calls.append("kill_tree")

        def close_stdio(self) -> None:
            self.calls.append("close_stdio")

        def wait_once(self) -> bool:
            self.calls.append("wait_once")
            return self.wait_result

        def unregister(self) -> None:
            self.calls.append("unregister")

        def abandon(self) -> None:
            self.calls.append("abandon")

        def close(self) -> None:
            self.calls.append("close")

    # First object: wait_once returns True
    m1 = PlainManaged(wait_result=True)
    w1 = _KokoroWorker(sys.executable, "unused.py")
    w1._managed = cast("Any", m1)
    assert w1.kill() is True
    assert m1.calls == ["kill_tree", "close_stdio", "wait_once", "unregister"]

    # Second object: wait_once returns False
    m2 = PlainManaged(wait_result=False)
    w2 = _KokoroWorker(sys.executable, "unused.py")
    w2._managed = cast("Any", m2)
    assert w2.kill() is True
    assert m2.calls == ["kill_tree", "close_stdio", "wait_once", "abandon"]

