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
