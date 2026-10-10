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
import pytest


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


@pytest.mark.parametrize(
    "value, value_2, expected",
    [
        pytest.param("FLATPAK_ID", "org.libreoffice.LibreOffice", "flatpak", id="test_detect_flatpak_via_env"),
        pytest.param("SNAP_NAME", "libreoffice", "snap", id="test_detect_snap"),
    ],
)
def test_detect_flatpak_via_env(value, value_2, expected):
    _reset_cache()
    try:
        with patch("plugin.scripting.sandbox.os.path.exists", return_value=False):
            with patch.dict("os.environ", {value: value_2}, clear=True):
                assert detect_sandbox() == expected
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


def test_scrub_env_and_workspace_path_accept_long_values(tmp_path) -> None:
    """PATH and workspace paths are longer than the old deal caps."""
    from plugin.framework.deal_shim import DEAL_MAX_ARGV, DEAL_MAX_PATH
    from plugin.scripting.sandbox import is_safe_workspace_path, scrub_subprocess_env

    env = scrub_subprocess_env({"PATH": "p" * (DEAL_MAX_ARGV + 1), "HOME": "/tmp"})
    assert env["PATH"] == "p" * (DEAL_MAX_ARGV + 1)
    assert env["PYTHONUTF8"] == "1"
    root = tmp_path / ("d" * 40)
    root.mkdir()
    target = "t" * (DEAL_MAX_PATH + 1)
    # A long relative name still resolves inside the root. The old pre
    # raised before that check.
    assert is_safe_workspace_path(target, str(root)) is True


def test_import_authorized_alias_uses_plugin_allowlist() -> None:
    """writeragent.X follows plugin.X. A blanket writeragent.* does not open config."""
    from plugin.scripting.sandbox import import_authorized

    allowed = [
        "plugin.scripting.analysis",
        "writeragent.*",
        "duckdb",
        "duckdb.*",
    ]
    assert import_authorized("writeragent.scripting.analysis", allowed) is True
    assert import_authorized("plugin.scripting.analysis", allowed) is True
    assert import_authorized("writeragent.framework.config", allowed) is False
    assert import_authorized("writeragent.framework.client.llm_client", allowed) is False
    assert import_authorized("plugin.framework.config", allowed) is False
    assert import_authorized("plugin.framework.client.llm_client", allowed) is False
    assert import_authorized("duckdb", allowed) is True
    assert import_authorized("duckdb.duckdb", allowed) is True


def test_import_authorized_explicit_alias_without_plugin_entry() -> None:
    from plugin.scripting.sandbox import import_authorized

    allowed = ["writeragent.vision", "writeragent.scripting.duckdb_sql", "writeragent.*"]
    assert import_authorized("writeragent.vision", allowed) is True
    assert import_authorized("writeragent.scripting.duckdb_sql", allowed) is True
    assert import_authorized("writeragent.vision.venv.vision", allowed) is False
    assert import_authorized("plugin.vision", allowed) is False
    assert import_authorized("plugin.scripting.duckdb_sql", allowed) is False
    assert import_authorized("writeragent.framework.config", allowed) is False


def test_venv_allowlist_mirrors_plugin_entries_and_keeps_duckdb() -> None:
    from plugin.scripting.sandbox import VENV_AUTHORIZED_IMPORTS, import_authorized

    assert "writeragent.*" not in VENV_AUTHORIZED_IMPORTS
    assert "duckdb" in VENV_AUTHORIZED_IMPORTS
    assert "duckdb.*" in VENV_AUTHORIZED_IMPORTS
    assert "plugin.scripting.duckdb_sql" not in VENV_AUTHORIZED_IMPORTS
    assert "plugin.vision.venv.vision" not in VENV_AUTHORIZED_IMPORTS
    for entry in VENV_AUTHORIZED_IMPORTS:
        if entry.startswith("plugin."):
            mirror = "writeragent." + entry[len("plugin."):]
            assert mirror in VENV_AUTHORIZED_IMPORTS
    assert import_authorized("import-not-used", VENV_AUTHORIZED_IMPORTS) is False
    assert import_authorized("writeragent.framework.config", VENV_AUTHORIZED_IMPORTS) is False
    assert import_authorized("writeragent.framework.client.llm_client", VENV_AUTHORIZED_IMPORTS) is False
    assert import_authorized("writeragent.scripting.analysis", VENV_AUTHORIZED_IMPORTS) is True
    assert import_authorized("writeragent.vision", VENV_AUTHORIZED_IMPORTS) is True
    assert import_authorized("writeragent.scripting.duckdb_sql", VENV_AUTHORIZED_IMPORTS) is True
    assert import_authorized("duckdb", VENV_AUTHORIZED_IMPORTS) is True
    assert import_authorized("duckdb.functional", VENV_AUTHORIZED_IMPORTS) is True
    assert import_authorized("plugin.scripting.duckdb_sql", VENV_AUTHORIZED_IMPORTS) is False


def test_import_authorized_intermediate_nodes_rejected() -> None:
    """Intermediate nodes (plugin, plugin.scripting, writeragent.scripting) must not be authorized."""
    from plugin.scripting.sandbox import VENV_AUTHORIZED_IMPORTS, import_authorized

    assert import_authorized("plugin", VENV_AUTHORIZED_IMPORTS) is False
    assert import_authorized("plugin.scripting", VENV_AUTHORIZED_IMPORTS) is False
    assert import_authorized("writeragent.scripting", VENV_AUTHORIZED_IMPORTS) is False
    assert import_authorized("plugin.scripting.analysis", VENV_AUTHORIZED_IMPORTS) is True
    assert import_authorized("writeragent.scripting.analysis", VENV_AUTHORIZED_IMPORTS) is True

    # Wildcard prefix tests: 'a.b.*' must allow 'a.b' and 'a.b.x' but reject 'a' or 'a.bc'.
    policy = ["a.b.*"]
    assert import_authorized("a", policy) is False
    assert import_authorized("a.b", policy) is True
    assert import_authorized("a.b.c", policy) is True
    assert import_authorized("a.b.c.d", policy) is True
    assert import_authorized("a.bc", policy) is False


def test_env_name_is_credential_matches_suffixes_and_dsn() -> None:
    """Tokens ending with blocked words, DATABASE_URL, and *_DSN must be detected while keeping KEYBOARD/XAUTHORITY."""
    from plugin.scripting.sandbox import _env_name_is_credential

    assert _env_name_is_credential("OPENAI_APIKEY") is True
    assert _env_name_is_credential("GITHUB_APITOKEN") is True
    assert _env_name_is_credential("AWS_SECRETKEY") is True
    assert _env_name_is_credential("DATABASE_URL") is True
    assert _env_name_is_credential("POSTGRES_DSN") is True
    assert _env_name_is_credential("SENTRY_DSN") is True
    assert _env_name_is_credential("DSN") is True
    assert _env_name_is_credential("KEY") is True
    assert _env_name_is_credential("MY_KEYS") is True

    # KEYBOARD device names and XAUTHORITY must not be blocked.
    assert _env_name_is_credential("KEYBOARD") is False
    assert _env_name_is_credential("KEYBOARD_LAYOUT") is False
    assert _env_name_is_credential("KEYBOARD_MODEL") is False
    assert _env_name_is_credential("XAUTHORITY") is False


def test_scrub_subprocess_env_blocked_exact_and_empty_dict() -> None:
    """PYTHONSTARTUP, PYTHONUSERBASE, PYTHONBREAKPOINT, PYTHONINSPECT are blocked, and empty dict gets overrides."""
    from plugin.scripting.sandbox import _BLOCKED_ENV_EXACT, scrub_subprocess_env

    for var in ("PYTHONSTARTUP", "PYTHONUSERBASE", "PYTHONBREAKPOINT", "PYTHONINSPECT", "DATABASE_URL", "WRITERAGENT_COMPUTE_WORKER"):
        assert var in _BLOCKED_ENV_EXACT

    base = {
        "PYTHONSTARTUP": "/etc/startup.py",
        "PYTHONUSERBASE": "/home/u/.local",
        "PYTHONBREAKPOINT": "0",
        "PYTHONINSPECT": "1",
        "DATABASE_URL": "postgres://user:pass@host/db",
        "WRITERAGENT_COMPUTE_WORKER": "1",
        "OPENAI_APIKEY": "sk-test",
        "POSTGRES_DSN": "postgres://...",
        "KEYBOARD_LAYOUT": "us",
        "USER_VAR": "hello",
    }
    scrubbed = scrub_subprocess_env(base)
    assert "PYTHONSTARTUP" not in scrubbed
    assert "PYTHONUSERBASE" not in scrubbed
    assert "PYTHONBREAKPOINT" not in scrubbed
    assert "PYTHONINSPECT" not in scrubbed
    assert "DATABASE_URL" not in scrubbed
    assert "WRITERAGENT_COMPUTE_WORKER" not in scrubbed
    assert "OPENAI_APIKEY" not in scrubbed
    assert "POSTGRES_DSN" not in scrubbed
    assert scrubbed["KEYBOARD_LAYOUT"] == "us"
    assert scrubbed["USER_VAR"] == "hello"

    # None returns empty; {} applies standard overrides.
    assert scrub_subprocess_env(None) == {}
    empty_scrubbed = scrub_subprocess_env({})
    assert empty_scrubbed.get("PYTHONUTF8") == "1"
    assert empty_scrubbed.get("PYTHONIOENCODING") == "utf-8"
    assert empty_scrubbed.get("PYTHONDONTWRITEBYTECODE") == "1"


def test_is_acceptable_python_basename_rejects_config() -> None:
    """python3.X-config scripts and pythonw must be rejected."""
    from plugin.scripting.sandbox import _is_acceptable_python_basename

    assert _is_acceptable_python_basename("python3.13-config") is False
    assert _is_acceptable_python_basename("python3-config") is False
    assert _is_acceptable_python_basename("python-config.exe") is False
    assert _is_acceptable_python_basename("pythonw") is False
    assert _is_acceptable_python_basename("pythonw.exe") is False
    assert _is_acceptable_python_basename("python") is True
    assert _is_acceptable_python_basename("python3") is True
    assert _is_acceptable_python_basename("python3.13") is True
    assert _is_acceptable_python_basename("python.exe") is True


def test_python_candidates_in_bin_dir_handles_oserror(tmp_path, monkeypatch) -> None:
    """OSError during os.listdir in bin dir must be swallowed."""
    from plugin.scripting.sandbox import _python_candidates_in_bin_dir

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()

    def mock_listdir(_path):
        raise OSError("Permission denied")

    monkeypatch.setattr("os.listdir", mock_listdir)
    candidates = _python_candidates_in_bin_dir(str(bin_dir))
    assert any("python" in c for c in candidates)


def test_detect_sandbox_cache_clear(monkeypatch) -> None:
    """detect_sandbox result is cached and _reset_cache clears it."""
    from plugin.scripting.sandbox import _reset_cache, detect_sandbox

    monkeypatch.delenv("FLATPAK_ID", raising=False)
    monkeypatch.delenv("SNAP_NAME", raising=False)
    monkeypatch.setattr("os.path.exists", lambda path: False)
    _reset_cache()

    assert detect_sandbox() is None

    monkeypatch.setenv("SNAP_NAME", "test-snap")
    # Still cached as None before reset
    assert detect_sandbox() is None

    _reset_cache()
    assert detect_sandbox() == "snap"
    _reset_cache()

