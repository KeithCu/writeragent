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
import json
import os
import pickle
import stat
import struct
import subprocess
import sys
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from plugin.scripting.config_limits import WARM_WORKER_TIMEOUT_SEC
from plugin.scripting.venv_worker import (
    PythonWorkerManager,
    _worker_error_message,
    probe_venv_path,
    resolve_libreoffice_python,
    resolve_venv_python,
    run_code_in_user_venv,
    run_venv_self_check,
    warm_venv_worker,
    scrub_subprocess_env,
)
from plugin.scripting.worker_harness import _execute_request, _serialize
from plugin.tests.testing_utils import setup_uno_mocks

setup_uno_mocks()


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
    r = _execute_request("result = sum(data)", [1, 2, 3, 4])
    assert r["status"] == "ok"
    assert r["result"] == 10


def test_execute_request_injects_data_list_single_range():
    r = _execute_request("result = (len(data_list), data_list[0] is data)", [1, 2, 3])
    assert r["status"] == "ok"
    assert r["result"] == [1, True]


def test_execute_request_injects_data_list_multi_range():
    from plugin.scripting.payload_codec import host_pack_multi_data

    wire = host_pack_multi_data([[1.0, 2.0, 3.0], [4.0, 5.0]], force="never")
    r = _execute_request("result = (len(data_list), data_list is data)", wire)
    assert r["status"] == "ok"
    assert r["result"] == [2, True]


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
        "from sentence_transformers import SentenceTransformer\nresult = SentenceTransformer.__name__",
        None,
    )
    assert r["status"] == "ok"
    assert r["result"] == "SentenceTransformer"


def test_harness_main_loop_integration():
    """Harness reads and writes Pickle (subprocess smoke)."""
    harness = __import__("plugin.scripting.worker_harness", fromlist=["main"])
    
    import pickle
    import struct
    proc_pickle = subprocess.Popen(
        [sys.executable, harness.__file__],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=False,
        bufsize=0,
    )
    req_dict = {"id": "t2", "code": "result = 2 ** 10"}
    payload = pickle.dumps(req_dict, protocol=5)
    header = struct.pack("!I", len(payload))
    proc_pickle.stdin.write(header)
    proc_pickle.stdin.write(payload)
    proc_pickle.stdin.flush()

    resp_header = proc_pickle.stdout.read(4)
    assert len(resp_header) == 4
    resp_size = struct.unpack("!I", resp_header)[0]
    resp_payload = proc_pickle.stdout.read(resp_size)
    assert len(resp_payload) == resp_size
    resp_dict = pickle.loads(resp_payload)
    assert resp_dict["id"] == "t2"
    assert resp_dict["status"] == "ok"
    assert resp_dict["result"] == 1024

    proc_pickle.stdin.close()
    proc_pickle.wait(timeout=5)



@patch("plugin.scripting.venv_worker.get_config_str", return_value="")
@patch("plugin.scripting.venv_worker.resolve_libreoffice_python", return_value=sys.executable)
def test_run_code_uses_manager(mock_lo_python, mock_cfg):
    from plugin.scripting.venv_worker import run_code_in_user_venv

    PythonWorkerManager.shutdown_all()
    ctx = MagicMock()
    r1 = run_code_in_user_venv(ctx, "result = 100")
    assert r1["status"] == "ok"
    assert r1["result"] == 100
    r2 = run_code_in_user_venv(ctx, "result = nope + 1")
    assert r2["status"] == "error"
    PythonWorkerManager.shutdown_all()


def test_manager_two_calls_same_process():
    PythonWorkerManager.shutdown_all()
    mgr = PythonWorkerManager.get(sys.executable, {"PATH": "/usr/bin:/bin"})
    r1 = mgr.execute("result = 1")
    assert r1["status"] == "ok"
    pid1 = mgr._proc.pid if mgr._proc else None
    r2 = mgr.execute("result = 2")
    assert r2["status"] == "ok"
    pid2 = mgr._proc.pid if mgr._proc else None
    assert pid1 is not None and pid1 == pid2
    r3 = mgr.execute("result = prev")
    assert r3["status"] == "error"
    PythonWorkerManager.shutdown_all()


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
    """Ingress split_grid: child receives ndarray from frombuffer."""
    np = pytest.importorskip("numpy")
    from plugin.calc.calc_addin_data import pack_calc_data_for_wire
    from plugin.scripting.payload_codec import BINARY_MIN_CELLS, is_split_grid
    from tests.scripting.payload_codec_test_support import NUMERIC_AT_THRESHOLD, sequential_grid_sum

    grid = NUMERIC_AT_THRESHOLD
    wire = pack_calc_data_for_wire(grid)
    assert is_split_grid(wire)
    r = _execute_request("result = float(data.sum())", wire)
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


def test_split_grid_result_round_trip_manager():
    """API responses unpack split_grid so LLM/UI never see wire envelopes."""
    pytest.importorskip("numpy")
    PythonWorkerManager.shutdown_all()
    mgr = PythonWorkerManager.get(sys.executable, {"PATH": "/usr/bin:/bin"})
    r = mgr.execute("import numpy as np\nresult = np.arange(16, dtype=np.float64).reshape(4, 4)")
    assert r["status"] == "ok"
    from plugin.scripting.payload_codec import is_split_grid

    assert not is_split_grid(r["result"])
    assert len(r["result"]) == 4
    assert r["result"][0][0] == pytest.approx(0.0)
    assert r["result"][3][3] == pytest.approx(15.0)
    PythonWorkerManager.shutdown_all()


def test_manager_unpacks_prime_tuple_list():
    """List-of-tuples large enough for split_grid on wire must return nested lists to callers."""
    pytest.importorskip("sympy")
    PythonWorkerManager.shutdown_all()
    mgr = PythonWorkerManager.get(sys.executable, {"PATH": "/usr/bin:/bin"})
    code = "result = [(i, int(sp.prime(i))) for i in range(100, 107)]"
    r = mgr.execute(code)
    assert r["status"] == "ok"
    from plugin.scripting.payload_codec import is_split_grid

    assert not is_split_grid(r["result"])
    assert r["result"] == [[100, 541], [101, 547], [102, 557], [103, 563], [104, 569], [105, 571], [106, 577]]
    assert all(isinstance(cell, int) for row in r["result"] for cell in row)
    PythonWorkerManager.shutdown_all()


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
    from plugin.scripting.venv_worker import run_code_in_user_venv
    ctx = MagicMock()

    # Call with no timeout and verify it gets default timeout of 10s
    run_code_in_user_venv(ctx, "result = 1")
    mock_execute.assert_called_once_with("result = 1", data=None, timeout_sec=10, session_id=None, init_script=None, init_session_id=None, init_script_hash=None)

    mock_execute.reset_mock()

    # Call with a custom timeout in the allowed range (e.g. 100s) and verify it is allowed
    run_code_in_user_venv(ctx, "result = 1", timeout_sec=100)
    mock_execute.assert_called_once_with("result = 1", data=None, timeout_sec=100, session_id=None, init_script=None, init_session_id=None, init_script_hash=None)

    mock_execute.reset_mock()

    # Call with a timeout exceeding 600s (e.g. 1000s) and verify it gets capped to 600s
    run_code_in_user_venv(ctx, "result = 1", timeout_sec=1000)
    mock_execute.assert_called_once_with("result = 1", data=None, timeout_sec=600, session_id=None, init_script=None, init_session_id=None, init_script_hash=None)

    mock_execute.reset_mock()

    # Call with 0s timeout and verify it gets set to 1s floor
    run_code_in_user_venv(ctx, "result = 1", timeout_sec=0)
    mock_execute.assert_called_once_with("result = 1", data=None, timeout_sec=1, session_id=None, init_script=None, init_session_id=None, init_script_hash=None)


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
    PythonWorkerManager.shutdown_all()
    mgr = PythonWorkerManager.get(sys.executable, {"PATH": "/usr/bin:/bin"})
    timeouts: list[int] = []
    original_read = mgr._read_response_bytes

    def record_read(stdout, timeout_sec):
        timeouts.append(timeout_sec)
        return original_read(stdout, timeout_sec)

    mgr._read_response_bytes = record_read  # type: ignore[method-assign]
    try:
        r = mgr.execute("result = 42", timeout_sec=3)
        assert r["status"] == "ok"
        assert r["result"] == 42
        assert timeouts == [WARM_WORKER_TIMEOUT_SEC, 3]
    finally:
        PythonWorkerManager.shutdown_all()


def test_warm_execute_uses_configured_timeout_only():
    """After priming, execute sends one IPC round at the configured timeout."""
    PythonWorkerManager.shutdown_all()
    mgr = PythonWorkerManager.get(sys.executable, {"PATH": "/usr/bin:/bin"})
    mgr.warm()
    timeouts: list[int] = []
    original_read = mgr._read_response_bytes

    def record_read(stdout, timeout_sec):
        timeouts.append(timeout_sec)
        return original_read(stdout, timeout_sec)

    mgr._read_response_bytes = record_read  # type: ignore[method-assign]
    try:
        r = mgr.execute("result = 7", timeout_sec=3)
        assert r["status"] == "ok"
        assert r["result"] == 7
        assert timeouts == [3]
    finally:
        PythonWorkerManager.shutdown_all()


def test_terminate_worker_re_primes_on_next_execute():
    """After worker kill, the next execute runs warm again before user code."""
    PythonWorkerManager.shutdown_all()
    mgr = PythonWorkerManager.get(sys.executable, {"PATH": "/usr/bin:/bin"})
    mgr.warm()
    mgr._terminate_worker()
    timeouts: list[int] = []
    original_read = mgr._read_response_bytes

    def record_read(stdout, timeout_sec):
        timeouts.append(timeout_sec)
        return original_read(stdout, timeout_sec)

    mgr._read_response_bytes = record_read  # type: ignore[method-assign]
    try:
        r = mgr.execute("result = 99", timeout_sec=3)
        assert r["status"] == "ok"
        assert r["result"] == 99
        assert timeouts == [WARM_WORKER_TIMEOUT_SEC, 3]
    finally:
        PythonWorkerManager.shutdown_all()


@patch("plugin.scripting.venv_worker.get_config_str", return_value="")
@patch("plugin.scripting.venv_worker.resolve_libreoffice_python", return_value=sys.executable)
def test_warm_venv_worker_resolves_and_warms(mock_lo_python, mock_cfg):
    from plugin.scripting.venv_worker import warm_venv_worker

    PythonWorkerManager.shutdown_all()
    ctx = MagicMock()
    warm_venv_worker(ctx)
    mgr = PythonWorkerManager.get(sys.executable, scrub_subprocess_env({"PATH": "/usr/bin:/bin"}))
    assert mgr._proc is not None and mgr._proc.poll() is None
    PythonWorkerManager.shutdown_all()


def test_split_grid_integration_pickle_mode():
    np = pytest.importorskip("numpy")
    from plugin.scripting.venv_worker import PythonWorkerManager

    PythonWorkerManager.shutdown_all()
    try:
        mgr = PythonWorkerManager.get(sys.executable, {"PATH": "/usr/bin:/bin"})

        # Execute some numpy array creation
        r = mgr.execute("import numpy as np\nresult = np.arange(100, dtype=np.float64).reshape(10, 10)")
        assert r["status"] == "ok"
        
        # Verify it was returned as regular nested list to callers (unpacked)
        assert len(r["result"]) == 10
        assert r["result"][0][0] == 0.0
        assert r["result"][9][9] == 99.0

        # Execute with input data as a large grid to trigger split-grid ingress packaging
        large_grid = [[float(r * 10 + c) for c in range(10)] for r in range(10)]
        r2 = mgr.execute("result = float(data.sum())", data=large_grid)
        assert r2["status"] == "ok"
        assert r2["result"] == pytest.approx(sum(r * 10 + c for r in range(10) for c in range(10)))
    finally:
        PythonWorkerManager.shutdown_all()



def _pack_response(obj: dict) -> bytes:
    """Encode a response the same way worker_harness.py does."""
    payload = pickle.dumps(obj, protocol=5)
    return struct.pack("!I", len(payload)) + payload


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

def test_resolve_venv_python_finds_posix_python(tmp_path):
    venv = tmp_path / "venv"
    bindir = venv / "bin"
    bindir.mkdir(parents=True)
    py = bindir / "python"
    py.write_text("#!/bin/sh\necho ok\n")
    py.chmod(py.stat().st_mode | stat.S_IEXEC)
    got = resolve_venv_python(str(venv))
    assert got == str(py)


def test_resolve_venv_python_finds_python3_only(tmp_path):
    venv = tmp_path / "venv"
    bindir = venv / "bin"
    bindir.mkdir(parents=True)
    py3 = bindir / "python3"
    py3.write_text("#!/bin/sh\necho ok\n")
    py3.chmod(py3.stat().st_mode | stat.S_IEXEC)
    got = resolve_venv_python(str(venv))
    assert got == str(py3)


def test_resolve_venv_python_none_when_missing(tmp_path):
    assert resolve_venv_python(str(tmp_path / "nope")) is None


def test_probe_venv_path_not_directory():
    ok, msg = probe_venv_path(__file__)
    assert ok is False
    assert "Not a directory" in msg or "directory" in msg.lower()


def test_probe_venv_path_blank_uses_process_python():
    with patch("plugin.scripting.venv_worker.resolve_libreoffice_python", return_value="/fake/lo/python") as mock_res:
        with patch("plugin.scripting.venv_worker.run_venv_self_check", return_value=(True, "ignored")) as mock_check:
            ok, msg = probe_venv_path("  ")
    assert ok is True
    assert "LibreOffice process Python" in msg
    assert "/fake/lo/python" in msg
    mock_res.assert_called_once()
    mock_check.assert_called_once_with("/fake/lo/python", timeout=10.0)


def test_probe_venv_path_blank_fails_when_no_process_interpreter():
    with patch("plugin.scripting.venv_worker.resolve_libreoffice_python", return_value=None):
        ok, msg = probe_venv_path("")
    assert ok is False
    assert "No process interpreter" in msg


def test_resolve_libreoffice_python_returns_executable(tmp_path, monkeypatch):
    p = tmp_path / "python"
    p.write_text("#!/bin/sh\necho\n")
    p.chmod(p.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setattr(sys, "executable", str(p))
    assert resolve_libreoffice_python() == str(p)


def test_resolve_libreoffice_python_none_when_missing_executable(tmp_path, monkeypatch):
    p = tmp_path / "python"
    p.write_text("not executable")
    p.chmod(0o644)
    monkeypatch.setattr(sys, "executable", str(p))
    if sys.platform == "win32":
        assert resolve_libreoffice_python() == str(p)
    else:
        assert resolve_libreoffice_python() is None


def test_resolve_libreoffice_python_empty_string(monkeypatch):
    monkeypatch.setattr(sys, "executable", "")
    assert resolve_libreoffice_python() is None


def test_resolve_libreoffice_python_nonexistent_path(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "executable", str(tmp_path / "does_not_exist"))
    assert resolve_libreoffice_python() is None
def test_run_venv_self_check_success():
    ok, msg = run_venv_self_check(sys.executable, timeout=10.0)
    assert ok is True
    assert "OK" in msg or "ok" in msg.lower()


def test_run_venv_self_check_worker_start_error():
    mock_mgr = MagicMock()
    mock_mgr.execute.side_effect = OSError("boom")
    with patch("plugin.scripting.venv_worker.PythonWorkerManager.get", return_value=mock_mgr):
        ok, msg = run_venv_self_check("/fake/python", timeout=1.0)
    assert ok is False
    assert "boom" in msg


def test_run_venv_self_check_worker_error_response():
    mock_mgr = MagicMock()
    mock_mgr.execute.return_value = {"status": "error", "message": "nope"}
    with patch("plugin.scripting.venv_worker.PythonWorkerManager.get", return_value=mock_mgr):
        ok, msg = run_venv_self_check("/x/python", timeout=1.0)
    assert ok is False
    assert "nope" in msg


def test_run_venv_self_check_timeout():
    mock_mgr = MagicMock()
    mock_mgr.execute.return_value = {
        "status": "error",
        "message": "Python worker failed: Command timed out after 1 seconds",
    }
    with patch("plugin.scripting.venv_worker.PythonWorkerManager.get", return_value=mock_mgr):
        ok, msg = run_venv_self_check("/x/python", timeout=1.0)
    assert ok is False
    assert "Timed out" in msg


def test_run_venv_self_check_reports_architecture():
    """Live self-check includes platform.machine() in the output."""
    ok, msg = run_venv_self_check(sys.executable, timeout=10.0)
    assert ok is True
    import platform
    expected_arch = platform.machine()
    assert expected_arch in msg


def test_format_self_check_success_with_arch():
    from plugin.scripting.venv_worker import _format_self_check_success
    data = {"v": "3.12.0", "arch": "ARM64", "p": {}, "sci": [], "ui": []}
    msg = _format_self_check_success(data)
    assert "Python 3.12.0 (ARM64)" in msg
    assert "responds OK" in msg


def test_format_self_check_success_without_arch():
    from plugin.scripting.venv_worker import _format_self_check_success
    data = {"v": "3.11.5", "p": {}, "sci": [], "ui": []}
    msg = _format_self_check_success(data)
    assert "Python 3.11.5 responds OK" in msg
    assert "(" not in msg.split("\n")[0]


# --- Subprocess spawn helper tests (relocated from test_subprocess_helpers.py) ---

from plugin.scripting.venv_worker import (
    _PIPE_BUF_TARGET,
    _reset_cache,
    detect_sandbox,
    optimize_pipe,
    optimize_popen_pipes,
    wrap_command_for_sandbox,
)


def test_detect_flatpak_via_file():
    _reset_cache()
    try:
        with patch("plugin.scripting.venv_worker.os.path.exists", return_value=True) as mock_exists:
            with patch.dict("os.environ", {}, clear=True):
                assert detect_sandbox() == "flatpak"
                mock_exists.assert_called_with("/.flatpak-info")
    finally:
        _reset_cache()


def test_detect_flatpak_via_env():
    _reset_cache()
    try:
        with patch("plugin.scripting.venv_worker.os.path.exists", return_value=False):
            with patch.dict("os.environ", {"FLATPAK_ID": "org.libreoffice.LibreOffice"}, clear=True):
                assert detect_sandbox() == "flatpak"
    finally:
        _reset_cache()


def test_detect_snap():
    _reset_cache()
    try:
        with patch("plugin.scripting.venv_worker.os.path.exists", return_value=False):
            with patch.dict("os.environ", {"SNAP_NAME": "libreoffice"}, clear=True):
                assert detect_sandbox() == "snap"
    finally:
        _reset_cache()


def test_detect_none():
    _reset_cache()
    try:
        with patch("plugin.scripting.venv_worker.os.path.exists", return_value=False):
            with patch.dict("os.environ", {}, clear=True):
                assert detect_sandbox() is None
    finally:
        _reset_cache()


def test_result_is_cached():
    _reset_cache()
    try:
        with patch("plugin.scripting.venv_worker.os.path.exists", return_value=True) as mock_exists:
            with patch.dict("os.environ", {}, clear=True):
                assert detect_sandbox() == "flatpak"
                assert detect_sandbox() == "flatpak"
                mock_exists.assert_called_once()
    finally:
        _reset_cache()


def test_wrap_flatpak():
    _reset_cache()
    try:
        with patch("plugin.scripting.venv_worker.os.path.exists", return_value=True):
            with patch.dict("os.environ", {}, clear=True):
                cmd = ["/home/user/.venv/bin/python", "script.py"]
                result = wrap_command_for_sandbox(cmd)
                assert result == ["flatpak-spawn", "--host", "/home/user/.venv/bin/python", "script.py"]
    finally:
        _reset_cache()


def test_wrap_snap_unchanged():
    _reset_cache()
    try:
        with patch("plugin.scripting.venv_worker.os.path.exists", return_value=False):
            with patch.dict("os.environ", {"SNAP_NAME": "libreoffice"}, clear=True):
                cmd = ["/home/user/.venv/bin/python", "script.py"]
                result = wrap_command_for_sandbox(cmd)
                assert result == cmd
    finally:
        _reset_cache()


def test_wrap_no_sandbox():
    _reset_cache()
    try:
        with patch("plugin.scripting.venv_worker.os.path.exists", return_value=False):
            with patch.dict("os.environ", {}, clear=True):
                cmd = ["/usr/bin/python3", "-c", "print('hello')"]
                result = wrap_command_for_sandbox(cmd)
                assert result == cmd
    finally:
        _reset_cache()


def test_wrap_does_not_mutate_original():
    _reset_cache()
    try:
        with patch("plugin.scripting.venv_worker.os.path.exists", return_value=True):
            with patch.dict("os.environ", {}, clear=True):
                cmd = ["/usr/bin/python3", "script.py"]
                original = cmd.copy()
                wrap_command_for_sandbox(cmd)
                assert cmd == original
    finally:
        _reset_cache()


@patch("plugin.scripting.venv_worker.sys.platform", "linux")
@patch("fcntl.fcntl")
def test_optimize_pipe_calls_fcntl(mock_fcntl: MagicMock) -> None:
    optimize_pipe(7)
    mock_fcntl.assert_called_once()
    args = mock_fcntl.call_args[0]
    assert args[0] == 7
    assert args[2] == _PIPE_BUF_TARGET


@patch("plugin.scripting.venv_worker.sys.platform", "linux")
@patch("fcntl.fcntl", side_effect=OSError("cap denied"))
def test_optimize_pipe_swallows_oserror(_mock_fcntl: MagicMock) -> None:
    optimize_pipe(3)


@patch("plugin.scripting.venv_worker.optimize_pipe")
def test_optimize_popen_pipes_iterates_streams(mock_optimize: MagicMock) -> None:
    proc = MagicMock()
    proc.stdin.fileno.return_value = 10
    proc.stdout.fileno.return_value = 11
    proc.stderr.fileno.return_value = 12
    optimize_popen_pipes(proc)
    assert mock_optimize.call_count == 3
    mock_optimize.assert_any_call(10)
    mock_optimize.assert_any_call(11)
    mock_optimize.assert_any_call(12)


@patch("plugin.scripting.venv_worker.optimize_pipe")
def test_optimize_popen_pipes_skips_none_streams(mock_optimize: MagicMock) -> None:
    proc = MagicMock()
    proc.stdin = None
    proc.stdout.fileno.return_value = 11
    proc.stderr = None
    optimize_popen_pipes(proc)
    mock_optimize.assert_called_once_with(11)


@patch("plugin.scripting.venv_worker.sys.platform", "win32")
@patch("fcntl.fcntl")
def test_optimize_pipe_noop_on_windows(mock_fcntl: MagicMock) -> None:
    optimize_pipe(5)
    mock_fcntl.assert_not_called()


@patch("plugin.scripting.venv_worker.sys.platform", "darwin")
@patch("fcntl.fcntl")
def test_optimize_pipe_noop_on_macos(mock_fcntl: MagicMock) -> None:
    optimize_pipe(5)
    mock_fcntl.assert_not_called()

