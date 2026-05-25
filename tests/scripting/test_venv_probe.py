# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

from __future__ import annotations

import stat
import sys
from unittest.mock import MagicMock, patch

import pytest

from plugin.scripting.python_worker_manager import PythonWorkerManager
from plugin.scripting.venv_probe import probe_venv_path, resolve_libreoffice_python, resolve_venv_python, run_venv_self_check


@pytest.fixture(autouse=True)
def _shutdown_python_workers():
    yield
    PythonWorkerManager.shutdown_all()


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
    with patch("plugin.scripting.venv_probe.resolve_libreoffice_python", return_value="/fake/lo/python") as mock_res:
        with patch("plugin.scripting.venv_probe.run_venv_self_check", return_value=(True, "ignored")) as mock_check:
            ok, msg = probe_venv_path("  ")
    assert ok is True
    assert "LibreOffice process Python" in msg
    assert "/fake/lo/python" in msg
    mock_res.assert_called_once()
    mock_check.assert_called_once_with("/fake/lo/python", timeout=10.0)


def test_probe_venv_path_blank_fails_when_no_process_interpreter():
    with patch("plugin.scripting.venv_probe.resolve_libreoffice_python", return_value=None):
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
    with patch("plugin.scripting.venv_probe.PythonWorkerManager.get", return_value=mock_mgr):
        ok, msg = run_venv_self_check("/fake/python", timeout=1.0)
    assert ok is False
    assert "boom" in msg


def test_run_venv_self_check_worker_error_response():
    mock_mgr = MagicMock()
    mock_mgr.execute.return_value = {"status": "error", "message": "nope"}
    with patch("plugin.scripting.venv_probe.PythonWorkerManager.get", return_value=mock_mgr):
        ok, msg = run_venv_self_check("/x/python", timeout=1.0)
    assert ok is False
    assert "nope" in msg


def test_run_venv_self_check_timeout():
    mock_mgr = MagicMock()
    mock_mgr.execute.return_value = {
        "status": "error",
        "message": "Python worker failed: Command timed out after 1 seconds",
    }
    with patch("plugin.scripting.venv_probe.PythonWorkerManager.get", return_value=mock_mgr):
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
    from plugin.scripting.venv_probe import _format_self_check_success
    data = {"v": "3.12.0", "arch": "ARM64", "p": {}, "sci": [], "ui": []}
    msg = _format_self_check_success(data)
    assert "Python 3.12.0 (ARM64)" in msg
    assert "responds OK" in msg


def test_format_self_check_success_without_arch():
    from plugin.scripting.venv_probe import _format_self_check_success
    data = {"v": "3.11.5", "p": {}, "sci": [], "ui": []}
    msg = _format_self_check_success(data)
    assert "Python 3.11.5 responds OK" in msg
    assert "(" not in msg.split("\n")[0]
