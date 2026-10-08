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
import logging
import os
import pickle
import signal
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
    reset_python_session,
    run_code_in_user_venv,
    scrub_subprocess_env,
    warm_venv_worker,
)
from plugin.scripting.venv.venv_sandbox import run_sandboxed_code, serialize_result


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
    assert serialize_result(np.int64(7)) == 7


def test_execute_request_fresh_namespace():
    # Without session_id each call gets a new namespace (isolated / default mode).
    r1 = run_sandboxed_code("x = 41\nresult = x + 1", None)
    assert r1["status"] == "ok"
    assert r1["result"] == 42
    r2 = run_sandboxed_code("result = x + 1", None)
    assert r2["status"] == "error"


def test_execute_request_injects_data():
    r = run_sandboxed_code("result = float(np.sum(data))", [[1, 2, 3, 4]])
    assert r["status"] == "ok"
    assert r["result"] == 10.0


def test_execute_request_1x1_data_arithmetic_dag():
    # Issue #412: 1x1 data in consumer formula participates directly in arithmetic
    r1 = run_sandboxed_code("result = data + 3", [[2]])
    assert r1["status"] == "ok"
    assert r1["result"] == 5

    r2 = run_sandboxed_code("result = data * 4", [[5.0]])
    assert r2["status"] == "ok"
    assert r2["result"] == 20.0


def test_execute_request_1x1_data_serialize_unwraps_to_scalar():
    # Issue #412: result = data on a 1x1 range serializes as a scalar, not [[2]]
    r = run_sandboxed_code("result = data", [[2]])
    assert r["status"] == "ok"
    assert r["result"] == 2
    assert not isinstance(r["result"], list)


def test_execute_request_fan_out_dag_returns_scalars():
    # Fan-out DAG (C2.4.3): multiple cells reading the same producer
    r1 = run_sandboxed_code("result = data", [[2]])
    r2 = run_sandboxed_code("result = data", [[2]])
    assert r1["status"] == "ok" and r1["result"] == 2
    assert r2["status"] == "ok" and r2["result"] == 2


def test_execute_request_injects_ranges_single_range():
    r = run_sandboxed_code(
        "result = (len(ranges), data is ranges[0], hasattr(data, 'to_pandas'))",
        [[1, 2, 3]],
    )
    assert r["status"] == "ok"
    assert r["result"] == [1, True, True]


def test_execute_request_injects_ranges_multi_polymorphic_data():
    from plugin.calc.calc_addin_data import pack_calc_multi_data_for_wire

    wire = pack_calc_multi_data_for_wire([[[1.0, 2.0, 3.0]], [[4.0, 5.0]]], force="never")
    r = run_sandboxed_code(
        "result = (len(ranges), data is ranges, data[1].values[0][0])",
        wire,
    )
    assert r["status"] == "ok"
    assert r["result"] == [2, True, 4.0]


def test_execute_request_does_not_inject_inputs():
    # LocalPythonExecutor raises InterpreterError (not NameError) for missing names.
    r = run_sandboxed_code("result = inputs", [[1]])
    assert r["status"] == "error"
    assert "inputs" in r.get("message", "").lower() and "not defined" in r.get("message", "").lower()


def test_run_code_in_user_venv_passes_stop_checker():
    """run_code_in_user_venv must pass stop_checker to the manager's execute method."""
    with patch("plugin.scripting.venv_worker._worker_manager_for_ctx") as mock_mgr_ctx:
        mock_mgr = MagicMock()
        mock_mgr.execute.return_value = {"status": "ok"}
        mock_mgr_ctx.return_value = (mock_mgr, None)

        ctx = MagicMock()

        def stop_fn() -> bool:
            return True

        run_code_in_user_venv(ctx, code="result = 1", stop_checker=stop_fn)

        mock_mgr.execute.assert_called_once()
        kwargs = mock_mgr.execute.call_args.kwargs
        assert kwargs.get("stop_checker") is stop_fn


def test_blocked_import_os():
    r = run_sandboxed_code("import os\nresult = 1", None)
    assert r["status"] == "error"
    assert "not allowed" in r.get("message", "").lower() or "Import" in r.get("message", "")


def test_blocked_import_not_on_allowlist():
    pytest.importorskip("requests")
    r = run_sandboxed_code("import requests\nresult = 1", None)
    assert r["status"] == "error"
    assert "not allowed" in r.get("message", "").lower() or "Import" in r.get("message", "")


def test_sentence_transformers_import_not_deep_wrapped():
    """Heavy embedder packages must bypass get_safe_module scanning (hangs on dir()/getattr)."""
    st = pytest.importorskip("sentence_transformers")
    from plugin.contrib.smolagents.local_python_executor import get_safe_module

    assert get_safe_module(st, []) is st
    r = run_sandboxed_code(
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
    r = run_sandboxed_code(
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

    started = read_pickle_frame(proc_pickle.stdout, require_dict=True)
    assert started is not None
    assert started["type"] == "exec_started"
    assert started["id"] == "t2"
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


def pid_is_alive(pid: int) -> bool:
    """True if *pid* still names a live process.

    ``os.kill(pid, 0)`` is POSIX. On Windows signal 0 is WinError 87, so a
    naive helper treated every live grandchild as dead (CI 33453184665).
    Production kill code does not call this; the grandchild tests do.
    """
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

    # PROCESS_QUERY_LIMITED_INFORMATION: exists-check without PROCESS_ALL_ACCESS.
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.OpenProcess(0x1000, False, pid)
    if handle:
        kernel32.CloseHandle(handle)
        return True
    # ACCESS_DENIED (5): process exists but this token cannot query it.
    return ctypes.get_last_error() == 5


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


def test_execute_refuses_reentry_while_this_thread_owns_the_pipe():
    mgr = PythonWorkerManager(sys.executable, {})
    mgr._io_owner = threading.get_ident()
    result = mgr.execute("result = 1", timeout_sec=1)
    assert result["status"] == "error"
    assert result["code"] == "WORKER_REENTRY"
    assert not mgr._io_lock.locked()


def test_large_stdin_write_completes_intact():
    mgr = PythonWorkerManager(sys.executable, {})
    stream = io.BytesIO()
    payload = b"large-payload-" * (256 * 1024)
    mgr._write_bytes_with_timeout(stream, payload, timeout_sec=2, label="test request")
    assert stream.getvalue() == payload
    assert mgr._stdin_writer_thread is None


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

    def dispatch(response, *, stdin_write, on_worker_event=None, stop_checker=None, cancellation_scope=None):
        del response, on_worker_event, stop_checker, cancellation_scope
        dispatch_calls.append(True)
        stdin_write(b"host response")
        return True

    monkeypatch.setattr(venv_worker_module, "_maybe_dispatch_ppt_master_response", dispatch)

    result = mgr._execute_ipc_unlocked("result = 1", timeout_sec=1, caller="ppt_master_venv")

    assert result["status"] == "error"
    assert "host RPC response timed out" in result["message"]
    assert len(dispatch_calls) == 1
    assert mgr._write_frame_with_timeout.call_count == 1
    assert mgr._read_response_bytes.call_count == 1
    assert mgr._terminate_worker.call_count == 1


def test_tool_call_then_broken_stdout_does_not_replay(monkeypatch):
    """A tool_call that already ran must not be followed by a second copy of the script."""
    import plugin.scripting.venv_worker as venv_worker_module

    mgr = PythonWorkerManager(sys.executable, {})
    proc = MagicMock()
    proc.poll.return_value = None
    proc.stdin = io.BytesIO()
    proc.stdout = io.BytesIO()
    mgr._proc = proc
    mgr._ensure_running = MagicMock()  # type: ignore[method-assign]
    mgr._write_frame_with_timeout = MagicMock()  # type: ignore[method-assign]
    response_payload = pickle.dumps({"status": "host_request"}, protocol=5)
    mgr._read_response_bytes = MagicMock(  # type: ignore[method-assign]
        side_effect=[response_payload, RuntimeError("Worker closed stdout")]
    )
    mgr._terminate_worker = MagicMock()  # type: ignore[method-assign]

    def dispatch(response, *, stdin_write, on_worker_event=None, stop_checker=None, cancellation_scope=None):
        del response, stdin_write, on_worker_event, stop_checker, cancellation_scope
        return True

    monkeypatch.setattr(venv_worker_module, "_maybe_dispatch_ppt_master_response", dispatch)

    result = mgr._execute_ipc_unlocked("result = 1", timeout_sec=1, caller="ppt_master_venv")

    assert result["status"] == "error"
    assert mgr._write_frame_with_timeout.call_count == 1
    assert mgr._terminate_worker.call_count == 1


def test_stdout_close_before_start_retries_once():
    """A death before exec_started has not run the request; recycle the child."""
    mgr = PythonWorkerManager(sys.executable, {})
    proc = MagicMock()
    proc.poll.return_value = None
    proc.stdin = io.BytesIO()
    proc.stdout = io.BytesIO()
    mgr._proc = proc
    mgr._ensure_running = MagicMock()  # type: ignore[method-assign]
    mgr._write_frame_with_timeout = MagicMock()  # type: ignore[method-assign]
    mgr._read_response_bytes = MagicMock(return_value=b"")  # type: ignore[method-assign]
    mgr._drain_stderr = MagicMock(return_value="")  # type: ignore[method-assign]
    mgr._terminate_worker = MagicMock()  # type: ignore[method-assign]

    result = mgr._execute_ipc_unlocked("result = 1", timeout_sec=1)

    assert result["status"] == "error"
    assert "without a response" in result["message"]
    assert mgr._write_frame_with_timeout.call_count == 2
    assert mgr._terminate_worker.call_count == 2


def test_runtime_error_before_exec_started_retries_once():
    """A RuntimeError before the child has started the script is still a failed start."""
    mgr = PythonWorkerManager(sys.executable, {})
    proc = MagicMock()
    proc.poll.return_value = None
    proc.stdin = io.BytesIO()
    proc.stdout = io.BytesIO()
    mgr._proc = proc
    mgr._ensure_running = MagicMock()  # type: ignore[method-assign]
    mgr._write_frame_with_timeout = MagicMock()  # type: ignore[method-assign]
    mgr._read_response_bytes = MagicMock(side_effect=RuntimeError("pipe died"))  # type: ignore[method-assign]
    mgr._terminate_worker = MagicMock()  # type: ignore[method-assign]

    result = mgr._execute_ipc_unlocked("result = 1", timeout_sec=1)

    assert result["status"] == "error"
    assert "pipe died" in result["message"]
    assert mgr._write_frame_with_timeout.call_count == 2
    assert mgr._terminate_worker.call_count == 2


def test_runtime_error_after_exec_started_does_not_replay():
    """A RuntimeError after exec_started must not resend the script on a new child."""
    mgr = PythonWorkerManager(sys.executable, {})
    proc = MagicMock()
    proc.poll.return_value = None
    proc.stdin = io.BytesIO()
    proc.stdout = io.BytesIO()
    mgr._proc = proc
    mgr._ensure_running = MagicMock()  # type: ignore[method-assign]
    captured: dict[str, dict] = {}

    def _write_frame(stdin, request, **kwargs):
        captured["request"] = request

    mgr._write_frame_with_timeout = _write_frame  # type: ignore[method-assign]
    reads = {"n": 0}

    def _read(stdout, timeout_sec, stop_checker=None):
        reads["n"] += 1
        request = captured["request"]
        if reads["n"] == 1:
            return pickle.dumps({"type": "exec_started", "id": request["id"]}, protocol=5)
        raise RuntimeError("worker frame was not a dict")

    mgr._read_response_bytes = _read  # type: ignore[method-assign]
    mgr._terminate_worker = MagicMock()  # type: ignore[method-assign]

    result = mgr._execute_ipc_unlocked("result = 1", timeout_sec=1)

    assert result["status"] == "error"
    assert result["code"] == "WORKER_IPC_ERROR"
    assert "not a dict" in result["message"]
    assert reads["n"] == 2
    assert mgr._terminate_worker.call_count == 1


def test_stdout_close_after_exec_started_does_not_replay():
    """In-process side effects after exec_started must not run under the same id again."""
    mgr = PythonWorkerManager(sys.executable, {})
    proc = MagicMock()
    proc.poll.return_value = None
    proc.stdin = io.BytesIO()
    proc.stdout = io.BytesIO()
    mgr._proc = proc
    mgr._ensure_running = MagicMock()  # type: ignore[method-assign]
    captured: dict[str, dict] = {}

    def _write_frame(stdin, request, **kwargs):
        captured["request"] = request

    mgr._write_frame_with_timeout = _write_frame  # type: ignore[method-assign]
    reads = {"n": 0}

    def _read(stdout, timeout_sec, stop_checker=None):
        reads["n"] += 1
        request = captured["request"]
        if reads["n"] == 1:
            return pickle.dumps({"type": "exec_started", "id": request["id"]}, protocol=5)
        return b""

    mgr._read_response_bytes = _read  # type: ignore[method-assign]
    mgr._drain_stderr = MagicMock(return_value="")  # type: ignore[method-assign]
    mgr._terminate_worker = MagicMock()  # type: ignore[method-assign]

    result = mgr._execute_ipc_unlocked("result = 1", timeout_sec=1)

    assert result["status"] == "error"
    assert result["code"] == "WORKER_IPC_ERROR"
    assert "without a response" in result["message"]
    assert "request" in captured
    assert reads["n"] == 2
    assert mgr._terminate_worker.call_count == 1


def test_exec_started_wrong_id_is_refused_without_replay():
    """An exec_started for another request is an id mismatch: kill, no resend."""
    mgr = PythonWorkerManager(sys.executable, {})
    proc = MagicMock()
    proc.poll.return_value = None
    proc.stdin = io.BytesIO()
    proc.stdout = io.BytesIO()
    mgr._proc = proc
    mgr._ensure_running = MagicMock()  # type: ignore[method-assign]
    mgr._write_frame_with_timeout = MagicMock()  # type: ignore[method-assign]
    mgr._read_response_bytes = MagicMock(  # type: ignore[method-assign]
        return_value=pickle.dumps({"type": "exec_started", "id": "someone-else"}, protocol=5)
    )
    mgr._terminate_worker = MagicMock()  # type: ignore[method-assign]

    result = mgr._execute_ipc_unlocked("result = 1", timeout_sec=1)

    assert result["code"] == "WORKER_IPC_ERROR"
    assert "id mismatch" in result["message"]
    assert mgr._write_frame_with_timeout.call_count == 1
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
    r = run_sandboxed_code("result = float(np.sum(data))", wire)
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


@pytest.mark.parametrize(
    "value, expected",
    [
        pytest.param("result = math.sqrt(16)", 4.0, id="test_automatic_imports_math"),
        pytest.param("import math as my_math\nresult = my_math.sqrt(16)", 4.0, id="test_automatic_imports_already_imported"),
        pytest.param("import math\nresult = math.sqrt(25)", 5.0, id="test_automatic_imports_explicit"),
    ],
)
def test_automatic_imports_math(value, expected):
    r = run_sandboxed_code(value, None)
    assert r["status"] == "ok"
    assert r["result"] == expected


@pytest.mark.parametrize(
    "value, value_2, expected",
    [
        pytest.param("numpy", "result = float(np.sum([1, 2, 3]))", 6.0, id="test_automatic_imports_numpy"),
        pytest.param("sympy", "result = str(sp.Symbol('x'))", "x", id="test_automatic_imports_sympy"),
    ],
)
def test_automatic_imports_numpy(value, value_2, expected):
    pytest.importorskip(value)
    r = run_sandboxed_code(value_2, None)
    assert r["status"] == "ok"
    assert r["result"] == expected

def test_build_request_sets_heartbeat_on_trusted_action():
    mgr = PythonWorkerManager.__new__(PythonWorkerManager)
    request = mgr._build_request(action="run_trusted_action", allow_heartbeat=True, data={"domain": "embeddings_index"})
    assert request["allow_heartbeat"] is True
    assert request["action"] == "run_trusted_action"
    assert request["data"]["domain"] == "embeddings_index"


@pytest.mark.parametrize("exc_type", [ValueError, OverflowError, TypeError])
def test_bad_result_after_tool_call_does_not_replay(monkeypatch, exc_type):
    """Unpack failures after a finished tool call must not resend the script.

    ValueError was already caught. OverflowError (int(inf) on an int column)
    and TypeError are on the same host-unpack contract and used to escape.
    """
    import plugin.scripting.venv_worker as venv_worker_module

    mgr = PythonWorkerManager(sys.executable, {})
    proc = MagicMock()
    proc.poll.return_value = None
    proc.stdin = io.BytesIO()
    proc.stdout = io.BytesIO()
    mgr._proc = proc
    mgr._ensure_running = MagicMock()  # type: ignore[method-assign]
    captured: dict[str, dict] = {}

    def _write_frame(stdin, request, **kwargs):
        captured["request"] = request

    mgr._write_frame_with_timeout = _write_frame  # type: ignore[method-assign]
    reads = {"n": 0}

    def _read(stdout, timeout_sec, stop_checker=None):
        reads["n"] += 1
        if reads["n"] == 1:
            return pickle.dumps({"status": "host_request"}, protocol=5)
        request = captured["request"]
        return pickle.dumps(
            {"status": "ok", "id": request["id"], "result": {"split_grid": "bad"}},
            protocol=5,
        )

    mgr._read_response_bytes = _read  # type: ignore[method-assign]
    mgr._terminate_worker = MagicMock()  # type: ignore[method-assign]

    def _normalize(response):
        raise exc_type("inconsistent split_grid")

    mgr._normalize_response = _normalize  # type: ignore[method-assign]

    dispatched = {"n": 0}

    def dispatch(response, *, stdin_write, on_worker_event=None, stop_checker=None, **kwargs):
        del response, stdin_write, on_worker_event, stop_checker, kwargs
        dispatched["n"] += 1
        # The first frame is the tool call. The next frame is the finished result.
        return dispatched["n"] == 1

    monkeypatch.setattr(venv_worker_module, "_maybe_dispatch_intermediate_response", dispatch)

    result = mgr._execute_ipc_unlocked("result = 1", timeout_sec=1)

    assert result["status"] == "error"
    assert result["code"] == "WORKER_IPC_ERROR"
    assert "inconsistent split_grid" in result["message"]
    assert "request" in captured
    assert reads["n"] == 2
    assert mgr._terminate_worker.call_count == 1


def test_get_replaces_previous_interpreter_in_the_same_pool():
    from plugin.framework.constants import WORKER_POOL_DEFAULT
    from plugin.scripting import venv_worker

    PythonWorkerManager.shutdown_all()
    first = PythonWorkerManager.get("/tmp/python-a", {"PATH": "/usr/bin"})
    second = PythonWorkerManager.get("/tmp/python-b", {"PATH": "/usr/bin"}, pool=WORKER_POOL_DEFAULT)
    try:
        assert first is not second
        assert PythonWorkerManager.get("/tmp/python-b", {"PATH": "/usr/bin"}) is second
        assert all(not key.endswith("python-a") for key in venv_worker._instances)
    finally:
        PythonWorkerManager.shutdown_all()


def test_get_refreshes_env_for_the_next_spawn():
    PythonWorkerManager.shutdown_all()
    try:
        first = PythonWorkerManager.get("/tmp/python-env", {"PATH": "/usr/bin", "A": "1"})
        second = PythonWorkerManager.get("/tmp/python-env", {"PATH": "/usr/bin", "A": "1", "B": "2"})
        assert first is second
        assert second.env["B"] == "2"
        assert second.env["WRITERAGENT_IS_WORKER"] == "1"
    finally:
        PythonWorkerManager.shutdown_all()


def test_retired_worker_does_not_respawn():
    from plugin.framework.constants import WORKER_POOL_DEFAULT

    PythonWorkerManager.shutdown_all()
    seen: list[bool] = []
    real_terminate = PythonWorkerManager._terminate_worker

    def _spy(self):
        seen.append(self._retired)
        return real_terminate(self)

    try:
        with patch.object(PythonWorkerManager, "_terminate_worker", _spy):
            first = PythonWorkerManager.get(
                "/tmp/python-old-path", {"PATH": "/usr/bin"}, pool=WORKER_POOL_DEFAULT
            )
            PythonWorkerManager.get("/tmp/python-new-path", {"PATH": "/usr/bin"}, pool=WORKER_POOL_DEFAULT)
        assert first._retired is True
        assert True in seen
        with pytest.raises(RuntimeError, match="will not be restarted"):
            first._ensure_running()
        assert first._proc is None
        assert PythonWorkerManager.pool_is_running("pool-that-was-never-started") is False
    finally:
        PythonWorkerManager.shutdown_all()


def test_shutdown_all_sets_retired_before_terminate():
    from plugin.scripting import venv_worker as vw

    mgr = vw.PythonWorkerManager.__new__(vw.PythonWorkerManager)
    mgr._retired = False
    mgr._proc = None
    calls = []
    mgr._terminate_worker = lambda: calls.append("term")
    key = "test-pool:shutdown-retire"
    with vw._registry_lock:
        previous = dict(vw._instances)
        vw._instances[key] = mgr
    try:
        vw.PythonWorkerManager.shutdown_all()
    finally:
        with vw._registry_lock:
            vw._instances.clear()
            vw._instances.update(previous)
    assert mgr._retired is True
    assert calls == ["term"]


def test_worker_harness_dies_with_parent(monkeypatch):
    import ctypes

    import plugin.scripting.venv.worker_harness as harness

    calls: list[tuple] = []

    class _Libc:
        def prctl(self, *args):
            calls.append(args)
            return 0

    monkeypatch.setattr(harness.sys, "platform", "linux")
    monkeypatch.setattr(ctypes, "CDLL", lambda *args, **kwargs: _Libc())
    monkeypatch.setattr(harness.os, "getppid", lambda: 50)
    harness._die_with_parent()
    assert calls == [(1, 9, 0, 0, 0)]

    killed: list[int] = []
    ppids = [50, 1]
    monkeypatch.setattr(harness.os, "getppid", lambda: ppids.pop(0))
    monkeypatch.setattr(harness.os, "getpid", lambda: 123)
    monkeypatch.setattr(harness.os, "kill", lambda pid, sig: killed.append(sig))
    harness._die_with_parent()
    assert killed == [9]

    # Under subreaper: ppid changes from 50 to 999 (not 1)
    killed.clear()
    subreaper_ppids = [50, 999]
    monkeypatch.setattr(harness.os, "getppid", lambda: subreaper_ppids.pop(0))
    harness._die_with_parent()
    assert killed == [9]

    monkeypatch.setattr(harness.sys, "platform", "win32")
    calls.clear()
    harness._die_with_parent()
    assert calls == []


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
        script_session_id=None,
        stop_checker=None,
        cancellation_scope=None,
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
        script_session_id=None,
        stop_checker=None,
        cancellation_scope=None,
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
        script_session_id=None,
        stop_checker=None,
        cancellation_scope=None,
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
        script_session_id=None,
        stop_checker=None,
        cancellation_scope=None,
    )


def test_run_code_forwards_script_session_pin_apart_from_kernel_session():
    ctx = MagicMock()
    with (
        patch("plugin.scripting.venv_worker.configured_python_exec_timeout", return_value=10),
        patch("plugin.scripting.venv_worker.get_config_str", return_value=""),
        patch("plugin.scripting.venv_worker.resolve_libreoffice_python", return_value=sys.executable),
        patch("plugin.scripting.venv_worker.PythonWorkerManager.execute") as mock_execute,
    ):
        mock_execute.return_value = {"status": "ok", "result": 1}
        run_code_in_user_venv(ctx, "result = 1", script_session_id="doc:pin")
    assert mock_execute.call_args.kwargs["script_session_id"] == "doc:pin"
    assert mock_execute.call_args.kwargs["session_id"] is None


def test_host_script_session_id_prefers_pin():
    from plugin.scripting.venv_worker import host_script_session_id

    assert host_script_session_id("rps:file:///a.odg", "doc:pin") == "doc:pin"
    assert host_script_session_id("rps:file:///a.odg", None) == "rps:file:///a.odg"
    assert host_script_session_id("rps:file:///a.odg", "  ") == "rps:file:///a.odg"
    assert host_script_session_id(None, None) is None
    assert host_script_session_id("  ", "doc:pin") == "doc:pin"


def _ipc_one_tool_call(*, session_id, script_session_id):
    """Drive one tool_call frame through the host without a real worker."""
    mgr = PythonWorkerManager(sys.executable, {})
    proc = MagicMock()
    proc.poll.return_value = None
    proc.stdin = io.BytesIO()
    proc.stdout = io.BytesIO()
    mgr._proc = proc
    mgr._ensure_running = MagicMock()  # type: ignore[method-assign]
    captured: dict[str, dict] = {}

    def _write_frame(stdin, request, **kwargs):
        del stdin, kwargs
        captured["request"] = request

    mgr._write_frame_with_timeout = _write_frame  # type: ignore[method-assign]
    reads = {"n": 0}

    def _read(stdout, timeout_sec, stop_checker=None):
        del stdout, timeout_sec
        reads["n"] += 1
        request = captured["request"]
        if reads["n"] == 1:
            return pickle.dumps(
                {"type": "tool_call", "id": "c1", "tool": "shape_upsert", "args": {"action": "create"}},
                protocol=5,
            )
        return pickle.dumps({"status": "ok", "id": request["id"], "result": 1}, protocol=5)

    mgr._read_response_bytes = _read  # type: ignore[method-assign]
    with patch("plugin.scripting.host_rpc.execute_tool", return_value={"status": "ok"}) as mock_tool:
        result = mgr._execute_ipc_unlocked(
            "result = 1",
            timeout_sec=1,
            session_id=session_id,
            script_session_id=script_session_id,
        )
    return captured["request"], mock_tool.call_args, result


def test_pinned_script_session_reaches_tool_rpc_without_kernel_session():
    request, call, result = _ipc_one_tool_call(session_id=None, script_session_id="doc:pinned-deck")
    assert result["status"] == "ok"
    assert "session_id" not in request
    assert call.kwargs["script_session_id"] == "doc:pinned-deck"


def test_request_session_id_still_reaches_tool_rpc_without_pin():
    request, call, result = _ipc_one_tool_call(session_id="rps:file:///deck.odg", script_session_id=None)
    assert result["status"] == "ok"
    assert request["session_id"] == "rps:file:///deck.odg"
    assert call.kwargs["script_session_id"] == "rps:file:///deck.odg"


def test_execute_forwards_script_session_id_not_as_kernel_session():
    mgr = PythonWorkerManager(sys.executable, {})
    with (
        patch.object(mgr, "_acquire_io", return_value=None),
        patch.object(mgr, "_release_io"),
        patch.object(mgr, "_ensure_warmed_unlocked", return_value=None),
        patch.object(mgr, "_execute_ipc_unlocked", return_value={"status": "ok"}) as ipc,
    ):
        mgr.execute("result = 1", script_session_id="doc:pin")
    assert ipc.call_args.kwargs["script_session_id"] == "doc:pin"
    assert ipc.call_args.kwargs["session_id"] is None


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


def test_warm_logs_prime_failure(caplog: pytest.LogCaptureFixture) -> None:
    mgr = PythonWorkerManager("/tmp/python-warm-fail", {"PATH": "/usr/bin"})
    mgr._ensure_warmed = lambda: {"status": "error", "message": "prime failed"}  # type: ignore[method-assign]
    with caplog.at_level(logging.WARNING, logger="plugin.scripting.venv_worker"):
        mgr.warm()
    assert "prime failed" in caplog.text


def test_retired_execute_does_not_clear_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """A replaced manager must not kill a worker or wipe the Calc scalar cache."""
    mgr = PythonWorkerManager("/tmp/python-retired", {"PATH": "/usr/bin"})
    mgr._retired = True
    terminated: list[bool] = []
    cleared: list[bool] = []
    monkeypatch.setattr(mgr, "_terminate_worker", lambda: terminated.append(True))

    def _clear() -> None:
        cleared.append(True)

    monkeypatch.setattr("plugin.calc.python.function.clear_python_addin_cache", _clear)
    result = mgr.execute("result = 1", timeout_sec=5)
    assert result["status"] == "error"
    assert result["code"] == "WORKER_IPC_ERROR"
    assert "will not be restarted" in result["message"]
    assert terminated == []
    assert cleared == []


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

    def record_read(stdout, timeout_sec, stop_checker=None):
        timeouts.append(timeout_sec)
        return original_read(stdout, timeout_sec)

    mgr._read_response_bytes = record_read  # type: ignore[method-assign]
    try:
        r = mgr.execute("result = 42", timeout_sec=3)
        assert r["status"] == "ok"
        assert r["result"] == 42
        # exec_started, then the prime result, then exec_started, then user code.
        grace = HOST_IPC_READ_GRACE_SEC
        assert timeouts == [
            WARM_WORKER_TIMEOUT_SEC + grace,
            WARM_WORKER_TIMEOUT_SEC + grace,
            3 + grace,
            3 + grace,
        ]
    finally:
        PythonWorkerManager.shutdown_all()


def test_warm_execute_uses_configured_timeout_only():
    """After priming, execute reads exec_started and the result at the configured timeout."""
    from plugin.scripting.config_limits import HOST_IPC_READ_GRACE_SEC

    PythonWorkerManager.shutdown_all()
    mgr = PythonWorkerManager.get(sys.executable, {"PATH": "/usr/bin:/bin"})
    mgr.warm()
    timeouts: list[float | int] = []
    original_read = mgr._read_response_bytes

    def record_read(stdout, timeout_sec, stop_checker=None):
        timeouts.append(timeout_sec)
        return original_read(stdout, timeout_sec)

    mgr._read_response_bytes = record_read  # type: ignore[method-assign]
    try:
        r = mgr.execute("result = 7", timeout_sec=3)
        assert r["status"] == "ok"
        assert r["result"] == 7
        grace = HOST_IPC_READ_GRACE_SEC
        assert timeouts == [3 + grace, 3 + grace]
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

    def record_read(stdout, timeout_sec, stop_checker=None):
        timeouts.append(timeout_sec)
        return original_read(stdout, timeout_sec)

    mgr._read_response_bytes = record_read  # type: ignore[method-assign]
    try:
        r = mgr.execute("result = 99", timeout_sec=3)
        assert r["status"] == "ok"
        assert r["result"] == 99
        grace = HOST_IPC_READ_GRACE_SEC
        assert timeouts == [
            WARM_WORKER_TIMEOUT_SEC + grace,
            WARM_WORKER_TIMEOUT_SEC + grace,
            3 + grace,
            3 + grace,
        ]
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

    @pytest.mark.parametrize(
        "value",
        [
            pytest.param(b"", id="test_returns_empty_on_eof"),
            pytest.param(b"\x00\x00", id="test_returns_empty_on_short_header"),
        ],
    )
    def test_returns_empty_on_eof(self, value):
        stdout = io.BytesIO(value)
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
        mgr._io_owner = None
        mgr._serving_tool_call = False
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


def test_init_and_cell_share_one_timeout_without_killing_worker():
    """Init and the cell used to each get a full alarm, so the host read killed the worker."""
    import plugin.scripting.venv_worker as vw

    mgr = PythonWorkerManager(sys.executable, {"PATH": os.environ.get("PATH", "")})
    sid = "test-shared-budget-session"
    try:
        with patch.object(vw, "HOST_IPC_READ_GRACE_SEC", 8.0):
            slow = mgr.execute(
                "import time\ntime.sleep(2)\nresult = 1",
                session_id=sid,
                timeout_sec=3,
                init_script="import time\ntime.sleep(2)\n",
                init_session_id=sid + ":init",
                init_script_hash="budget-v1",
            )
        assert slow["status"] == "error"
        assert "execution time" in slow.get("message", "").lower() or "timed out" in slow.get("message", "").lower()
        assert mgr._proc is not None and mgr._proc.poll() is None
        follow = mgr.execute("result = 7", session_id=sid)
        assert follow["status"] == "ok"
        assert follow["result"] == 7
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
        script_session_id=None,
    )
    assert len(written) == 1
    resp = read_pickle_frame(io.BytesIO(written[0]), require_dict=True)
    assert resp is not None
    assert resp["status"] == "ok"
    assert resp["id"] == "t1"


def test_python_worker_manager_sets_is_worker_env():
    mgr = PythonWorkerManager(sys.executable, {"PATH": "/usr/bin"})
    assert mgr.env.get("WRITERAGENT_IS_WORKER") == "1"


def test_acquire_io_ui_thread_returns_busy_when_lock_held(monkeypatch):
    """The UI thread must not block on the pipe lock."""
    monkeypatch.setattr("plugin.framework.thread_guard.on_main_thread", lambda: True)
    mgr = PythonWorkerManager(sys.executable, {})
    assert mgr._io_lock.acquire(timeout=1)
    try:
        started = time.monotonic()
        result = mgr._acquire_io()
        assert time.monotonic() - started < 1.0
        assert result is not None
        assert result["status"] == "error"
        assert result["code"] == "WORKER_REENTRY"
        assert "busy" in result["message"]
        assert mgr._io_owner is None
    finally:
        mgr._io_lock.release()


def test_acquire_io_refuses_other_thread_during_tool_rpc():
    """A tool RPC may be waiting on this thread; do not block in acquire()."""
    mgr = PythonWorkerManager(sys.executable, {})
    mgr._serving_tool_call = True
    mgr._io_owner = threading.get_ident() + 1
    started = time.monotonic()
    result = mgr._acquire_io()
    assert time.monotonic() - started < 1.0
    assert result is not None
    assert result["code"] == "WORKER_REENTRY"
    assert not mgr._io_lock.locked()


def test_acquire_io_stops_waiting_when_holder_enters_tool_rpc(monkeypatch):
    """A waiter parked on the lock must leave once a tool RPC starts."""
    import plugin.scripting.venv_worker as venv_worker_module

    monkeypatch.setattr("plugin.framework.thread_guard.on_main_thread", lambda: False)
    monkeypatch.setattr(venv_worker_module, "_IO_LOCK_ACQUIRE_TIMEOUT_SEC", 30.0)
    mgr = PythonWorkerManager(sys.executable, {})
    assert mgr._io_lock.acquire(timeout=1)
    mgr._io_owner = threading.get_ident()
    outcome: dict[str, dict] = {}

    def _other() -> None:
        got = mgr._acquire_io()
        assert got is not None
        outcome["result"] = got

    waiter = threading.Thread(target=_other)
    waiter.start()
    time.sleep(0.1)
    mgr._serving_tool_call = True
    waiter.join(timeout=2)
    try:
        assert not waiter.is_alive()
        assert outcome["result"]["code"] == "WORKER_REENTRY"
    finally:
        mgr._serving_tool_call = False
        mgr._io_owner = None
        mgr._io_lock.release()


def test_acquire_io_times_out_instead_of_blocking(monkeypatch):
    """A holder that never releases must not pin the waiter forever."""
    import plugin.scripting.venv_worker as venv_worker_module

    monkeypatch.setattr("plugin.framework.thread_guard.on_main_thread", lambda: False)
    monkeypatch.setattr(venv_worker_module, "_IO_LOCK_ACQUIRE_TIMEOUT_SEC", 0.15)
    mgr = PythonWorkerManager(sys.executable, {})
    assert mgr._io_lock.acquire(timeout=1)
    outcome: dict[str, dict] = {}

    def _other() -> None:
        got = mgr._acquire_io()
        assert got is not None
        outcome["result"] = got

    try:
        started = time.monotonic()
        waiter = threading.Thread(target=_other)
        waiter.start()
        waiter.join(timeout=2)
        assert not waiter.is_alive()
        assert time.monotonic() - started < 2.0
        assert outcome["result"]["code"] == "WORKER_REENTRY"
        assert "busy" in outcome["result"]["message"]
    finally:
        mgr._io_lock.release()


def test_acquire_io_takes_lock_after_holder_releases(monkeypatch):
    """A bounded wait still serializes a second caller once the pipe is free."""
    monkeypatch.setattr("plugin.framework.thread_guard.on_main_thread", lambda: False)
    mgr = PythonWorkerManager(sys.executable, {})
    assert mgr._io_lock.acquire(timeout=1)
    mgr._io_owner = threading.get_ident()
    outcome: list = []

    def _other() -> None:
        outcome.append(mgr._acquire_io())

    waiter = threading.Thread(target=_other)
    waiter.start()
    time.sleep(0.1)
    mgr._io_owner = None
    mgr._io_lock.release()
    waiter.join(timeout=2)
    assert not waiter.is_alive()
    assert outcome == [None]
    assert mgr._io_owner == waiter.ident
    mgr._release_io()


def test_kill_process_tree_signals_group_after_leader_exits(monkeypatch):
    """Descendants must still be signaled when the direct child has already exited."""
    import plugin.scripting.venv_worker as venv_worker_module

    proc = MagicMock()
    proc.pid = 4242
    proc.poll.return_value = 0
    if sys.platform == "win32":
        run = MagicMock()
        monkeypatch.setattr(venv_worker_module.subprocess, "run", run)
        venv_worker_module._kill_process_tree(proc)
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

    monkeypatch.setattr(os, "getpgid", _getpgid)
    monkeypatch.setattr(os, "killpg", _killpg)

    # Leader already exited, proc.poll() returns 0.
    proc.poll.return_value = 0
    venv_worker_module._kill_process_tree(proc)
    assert killed == {"pgid": 4242, "sig": signal.SIGKILL}
    proc.kill.assert_not_called()

def test_kill_process_tree_no_pgid_but_alive(monkeypatch):
    """If getpgid fails and the leader is still alive, we fallback to proc.kill()."""
    import plugin.scripting.venv_worker as venv_worker_module

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

    monkeypatch.setattr(os, "getpgid", _getpgid)
    monkeypatch.setattr(os, "killpg", _killpg)

    venv_worker_module._kill_process_tree(proc)
    assert not killed  # os.killpg not called
    proc.kill.assert_called_once()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups")
def test_kill_process_tree_reaps_grandchild_after_leader_exits():
    """Real session: leader exit must not leave the grandchild running."""
    import plugin.scripting.venv_worker as venv_worker_module

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
        venv_worker_module._kill_process_tree(proc)
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


def test_ensure_running_starts_new_session_without_preexec(monkeypatch):
    """setsid belongs on the C spawn path, not a Python preexec_fn."""
    import plugin.scripting.venv_worker as venv_worker_module

    popen = MagicMock()
    proc = MagicMock()
    proc.pid = 7
    proc.stdin = None
    proc.stdout = None
    proc.stderr = None
    popen.return_value = proc
    monkeypatch.setattr(venv_worker_module.subprocess, "Popen", popen)
    monkeypatch.setattr(venv_worker_module, "wrap_command_for_sandbox", lambda cmd: cmd)
    monkeypatch.setattr(venv_worker_module, "optimize_popen_pipes", lambda _proc: None)
    monkeypatch.setattr(venv_worker_module, "start_stderr_drain", lambda *_args, **_kwargs: None)
    mgr = PythonWorkerManager(sys.executable, {})
    mgr._ensure_running()
    kwargs = popen.call_args.kwargs
    assert "preexec_fn" not in kwargs
    if sys.platform == "win32":
        assert kwargs.get("creationflags") == subprocess.CREATE_NO_WINDOW
    else:
        assert kwargs.get("start_new_session") is True


def test_script_llm_request_does_not_dispatch_host_llm(monkeypatch):
    """caller='script' (including =PY()) must not run llm_request on host credentials."""
    from plugin.scripting.venv_worker import _maybe_dispatch_intermediate_response

    def _boom(_payload):
        raise AssertionError("host LLM must not run for a non-ppt worker")

    monkeypatch.setattr("plugin.ppt_master.venv.host_rpc.handle_llm_request", _boom)
    frame = {"type": "llm_request", "id": "1", "messages": [{"role": "user", "content": "x"}]}
    written: list[bytes] = []
    handled = _maybe_dispatch_intermediate_response(
        frame,
        stdin_write=written.append,
        caller="script",
    )
    assert handled is False
    assert written == []
    handled_default = _maybe_dispatch_intermediate_response(frame, stdin_write=written.append)
    assert handled_default is False
    assert written == []


def test_ppt_master_llm_request_still_dispatches(monkeypatch):
    from plugin.scripting.venv_worker import _maybe_dispatch_intermediate_response

    monkeypatch.setattr(
        "plugin.ppt_master.venv.host_rpc.handle_llm_request",
        lambda _payload: {"status": "ok", "result": {"content": "hi"}},
    )
    written: list[bytes] = []
    handled = _maybe_dispatch_intermediate_response(
        {"type": "llm_request", "id": "9", "messages": []},
        stdin_write=written.append,
        caller="ppt_master_venv",
    )
    assert handled is True
    assert len(written) == 1


def test_drain_stderr_fallback_does_not_wait_for_eof():
    """An open stderr pipe must not hang the fallback reader."""
    read_fd, write_fd = os.pipe()
    mgr = PythonWorkerManager(sys.executable, {})
    mgr._stderr_drain = None
    proc = MagicMock()
    proc.stderr = os.fdopen(read_fd, "rb", buffering=0)
    proc.wait.return_value = 0
    mgr._proc = proc
    try:
        os.write(write_fd, b"boom\n")
        started = time.monotonic()
        text = mgr._drain_stderr()
        assert time.monotonic() - started < 2.0
        assert "boom" in text

        started = time.monotonic()
        empty = mgr._drain_stderr()
        assert time.monotonic() - started < 2.0
        assert empty == ""
    finally:
        os.close(write_fd)
        proc.stderr.close()

def test_warm_venv_worker_embeddings_timeout(monkeypatch):
    from plugin.scripting import venv_worker as vw
    from plugin.scripting.venv_worker import WORKER_POOL_EMBEDDINGS

    mock_execute_calls = []

    class DummyManager:
        def execute(self, action, data, timeout_sec=None, allow_heartbeat=False, **kwargs):
            mock_execute_calls.append({"action": action, "timeout_sec": timeout_sec, "allow_heartbeat": allow_heartbeat})
            return {"status": "ok"}

        def warm(self):
            pass

    monkeypatch.setattr(vw, "_resolve_worker_python", lambda ctx, pool: ("dummy_exe", None))
    monkeypatch.setattr(vw.PythonWorkerManager, "get", lambda exe, env, pool: DummyManager())

    # Mock embedding_client.get_embedding_model
    import sys
    import types
    mod = types.ModuleType("plugin.embeddings.embedding_client")
    mod.get_embedding_model = lambda: "dummy-model"  # type: ignore # type: ignore
    sys.modules["plugin.embeddings.embedding_client"] = mod

    # Mock config_limits
    mod2 = types.ModuleType("plugin.scripting.config_limits")
    mod2.embeddings_worker_timeout_sec = lambda: 300  # type: ignore # type: ignore
    sys.modules["plugin.scripting.config_limits"] = mod2

    try:
        vw.warm_venv_worker(None, pool=WORKER_POOL_EMBEDDINGS)
    finally:
        sys.modules.pop("plugin.embeddings.embedding_client", None)
        sys.modules.pop("plugin.scripting.config_limits", None)

    assert len(mock_execute_calls) == 1
    assert mock_execute_calls[0]["action"] == "run_trusted_action"
    assert mock_execute_calls[0]["timeout_sec"] == 300
    assert mock_execute_calls[0]["allow_heartbeat"] is True


def test_terminate_worker_race_condition(monkeypatch):
    from plugin.scripting import venv_worker as vw
    import threading

    mgr = vw.PythonWorkerManager.__new__(vw.PythonWorkerManager)
    mgr.exe = "dummy"
    mgr._proc_lock = threading.Lock()
    mgr._retired = False

    class DummyProc:
        def __init__(self):
            self.pid = 123
        def poll(self):
            return None
        def kill(self):
            pass
        def wait(self, timeout=None):
            pass

    mgr._proc = DummyProc()  # type: ignore # type: ignore
    mgr._stderr_drain = None
    mgr._primed = True

    # Simulate a concurrent terminate inside read_response_bytes
    def mocked_select(r, w, x, timeout):
        mgr._terminate_worker() # Nulls out _proc
        return [r], [], []

    with monkeypatch.context() as m:
        m.setattr(vw.select, "select", mocked_select)

        class MockStdout:
            def read(self, n):
                return b""

        try:
            # POSIX path uses _read_response_bytes_select
            if sys.platform != "win32":
                mgr._read_response_bytes_select(MockStdout(), timeout_sec=1)  # type: ignore
        except (vw.subprocess.TimeoutExpired, EOFError):
            pass # Expected

    if sys.platform != "win32":
        assert mgr._proc is None

def test_read_response_with_heartbeats_swallows_callback_exceptions():
    from plugin.scripting.venv_worker import PythonWorkerManager
    from plugin.scripting.venv.worker_heartbeat import FRAME_HEARTBEAT, FRAME_RESULT
    import io

    mgr = PythonWorkerManager.__new__(PythonWorkerManager)

    # Mock parse_frame to first return a heartbeat, then a result frame
    call_count = 0
    def mock_parse_frame(frame_bytes):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return {"frame_type": FRAME_HEARTBEAT, "payload": {"phase": "test"}}
        return {"frame_type": FRAME_RESULT, "status": "ok"}


    # Track calls to on_heartbeat
    heartbeat_calls = []
    def on_heartbeat(payload):
        heartbeat_calls.append(payload)
        raise RuntimeError("simulated ui callback error")

    with patch.object(mgr, "_read_frame_bytes", return_value=b"dummy"), \
         patch("plugin.scripting.venv.worker_heartbeat.parse_frame", side_effect=mock_parse_frame):

        # It shouldn't crash, it should return the b"dummy" frame ultimately
        result = mgr._read_response_with_heartbeats(
            stdout=io.BytesIO(),
            timeout_sec=10.0,
            grace_sec=5,
            on_heartbeat=on_heartbeat
        )

    assert result == b"dummy"
    assert heartbeat_calls == [{"phase": "test"}]


def test_execute_ipc_attempts_stop_checker_aborts(monkeypatch: pytest.MonkeyPatch) -> None:
    # If stop_checker returns True, the host read loop should abort and not hang.
    import time
    from plugin.scripting import venv_worker

    # Mock _ensure_running, _write_frame_with_timeout to do nothing
    monkeypatch.setattr(venv_worker.PythonWorkerManager, "_ensure_running", lambda self: None)
    monkeypatch.setattr(venv_worker.PythonWorkerManager, "_write_frame_with_timeout", lambda self, stdin, req, timeout_sec, label: None)

    # Mock proc and io
    class MockProc:
        def __init__(self):
            self.stdin = "stdin"
            self.stdout = "stdout"
            self.stderr = None
        def poll(self):
            return None

    mock_proc = MockProc()

    class DummyManager(venv_worker.PythonWorkerManager):
        def __init__(self):
            self.exe = "python"
            self._proc = mock_proc
            self._stderr_drain = None

        def _ensure_running(self):
            pass

        # Bypass thread selection to test generic read select logic if possible, or just test the _read_response_bytes directly.
        # Actually, let's just test _read_response_bytes directly.

    m = DummyManager()

    # We want to test _read_response_bytes_select and _read_response_bytes_threaded
    class MockStdout:
        def read(self, n):
            time.sleep(10)
            return b""

    # threaded
    stop_called = [False]
    def stop_checker():
        stop_called[0] = True
        return True

    with pytest.raises(venv_worker._StopRequested):
        m._read_response_bytes_threaded(MockStdout(), timeout_sec=60, stop_checker=stop_checker)

    assert stop_called[0]

    # select (POSIX only: on win32 the select path delegates to the PeekNamedPipe reader)
    if sys.platform == "win32":
        return
    stop_called2 = [False]
    def stop_checker2():
        stop_called2[0] = True
        return True

    monkeypatch.setattr(venv_worker.select, "select", lambda r,w,x,t: ([], [], []))
    with pytest.raises(venv_worker._StopRequested):
        m._read_response_bytes_select(MockStdout(), timeout_sec=60, stop_checker=stop_checker2)

    assert stop_called2[0]


def _raising_then_false_stop_checker():
    seen: list[BaseException] = []

    def stop_checker() -> bool:
        if not seen:
            err = KeyError("stop_checker blew up")
            seen.append(err)
            raise err
        return False

    return seen, stop_checker


def test_raising_stop_checker_on_threaded_read_returns_frame() -> None:
    """A broken stop_checker must not escape or abandon the queued frame."""
    from plugin.scripting import venv_worker

    payload = {"status": "ok", "result": 1}
    frame = pack_pickle_frame(payload)
    stdout = io.BytesIO(frame)

    class DummyManager(venv_worker.PythonWorkerManager):
        def __init__(self) -> None:
            self.exe = "python"
            self._proc = None
            self._stderr_drain = None

    seen, stop_checker = _raising_then_false_stop_checker()
    result = DummyManager()._read_response_bytes_threaded(stdout, timeout_sec=5, stop_checker=stop_checker)
    assert seen
    assert venv_worker.unpack_pickle_frame(result) == payload


def test_raising_stop_checker_on_select_read_returns_frame(monkeypatch: pytest.MonkeyPatch) -> None:
    from plugin.scripting import venv_worker

    if sys.platform == "win32":
        return

    payload = {"status": "ok", "result": 1}
    frame = pack_pickle_frame(payload)
    stdout = io.BytesIO(frame)

    class DummyManager(venv_worker.PythonWorkerManager):
        def __init__(self) -> None:
            self.exe = "python"
            self._proc = None
            self._stderr_drain = None

    monkeypatch.setattr(venv_worker.select, "select", lambda r, w, x, t: (list(r), [], []))
    seen, stop_checker = _raising_then_false_stop_checker()
    result = DummyManager()._read_response_bytes_select(stdout, timeout_sec=5, stop_checker=stop_checker)
    assert seen
    assert venv_worker.unpack_pickle_frame(result) == payload


def test_read_response_with_heartbeats_stop_checker(monkeypatch: pytest.MonkeyPatch) -> None:
    from plugin.scripting import venv_worker

    class MockProc:
        def __init__(self):
            self.stdin = "stdin"
            self.stdout = "stdout"
            self.stderr = None
        def poll(self):
            return None

    class DummyManager(venv_worker.PythonWorkerManager):
        def __init__(self):
            self.exe = "python"
            self._proc = MockProc()
            self._stderr_drain = None

        def _read_exact_before_deadline(self, stdout, nbytes, deadline):
            return b""

    m = DummyManager()

    stop_called = [False]
    def stop_checker():
        stop_called[0] = True
        return True

    class MockStdout:
        def read(self, n):
            return b""

    with pytest.raises(venv_worker._StopRequested):
        m._read_response_with_heartbeats(MockStdout(), timeout_sec=60, grace_sec=10, on_heartbeat=None, stop_checker=stop_checker)

    assert stop_called[0]

def test_host_pack_oversize_returns_error_without_terminate(monkeypatch):
    """An oversize request fails before any write; the warm worker must survive."""
    import plugin.scripting.venv_worker as venv_worker_module
    from plugin.scripting.ipc import IpcFrameError

    def _pack(*args, **kwargs):
        raise IpcFrameError("payload too large")

    monkeypatch.setattr(venv_worker_module, "pack_pickle_frame", _pack)
    mgr = PythonWorkerManager(sys.executable, {})
    proc = MagicMock()
    proc.poll.return_value = None
    proc.stdin = io.BytesIO()
    proc.stdout = io.BytesIO()
    mgr._proc = proc
    mgr._ensure_running = MagicMock()  # type: ignore[method-assign]
    mgr._terminate_worker = MagicMock()  # type: ignore[method-assign]

    result = mgr._execute_ipc_unlocked("result = 1", timeout_sec=1)

    assert result["status"] == "error"
    assert result["code"] == "WORKER_IPC_ERROR"
    assert "payload too large" in result["message"]
    mgr._terminate_worker.assert_not_called()
    assert proc.stdin.getvalue() == b""


def test_missing_proc_after_concurrent_terminate():
    """A concurrent terminate (shutdown_all) leaves _proc None; return an error, not AttributeError."""
    mgr = PythonWorkerManager(sys.executable, {})
    mgr._proc = None
    mgr._ensure_running = MagicMock()  # type: ignore[method-assign]
    mgr._terminate_worker = MagicMock()  # type: ignore[method-assign]

    result = mgr._execute_ipc_unlocked("result = 1", timeout_sec=1)

    assert result["status"] == "error"
    assert result["code"] == "WORKER_IPC_ERROR"
    assert "worker terminated concurrently" in result["message"]


def test_stop_checker_returns_cancelled_code():
    """Stop during the IPC wait kills the worker, does not replay, and returns CANCELLED."""
    from plugin.scripting.venv_worker import _StopRequested

    mgr = PythonWorkerManager(sys.executable, {})
    proc = MagicMock()
    proc.poll.return_value = None
    proc.stdin = io.BytesIO()
    proc.stdout = io.BytesIO()
    mgr._proc = proc
    mgr._ensure_running = MagicMock()  # type: ignore[method-assign]
    mgr._terminate_worker = MagicMock()  # type: ignore[method-assign]
    mgr._write_frame_with_timeout = MagicMock()  # type: ignore[method-assign]
    mgr._read_response_bytes = MagicMock(side_effect=_StopRequested())  # type: ignore[method-assign]

    result = mgr._execute_ipc_unlocked("result = 1", timeout_sec=1, stop_checker=lambda: True)

    assert result["status"] == "error"
    assert result["code"] == "CANCELLED"
    assert result["message"] == "Python worker stopped by user"
    assert mgr._write_frame_with_timeout.call_count == 1
    mgr._terminate_worker.assert_called_once()


def test_partial_stdin_write_delivers_full_frame():
    """A raw pipe write() can accept fewer bytes than given; the rest must still be sent."""
    mgr = PythonWorkerManager(sys.executable, {})
    stdin = MagicMock()
    written: list[bytes] = []

    def _short_write(b):
        chunk = bytes(b[:2])
        written.append(chunk)
        return len(chunk)

    stdin.write = _short_write

    mgr._write_bytes_with_timeout(stdin, b"12345", timeout_sec=1, label="test")

    assert written == [b"12", b"34", b"5"]
    stdin.flush.assert_called_once()


def test_kill_process_tree_host_pgid_protection(monkeypatch):
    """Never killpg the host's own group; fall back to killing just the child."""
    import plugin.scripting.venv_worker as venv_worker_module

    if sys.platform == "win32":
        pytest.skip("POSIX process groups")
    proc = MagicMock()
    proc.pid = 12345
    proc.poll.return_value = None
    killpg = MagicMock()
    monkeypatch.setattr(venv_worker_module.os, "getpgid", lambda pid: 9999)
    monkeypatch.setattr(venv_worker_module.os, "getpgrp", lambda: 9999)
    monkeypatch.setattr(venv_worker_module.os, "killpg", killpg)

    venv_worker_module._kill_process_tree(proc)

    killpg.assert_not_called()
    proc.kill.assert_called_once()


class _FakeStdStream:
    def __init__(self, buf):
        self.buffer = buf


def test_harness_main_value_error_in_handle_request_returns_traceback(monkeypatch):
    import io
    from plugin.scripting.ipc import read_pickle_frame
    import plugin.scripting.venv.worker_harness as harness

    req_data = pack_pickle_frame({"id": "req-ve", "action": "execute", "code": "result = 1"})
    stdin = io.BytesIO(req_data)
    stdout = io.BytesIO()

    monkeypatch.setattr(harness.sys, "stdin", _FakeStdStream(stdin))
    monkeypatch.setattr(harness.sys, "stdout", _FakeStdStream(stdout))
    monkeypatch.setattr(harness, "_die_with_parent", lambda: None)
    monkeypatch.setattr(harness, "_init_logging", lambda: None)

    def _raise_ve(*args, **kwargs):
        raise ValueError("custom value error")

    monkeypatch.setattr(harness, "_handle_request", _raise_ve)

    harness.main()

    stdout.seek(0)
    started = read_pickle_frame(stdout, require_dict=True)
    assert started == {"type": "exec_started", "id": "req-ve"}
    res = read_pickle_frame(stdout, require_dict=True)
    assert res is not None
    assert res["id"] == "req-ve"
    assert res["status"] == "error"
    assert "custom value error" in res["message"]
    assert "Invalid pickle request" not in res["message"]
    assert "traceback" in res and "ValueError: custom value error" in res["traceback"]


def test_harness_main_value_error_in_read_stage_returns_invalid_pickle_request(monkeypatch):
    import io
    import struct
    from plugin.scripting.ipc import read_pickle_frame
    import plugin.scripting.venv.worker_harness as harness

    # Pickle of a list ([1, 2, 3]), which violates require_dict=True in read_pickle_frame
    payload = pickle.dumps([1, 2, 3], protocol=5)
    bad_frame = struct.pack("!I", len(payload)) + payload
    stdin = io.BytesIO(bad_frame)
    stdout = io.BytesIO()

    monkeypatch.setattr(harness.sys, "stdin", _FakeStdStream(stdin))
    monkeypatch.setattr(harness.sys, "stdout", _FakeStdStream(stdout))
    monkeypatch.setattr(harness, "_die_with_parent", lambda: None)
    monkeypatch.setattr(harness, "_init_logging", lambda: None)

    harness.main()

    stdout.seek(0)
    res = read_pickle_frame(stdout, require_dict=True)
    assert res is not None
    assert res["status"] == "error"
    assert "Invalid pickle request" in res["message"]
    assert "must contain a dict" in res["message"]


def test_harness_main_ipc_frame_error_in_read_stage_breaks_without_response(monkeypatch):
    import io
    import struct
    import plugin.scripting.venv.worker_harness as harness

    # Invalid frame size prefix (> DEFAULT_MAX_PAYLOAD_BYTES)
    bad_header = struct.pack("!I", DEFAULT_MAX_PAYLOAD_BYTES + 100) + b"extra"
    stdin = io.BytesIO(bad_header)
    stdout = io.BytesIO()

    monkeypatch.setattr(harness.sys, "stdin", _FakeStdStream(stdin))
    monkeypatch.setattr(harness.sys, "stdout", _FakeStdStream(stdout))
    monkeypatch.setattr(harness, "_die_with_parent", lambda: None)
    monkeypatch.setattr(harness, "_init_logging", lambda: None)

    harness.main()

    assert stdout.getvalue() == b""


def test_harness_main_read_desync_during_handle_request_breaks(monkeypatch):
    import io
    from plugin.scripting.ipc import IpcFrameReadError, read_pickle_frame
    import plugin.scripting.venv.worker_harness as harness

    req_data = pack_pickle_frame({"id": "req-desync", "action": "execute", "code": "result = 1"})
    stdin = io.BytesIO(req_data)
    stdout = io.BytesIO()

    monkeypatch.setattr(harness.sys, "stdin", _FakeStdStream(stdin))
    monkeypatch.setattr(harness.sys, "stdout", _FakeStdStream(stdout))
    monkeypatch.setattr(harness, "_die_with_parent", lambda: None)
    monkeypatch.setattr(harness, "_init_logging", lambda: None)

    def _raise_read_desync(*args, **kwargs):
        raise IpcFrameReadError("tool_call response stream desynchronized")

    monkeypatch.setattr(harness, "_handle_request", _raise_read_desync)

    harness.main()

    stdout.seek(0)
    started = read_pickle_frame(stdout, require_dict=True)
    assert started == {"type": "exec_started", "id": "req-desync"}
    # Worker must break without writing a corrupted frame
    assert stdout.read() == b""


def test_harness_main_pack_size_error_during_handle_request_writes_error_frame(monkeypatch):
    import io
    from plugin.scripting.ipc import IpcPayloadSizeError, read_pickle_frame
    import plugin.scripting.venv.worker_harness as harness

    req_data = pack_pickle_frame({"id": "req-pack-err", "action": "execute", "code": "result = 1"})
    stdin = io.BytesIO(req_data)
    stdout = io.BytesIO()

    monkeypatch.setattr(harness.sys, "stdin", _FakeStdStream(stdin))
    monkeypatch.setattr(harness.sys, "stdout", _FakeStdStream(stdout))
    monkeypatch.setattr(harness, "_die_with_parent", lambda: None)
    monkeypatch.setattr(harness, "_init_logging", lambda: None)

    def _raise_pack_err(*args, **kwargs):
        raise IpcPayloadSizeError("Pickle frame exceeds maximum payload size: 20000000")

    monkeypatch.setattr(harness, "_handle_request", _raise_pack_err)

    harness.main()

    stdout.seek(0)
    started = read_pickle_frame(stdout, require_dict=True)
    assert started == {"type": "exec_started", "id": "req-pack-err"}
    res = read_pickle_frame(stdout, require_dict=True)
    assert res is not None
    assert res["id"] == "req-pack-err"
    assert res["status"] == "error"
    assert "maximum payload size" in res["message"]
    assert "traceback" in res


def test_harness_main_user_stopped_writes_user_stopped_frame(monkeypatch):
    import io
    from plugin.scripting.ipc import UserStopped, read_pickle_frame
    import plugin.scripting.venv.worker_harness as harness

    req_data = pack_pickle_frame({"id": "req-stop", "action": "execute", "code": "result = 1"})
    stdin = io.BytesIO(req_data)
    stdout = io.BytesIO()

    monkeypatch.setattr(harness.sys, "stdin", _FakeStdStream(stdin))
    monkeypatch.setattr(harness.sys, "stdout", _FakeStdStream(stdout))
    monkeypatch.setattr(harness, "_die_with_parent", lambda: None)
    monkeypatch.setattr(harness, "_init_logging", lambda: None)

    def _raise_user_stopped(*args, **kwargs):
        raise UserStopped("Interrupted by user.")

    monkeypatch.setattr(harness, "_handle_request", _raise_user_stopped)

    harness.main()

    stdout.seek(0)
    started = read_pickle_frame(stdout, require_dict=True)
    assert started == {"type": "exec_started", "id": "req-stop"}
    res = read_pickle_frame(stdout, require_dict=True)
    assert res is not None
    assert res["id"] == "req-stop"
    assert res["status"] == "error"
    assert res["code"] == "USER_STOPPED"
    assert "Interrupted by user" in res["message"]
