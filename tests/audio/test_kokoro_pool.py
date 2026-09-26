# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Warm Kokoro pool against a fake child that speaks the Pickle 5 stdio protocol."""

from __future__ import annotations

import os
import sys
import threading
import time

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
