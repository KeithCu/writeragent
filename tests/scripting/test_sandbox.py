# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""Tests for plugin.scripting.sandbox (whitelist host utilities + path resolution)."""

from __future__ import annotations

import stat
import sys
import types
from unittest.mock import MagicMock, patch


if "fcntl" not in sys.modules:
    fake_fcntl = types.ModuleType("fcntl")
    fake_fcntl.F_SETPIPE_SZ = 1031
    fake_fcntl.fcntl = lambda *args, **kwargs: None
    sys.modules["fcntl"] = fake_fcntl
elif not hasattr(sys.modules["fcntl"], "F_SETPIPE_SZ"):
    setattr(sys.modules["fcntl"], "F_SETPIPE_SZ", 1031)

from plugin.scripting.sandbox import (
    _PIPE_BUF_TARGET,
    _reset_cache,
    detect_sandbox,
    optimize_pipe,
    optimize_popen_pipes,
    resolve_libreoffice_python,
    resolve_venv_python,
    wrap_command_for_sandbox,
)
import plugin.scripting.sandbox as sandbox

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


def test_resolve_venv_python_accepts_bin_python_path(tmp_path):
    venv = tmp_path / "venv"
    bindir = venv / "bin"
    bindir.mkdir(parents=True)
    py = bindir / "python3.12"
    py.write_text("#!/bin/sh\necho ok\n")
    py.chmod(py.stat().st_mode | stat.S_IEXEC)
    assert resolve_venv_python(str(py)) == str(py)


def test_resolve_venv_python_accepts_bin_directory(tmp_path):
    venv = tmp_path / "venv"
    bindir = venv / "bin"
    bindir.mkdir(parents=True)
    py = bindir / "python"
    py.write_text("#!/bin/sh\necho ok\n")
    py.chmod(py.stat().st_mode | stat.S_IEXEC)
    assert resolve_venv_python(str(bindir)) == str(py)


def _make_posix_venv(root):
    bindir = root / "bin"
    bindir.mkdir(parents=True)
    py = bindir / "python"
    py.write_text("#!/bin/sh\necho ok\n")
    py.chmod(py.stat().st_mode | stat.S_IEXEC)
    return py


def test_resolve_venv_python_spaces_parens_non_ascii(tmp_path):
    venv = tmp_path / "José Doe (work)" / ".venv"
    py = _make_posix_venv(venv)
    assert resolve_venv_python(str(venv)) == str(py)


def test_resolve_venv_python_strips_surrounding_quotes(tmp_path):
    venv = tmp_path / "José Doe (work)" / ".venv"
    py = _make_posix_venv(venv)
    assert resolve_venv_python(f'"{venv}"') == str(py)
    assert resolve_venv_python(f"'{venv}'") == str(py)


def test_resolve_venv_python_accepts_file_url(tmp_path):
    venv = tmp_path / "José Doe (work)" / ".venv"
    py = _make_posix_venv(venv)
    assert resolve_venv_python(venv.as_uri()) == str(py)


def test_resolve_venv_python_windows_scripts_layout(tmp_path, monkeypatch):
    monkeypatch.setattr("plugin.scripting.sandbox.os.name", "nt")
    venv = tmp_path / "venv"
    scripts = venv / "Scripts"
    scripts.mkdir(parents=True)
    py = scripts / "python.exe"
    py.write_text("")
    assert resolve_venv_python(str(venv)) == str(py)


def test_resolve_venv_python_env_root_python_exe(tmp_path, monkeypatch):
    """conda / pyenv-win: python.exe at env root; Scripts/ may exist without python.exe."""
    monkeypatch.setattr("plugin.scripting.sandbox.os.name", "nt")
    venv = tmp_path / "conda-env"
    (venv / "Scripts").mkdir(parents=True)
    py = venv / "python.exe"
    py.write_text("")
    assert resolve_venv_python(str(venv)) == str(py)


def test_resolve_venv_python_direct_python_exe(tmp_path, monkeypatch):
    monkeypatch.setattr("plugin.scripting.sandbox.os.name", "nt")
    py = tmp_path / "python.exe"
    py.write_text("")
    assert resolve_venv_python(str(py)) == str(py)


def test_resolve_venv_python_rejects_pythonw_exe(tmp_path, monkeypatch):
    monkeypatch.setattr("plugin.scripting.sandbox.os.name", "nt")
    venv = tmp_path / "venv"
    venv.mkdir()
    (venv / "pythonw.exe").write_text("")
    assert resolve_venv_python(str(venv)) is None
    assert resolve_venv_python(str(venv / "pythonw.exe")) is None


def test_resolve_venv_python_none_for_empty_dir(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    assert resolve_venv_python(str(empty)) is None


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
    monkeypatch.setattr(sandbox, "_bundled_lo_python_candidates", lambda: [])
    if sys.platform == "win32":
        assert resolve_libreoffice_python() == str(p)
    else:
        assert resolve_libreoffice_python() is None


def test_resolve_libreoffice_python_empty_string(monkeypatch):
    monkeypatch.setattr(sys, "executable", "")
    monkeypatch.setattr(sandbox, "_bundled_lo_python_candidates", lambda: [])
    assert resolve_libreoffice_python() is None


def test_resolve_libreoffice_python_nonexistent_path(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "executable", str(tmp_path / "does_not_exist"))
    monkeypatch.setattr(sandbox, "_bundled_lo_python_candidates", lambda: [])
    assert resolve_libreoffice_python() is None


def test_resolve_libreoffice_python_neighbor_of_soffice(tmp_path, monkeypatch):
    """Windows GHA 33752806292: sys.executable is soffice.exe; use sibling python.exe."""
    soffice = tmp_path / "soffice.exe"
    py = tmp_path / "python.exe"
    soffice.write_text("", encoding="utf-8")
    py.write_text("#!/bin/sh\n", encoding="utf-8")
    py.chmod(py.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setattr(sys, "executable", str(soffice))
    monkeypatch.setattr(sandbox, "_bundled_lo_python_candidates", lambda: [])
    assert resolve_libreoffice_python() == str(py)


def test_resolve_libreoffice_python_macos_resources(tmp_path, monkeypatch):
    """Darwin: Contents/MacOS/soffice → Contents/Resources/python (PR #561)."""
    macos = tmp_path / "Contents" / "MacOS"
    resources = tmp_path / "Contents" / "Resources"
    macos.mkdir(parents=True)
    resources.mkdir()
    soffice = macos / "soffice"
    py = resources / "python"
    soffice.write_text("", encoding="utf-8")
    py.write_text("#!/bin/sh\n", encoding="utf-8")
    py.chmod(py.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setattr(sys, "executable", str(soffice))
    monkeypatch.setattr(sandbox, "_bundled_lo_python_candidates", lambda: [])
    assert resolve_libreoffice_python() == str(py)


def test_resolve_libreoffice_python_empty_uses_bundled(tmp_path, monkeypatch):
    """Darwin soffice sys.executable is often empty (GHA 33749078050)."""
    py = tmp_path / "python"
    py.write_text("#!/bin/sh\n", encoding="utf-8")
    py.chmod(py.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setattr(sys, "executable", "")
    monkeypatch.setattr(sandbox, "_bundled_lo_python_candidates", lambda: [str(py)])
    assert resolve_libreoffice_python() == str(py)


# --- Subprocess spawn helper tests (relocated from test_subprocess_helpers.py) ---

def test_detect_flatpak_via_file():
    _reset_cache()
    try:
        with patch("plugin.scripting.sandbox.os.path.exists", return_value=True) as mock_exists:
            with patch.dict("os.environ", {}, clear=True):
                assert detect_sandbox() == "flatpak"
                mock_exists.assert_called_with("/.flatpak-info")
    finally:
        _reset_cache()


def test_detect_flatpak_via_env():
    _reset_cache()
    try:
        with patch("plugin.scripting.sandbox.os.path.exists", return_value=False):
            with patch.dict("os.environ", {"FLATPAK_ID": "org.libreoffice.LibreOffice"}, clear=True):
                assert detect_sandbox() == "flatpak"
    finally:
        _reset_cache()


def test_detect_snap():
    _reset_cache()
    try:
        with patch("plugin.scripting.sandbox.os.path.exists", return_value=False):
            with patch.dict("os.environ", {"SNAP_NAME": "libreoffice"}, clear=True):
                assert detect_sandbox() == "snap"
    finally:
        _reset_cache()


def test_detect_none():
    _reset_cache()
    try:
        with patch("plugin.scripting.sandbox.os.path.exists", return_value=False):
            with patch.dict("os.environ", {}, clear=True):
                assert detect_sandbox() is None
    finally:
        _reset_cache()


def test_result_is_cached():
    _reset_cache()
    try:
        with patch("plugin.scripting.sandbox.os.path.exists", return_value=True) as mock_exists:
            with patch.dict("os.environ", {}, clear=True):
                assert detect_sandbox() == "flatpak"
                assert detect_sandbox() == "flatpak"
                mock_exists.assert_called_once()
    finally:
        _reset_cache()


def test_wrap_flatpak():
    _reset_cache()
    try:
        with patch("plugin.scripting.sandbox.os.path.exists", return_value=True):
            with patch.dict("os.environ", {}, clear=True):
                cmd = ["/home/user/.venv/bin/python", "script.py"]
                result = wrap_command_for_sandbox(cmd)
                assert result == ["flatpak-spawn", "--host", "/home/user/.venv/bin/python", "script.py"]
    finally:
        _reset_cache()


def test_wrap_snap_unchanged():
    _reset_cache()
    try:
        with patch("plugin.scripting.sandbox.os.path.exists", return_value=False):
            with patch.dict("os.environ", {"SNAP_NAME": "libreoffice"}, clear=True):
                cmd = ["/home/user/.venv/bin/python", "script.py"]
                result = wrap_command_for_sandbox(cmd)
                assert result == cmd
    finally:
        _reset_cache()


def test_wrap_no_sandbox():
    _reset_cache()
    try:
        with patch("plugin.scripting.sandbox.os.path.exists", return_value=False):
            with patch.dict("os.environ", {}, clear=True):
                cmd = ["/usr/bin/python3", "-c", "print('hello')"]
                result = wrap_command_for_sandbox(cmd)
                assert result == cmd
    finally:
        _reset_cache()


def test_wrap_does_not_mutate_original():
    _reset_cache()
    try:
        with patch("plugin.scripting.sandbox.os.path.exists", return_value=True):
            with patch.dict("os.environ", {}, clear=True):
                cmd = ["/usr/bin/python3", "script.py"]
                original = cmd.copy()
                wrap_command_for_sandbox(cmd)
                assert cmd == original
    finally:
        _reset_cache()


@patch("plugin.scripting.sandbox.sys.platform", "linux")
@patch("fcntl.fcntl")
def test_optimize_pipe_calls_fcntl(mock_fcntl: MagicMock) -> None:
    optimize_pipe(7)
    mock_fcntl.assert_called_once()
    args = mock_fcntl.call_args[0]
    assert args[0] == 7
    assert args[2] == _PIPE_BUF_TARGET


@patch("plugin.scripting.sandbox.sys.platform", "linux")
@patch("fcntl.fcntl", side_effect=OSError("cap denied"))
def test_optimize_pipe_swallows_oserror(_mock_fcntl: MagicMock) -> None:
    optimize_pipe(3)


@patch("plugin.scripting.sandbox.optimize_pipe")
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


@patch("plugin.scripting.sandbox.optimize_pipe")
def test_optimize_popen_pipes_skips_none_streams(mock_optimize: MagicMock) -> None:
    proc = MagicMock()
    proc.stdin = None
    proc.stdout.fileno.return_value = 11
    proc.stderr = None
    optimize_popen_pipes(proc)
    mock_optimize.assert_called_once_with(11)


@patch("plugin.scripting.sandbox.sys.platform", "win32")
@patch("fcntl.fcntl")
def test_optimize_pipe_noop_on_windows(mock_fcntl: MagicMock) -> None:
    optimize_pipe(5)
    mock_fcntl.assert_not_called()


@patch("plugin.scripting.sandbox.sys.platform", "darwin")
@patch("fcntl.fcntl")
def test_optimize_pipe_noop_on_macos(mock_fcntl: MagicMock) -> None:
    optimize_pipe(5)
    mock_fcntl.assert_not_called()

