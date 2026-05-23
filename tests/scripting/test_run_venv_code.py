# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""Tests for venv execution via PythonWorkerManager + worker_harness."""

from __future__ import annotations

import json
import subprocess
import sys
from unittest.mock import MagicMock, patch

import pytest

from plugin.scripting.python_worker_manager import PythonWorkerManager
from plugin.scripting.worker_harness import _execute_request, _serialize
from plugin.tests.testing_utils import setup_uno_mocks

setup_uno_mocks()


def test_serialize_numpy_scalar():
    np = pytest.importorskip("numpy")
    assert _serialize(np.int64(7)) == 7


def test_execute_request_fresh_namespace():
    r1 = _execute_request("x = 41\nresult = x + 1", None)
    assert r1["status"] == "ok"
    assert r1["result"] == 42
    r2 = _execute_request("result = x + 1", None)
    assert r2["status"] == "error"


def test_execute_request_injects_data():
    r = _execute_request("result = sum(data)", [1, 2, 3, 4])
    assert r["status"] == "ok"
    assert r["result"] == 10


def test_blocked_import_os():
    r = _execute_request("import os\nresult = 1", None)
    assert r["status"] == "error"
    assert "not allowed" in r.get("message", "").lower() or "Import" in r.get("message", "")


def test_blocked_import_not_on_allowlist():
    pytest.importorskip("requests")
    r = _execute_request("import requests\nresult = 1", None)
    assert r["status"] == "error"
    assert "not allowed" in r.get("message", "").lower() or "Import" in r.get("message", "")


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



@patch("plugin.scripting.run_venv_code.get_config_str", return_value="")
@patch("plugin.scripting.run_venv_code.resolve_libreoffice_python", return_value=sys.executable)
def test_run_code_uses_manager(mock_lo_python, mock_cfg):
    from plugin.scripting.run_venv_code import run_code_in_user_venv

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


def test_split_grid_data_round_trip_execute_request():
    """Ingress split_grid: child receives ndarray from frombuffer."""
    np = pytest.importorskip("numpy")
    grid = [[float(r * 10 + c) for c in range(4)] for r in range(4)]
    from plugin.calc.calc_addin_data import pack_calc_data_for_wire
    from plugin.scripting.payload_codec import is_split_grid

    wire = pack_calc_data_for_wire(grid)
    assert is_split_grid(wire)
    r = _execute_request("result = float(data.sum())", wire)
    assert r["status"] == "ok"
    assert r["result"] == pytest.approx(sum(r * 10 + c for r in range(4) for c in range(4)))


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


@patch("plugin.scripting.run_venv_code.configured_python_exec_timeout", return_value=10)
@patch("plugin.scripting.run_venv_code.get_config_str", return_value="")
@patch("plugin.scripting.run_venv_code.resolve_libreoffice_python", return_value=sys.executable)
@patch("plugin.scripting.python_worker_manager.PythonWorkerManager.execute")
def test_run_venv_code_timeout_capped(mock_execute, mock_lo_python, mock_cfg, mock_configured_timeout):
    from plugin.scripting.run_venv_code import run_code_in_user_venv
    ctx = MagicMock()

    # Call with no timeout and verify it gets default timeout of 10s
    run_code_in_user_venv(ctx, "result = 1")
    mock_execute.assert_called_once_with("result = 1", data=None, timeout_sec=10)

    mock_execute.reset_mock()

    # Call with a custom timeout in the allowed range (e.g. 100s) and verify it is allowed
    run_code_in_user_venv(ctx, "result = 1", timeout_sec=100)
    mock_execute.assert_called_once_with("result = 1", data=None, timeout_sec=100)

    mock_execute.reset_mock()

    # Call with a timeout exceeding 600s (e.g. 1000s) and verify it gets capped to 600s
    run_code_in_user_venv(ctx, "result = 1", timeout_sec=1000)
    mock_execute.assert_called_once_with("result = 1", data=None, timeout_sec=600)

    mock_execute.reset_mock()

    # Call with 0s timeout and verify it gets set to 1s floor
    run_code_in_user_venv(ctx, "result = 1", timeout_sec=0)
    mock_execute.assert_called_once_with("result = 1", data=None, timeout_sec=1)


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


def test_split_grid_integration_pickle_mode():
    np = pytest.importorskip("numpy")
    from plugin.scripting.python_worker_manager import PythonWorkerManager

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



