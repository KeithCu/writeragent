# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import os

import plugin.testing_runner as tr


def test_parse_cli_user_profile_sets_flags(monkeypatch) -> None:
    monkeypatch.setattr(tr, "use_user_profile", False)
    monkeypatch.setattr(tr, "show_window", False)
    monkeypatch.delenv("WRITERAGENT_UNO_USER_PROFILE", raising=False)
    rest = tr._parse_cli_args(["--user-profile", "tests/chatbot/test_mock_llm_sidebar_uno.py"])
    assert tr.use_user_profile is True
    assert tr.show_window is True
    assert rest == ["tests/chatbot/test_mock_llm_sidebar_uno.py"]
    assert os.environ.get("WRITERAGENT_UNO_USER_PROFILE") == "1"


def test_soffice_strip_env_names() -> None:
    assert "PYTHONPATH" in tr._SOFFICE_STRIP_ENV
    assert "PYTHONHOME" in tr._SOFFICE_STRIP_ENV


def test_user_profile_child_env_disables_uno_thread_guard(monkeypatch) -> None:
    monkeypatch.setenv("PYTHONPATH", "/checkout")
    env = tr._child_env_without_runner_python(uno_thread_guard=False)
    assert "PYTHONPATH" not in env
    assert env.get("WRITERAGENT_UNO_THREAD_GUARD") == "0"


def test_parse_cli_default_is_headless_suite(monkeypatch) -> None:
    monkeypatch.setattr(tr, "use_user_profile", False)
    monkeypatch.setattr(tr, "show_window", False)
    monkeypatch.delenv("WRITERAGENT_UNO_USER_PROFILE", raising=False)
    rest = tr._parse_cli_args(["--visible", "test_charts_uno"])
    assert tr.use_user_profile is False
    assert tr.show_window is True
    assert rest == ["test_charts_uno"]


def test_user_profile_soffice_argv_skips_nodefault_and_restore() -> None:
    from pathlib import Path

    argv = tr._user_profile_soffice_argv(Path("/usr/bin/soffice"), "socket,host=127.0.0.1,port=9;urp;")
    assert "--norestore" in argv
    assert "--writer" in argv
    assert "--nologo" in argv
    assert "--nodefault" not in argv
    assert any(a.startswith("--accept=socket") for a in argv)


def test_libreoffice_user_lock_path_is_under_profile() -> None:
    lock = tr._libreoffice_user_lock_path()
    assert lock.name == ".lock"
    assert "libreoffice" in str(lock).lower() or "LibreOffice" in str(lock)


def test_clear_stale_user_profile_ipc_globs_os_tempdir(monkeypatch, tmp_path) -> None:
    import glob
    import tempfile

    monkeypatch.setattr(tr, "_soffice_bin_running", lambda: False)
    monkeypatch.setattr(tr, "_libreoffice_user_lock_path", lambda: tmp_path / "missing.lock")
    seen: list[str] = []

    def fake_glob(pattern: str) -> list[str]:
        seen.append(pattern)
        return []

    monkeypatch.setattr(glob, "glob", fake_glob)
    tr._clear_stale_user_profile_ipc()
    assert seen == [
        os.path.join(tempfile.gettempdir(), "OSL_PIPE_%s_*" % os.getuid())
    ]
