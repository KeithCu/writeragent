# WriterAgent - Python Compute Service Formula Pool tests
# Copyright (c) 2026 KeithCu
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import json
import logging
import os
import socket
import threading
import time
import urllib.error
import urllib.request

import pytest

from compute_service.config import ComputeSettings
from compute_service.formula_pool import (
    FormulaProcessPool,
    shutdown_formula_pool,
)
from compute_service.server import WSGIDualStackServer, create_wsgi_app


def get_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(autouse=True)
def cleanup_formula_pool():
    yield
    shutdown_formula_pool()


class TestFormulaPoolSupervisor:
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

    def test_shared_and_isolated_exclusive_occupancy(self) -> None:
        pool = FormulaProcessPool(num_workers=1, default_timeout_sec=15)
        try:
            sid = "occupancy-session"
            first = pool.execute(
                code="x = 5\nresult = x",
                session_id=sid,
                mode="shared",
                req_id="occ-1",
            )
            assert first.get("status") == "ok"

            isolated_holder: list[dict] = []

            def _isolated() -> None:
                isolated_holder.append(
                    pool.execute(
                        code="import time\ntime.sleep(0.2)\nresult = 99",
                        mode="isolated",
                        timeout_sec=10,
                        req_id="occ-iso",
                    )
                )

            thread = threading.Thread(target=_isolated)
            thread.start()
            time.sleep(0.05)
            shared = pool.execute(
                code="result = x",
                session_id=sid,
                mode="shared",
                timeout_sec=10,
                req_id="occ-2",
            )
            thread.join(timeout=10)
            assert isolated_holder and isolated_holder[0].get("status") == "ok"
            assert isolated_holder[0].get("result") == 99
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
        pool = FormulaProcessPool(num_workers=1, default_timeout_sec=1)
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
        """After max_tasks is reached, release_worker must re-spawn the worker so the
        idle set never contains a dead process (Bug 3 fix)."""
        pool = FormulaProcessPool(num_workers=1, default_timeout_sec=15, max_tasks=1)
        try:
            # Execute exactly max_tasks=1 task to trigger recycling on the next release
            res = pool.execute(code="result = 'first'", req_id="recycle-1")
            assert res.get("status") == "ok"

            # After release_worker ran, the worker in idle must be alive (re-spawned)
            with pool._cond:
                idle_workers = list(pool._idle)
            assert len(idle_workers) == 1, "Expected exactly one worker back in idle"
            assert idle_workers[0].is_alive(), "Recycled worker must be alive after re-spawn in release_worker"
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

            # Execute an isolated task; worker will recycle because tasks_executed (3) >= max_tasks (2) and no active sessions
            r4 = pool.execute(code="result = 'fresh'", mode="isolated", req_id="sp-4")
            assert r4.get("status") == "ok"

            pid_after = pool.workers[0].process.pid if pool.workers[0].process else None
            assert pid_after != pid_before, "Worker should recycle after session is reset when tasks exceed max_tasks"
        finally:
            pool.shutdown()

    def test_session_ttl_evicts_idle_session(self) -> None:
        """Session TTL reaper must evict sessions idle longer than shared_kernel_ttl_sec."""
        pool = FormulaProcessPool(num_workers=1, default_timeout_sec=15, max_tasks=1, shared_kernel_ttl_sec=3600.0)
        try:
            sid = "ttl-evict-test"
            r1 = pool.execute(code="val = 42\nresult = val", session_id=sid, mode="shared", req_id="ttl-1")
            assert r1.get("status") == "ok"
            assert pool._active_sessions.get(sid) is not None

            # Simulate passage of idle time and trigger eviction
            with pool._lock:
                pool._session_last_activity[sid] = time.monotonic() - 4000.0
            pool._evict_stale_sessions()
            assert pool._active_sessions.get(sid) is None, "Session should be evicted after TTL expiration"

            # After eviction, next task will recycle worker because tasks_executed (1) >= max_tasks (1)
            pid_before = pool.workers[0].process.pid if pool.workers[0].process else None
            r2 = pool.execute(code="result = 'recycled'", mode="isolated", req_id="ttl-2")
            assert r2.get("status") == "ok"
            pid_after = pool.workers[0].process.pid if pool.workers[0].process else None
            assert pid_after != pid_before, "Worker should be recycled after session TTL eviction"
        finally:
            pool.shutdown()

    def test_idle_worker_reaper(self) -> None:
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
            pool._evict_idle_workers()
            assert not worker.is_alive(), "Idle worker process should be killed by idle worker reaper"

            # Subsequent execution lazily re-spawns worker
            res2 = pool.execute(code="result = 456", req_id="idle-2")
            assert res2.get("status") == "ok"
            assert res2.get("result") == 456
            assert worker.is_alive(), "Worker should re-spawn lazily on next request"
        finally:
            pool.shutdown()

    def test_evicted_idle_worker_removed_from_idle_during_kill(self) -> None:
        """Evicted workers must be removed from _idle during kill (race prevention)
        and re-added dead so lease_any() can lazy-respawn them."""
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

            # Worker process must be dead
            assert not w0.is_alive(), "Evicted worker process must be killed"
            # Worker re-added to _idle (dead) so lease_any() can lazy-respawn
            with pool._cond:
                assert w0 in pool._idle, "Dead worker must be back in _idle for lazy re-spawn"
                assert len(pool._idle) == 2, "Both workers should be in _idle"

            # Verify lazy re-spawn works
            res = pool.execute(code="result = 999", req_id="respawn-1")
            assert res.get("status") == "ok"
            assert res.get("result") == 999
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
            max_threads=2,
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

    def _post(self, url: str, payload: dict, headers: dict | None = None) -> tuple[int, dict]:
        req_headers = {"Content-Type": "application/json"}
        if headers:
            req_headers.update(headers)
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(f"{url}/v1/execute", data=data, headers=req_headers, method="POST")
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

    def test_http_inflight_limit_503(self) -> None:
        port = get_free_port()
        settings = ComputeSettings(
            host="127.0.0.1",
            port=port,
            api_key="formula-secret",
            workers=1,
            threads=4,
            max_inflight=1,
        )
        app = create_wsgi_app(settings)
        server = WSGIDualStackServer("127.0.0.1", port, max_threads=4)
        server.set_app(app)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        time.sleep(0.15)
        try:
            holder: list[tuple[int, dict]] = []

            def _slow() -> None:
                holder.append(
                    self._post(
                        f"http://127.0.0.1:{port}",
                        {
                            "id": "hold",
                            "code": "import time\ntime.sleep(1.2)\nresult = 1",
                            "timeout_ms": 5000,
                        },
                        headers={"Authorization": "Bearer formula-secret"},
                    )
                )

            slow = threading.Thread(target=_slow)
            slow.start()
            time.sleep(0.2)
            status, body = self._post(
                f"http://127.0.0.1:{port}",
                {"id": "busy", "code": "result = 2"},
                headers={"Authorization": "Bearer formula-secret"},
            )
            assert status == 503
            assert body.get("code") == "INFLIGHT_LIMIT"
            slow.join(timeout=8)
            assert holder and holder[0][0] == 200
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_execute_shared_session(self, formula_server: str) -> None:
        session_id = "session-http-123"
        status1, body1 = self._post(
            formula_server,
            {"id": "req-s1", "code": "val = 42\nresult = val", "session_id": session_id, "mode": "shared"},
            headers={"Authorization": "Bearer formula-secret"},
        )
        assert status1 == 200
        assert body1.get("result") == 42

        status2, body2 = self._post(
            formula_server,
            {"id": "req-s2", "code": "val += 8\nresult = val", "session_id": session_id, "mode": "shared"},
            headers={"Authorization": "Bearer formula-secret"},
        )
        assert status2 == 200
        assert body2.get("result") == 50
