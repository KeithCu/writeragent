# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

from __future__ import annotations

import stat
import subprocess
import sys
from unittest.mock import patch

from plugin.scripting.venv_probe import probe_venv_path, resolve_libreoffice_python, resolve_venv_python, run_venv_self_check


def _fake_completed(returncode: int, stdout: str = "", stderr: str = ""):
    class R:
        pass

    r = R()
    r.returncode = returncode
    r.stdout = stdout
    r.stderr = stderr
    return r


def test_resolve_venv_python_finds_posix_python(tmp_path):
    venv = tmp_path / "venv"
    bindir = venv / "bin"
    bindir.mkdir(parents=True)
    py = bindir / "python"
    py.write_text("#!/bin/sh\necho ok\n")
    py.chmod(py.stat().st_mode | stat.S_IEXEC)
    got = resolve_venv_python(str(venv))
    assert got == str(py)


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


def test_run_venv_self_check_subprocess_error():
    with patch("plugin.scripting.venv_probe.subprocess.run", side_effect=OSError("boom")):
        ok, msg = run_venv_self_check("/fake/python", timeout=1.0)
    assert ok is False
    assert "boom" in msg


def test_run_venv_self_check_bad_exit():
    fake = _fake_completed(1, stdout="", stderr="nope")
    with patch("plugin.scripting.venv_probe.subprocess.run", return_value=fake):
        ok, msg = run_venv_self_check("/x/python", timeout=1.0)
    assert ok is False
    assert "1" in msg
    assert "nope" in msg


def test_run_venv_self_check_timeout():
    with patch(
        "plugin.scripting.venv_probe.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="x", timeout=1.0),
    ):
        ok, msg = run_venv_self_check("/x/python", timeout=1.0)
    assert ok is False
    assert "Timed out" in msg
