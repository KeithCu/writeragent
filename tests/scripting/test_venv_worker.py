# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""Tests for venv_worker (paths, warm worker, run_code) via PythonWorkerManager + worker_harness."""

from __future__ import annotations

import io
import os
import pickle
import struct
import subprocess
import sys
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from plugin.scripting.config_limits import WARM_WORKER_TIMEOUT_SEC
from plugin.scripting.ipc import DEFAULT_MAX_PAYLOAD_BYTES, IpcFrameError, pack_pickle_frame, read_pickle_frame
from plugin.scripting.venv_worker import (
    PythonWorkerManager,
    _worker_error,
    _worker_error_message,
    pid_is_alive,
    reset_python_session,
    run_code_in_user_venv,
    scrub_subprocess_env,
    warm_venv_worker,
)
from plugin.scripting.venv.worker_harness import _execute_request, _serialize
from plugin.tests.testing_utils import setup_uno_mocks

setup_uno_mocks()


@pytest.fixture(scope="module", autouse=True)
def _shutdown_workers_after_module():
    """Ensure no leftover worker processes after this file finishes."""
    yield
    PythonWorkerManager.shutdown_all()


def test_worker_error_message_strips_command_path():
    long_cmd = ["/very/long/path/to/python", "/very/long/path/to/worker_harness.py"]
    exc = subprocess.TimeoutExpired(cmd=long_cmd, timeout=3)
    msg = _worker_error_message(exc)
    assert msg == "Python worker failed: timed out after 3 seconds"
    assert "Command" not in msg
    assert "/very/long" not in msg


def test_serialize_numpy_scalar():
    np = pytest.importorskip("numpy")
    assert _serialize(np.int64(7)) == 7


def test_execute_request_fresh_namespace():
    # Without session_id each call gets a new namespace (isolated / default mode).
    r1 = _execute_request("x = 41\nresult = x + 1", None)
    assert r1["status"] == "ok"
    assert r1["result"] == 42
    r2 = _execute_request("result = x + 1", None)
    assert r2["status"] == "error"


def test_execute_request_injects_data():
    r = _execute_request("result = float(np.sum(data))", [[1, 2, 3, 4]])
    assert r["status"] == "ok"
    assert r["result"] == 10.0


def test_execute_request_1x1_data_arithmetic_dag():
    # Issue #412: 1x1 data in consumer formula participates directly in arithmetic
    r1 = _execute_request("result = data + 3", [[2]])
    assert r1["status"] == "ok"
    assert r1["result"] == 5

    r2 = _execute_request("result = data * 4", [[5.0]])
    assert r2["status"] == "ok"
    assert r2["result"] == 20.0


def test_execute_request_1x1_data_serialize_unwraps_to_scalar():
    # Issue #412: result = data on a 1x1 range serializes as a scalar, not [[2]]
    r = _execute_request("result = data", [[2]])
    assert r["status"] == "ok"
    assert r["result"] == 2
    assert not isinstance(r["result"], list)


def test_execute_request_fan_out_dag_returns_scalars():
    # Fan-out DAG (C2.4.3): multiple cells reading the same producer
    r1 = _execute_request("result = data", [[2]])
    r2 = _execute_request("result = data", [[2]])
    assert r1["status"] == "ok" and r1["result"] == 2
    assert r2["status"] == "ok" and r2["result"] == 2


def test_execute_request_injects_ranges_single_range():
    r = _execute_request(
        "result = (len(ranges), data is ranges[0], hasattr(data, 'to_pandas'))",
        [[1, 2, 3]],
    )
    assert r["status"] == "ok"
    assert r["result"] == [1, True, True]


def test_execute_request_injects_ranges_multi_polymorphic_data():
    from plugin.calc.calc_addin_data import pack_calc_multi_data_for_wire

    wire = pack_calc_multi_data_for_wire([[[1.0, 2.0, 3.0]], [[4.0, 5.0]]], force="never")
    r = _execute_request(
        "result = (len(ranges), data is ranges, data[1].values[0][0])",
        wire,
    )
    assert r["status"] == "ok"
    assert r["result"] == [2, True, 4.0]


def test_execute_request_does_not_inject_inputs():
    # LocalPythonExecutor raises InterpreterError (not NameError) for missing names.
    r = _execute_request("result = inputs", [[1]])
    assert r["status"] == "error"
    assert "inputs" in r.get("message", "").lower() and "not defined" in r.get("message", "").lower()


def test_blocked_import_os():
    r = _execute_request("import os\nresult = 1", None)
    assert r["status"] == "error"
    assert "not allowed" in r.get("message", "").lower() or "Import" in r.get("message", "")


def test_blocked_import_not_on_allowlist():
    pytest.importorskip("requests")
    r = _execute_request("import requests\nresult = 1", None)
    assert r["status"] == "error"
    assert "not allowed" in r.get("message", "").lower() or "Import" in r.get("message", "")


def test_sentence_transformers_import_not_deep_wrapped():
    """Heavy embedder packages must bypass get_safe_module scanning (hangs on dir()/getattr)."""
    st = pytest.importorskip("sentence_transformers")
    from plugin.contrib.smolagents.local_python_executor import get_safe_module

    assert get_safe_module(st, []) is st
    r = _execute_request(
        "from sentence_transformers import SentenceTransformer\nresult = str(SentenceTransformer)",
        None,
    )
    assert r["status"] == "ok"
    assert "SentenceTransformer" in r["result"]


def test_duckdb_import_not_deep_wrapped():
    """DuckDB (C-backed analytics lib) must bypass get_safe_module like other heavy packages."""
    duck = pytest.importorskip("duckdb")
    from plugin.contrib.smolagents.local_python_executor import get_safe_module

    assert get_safe_module(duck, []) is duck
    # Simple execution test to ensure import + basic use works inside the sandbox
    r = _execute_request(
        "import duckdb\ncon = duckdb.connect()\nresult = con.execute('SELECT 42 AS x').df().to_dict()",
        None,
    )
    assert r["status"] == "ok"
    assert "x" in str(r.get("result", "")) or 42 in str(r.get("result", ""))


def test_harness_main_loop_integration():
    """Harness reads and writes Pickle (subprocess smoke)."""
    harness = __import__("plugin.scripting.venv.worker_harness", fromlist=["main"])

    proc_pickle = subprocess.Popen(
        [sys.executable, harness.__file__],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=False,
        bufsize=0,
    )
    req_dict = {"id": "t2", "code": "result = 2 ** 10"}
    proc_pickle.stdin.write(pack_pickle_frame(req_dict))
    proc_pickle.stdin.flush()

    resp_dict = read_pickle_frame(proc_pickle.stdout, require_dict=True)
    assert resp_dict is not None
    assert resp_dict["id"] == "t2"
    assert resp_dict["status"] == "ok"
    assert resp_dict["result"] == 1024

    proc_pickle.stdin.close()
    proc_pickle.wait(timeout=5)



def test_manager_real_spawn_drains_stderr_flood(tmp_path, monkeypatch):
    """The manager's actual Popen path must drain stderr before waiting for a response."""
    import plugin.scripting.venv_worker as venv_worker_module

    child = tmp_path / "stderr_flood_worker.py"
    child.write_text(
        """
import pickle
import struct
import sys

sys.stderr.buffer.write(b"x" * (128 * 1024))
sys.stderr.buffer.flush()

while True:
    header = sys.stdin.buffer.read(4)
    if len(header) < 4:
        break
    size = struct.unpack("!I", header)[0]
    payload = sys.stdin.buffer.read(size)
    if len(payload) < size:
        break
    request = pickle.loads(payload)
    response = {"id": request.get("id"), "status": "ok", "result": 42, "stdout": ""}
    encoded = pickle.dumps(response, protocol=5)
    sys.stdout.buffer.write(struct.pack("!I", len(encoded)) + encoded)
    sys.stdout.buffer.flush()
""".lstrip(),
        encoding="utf-8",
    )
    monkeypatch.setattr(venv_worker_module, "_HARNESS_PATH", str(child))
    monkeypatch.setattr(venv_worker_module, "wrap_command_for_sandbox", lambda cmd: cmd)

    mgr = PythonWorkerManager(sys.executable, {"PATH": os.environ.get("PATH", "")})
    drain = None
    try:
        result = mgr.execute("result = 42", timeout_sec=5)
        drain = mgr._stderr_drain
        assert result["status"] == "ok"
        assert result["result"] == 42
        assert drain is not None
        # optimize_popen_pipes raises Linux stderr toward 1 MiB, so the child
        # can write 128 KiB and reply before the drain thread is scheduled.
        # execute() returning already proves we did not deadlock on the pipe.
        # Wait for the drain to catch that flood (CI failed with text() == "").
        deadline = time.monotonic() + 2.0
        captured = ""
        while time.monotonic() < deadline:
            captured = drain.text()
            if "x" * 1024 in captured:
                break
            time.sleep(0.01)
        assert "x" * 1024 in captured
    finally:
        mgr._terminate_worker()
    assert drain is not None
    assert not drain.is_alive


def test_worker_crash_mid_request_retries_and_recovers(tmp_path, monkeypatch):
    """A child that dies once mid-IPC must be recycled; the retried turn should succeed."""
    import plugin.scripting.venv_worker as venv_worker_module

    crash_once = tmp_path / "crash_once"
    crash_once.write_text("1", encoding="utf-8")
    child = tmp_path / "crash_once_worker.py"
    child.write_text(
        f"""
import os
import pickle
import struct
import sys

flag = {str(crash_once)!r}

while True:
    header = sys.stdin.buffer.read(4)
    if len(header) < 4:
        break
    size = struct.unpack("!I", header)[0]
    payload = sys.stdin.buffer.read(size)
    if len(payload) < size:
        break
    if os.path.exists(flag):
        os.remove(flag)
        os._exit(1)
    request = pickle.loads(payload)
    response = {{"id": request.get("id"), "status": "ok", "result": 42, "stdout": ""}}
    encoded = pickle.dumps(response, protocol=5)
    sys.stdout.buffer.write(struct.pack("!I", len(encoded)) + encoded)
    sys.stdout.buffer.flush()
""".lstrip(),
        encoding="utf-8",
    )
    monkeypatch.setattr(venv_worker_module, "_HARNESS_PATH", str(child))
    monkeypatch.setattr(venv_worker_module, "wrap_command_for_sandbox", lambda cmd: cmd)

    mgr = PythonWorkerManager(sys.executable, {"PATH": os.environ.get("PATH", "")})
    try:
        result = mgr.execute("result = 42", timeout_sec=5)
        assert result["status"] == "ok"
        assert result["result"] == 42
        assert mgr._proc is not None and mgr._proc.poll() is None
    finally:
        mgr._terminate_worker()


def test_pid_is_alive_current_process():
    assert pid_is_alive(os.getpid())
    assert not pid_is_alive(0)
    assert not pid_is_alive(-1)


def test_terminate_worker_kills_grandchild(tmp_path, monkeypatch):
    """Timeout/crash cleanup must kill descendants (joblib/loky, DataLoader), not only the worker."""
    import plugin.scripting.venv_worker as venv_worker_module

    child = tmp_path / "tree_worker.py"
    child.write_text(
        """
import subprocess
import sys
import time

grandchild = subprocess.Popen(
    [sys.executable, "-c", "import time; time.sleep(120)"],
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
sys.stderr.write(f"GRANDCHILD {grandchild.pid}\\n")
sys.stderr.flush()
time.sleep(120)
""".lstrip(),
        encoding="utf-8",
    )
    monkeypatch.setattr(venv_worker_module, "_HARNESS_PATH", str(child))
    monkeypatch.setattr(venv_worker_module, "wrap_command_for_sandbox", lambda cmd: cmd)

    mgr = PythonWorkerManager(sys.executable, {"PATH": os.environ.get("PATH", "")})
    gpid = None
    try:
        mgr._ensure_running()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            drain = mgr._stderr_drain
            text = drain.text() if drain is not None else ""
            for line in text.splitlines():
                if line.startswith("GRANDCHILD "):
                    gpid = int(line.split()[1])
                    break
            if gpid is not None:
                break
            time.sleep(0.05)
        assert gpid is not None, "worker did not report grandchild pid"
        assert pid_is_alive(gpid)
        mgr._terminate_worker()
        dead_deadline = time.monotonic() + 5
        while time.monotonic() < dead_deadline and pid_is_alive(gpid):
            time.sleep(0.05)
        assert not pid_is_alive(gpid), f"grandchild pid {gpid} survived worker termination"
    finally:
        mgr._terminate_worker()
        if gpid is not None and pid_is_alive(gpid):
            try:
                os.kill(gpid, 9)
            except OSError:
                pass


def test_blocked_stdin_write_times_out_and_releases_lock(tmp_path, monkeypatch):
    """A child that never reads stdin must not hold the manager's pool lock forever."""
    import plugin.scripting.venv_worker as venv_worker_module

    child = tmp_path / "blocked_stdin_worker.py"
    child.write_text("import time\ntime.sleep(60)\n", encoding="utf-8")
    monkeypatch.setattr(venv_worker_module, "_HARNESS_PATH", str(child))
    monkeypatch.setattr(venv_worker_module, "wrap_command_for_sandbox", lambda cmd: cmd)

    mgr = PythonWorkerManager(sys.executable, {"PATH": os.environ.get("PATH", "")})
    try:
        mgr._ensure_running()
        assert mgr._proc is not None and mgr._proc.stdin is not None
        started = time.monotonic()
        with mgr._io_lock:
            with pytest.raises(subprocess.TimeoutExpired):
                mgr._write_bytes_with_timeout(
                    mgr._proc.stdin,
                    b"x" * (8 * 1024 * 1024),
                    timeout_sec=0.2,
                    label="test request",
                )
        assert time.monotonic() - started < 5
        assert mgr._proc is None
        assert mgr._stdin_writer_thread is not None
        assert not mgr._stdin_writer_thread.is_alive()
        assert mgr._io_lock.acquire(timeout=1)
        mgr._io_lock.release()
    finally:
        mgr._terminate_worker()


def test_large_stdin_write_completes_intact():
    mgr = PythonWorkerManager(sys.executable, {})
    stream = io.BytesIO()
    payload = b"large-payload-" * (256 * 1024)
    mgr._write_bytes_with_timeout(stream, payload, timeout_sec=2, label="test request")
    assert stream.getvalue() == payload
    assert mgr._stdin_writer_thread is not None
    assert not mgr._stdin_writer_thread.is_alive()


def test_initial_write_timeout_retries_once():
    mgr = PythonWorkerManager(sys.executable, {})
    proc = MagicMock()
    proc.poll.return_value = None
    proc.stdin = io.BytesIO()
    proc.stdout = io.BytesIO()
    mgr._proc = proc
    mgr._ensure_running = MagicMock()  # type: ignore[method-assign]
    mgr._write_frame_with_timeout = MagicMock(  # type: ignore[method-assign]
        side_effect=subprocess.TimeoutExpired(cmd=sys.executable, timeout=1)
    )
    mgr._terminate_worker = MagicMock()  # type: ignore[method-assign]

    result = mgr._execute_ipc_unlocked("result = 1", timeout_sec=1)

    assert result["status"] == "error"
    assert "timed out after 1 seconds" in result["message"]
    assert mgr._write_frame_with_timeout.call_count == 2
    assert mgr._terminate_worker.call_count == 2


def test_ppt_master_write_timeout_does_not_replay(monkeypatch):
    import plugin.scripting.venv_worker as venv_worker_module

    mgr = PythonWorkerManager(sys.executable, {})
    proc = MagicMock()
    proc.poll.return_value = None
    proc.stdin = io.BytesIO()
    proc.stdout = io.BytesIO()
    mgr._proc = proc
    mgr._ensure_running = MagicMock()  # type: ignore[method-assign]
    mgr._write_frame_with_timeout = MagicMock()  # type: ignore[method-assign]
    mgr._write_bytes_with_timeout = MagicMock(  # type: ignore[method-assign]
        side_effect=subprocess.TimeoutExpired(cmd=sys.executable, timeout=1)
    )
    response_payload = pickle.dumps({"status": "host_request"}, protocol=5)
    mgr._read_response_bytes = MagicMock(return_value=response_payload)  # type: ignore[method-assign]
    mgr._terminate_worker = MagicMock()  # type: ignore[method-assign]
    dispatch_calls = []

    def dispatch(response, *, stdin_write, on_worker_event=None, stop_checker=None):
        del response, on_worker_event, stop_checker
        dispatch_calls.append(True)
        stdin_write(b"host response")
        return True

    monkeypatch.setattr(venv_worker_module, "_maybe_dispatch_ppt_master_response", dispatch)

    result = mgr._execute_ipc_unlocked("result = 1", timeout_sec=1)

    assert result["status"] == "error"
    assert "host RPC response timed out" in result["message"]
    assert len(dispatch_calls) == 1
    assert mgr._write_frame_with_timeout.call_count == 1
    assert mgr._read_response_bytes.call_count == 1
    assert mgr._terminate_worker.call_count == 1


def test_host_read_timeout_does_not_retry():
    """Hung user code must not be replayed; that would double the configured timeout."""
    mgr = PythonWorkerManager(sys.executable, {})
    proc = MagicMock()
    proc.poll.return_value = None
    proc.stdin = io.BytesIO()
    proc.stdout = io.BytesIO()
    mgr._proc = proc
    mgr._ensure_running = MagicMock()  # type: ignore[method-assign]
    mgr._write_frame_with_timeout = MagicMock()  # type: ignore[method-assign]
    mgr._read_response_bytes = MagicMock(  # type: ignore[method-assign]
        side_effect=subprocess.TimeoutExpired(cmd=sys.executable, timeout=1)
    )
    mgr._terminate_worker = MagicMock()  # type: ignore[method-assign]

    result = mgr._execute_ipc_unlocked("result = 1", timeout_sec=1)

    assert result["status"] == "error"
    assert "timed out after 1 seconds" in result["message"]
    assert mgr._write_frame_with_timeout.call_count == 1
    assert mgr._read_response_bytes.call_count == 1
    assert mgr._terminate_worker.call_count == 1


def test_kill_process_tree_win32_uses_taskkill(monkeypatch):
    """Windows must kill grandchildren; TerminateProcess on the worker PID is not enough."""
    import plugin.scripting.venv_worker as venv_worker_module

    run = MagicMock()
    monkeypatch.setattr(venv_worker_module.subprocess, "run", run)

    proc = MagicMock()
    proc.poll.return_value = 1
    proc.pid = 4242
    venv_worker_module._kill_process_tree_win32(proc)
    run.assert_called_once()
    assert run.call_args[0][0] == ["taskkill", "/F", "/T", "/PID", "4242"]
    proc.kill.assert_not_called()


def test_manager_separate_pools_same_exe():
    from plugin.framework.constants import WORKER_POOL_DEFAULT, WORKER_POOL_EMBEDDINGS

    PythonWorkerManager.shutdown_all()
    env = {"PATH": "/usr/bin:/bin"}
    default_mgr = PythonWorkerManager.get(sys.executable, env, pool=WORKER_POOL_DEFAULT)
    embed_mgr = PythonWorkerManager.get(sys.executable, env, pool=WORKER_POOL_EMBEDDINGS)
    assert default_mgr is not embed_mgr
    assert default_mgr is PythonWorkerManager.get(sys.executable, env, pool=WORKER_POOL_DEFAULT)
    PythonWorkerManager.shutdown_all()


def test_split_grid_data_round_trip_execute_request():
    """Ingress split_grid: child receives CalcRange backed by numeric values."""
    pytest.importorskip("numpy")
    from plugin.calc.calc_addin_data import pack_calc_data_for_wire
    from plugin.scripting.payload_codec import BINARY_MIN_CELLS, is_split_grid
    from plugin.scripting.calc_range import is_calc_range_payload
    from tests.scripting.payload_codec_test_support import NUMERIC_AT_THRESHOLD, sequential_grid_sum

    grid = NUMERIC_AT_THRESHOLD
    wire = pack_calc_data_for_wire(grid)
    assert is_calc_range_payload(wire)
    assert is_split_grid(wire["data"])
    r = _execute_request("result = float(np.sum(data))", wire)
    assert r["status"] == "ok"
    assert r["result"] == pytest.approx(sequential_grid_sum(BINARY_MIN_CELLS))


def test_normalize_response_unpacks_split_grid():
    from plugin.scripting.payload_codec import host_pack_split_grid, is_split_grid

    grid = [[float(r * 10 + c) for c in range(5)] for r in range(5)]
    wire = host_pack_split_grid(grid)
    assert is_split_grid(wire)
    mgr = PythonWorkerManager(sys.executable, {"PATH": "/usr/bin:/bin"})
    out = mgr._normalize_response({"status": "ok", "result": wire, "stdout": ""})
    assert out["status"] == "ok"
    assert not is_split_grid(out["result"])
    assert len(out["result"]) == 5
    assert out["result"][0][0] == pytest.approx(0.0)
    assert out["result"][4][4] == pytest.approx(44.0)


def test_automatic_imports_math():
    r = _execute_request("result = math.sqrt(16)", None)
    assert r["status"] == "ok"
    assert r["result"] == 4.0


def test_automatic_imports_numpy():
    pytest.importorskip("numpy")
    r = _execute_request("result = float(np.sum([1, 2, 3]))", None)
    assert r["status"] == "ok"
    assert r["result"] == 6.0


def test_automatic_imports_sympy():
    pytest.importorskip("sympy")
    r = _execute_request("result = str(sp.Symbol('x'))", None)
    assert r["status"] == "ok"
    assert r["result"] == "x"


def test_automatic_imports_already_imported():
    r = _execute_request("import math as my_math\nresult = my_math.sqrt(16)", None)
    assert r["status"] == "ok"
    assert r["result"] == 4.0


def test_automatic_imports_explicit():
    r = _execute_request("import math\nresult = math.sqrt(25)", None)
    assert r["status"] == "ok"
    assert r["result"] == 5.0


@patch("plugin.scripting.venv_worker.configured_python_exec_timeout", return_value=10)
@patch("plugin.scripting.venv_worker.get_config_str", return_value="")
@patch("plugin.scripting.venv_worker.resolve_libreoffice_python", return_value=sys.executable)
@patch("plugin.scripting.venv_worker.PythonWorkerManager.execute")
def test_run_venv_code_timeout_capped(mock_execute, mock_lo_python, mock_cfg, mock_configured_timeout):
    ctx = MagicMock()

    # Call with no timeout and verify it gets default timeout of 10s
    run_code_in_user_venv(ctx, "result = 1")
    mock_execute.assert_called_once_with(
        "result = 1",
        data=None,
        bindings=None,
        timeout_sec=10,
        session_id=None,
        init_script=None,
        init_session_id=None,
        init_script_hash=None,
        allow_heartbeat=False,
        heartbeat_grace_sec=None,
        on_heartbeat=None,
        action=None,
        python_tool_domain=None,
    )

    mock_execute.reset_mock()

    # Call with a custom timeout in the allowed range (e.g. 100s) and verify it is allowed
    run_code_in_user_venv(ctx, "result = 1", timeout_sec=100)
    mock_execute.assert_called_once_with(
        "result = 1",
        data=None,
        bindings=None,
        timeout_sec=100,
        session_id=None,
        init_script=None,
        init_session_id=None,
        init_script_hash=None,
        allow_heartbeat=False,
        heartbeat_grace_sec=None,
        on_heartbeat=None,
        action=None,
        python_tool_domain=None,
    )

    mock_execute.reset_mock()

    # Call with a timeout exceeding 600s (e.g. 1000s) and verify it gets capped to 600s
    run_code_in_user_venv(ctx, "result = 1", timeout_sec=1000)
    mock_execute.assert_called_once_with(
        "result = 1",
        data=None,
        bindings=None,
        timeout_sec=600,
        session_id=None,
        init_script=None,
        init_session_id=None,
        init_script_hash=None,
        allow_heartbeat=False,
        heartbeat_grace_sec=None,
        on_heartbeat=None,
        action=None,
        python_tool_domain=None,
    )

    mock_execute.reset_mock()

    # Call with 0s timeout and verify it gets set to 1s floor
    run_code_in_user_venv(ctx, "result = 1", timeout_sec=0)
    mock_execute.assert_called_once_with(
        "result = 1",
        data=None,
        bindings=None,
        timeout_sec=1,
        session_id=None,
        init_script=None,
        init_session_id=None,
        init_script_hash=None,
        allow_heartbeat=False,
        heartbeat_grace_sec=None,
        on_heartbeat=None,
        action=None,
        python_tool_domain=None,
    )


def test_split_grid_pickle_and_json_round_trip():
    """Regression: production buffer path vs historical Base64 JSON split_grid."""
    from plugin.scripting.payload_codec import is_split_grid
    from tests.scripting.payload_codec_test_support import (
        child_pack_split_grid,
        child_unpack_split_grid,
        host_pack_split_grid,
        host_unpack_split_grid,
        legacy_b64_child_pack_split_grid,
        legacy_b64_child_unpack_split_grid,
        legacy_b64_host_pack_split_grid,
        legacy_b64_host_unpack_split_grid,
    )

    np = pytest.importorskip("numpy")
    grid = [[float(r * 10 + c) for c in range(4)] for r in range(4)]

    wire_json = legacy_b64_host_pack_split_grid(grid)
    assert is_split_grid(wire_json)
    assert "b64" in wire_json
    assert "buffer" not in wire_json
    assert isinstance(wire_json["b64"], str)
    # Host unpacks
    unpacked_host_json = legacy_b64_host_unpack_split_grid(wire_json)
    assert unpacked_host_json == grid
    unpacked_child_json = legacy_b64_child_unpack_split_grid(wire_json)
    assert isinstance(unpacked_child_json, np.ndarray)
    assert unpacked_child_json.shape == (4, 4)
    np.testing.assert_allclose(unpacked_child_json, np.array(grid))

    # 2. Test production binary mode
    wire_pickle = host_pack_split_grid(grid)
    assert is_split_grid(wire_pickle)
    assert "buffer" in wire_pickle
    assert "b64" not in wire_pickle
    assert isinstance(wire_pickle["buffer"], bytes)
    # Host unpacks
    unpacked_host_pickle = host_unpack_split_grid(wire_pickle)
    assert unpacked_host_pickle == grid
    # Child unpacks
    unpacked_child_pickle = child_unpack_split_grid(wire_pickle)
    assert isinstance(unpacked_child_pickle, np.ndarray)
    assert unpacked_child_pickle.shape == (4, 4)
    np.testing.assert_allclose(unpacked_child_pickle, np.array(grid))

    # 3. Test child pack with Base64 via local helper
    child_wire_json = legacy_b64_child_pack_split_grid(np.array(grid))
    assert is_split_grid(child_wire_json)
    assert "b64" in child_wire_json
    assert "buffer" not in child_wire_json
    # Host unpacks
    unpacked_host_json_from_child = legacy_b64_host_unpack_split_grid(child_wire_json)
    assert unpacked_host_json_from_child == grid

    # 4. Test production child pack
    child_wire_pickle = child_pack_split_grid(np.array(grid))
    assert is_split_grid(child_wire_pickle)
    assert "buffer" in child_wire_pickle
    assert "b64" not in child_wire_pickle
    # Host unpacks
    unpacked_host_pickle_from_child = host_unpack_split_grid(child_wire_pickle)
    assert unpacked_host_pickle_from_child == grid


def test_warm_spawns_and_primes_worker():
    """warm() makes the next execute instant by pre-spawning the process and triggering auto-imports."""
    PythonWorkerManager.shutdown_all()
    mgr = PythonWorkerManager.get(sys.executable, {"PATH": "/usr/bin:/bin"})
    assert mgr._proc is None
    mgr.warm()
    assert mgr._proc is not None and mgr._proc.poll() is None
    assert mgr._primed is True
    r = mgr.execute("result = 42")
    assert r["status"] == "ok"
    assert r["result"] == 42
    PythonWorkerManager.shutdown_all()


def test_cold_execute_warms_with_separate_timeout():
    """First execute primes the worker under WARM_WORKER_TIMEOUT_SEC, then runs user code at configured timeout."""
    from plugin.scripting.config_limits import HOST_IPC_READ_GRACE_SEC

    PythonWorkerManager.shutdown_all()
    mgr = PythonWorkerManager.get(sys.executable, {"PATH": "/usr/bin:/bin"})
    timeouts: list[float | int] = []
    original_read = mgr._read_response_bytes

    def record_read(stdout, timeout_sec):
        timeouts.append(timeout_sec)
        return original_read(stdout, timeout_sec)

    mgr._read_response_bytes = record_read  # type: ignore[method-assign]
    try:
        r = mgr.execute("result = 42", timeout_sec=3)
        assert r["status"] == "ok"
        assert r["result"] == 42
        assert timeouts == [WARM_WORKER_TIMEOUT_SEC + HOST_IPC_READ_GRACE_SEC, 3 + HOST_IPC_READ_GRACE_SEC]
    finally:
        PythonWorkerManager.shutdown_all()


def test_warm_execute_uses_configured_timeout_only():
    """After priming, execute sends one IPC round at the configured timeout."""
    from plugin.scripting.config_limits import HOST_IPC_READ_GRACE_SEC

    PythonWorkerManager.shutdown_all()
    mgr = PythonWorkerManager.get(sys.executable, {"PATH": "/usr/bin:/bin"})
    mgr.warm()
    timeouts: list[float | int] = []
    original_read = mgr._read_response_bytes

    def record_read(stdout, timeout_sec):
        timeouts.append(timeout_sec)
        return original_read(stdout, timeout_sec)

    mgr._read_response_bytes = record_read  # type: ignore[method-assign]
    try:
        r = mgr.execute("result = 7", timeout_sec=3)
        assert r["status"] == "ok"
        assert r["result"] == 7
        assert timeouts == [3 + HOST_IPC_READ_GRACE_SEC]
    finally:
        PythonWorkerManager.shutdown_all()


def test_terminate_worker_re_primes_on_next_execute():
    """After worker kill, the next execute runs warm again before user code."""
    from plugin.scripting.config_limits import HOST_IPC_READ_GRACE_SEC

    PythonWorkerManager.shutdown_all()
    mgr = PythonWorkerManager.get(sys.executable, {"PATH": "/usr/bin:/bin"})
    mgr.warm()
    mgr._terminate_worker()
    timeouts: list[float | int] = []
    original_read = mgr._read_response_bytes

    def record_read(stdout, timeout_sec):
        timeouts.append(timeout_sec)
        return original_read(stdout, timeout_sec)

    mgr._read_response_bytes = record_read  # type: ignore[method-assign]
    try:
        r = mgr.execute("result = 99", timeout_sec=3)
        assert r["status"] == "ok"
        assert r["result"] == 99
        assert timeouts == [WARM_WORKER_TIMEOUT_SEC + HOST_IPC_READ_GRACE_SEC, 3 + HOST_IPC_READ_GRACE_SEC]
    finally:
        PythonWorkerManager.shutdown_all()


@patch("plugin.scripting.venv_worker.get_config_str", return_value="")
@patch("plugin.scripting.venv_worker.resolve_libreoffice_python", return_value=sys.executable)
def test_warm_venv_worker_resolves_and_warms(mock_lo_python, mock_cfg):

    PythonWorkerManager.shutdown_all()
    ctx = MagicMock()
    warm_venv_worker(ctx)
    mgr = PythonWorkerManager.get(sys.executable, scrub_subprocess_env({"PATH": "/usr/bin:/bin"}))
    assert mgr._proc is not None and mgr._proc.poll() is None
    PythonWorkerManager.shutdown_all()


class TestLiveWorkerReuse:
    """Contiguous live-worker tests sharing one warmed manager (after lifecycle tests above)."""

    @pytest.fixture(scope="class")
    def warmed_worker_manager(self):
        PythonWorkerManager.shutdown_all()
        mgr = PythonWorkerManager.get(sys.executable, {"PATH": "/usr/bin:/bin"})
        mgr.warm()
        yield mgr
        PythonWorkerManager.shutdown_all()

    @patch("plugin.scripting.venv_worker.get_config_str", return_value="")
    @patch("plugin.scripting.venv_worker.resolve_libreoffice_python", return_value=sys.executable)
    def test_run_code_uses_manager(self, mock_lo_python, mock_cfg, warmed_worker_manager):
        del warmed_worker_manager
        ctx = MagicMock()
        r1 = run_code_in_user_venv(ctx, "result = 100")
        assert r1["status"] == "ok"
        assert r1["result"] == 100
        r2 = run_code_in_user_venv(ctx, "result = nope + 1")
        assert r2["status"] == "error"

    def test_manager_two_calls_same_process(self, warmed_worker_manager):
        mgr = warmed_worker_manager
        r1 = mgr.execute("result = 1")
        assert r1["status"] == "ok"
        pid1 = mgr._proc.pid if mgr._proc else None
        r2 = mgr.execute("result = 2")
        assert r2["status"] == "ok"
        pid2 = mgr._proc.pid if mgr._proc else None
        assert pid1 is not None and pid1 == pid2
        r3 = mgr.execute("result = prev")
        assert r3["status"] == "error"

    def test_split_grid_result_round_trip_manager(self, warmed_worker_manager):
        """API responses unpack split_grid so LLM/UI never see wire envelopes."""
        pytest.importorskip("numpy")
        mgr = warmed_worker_manager
        r = mgr.execute("import numpy as np\nresult = np.arange(16, dtype=np.float64).reshape(4, 4)")
        assert r["status"] == "ok"
        from plugin.scripting.payload_codec import is_split_grid

        assert not is_split_grid(r["result"])
        assert len(r["result"]) == 4
        assert r["result"][0][0] == pytest.approx(0.0)
        assert r["result"][3][3] == pytest.approx(15.0)

    def test_manager_unpacks_prime_tuple_list(self, warmed_worker_manager):
        """List-of-tuples large enough for split_grid on wire must return nested lists to callers."""
        pytest.importorskip("sympy")
        mgr = warmed_worker_manager
        code = "result = [(i, int(sp.prime(i))) for i in range(100, 107)]"
        r = mgr.execute(code)
        assert r["status"] == "ok"
        from plugin.scripting.payload_codec import is_split_grid

        assert not is_split_grid(r["result"])
        assert r["result"] == [[100, 541], [101, 547], [102, 557], [103, 563], [104, 569], [105, 571], [106, 577]]
        assert all(isinstance(cell, int) for row in r["result"] for cell in row)

    def test_split_grid_integration_pickle_mode(self, warmed_worker_manager):
        pytest.importorskip("numpy")
        mgr = warmed_worker_manager

        r = mgr.execute("import numpy as np\nresult = np.arange(100, dtype=np.float64).reshape(10, 10)")
        assert r["status"] == "ok"

        assert len(r["result"]) == 10
        assert r["result"][0][0] == 0.0
        assert r["result"][9][9] == 99.0

        large_grid = [[float(r * 10 + c) for c in range(10)] for r in range(10)]
        from plugin.calc.calc_addin_data import pack_calc_data_for_wire

        r2 = mgr.execute("result = float(np.sum(data))", data=pack_calc_data_for_wire(large_grid))
        assert r2["status"] == "ok"
        assert r2["result"] == pytest.approx(sum(r * 10 + c for r in range(10) for c in range(10)))


def _pack_response(obj: dict) -> bytes:
    """Encode a response the same way worker_harness.py does."""
    return pack_pickle_frame(obj)


class TestReadResponseBytesThreaded:
    """Tests for the Windows-safe threaded reader."""

    def test_reads_valid_response(self):
        response = {"status": "ok", "result": 42, "id": "test"}
        raw = _pack_response(response)
        stdout = io.BytesIO(raw)
        mgr = PythonWorkerManager.__new__(PythonWorkerManager)
        mgr.exe = "python"
        mgr._proc = True  # just needs to be non-None for the assert
        got = mgr._read_response_bytes_threaded(stdout, timeout_sec=5)
        assert got
        decoded = pickle.loads(got)
        assert decoded["status"] == "ok"
        assert decoded["result"] == 42

    def test_returns_empty_on_eof(self):
        stdout = io.BytesIO(b"")
        mgr = PythonWorkerManager.__new__(PythonWorkerManager)
        mgr.exe = "python"
        mgr._proc = True
        got = mgr._read_response_bytes_threaded(stdout, timeout_sec=2)
        assert got == b""

    def test_returns_empty_on_short_header(self):
        stdout = io.BytesIO(b"\x00\x00")
        mgr = PythonWorkerManager.__new__(PythonWorkerManager)
        mgr.exe = "python"
        mgr._proc = True
        got = mgr._read_response_bytes_threaded(stdout, timeout_sec=2)
        assert got == b""

    def test_returns_empty_on_truncated_payload(self):
        header = struct.pack("!I", 100)
        stdout = io.BytesIO(header + b"short")
        mgr = PythonWorkerManager.__new__(PythonWorkerManager)
        mgr.exe = "python"
        mgr._proc = True
        got = mgr._read_response_bytes_threaded(stdout, timeout_sec=2)
        assert got == b""

    def test_timeout_raises(self):
        """A blocking read that never yields data should raise TimeoutExpired."""
        class SlowIO(io.RawIOBase):
            def readable(self):
                return True
            def readinto(self, b):
                time.sleep(10)
                return 0
        slow = io.BufferedReader(SlowIO())
        mgr = PythonWorkerManager.__new__(PythonWorkerManager)
        mgr.exe = "python"
        mgr._proc = True
        with pytest.raises(subprocess.TimeoutExpired):
            mgr._read_response_bytes_threaded(slow, timeout_sec=1)

    def test_propagates_read_error(self):
        class ErrorIO(io.RawIOBase):
            def readable(self):
                return True
            def readinto(self, b):
                raise IOError("pipe broken")
        broken = io.BufferedReader(ErrorIO())
        mgr = PythonWorkerManager.__new__(PythonWorkerManager)
        mgr.exe = "python"
        mgr._proc = True
        with pytest.raises(IOError, match="pipe broken"):
            mgr._read_response_bytes_threaded(broken, timeout_sec=2)


@pytest.mark.skipif(os.name == "nt", reason="select.select() does not support pipes/BytesIO on Windows")
class TestReadResponseBytesSelect:
    """Tests for the POSIX select-based reader."""

    def test_reads_valid_response(self):
        response = {"status": "ok", "result": "hello", "id": "test"}
        raw = _pack_response(response)
        r_fd, w_fd = os.pipe()
        os.write(w_fd, raw)
        os.close(w_fd)
        stdout = os.fdopen(r_fd, "rb")
        mgr = PythonWorkerManager.__new__(PythonWorkerManager)
        mgr.exe = "python"
        # _read_response_bytes_select needs _proc with a poll() method
        class FakeProc:
            def poll(self):
                return None
        mgr._proc = FakeProc()
        got = mgr._read_response_bytes_select(stdout, timeout_sec=5)
        stdout.close()
        assert got
        decoded = pickle.loads(got)
        assert decoded["result"] == "hello"


class TestExecuteOSErrorRetry:
    """Verify that OSError in the execute loop triggers retry instead of propagation."""

    def test_oserror_retried(self):
        mgr = PythonWorkerManager.__new__(PythonWorkerManager)
        mgr.exe = "python"
        mgr._proc = None
        mgr._io_lock = threading.Lock()
        mgr._primed = False
        mgr.env = {}

        call_count = [0]

        def fake_ensure():
            call_count[0] += 1
            raise OSError("[WinError 10038] not a socket")

        mgr._ensure_warmed_unlocked = lambda: None
        mgr._ensure_running = fake_ensure
        mgr._terminate_worker = lambda: None
        result = mgr.execute("result = 1", timeout_sec=1)
        assert result["status"] == "error"
        assert "10038" in result["message"]
        assert call_count[0] == 2  # retried once


def test_maybe_dispatch_ppt_master_skips_when_module_missing(monkeypatch):
    import builtins

    from plugin.scripting.venv_worker import _maybe_dispatch_ppt_master_response

    real_import = builtins.__import__

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "plugin.ppt_master.venv.host_rpc" or (
            name == "plugin.ppt_master" and fromlist and "venv" in fromlist
        ):
            raise ImportError(f"No module named {name!r}")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    assert (
        _maybe_dispatch_ppt_master_response(
            {"status": "ok", "result": 2},
            stdin_write=MagicMock(),
        )
        is False
    )


def test_shared_session_persists_after_soft_timeout():
    """Soft in-process timeout must return an error without terminating the worker or shared session."""
    import plugin.scripting.venv_worker as vw

    mgr = PythonWorkerManager(sys.executable, {"PATH": os.environ.get("PATH", "")})
    sid = "test-shared-timeout-session"
    try:
        r1 = mgr.execute("x = 12345\nresult = x", session_id=sid)
        assert r1["status"] == "ok"
        assert r1["result"] == 12345

        # sleep, not "while True: pass": if SIGALRM is unavailable the executor
        # falls back to ThreadPoolExecutor, whose shutdown waits for the thread.
        # A tight loop never finishes, so the host hits the 9s read timeout and
        # kills the worker (CI: r3 status error). sleep(5) is interrupted by
        # SIGALRM, or finishes within the 8s grace on the thread fallback.
        with patch.object(vw, "HOST_IPC_READ_GRACE_SEC", 8.0):
            r2 = mgr.execute("import time\ntime.sleep(5)", session_id=sid, timeout_sec=1)
        assert r2["status"] == "error"
        assert "execution time" in r2.get("message", "").lower() or "timed out" in r2.get("message", "").lower()

        # Shared kernel namespace must still retain x from step 1
        r3 = mgr.execute("result = x + 1", session_id=sid)
        assert r3["status"] == "ok"
        assert r3["result"] == 12346
    finally:
        mgr._terminate_worker()


def test_session_executor_updates_timeout_seconds():
    """_get_or_create_session_executor updates timeout_seconds on an existing session."""
    from plugin.scripting.venv.venv_sandbox import (
        _get_or_create_session_executor,
        reset_sandbox_session,
    )

    sid = "test-dynamic-timeout-session"
    try:
        exec1 = _get_or_create_session_executor(sid, timeout_sec=10)
        assert exec1.timeout_seconds == 10

        exec2 = _get_or_create_session_executor(sid, timeout_sec=3)
        assert exec2 is exec1
        assert exec2.timeout_seconds == 3
    finally:
        reset_sandbox_session(sid)


def test_venv_worker_error_codes_and_context():
    """Verify venv worker returns structured error codes and context on failure."""
    mgr = PythonWorkerManager(sys.executable, {"PATH": os.environ.get("PATH", "")})
    try:
        # 1. Syntax/Execution error returns VENV_EXEC_ERROR code and traceback
        res = mgr.execute("1 / 0")
        assert res["status"] == "error"
        assert res.get("code") == "VENV_EXEC_ERROR"
        assert "ZeroDivisionError" in res.get("message", "")
        assert "traceback" in res

        # 2. Timeout error code
        res_to = mgr.execute("import time\ntime.sleep(5)", timeout_sec=1)
        assert res_to["status"] == "error"
        assert res_to.get("code") in ("VENV_TIMEOUT", "VENV_EXEC_ERROR")
    finally:
        mgr._terminate_worker()


def test_worker_error_shape():
    err = _worker_error("WORKER_IPC_ERROR", "No code provided.")
    assert err == {
        "status": "error",
        "code": "WORKER_IPC_ERROR",
        "message": "No code provided.",
        "details": {},
    }


def test_run_code_and_reset_session_missing_inputs_include_code():
    empty_code = run_code_in_user_venv(MagicMock(), "   ")
    assert empty_code["status"] == "error"
    assert empty_code["code"] == "WORKER_IPC_ERROR"
    assert empty_code["message"] == "No code provided."
    assert "details" in empty_code

    empty_sid = reset_python_session(MagicMock(), "  ")
    assert empty_sid["status"] == "error"
    assert empty_sid["code"] == "WORKER_IPC_ERROR"
    assert empty_sid["message"] == "No session_id provided."
    assert "details" in empty_sid


def test_worker_read_rejects_oversize_length_prefix():
    mgr = PythonWorkerManager(sys.executable, {"PATH": os.environ.get("PATH", "")})
    too_big = struct.pack("!I", DEFAULT_MAX_PAYLOAD_BYTES + 1)
    with pytest.raises(IpcFrameError, match="venv worker frame"):
        mgr._read_frame_bytes(io.BytesIO(too_big), read_exact=lambda n: too_big[:n])


def test_maybe_dispatch_tool_call_without_ppt_master(monkeypatch):
    """tool_call must round-trip even when ppt-master is not bundled."""
    import builtins

    from plugin.scripting.venv_worker import _maybe_dispatch_intermediate_response

    real_import = builtins.__import__

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "plugin.ppt_master.venv.host_rpc" or (
            name == "plugin.ppt_master" and fromlist and "venv" in fromlist
        ):
            raise ImportError(f"No module named {name!r}")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    written: list[bytes] = []
    with patch("plugin.scripting.host_rpc.execute_tool", return_value={"ok": True}) as mock_tool:
        handled = _maybe_dispatch_intermediate_response(
            {"type": "tool_call", "id": "t1", "tool": "apply_document_content", "args": {"content": ["x"]}},
            stdin_write=written.append,
        )
    assert handled is True
    mock_tool.assert_called_once_with(
        "apply_document_content",
        {"content": ["x"]},
        caller="script",
        allowed_tools=None,
    )
    assert len(written) == 1
    resp = read_pickle_frame(io.BytesIO(written[0]), require_dict=True)
    assert resp is not None
    assert resp["status"] == "ok"
    assert resp["id"] == "t1"


def test_python_worker_manager_sets_is_worker_env():
    mgr = PythonWorkerManager(sys.executable, {"PATH": "/usr/bin"})
    assert mgr.env.get("WRITERAGENT_IS_WORKER") == "1"


