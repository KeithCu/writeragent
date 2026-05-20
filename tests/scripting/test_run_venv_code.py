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
    """Harness reads one JSON line and writes one response (subprocess smoke)."""
    harness = __import__("plugin.scripting.worker_harness", fromlist=["main"])
    proc = subprocess.Popen(
        [sys.executable, harness.__file__],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    req = json.dumps({"id": "t1", "code": "result = 2 ** 10"}) + "\n"
    out, err = proc.communicate(input=req, timeout=30)
    assert proc.returncode == 0, err
    line = out.strip().split("\n")[-1]
    resp = json.loads(line)
    assert resp["id"] == "t1"
    assert resp["status"] == "ok"
    assert resp["result"] == 1024


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


def test_f64_blob_data_round_trip_execute_request():
    """Ingress f64_blob: child receives ndarray from frombuffer."""
    np = pytest.importorskip("numpy")
    grid = [[float(r * 10 + c) for c in range(4)] for r in range(4)]
    from plugin.calc.calc_addin_data import pack_calc_data_for_wire
    from plugin.scripting.payload_codec import is_f64_blob

    wire = pack_calc_data_for_wire(grid)
    assert is_f64_blob(wire)
    r = _execute_request("result = float(data.sum())", wire)
    assert r["status"] == "ok"
    assert r["result"] == pytest.approx(sum(r * 10 + c for r in range(4) for c in range(4)))


def test_f64_blob_result_round_trip_harness():
    np = pytest.importorskip("numpy")
    r = _execute_request(
        "import numpy as np\nresult = np.arange(16, dtype=np.float64).reshape(4, 4)",
        None,
    )
    assert r["status"] == "ok"
    from plugin.scripting.payload_codec import is_f64_blob

    assert is_f64_blob(r["result"])
    from plugin.scripting.payload_codec import host_unpack_data

    back = host_unpack_data(r["result"])
    assert len(back) == 4
    assert back[0][0] == pytest.approx(0.0)
    assert back[3][3] == pytest.approx(15.0)


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


