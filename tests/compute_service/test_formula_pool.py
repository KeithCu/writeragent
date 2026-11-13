# WriterAgent - Python Compute Service Formula Pool tests
# Copyright (c) 2026 KeithCu
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from typing import Any
from unittest.mock import patch

import pytest

from compute_service.config import ComputeSettings
from compute_service.formula_pool import FormulaProcessPool, get_formula_pool, shutdown_formula_pool
from compute_service.server import WSGIDualStackServer, create_wsgi_app
from plugin.framework.child_runtime import Deadline
from compute_service.vision import get_vision_pool, shutdown_vision_pool
from tests.compute_service.conftest import decode_result_json, fake_child, get_free_port, pool_without_children, run_cell, session_worker, worker_sessions


def _wait_until_not_alive(worker: Any, timeout: float = 5.0) -> None:
    """Windows may lag after taskkill before is_alive() goes false."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not worker.is_alive():
            return
        time.sleep(0.05)
    assert not worker.is_alive()


@pytest.fixture(autouse=True)
def cleanup_formula_pool():
    yield
    shutdown_formula_pool()


@pytest.mark.parametrize("empty_sid", [None, ""])
def test_no_session_id_runs_isolated(empty_sid: str | None) -> None:
    """Missing or empty session_id runs one-shot isolated, while whitespace-only is rejected."""
    pool = FormulaProcessPool(settings=ComputeSettings(workers=1, idle_worker_ttl_sec=0))
    try:
        r1 = run_cell(pool, code="a = 5\nresult = a", session_id=empty_sid, req_id="iso-1")
        assert r1.get("status") == "ok" and r1.get("result") == 5
        r2 = run_cell(pool, code="result = a", session_id=empty_sid, req_id="iso-2")
        assert r2.get("status") == "error"
        with pool._cond:
            assert len(pool._sessions) == 0

        bad = run_cell(pool, code="result = 1", session_id="   ")
        assert bad.get("code") == "INVALID_REQUEST"
    finally:
        pool.shutdown()


def test_replaced_child_drops_only_that_workers_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """A replaced child drops that slot's session. Another slot's child stays.

    Rows store the Child, not a pid: a reused pid can belong to a live slot
    by the time the scan runs. A reservation has no child yet and stays.
    """
    from compute_service.formula_pool import _Session

    monkeypatch.setattr("compute_service.worker_base.BaseProcessWorker.respawn", lambda self, timeout_sec=0.0, deadline=None: None)
    pool = FormulaProcessPool(settings=ComputeSettings(workers=2, idle_worker_ttl_sec=0))
    try:
        owner, other = pool.workers
        replaced = fake_child(owner)
        current = fake_child(owner)
        kept = fake_child(other)
        owner.child = current
        other.child = kept
        with pool._cond:
            pool._sessions["owned"] = _Session(worker=owner, child=replaced)
            pool._sessions["reused"] = _Session(worker=other, child=kept)
        assert session_worker(pool, "owned") is None
        assert session_worker(pool, "reused") is other
        assert "owned" in pool._sessions
        assert pool._sessions["reused"].worker is other
        assert pool._sessions["reused"].child is kept
    finally:
        pool.shutdown()


def test_dead_managed_child_drops_its_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """A session whose Child is dead drops even when it is still the slot's child."""
    from compute_service.formula_pool import _Session

    monkeypatch.setattr("compute_service.worker_base.BaseProcessWorker.respawn", lambda self, timeout_sec=0.0, deadline=None: None)
    pool = FormulaProcessPool(settings=ComputeSettings(workers=2, idle_worker_ttl_sec=0))
    try:
        w1, w2 = pool.workers
        dead_child = fake_child(w1, alive=False)
        live_child = fake_child(w2, alive=True)
        w1.child = dead_child
        w2.child = live_child

        with pool._cond:
            pool._sessions["dead"] = _Session(worker=w1, child=dead_child)
            pool._sessions["live"] = _Session(worker=w2, child=live_child)
        assert session_worker(pool, "dead") is None
        assert session_worker(pool, "live") is w2
        assert "dead" in pool._sessions
        assert "live" in pool._sessions
    finally:
        pool.shutdown()


def test_shutdown_clears_sessions(monkeypatch: pytest.MonkeyPatch) -> None:
    """Shutdown drops session maps with the workers.

    A pool object kept after shutdown must not advertise session ids.
    """
    from compute_service.formula_pool import _Session

    monkeypatch.setattr("compute_service.worker_base.BaseProcessWorker.respawn", lambda self, timeout_sec=0.0, deadline=None: None)
    pool = FormulaProcessPool(settings=ComputeSettings(workers=1, idle_worker_ttl_sec=0))
    worker = pool.workers[0]
    with pool._cond:
        pool._sessions["live"] = _Session(worker=worker, child=worker.child)
    pool.shutdown()
    assert not pool._sessions


class TestFormulaPoolSupervisor:
    def test_session_locks_removed_and_reset_succeeds(self) -> None:
        """Verify dead session locks are removed and reset_session succeeds cleanly."""
        import compute_service.formula_worker as fw
        from compute_service.formula_worker import _handle_request

        assert not hasattr(fw, "_SESSION_RUN_LOCKS")
        assert not hasattr(fw, "_session_lock")
        assert not hasattr(fw, "release_session_lock")

        sid = "clean-reset-session"
        res = _handle_request({"action": "reset_session", "session_id": sid})
        assert res.get("status") == "ok"

    def test_default_pool_workers(self) -> None:
        pool = FormulaProcessPool(settings=ComputeSettings(default_timeout_sec=15))
        try:
            assert pool.is_enabled()
            assert len(pool.workers) == 2
        finally:
            pool.shutdown()

    def test_get_formula_pool_default_workers(self) -> None:
        pool = get_formula_pool()
        assert pool.is_enabled()
        assert len(pool.workers) == 2

    def test_pool_from_settings(self) -> None:
        cfg = ComputeSettings(workers=1, worker_max_tasks=42, default_timeout_sec=15)
        pool = FormulaProcessPool(settings=cfg)
        try:
            assert pool.is_enabled()
            assert len(pool.workers) == 1
            assert pool.max_tasks == 42
            assert pool.default_timeout_sec == 15
        finally:
            pool.shutdown()

    def test_pool_lifecycle(self) -> None:
        pool = FormulaProcessPool(settings=ComputeSettings(workers=2, default_timeout_sec=15))
        try:
            assert pool.is_enabled()
            assert len(pool.workers) == 2
            res = run_cell(pool, code="result = 10 + 20", req_id="f-1")
            assert res.get("id") == "f-1"
            assert res.get("status") == "ok"
            assert res.get("result") == 30
        finally:
            pool.shutdown()
            assert not pool.is_enabled()

    def test_check_dependencies_success(self) -> None:
        pool = FormulaProcessPool(settings=ComputeSettings(workers=1, default_timeout_sec=15))
        try:
            ok, err = pool.check_dependencies()
            assert ok is True
            assert err is None
        finally:
            pool.shutdown()

    def test_check_dependencies_missing(self) -> None:
        pool = FormulaProcessPool(settings=ComputeSettings(workers=1, default_timeout_sec=15))
        try:
            ok, err = pool.check_dependencies(["nonexistent_pkg_xyz_12345"])
            assert ok is False
            assert err is not None
            assert "nonexistent_pkg_xyz_12345" in err
            assert "./compute_service/start.sh" in err
        finally:
            pool.shutdown()

    def test_sticky_session_affinity(self) -> None:
        pool = FormulaProcessPool(settings=ComputeSettings(workers=4, default_timeout_sec=15))
        try:
            session_id = "test-workbook-session-42"
            # First cell execution: set a variable
            res1 = run_cell(pool, code="x = 100\nresult = x", session_id=session_id, req_id="c-1")
            assert res1.get("status") == "ok"
            assert res1.get("result") == 100

            # Second cell execution: read and increment variable in same session
            res2 = run_cell(pool, code="x += 50\nresult = x", session_id=session_id, req_id="c-2")
            assert res2.get("status") == "ok"
            assert res2.get("result") == 150
        finally:
            pool.shutdown()

    def test_worker_crash_recovery(self) -> None:
        pool = FormulaProcessPool(settings=ComputeSettings(workers=1, default_timeout_sec=10))
        try:
            worker = pool.workers[0]
            # Kill worker externally
            worker.kill()
            assert not worker.is_alive()

            # Next request should automatically spawn a fresh worker and succeed
            res = run_cell(pool, code="result = 'recovered'", req_id="f-rec")
            assert res.get("status") == "ok"
            assert res.get("result") == "recovered"
            assert worker.is_alive()
        finally:
            pool.shutdown()

    def test_idle_death_drops_other_shared_sessions(self) -> None:
        """A dead idle process must not keep its session map after respawn."""
        pool = FormulaProcessPool(settings=ComputeSettings(workers=1, default_timeout_sec=15))
        try:
            sid_a = "sess-a"
            sid_b = "sess-b"
            res_a = run_cell(pool, code="marker = 'a'\nresult = marker", session_id=sid_a)
            res_b = run_cell(pool, code="marker = 'b'\nresult = marker", session_id=sid_b)
            assert res_a.get("status") == "ok", res_a
            assert res_b.get("status") == "ok", res_b
            worker = pool._sessions[sid_a].worker
            assert pool._sessions[sid_b].worker is worker
            worker.kill()
            assert not worker.is_alive()
            again = run_cell(pool, code="result = marker", session_id=sid_a)
            assert again.get("status") == "error"
            assert again.get("session_reset") is True
            assert sid_b in pool._sessions
            assert session_worker(pool, sid_b) is None
            assert pool._sessions[sid_b].child is not worker.child
            assert session_worker(pool, sid_a) is worker
            assert worker.is_alive()
            again_b = run_cell(pool, code="result = marker", session_id=sid_b)
            assert again_b.get("status") == "error"
            assert again_b.get("session_reset") is True
        finally:
            pool.shutdown()

    def test_formula_worker_blocks_document_tools(self) -> None:
        pool = FormulaProcessPool(settings=ComputeSettings(workers=1, default_timeout_sec=30))
        try:
            res = run_cell(pool, code=("from plugin.scripting.host_tool_call import call_host_tool\ntry:\n    call_host_tool('list_open_documents')\n    result = 'called'\nexcept Exception as exc:\n    result = str(exc)\n"))
            assert res.get("status") == "ok", res
            assert "compute service" in str(res.get("result"))
        finally:
            pool.shutdown()

    def test_stderr_flood_does_not_deadlock(self, tmp_path) -> None:
        """Child OS-stderr flood must not deadlock the parent pickle reader."""
        from compute_service.worker_base import BaseProcessWorker

        script = tmp_path / "flood_worker.py"
        script.write_text(
            "\n".join(
                [
                    "import os, sys",
                    f"sys.path.insert(0, {os.path.abspath('.')!r})",
                    "from compute_service.worker_stdio import run_worker_stdio_loop",
                    "def handle(req):",
                    "    sys.stderr.write('x' * 200000)",
                    "    sys.stderr.flush()",
                    "    return {'status': 'ok', 'result': 1}",
                    "if __name__ == '__main__':",
                    "    raise SystemExit(run_worker_stdio_loop(handle))",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        worker = BaseProcessWorker(1, str(script), worker_name="Flood worker")
        try:
            res = worker.execute({"ping": True}, timeout_sec=10)
            assert res.get("status") == "ok"
            assert res.get("result") == 1
        finally:
            worker.kill()

    def test_spawn_timeout_logs_stderr_snippet(self, tmp_path, caplog, monkeypatch) -> None:
        """Handshake TimeoutExpired must log child stderr (H2), not a bare one-liner."""
        from plugin.framework import process_worker as pw
        from compute_service.worker_base import BaseProcessWorker

        monkeypatch.setattr(pw, "SPAWN_READY_TIMEOUT_SEC", 0.5)
        script = tmp_path / "hang_worker.py"
        script.write_text("\n".join(["import sys, time", "sys.stderr.write('hang-before-ready\\n')", "sys.stderr.flush()", "time.sleep(30)"]), encoding="utf-8")
        with caplog.at_level(logging.ERROR, logger="compute_service.worker"):
            worker = BaseProcessWorker(1, str(script), worker_name="Hang worker")
        try:
            joined = "\n".join(r.getMessage() for r in caplog.records)
            assert "spawn handshake timed out" in joined
            assert "returncode=" in joined
            assert "hang-before-ready" in joined
            assert not worker.is_alive()
        finally:
            worker.kill()

    def test_spawn_stdout_garbage_fails_fast(self, tmp_path, caplog) -> None:
        """Text on stdout (Keith 2026-09-02: frame size 1165128303 == b'Erro') must not wait 15s."""
        from compute_service.worker_base import BaseProcessWorker

        script = tmp_path / "garbage_worker.py"
        script.write_text("\n".join(["import sys, time", "sys.stdout.write('Error: boom\\n')", "sys.stdout.flush()", "time.sleep(30)"]), encoding="utf-8")
        t0 = time.monotonic()
        with caplog.at_level(logging.ERROR, logger="compute_service.worker"):
            worker = BaseProcessWorker(1, str(script), worker_name="Garbage worker")
        elapsed = time.monotonic() - t0
        try:
            joined = "\n".join(r.getMessage() for r in caplog.records)
            assert elapsed < 3.0, elapsed
            assert "header=b'Erro'" in joined
            assert "stdout_rest=" in joined
            assert "spawn handshake timed out" not in joined
            assert not worker.is_alive()
        finally:
            worker.kill()

    def test_spawn_eof_before_ready_is_killed(self, tmp_path, caplog) -> None:
        """A child that exits before any frame must not be marked ready."""
        from compute_service.worker_base import BaseProcessWorker

        script = tmp_path / "exit_worker.py"
        script.write_text("import sys\nsys.exit(0)\n", encoding="utf-8")
        t0 = time.monotonic()
        with caplog.at_level(logging.ERROR, logger="compute_service.worker"):
            worker = BaseProcessWorker(1, str(script), worker_name="Exit worker")
        try:
            joined = "\n".join(r.getMessage() for r in caplog.records)
            assert time.monotonic() - t0 < 3.0
            assert "was not ready" in joined
            assert "status=None" in joined
            assert not worker.is_alive()
        finally:
            worker.kill()

    def test_spawn_non_ready_status_is_killed(self, tmp_path, caplog) -> None:
        """A live child whose first dict is not status=ready must be killed."""
        from compute_service.worker_base import BaseProcessWorker

        root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
        script = tmp_path / "booting_worker.py"
        script.write_text("\n".join(["import sys, time", f"sys.path.insert(0, {root!r})", "from plugin.framework.ipc import write_pickle_frame", "write_pickle_frame(sys.stdout.buffer, {'status': 'booting'})", "sys.stdout.buffer.flush()", "time.sleep(30)"]), encoding="utf-8")
        t0 = time.monotonic()
        with caplog.at_level(logging.ERROR, logger="compute_service.worker"):
            worker = BaseProcessWorker(1, str(script), worker_name="Booting worker")
        try:
            joined = "\n".join(r.getMessage() for r in caplog.records)
            assert time.monotonic() - t0 < 3.0
            assert "was not ready" in joined
            assert "booting" in joined
            assert not worker.is_alive()
        finally:
            worker.kill()

    @pytest.mark.skipif(sys.platform == "win32", reason="zombie reaping and SIGKILL are POSIX")
    def test_spawn_reaps_exited_child(self, tmp_path) -> None:
        """Replacing a dead Popen must wait() it so the pid is not a zombie."""
        import signal

        from compute_service.worker_base import BaseProcessWorker

        root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
        script = tmp_path / "reap_worker.py"
        script.write_text(
            "\n".join(["import sys", f"sys.path.insert(0, {root!r})", "from compute_service.worker_stdio import run_worker_stdio_loop", "def handle(req):", "    return {'status': 'ok'}", "if __name__ == '__main__':", "    raise SystemExit(run_worker_stdio_loop(handle))", ""]), encoding="utf-8"
        )
        worker = BaseProcessWorker(1, str(script), worker_name="Reap worker")
        previous = worker.process
        assert previous is not None
        os.kill(previous.pid, signal.SIGKILL)
        try:
            worker.respawn()
            assert previous.returncode is not None
            assert worker.is_alive()
            assert worker.process is not previous
        finally:
            worker.kill()

    def test_spawn_scrubs_parent_env(self, tmp_path, monkeypatch) -> None:
        from compute_service.worker_base import BaseProcessWorker

        monkeypatch.setenv("PYTHONPATH", "/should-not-leak")
        monkeypatch.setenv("PYTHON_COMPUTE_TEST_SECRET", "hidden")
        root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
        script = tmp_path / "env_worker.py"
        script.write_text(
            "\n".join(
                [
                    "import os, sys",
                    f"sys.path.insert(0, {root!r})",
                    "from compute_service.worker_stdio import run_worker_stdio_loop",
                    "def handle(req):",
                    "    return {'status': 'ok', 'pythonpath': os.environ.get('PYTHONPATH'), 'secret': os.environ.get('PYTHON_COMPUTE_TEST_SECRET')}",
                    "if __name__ == '__main__':",
                    "    raise SystemExit(run_worker_stdio_loop(handle))",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        worker = BaseProcessWorker(1, str(script), worker_name="Env worker")
        try:
            res = worker.execute({}, timeout_sec=10)
            assert res.get("status") == "ok"
            assert res.get("pythonpath") is None
            assert res.get("secret") is None
        finally:
            worker.kill()

    def test_slow_spawn_is_not_immediately_idle(self, tmp_path) -> None:
        """last_active is taken after the handshake, not before the spawn loop."""
        from compute_service.worker_base import BaseProcessPool

        root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
        script = tmp_path / "slow_ready.py"
        script.write_text(
            "\n".join(
                ["import sys, time", f"sys.path.insert(0, {root!r})", "time.sleep(0.35)", "from compute_service.worker_stdio import run_worker_stdio_loop", "def handle(req):", "    return {'status': 'ok'}", "if __name__ == '__main__':", "    raise SystemExit(run_worker_stdio_loop(handle))", ""]
            ),
            encoding="utf-8",
        )
        pool = BaseProcessPool(str(script), num_workers=1, idle_worker_ttl_sec=0.2, worker_name="Slow worker")
        try:
            worker = pool.workers[0]
            pool._evict_idle_workers()
            assert worker.is_alive()
        finally:
            pool.shutdown()

    def test_formula_worker_import_skips_sandbox(self) -> None:
        import subprocess
        import sys

        root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
        code = "\n".join(["import sys", "import compute_service.formula_worker", "assert 'plugin.scripting.venv.venv_sandbox' not in sys.modules"])
        env = os.environ.copy()
        env["PYTHONPATH"] = root + os.pathsep + env.get("PYTHONPATH", "")
        proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, check=False)
        assert proc.returncode == 0, proc.stderr

    def test_shared_and_isolated_exclusive_occupancy(self) -> None:
        """Isolated work does not run on the process that owns a shared session."""
        pool = FormulaProcessPool(settings=ComputeSettings(workers=2, default_timeout_sec=15))
        try:
            sid = "occupancy-session"
            first = run_cell(pool, code="x = 5\nresult = x", session_id=sid, req_id="occ-1")
            assert first.get("status") == "ok"
            owner = session_worker(pool, sid)
            assert owner is not None and owner.process is not None
            shared_pid = owner.process.pid

            before = owner.tasks_executed
            isolated = run_cell(pool, code="result = 99", timeout_sec=10, req_id="occ-iso")
            assert isolated.get("status") == "ok"
            assert isolated.get("result") == 99
            # The isolated cell did not run on the process that owns the session.
            assert owner.tasks_executed == before
            assert owner.process is not None and owner.process.pid == shared_pid
            shared = run_cell(pool, code="result = x", session_id=sid, timeout_sec=10, req_id="occ-2")
            assert shared.get("status") == "ok"
            assert shared.get("result") == 5
        finally:
            pool.shutdown()

    def test_timeout_watchdog_kill(self) -> None:
        pool = FormulaProcessPool(settings=ComputeSettings(workers=1, default_timeout_sec=1))
        try:
            # Code that takes longer than 1s timeout
            res = run_cell(pool, code="import time\ntime.sleep(5)\nresult = 'done'", timeout_sec=1, req_id="f-timeout")
            assert res.get("status") == "error"
            # Code is either EXECUTION_TIMEOUT from pool or timeout from sandbox
            assert "code execution exceeded the maximum execution time of 1 seconds" in str(res.get("error", "")).lower() or "timeout" in str(res.get("code", "")).lower()
            nxt = run_cell(pool, code="result = 1", timeout_sec=10, req_id="f-timeout-next")
            assert nxt.get("status") == "ok"
            assert nxt.get("result") == 1
        finally:
            pool.shutdown()

    def test_timeout_tight_loop_then_next_cell(self) -> None:
        pool = FormulaProcessPool(settings=ComputeSettings(workers=1, default_timeout_sec=1))
        try:
            res = run_cell(pool, code="while True:\n    pass\nresult = 0", timeout_sec=1, req_id="f-loop")
            assert res.get("status") == "error"
            nxt = run_cell(pool, code="result = 2", timeout_sec=10, req_id="f-loop-next")
            assert nxt.get("status") == "ok"
            assert nxt.get("result") == 2
        finally:
            pool.shutdown()

    def test_shared_hang_does_not_wedge_pool(self) -> None:
        pool = FormulaProcessPool(settings=ComputeSettings(workers=2, default_timeout_sec=1))
        try:
            hung = run_cell(pool, code="import time\ntime.sleep(5)\nresult = 1", session_id="s-hang", timeout_sec=1, req_id="sh-1")
            assert hung.get("status") == "error"
            iso = run_cell(pool, code="result = 3", timeout_sec=10, req_id="sh-iso")
            assert iso.get("status") == "ok"
            assert iso.get("result") == 3
        finally:
            pool.shutdown()

    @pytest.mark.skipif(sys.platform == "win32", reason="the in-child cell timeout uses signal.alarm; Windows falls back to the host kill")
    def test_shared_timeout_keeps_other_session(self) -> None:
        """A cell timeout must not SIGKILL the worker and drop other workbooks on it."""
        pool = FormulaProcessPool(settings=ComputeSettings(workers=1, default_timeout_sec=15))
        try:
            kept = run_cell(pool, code="keep = 7\nresult = keep", session_id="doc-keep", req_id="keep-1")
            assert kept.get("status") == "ok"
            assert kept.get("result") == 7
            worker = pool.workers[0]
            pid = worker.process.pid if worker.process is not None else None
            hung = run_cell(pool, code="import time\ntime.sleep(30)\nresult = 1", session_id="doc-hang", timeout_sec=1, req_id="hang-1")
            assert hung.get("status") == "error"
            assert hung.get("code") != "EXECUTION_TIMEOUT"
            assert worker.process is not None and worker.process.pid == pid
            again = run_cell(pool, code="result = keep", session_id="doc-keep", req_id="keep-2")
            assert again.get("status") == "ok"
            assert again.get("result") == 7
        finally:
            pool.shutdown()

    def test_isolated_does_not_leak_globals(self) -> None:
        pool = FormulaProcessPool(settings=ComputeSettings(workers=1, default_timeout_sec=15))
        try:
            first = run_cell(pool, code="x = 1\nresult = x", req_id="iso-1")
            assert first.get("status") == "ok"
            second = run_cell(pool, code="result = x", req_id="iso-2")
            assert second.get("status") == "error"
        finally:
            pool.shutdown()

    def test_shared_sessions_do_not_cross(self) -> None:
        pool = FormulaProcessPool(settings=ComputeSettings(workers=2, default_timeout_sec=15))
        try:
            a = run_cell(pool, code="x = 11\nresult = x", session_id="doc-A", req_id="xa")
            assert a.get("status") == "ok"
            b = run_cell(pool, code="result = x", session_id="doc-B", req_id="xb")
            assert b.get("status") == "error"
            a2 = run_cell(pool, code="result = x", session_id="doc-A", req_id="xa2")
            assert a2.get("status") == "ok"
            assert a2.get("result") == 11
        finally:
            pool.shutdown()

    def test_recycled_worker_is_alive_after_release(self) -> None:
        """max_tasks kills the slot and leaves it dead. The next execute respawns it."""
        pool = FormulaProcessPool(settings=ComputeSettings(workers=1, default_timeout_sec=15, worker_max_tasks=1))
        try:
            worker = pool.workers[0]
            res = run_cell(pool, code="result = 'first'", req_id="recycle-1")
            assert res.get("status") == "ok"
            assert not worker.is_alive()
            with pool._cond:
                assert worker not in pool._idle
                assert worker not in pool._leased
            # The slot is dead, so this cell can succeed only if execute respawns.
            # max_tasks is 1, so release retires that new process too.
            again = run_cell(pool, code="result = 'second'", req_id="recycle-2")
            assert again.get("status") == "ok", again
            assert again.get("result") == "second"
            assert not worker.is_alive()
        finally:
            pool.shutdown()

    def test_recycle_after_shutdown_does_not_orphan_process(self) -> None:
        """If shutdown() wins the race, recycle must not leave a newly spawned child running."""
        pool = FormulaProcessPool(settings=ComputeSettings(workers=1, default_timeout_sec=15, worker_max_tasks=1))
        worker = pool.workers[0]
        worker.tasks_executed = 1
        pool.shutdown()
        pool.release_worker(worker)
        assert not worker.is_alive(), "Recycle spawn after shutdown must be killed"

    def test_sticky_routing_no_index_error_on_concurrent_shutdown(self) -> None:
        """Sticky routing must not raise IndexError if shutdown() clears workers concurrently (Bug 4 fix)."""
        pool = FormulaProcessPool(settings=ComputeSettings(workers=4, default_timeout_sec=5))
        errors: list[Exception] = []

        def _shutdown_soon() -> None:
            time.sleep(0.02)
            pool.shutdown()

        shutdown_thread = threading.Thread(target=_shutdown_soon)
        shutdown_thread.start()
        # Repeatedly attempt sticky-mode execution while shutdown races; must not raise IndexError
        for i in range(20):
            try:
                run_cell(pool, code="result = 1", session_id=f"race-session-{i % 4}", timeout_sec=2, req_id=f"race-{i}")
            except IndexError as exc:
                errors.append(exc)
            except Exception:
                pass  # timeout / pool-busy during shutdown is fine
        shutdown_thread.join(timeout=5)
        assert not errors, f"IndexError raised during concurrent shutdown+sticky routing: {errors}"

    def test_shared_session_persists_across_max_tasks(self) -> None:
        """Shared session worker must NOT recycle at max_tasks, keeping state intact."""
        pool = FormulaProcessPool(settings=ComputeSettings(workers=1, default_timeout_sec=15, worker_max_tasks=2))
        try:
            sid = "shared-persist-test"
            r1 = run_cell(pool, code="val = 100\nresult = val", session_id=sid, req_id="sp-1")
            assert r1.get("status") == "ok"
            assert r1.get("result") == 100

            r2 = run_cell(pool, code="val += 50\nresult = val", session_id=sid, req_id="sp-2")
            assert r2.get("status") == "ok"
            assert r2.get("result") == 150

            # tasks_executed is now 2 (== max_tasks). Without session awareness, release_worker would kill process.
            # With session awareness, recycling is skipped and state is preserved.
            r3 = run_cell(pool, code="val += 25\nresult = val", session_id=sid, req_id="sp-3")
            assert r3.get("status") == "ok"
            assert r3.get("result") == 175, "State must persist across task count >= max_tasks for shared sessions"

            # Resetting session clears active session tracking and recycles if task count >= max_tasks
            worker_before = pool.workers[0]
            pid_before = worker_before.process.pid if worker_before.process else None

            reset_res = pool.reset_session(sid)
            assert reset_res.get("status") == "ok"
            # Unknown / already-gone stays idempotent ok (HTTP /v1/session/reset contract).
            assert pool.reset_session("never-mapped-session").get("status") == "ok"

            # Execute an isolated task; worker will recycle because tasks_executed (3) >= max_tasks (2) and no active sessions
            r4 = run_cell(pool, code="result = 'fresh'", req_id="sp-4")
            assert r4.get("status") == "ok"

            pid_after = pool.workers[0].process.pid if pool.workers[0].process else None
            assert pid_after != pid_before, "Worker should recycle after session is reset when tasks exceed max_tasks"
        finally:
            pool.shutdown()

    def test_session_ttl_evicts_idle_session(self) -> None:
        """An idle worker past idle TTL is killed even though it holds a session. The next sticky call reports session_reset."""
        pool = FormulaProcessPool(settings=ComputeSettings(workers=1, default_timeout_sec=15, idle_worker_ttl_sec=3600.0))
        try:
            sid = "ttl-evict-test"
            r1 = run_cell(pool, code="val = 42\nresult = val", session_id=sid, req_id="ttl-1")
            assert r1.get("status") == "ok"
            assert session_worker(pool, sid) is not None

            worker = pool.workers[0]
            with pool._cond:
                pool._idle[worker] = time.monotonic() - 4000.0
            pool._evict_idle_workers()
            assert session_worker(pool, sid) is None, "Session should be dropped when its worker is killed"

            assert not worker.is_alive()
            with pool._cond:
                assert worker not in pool._idle
                assert sid in pool._sessions

            # Later sticky call reports session_reset and does not see the old name
            r2 = run_cell(pool, code="try:\n    result = val\nexcept NameError:\n    result = 'missing'", session_id=sid, req_id="ttl-2")
            assert r2.get("status") == "ok", r2
            assert r2.get("result") == "missing"
            assert r2.get("session_reset") is True
        finally:
            pool.shutdown()

    def test_idle_worker_reaper(self, caplog: pytest.LogCaptureFixture) -> None:
        """Idle worker reaper must terminate workers idle for longer than idle_worker_ttl_sec."""
        pool = FormulaProcessPool(settings=ComputeSettings(workers=1, default_timeout_sec=15, idle_worker_ttl_sec=3600.0))
        try:
            # Run one task so worker is active and returned to idle
            res = run_cell(pool, code="result = 123", req_id="idle-1")
            assert res.get("status") == "ok"

            worker = pool.workers[0]
            assert worker.is_alive()

            # Simulate passage of idle time and trigger reaper eviction
            with pool._cond:
                pool._idle[worker] = time.monotonic() - 4000.0
            with caplog.at_level(logging.INFO, logger="compute_service.worker"):
                pool._evict_idle_workers()
            assert "idle for >" in caplog.text
            assert not worker.is_alive(), "Idle worker process should be killed by idle worker reaper"

            # Subsequent execution lazily re-spawns worker
            res2 = run_cell(pool, code="result = 456", req_id="idle-2")
            assert res2.get("status") == "ok"
            assert res2.get("result") == 456
            assert worker.is_alive(), "Worker should re-spawn lazily on next request"
        finally:
            pool.shutdown()

    def test_busy_worker_not_evicted_and_preserves_session(self) -> None:
        """A leased worker is absent from _idle, so _evict_idle_workers leaves it up.
        A subsequent sticky call reads variables set previously, with no session_reset.
        """
        pool = FormulaProcessPool(settings=ComputeSettings(workers=1, default_timeout_sec=15, idle_worker_ttl_sec=0.05))
        try:
            sid = "busy-worker-test"
            r1 = run_cell(pool, code="val = 'persisted'\nresult = val", session_id=sid)
            assert r1.get("status") == "ok"
            worker = session_worker(pool, sid)
            assert worker is not None

            # Lease the worker so it is absent from _idle
            with pool.leased(worker, timeout_sec=5.0) as leased:
                assert leased is worker
                with pool._cond:
                    assert worker not in pool._idle
                # Call eviction while leased
                pool._evict_idle_workers()
                assert worker.is_alive()

            # Second sticky call still reads the name set before the lease. No session_reset.
            r2 = run_cell(pool, code="result = val", session_id=sid)
            assert r2.get("status") == "ok"
            assert r2.get("result") == "persisted"
            assert r2.get("session_reset") is not True
        finally:
            pool.shutdown()

    def test_idle_worker_eviction_drops_multiple_sessions(self) -> None:
        """When an idle worker holding multiple sessions is evicted, all sessions are lost and report session_reset once."""
        pool = FormulaProcessPool(settings=ComputeSettings(workers=1, default_timeout_sec=15, idle_worker_ttl_sec=3600.0))
        try:
            sid1 = "sess-1"
            sid2 = "sess-2"
            r1 = run_cell(pool, code="val1 = 111\nresult = val1", session_id=sid1)
            assert r1.get("status") == "ok"
            r2 = run_cell(pool, code="val2 = 222\nresult = val2", session_id=sid2)
            assert r2.get("status") == "ok"

            worker = session_worker(pool, sid1)
            assert worker is not None
            assert session_worker(pool, sid2) is worker

            # Backdate the _idle stamp past the TTL
            with pool._cond:
                pool._idle[worker] = time.monotonic() - 4000.0
            pool._evict_idle_workers()

            assert session_worker(pool, sid1) is None
            assert session_worker(pool, sid2) is None
            with pool._cond:
                assert sid1 in pool._sessions
                assert sid2 in pool._sessions

            # Each next sticky call reports session_reset once
            r3 = run_cell(pool, code="try:\n    result = val1\nexcept NameError:\n    result = 'gone'", session_id=sid1)
            assert r3.get("status") == "ok"
            assert r3.get("result") == "gone"
            assert r3.get("session_reset") is True

            r4 = run_cell(pool, code="try:\n    result = val2\nexcept NameError:\n    result = 'gone'", session_id=sid2)
            assert r4.get("status") == "ok"
            assert r4.get("result") == "gone"
            assert r4.get("session_reset") is True
        finally:
            pool.shutdown()

    def test_reset_keeps_map_when_worker_is_busy(self) -> None:
        pool = FormulaProcessPool(settings=ComputeSettings(workers=1, default_timeout_sec=15))
        try:
            sid = "busy-reset"
            ok = run_cell(pool, code="x = 1\nresult = x", session_id=sid, req_id="busy-1")
            assert ok.get("status") == "ok"
            worker = pool._sessions[sid].worker
            held = pool.lease_specific(worker, timeout_sec=1)
            assert held is worker
            try:
                res = pool.reset_session(sid, timeout_sec=0.05)
                assert res.get("code") == "WORKER_POOL_BUSY"
                assert session_worker(pool, sid) is worker
            finally:
                pool.release_worker(held)
        finally:
            pool.shutdown()

    def test_reset_lease_failure_keeps_row(self) -> None:
        """A busy reset keeps the row and does not report session_reset on next call."""
        pool = FormulaProcessPool(settings=ComputeSettings(workers=1, default_timeout_sec=15))
        try:
            sid = "busy-reset"
            ok = run_cell(pool, code="x = 1\nresult = x", session_id=sid, req_id="busy-1")
            assert ok.get("status") == "ok"
            worker = pool._sessions[sid].worker
            held = pool.lease_specific(worker, timeout_sec=1)
            assert held is worker
            try:
                res = pool.reset_session(sid, timeout_sec=0.05)
                assert res.get("code") == "WORKER_POOL_BUSY"
                with pool._cond:
                    assert sid in pool._sessions
            finally:
                pool.release_worker(held)
            again = run_cell(pool, code="result = x", session_id=sid)
            assert again.get("status") == "ok"
            assert again.get("result") == 1
            assert again.get("session_reset") is not True
        finally:
            pool.shutdown()

    def test_three_hundred_shared_sessions_do_not_fail_or_evict(self) -> None:
        """Active shared sessions are uncapped; 300 sessions neither fail nor evict."""
        pool = FormulaProcessPool(settings=ComputeSettings(workers=1, default_timeout_sec=15, idle_worker_ttl_sec=0))
        try:
            assert not hasattr(pool, "_max_sessions")
            worker = pool.workers[0]

            def mock_execute(payload: dict[str, Any], timeout_sec: float = 15.0, *, req_id: Any = None) -> dict[str, Any]:
                del timeout_sec, req_id
                if payload.get("action") == "reset_session":
                    raise AssertionError("reset_session should not be called")
                return {"status": "ok", "result": 1}

            setattr(worker, "execute", mock_execute)

            for i in range(300):
                sid = f"s{i}"
                res = run_cell(pool, code="result = 1", session_id=sid)
                assert res.get("status") == "ok"
                assert res.get("session_reset") is not True

            assert len(pool._sessions) == 300
            assert "s0" in pool._sessions
            assert "s299" in pool._sessions
        finally:
            pool.shutdown()

    def test_reset_keeps_map_when_worker_reset_fails(self, caplog: pytest.LogCaptureFixture) -> None:
        """A non-ok reset must not forget a namespace the worker still holds."""
        pool = FormulaProcessPool(settings=ComputeSettings(workers=1, default_timeout_sec=15))
        try:
            sid = "reset-fail-keeps-map"
            ok = run_cell(pool, code="x = 9\nresult = x", session_id=sid, req_id="rf-1")
            assert ok.get("status") == "ok"
            worker = pool._sessions[sid].worker
            real_execute = worker.execute

            def fail_reset(payload: dict, timeout_sec: float = 5.0, *, req_id: Any = None) -> dict:
                if payload.get("action") == "reset_session":
                    return {"status": "error", "error": "namespace still held"}
                return real_execute(payload, timeout_sec, req_id=req_id)

            setattr(worker, "execute", fail_reset)
            with caplog.at_level(logging.ERROR, logger="compute_service.formula"):
                res = pool.reset_session(sid)
            assert res.get("status") == "error"
            assert session_worker(pool, sid) is worker
            assert sid in worker_sessions(pool, worker)
            assert "keeping session map" in caplog.text
            again = run_cell(pool, code="result = x", session_id=sid, req_id="rf-2")
            assert again.get("status") == "ok"
            assert again.get("result") == 9
        finally:
            pool.shutdown()

    def test_reset_clears_init_companion(self) -> None:
        pool = FormulaProcessPool(settings=ComputeSettings(workers=1, default_timeout_sec=15))
        try:
            sid = "init-reset"
            init = "items = [1]"
            first = run_cell(pool, code="items.append(2)\nresult = list(items)", session_id=sid, init_script=init, req_id="init-1")
            assert first.get("status") == "ok"
            assert first.get("result") == [1, 2]
            reset = pool.reset_session(sid)
            assert reset.get("status") == "ok"
            second = run_cell(pool, code="result = list(items)", session_id=sid, init_script=init, req_id="init-2")
            assert second.get("status") == "ok"
            assert second.get("result") == [1]
        finally:
            pool.shutdown()

    def test_worker_ignores_data_field(self) -> None:
        """data is not a payload field; the child reads only data_json."""
        from compute_service.formula_worker import _handle_request

        plain = decode_result_json(_handle_request({"id": "data-field", "code": "result = data", "data": [1, 2, 3]}))
        assert plain.get("result") != [1, 2, 3]

    def test_worker_error_omits_traceback(self) -> None:
        from compute_service.formula_worker import _handle_request

        res = _handle_request({"id": "bad-json", "code": "result = 1", "wire": "json_forward", "data_json": b"not-json"})
        raw = res.get("result_json")
        assert isinstance(raw, (bytes, bytearray))
        body = json.loads(bytes(raw))
        assert body.get("status") == "error"
        assert "traceback" not in body
        assert "Traceback" not in body.get("error", "")

    def test_evicted_idle_worker_removed_from_idle_during_kill(self) -> None:
        """Evicted workers leave the idle set. The next execute respawns the cold slot."""
        pool = FormulaProcessPool(settings=ComputeSettings(workers=2, default_timeout_sec=15, idle_worker_ttl_sec=3600.0))
        try:
            # Execute a task on each worker to populate _idle with both
            for i in range(2):
                res = run_cell(pool, code=f"result = {i}", req_id=f"evict-{i}")
                assert res.get("status") == "ok"

            # Both workers should be idle now
            with pool._cond:
                assert len(pool._idle) == 2

            # Simulate only one worker being stale
            w0 = pool.workers[0]
            with pool._cond:
                pool._idle[w0] = time.monotonic() - 4000.0

            pool._evict_idle_workers()

            # Worker process must be dead, and a dead pid is not idle.
            # The next lease claims the cold slot and execute respawns it.
            assert not w0.is_alive(), "Evicted worker process must be killed"
            with pool._cond:
                assert w0 not in pool._idle, "Dead worker must not sit in the idle set"
                assert len(pool._idle) == 1, "Only the live worker stays idle"

            # Verify lazy re-spawn works
            res = run_cell(pool, code="result = 999", req_id="respawn-1")
            assert res.get("status") == "ok"
            assert res.get("result") == 999
        finally:
            pool.shutdown()

    def test_active_session_routes_to_mapped_worker(self) -> None:
        """Shared session execution routes to _active_sessions[sid] if already mapped."""
        pool = FormulaProcessPool(settings=ComputeSettings(workers=3, default_timeout_sec=15))
        try:
            sid = "affinity-direct"
            # Map session to worker #2 explicitly, including that worker's child.
            # A map entry for a different child is a stale kernel and is dropped.
            target_worker = pool.workers[2]
            with pool._cond:
                from compute_service.formula_pool import _Session

                assert target_worker.child is not None
                pool._sessions[sid] = _Session(worker=target_worker, child=target_worker.child)

            res = run_cell(pool, code="state = 42\nresult = state", session_id=sid)
            assert res.get("status") == "ok"
            assert res.get("result") == 42
            with pool._cond:
                assert session_worker(pool, sid) is target_worker
        finally:
            pool.shutdown()

    def test_pool_shutdown_signals_reaper_event(self) -> None:
        """Pool shutdown sets _reaper_stop_event so reaper threads terminate promptly."""
        pool = FormulaProcessPool(settings=ComputeSettings(workers=1, idle_worker_ttl_sec=3600.0))
        assert not pool._reaper_stop_event.is_set()
        pool.shutdown()
        assert pool._reaper_stop_event.is_set()

    def test_evict_idle_workers_skips_dead_worker(self) -> None:
        """_evict_idle_workers must skip dead workers already in _idle via continue."""
        pool = FormulaProcessPool(settings=ComputeSettings(workers=1, idle_worker_ttl_sec=0.01))
        try:
            worker = pool.lease_any(2)
            assert worker is not None
            worker.kill()
            with pool._cond:
                pool._idle[worker] = time.monotonic() - 100.0
                pool._leased.discard(worker)
            pool._evict_idle_workers()
            with pool._cond:
                assert worker not in pool._idle
        finally:
            pool.shutdown()

    def test_deadline_left_is_negative_when_spent(self) -> None:
        from plugin.framework.child_runtime import Deadline

        future = Deadline.from_absolute(10.0, time.monotonic() + 10.0)
        assert future.left() > 0.0
        assert future.usable() is not None
        spent = Deadline.from_absolute(10.0, time.monotonic() - 10.0)
        assert spent.left() < 0
        assert spent.usable() is None

    @pytest.mark.skipif(sys.platform == "win32", reason="uses SIGKILL")
    def test_shared_session_dies_with_its_process(self) -> None:
        """Killing the pid drops the session. A respawn is not the same workbook."""
        import signal

        pool = FormulaProcessPool(settings=ComputeSettings(workers=1, default_timeout_sec=15))
        try:
            sid = "dies-with-pid"
            created = run_cell(pool, code="keep = 5\nresult = keep", session_id=sid, req_id="die-1")
            assert created.get("status") == "ok"
            assert created.get("result") == 5
            owner = session_worker(pool, sid)
            assert owner is not None and owner.process is not None
            pid = owner.process.pid
            os.kill(pid, signal.SIGKILL)
            owner.process.wait(timeout=2)
            assert session_worker(pool, sid) is None
            again = run_cell(pool, code="result = keep", session_id=sid, timeout_sec=15, req_id="die-2")
            assert again.get("status") == "error"
            assert again.get("result") != 5
            assert again.get("session_reset") is True
        finally:
            pool.shutdown()

    def test_sigkill_drops_every_session_on_the_pid(self) -> None:
        pool = FormulaProcessPool(settings=ComputeSettings(workers=1, default_timeout_sec=15))
        try:
            first = run_cell(pool, code="a = 1\nresult = a", session_id="sess-a")
            second = run_cell(pool, code="b = 2\nresult = b", session_id="sess-b")
            assert first.get("status") == "ok"
            assert second.get("status") == "ok"
            owner = session_worker(pool, "sess-a")
            assert owner is not None
            assert owner is session_worker(pool, "sess-b")
            owner.kill()
            assert session_worker(pool, "sess-a") is None
            assert session_worker(pool, "sess-b") is None
        finally:
            pool.shutdown()

    @pytest.mark.skipif(sys.platform == "win32", reason="the in-child cell timeout uses signal.alarm; Windows falls back to the host kill")
    def test_timeout_does_not_sigkill_healthy_shared_kernel(self) -> None:
        """A sleep past the cell budget returns an error frame. The pid stays.

        The child used to be given the original timeout_sec while the host
        read only the time left on the deadline, so this sleep was SIGKILL
        and every other workbook on that process disappeared.
        """
        pool = FormulaProcessPool(settings=ComputeSettings(workers=1, default_timeout_sec=30))
        try:
            kept = run_cell(pool, code="keep = 7\nresult = keep", session_id="doc-keep", req_id="budget-keep")
            assert kept.get("status") == "ok"
            owner = session_worker(pool, "doc-keep")
            assert owner is not None and owner.process is not None
            pid = owner.process.pid
            # The budget is the original 30s. The deadline is only about a
            # second from now, which is what the child must honor.
            hung = run_cell(pool, code="import time\ntime.sleep(8)\nresult = 1", session_id="doc-hang", deadline=Deadline.from_absolute(30.0, time.monotonic() + 1.2), req_id="budget-hang")
            assert hung.get("status") == "error"
            assert hung.get("code") != "EXECUTION_TIMEOUT"
            assert owner.process is not None and owner.process.pid == pid
            assert owner.is_alive()
            again = run_cell(pool, code="result = keep", session_id="doc-keep", timeout_sec=15, req_id="budget-again")
            assert again.get("status") == "ok"
            assert again.get("result") == 7
        finally:
            pool.shutdown()

    def test_isolated_leases_session_process_when_all_workers_have_sessions(self) -> None:
        pool = FormulaProcessPool(settings=ComputeSettings(workers=1, default_timeout_sec=15))
        try:
            created = run_cell(pool, code="keep = 1\nresult = keep", session_id="only-shared")
            assert created.get("status") == "ok"
            owner = session_worker(pool, "only-shared")
            assert owner is not None and owner.process is not None
            pid = owner.process.pid
            ran = run_cell(pool, code="result = 1", timeout_sec=1, req_id="iso-blocked")
            assert ran.get("status") == "ok"
            assert ran.get("result") == 1
            assert owner.is_alive()
            assert owner.process is not None and owner.process.pid == pid
        finally:
            pool.shutdown()

    @pytest.mark.skipif(sys.platform == "win32", reason="uses SIGKILL")
    def test_reap_dead_worker_does_not_respawn(self) -> None:
        """An exited pid is marked lost. Reaping it must not lease and respawn."""
        pool = FormulaProcessPool(settings=ComputeSettings(workers=1, default_timeout_sec=15, idle_worker_ttl_sec=3600.0))
        try:
            sid = "ttl-dead"
            created = run_cell(pool, code="result = 1", session_id=sid, req_id="ttl-dead-1")
            assert created.get("status") == "ok"
            worker = pool.workers[0]
            old_child = worker.child
            proc = worker.process
            assert proc is not None
            proc.kill()
            proc.wait(timeout=5)
            assert session_worker(pool, sid) is None
            assert worker.child is old_child
            assert worker.process is proc
            assert not worker.is_alive()
            with pool._cond:
                assert sid in pool._sessions
                assert pool._sessions[sid].child is old_child
        finally:
            pool.shutdown()


class TestFormulaHttpEndpoint:
    @pytest.fixture
    def formula_server(self):
        port = get_free_port()
        settings = ComputeSettings(host="127.0.0.1", port=port, api_key="formula-secret")
        app = create_wsgi_app(settings)
        server = WSGIDualStackServer("127.0.0.1", port, listener_threads=8)
        server.set_app(app)

        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        time.sleep(0.15)
        yield f"http://127.0.0.1:{port}"
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)

    def _post(self, url: str, payload: dict, headers: dict | None = None, path: str = "/v1/execute") -> tuple[int, dict]:
        req_headers = {"Content-Type": "application/json"}
        if headers:
            req_headers.update(headers)
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(f"{url}{path}", data=data, headers=req_headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=30.0) as resp:
                body = json.loads(resp.read().decode("utf-8"))
                return resp.status, body
        except urllib.error.HTTPError as e:
            body = json.loads(e.read().decode("utf-8"))
            return e.code, body

    def test_execute_success(self, formula_server: str) -> None:
        status, body = self._post(formula_server, {"id": "req-1", "code": "result = 7 * 8"}, headers={"Authorization": "Bearer formula-secret"})
        assert status == 200
        assert body.get("id") == "req-1"
        assert body.get("status") == "ok"
        assert body.get("result") == 56

    def test_http_timeout_is_200_error_then_next_ok(self, formula_server: str) -> None:
        # Tip: warm a worker first. A cold spawn on a slow Windows runner can use up the
        # 1.5 s budget before the code runs, which is a 503 QUEUE_TIMEOUT, not this case.
        warm_status, _warm = self._post(formula_server, {"id": "warm", "code": "result = 0"}, headers={"Authorization": "Bearer formula-secret"})
        assert warm_status == 200
        status, body = self._post(formula_server, {"id": "slow-1", "code": "import time\ntime.sleep(5)\nresult = 1", "timeout_ms": 1500}, headers={"Authorization": "Bearer formula-secret"})
        assert status == 200, body
        assert body.get("status") == "error"
        status2, body2 = self._post(formula_server, {"id": "slow-2", "code": "result = 4"}, headers={"Authorization": "Bearer formula-secret"})
        assert status2 == 200
        assert body2.get("status") == "ok"
        assert body2.get("result") == 4

    def test_http_code_too_large(self, formula_server: str) -> None:
        status, body = self._post(formula_server, {"id": "big", "code": "result = 1\n" + ("x = 1\n" * 200000)}, headers={"Authorization": "Bearer formula-secret"})
        assert status == 400
        assert body.get("code") == "CODE_TOO_LARGE"

    def test_http_eval_error_is_200(self, formula_server: str) -> None:
        status, body = self._post(formula_server, {"id": "div0", "code": "result = 1 / 0"}, headers={"Authorization": "Bearer formula-secret"})
        assert status == 200
        assert body.get("status") == "error"
        assert body.get("id") == "div0"
        assert "error" in body

    def test_execute_shared_session(self, formula_server: str) -> None:
        session_id = "session-http-123"
        status1, body1 = self._post(formula_server, {"id": "req-s1", "code": "val = 42\nresult = val", "mode": "shared"}, headers={"Authorization": "Bearer formula-secret"}, path=f"/v1/execute?session_id={session_id}")
        assert status1 == 200
        assert body1.get("result") == 42

        status2, body2 = self._post(formula_server, {"id": "req-s2", "code": "val += 8\nresult = val", "mode": "shared"}, headers={"Authorization": "Bearer formula-secret"}, path=f"/v1/execute?session_id={session_id}")
        assert status2 == 200
        assert body2.get("result") == 50

    def test_http_session_reset_unknown_is_ok(self, formula_server: str) -> None:
        status, body = self._post(formula_server, {"id": "reset-unknown"}, headers={"Authorization": "Bearer formula-secret"}, path="/v1/session/reset?session_id=never-created")
        assert status == 200
        assert body == {"id": "reset-unknown", "status": "ok"}

    def test_http_session_reset_clears_shared_state(self, formula_server: str) -> None:
        session_id = "session-reset-http"
        headers = {"Authorization": "Bearer formula-secret"}
        status1, body1 = self._post(formula_server, {"id": "rs-1", "code": "kept = 19\nresult = kept", "mode": "shared"}, headers=headers, path=f"/v1/execute?session_id={session_id}")
        assert status1 == 200
        assert body1.get("result") == 19

        status_reset, reset_body = self._post(formula_server, {"id": "rs-reset"}, headers=headers, path=f"/v1/session/reset?session_id={session_id}")
        assert status_reset == 200
        assert reset_body == {"id": "rs-reset", "status": "ok"}

        status2, body2 = self._post(formula_server, {"id": "rs-2", "code": "result = kept", "mode": "shared"}, headers=headers, path=f"/v1/execute?session_id={session_id}")
        assert status2 == 200
        assert body2.get("status") == "error"
        assert "kept" in body2.get("error", "") or "NameError" in body2.get("error", "")

    def test_shared_session_balances_across_least_loaded_worker(self) -> None:
        """New shared sessions must be assigned to the worker holding the fewest active sessions."""
        pool = FormulaProcessPool(settings=ComputeSettings(workers=2, default_timeout_sec=15))
        try:
            # First session lands on the least-loaded live worker. On a tie
            # that is the lowest worker_id (hash() used to depend on PYTHONHASHSEED).
            res1 = run_cell(pool, code="result = 1", session_id="sess-1")
            assert res1.get("status") == "ok"
            w1 = session_worker(pool, "sess-1")

            # Second session should pick the other worker because it has 0 sessions
            res2 = run_cell(pool, code="result = 2", session_id="sess-2")
            assert res2.get("status") == "ok"
            w2 = session_worker(pool, "sess-2")

            assert w1 is not None and w2 is not None
            live = [w for w in pool.workers if w.is_alive()]
            assert w1.worker_id == min(w.worker_id for w in live)
            assert w1 is not w2, "Sessions must balance across distinct workers when both are available"
        finally:
            pool.shutdown()

    def test_isolated_worker_lease_prefers_session_free_worker(self) -> None:
        """Isolated workload lease must prefer idle workers that host zero shared sessions."""
        pool = FormulaProcessPool(settings=ComputeSettings(workers=2, default_timeout_sec=15))
        try:
            # Register a shared session on one worker
            res1 = run_cell(pool, code="x = 10; result = x", session_id="shared-worker-test")
            assert res1.get("status") == "ok"
            shared_worker = session_worker(pool, "shared-worker-test")

            # An isolated execution should prefer the session-free worker
            with pool._cond:
                picked = pool._pick_idle_worker()
                assert picked is not None
                assert picked is not shared_worker
                # Put it back
                pool._idle[picked] = time.monotonic()
        finally:
            pool.shutdown()

    def test_isolated_worker_lease_falls_back_when_all_workers_have_sessions(self) -> None:
        """When all idle workers hold shared sessions, _pick_idle_worker still picks an idle worker."""
        pool = FormulaProcessPool(settings=ComputeSettings(workers=2, default_timeout_sec=15))
        try:
            res1 = run_cell(pool, code="result = 1", session_id="s1")
            assert res1.get("status") == "ok"
            res2 = run_cell(pool, code="result = 2", session_id="s2")
            assert res2.get("status") == "ok"

            with pool._cond:
                # Both workers have sessions and both are idle
                assert len(pool._idle) == 2
                picked = pool._pick_idle_worker()
                assert picked is not None
                pool._idle[picked] = time.monotonic()
        finally:
            pool.shutdown()

    def test_subsecond_budget_does_not_sigkill_worker(self) -> None:
        """A deadline under one second is refused. It does not lease or SIGKILL."""
        pool = FormulaProcessPool(settings=ComputeSettings(workers=1, default_timeout_sec=30))
        try:
            warm = run_cell(pool, code="result = 1", req_id="warmup")
            assert warm.get("status") == "ok"

            worker = pool.workers[0]
            calls: list[float] = []
            real_execute = worker.execute

            def spy_execute(payload: dict[str, Any], timeout_sec: float, **kwargs: Any) -> dict[str, Any]:
                calls.append(timeout_sec)
                return real_execute(payload, timeout_sec, **kwargs)

            setattr(worker, "execute", spy_execute)
            res = run_cell(pool, code="result = 42", deadline=Deadline.from_absolute(30.0, time.monotonic() + 0.5), req_id="subsecond-test")
            assert res.get("code") == "QUEUE_TIMEOUT"
            assert calls == []
            assert worker.is_alive()
        finally:
            pool.shutdown()

    @pytest.mark.skipif(sys.platform == "win32", reason="uses SIGKILL")
    def test_shared_session_loss_reports_session_reset(self) -> None:
        """After SIGKILL, next call on the same session_id lands on fresh kernel and returns session_reset: True."""
        import json
        import signal

        pool = FormulaProcessPool(settings=ComputeSettings(workers=1, default_timeout_sec=15))
        try:
            sid = "lost-session-test"
            r1 = run_cell(pool, code="x = 100\nresult = x", session_id=sid, req_id="r1")
            assert r1.get("status") == "ok"
            assert r1.get("session_reset") is not True

            owner = session_worker(pool, sid)
            assert owner is not None and owner.process is not None
            os.kill(owner.process.pid, signal.SIGKILL)
            owner.process.wait(timeout=2)

            r2 = run_cell(pool, code="result = 200", session_id=sid, req_id="r2")
            assert r2.get("status") == "ok"
            assert r2.get("session_reset") is True
            if "result_json" in r2:
                payload = json.loads(r2["result_json"])
                assert payload.get("session_reset") is True

            # Third call on same session does not report session_reset again (it was consumed)
            r3 = run_cell(pool, code="result = 300", session_id=sid, req_id="r3")
            assert r3.get("status") == "ok"
            assert r3.get("session_reset") is not True
        finally:
            pool.shutdown()

    def test_payload_too_large_keeps_session_reset(self) -> None:
        """A frame that never reaches the child must not consume session_reset."""
        pool = FormulaProcessPool(settings=ComputeSettings(workers=1, default_timeout_sec=15))
        try:
            sid = "unrun-lost"
            created = run_cell(pool, code="x = 1\nresult = x", session_id=sid, req_id="warm")
            assert created.get("status") == "ok"
            worker = session_worker(pool, sid)
            assert worker is not None
            saved_cap = worker.max_payload_bytes
            worker.kill()
            assert not worker.is_alive()
            worker.max_payload_bytes = 32
            try:
                rejected = run_cell(pool, code="result = 1", data={"blob": "x" * 200}, session_id=sid, req_id="too-big")
            finally:
                worker.max_payload_bytes = saved_cap
            assert rejected.get("code") == "PAYLOAD_TOO_LARGE"
            with pool._cond:
                assert sid in pool._sessions
            nxt = run_cell(pool, code="result = 2", session_id=sid, req_id="after")
            assert nxt.get("status") == "ok"
            assert nxt.get("session_reset") is True
        finally:
            pool.shutdown()

    def test_lease_miss_keeps_session_reset(self) -> None:
        """A lease failure preserves the session binding so a later call still reports session_reset."""
        pool = FormulaProcessPool(settings=ComputeSettings(workers=1, default_timeout_sec=15))
        try:
            sid = "lost-lease-miss"
            created = run_cell(pool, code="x = 42\nresult = x", session_id=sid)
            assert created.get("status") == "ok"
            assert created.get("result") == 42

            worker = session_worker(pool, sid)
            assert worker is not None
            worker.kill()
            assert not worker.is_alive()

            with patch.object(pool, "lease_specific", return_value=None):
                res = run_cell(pool, code="result = 1", session_id=sid)
                assert res.get("code") == "WORKER_POOL_BUSY"

            # The session binding is preserved across the lease miss, so the next sticky call reports session_reset
            again = run_cell(pool, code="result = 1", session_id=sid)
            assert again.get("status") == "ok"
            assert again.get("session_reset") is True

            # Third call on same session does not report session_reset again
            third = run_cell(pool, code="result = 1", session_id=sid)
            assert third.get("status") == "ok"
            assert third.get("session_reset") is not True
        finally:
            pool.shutdown()

    def test_concurrent_first_calls_same_session(self) -> None:
        """Concurrent first calls for the same session_id must reserve and route to the same worker."""
        pool = FormulaProcessPool(settings=ComputeSettings(workers=2, default_timeout_sec=15))
        try:
            sid = "concurrent-first-call-session"
            barrier = threading.Barrier(2)
            results: list[dict] = [{}, {}]

            def run_worker(idx: int, code: str) -> None:
                barrier.wait()
                res = run_cell(pool, code=code, session_id=sid, req_id=f"req-{idx}")
                results[idx] = res

            t1 = threading.Thread(target=run_worker, args=(0, "result = 42"))
            t2 = threading.Thread(target=run_worker, args=(1, "result = 43"))

            t1.start()
            t2.start()
            # What was wrong: join(5) returned while both executes were still
            # inside a cold Windows worker spawn, so results stayed {}.
            # The pool timeout is 15s and lease_specific is bounded by it
            # (GHA 37719557033).
            # Why: wait past that budget plus process-start slack. A hung
            # lease still fails; a slow spawn can finish.
            t1.join(timeout=45)
            t2.join(timeout=45)

            assert results[0].get("status") == "ok"
            assert results[0].get("result") == 42
            assert results[1].get("status") == "ok"
            assert results[1].get("result") == 43
            # Session must be bound to exactly one worker
            with pool._cond:
                worker_ids = [w.worker_id for w in pool.workers if sid in worker_sessions(pool, w)]
                assert len(worker_ids) == 1

            # Shared state is maintained on this worker
            r3 = run_cell(pool, code="x = 100\nresult = x", session_id=sid)
            assert r3.get("status") == "ok"
            r4 = run_cell(pool, code="result = x + 1", session_id=sid)
            assert r4.get("status") == "ok"
            assert r4.get("result") == 101
        finally:
            pool.shutdown()

    def test_concurrent_first_calls_gate_prevents_split_placement(self) -> None:
        """Per-session gate ensures concurrent first calls do not pick different workers."""
        from compute_service.worker_base import BaseProcessWorker

        pool = FormulaProcessPool(settings=ComputeSettings(workers=2, default_timeout_sec=15))
        try:
            sid = "gate-split-test"
            w0, w1 = pool.workers[0], pool.workers[1]
            pick_returns = [w0, w1]
            pick_calls: list[BaseProcessWorker] = []

            def fake_pick() -> BaseProcessWorker | None:
                if pick_returns:
                    chosen = pick_returns.pop(0)
                    pick_calls.append(chosen)
                    return chosen
                return None

            setattr(pool, "_pick_new_shared_worker", fake_pick)

            barrier = threading.Barrier(2)
            results: list[dict[str, Any]] = [{}, {}]

            def run_worker(idx: int, code: str) -> None:
                barrier.wait()
                res = run_cell(pool, code=code, session_id=sid, req_id=f"gate-{idx}")
                results[idx] = res

            t1 = threading.Thread(target=run_worker, args=(0, "result = 1"))
            t2 = threading.Thread(target=run_worker, args=(1, "result = 2"))

            t1.start()
            t2.start()
            t1.join(timeout=45)
            t2.join(timeout=45)

            assert len(pick_calls) == 1
            assert results[0].get("status") == "ok"
            assert results[1].get("status") == "ok"
            with pool._cond:
                worker_ids = [w.worker_id for w in pool.workers if sid in worker_sessions(pool, w)]
                assert len(worker_ids) == 1

            r3 = run_cell(pool, code="x = 100\nresult = x", session_id=sid)
            assert r3.get("status") == "ok"
            assert r3.get("result") == 100
            r4 = run_cell(pool, code="result = x + 1", session_id=sid)
            assert r4.get("status") == "ok"
            assert r4.get("result") == 101
        finally:
            pool.shutdown()

    def test_explicit_reset_does_not_mark_session_lost(self) -> None:
        """Explicit reset_session clears session without marking it lost (no session_reset on next call)."""
        pool = FormulaProcessPool(settings=ComputeSettings(workers=1, default_timeout_sec=15))
        try:
            sid = "explicit-reset-session"
            r1 = run_cell(pool, code="x = 99\nresult = x", session_id=sid)
            assert r1.get("status") == "ok"

            reset_res = pool.reset_session(sid)
            assert reset_res.get("status") == "ok"

            with pool._cond:
                assert sid not in pool._sessions

            r2 = run_cell(pool, code="result = 100", session_id=sid)
            assert r2.get("status") == "ok"
            assert r2.get("session_reset") is not True
        finally:
            pool.shutdown()

    def test_session_locks_refcounted_and_pruned(self) -> None:
        """Verify per-session locks are refcounted, shared by concurrent callers, and pruned on exit."""
        pool = FormulaProcessPool(settings=ComputeSettings(workers=1, default_timeout_sec=15))
        try:
            sid = "refcount-prune-test"
            assert not pool._session_locks

            # Shared execute prunes lock upon completion
            res = run_cell(pool, code="x = 10\nresult = x", session_id=sid)
            assert res.get("status") == "ok"
            assert res.get("result") == 10
            assert not pool._session_locks

            # reset_session prunes lock upon completion
            res_reset = pool.reset_session(sid)
            assert res_reset.get("status") == "ok"
            assert not pool._session_locks

            # Concurrent callers on same session_id share one lock and prune upon completion
            gate1_entered = threading.Event()
            gate1_proceed = threading.Event()
            gate2_entered = threading.Event()
            same_lock_verified = False

            def caller1() -> None:
                with pool._session_gate(sid, 30.0):
                    gate1_entered.set()
                    gate1_proceed.wait(timeout=10.0)

            def caller2() -> None:
                with pool._session_gate(sid, 30.0):
                    gate2_entered.set()

            t1 = threading.Thread(target=caller1)
            t2 = threading.Thread(target=caller2)

            t1.start()
            assert gate1_entered.wait(timeout=10.0)

            with pool._cond:
                entry1 = pool._session_locks.get(sid)
                assert entry1 is not None
                assert entry1.users == 1
                lock1 = entry1.lock

            t2.start()
            t0 = time.monotonic()
            while time.monotonic() - t0 < 5.0:
                with pool._cond:
                    if entry1.users == 2:
                        same_lock_verified = (pool._session_locks.get(sid) is entry1) and (entry1.lock is lock1)
                        break
                time.sleep(0.01)

            assert same_lock_verified
            gate1_proceed.set()
            assert gate2_entered.wait(timeout=10.0)

            t1.join(timeout=10.0)
            t2.join(timeout=10.0)

            assert not pool._session_locks
        finally:
            pool.shutdown()

    def test_session_gate_timeout_returns_busy(self) -> None:
        """Shared execute and reset time out waiting for the session gate and return WORKER_POOL_BUSY."""
        pool = FormulaProcessPool(settings=ComputeSettings(workers=1, default_timeout_sec=15))
        try:
            sid = "gate-busy"
            gate_held = threading.Event()
            gate_proceed = threading.Event()

            def holder() -> None:
                with pool._session_gate(sid, 30.0):
                    gate_held.set()
                    gate_proceed.wait(timeout=10.0)

            t = threading.Thread(target=holder)
            t.start()
            assert gate_held.wait(timeout=5.0)

            with pool._cond:
                entry = pool._session_locks.get(sid)
                assert entry is not None

            t0 = time.monotonic()
            res_exec = run_cell(pool, code="result = 1", session_id=sid, timeout_sec=1, req_id="gate-busy")
            exec_duration = time.monotonic() - t0
            assert exec_duration < 8.0
            assert res_exec.get("status") == "error"
            assert res_exec.get("code") == "WORKER_POOL_BUSY"
            assert res_exec.get("id") == "gate-busy"

            t0 = time.monotonic()
            res_reset = pool.reset_session(sid, timeout_sec=1)
            reset_duration = time.monotonic() - t0
            assert reset_duration < 8.0
            assert res_reset.get("status") == "error"
            assert res_reset.get("code") == "WORKER_POOL_BUSY"

            # Before releasing the holder, pool._session_locks["gate-busy"].users == 1 and the same entry object is still mapped.
            with pool._cond:
                assert pool._session_locks.get(sid) is entry
                assert pool._session_locks[sid].users == 1

            gate_proceed.set()
            t.join(timeout=5.0)

            # After join, that id is absent from _session_locks and from _sessions.
            with pool._cond:
                assert sid not in pool._session_locks
                assert sid not in pool._sessions
        finally:
            pool.shutdown()

    def test_reaper_drops_stale_sessions_older_than_idle_ttl(self) -> None:
        """Stale session rows expire on idle_worker_ttl_sec; fresh stale and live rows stay."""
        pool = FormulaProcessPool(settings=ComputeSettings(workers=1, idle_worker_ttl_sec=60))
        try:
            worker = pool.workers[0]
            current_child = worker.child
            assert current_child is not None

            old_time = time.monotonic() - 120.0
            fresh_time = time.monotonic()

            class _DeadProcess:
                def is_alive(self) -> bool:
                    return False

            dead_child = _DeadProcess()

            from compute_service.formula_pool import _Session

            with pool._cond:
                # Stale row older than idle_worker_ttl_sec
                pool._sessions["stale-old"] = _Session(worker=worker, child=dead_child, last_used=old_time, stale_since=old_time)
                # Stale row newer than idle_worker_ttl_sec
                pool._sessions["stale-fresh"] = _Session(worker=worker, child=dead_child, last_used=fresh_time)
                # Live row older than idle_worker_ttl_sec (live rows unaffected)
                pool._sessions["live-old"] = _Session(worker=worker, child=current_child, last_used=old_time)

            pool._evict_idle_workers()

            with pool._cond:
                assert "stale-old" not in pool._sessions
                assert "stale-fresh" in pool._sessions
                assert "live-old" in pool._sessions

            # Next call on the fresh stale row still reports session_reset
            res = run_cell(pool, code="result = 42", session_id="stale-fresh")
            assert res.get("status") == "ok"
            assert res.get("session_reset") is True
        finally:
            pool.shutdown()

    def test_idle_evicted_session_preserves_session_reset_across_reaper_passes(self) -> None:
        """A session whose worker was idle-evicted preserves its stale row across multiple reaper passes until TTL."""
        pool = FormulaProcessPool(settings=ComputeSettings(workers=1, default_timeout_sec=15, idle_worker_ttl_sec=0.2))
        try:
            sid = "idle-evict-reaper-test"
            r1 = run_cell(pool, code="x = 1\nresult = x", session_id=sid)
            assert r1.get("status") == "ok"
            assert r1.get("result") == 1
            worker = session_worker(pool, sid)
            assert worker is not None
            assert worker.is_alive()

            # Wait for worker idle TTL to elapse, then evict
            time.sleep(0.3)
            pool._evict_idle_workers()
            _wait_until_not_alive(worker)

            # Second pass occurs well after eviction but before TTL elapses since eviction
            time.sleep(0.05)
            pool._evict_idle_workers()
            time.sleep(0.05)
            pool._evict_idle_workers()
            with pool._cond:
                assert sid in pool._sessions

            # Next sticky call on that session must report session_reset is True and x is gone
            r2 = run_cell(pool, code="try:\n    result = x\nexcept NameError:\n    result = 'gone'", session_id=sid)
            assert r2.get("status") == "ok"
            assert r2.get("result") == "gone"
            assert r2.get("session_reset") is True
        finally:
            pool.shutdown()

    def test_stale_session_row_dropped_after_ttl_elapses(self) -> None:
        """A stale session row is popped only after idle_worker_ttl_sec elapses since it became stale."""
        pool = FormulaProcessPool(settings=ComputeSettings(workers=1, default_timeout_sec=15, idle_worker_ttl_sec=0.2))
        try:
            sid = "stale-ttl-expiry-test"
            r1 = run_cell(pool, code="x = 1\nresult = x", session_id=sid)
            assert r1.get("status") == "ok"
            worker = session_worker(pool, sid)
            assert worker is not None

            # Idle evict the worker
            time.sleep(0.3)
            pool._evict_idle_workers()
            _wait_until_not_alive(worker)

            # Record that it is stale on the first reaper pass
            pool._evict_idle_workers()
            with pool._cond:
                assert sid in pool._sessions

            # Wait until TTL elapses since it went stale
            time.sleep(0.3)
            pool._evict_idle_workers()
            with pool._cond:
                assert sid not in pool._sessions
        finally:
            pool.shutdown()

    def test_failed_lease_specific_drops_new_session(self) -> None:
        """When lease_specific fails, the newly reserved session is dropped (Bug 2)."""
        pool = FormulaProcessPool(settings=ComputeSettings(workers=1, default_timeout_sec=15))
        try:
            sid = "new-phantom-test"
            with patch.object(pool, "lease_specific", return_value=None):
                res = run_cell(pool, code="result = 1", session_id=sid)
                assert res.get("status") == "error"
                assert res.get("code") == "WORKER_POOL_BUSY"

            # The newly reserved session must NOT remain as a phantom in _sessions
            with pool._cond:
                assert sid not in pool._sessions
                assert not pool._worker_has_sessions(pool.workers[0])
        finally:
            pool.shutdown()

    def test_post_lease_queue_timeout_drops_new_session(self) -> None:
        """When deadline expires right after leasing, new session reservation is dropped."""
        from plugin.framework.child_runtime import Deadline

        pool = FormulaProcessPool(settings=ComputeSettings(workers=1, idle_worker_ttl_sec=0))
        try:
            warm = run_cell(pool, code="result = 1")
            assert warm.get("status") == "ok"

            usable_calls = [10.0, None]

            def fake_usable(self_deadline: Any) -> float | None:
                if usable_calls:
                    return usable_calls.pop(0)
                return None

            sid = "post-lease-timeout-new"
            with patch.object(Deadline, "usable", fake_usable):
                res = run_cell(pool, code="result = 2", session_id=sid)
                assert res.get("status") == "error"
                assert res.get("code") == "QUEUE_TIMEOUT"

            with pool._cond:
                assert sid not in pool._sessions
        finally:
            pool.shutdown()

    def test_post_lease_queue_timeout_keeps_existing_session(self) -> None:
        """When deadline expires right after leasing, an existing session is preserved."""
        from plugin.framework.child_runtime import Deadline

        pool = FormulaProcessPool(settings=ComputeSettings(workers=1, idle_worker_ttl_sec=0))
        try:
            sid = "post-lease-timeout-existing"
            res1 = run_cell(pool, code="x = 42\nresult = x", session_id=sid)
            assert res1.get("status") == "ok"
            assert res1.get("result") == 42

            usable_calls = [10.0, None]

            def fake_usable(self_deadline: Any) -> float | None:
                if usable_calls:
                    return usable_calls.pop(0)
                return None

            with patch.object(Deadline, "usable", fake_usable):
                res2 = run_cell(pool, code="x = x + 1\nresult = x", session_id=sid)
                assert res2.get("status") == "error"
                assert res2.get("code") == "QUEUE_TIMEOUT"

            with pool._cond:
                assert sid in pool._sessions

            res3 = run_cell(pool, code="result = x", session_id=sid)
            assert res3.get("status") == "ok"
            assert res3.get("result") == 42
            assert res3.get("session_reset") is not True
        finally:
            pool.shutdown()

    def test_worker_session_index(self) -> None:
        """Session counts are derived from _sessions and count only live sessions."""
        pool = FormulaProcessPool(settings=ComputeSettings(workers=2, default_timeout_sec=15))
        try:
            w1 = pool.workers[0]
            w2 = pool.workers[1]
            from compute_service.formula_pool import _Session

            assert pool._worker_session_count(w1) == 0
            assert not pool._worker_has_sessions(w1)
            assert worker_sessions(pool, w1) == []

            with pool._cond:
                pool._sessions["s1"] = _Session(worker=w1, child=w1.child)
                pool._sessions["s2"] = _Session(worker=w1, child=w1.child)
                pool._sessions["s3"] = _Session(worker=w2, child=w2.child)
                pool._sessions["stale"] = _Session(worker=w1, child=fake_child(w1))

            assert pool._worker_session_count(w1) == 2
            assert pool._worker_has_sessions(w1)
            assert set(worker_sessions(pool, w1)) == {"s1", "s2"}
            assert pool._worker_session_count(w2) == 1
            assert worker_sessions(pool, w2) == ["s3"]

            with pool._cond:
                pool._sessions.pop("s1", None)

            assert pool._worker_session_count(w1) == 1
            assert worker_sessions(pool, w1) == ["s2"]

            # Reassigning session to different worker updates both sets
            with pool._cond:
                pool._sessions["s2"] = _Session(worker=w2, child=w2.child)

            assert pool._worker_session_count(w1) == 0
            assert not pool._worker_has_sessions(w1)
            assert pool._worker_session_count(w2) == 2
            assert set(worker_sessions(pool, w2)) == {"s2", "s3"}
        finally:
            pool.shutdown()

    def test_drop_session_helper(self) -> None:
        """_drop_session drops a session from the active map."""
        pool = FormulaProcessPool(settings=ComputeSettings(workers=1, default_timeout_sec=15))
        try:
            w = pool.workers[0]
            from compute_service.formula_pool import _Session

            with pool._cond:
                pool._sessions["drop-clean"] = _Session(worker=w, child=w.child)

            pool._drop_session("drop-clean")
            with pool._cond:
                assert "drop-clean" not in pool._sessions
        finally:
            pool.shutdown()

    def test_pool_leased_context_manager(self) -> None:
        """BaseProcessPool.leased context manager leases and releases workers."""
        pool = FormulaProcessPool(settings=ComputeSettings(workers=1, default_timeout_sec=15))
        try:
            with pool.leased(timeout_sec=5.0) as worker:
                assert worker is not None
                assert worker in pool._leased
            # Released upon exiting context
            assert worker not in pool._leased
            assert worker in pool._idle
        finally:
            pool.shutdown()

    def test_expired_lease_budget_does_not_execute(self) -> None:
        """Selecting a worker must not start the cell once the deadline has passed."""
        pool = FormulaProcessPool(settings=ComputeSettings(workers=1, default_timeout_sec=15))
        try:
            warm = run_cell(pool, code="result = 1")
            assert warm.get("status") == "ok"
            worker = pool.workers[0]
            called: list[str] = []
            real_execute = worker.execute

            def spy(payload: dict[str, Any], timeout_sec: float, **kwargs: Any) -> dict[str, Any]:
                called.append("exec")
                return real_execute(payload, timeout_sec, **kwargs)

            setattr(worker, "execute", spy)
            orig = pool._select_shared_worker

            def slow(sid: str) -> tuple[Any, bool]:
                time.sleep(0.2)
                return orig(sid)

            setattr(pool, "_select_shared_worker", slow)
            res = run_cell(pool, code="result = 2", session_id="budget-sid", deadline=Deadline.from_absolute(30.0, time.monotonic() + 0.05))
            assert res.get("code") == "QUEUE_TIMEOUT"
            assert called == []
            assert session_worker(pool, "budget-sid") is None
        finally:
            pool.shutdown()

    def test_zero_timeout_is_not_the_default(self) -> None:
        """timeout_sec=0 expires immediately. It used to become the 30s default."""
        pool = FormulaProcessPool(settings=ComputeSettings(workers=1, default_timeout_sec=30))
        try:
            res = run_cell(pool, code="result = 1", timeout_sec=0)
            assert res.get("code") == "QUEUE_TIMEOUT"
        finally:
            pool.shutdown()


def _idle_reaper_idents() -> set[int]:
    return {t.ident for t in threading.enumerate() if t.name.endswith("-idle-reaper") and t.ident is not None}


def test_idle_ttl_zero_or_none_does_not_start_reaper(monkeypatch: pytest.MonkeyPatch) -> None:
    """0 or None on idle TTL leaves the reaper off. A zero interval would spin."""
    before = _idle_reaper_idents()
    pool = pool_without_children(monkeypatch)
    try:
        assert _idle_reaper_idents() - before == set()
    finally:
        pool.shutdown()

    before = _idle_reaper_idents()
    from compute_service.formula_pool import _WORKER_SCRIPT
    from compute_service.worker_base import BaseProcessPool

    pool_none = BaseProcessPool(script_path=_WORKER_SCRIPT, num_workers=0, idle_worker_ttl_sec=None)
    try:
        assert _idle_reaper_idents() - before == set()
    finally:
        pool_none.shutdown()


def test_run_execution_alarm_keeps_full_remaining_second(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 2 s budget arms a 2 s child alarm, not int() floor it to 1 s."""
    from compute_service.worker_base import BaseProcessWorker
    from plugin.scripting.config_limits import HOST_IPC_READ_GRACE_SEC

    worker = BaseProcessWorker(1, "unused.py", start=False)
    seen: dict[str, Any] = {}

    def _capture(payload: dict[str, Any], timeout_sec: float, *, req_id: Any = None) -> dict[str, Any]:
        seen["alarm"] = payload["timeout_sec"]
        seen["host"] = timeout_sec
        return {"status": "ok", "result": 1, "id": req_id}

    worker.execute = _capture  # type: ignore[method-assign]
    pool = pool_without_children(monkeypatch)
    try:
        pool._run_execution(worker, {}, Deadline(2.0), "t")
        assert seen["alarm"] == 2
        assert seen["host"] == 2 + HOST_IPC_READ_GRACE_SEC
    finally:
        pool.shutdown()


def test_drain_timeout_then_pool_teardown_unblocks_inflight_formula(monkeypatch: pytest.MonkeyPatch) -> None:
    from plugin.framework import warm_child

    real_read = warm_child.read_pickle_frame
    armed = False
    read_entered = threading.Event()

    def _read(*args: Any, **kwargs: Any) -> Any:
        nonlocal armed
        if armed:
            armed = False
            read_entered.set()
        return real_read(*args, **kwargs)

    monkeypatch.setattr(warm_child, "read_pickle_frame", _read)

    settings = ComputeSettings(host="127.0.0.1", port=get_free_port(), workers=1, ocr_workers=0, idle_worker_ttl_sec=3600.0)
    pool = get_formula_pool(settings)
    server = WSGIDualStackServer("127.0.0.1", settings.port, listener_threads=4)
    server.set_app(create_wsgi_app(settings))
    serve = threading.Thread(target=server.serve_forever, name="drain-race-serve", daemon=True)
    client_done = threading.Event()
    client_box: dict[str, Any] = {}
    before = {t.ident for t in threading.enumerate()}

    def _post(payload: dict[str, Any], timeout: float) -> None:
        try:
            req_headers = {"Content-Type": "application/json"}
            data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(f"http://127.0.0.1:{settings.port}/v1/execute", data=data, headers=req_headers, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    client_box["status"] = resp.status
                    client_box["body"] = json.loads(resp.read().decode("utf-8"))
                    client_box["exc"] = None
            except urllib.error.HTTPError as e:
                client_box["status"] = e.code
                client_box["body"] = json.loads(e.read().decode("utf-8"))
                client_box["exc"] = None
        except Exception as exc:
            client_box["exc"] = exc
        finally:
            client_done.set()

    try:
        serve.start()
        # Warm so the ready handshake is not the armed read.
        warm = threading.Thread(target=lambda: _post({"id": "warm", "code": "result = 1", "timeout_ms": 15000}, 20), daemon=True)
        warm.start()
        warm.join(timeout=20)
        assert client_box["status"] == 200

        armed = True
        client_box.clear()
        client_done.clear()
        client = threading.Thread(target=lambda: _post({"id": "slow", "code": "import time\ntime.sleep(30)\nresult = 1", "timeout_ms": 20000}, 12), name="drain-race-client", daemon=True)
        client.start()
        assert read_entered.wait(timeout=10)

        server.shutdown()
        serve.join(timeout=5)
        assert not serve.is_alive()
        server.close_sockets()
        server.drain_executor(0.05)
        # Formula is still in the pipe read. Drain must have given up.
        assert not client_done.is_set()
        server.server_close()
        shutdown_formula_pool(permanent=True)
        shutdown_vision_pool(permanent=True)

        client.join(timeout=8)
        assert not client.is_alive()
        assert client_done.is_set()
        assert client_box["exc"] is None
        assert client_box["status"] == 503
        body = client_box["body"]
        assert isinstance(body, dict)
        assert body.get("status") == "error"
        assert body.get("code") == "SERVICE_SHUTDOWN"
        assert body.get("id") == "slow"
        assert "result_json" not in body

        assert pool._is_shutdown is True
        assert pool.workers == []
        assert pool._leased == set()
        assert len(pool._idle) == 0
        assert pool._sessions == {}
        with pytest.raises(RuntimeError, match="shut down"):
            get_formula_pool(settings)
        with pytest.raises(RuntimeError, match="shut down"):
            get_vision_pool()

        lingering = [t for t in threading.enumerate() if t.ident not in before and t.is_alive() and not t.daemon]
        assert lingering == []
    finally:
        try:
            server.shutdown()
        except Exception:
            pass
        server.server_close()
        serve.join(timeout=5)
        # Non-permanent shutdown clears _closed even when the pool is already None.
        shutdown_formula_pool()
        shutdown_vision_pool()


class _RecycleTestSlot:
    def __init__(self, worker_id: int, tasks: int = 0) -> None:
        self.worker_id = worker_id
        self.worker_name = "Formula worker"
        self.tasks_executed = tasks
        self._alive = True
        self.killed = 0
        self.pending_exit = False
        self.child = fake_child(self)

    def is_alive(self) -> bool:
        return self._alive

    def kill(self) -> None:
        self.killed += 1
        self._alive = False


def test_recycle_vs_new_shared_session_order_a(monkeypatch: pytest.MonkeyPatch) -> None:
    """Order A: mark then select picks the other worker, dying worker killed."""
    pool = pool_without_children(monkeypatch, worker_max_tasks=1)
    dying = _RecycleTestSlot(worker_id=1, tasks=1)
    other = _RecycleTestSlot(worker_id=2, tasks=0)
    pool.workers.extend([dying, other])
    pool._leased.add(dying)

    real_should_recycle = pool.should_recycle_worker
    marked = threading.Event()
    proceed = threading.Event()
    call_count = 0

    def wrapped_should_recycle(w: Any) -> bool:
        nonlocal call_count
        res = real_should_recycle(w)
        call_count += 1
        if call_count == 1 and res:
            marked.set()
            assert proceed.wait(timeout=5.0)
        return res

    pool.should_recycle_worker = wrapped_should_recycle  # type: ignore[method-assign]
    thread_r = threading.Thread(target=pool.release_worker, args=(dying,), daemon=True)
    thread_r.start()

    assert marked.wait(timeout=5.0)
    assert dying in pool._recycling

    target_worker = pool._select_shared_worker("s-new")
    assert target_worker is other
    assert "s-new" not in pool._sessions
    # The other worker gets the row only after a cell that actually ran.
    pool._finalize_session(other, "s-new", result={"status": "ok"})
    assert pool._sessions["s-new"].worker is other
    assert dying not in [s.worker for s in pool._sessions.values()]

    proceed.set()
    thread_r.join(timeout=5.0)
    assert not thread_r.is_alive()

    assert dying.killed == 1
    assert dying not in pool._idle
    assert dying not in pool._leased
    assert dying not in pool._recycling
    assert other.killed == 0


def test_recycle_vs_new_shared_session_order_b() -> None:
    """The only worker is past max_tasks and has no row yet. Release kills it.
    The new session's execute respawns and then inserts."""
    pool = FormulaProcessPool(settings=ComputeSettings(workers=1, worker_max_tasks=1, idle_worker_ttl_sec=0))
    try:
        worker = pool.workers[0]
        res1 = run_cell(pool, code="result = 1")
        assert res1.get("status") == "ok"
        assert res1.get("result") == 1
        assert worker.tasks_executed >= 1
        # Worker was recycled and killed upon release
        assert not worker.is_alive()

        res2 = run_cell(pool, code="result = 42", session_id="s-new")
        assert res2.get("status") == "ok"
        assert res2.get("result") == 42
        assert worker.is_alive()
        with pool._cond:
            sess = pool._sessions.get("s-new")
            assert sess is not None
            assert sess.child is not None
            assert sess.child is worker.child
            assert session_worker(pool, "s-new") is worker
    finally:
        pool.shutdown()


def test_session_created_before_should_recycle_prevents_mark() -> None:
    """A session created before should_recycle_worker prevents the mark."""
    pool = FormulaProcessPool(settings=ComputeSettings(workers=1, worker_max_tasks=1, idle_worker_ttl_sec=0))
    try:
        res = run_cell(pool, code="result = 1", session_id="s-existing")
        assert res.get("status") == "ok"
        worker = pool.workers[0]
        assert worker.tasks_executed >= 1
        assert pool.should_recycle_worker(worker) is False
        assert worker not in pool._recycling
    finally:
        pool.shutdown()


def test_pending_exit_reserves_with_none_child_and_release_kills(monkeypatch: pytest.MonkeyPatch) -> None:
    """Single-threaded pending_exit test: release still kills it, no idle, no child None row."""
    pool = pool_without_children(monkeypatch, worker_max_tasks=1)
    worker = _RecycleTestSlot(worker_id=1, tasks=0)
    worker.pending_exit = True
    pool.workers.append(worker)
    pool._leased.add(worker)

    target_worker = pool._select_shared_worker("s-pending")
    assert target_worker is worker
    assert "s-pending" not in pool._sessions

    pool.release_worker(worker)
    assert worker.killed == 1
    assert worker not in pool._idle
    assert worker not in pool._leased
    assert worker.pending_exit is False
    assert worker not in pool._recycling
    assert "s-pending" not in pool._sessions


def test_new_session_spawn_failure_does_not_set_session_lost() -> None:
    pool = FormulaProcessPool(settings=ComputeSettings(workers=1, default_timeout_sec=15))
    try:
        worker = pool.workers[0]
        worker.kill()
        assert not worker.is_alive()

        real_execute = worker.execute
        first = True

        def mock_execute(*args: Any, **kwargs: Any) -> dict[str, Any]:
            nonlocal first
            if first:
                first = False
                return {"status": "error", "code": "WORKER_SPAWN_FAILED", "error": "Spawn failed"}
            return real_execute(*args, **kwargs)

        worker.execute = mock_execute  # type: ignore[method-assign]
        sid = "new-sid-spawn-fail"
        res1 = run_cell(pool, code="result = 1", session_id=sid)
        assert res1.get("status") == "error"
        assert res1.get("code") == "WORKER_SPAWN_FAILED"

        with pool._cond:
            assert sid not in pool._sessions

        res2 = run_cell(pool, code="result = 1", session_id=sid)
        assert res2.get("status") == "ok"
        assert res2.get("result") == 1
        assert res2.get("session_reset") is not True
    finally:
        pool.shutdown()


def test_cold_start_kill_after_cell_marks_session_lost() -> None:
    pool = FormulaProcessPool(settings=ComputeSettings(workers=1, default_timeout_sec=15))
    try:
        worker = pool.workers[0]
        worker.kill()
        assert not worker.is_alive()

        real_execute = worker.execute
        first = True

        def mock_execute(*args: Any, **kwargs: Any) -> dict[str, Any]:
            nonlocal first
            res = real_execute(*args, **kwargs)
            if first:
                first = False
                worker.kill()
            return res

        worker.execute = mock_execute  # type: ignore[method-assign]
        sid = "cold-kill-after-cell"
        res1 = run_cell(pool, code="x = 5\nresult = x", session_id=sid)
        assert res1.get("status") == "ok"
        assert res1.get("result") == 5

        with pool._cond:
            assert sid in pool._sessions
            assert session_worker(pool, sid) is None

        res2 = pool.execute(code="result = 10", session_id=sid, deadline=Deadline(15.0))
        assert res2.get("status") == "ok"
        assert res2.get("namespace_created") is True
        assert res2.get("session_reset") is True

        res3 = pool.execute(code="result = 20", session_id=sid, deadline=Deadline(15.0))
        assert res3.get("status") == "ok"
        assert res3.get("namespace_created") is False
        assert res3.get("session_reset") is not True
    finally:
        pool.shutdown()


def test_execute_sends_expect_namespace_only_for_existing_row(monkeypatch: pytest.MonkeyPatch) -> None:
    """The pool asks the worker to detect a reset namespace only when a session row already exists."""
    from compute_service.worker_base import BaseProcessWorker

    pool = pool_without_children(monkeypatch)
    fake_worker = BaseProcessWorker(1, "unused.py", start=False)
    seen_expect: list[Any] = []

    def fake_exec(payload: dict[str, Any], timeout_sec: float, *, req_id: Any = None) -> dict[str, Any]:
        del timeout_sec
        seen_expect.append(payload.get("expect_namespace"))
        return {"status": "ok", "id": req_id}

    fake_worker.execute = fake_exec  # type: ignore[method-assign]
    fake_worker.child = fake_child(fake_worker)
    pool.workers.append(fake_worker)
    pool._idle[fake_worker] = time.monotonic()

    try:
        sid = "expect-namespace-session"
        res1 = pool.execute(code="result = 1", session_id=sid, deadline=Deadline(15.0))
        assert res1.get("status") == "ok"
        with pool._cond:
            assert sid in pool._sessions

        res2 = pool.execute(code="result = 1", session_id=sid, deadline=Deadline(15.0))
        assert res2.get("status") == "ok"

        res3 = pool.execute(code="result = 1", session_id=sid, deadline=Deadline(15.0))
        assert res3.get("status") == "ok"

        assert seen_expect == [None, True, True]
    finally:
        pool.shutdown()


def test_check_dependencies_does_not_import_packages(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """The worker check finds packages without importing them (no import cost at startup)."""
    from compute_service.formula_worker import _handle_request

    (tmp_path / "wa_dep_probe_xyz.py").write_text("raise RuntimeError('check_dependencies must not import')\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    res = _handle_request({"id": "deps-1", "action": "check_dependencies", "packages": ["wa_dep_probe_xyz"]})
    assert res == {"id": "deps-1", "status": "ok"}
    assert "wa_dep_probe_xyz" not in sys.modules

    missing = _handle_request({"id": "deps-2", "action": "check_dependencies", "packages": ["nonexistent_pkg_xyz_12345", "nonexistent_parent_xyz.child"]})
    assert missing["code"] == "MISSING_DEPENDENCIES"
    assert missing["missing"] == ["nonexistent_pkg_xyz_12345", "nonexistent_parent_xyz.child"]


def test_run_execution_lifts_one_second_budget_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """A one-second request with 0.3s left still arms a 1 s child alarm: run_for() is the only lift."""
    from compute_service.worker_base import BaseProcessWorker
    from plugin.scripting.config_limits import HOST_IPC_READ_GRACE_SEC

    worker = BaseProcessWorker(1, "unused.py", start=False)
    seen: dict[str, Any] = {}

    def _capture(payload: dict[str, Any], timeout_sec: float, *, req_id: Any = None) -> dict[str, Any]:
        seen["alarm"] = payload["timeout_sec"]
        seen["host"] = timeout_sec
        return {"status": "ok", "result": 1, "id": req_id}

    worker.execute = _capture  # type: ignore[method-assign]
    pool = pool_without_children(monkeypatch)
    try:
        clock = Deadline.from_absolute(1.0, time.monotonic() + 0.3)
        assert clock.usable() is not None
        pool._run_execution(worker, {}, clock, "t")
        assert seen["alarm"] == 1
        assert seen["host"] == 1 + HOST_IPC_READ_GRACE_SEC
    finally:
        pool.shutdown()


def test_is_session_live_reads_the_child_process(monkeypatch: pytest.MonkeyPatch) -> None:
    """_is_session_live returns True only when the stored child matches the active slot and is alive."""
    from compute_service.formula_pool import _Session
    from compute_service.worker_base import BaseProcessWorker

    pool = pool_without_children(monkeypatch)
    worker = BaseProcessWorker(1, "unused.py", start=False)
    live = fake_child(worker, alive=True)
    worker.child = live

    row_live = _Session(worker=worker, child=live)
    assert pool._is_session_live(row_live) is True

    dead = fake_child(worker, alive=False)
    worker.child = dead
    row_dead = _Session(worker=worker, child=dead)
    assert pool._is_session_live(row_dead) is False

    other_child = fake_child(worker, alive=True)
    row_mismatched = _Session(worker=worker, child=other_child)
    worker.child = live
    assert pool._is_session_live(row_mismatched) is False
