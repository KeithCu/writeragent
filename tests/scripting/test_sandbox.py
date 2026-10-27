# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""Tests for plugin.scripting.sandbox (import blocklist host utilities + path resolution)."""

from __future__ import annotations

import stat
import sys
from unittest.mock import patch
import pytest

from plugin.scripting.sandbox import (
    _reset_cache,
    detect_sandbox,
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


def test_resolve_venv_python_accepts_uppercase_file_url(tmp_path):
    venv = tmp_path / "José Doe (work)" / ".venv"
    py = _make_posix_venv(venv)
    uri = venv.as_uri()
    assert resolve_venv_python("FILE:" + uri[len("file:"):]) == str(py)


def test_path_from_file_url_scheme_case_and_unc(monkeypatch):
    from plugin.scripting.sandbox import _path_from_file_url

    # Check the POSIX rules first, even on Windows; the nt rules (UNC paths) are below.
    monkeypatch.setattr("plugin.scripting.sandbox.os.name", "posix")
    assert _path_from_file_url("FILE:///tmp/x") == "/tmp/x"
    assert _path_from_file_url("File:/tmp/x") == "/tmp/x"
    assert _path_from_file_url("file://localhost/tmp/x") == "/tmp/x"
    assert _path_from_file_url("file://server/share/a") == "/share/a"
    assert _path_from_file_url("file:tmp") is None
    assert _path_from_file_url("FILE:") is None

    monkeypatch.setattr("plugin.scripting.sandbox.os.name", "nt")
    assert _path_from_file_url("file:///C:/x") == r"C:\x"
    assert _path_from_file_url("FILE:///C:/x") == r"C:\x"
    assert _path_from_file_url("file://localhost/C:/x") == r"C:\x"
    assert _path_from_file_url("file://LocalHost/C:/Users/a") == r"C:\Users\a"
    assert _path_from_file_url("file://server/share/a") == r"\\server\share\a"
    assert _path_from_file_url("file://server/C:/x") == r"\\server\C:\x"


def test_normalize_venv_path_input_uppercase_file_scheme():
    from plugin.scripting.sandbox import _normalize_venv_path_input

    assert _normalize_venv_path_input("FILE:///tmp/x") == "/tmp/x"
    assert _normalize_venv_path_input('"FILE:///tmp/x"') == "/tmp/x"


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


def test_resolve_venv_python_rejects_python_prefix_files(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir(parents=True)
    fake = bindir / "pythonista"
    fake.write_text("#!/bin/sh\necho fake\n")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    assert resolve_venv_python(str(fake)) is None

    pybin = bindir / "python.bin"
    pybin.write_text("#!/bin/sh\necho real\n")
    pybin.chmod(pybin.stat().st_mode | stat.S_IEXEC)
    assert resolve_venv_python(str(pybin)) == str(pybin)


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


def test_wrap_flatpak_forwards_env_and_cwd():
    cmd = ["/py", "a"]
    _reset_cache()
    try:
        with patch("plugin.scripting.sandbox.os.path.exists", return_value=True):
            with patch.dict("os.environ", {}, clear=True):
                res = wrap_command_for_sandbox(cmd, env={"PATH": "/usr/bin", "A": "b=c"}, cwd="/work")
                assert res == [
                    "flatpak-spawn",
                    "--host",
                    "--clear-env",
                    "--directory=/work",
                    "--env=PATH=/usr/bin",
                    "--env=A=b=c",
                    "/py",
                    "a",
                ]

                res_empty_env = wrap_command_for_sandbox(cmd, env={})
                assert res_empty_env == [
                    "flatpak-spawn",
                    "--host",
                    "--clear-env",
                    "/py",
                    "a",
                ]

        # Outside Flatpak
        with patch("plugin.scripting.sandbox.os.path.exists", return_value=False):
            with patch.dict("os.environ", {}, clear=True):
                _reset_cache()
                res_outside = wrap_command_for_sandbox(cmd, env={"PATH": "/usr/bin", "A": "b=c"}, cwd="/work")
                assert res_outside == ["/py", "a"]

        # Snap with env=
        with patch("plugin.scripting.sandbox.os.path.exists", return_value=False):
            with patch.dict("os.environ", {"SNAP_NAME": "libreoffice"}, clear=True):
                _reset_cache()
                res_snap = wrap_command_for_sandbox(cmd, env={"PATH": "/usr/bin", "A": "b=c"}, cwd="/work")
                assert res_snap == ["/py", "a"]
    finally:
        _reset_cache()


def test_wrap_flatpak_watch_bus():
    cmd = ["/py", "a"]
    _reset_cache()
    try:
        with patch("plugin.scripting.sandbox.os.path.exists", return_value=True):
            with patch.dict("os.environ", {}, clear=True):
                res_bus = wrap_command_for_sandbox(cmd, env={"A": "b"}, cwd="/work", watch_bus=True)
                assert res_bus == [
                    "flatpak-spawn",
                    "--host",
                    "--watch-bus",
                    "--clear-env",
                    "--directory=/work",
                    "--env=A=b",
                    "/py",
                    "a",
                ]
                res_no_bus = wrap_command_for_sandbox(cmd, env={"A": "b"}, cwd="/work", watch_bus=False)
                assert "--watch-bus" not in res_no_bus
                assert res_no_bus == [
                    "flatpak-spawn",
                    "--host",
                    "--clear-env",
                    "--directory=/work",
                    "--env=A=b",
                    "/py",
                    "a",
                ]

        with patch("plugin.scripting.sandbox.os.path.exists", return_value=False):
            with patch.dict("os.environ", {}, clear=True):
                _reset_cache()
                res_outside = wrap_command_for_sandbox(cmd, env={"A": "b"}, cwd="/work", watch_bus=True)
                assert res_outside == ["/py", "a"]
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
    """writeragent.X follows plugin.X. A blanket writeragent.* is ignored and public aliases are gated separately."""
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

    assert import_authorized("writeragent.vision", ()) is True
    assert import_authorized("writeragent.scripting.duckdb_sql", ()) is True
    assert import_authorized("writeragent.vision.venv.vision", ()) is False
    assert import_authorized("plugin.vision", ()) is False
    assert import_authorized("plugin.scripting.duckdb_sql", ()) is False
    assert import_authorized("writeragent.framework.config", ()) is False


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
    assert import_authorized("import-not-used", ()) is True
    assert import_authorized("subprocess", ()) is False
    assert import_authorized("writeragent.framework.config", ()) is False
    assert import_authorized("writeragent.framework.client.llm_client", ()) is False
    assert import_authorized("writeragent.scripting.analysis", ()) is True
    assert import_authorized("writeragent.vision", ()) is True
    assert import_authorized("writeragent.scripting.duckdb_sql", ()) is True
    assert import_authorized("duckdb", ()) is True
    assert import_authorized("duckdb.functional", ()) is True
    assert import_authorized("plugin.scripting.duckdb_sql", ()) is False


def test_import_authorized_intermediate_nodes_rejected() -> None:
    """Intermediate nodes (plugin, plugin.scripting, writeragent.scripting) must not be authorized."""
    from plugin.scripting.sandbox import import_authorized

    assert import_authorized("plugin", ()) is False
    assert import_authorized("plugin.scripting", ()) is False
    assert import_authorized("writeragent.scripting", ()) is False
    assert import_authorized("plugin.scripting.analysis", ()) is True
    assert import_authorized("writeragent.scripting.analysis", ()) is True

    # Wildcard prefix tests: 'a.b.*' must allow 'a.b' and 'a.b.x' but reject 'a' or 'a.bc'.
    policy = ["a.b.*"]
    blocked = ["a"]
    assert import_authorized("a", policy, blocked_imports=blocked) is False
    assert import_authorized("a.b", policy, blocked_imports=blocked) is True
    assert import_authorized("a.b.c", policy, blocked_imports=blocked) is True
    assert import_authorized("a.b.c.d", policy, blocked_imports=blocked) is True
    assert import_authorized("a.bc", policy, blocked_imports=blocked) is False


def test_import_authorized_ignores_bare_star_and_allows_future_and_collections_abc() -> None:
    from plugin.scripting.sandbox import BASE_BUILTIN_MODULES, import_authorized

    assert import_authorized("__future__", BASE_BUILTIN_MODULES) is True
    assert import_authorized("collections.abc", BASE_BUILTIN_MODULES) is True
    assert import_authorized("collections", BASE_BUILTIN_MODULES) is True
    assert import_authorized("concurrent.futures", BASE_BUILTIN_MODULES) is True
    assert import_authorized("json.tool", BASE_BUILTIN_MODULES) is True
    assert import_authorized("sys", BASE_BUILTIN_MODULES) is True
    assert import_authorized("io", BASE_BUILTIN_MODULES) is True
    assert import_authorized("pathlib", BASE_BUILTIN_MODULES) is True
    assert import_authorized("pickle", BASE_BUILTIN_MODULES) is False
    assert import_authorized("subprocess", BASE_BUILTIN_MODULES) is False
    assert import_authorized("subprocess", ["*"]) is False
    assert import_authorized("plugin.framework.config", ["*"]) is False
    assert import_authorized("math", ["*", "math"]) is True
    assert import_authorized("matplotlib.pyplot", ["matplotlib"]) is True
    assert import_authorized("matplotlib.pyplot", ["matplotlib.*"]) is True


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
    assert _env_name_is_credential("MYSQL_PWD") is True
    assert _env_name_is_credential("GPG_PASSPHRASE") is True
    assert _env_name_is_credential("PASSWD") is True
    assert _env_name_is_credential("GITHUB_PAT") is True
    assert _env_name_is_credential("SLACK_WEBHOOK_URL") is True
    assert _env_name_is_credential("APIKEY") is True
    assert _env_name_is_credential("OPENAIAPIKEY") is True
    assert _env_name_is_credential("GITHUBAPITOKEN") is True
    assert _env_name_is_credential("AWSSECRETKEY") is True
    assert _env_name_is_credential("OPENAI-APIKEY") is True
    assert _env_name_is_credential("OPENAI_API_KEY") is True
    assert _env_name_is_credential("AWS_ACCESS_KEY_ID") is True
    assert _env_name_is_credential("KEYRING") is True
    assert _env_name_is_credential("AUTHORIZATION") is True

    # Allowed tokens/names
    assert _env_name_is_credential("PWD") is False
    assert _env_name_is_credential("TOKENIZERS_PARALLELISM") is False
    assert _env_name_is_credential("MONKEY") is False
    assert _env_name_is_credential("TURKEY") is False
    assert _env_name_is_credential("MONKEY_PATCH") is False
    assert _env_name_is_credential("SECRETARY") is False
    assert _env_name_is_credential("KEYWORD") is False
    assert _env_name_is_credential("AUTHORITY") is False
    assert _env_name_is_credential("PATH") is False
    assert _env_name_is_credential("KEYBOARD") is False
    assert _env_name_is_credential("KEYBOARD_LAYOUT") is False
    assert _env_name_is_credential("KEYBOARD_MODEL") is False
    assert _env_name_is_credential("XAUTHORITY") is False


def test_scrub_drops_dyld_and_python_executable() -> None:
    from plugin.scripting.sandbox import _BLOCKED_ENV_EXACT, scrub_subprocess_env

    for var in (
        "DYLD_LIBRARY_PATH",
        "DYLD_FRAMEWORK_PATH",
        "DYLD_FALLBACK_LIBRARY_PATH",
        "DYLD_FALLBACK_FRAMEWORK_PATH",
        "PYTHONEXECUTABLE",
        "PYTHONNOUSERSITE",
    ):
        assert var in _BLOCKED_ENV_EXACT

    base = {
        "DYLD_LIBRARY_PATH": "/lib",
        "DYLD_FRAMEWORK_PATH": "/frameworks",
        "DYLD_FALLBACK_LIBRARY_PATH": "/fallback",
        "DYLD_FALLBACK_FRAMEWORK_PATH": "/fallback_fw",
        "PYTHONEXECUTABLE": "/usr/bin/python",
        "PYTHONNOUSERSITE": "1",
        "PATH": "/usr/bin:/bin",
    }
    scrubbed = scrub_subprocess_env(base)
    for var in (
        "DYLD_LIBRARY_PATH",
        "DYLD_FRAMEWORK_PATH",
        "DYLD_FALLBACK_LIBRARY_PATH",
        "DYLD_FALLBACK_FRAMEWORK_PATH",
        "PYTHONEXECUTABLE",
        "PYTHONNOUSERSITE",
    ):
        assert var not in scrubbed
    assert scrubbed["PATH"] == "/usr/bin:/bin"


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
        "OPENAIAPIKEY": "sk-test-glued",
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
    assert "OPENAIAPIKEY" not in scrubbed
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
    assert _is_acceptable_python_basename("python.bin") is True
    assert _is_acceptable_python_basename("Python.BIN") is True
    assert _is_acceptable_python_basename("python3.exe") is True
    assert _is_acceptable_python_basename("python3.13.exe") is True
    assert _is_acceptable_python_basename("python3.12m") is True
    assert _is_acceptable_python_basename("python3.13t.exe") is True
    assert _is_acceptable_python_basename("python3.13d") is True

    assert _is_acceptable_python_basename("pythonista") is False
    assert _is_acceptable_python_basename("python-magic") is False
    assert _is_acceptable_python_basename("python_settings") is False
    assert _is_acceptable_python_basename("python39") is False
    assert _is_acceptable_python_basename("python3.12.1") is False
    assert _is_acceptable_python_basename("python.bin.exe") is False


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


def test_writeragent_alias_authorization_matches_mirrored_plugin_entry() -> None:
    from plugin.scripting.sandbox import VENV_AUTHORIZED_IMPORTS, import_authorized

    for entry in VENV_AUTHORIZED_IMPORTS:
        if not entry.startswith("plugin."):
            continue
        alias = "writeragent." + entry[len("plugin.") :]
        assert import_authorized(alias, ()) == import_authorized(
            entry, ()
        )
        if entry.endswith(".*"):
            prefix = entry[: -2]
            child = prefix + ".child"
            alias_child = "writeragent." + child[len("plugin.") :]
            assert import_authorized(alias_child, ()) == import_authorized(
                child, ()
            )
    assert import_authorized("writeragent.vision", ()) is True
    assert import_authorized("plugin.vision", ()) is False
    assert import_authorized("writeragent.scripting.duckdb_sql", ()) is True
    assert import_authorized("plugin.scripting.duckdb_sql", ()) is False


def test_sandbox_policy_module_yaml_keys() -> None:
    from pathlib import Path
    import yaml

    yaml_path = Path(__file__).resolve().parents[2] / "plugin" / "scripting" / "module.yaml"
    with open(yaml_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    cfg = data.get("config", {})

    for old_key in (
        "python_allow_file_writes",
        "python_allow_subprocess",
        "python_allow_run_programs",
        "python_allow_file_os",
    ):
        assert old_key not in cfg

    net = cfg.get("python_allow_network")
    assert net is not None
    assert net.get("default") is False
    assert net.get("label") == "Allow network access"
    assert "network connections" in net.get("helper", "")
    assert net.get("public") is True

    files = cfg.get("python_allow_files")
    assert files is not None
    assert files.get("default") is False
    assert files.get("label") == "Allow file access"
    assert "read and write" in files.get("helper", "")
    assert files.get("public") is True

    os_key = cfg.get("python_allow_os")
    assert os_key is not None
    assert os_key.get("default") is False
    assert os_key.get("label") == "Allow OS access"
    assert "file read and write" in os_key.get("helper", "")
    assert os_key.get("public") is True

    extra = cfg.get("python_extra_modules")
    assert extra is not None
    assert extra.get("default") == ""
    assert extra.get("label") == "Extra allowed modules"
    assert "file, network, and process" in extra.get("helper", "")
    assert extra.get("public") is True


def test_sandbox_policy_defaults() -> None:
    from plugin.scripting.sandbox import (
        build_sandbox_policy,
        policy_allowlist,
        policy_blocked_imports,
        policy_relaxed_roots,
    )

    p = build_sandbox_policy()
    assert policy_allowlist(p) == []
    blocked = policy_blocked_imports(p)
    assert "subprocess" in blocked
    assert "multiprocessing" in blocked
    assert "pty" in blocked
    assert "socket" in blocked
    assert "importlib" in blocked
    assert "pickle" in blocked
    assert "os" not in blocked
    assert policy_relaxed_roots(p) == frozenset()


def test_sandbox_policy_network() -> None:
    from plugin.scripting.sandbox import (
        NETWORK_BLOCKED_MODULES,
        build_sandbox_policy,
        import_authorized,
        policy_allowlist,
        policy_relaxed_roots,
    )

    p = build_sandbox_policy(network=True)
    allowed = policy_allowlist(p)
    for mod in NETWORK_BLOCKED_MODULES:
        assert mod in allowed
    assert import_authorized("socket", allowed) is True
    assert import_authorized("http.client", allowed) is True
    assert import_authorized("urllib.request", allowed) is True
    assert import_authorized("ssl", allowed) is True
    assert import_authorized("requests", allowed) is True
    assert import_authorized("httpx", allowed) is True
    assert policy_relaxed_roots(p) == frozenset({"socket"})


def test_sandbox_policy_files() -> None:
    from plugin.scripting.sandbox import (
        build_sandbox_policy,
        import_authorized,
        policy_allowlist,
        policy_relaxed_roots,
    )

    p = build_sandbox_policy(files=True)
    allowed = policy_allowlist(p)
    assert import_authorized("os", allowed) is True
    assert import_authorized("os.path", allowed) is True
    assert import_authorized("posixpath", allowed) is True
    assert import_authorized("pathlib", allowed) is True
    assert import_authorized("pathlib.Path", allowed) is True
    assert import_authorized("shutil", allowed) is True
    assert import_authorized("io", allowed) is True
    assert import_authorized("tempfile", allowed) is True
    assert import_authorized("sqlite3", allowed) is True
    assert import_authorized("glob", allowed) is True
    assert import_authorized("subprocess", allowed) is False
    assert policy_relaxed_roots(p) == frozenset({"shutil", "pathlib", "io"})


def test_sandbox_policy_os() -> None:
    from plugin.scripting.sandbox import (
        build_sandbox_policy,
        import_authorized,
        policy_allowlist,
        policy_relaxed_roots,
    )

    p = build_sandbox_policy(os_=True)
    assert p.files is True
    allowed = policy_allowlist(p)
    assert import_authorized("os", allowed) is True
    assert import_authorized("os.path", allowed) is True
    assert import_authorized("subprocess", allowed) is True
    assert import_authorized("subprocess.run", allowed) is True
    assert import_authorized("multiprocessing", allowed) is True
    assert import_authorized("multiprocessing.pool", allowed) is True
    assert import_authorized("pty", allowed) is True
    assert policy_relaxed_roots(p) == frozenset({
        "shutil",
        "pathlib",
        "io",
        "os",
        "posix",
        "nt",
        "subprocess",
        "multiprocessing",
        "_multiprocessing",
        "pty",
    })


def test_sandbox_policy_extra_modules() -> None:
    from plugin.scripting.sandbox import (
        build_sandbox_policy,
        import_authorized,
        parse_extra_modules,
        policy_allowlist,
        policy_relaxed_roots,
    )

    extra = parse_extra_modules("requests")
    assert extra == ("requests",)
    p = build_sandbox_policy(extra=extra)
    allowed = policy_allowlist(p)
    assert import_authorized("requests", allowed) is True
    assert import_authorized("requests.adapters", allowed) is True
    assert policy_relaxed_roots(p) == frozenset({"requests"})


def test_parse_extra_modules_validation() -> None:
    from plugin.scripting.sandbox import parse_extra_modules

    for listed in ("os", "os.path", "posix", "nt", "socket", "subprocess.run", "multiprocessing"):
        assert parse_extra_modules(listed) == (listed,)

    bad_names = [
        "*",
        "writeragent.*",
        "bad-token",
        "foo..bar",
        "123bad",
        "a" * 63,
    ]
    for bad in bad_names:
        with pytest.raises(ValueError):
            parse_extra_modules(bad)

    valid_62 = "a" * 62
    assert parse_extra_modules(valid_62) == (valid_62,)


def test_sandbox_policy_digest() -> None:
    from plugin.scripting.sandbox import build_sandbox_policy, policy_digest

    p1 = build_sandbox_policy(network=True, files=True, extra=("requests",))
    p2 = build_sandbox_policy(network=True, files=True, extra=("requests",))
    assert policy_digest(p1) == policy_digest(p2)

    d0 = policy_digest(build_sandbox_policy())
    d_net = policy_digest(build_sandbox_policy(network=True))
    d_files = policy_digest(build_sandbox_policy(files=True))
    d_os = policy_digest(build_sandbox_policy(os_=True))
    d_extra = policy_digest(build_sandbox_policy(extra=("foo",)))
    assert len({d0, d_net, d_files, d_os, d_extra}) == 5


def test_policy_blocked_imports_drops_flag_and_extra_entries() -> None:
    from plugin.scripting.sandbox import (
        FILE_BLOCKED_MODULES,
        NETWORK_BLOCKED_MODULES,
        build_sandbox_policy,
        policy_blocked_imports,
    )

    default_blocked = set(policy_blocked_imports(build_sandbox_policy()))
    assert "subprocess" in default_blocked
    assert "socket" in default_blocked
    assert "shutil" in default_blocked
    assert "os" not in default_blocked

    net_blocked = set(policy_blocked_imports(build_sandbox_policy(network=True)))
    for mod in NETWORK_BLOCKED_MODULES:
        assert mod not in net_blocked

    files_blocked = set(policy_blocked_imports(build_sandbox_policy(files=True)))
    for mod in FILE_BLOCKED_MODULES:
        assert mod not in files_blocked
    assert "os" not in files_blocked

    os_blocked = set(policy_blocked_imports(build_sandbox_policy(os_=True)))
    assert "subprocess" not in os_blocked

    extra_blocked = set(policy_blocked_imports(build_sandbox_policy(extra=("requests",))))
    assert "requests" not in extra_blocked


def test_import_authorized_default_allows_safe_stdlib() -> None:
    from plugin.scripting.sandbox import import_authorized

    for safe_mod in (
        "io",
        "logging",
        "glob",
        "zipfile",
        "gzip",
        "bz2",
        "lzma",
        "asyncio",
        "inspect",
        "sys",
        "signal",
        "concurrent.futures",
        "pathlib",
    ):
        assert import_authorized(safe_mod, ()) is True

    for blocked_mod in ("subprocess", "pickle", "importlib", "builtins", "ctypes"):
        assert import_authorized(blocked_mod, ()) is False


def test_extra_modules_override_name_star() -> None:
    from plugin.scripting.sandbox import import_authorized

    assert import_authorized("subprocess", ()) is False
    assert import_authorized("subprocess.run", ["subprocess"]) is True
    assert import_authorized("subprocess", ["subprocess"]) is True

    assert import_authorized("a", ["a.b"], blocked_imports=["a"]) is False
    assert import_authorized("a.b", ["a.b"], blocked_imports=["a"]) is True
    assert import_authorized("a.b.c", ["a.b"], blocked_imports=["a"]) is True


def test_plugin_public_alias_allowed_but_framework_blocked() -> None:
    from plugin.scripting.sandbox import import_authorized

    assert import_authorized("plugin.scripting.analysis", ()) is True
    assert import_authorized("writeragent.scripting.analysis", ()) is True
    assert import_authorized("plugin.framework.config", ()) is False
    assert import_authorized("writeragent.framework.config", ()) is False



