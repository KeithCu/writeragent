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
