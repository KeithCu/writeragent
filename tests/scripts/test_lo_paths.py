# WriterAgent — tests for scripts/lo_paths.py
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""LibreOffice program discovery per platform (fake trees) and profile URLs."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from scripts import lo_paths


def _touch(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("")
    return path


@pytest.fixture
def linux(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Linux with no real install visible: only what the test creates."""
    monkeypatch.setattr(lo_paths.sys, "platform", "linux")
    monkeypatch.setattr(lo_paths.shutil, "which", lambda _name: None)
    monkeypatch.setattr(lo_paths, "_LINUX_DIRS", (str(tmp_path / "distro" / "program"),))
    monkeypatch.setattr(lo_paths, "_LINUX_OPT_GLOB", "no-such-dir*/program")
    monkeypatch.setattr(lo_paths, "_LINUX_SYSTEM_PYTHON", str(tmp_path / "no-python3"))
    return tmp_path


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privilege on Windows")
def test_linux_resolves_soffice_symlink_on_path(linux: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    prog = linux / "lib" / "libreoffice" / "program"
    _touch(prog / "soffice")
    unopkg = _touch(prog / "unopkg")
    link = linux / "bin" / "soffice"
    link.parent.mkdir()
    link.symlink_to(prog / "soffice")
    monkeypatch.setattr(lo_paths.shutil, "which", lambda _name: str(link))
    assert lo_paths.program_dir() == prog
    assert lo_paths.find("unopkg") == unopkg


def test_linux_distro_dir_and_system_python(linux: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    prog = linux / "distro" / "program"
    soffice = _touch(prog / "soffice")
    python3 = _touch(linux / "python3")
    monkeypatch.setattr(lo_paths, "_LINUX_SYSTEM_PYTHON", str(python3))
    assert lo_paths.find("soffice") == soffice
    # No program/python (distro build): fall back to the system interpreter.
    assert lo_paths.find("python") == python3
    # A TDF build's own program/python wins.
    own = _touch(prog / "python")
    assert lo_paths.find("python") == own


def test_linux_not_installed(linux: Path) -> None:
    assert lo_paths.program_dir() is None
    assert lo_paths.find("unopkg") is None
    assert lo_paths.main(["unopkg"]) == 1


def test_windows_registry_then_program_files(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(lo_paths.sys, "platform", "win32")
    monkeypatch.setattr(lo_paths, "_registry_install_path", lambda: None)
    monkeypatch.setenv("ProgramFiles", str(tmp_path / "PF"))
    monkeypatch.delenv("ProgramFiles(x86)", raising=False)
    prog = tmp_path / "PF" / "LibreOffice" / "program"
    _touch(prog / "soffice.exe")
    _touch(prog / "unopkg.exe")
    com = _touch(prog / "unopkg.com")
    python = _touch(prog / "python.exe")
    assert lo_paths.find("unopkg") == com
    assert lo_paths.find("python") == python

    reg = tmp_path / "Custom" / "program"
    reg_soffice = _touch(reg / "soffice.exe")
    monkeypatch.setattr(lo_paths, "_registry_install_path", lambda: reg)
    assert lo_paths.find("soffice") == reg_soffice


def test_macos_app_bundle(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(lo_paths.sys, "platform", "darwin")
    monkeypatch.setattr(lo_paths.Path, "home", lambda: tmp_path)
    contents = tmp_path / "Applications" / "LibreOffice.app" / "Contents"
    if Path("/Applications/LibreOffice.app/Contents/MacOS/soffice").is_file():
        pytest.skip("a real /Applications/LibreOffice.app takes precedence")
    _touch(contents / "MacOS" / "soffice")
    unopkg = _touch(contents / "MacOS" / "unopkg")
    python = _touch(contents / "Resources" / "python")
    assert lo_paths.find("unopkg") == unopkg
    assert lo_paths.find("python") == python


def test_profile_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    spaced = tmp_path / "my profile"
    url = lo_paths.profile_url(spaced)
    assert url == spaced.resolve().as_uri()
    assert url.startswith("file:///")
    assert "%20" in url
    assert not url.endswith("/")
    monkeypatch.chdir(tmp_path)
    assert lo_paths.profile_url("rel") == (tmp_path / "rel").resolve().as_uri()
    with pytest.raises(ValueError, match="user/"):
        lo_paths.profile_url(tmp_path / "profile" / "user")


def test_main_usage() -> None:
    assert lo_paths.main([]) == 2
    assert lo_paths.main(["bogus"]) == 2
