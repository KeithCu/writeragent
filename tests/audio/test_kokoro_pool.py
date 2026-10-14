# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Warm Kokoro pool against a fake child that speaks the Pickle 5 stdio protocol."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from typing import Any

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
        "    out = req.get('out_path')\n"
        "    if not isinstance(out, str) or not out:\n"
        "        return {'status': 'error', 'error': 'missing out_path'}\n"
        "    with open(out, 'wb') as handle:\n"
        "        handle.write(b'wav')\n"
        "    return {'status': 'ok', 'path': out, 'echo': text, 'pid': os.getpid()}\n"
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
    pool._worker = warm  # type: ignore[assignment]
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
            proc = worker.process if worker is not None else None
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
    pool._worker = stuck
    pool._inflight = True
    pool._exec_token = token
    try:
        pool.cancel_inflight(token)
        assert pool._worker is None
        assert stuck.kills == 1
        assert stuck.shutdowns == 1
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
    pool._worker = stuck  # type: ignore[assignment]
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

    worker = _KokoroWorker(sys.executable, "dummy_script.py", env=_child_env())
    # Command wrapper must be None
    assert worker._managed._command_wrapper is None

    worker._managed.start()
    worker.kill()

    # Argv is [sys.executable, "dummy_script.py"] with no flatpak-spawn or wrapper prefix
    assert captured["cmd"] == [sys.executable, "dummy_script.py"]

    # Environment passed to Popen must not contain the scrubbed secret
    popen_env = captured["kwargs"]["env"]
    assert "OPENAI_API_KEY" not in popen_env
    assert popen_env.get("PYTHONUTF8") == "1"
    assert "PYTHONPATH" in popen_env
