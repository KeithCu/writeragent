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
from compute_service.formula_pool import (
    FormulaProcessPool,
    get_formula_pool,
    shutdown_formula_pool,
)
from compute_service.server import WSGIDualStackServer, create_wsgi_app
from tests.compute_service.conftest import get_free_port


@pytest.fixture(autouse=True)
def cleanup_formula_pool():
    yield
    shutdown_formula_pool()


def test_shared_mode_without_session_id_does_not_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """mode=shared with no session id must not run as an isolated cell.

    HTTP already rejects this. The pool used to lease a worker, and the
    child treated a missing id as isolated.
    """
    spawns = {"n": 0}

    def _no_spawn(self: object, timeout_sec: float = 0.0) -> None:
        del self, timeout_sec
        spawns["n"] += 1

    monkeypatch.setattr("compute_service.worker_base.BaseProcessWorker.respawn", _no_spawn)
    pool = FormulaProcessPool(num_workers=1, shared_kernel_ttl_sec=0, idle_worker_ttl_sec=0)
    try:
        spawned_at_init = spawns["n"]
        for sid in (None, "", "   "):
            res = pool.execute(code="result = 1", session_id=sid, mode="shared", req_id="nosid")
            assert res.get("status") == "error"
            assert res.get("code") == "INVALID_REQUEST"
            assert res.get("id") == "nosid"
            assert "session_id" in str(res.get("error"))
        assert spawns["n"] == spawned_at_init
    finally:
        pool.shutdown()


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
        pool = FormulaProcessPool(default_timeout_sec=15)
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
        pool = FormulaProcessPool(num_workers=2, default_timeout_sec=15)
        try:
            assert pool.is_enabled()
            assert len(pool.workers) == 2
            res = pool.execute(code="result = 10 + 20", req_id="f-1")
            assert res.get("id") == "f-1"
            assert res.get("status") == "ok"
            assert res.get("result") == 30
        finally:
            pool.shutdown()
            assert not pool.is_enabled()

    def test_check_dependencies_success(self) -> None:
        pool = FormulaProcessPool(num_workers=1, default_timeout_sec=15)
        try:
            ok, err = pool.check_dependencies(["numpy", "sympy"])
            assert ok is True
            assert err is None
        finally:
            pool.shutdown()

    def test_check_dependencies_missing(self) -> None:
        pool = FormulaProcessPool(num_workers=1, default_timeout_sec=15)
        try:
            ok, err = pool.check_dependencies(["nonexistent_pkg_xyz_12345"])
            assert ok is False
            assert err is not None
            assert "nonexistent_pkg_xyz_12345" in err
            assert "./compute_service/start.sh" in err
        finally:
            pool.shutdown()

    def test_sticky_session_affinity(self) -> None:
        pool = FormulaProcessPool(num_workers=4, default_timeout_sec=15)
        try:
            session_id = "test-workbook-session-42"
            # First cell execution: set a variable
            res1 = pool.execute(
                code="x = 100\nresult = x",
                session_id=session_id,
                mode="shared",
                req_id="c-1",
            )
            assert res1.get("status") == "ok"
            assert res1.get("result") == 100

            # Second cell execution: read and increment variable in same session
            res2 = pool.execute(
                code="x += 50\nresult = x",
                session_id=session_id,
                mode="shared",
                req_id="c-2",
            )
            assert res2.get("status") == "ok"
            assert res2.get("result") == 150
        finally:
            pool.shutdown()

    def test_worker_crash_recovery(self) -> None:
        pool = FormulaProcessPool(num_workers=1, default_timeout_sec=10)
        try:
            worker = pool.workers[0]
            # Kill worker externally
            worker.kill()
            assert not worker.is_alive()

            # Next request should automatically spawn a fresh worker and succeed
            res = pool.execute(code="result = 'recovered'", req_id="f-rec")
            assert res.get("status") == "ok"
            assert res.get("result") == "recovered"
            assert worker.is_alive()
        finally:
            pool.shutdown()

    def test_idle_death_drops_other_shared_sessions(self) -> None:
        """A dead idle process must not keep its session map after respawn."""
        pool = FormulaProcessPool(num_workers=1, default_timeout_sec=15, shared_kernel_ttl_sec=3600)
        try:
            sid_a = "sess-a"
            sid_b = "sess-b"
            res_a = pool.execute(code="marker = 'a'\nresult = marker", session_id=sid_a, mode="shared")
            res_b = pool.execute(code="marker = 'b'\nresult = marker", session_id=sid_b, mode="shared")
            assert res_a.get("status") == "ok", res_a
            assert res_b.get("status") == "ok", res_b
            worker = pool._sessions[sid_a].worker
            assert pool._sessions[sid_b].worker is worker
            worker.kill()
            assert not worker.is_alive()
            again = pool.execute(code="result = marker", session_id=sid_a, mode="shared")
            assert again.get("status") == "error"
            assert sid_b not in pool._sessions
            assert pool.live_session_worker(sid_a) is worker
            assert worker.is_alive()
        finally:
            pool.shutdown()

    def test_formula_worker_blocks_document_tools(self) -> None:
        pool = FormulaProcessPool(num_workers=1, default_timeout_sec=30)
        try:
            res = pool.execute(
                code=(
                    "from plugin.scripting.writeragent_api import _rpc_call\n"
                    "try:\n"
                    "    _rpc_call('list_open_documents')\n"
                    "    result = 'called'\n"
                    "except Exception as exc:\n"
                    "    result = str(exc)\n"
                )
            )
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
                    "from compute_service.worker_base import run_worker_stdio_loop",
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
        from compute_service import worker_base
        from compute_service.worker_base import BaseProcessWorker

        monkeypatch.setattr(worker_base, "_SPAWN_READY_TIMEOUT_SEC", 0.5)
        script = tmp_path / "hang_worker.py"
        script.write_text(
            "\n".join(
                [
                    "import sys, time",
                    "sys.stderr.write('hang-before-ready\\n')",
                    "sys.stderr.flush()",
                    "time.sleep(30)",
                ]
            ),
            encoding="utf-8",
        )
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
        script.write_text(
            "\n".join(
                [
                    "import sys, time",
                    "sys.stdout.write('Error: boom\\n')",
                    "sys.stdout.flush()",
                    "time.sleep(30)",
                ]
            ),
            encoding="utf-8",
        )
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
        script.write_text(
            "\n".join(
                [
                    "import sys, time",
                    f"sys.path.insert(0, {root!r})",
                    "from plugin.scripting.ipc import write_pickle_frame",
                    "write_pickle_frame(sys.stdout.buffer, {'status': 'booting'})",
                    "sys.stdout.buffer.flush()",
                    "time.sleep(30)",
                ]
            ),
            encoding="utf-8",
        )
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
            "\n".join(
                [
                    "import sys",
                    f"sys.path.insert(0, {root!r})",
                    "from compute_service.worker_base import run_worker_stdio_loop",
                    "def handle(req):",
                    "    return {'status': 'ok'}",
                    "if __name__ == '__main__':",
                    "    raise SystemExit(run_worker_stdio_loop(handle))",
                    "",
                ]
            ),
            encoding="utf-8",
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
                    "from compute_service.worker_base import run_worker_stdio_loop",
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
                [
                    "import sys, time",
                    f"sys.path.insert(0, {root!r})",
                    "time.sleep(0.35)",
                    "from compute_service.worker_base import run_worker_stdio_loop",
                    "def handle(req):",
                    "    return {'status': 'ok'}",
                    "if __name__ == '__main__':",
                    "    raise SystemExit(run_worker_stdio_loop(handle))",
                    "",
                ]
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
        code = "\n".join(
            [
                "import sys",
                "import compute_service.formula_worker",
                "assert 'plugin.scripting.venv.venv_sandbox' not in sys.modules",
            ]
        )
        env = os.environ.copy()
        env["PYTHONPATH"] = root + os.pathsep + env.get("PYTHONPATH", "")
        proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, check=False)
        assert proc.returncode == 0, proc.stderr

    def test_shared_and_isolated_exclusive_occupancy(self) -> None:
        """Isolated work does not run on the process that owns a shared session."""
        pool = FormulaProcessPool(num_workers=2, default_timeout_sec=15)
        try:
            sid = "occupancy-session"
            first = pool.execute(
                code="x = 5\nresult = x",
                session_id=sid,
                mode="shared",
                req_id="occ-1",
            )
            assert first.get("status") == "ok"
            owner = pool.live_session_worker(sid)
            assert owner is not None and owner.process is not None
            shared_pid = owner.process.pid

            before = owner.tasks_executed
            isolated = pool.execute(
                code="result = 99",
                mode="isolated",
                timeout_sec=10,
                req_id="occ-iso",
            )
            assert isolated.get("status") == "ok"
            assert isolated.get("result") == 99
            # The isolated cell did not run on the process that owns the session.
            assert owner.tasks_executed == before
            assert owner.process is not None and owner.process.pid == shared_pid
            shared = pool.execute(
                code="result = x",
                session_id=sid,
                mode="shared",
                timeout_sec=10,
                req_id="occ-2",
            )
            assert shared.get("status") == "ok"
            assert shared.get("result") == 5
        finally:
            pool.shutdown()

    def test_timeout_watchdog_kill(self) -> None:
        pool = FormulaProcessPool(num_workers=1, default_timeout_sec=1)
        try:
            # Code that takes longer than 1s timeout
            res = pool.execute(
                code="import time\ntime.sleep(5)\nresult = 'done'",
                timeout_sec=1,
                req_id="f-timeout",
            )
            assert res.get("status") == "error"
            # Code is either EXECUTION_TIMEOUT from pool or timeout from sandbox
            assert "timeout" in str(res.get("error", "")).lower() or "timeout" in str(res.get("code", "")).lower()
            nxt = pool.execute(code="result = 1", timeout_sec=10, req_id="f-timeout-next")
            assert nxt.get("status") == "ok"
            assert nxt.get("result") == 1
        finally:
            pool.shutdown()

    def test_timeout_tight_loop_then_next_cell(self) -> None:
        pool = FormulaProcessPool(num_workers=1, default_timeout_sec=1)
        try:
            res = pool.execute(
                code="while True:\n    pass\nresult = 0",
                timeout_sec=1,
                req_id="f-loop",
            )
            assert res.get("status") == "error"
            nxt = pool.execute(code="result = 2", timeout_sec=10, req_id="f-loop-next")
            assert nxt.get("status") == "ok"
            assert nxt.get("result") == 2
        finally:
            pool.shutdown()

    def test_shared_hang_does_not_wedge_pool(self) -> None:
        pool = FormulaProcessPool(num_workers=2, default_timeout_sec=1)
        try:
            hung = pool.execute(
                code="import time\ntime.sleep(5)\nresult = 1",
                session_id="s-hang",
                mode="shared",
                timeout_sec=1,
                req_id="sh-1",
            )
            assert hung.get("status") == "error"
            iso = pool.execute(code="result = 3", mode="isolated", timeout_sec=10, req_id="sh-iso")
            assert iso.get("status") == "ok"
            assert iso.get("result") == 3
        finally:
            pool.shutdown()

    @pytest.mark.skipif(sys.platform == "win32", reason="the in-child cell timeout uses signal.alarm; Windows falls back to the host kill")
    def test_shared_timeout_keeps_other_session(self) -> None:
        """A cell timeout must not SIGKILL the worker and drop other workbooks on it."""
        pool = FormulaProcessPool(num_workers=1, default_timeout_sec=15)
        try:
            kept = pool.execute(code="keep = 7\nresult = keep", session_id="doc-keep", mode="shared", req_id="keep-1")
            assert kept.get("status") == "ok"
            assert kept.get("result") == 7
            worker = pool.workers[0]
            pid = worker.process.pid if worker.process is not None else None
            hung = pool.execute(
                code="import time\ntime.sleep(30)\nresult = 1",
                session_id="doc-hang",
                mode="shared",
                timeout_sec=1,
                req_id="hang-1",
            )
            assert hung.get("status") == "error"
            assert hung.get("code") != "EXECUTION_TIMEOUT"
            assert worker.process is not None and worker.process.pid == pid
            again = pool.execute(code="result = keep", session_id="doc-keep", mode="shared", req_id="keep-2")
            assert again.get("status") == "ok"
            assert again.get("result") == 7
        finally:
            pool.shutdown()

    def test_isolated_does_not_leak_globals(self) -> None:
        pool = FormulaProcessPool(num_workers=1, default_timeout_sec=15)
        try:
            first = pool.execute(code="x = 1\nresult = x", mode="isolated", req_id="iso-1")
            assert first.get("status") == "ok"
            second = pool.execute(code="result = x", mode="isolated", req_id="iso-2")
            assert second.get("status") == "error"
        finally:
            pool.shutdown()

    def test_shared_sessions_do_not_cross(self) -> None:
        pool = FormulaProcessPool(num_workers=2, default_timeout_sec=15)
        try:
            a = pool.execute(code="x = 11\nresult = x", session_id="doc-A", mode="shared", req_id="xa")
            assert a.get("status") == "ok"
            b = pool.execute(code="result = x", session_id="doc-B", mode="shared", req_id="xb")
            assert b.get("status") == "error"
            a2 = pool.execute(code="result = x", session_id="doc-A", mode="shared", req_id="xa2")
            assert a2.get("status") == "ok"
            assert a2.get("result") == 11
        finally:
            pool.shutdown()

    def test_session_ttl_resets_namespace(self) -> None:
        pool = FormulaProcessPool(num_workers=1, default_timeout_sec=15, shared_kernel_ttl_sec=0.25)
        try:
            setx = pool.execute(code="x = 99\nresult = x", session_id="ttl-1", mode="shared", req_id="ttl-set")
            assert setx.get("status") == "ok"
            time.sleep(0.7)
            later = pool.execute(code="result = x", session_id="ttl-1", mode="shared", req_id="ttl-get")
            assert later.get("status") == "error"
        finally:
            pool.shutdown()

    def test_recycled_worker_is_alive_after_release(self) -> None:
        """max_tasks kills the slot and leaves it dead. The next execute respawns it."""
        pool = FormulaProcessPool(num_workers=1, default_timeout_sec=15, max_tasks=1)
        try:
            worker = pool.workers[0]
            res = pool.execute(code="result = 'first'", req_id="recycle-1")
            assert res.get("status") == "ok"
            assert not worker.is_alive()
            with pool._cond:
                assert worker not in pool._idle
                assert worker not in pool._leased
            # The slot is dead, so this cell can succeed only if execute respawns.
            # max_tasks is 1, so release retires that new process too.
            again = pool.execute(code="result = 'second'", req_id="recycle-2")
            assert again.get("status") == "ok", again
            assert again.get("result") == "second"
            assert not worker.is_alive()
        finally:
            pool.shutdown()

    def test_recycle_after_shutdown_does_not_orphan_process(self) -> None:
        """If shutdown() wins the race, recycle must not leave a newly spawned child running."""
        pool = FormulaProcessPool(num_workers=1, default_timeout_sec=15, max_tasks=1)
        worker = pool.workers[0]
        worker.tasks_executed = 1
        pool.shutdown()
        pool.release_worker(worker)
        assert not worker.is_alive(), "Recycle spawn after shutdown must be killed"

    def test_sticky_routing_no_index_error_on_concurrent_shutdown(self) -> None:
        """Sticky routing must not raise IndexError if shutdown() clears workers concurrently (Bug 4 fix)."""
        pool = FormulaProcessPool(num_workers=4, default_timeout_sec=5)
        errors: list[Exception] = []

        def _shutdown_soon() -> None:
            time.sleep(0.02)
            pool.shutdown()

        shutdown_thread = threading.Thread(target=_shutdown_soon)
        shutdown_thread.start()
        # Repeatedly attempt sticky-mode execution while shutdown races; must not raise IndexError
        for i in range(20):
            try:
                pool.execute(
                    code="result = 1",
                    session_id=f"race-session-{i % 4}",
                    mode="shared",
                    timeout_sec=2,
                    req_id=f"race-{i}",
                )
            except IndexError as exc:
                errors.append(exc)
            except Exception:
                pass  # timeout / pool-busy during shutdown is fine
        shutdown_thread.join(timeout=5)
        assert not errors, f"IndexError raised during concurrent shutdown+sticky routing: {errors}"

    def test_shared_session_persists_across_max_tasks(self) -> None:
        """Shared session worker must NOT recycle at max_tasks, keeping state intact."""
        pool = FormulaProcessPool(num_workers=1, default_timeout_sec=15, max_tasks=2)
        try:
            sid = "shared-persist-test"
            r1 = pool.execute(code="val = 100\nresult = val", session_id=sid, mode="shared", req_id="sp-1")
            assert r1.get("status") == "ok"
            assert r1.get("result") == 100

            r2 = pool.execute(code="val += 50\nresult = val", session_id=sid, mode="shared", req_id="sp-2")
            assert r2.get("status") == "ok"
            assert r2.get("result") == 150

            # tasks_executed is now 2 (== max_tasks). Without session awareness, release_worker would kill process.
            # With session awareness, recycling is skipped and state is preserved.
            r3 = pool.execute(code="val += 25\nresult = val", session_id=sid, mode="shared", req_id="sp-3")
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
            r4 = pool.execute(code="result = 'fresh'", mode="isolated", req_id="sp-4")
            assert r4.get("status") == "ok"

            pid_after = pool.workers[0].process.pid if pool.workers[0].process else None
            assert pid_after != pid_before, "Worker should recycle after session is reset when tasks exceed max_tasks"
        finally:
            pool.shutdown()

    def test_session_ttl_evicts_idle_session(self, caplog: pytest.LogCaptureFixture) -> None:
        """An idle worker whose session is past TTL is killed. The next lease respawns it."""
        pool = FormulaProcessPool(num_workers=1, default_timeout_sec=15, max_tasks=1, shared_kernel_ttl_sec=3600.0)
        try:
            sid = "ttl-evict-test"
            r1 = pool.execute(code="val = 42\nresult = val", session_id=sid, mode="shared", req_id="ttl-1")
            assert r1.get("status") == "ok"
            assert pool.live_session_worker(sid) is not None

            # The worker itself is not past idle TTL. Session TTL is what
            # makes the idle reaper kill it.
            with pool._cond:
                pool._sessions[sid].last_active = time.monotonic() - 4000.0
            with caplog.at_level(logging.INFO, logger="compute_service.worker"):
                pool._evict_idle_workers()
            assert "shared sessions were all past the session TTL" in caplog.text
            assert "idle for >" not in caplog.text
            assert pool.live_session_worker(sid) is None, "Session should be dropped when its worker is killed"

            worker = pool.workers[0]
            assert not worker.is_alive()
            with pool._cond:
                assert worker not in pool._idle
                assert sid in pool._lost_sessions

            # The slot is dead, so this cell runs only after execute respawns.
            # max_tasks is 1, so release retires that new process too.
            r2 = pool.execute(code="result = 'recycled'", mode="isolated", req_id="ttl-2")
            assert r2.get("status") == "ok", r2
            assert r2.get("result") == "recycled"
            assert not worker.is_alive()
        finally:
            pool.shutdown()

    def test_idle_worker_reaper(self, caplog: pytest.LogCaptureFixture) -> None:
        """Idle worker reaper must terminate workers idle for longer than idle_worker_ttl_sec."""
        pool = FormulaProcessPool(num_workers=1, default_timeout_sec=15, idle_worker_ttl_sec=3600.0)
        try:
            # Run one task so worker is active and returned to idle
            res = pool.execute(code="result = 123", req_id="idle-1")
            assert res.get("status") == "ok"

            worker = pool.workers[0]
            assert worker.is_alive()

            # Simulate passage of idle time and trigger reaper eviction
            with pool._cond:
                pool._worker_last_active[worker] = time.monotonic() - 4000.0
            with caplog.at_level(logging.INFO, logger="compute_service.worker"):
                pool._evict_idle_workers()
            assert "idle for >" in caplog.text
            assert "shared sessions were all past the session TTL" not in caplog.text
            assert not worker.is_alive(), "Idle worker process should be killed by idle worker reaper"

            # Subsequent execution lazily re-spawns worker
            res2 = pool.execute(code="result = 456", req_id="idle-2")
            assert res2.get("status") == "ok"
            assert res2.get("result") == 456
            assert worker.is_alive(), "Worker should re-spawn lazily on next request"
        finally:
            pool.shutdown()

    def test_idle_reaper_skips_shared_session(self) -> None:
        pool = FormulaProcessPool(num_workers=1, default_timeout_sec=15, idle_worker_ttl_sec=3600.0, shared_kernel_ttl_sec=3600.0)
        try:
            res = pool.execute(code="keep = 4\nresult = keep", session_id="idle-shared", mode="shared", req_id="idle-s1")
            assert res.get("status") == "ok"
            worker = pool.workers[0]
            with pool._cond:
                pool._worker_last_active[worker] = time.monotonic() - 4000.0
            pool._evict_idle_workers()
            assert worker.is_alive()
            again = pool.execute(code="result = keep", session_id="idle-shared", mode="shared", req_id="idle-s2")
            assert again.get("status") == "ok"
            assert again.get("result") == 4
        finally:
            pool.shutdown()

    def test_skip_idle_evict_tracks_worker_sessions(self) -> None:
        """_skip_idle_evict returns True only while worker holds active shared sessions."""
        pool = FormulaProcessPool(num_workers=1, default_timeout_sec=15)
        try:
            worker = pool.workers[0]
            # Initially no sessions attached
            assert pool._skip_idle_evict(worker) is False

            # Attach a shared session
            sid = "skip-evict-sid"
            res = pool.execute(code="val = 42\nresult = val", session_id=sid, mode="shared")
            assert res.get("status") == "ok"
            assert pool._skip_idle_evict(worker) is True

            # Reset the session; _skip_idle_evict should return False again
            reset_res = pool.reset_session(sid)
            assert reset_res.get("status") == "ok"
            assert pool._skip_idle_evict(worker) is False
        finally:
            pool.shutdown()

    def test_reset_keeps_map_when_worker_is_busy(self) -> None:
        pool = FormulaProcessPool(num_workers=1, default_timeout_sec=15)
        try:
            sid = "busy-reset"
            ok = pool.execute(code="x = 1\nresult = x", session_id=sid, mode="shared", req_id="busy-1")
            assert ok.get("status") == "ok"
            worker = pool._sessions[sid].worker
            held = pool.lease_specific(worker, timeout_sec=1)
            assert held is worker
            try:
                res = pool.reset_session(sid, timeout_sec=0.05)
                assert res.get("code") == "WORKER_POOL_BUSY"
                assert pool.live_session_worker(sid) is worker
            finally:
                pool.release_worker(held)
        finally:
            pool.shutdown()

    def test_reset_lease_failure_keeps_lost_marker(self) -> None:
        """A busy reset must not drop the lost marker before the kernel is reset.

        What was wrong: reset_session popped _lost_sessions before leased().
        WORKER_POOL_BUSY left the kernel untouched and the next sticky call
        did not report session_reset.
        """
        pool = FormulaProcessPool(num_workers=1, default_timeout_sec=15)
        try:
            sid = "busy-lost-reset"
            ok = pool.execute(code="x = 1\nresult = x", session_id=sid, mode="shared", req_id="busy-lost-1")
            assert ok.get("status") == "ok"
            worker = pool._sessions[sid].worker
            with pool._cond:
                pool._lost_sessions[sid] = time.monotonic()
            held = pool.lease_specific(worker, timeout_sec=1)
            assert held is worker
            try:
                res = pool.reset_session(sid, timeout_sec=0.05)
                assert res.get("code") == "WORKER_POOL_BUSY"
                with pool._cond:
                    assert sid in pool._lost_sessions
            finally:
                pool.release_worker(held)
            again = pool.execute(code="result = 1", session_id=sid, mode="shared")
            assert again.get("status") == "ok"
            assert again.get("session_reset") is True
        finally:
            pool.shutdown()

    def test_successful_reset_clears_lost_marker(self) -> None:
        """An explicit reset that reaches the worker drops the lost marker."""
        pool = FormulaProcessPool(num_workers=1, default_timeout_sec=15)
        try:
            sid = "reset-clears-lost"
            ok = pool.execute(code="x = 1\nresult = x", session_id=sid, mode="shared")
            assert ok.get("status") == "ok"
            with pool._cond:
                pool._lost_sessions[sid] = time.monotonic()
            reset_res = pool.reset_session(sid)
            assert reset_res.get("status") == "ok"
            with pool._cond:
                assert sid not in pool._lost_sessions
            again = pool.execute(code="result = 2", session_id=sid, mode="shared")
            assert again.get("status") == "ok"
            assert again.get("session_reset") is not True
        finally:
            pool.shutdown()

    def test_lost_sessions_cap_drops_oldest(self) -> None:
        """The lost set keeps the newest ids and drops the oldest past the cap."""
        pool = FormulaProcessPool(num_workers=0, shared_kernel_ttl_sec=0, idle_worker_ttl_sec=0)
        try:
            pool._max_lost_sessions = 1
            with pool._cond:
                pool._mark_session_lost_unlocked("workbook-1")
                pool._mark_session_lost_unlocked("workbook-2")
                assert "workbook-1" not in pool._lost_sessions
                assert "workbook-2" in pool._lost_sessions
        finally:
            pool.shutdown()

    def test_reset_keeps_map_when_worker_reset_fails(self, caplog: pytest.LogCaptureFixture) -> None:
        """A non-ok reset must not forget a namespace the worker still holds."""
        pool = FormulaProcessPool(num_workers=1, default_timeout_sec=15)
        try:
            sid = "reset-fail-keeps-map"
            ok = pool.execute(code="x = 9\nresult = x", session_id=sid, mode="shared", req_id="rf-1")
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
            assert pool.live_session_worker(sid) is worker
            assert sid in pool._worker_sessions_for(worker)
            assert "keeping session map" in caplog.text
            again = pool.execute(code="result = x", session_id=sid, mode="shared", req_id="rf-2")
            assert again.get("status") == "ok"
            assert again.get("result") == 9
        finally:
            pool.shutdown()

    def test_ttl_keeps_session_when_worker_reset_fails(self, caplog: pytest.LogCaptureFixture) -> None:
        """A stale sticky call must not drop the map when the worker reset is not ok."""
        pool = FormulaProcessPool(num_workers=1, default_timeout_sec=15, shared_kernel_ttl_sec=3600.0)
        try:
            sid = "ttl-reset-fail"
            ok = pool.execute(code="x = 4\nresult = x", session_id=sid, mode="shared", req_id="ttl-fail-1")
            assert ok.get("status") == "ok"
            worker = pool._sessions[sid].worker
            real_execute = worker.execute

            def fail_reset(payload: dict, timeout_sec: float = 5.0, *, req_id: Any = None) -> dict:
                if payload.get("action") == "reset_session":
                    return {"status": "error", "error": "namespace still held"}
                return real_execute(payload, timeout_sec, req_id=req_id)

            setattr(worker, "execute", fail_reset)
            with pool._cond:
                pool._sessions[sid].last_active = time.monotonic() - 4000.0
            with caplog.at_level(logging.ERROR, logger="compute_service.formula"):
                again = pool.execute(code="result = x", session_id=sid, mode="shared", req_id="ttl-fail-2")
            assert again.get("status") == "ok"
            assert again.get("result") == 4
            assert again.get("session_reset") is not True
            assert pool.live_session_worker(sid) is worker
            assert sid in pool._worker_sessions_for(worker)
            assert "keeping session map" in caplog.text
        finally:
            pool.shutdown()

    def test_reset_clears_init_companion(self) -> None:
        pool = FormulaProcessPool(num_workers=1, default_timeout_sec=15)
        try:
            sid = "init-reset"
            init = "items = [1]"
            first = pool.execute(
                code="items.append(2)\nresult = list(items)",
                session_id=sid,
                mode="shared",
                init_script=init,
                req_id="init-1",
            )
            assert first.get("status") == "ok"
            assert first.get("result") == [1, 2]
            reset = pool.reset_session(sid)
            assert reset.get("status") == "ok"
            second = pool.execute(
                code="result = list(items)",
                session_id=sid,
                mode="shared",
                init_script=init,
                req_id="init-2",
            )
            assert second.get("status") == "ok"
            assert second.get("result") == [1]
        finally:
            pool.shutdown()

    def test_worker_rejects_pickle_payload_and_false_mode(self) -> None:
        """Stdio must not run a payload or a mode the HTTP edge rejects.

        ``data`` was the pickle/split_grid field. ``mode: false`` was rewritten
        to isolated on the HTTP path and rejected here.
        """
        from compute_service.formula_worker import _handle_request
        from compute_service.json_forward import decode_worker_result

        pickled = decode_worker_result(_handle_request({"id": "pk", "code": "result = data", "wire": "pickle", "data": [1, 2, 3]}))
        assert pickled.get("code") == "INVALID_REQUEST"
        assert pickled.get("result") != [1, 2, 3]

        plain = decode_worker_result(_handle_request({"id": "data-field", "code": "result = data", "data": [1, 2, 3]}))
        assert plain.get("result") != [1, 2, 3]

        bad_mode = decode_worker_result(_handle_request({"id": "m", "code": "result = 1", "mode": False}))
        assert bad_mode.get("code") == "INVALID_REQUEST"
        assert bad_mode.get("result") != 1

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
        pool = FormulaProcessPool(num_workers=2, default_timeout_sec=15, idle_worker_ttl_sec=3600.0)
        try:
            # Execute a task on each worker to populate _idle with both
            for i in range(2):
                res = pool.execute(code=f"result = {i}", req_id=f"evict-{i}")
                assert res.get("status") == "ok"

            # Both workers should be idle now
            with pool._cond:
                assert len(pool._idle) == 2

            # Simulate only one worker being stale
            w0 = pool.workers[0]
            with pool._cond:
                pool._worker_last_active[w0] = time.monotonic() - 4000.0

            pool._evict_idle_workers()

            # Worker process must be dead, and a dead pid is not idle.
            # The next lease claims the cold slot and execute respawns it.
            assert not w0.is_alive(), "Evicted worker process must be killed"
            with pool._cond:
                assert w0 not in pool._idle, "Dead worker must not sit in the idle set"
                assert len(pool._idle) == 1, "Only the live worker stays idle"

            # Verify lazy re-spawn works
            res = pool.execute(code="result = 999", req_id="respawn-1")
            assert res.get("status") == "ok"
            assert res.get("result") == 999
        finally:
            pool.shutdown()

    def test_active_session_routes_to_mapped_worker(self) -> None:
        """Shared session execution routes to _active_sessions[sid] if already mapped."""
        pool = FormulaProcessPool(num_workers=3, default_timeout_sec=15)
        try:
            sid = "affinity-direct"
            # Map session to worker #2 explicitly, including the owning pid.
            # A map entry without that pid is a stale cache and is dropped.
            target_worker = pool.workers[2]
            with pool._cond:
                from compute_service.formula_pool import _Session
                assert target_worker.process is not None
                pool._sessions[sid] = _Session(worker=target_worker, pid=target_worker.process.pid, last_active=time.monotonic())

            res = pool.execute(code="state = 42\nresult = state", session_id=sid, mode="shared")
            assert res.get("status") == "ok"
            assert res.get("result") == 42
            with pool._cond:
                assert pool.live_session_worker(sid) is target_worker
        finally:
            pool.shutdown()

    def test_pool_shutdown_signals_reaper_event(self) -> None:
        """Pool shutdown sets _reaper_stop_event so reaper threads terminate promptly."""
        pool = FormulaProcessPool(num_workers=1, idle_worker_ttl_sec=3600.0)
        assert not pool._reaper_stop_event.is_set()
        pool.shutdown()
        assert pool._reaper_stop_event.is_set()

    def test_evict_idle_workers_skips_dead_worker(self) -> None:
        """_evict_idle_workers must skip dead workers already in _idle via continue."""
        pool = FormulaProcessPool(num_workers=1, idle_worker_ttl_sec=0.01)
        try:
            worker = pool.lease_any(2)
            assert worker is not None
            worker.kill()
            with pool._cond:
                pool._idle[worker] = None
                pool._leased.discard(worker)
                pool._worker_last_active[worker] = time.monotonic() - 100.0
            pool._evict_idle_workers()
            with pool._cond:
                assert worker not in pool._idle
        finally:
            pool.shutdown()

    def test_convenience_data_that_is_not_strict_json_returns_error(self) -> None:
        """json.dumps(allow_nan=False) used to raise out of execute().

        HTTP always passes data_json. Those bytes are forwarded unchanged.
        """
        forwarded = FormulaProcessPool._build_execute_payload(
            code="result = 1",
            data={"x": float("nan")},
            data_json=b'{"x": NaN}',
            session_id=None,
            mode="isolated",
            init_script=None,
            req_id="wire",
        )
        assert forwarded["data_json"] == b'{"x": NaN}'

        pool = FormulaProcessPool(num_workers=1, default_timeout_sec=15)
        try:
            nan_res = pool.execute(code="result = data", data={"x": float("nan")}, req_id="nan-data")
            assert nan_res.get("status") == "error"
            assert nan_res.get("code") == "INVALID_REQUEST"
            assert nan_res.get("id") == "nan-data"
            obj_res = pool.execute(code="result = data", data={"x": object()}, req_id="obj-data")
            assert obj_res.get("status") == "error"
            assert obj_res.get("code") == "INVALID_REQUEST"
            assert obj_res.get("id") == "obj-data"
        finally:
            pool.shutdown()

    def test_build_execute_payload_rejects_unknown_wire(self) -> None:
        """Unknown wire is an error. It used to be rewritten to json_forward."""
        from compute_service.json_forward import ExecuteRequestError

        with pytest.raises(ExecuteRequestError, match="wire"):
            FormulaProcessPool._build_execute_payload(
                code="result = 1",
                data=None,
                data_json=None,
                session_id=None,
                mode="isolated",
                init_script=None,
                req_id="test-wire",
                wire="bogus_wire",
            )

    def test_deadline_left_floors_spent_clock(self) -> None:
        from compute_service.worker_base import _PIPE_WAIT_FLOOR, _Deadline

        future = _Deadline.from_absolute(10.0, time.monotonic() + 10.0)
        assert future.left() > 0.0
        spent = _Deadline.from_absolute(10.0, time.monotonic() - 10.0)
        assert spent.left() == _PIPE_WAIT_FLOOR

    @pytest.mark.skipif(sys.platform == "win32", reason="uses SIGKILL")
    def test_shared_session_dies_with_its_process(self) -> None:
        """Killing the pid drops the session. A respawn is not the same workbook."""
        import signal

        pool = FormulaProcessPool(num_workers=1, default_timeout_sec=15)
        try:
            sid = "dies-with-pid"
            created = pool.execute(code="keep = 5\nresult = keep", session_id=sid, mode="shared", req_id="die-1")
            assert created.get("status") == "ok"
            assert created.get("result") == 5
            owner = pool.live_session_worker(sid)
            assert owner is not None and owner.process is not None
            pid = owner.process.pid
            os.kill(pid, signal.SIGKILL)
            owner.process.wait(timeout=2)
            assert pool.live_session_worker(sid) is None
            again = pool.execute(code="result = keep", session_id=sid, mode="shared", timeout_sec=15, req_id="die-2")
            assert again.get("status") == "error"
            assert again.get("result") != 5
            assert again.get("session_reset") is True
        finally:
            pool.shutdown()

    def test_sigkill_drops_every_session_on_the_pid(self) -> None:
        pool = FormulaProcessPool(num_workers=1, default_timeout_sec=15)
        try:
            first = pool.execute(code="a = 1\nresult = a", session_id="sess-a", mode="shared")
            second = pool.execute(code="b = 2\nresult = b", session_id="sess-b", mode="shared")
            assert first.get("status") == "ok"
            assert second.get("status") == "ok"
            owner = pool.live_session_worker("sess-a")
            assert owner is not None
            assert owner is pool.live_session_worker("sess-b")
            owner.kill()
            assert pool.live_session_worker("sess-a") is None
            assert pool.live_session_worker("sess-b") is None
        finally:
            pool.shutdown()

    @pytest.mark.skipif(sys.platform == "win32", reason="the in-child cell timeout uses signal.alarm; Windows falls back to the host kill")
    def test_timeout_does_not_sigkill_healthy_shared_kernel(self) -> None:
        """A sleep past the cell budget returns an error frame. The pid stays.

        The child used to be given the original timeout_sec while the host
        read only the time left on the deadline, so this sleep was SIGKILL
        and every other workbook on that process disappeared.
        """
        pool = FormulaProcessPool(num_workers=1, default_timeout_sec=30)
        try:
            kept = pool.execute(code="keep = 7\nresult = keep", session_id="doc-keep", mode="shared", req_id="budget-keep")
            assert kept.get("status") == "ok"
            owner = pool.live_session_worker("doc-keep")
            assert owner is not None and owner.process is not None
            pid = owner.process.pid
            # timeout_sec is the original 30s budget. The deadline is only
            # about a second from now, which is what the child must honor.
            hung = pool.execute(
                code="import time\ntime.sleep(8)\nresult = 1",
                session_id="doc-hang",
                mode="shared",
                timeout_sec=30,
                deadline=time.monotonic() + 1.2,
                req_id="budget-hang",
            )
            assert hung.get("status") == "error"
            assert hung.get("code") != "EXECUTION_TIMEOUT"
            assert owner.process is not None and owner.process.pid == pid
            assert owner.is_alive()
            again = pool.execute(code="result = keep", session_id="doc-keep", mode="shared", timeout_sec=15, req_id="budget-again")
            assert again.get("status") == "ok"
            assert again.get("result") == 7
        finally:
            pool.shutdown()

    def test_bad_mode_does_not_run(self) -> None:
        pool = FormulaProcessPool(num_workers=1, default_timeout_sec=15)
        try:
            res = pool.execute(code="result = 1", mode="Shared", req_id="bad-mode")
            assert res.get("status") == "error"
            assert res.get("code") == "INVALID_REQUEST"
            assert res.get("result") != 1
        finally:
            pool.shutdown()

    def test_isolated_leases_session_process_when_all_workers_have_sessions(self) -> None:
        pool = FormulaProcessPool(num_workers=1, default_timeout_sec=15)
        try:
            created = pool.execute(code="keep = 1\nresult = keep", session_id="only-shared", mode="shared")
            assert created.get("status") == "ok"
            owner = pool.live_session_worker("only-shared")
            assert owner is not None and owner.process is not None
            pid = owner.process.pid
            ran = pool.execute(code="result = 1", mode="isolated", timeout_sec=1, req_id="iso-blocked")
            assert ran.get("status") == "ok"
            assert ran.get("result") == 1
            assert owner.is_alive()
            assert owner.process is not None and owner.process.pid == pid
        finally:
            pool.shutdown()

    @pytest.mark.skipif(sys.platform == "win32", reason="uses SIGKILL")
    def test_reap_dead_worker_does_not_respawn(self) -> None:
        """An exited pid is marked lost. Reaping it must not lease and respawn."""
        pool = FormulaProcessPool(num_workers=1, default_timeout_sec=15, shared_kernel_ttl_sec=3600.0)
        try:
            sid = "ttl-dead"
            created = pool.execute(code="result = 1", session_id=sid, mode="shared", req_id="ttl-dead-1")
            assert created.get("status") == "ok"
            worker = pool.workers[0]
            proc = worker.process
            assert proc is not None
            proc.kill()
            proc.wait(timeout=5)
            assert pool.live_session_worker(sid) is None
            assert worker.process is proc
            assert not worker.is_alive()
            with pool._cond:
                assert sid not in pool._sessions
                assert sid in pool._lost_sessions
        finally:
            pool.shutdown()


class TestFormulaHttpEndpoint:
    @pytest.fixture
    def formula_server(self):
        port = get_free_port()
        settings = ComputeSettings(
            host="127.0.0.1",
            port=port,
            api_key="formula-secret",
        )
        app = create_wsgi_app(settings)
        server = WSGIDualStackServer("127.0.0.1", port, max_threads=2)
        server.set_app(app)

        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        time.sleep(0.15)
        yield f"http://127.0.0.1:{port}"
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)

    def _post(
        self,
        url: str,
        payload: dict,
        headers: dict | None = None,
        path: str = "/v1/execute",
    ) -> tuple[int, dict]:
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
        status, body = self._post(
            formula_server,
            {"id": "req-1", "code": "result = 7 * 8"},
            headers={"Authorization": "Bearer formula-secret"},
        )
        assert status == 200
        assert body.get("id") == "req-1"
        assert body.get("status") == "ok"
        assert body.get("result") == 56

    def test_http_timeout_is_200_error_then_next_ok(self, formula_server: str) -> None:
        status, body = self._post(
            formula_server,
            {
                "id": "slow-1",
                "code": "import time\ntime.sleep(5)\nresult = 1",
                "timeout_ms": 1500,
            },
            headers={"Authorization": "Bearer formula-secret"},
        )
        assert status == 200
        assert body.get("status") == "error"
        status2, body2 = self._post(
            formula_server,
            {"id": "slow-2", "code": "result = 4"},
            headers={"Authorization": "Bearer formula-secret"},
        )
        assert status2 == 200
        assert body2.get("status") == "ok"
        assert body2.get("result") == 4

    def test_http_code_too_large(self, formula_server: str) -> None:
        status, body = self._post(
            formula_server,
            {"id": "big", "code": "result = 1\n" + ("x = 1\n" * 200000)},
            headers={"Authorization": "Bearer formula-secret"},
        )
        assert status == 400
        assert body.get("code") == "CODE_TOO_LARGE"

    def test_http_eval_error_is_200(self, formula_server: str) -> None:
        status, body = self._post(
            formula_server,
            {"id": "div0", "code": "result = 1 / 0"},
            headers={"Authorization": "Bearer formula-secret"},
        )
        assert status == 200
        assert body.get("status") == "error"
        assert body.get("id") == "div0"
        assert "error" in body

    def test_execute_shared_session(self, formula_server: str) -> None:
        session_id = "session-http-123"
        status1, body1 = self._post(
            formula_server,
            {"id": "req-s1", "code": "val = 42\nresult = val", "mode": "shared"},
            headers={"Authorization": "Bearer formula-secret"},
            path=f"/v1/execute?session_id={session_id}",
        )
        assert status1 == 200
        assert body1.get("result") == 42

        status2, body2 = self._post(
            formula_server,
            {"id": "req-s2", "code": "val += 8\nresult = val", "mode": "shared"},
            headers={"Authorization": "Bearer formula-secret"},
            path=f"/v1/execute?session_id={session_id}",
        )
        assert status2 == 200
        assert body2.get("result") == 50

    def test_http_session_reset_unknown_is_ok(self, formula_server: str) -> None:
        status, body = self._post(
            formula_server,
            {"id": "reset-unknown"},
            headers={"Authorization": "Bearer formula-secret"},
            path="/v1/session/reset?session_id=never-created",
        )
        assert status == 200
        assert body == {"id": "reset-unknown", "status": "ok"}

    def test_http_session_reset_clears_shared_state(self, formula_server: str) -> None:
        session_id = "session-reset-http"
        headers = {"Authorization": "Bearer formula-secret"}
        status1, body1 = self._post(
            formula_server,
            {"id": "rs-1", "code": "kept = 19\nresult = kept", "mode": "shared"},
            headers=headers,
            path=f"/v1/execute?session_id={session_id}",
        )
        assert status1 == 200
        assert body1.get("result") == 19

        status_reset, reset_body = self._post(
            formula_server,
            {"id": "rs-reset"},
            headers=headers,
            path=f"/v1/session/reset?session_id={session_id}",
        )
        assert status_reset == 200
        assert reset_body == {"id": "rs-reset", "status": "ok"}

        status2, body2 = self._post(
            formula_server,
            {"id": "rs-2", "code": "result = kept", "mode": "shared"},
            headers=headers,
            path=f"/v1/execute?session_id={session_id}",
        )
        assert status2 == 200
        assert body2.get("status") == "error"
        assert "kept" in body2.get("error", "") or "NameError" in body2.get("error", "")

    def test_shared_session_balances_across_least_loaded_worker(self) -> None:
        """New shared sessions must be assigned to the worker holding the fewest active sessions."""
        pool = FormulaProcessPool(num_workers=2, default_timeout_sec=15)
        try:
            # First session lands on the least-loaded live worker. On a tie
            # that is the lowest worker_id (hash() used to depend on PYTHONHASHSEED).
            res1 = pool.execute(code="result = 1", session_id="sess-1", mode="shared")
            assert res1.get("status") == "ok"
            w1 = pool.live_session_worker("sess-1")

            # Second session should pick the other worker because it has 0 sessions
            res2 = pool.execute(code="result = 2", session_id="sess-2", mode="shared")
            assert res2.get("status") == "ok"
            w2 = pool.live_session_worker("sess-2")

            assert w1 is not None and w2 is not None
            live = [w for w in pool.workers if w.is_alive()]
            assert w1.worker_id == min(w.worker_id for w in live)
            assert w1 is not w2, "Sessions must balance across distinct workers when both are available"
        finally:
            pool.shutdown()

    def test_isolated_worker_lease_prefers_session_free_worker(self) -> None:
        """Isolated workload lease must prefer idle workers that host zero shared sessions."""
        pool = FormulaProcessPool(num_workers=2, default_timeout_sec=15)
        try:
            # Register a shared session on one worker
            res1 = pool.execute(code="x = 10; result = x", session_id="shared-worker-test", mode="shared")
            assert res1.get("status") == "ok"
            shared_worker = pool.live_session_worker("shared-worker-test")

            # An isolated execution should prefer the session-free worker
            with pool._cond:
                picked = pool._pick_idle_worker()
                assert picked is not None
                assert picked is not shared_worker
                # Put it back
                pool._idle[picked] = None
        finally:
            pool.shutdown()

    def test_isolated_worker_lease_falls_back_when_all_workers_have_sessions(self) -> None:
        """When all idle workers hold shared sessions, _pick_idle_worker still picks an idle worker."""
        pool = FormulaProcessPool(num_workers=2, default_timeout_sec=15)
        try:
            res1 = pool.execute(code="result = 1", session_id="s1", mode="shared")
            assert res1.get("status") == "ok"
            res2 = pool.execute(code="result = 2", session_id="s2", mode="shared")
            assert res2.get("status") == "ok"

            with pool._cond:
                # Both workers have sessions and both are idle
                assert len(pool._idle) == 2
                picked = pool._pick_idle_worker()
                assert picked is not None
                pool._idle[picked] = None
        finally:
            pool.shutdown()

    def test_subsecond_budget_does_not_sigkill_worker(self) -> None:
        """A deadline under one second is refused. It does not lease or SIGKILL."""
        pool = FormulaProcessPool(num_workers=1, default_timeout_sec=30)
        try:
            warm = pool.execute(code="result = 1", req_id="warmup")
            assert warm.get("status") == "ok"

            worker = pool.workers[0]
            calls: list[float] = []
            real_execute = worker.execute

            def spy_execute(payload: dict[str, Any], timeout_sec: float, **kwargs: Any) -> dict[str, Any]:
                calls.append(timeout_sec)
                return real_execute(payload, timeout_sec, **kwargs)

            setattr(worker, "execute", spy_execute)
            res = pool.execute(
                code="result = 42",
                deadline=time.monotonic() + 0.5,
                req_id="subsecond-test",
            )
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

        pool = FormulaProcessPool(num_workers=1, default_timeout_sec=15)
        try:
            sid = "lost-session-test"
            r1 = pool.execute(code="x = 100\nresult = x", session_id=sid, mode="shared", req_id="r1")
            assert r1.get("status") == "ok"
            assert r1.get("session_reset") is not True

            owner = pool.live_session_worker(sid)
            assert owner is not None and owner.process is not None
            os.kill(owner.process.pid, signal.SIGKILL)
            owner.process.wait(timeout=2)

            r2 = pool.execute(code="result = 200", session_id=sid, mode="shared", req_id="r2")
            assert r2.get("status") == "ok"
            assert r2.get("session_reset") is True
            if "result_json" in r2:
                payload = json.loads(r2["result_json"])
                assert payload.get("session_reset") is True

            # Third call on same session does not report session_reset again (it was consumed)
            r3 = pool.execute(code="result = 300", session_id=sid, mode="shared", req_id="r3")
            assert r3.get("status") == "ok"
            assert r3.get("session_reset") is not True
        finally:
            pool.shutdown()

    def test_payload_too_large_keeps_session_reset(self) -> None:
        """A frame that never reaches the child must not consume session_reset."""
        pool = FormulaProcessPool(num_workers=1, default_timeout_sec=15)
        try:
            sid = "unrun-lost"
            created = pool.execute(code="x = 1\nresult = x", session_id=sid, mode="shared", req_id="warm")
            assert created.get("status") == "ok"
            worker = pool.live_session_worker(sid)
            assert worker is not None
            saved_cap = worker.max_payload_bytes
            with pool._cond:
                pool._lost_sessions[sid] = time.monotonic()
            worker.max_payload_bytes = 32
            try:
                rejected = pool.execute(
                    code="result = 1",
                    data={"blob": "x" * 200},
                    session_id=sid,
                    mode="shared",
                    req_id="too-big",
                )
            finally:
                worker.max_payload_bytes = saved_cap
            assert rejected.get("code") == "PAYLOAD_TOO_LARGE"
            with pool._cond:
                assert sid in pool._lost_sessions
            nxt = pool.execute(code="result = 2", session_id=sid, mode="shared", req_id="after")
            assert nxt.get("status") == "ok"
            assert nxt.get("session_reset") is True
        finally:
            pool.shutdown()

    def test_concurrent_first_calls_same_session(self) -> None:
        """Concurrent first calls for the same session_id must reserve and route to the same worker."""
        pool = FormulaProcessPool(num_workers=2, default_timeout_sec=15)
        try:
            sid = "concurrent-first-call-session"
            barrier = threading.Barrier(2)
            results: list[dict] = [{}, {}]

            def run_worker(idx: int, code: str) -> None:
                barrier.wait()
                res = pool.execute(code=code, session_id=sid, mode="shared", req_id=f"req-{idx}")
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
                worker_ids = [w.worker_id for w in pool.workers if sid in pool._worker_sessions_for(w)]
                assert len(worker_ids) == 1

            # Shared state is maintained on this worker
            r3 = pool.execute(code="x = 100\nresult = x", session_id=sid, mode="shared")
            assert r3.get("status") == "ok"
            r4 = pool.execute(code="result = x + 1", session_id=sid, mode="shared")
            assert r4.get("status") == "ok"
            assert r4.get("result") == 101
        finally:
            pool.shutdown()

    def test_ttl_expiry_reports_session_reset_on_the_sticky_call(self) -> None:
        """The sticky call after session TTL resets that kernel and reports the flag once."""
        pool = FormulaProcessPool(num_workers=1, default_timeout_sec=15, shared_kernel_ttl_sec=3600.0)
        try:
            sid = "ttl-evicted-session"
            r1 = pool.execute(code="x = 77\nresult = x", session_id=sid, mode="shared")
            assert r1.get("status") == "ok"
            assert r1.get("session_reset") is not True
            worker = pool.workers[0]

            with pool._cond:
                pool._sessions[sid].last_active = time.monotonic() - 4000.0

            r2 = pool.execute(code="result = 88", session_id=sid, mode="shared")
            assert r2.get("status") == "ok"
            assert r2.get("result") == 88
            assert r2.get("session_reset") is True
            # The map stays on this process. Marking the id lost would make
            # the following cell report session_reset again.
            with pool._cond:
                assert pool._sessions[sid].worker is worker
                assert sid not in pool._lost_sessions
            gone = pool.execute(code="result = x", session_id=sid, mode="shared")
            assert gone.get("status") == "error"
            assert gone.get("session_reset") is not True
        finally:
            pool.shutdown()

    def test_stale_session_resets_without_killing_fresh_sibling(self) -> None:
        """A fresh session on the same worker keeps the process up. The stale id resets on its next call."""
        pool = FormulaProcessPool(num_workers=1, default_timeout_sec=15, shared_kernel_ttl_sec=3600.0, idle_worker_ttl_sec=3600.0)
        try:
            fresh = pool.execute(code="a = 1\nresult = a", session_id="fresh-sib", mode="shared")
            stale = pool.execute(code="b = 2\nresult = b", session_id="stale-sib", mode="shared")
            assert fresh.get("status") == "ok"
            assert stale.get("status") == "ok"
            worker = pool.workers[0]
            with pool._cond:
                pool._sessions["stale-sib"].last_active = time.monotonic() - 4000.0
                pool._worker_last_active[worker] = time.monotonic() - 4000.0
            pool._evict_idle_workers()
            assert worker.is_alive()
            later = pool.execute(code="result = b", session_id="stale-sib", mode="shared")
            assert later.get("session_reset") is True
            assert later.get("status") == "error"
            still = pool.execute(code="result = a", session_id="fresh-sib", mode="shared")
            assert still.get("status") == "ok"
            assert still.get("result") == 1
            assert still.get("session_reset") is not True
        finally:
            pool.shutdown()

    def test_explicit_reset_does_not_mark_session_lost(self) -> None:
        """Explicit reset_session clears session without marking it lost (no session_reset on next call)."""
        pool = FormulaProcessPool(num_workers=1, default_timeout_sec=15)
        try:
            sid = "explicit-reset-session"
            r1 = pool.execute(code="x = 99\nresult = x", session_id=sid, mode="shared")
            assert r1.get("status") == "ok"

            reset_res = pool.reset_session(sid)
            assert reset_res.get("status") == "ok"

            with pool._cond:
                assert sid not in pool._lost_sessions

            r2 = pool.execute(code="result = 100", session_id=sid, mode="shared")
            assert r2.get("status") == "ok"
            assert r2.get("session_reset") is not True
        finally:
            pool.shutdown()

    def test_lost_sessions_is_capped(self) -> None:
        """_lost_sessions is bounded and prunes oldest entries."""
        pool = FormulaProcessPool(num_workers=1, default_timeout_sec=15)
        try:
            with pool._cond:
                for i in range(1200):
                    pool._mark_session_lost_unlocked(f"lost-{i}")
                assert len(pool._lost_sessions) == 1000
                assert "lost-0" not in pool._lost_sessions
                assert "lost-1199" in pool._lost_sessions
        finally:
            pool.shutdown()

    def test_failed_lease_specific_drops_new_session(self) -> None:
        """When lease_specific fails, the newly reserved session is dropped (Bug 2)."""
        pool = FormulaProcessPool(num_workers=1, default_timeout_sec=15)
        try:
            sid = "new-phantom-test"
            with patch.object(pool, "lease_specific", return_value=None):
                res = pool.execute(code="result = 1", session_id=sid, mode="shared")
                assert res.get("status") == "error"
                assert res.get("code") == "WORKER_POOL_BUSY"

            # The newly reserved session must NOT remain as a phantom in _sessions
            with pool._cond:
                assert sid not in pool._sessions
                assert not pool._worker_has_sessions(pool.workers[0])
        finally:
            pool.shutdown()

    def test_readded_session_pops_lost_session(self) -> None:
        """_finalize_session pops _lost_sessions when session is re-added or alive (Bug 4)."""
        pool = FormulaProcessPool(num_workers=1, default_timeout_sec=15)
        try:
            sid = "lost-race-test"
            # Simulate session being marked lost in _lost_sessions concurrently
            with pool._cond:
                pool._lost_sessions[sid] = time.monotonic()
                assert sid in pool._lost_sessions

            # Execute code in shared mode; finally block re-adds/confirms alive
            res = pool.execute(code="result = 123", session_id=sid, mode="shared")
            assert res.get("status") == "ok"
            assert res.get("result") == 123

            with pool._cond:
                # Must be popped from _lost_sessions
                assert sid not in pool._lost_sessions
                assert sid in pool._sessions
        finally:
            pool.shutdown()

    def test_worker_session_index(self) -> None:
        """Session counts are derived from _sessions."""
        pool = FormulaProcessPool(num_workers=2, default_timeout_sec=15)
        try:
            w1 = pool.workers[0]
            w2 = pool.workers[1]
            from compute_service.formula_pool import _Session

            assert pool._worker_session_count(w1) == 0
            assert not pool._worker_has_sessions(w1)
            assert pool._worker_sessions_for(w1) == []

            with pool._cond:
                pool._sessions["s1"] = _Session(worker=w1, pid=111, last_active=time.monotonic())
                pool._sessions["s2"] = _Session(worker=w1, pid=111, last_active=time.monotonic())
                pool._sessions["s3"] = _Session(worker=w2, pid=222, last_active=time.monotonic())

            assert pool._worker_session_count(w1) == 2
            assert pool._worker_has_sessions(w1)
            assert set(pool._worker_sessions_for(w1)) == {"s1", "s2"}
            assert pool._worker_session_count(w2) == 1
            assert pool._worker_sessions_for(w2) == ["s3"]

            with pool._cond:
                pool._sessions.pop("s1", None)

            assert pool._worker_session_count(w1) == 1
            assert pool._worker_sessions_for(w1) == ["s2"]

            # Reassigning session to different worker updates both sets
            with pool._cond:
                pool._sessions["s2"] = _Session(worker=w2, pid=222, last_active=time.monotonic())

            assert pool._worker_session_count(w1) == 0
            assert not pool._worker_has_sessions(w1)
            assert pool._worker_session_count(w2) == 2
            assert set(pool._worker_sessions_for(w2)) == {"s2", "s3"}
        finally:
            pool.shutdown()

    def test_drop_session_helper(self) -> None:
        """_drop_session drops a session and optionally records it as lost."""
        pool = FormulaProcessPool(num_workers=1, default_timeout_sec=15)
        try:
            w = pool.workers[0]
            from compute_service.formula_pool import _Session
            with pool._cond:
                pool._sessions["drop-lost"] = _Session(worker=w, pid=100, last_active=time.monotonic())
                pool._sessions["drop-clean"] = _Session(worker=w, pid=100, last_active=time.monotonic())

            pool._drop_session("drop-lost", lost=True)
            with pool._cond:
                assert "drop-lost" not in pool._sessions
                assert "drop-lost" in pool._lost_sessions

            pool._drop_session("drop-clean", lost=False)
            with pool._cond:
                assert "drop-clean" not in pool._sessions
                assert "drop-clean" not in pool._lost_sessions
        finally:
            pool.shutdown()

    def test_pool_leased_context_manager(self) -> None:
        """BaseProcessPool.leased context manager leases and releases workers."""
        pool = FormulaProcessPool(num_workers=1, default_timeout_sec=15)
        try:
            with pool.leased(timeout_sec=5.0) as worker:
                assert worker is not None
                assert worker in pool._leased
            # Released upon exiting context
            assert worker not in pool._leased
            assert worker in pool._idle
        finally:
            pool.shutdown()

    def test_reap_keeps_unbound_reservation(self) -> None:
        """A new session with pid None is not lost while its worker is still dead."""
        from compute_service.formula_pool import _Session

        pool = FormulaProcessPool(num_workers=1, default_timeout_sec=5)
        try:
            worker = pool.workers[0]
            worker.kill()
            with pool._cond:
                pool._sessions["pending"] = _Session(worker=worker, pid=None, last_active=time.monotonic())
                pool._sessions["dead"] = _Session(worker=worker, pid=999999, last_active=time.monotonic())
                pool._reap_dead_sessions_unlocked()
                assert "pending" in pool._sessions
                assert "pending" not in pool._lost_sessions
                assert "dead" not in pool._sessions
                assert "dead" in pool._lost_sessions
        finally:
            pool.shutdown()

    def test_expired_lease_budget_does_not_execute(self) -> None:
        """Selecting a worker must not start the cell once the deadline has passed."""
        pool = FormulaProcessPool(num_workers=1, default_timeout_sec=15)
        try:
            warm = pool.execute(code="result = 1")
            assert warm.get("status") == "ok"
            worker = pool.workers[0]
            called: list[str] = []
            real_execute = worker.execute

            def spy(payload: dict[str, Any], timeout_sec: float, **kwargs: Any) -> dict[str, Any]:
                called.append("exec")
                return real_execute(payload, timeout_sec, **kwargs)

            setattr(worker, "execute", spy)
            orig = pool._select_shared_worker

            def slow(sid: str) -> tuple[Any, bool, bool, int]:
                time.sleep(0.2)
                return orig(sid)

            setattr(pool, "_select_shared_worker", slow)
            res = pool.execute(
                code="result = 2",
                session_id="budget-sid",
                mode="shared",
                deadline=time.monotonic() + 0.05,
            )
            assert res.get("code") == "QUEUE_TIMEOUT"
            assert called == []
            assert pool.live_session_worker("budget-sid") is None
        finally:
            pool.shutdown()

    def test_zero_timeout_is_not_the_default(self) -> None:
        """timeout_sec=0 expires immediately. It used to become the 30s default."""
        pool = FormulaProcessPool(num_workers=1, default_timeout_sec=30)
        try:
            res = pool.execute(code="result = 1", timeout_sec=0)
            assert res.get("code") == "QUEUE_TIMEOUT"
        finally:
            pool.shutdown()


def _idle_reaper_idents() -> set[int]:
    return {t.ident for t in threading.enumerate() if t.name.endswith("-idle-reaper") and t.ident is not None}


def test_session_ttl_starts_reaper_when_idle_ttl_is_zero() -> None:
    """Idle TTL 0 must not disable session-TTL eviction.

    What was wrong: the reaper thread started only when idle_worker_ttl_sec
    was positive, and it is the only pass that kills a worker whose shared
    sessions are all past the session TTL.
    """
    before = _idle_reaper_idents()
    pool = FormulaProcessPool(num_workers=0, idle_worker_ttl_sec=0, shared_kernel_ttl_sec=30)
    try:
        started = _idle_reaper_idents() - before
        assert started
    finally:
        pool.shutdown()


def test_both_ttls_zero_does_not_start_reaper() -> None:
    """0 on both timers leaves the reaper off. A zero interval would spin."""
    before = _idle_reaper_idents()
    pool = FormulaProcessPool(num_workers=0, idle_worker_ttl_sec=0, shared_kernel_ttl_sec=0)
    try:
        assert _idle_reaper_idents() - before == set()
    finally:
        pool.shutdown()


def test_idle_ttl_zero_reaps_abandoned_sessions_only(caplog: pytest.LogCaptureFixture) -> None:
    """With idle TTL 0, a fresh session stays. A fully stale worker is killed.

    Idle TTL 0 must not count as already expired, or the first scan would
    kill every idle child.
    """
    from compute_service.formula_pool import _Session

    class _StandIn:
        def __init__(self) -> None:
            self.killed = 0

        def is_alive(self) -> bool:
            return True

        def kill(self) -> None:
            self.killed += 1

        def _cap_stderr_log(self) -> None:
            return None

    pool = FormulaProcessPool(num_workers=0, idle_worker_ttl_sec=0, shared_kernel_ttl_sec=3600.0)
    fresh = _StandIn()
    stale = _StandIn()
    now = time.monotonic()
    try:
        with pool._cond:
            pool._idle[fresh] = None  # type: ignore[index]
            pool._idle[stale] = None  # type: ignore[index]
            pool._worker_last_active[fresh] = now  # type: ignore[index]
            pool._worker_last_active[stale] = now  # type: ignore[index]
            pool._sessions["fresh"] = _Session(worker=fresh, pid=1, last_active=now)  # type: ignore[arg-type]
            pool._sessions["stale"] = _Session(worker=stale, pid=2, last_active=now - 4000.0)  # type: ignore[arg-type]
        with caplog.at_level(logging.INFO, logger="compute_service.worker"):
            pool._evict_idle_workers()
        assert fresh.killed == 0
        assert fresh in pool._idle
        assert stale.killed == 1
        assert stale not in pool._idle
        assert "shared sessions were all past the session TTL" in caplog.text
        assert "idle for >" not in caplog.text
    finally:
        pool.shutdown()


def test_bad_result_json_is_worker_crashed_without_kill(monkeypatch: pytest.MonkeyPatch) -> None:
    """A result_json that is not JSON is an error dict, and the child stays.

    What was wrong: decode_worker_result's json.loads escaped execute.
    The pickle frame was already consumed, so killing the child would drop
    shared sessions for a bad inner blob.
    """
    from compute_service.worker_base import BaseProcessWorker, _Deadline

    monkeypatch.setattr(BaseProcessWorker, "respawn", lambda self, timeout_sec=0.0, deadline=None: None)
    worker = BaseProcessWorker(1, "unused.py")
    killed: list[bool] = []
    worker.kill = lambda: killed.append(True)  # type: ignore[method-assign]
    blob = {"raw": b"not-json"}

    def _return_blob(payload: dict[str, Any], timeout_sec: float, *, req_id: Any = None) -> dict[str, Any]:
        del payload, timeout_sec
        return {"status": "ok", "result_json": blob["raw"], "id": req_id}

    worker.execute = _return_blob  # type: ignore[method-assign]
    pool = FormulaProcessPool(num_workers=0, idle_worker_ttl_sec=0, shared_kernel_ttl_sec=0)
    try:
        clock = _Deadline(30.0)
        res = pool._run_execution(worker, {}, clock, False, "bad-json", True)
        assert res.get("code") == "WORKER_CRASHED"
        assert res.get("id") == "bad-json"
        assert "session_reset" not in res
        assert "could not be decoded" in str(res.get("error"))

        blob["raw"] = b"\xff"
        lost = pool._run_execution(worker, {}, clock, True, "bad-utf8", True)
        assert lost.get("code") == "WORKER_CRASHED"
        assert lost.get("id") == "bad-utf8"
        assert lost.get("session_reset") is True
        assert killed == []
    finally:
        pool.shutdown()




