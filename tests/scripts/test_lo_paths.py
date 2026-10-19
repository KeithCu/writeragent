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
    monkeypatch.setattr(lo_paths, "_SNAP_PROGRAM_DIR", str(tmp_path / "snap" / "program"))
    monkeypatch.setattr(lo_paths, "_LINUX_PYTHON_LIB_DIRS", (str(tmp_path / "usr_lib"), str(tmp_path / "usr_lib64")))
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


def test_linux_distro_wins_over_snap(linux: Path) -> None:
    distro_soffice = _touch(linux / "distro" / "program" / "soffice")
    _touch(linux / "snap" / "program" / "soffice")
    assert lo_paths.program_dir() == distro_soffice.parent


def test_linux_snap_found_when_distro_empty(linux: Path) -> None:
    snap_soffice = _touch(linux / "snap" / "program" / "soffice")
    assert lo_paths.program_dir() == snap_soffice.parent


def test_macos_homebrew_cask(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(lo_paths.sys, "platform", "darwin")
    monkeypatch.setattr(lo_paths.Path, "home", lambda: tmp_path / "home")
    cask_dir = tmp_path / "cask"
    monkeypatch.setattr(lo_paths, "_DARWIN_CASK_ROOTS", (cask_dir,))
    app = cask_dir / "libreoffice" / "LibreOffice.app"
    soffice = _touch(app / "Contents" / "MacOS" / "soffice")
    python = _touch(app / "Contents" / "Resources" / "python")
    _touch(app / "Contents" / "Resources" / "uno.py")
    if Path("/Applications/LibreOffice.app/Contents/MacOS/soffice").is_file():
        pytest.skip("a real /Applications/LibreOffice.app takes precedence")
    assert lo_paths.program_dir() == soffice.parent
    assert lo_paths.find("python") == python
    assert lo_paths.uno_link_dirs() == (app / "Contents" / "Resources", None)


def test_uno_link_dirs_prefers_venv_minor(linux: Path) -> None:
    target_uno = _touch(linux / "usr_lib" / "python3.13" / "site-packages" / "uno.py")
    _touch(linux / "usr_lib" / "python3.14" / "site-packages" / "uno.py")
    _touch(linux / "distro" / "program" / "pyuno.so")
    _touch(linux / "distro" / "program" / "soffice")
    uno_dir, extra = lo_paths.uno_link_dirs(venv_py=(3, 13))
    assert uno_dir == target_uno.parent
    assert extra == linux / "distro" / "program"


def test_uno_link_dirs_single_other_version(linux: Path) -> None:
    target_uno = _touch(linux / "usr_lib" / "python3.14" / "site-packages" / "uno.py")
    uno_dir, _extra = lo_paths.uno_link_dirs(venv_py=(3, 13))
    assert uno_dir == target_uno.parent


def test_uno_link_dirs_multiple_versions_follow_system_python(linux: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess

    _touch(linux / "usr_lib" / "python3.11" / "site-packages" / "uno.py")
    target_uno = _touch(linux / "usr_lib" / "python3.12" / "site-packages" / "uno.py")
    sys_py = _touch(linux / "bin" / "python3")
    monkeypatch.setattr(lo_paths, "_LINUX_SYSTEM_PYTHON", str(sys_py))

    def fake_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess([], 0, stdout=str(target_uno))

    monkeypatch.setattr(lo_paths.subprocess, "run", fake_run)
    uno_dir, _extra = lo_paths.uno_link_dirs(venv_py=(3, 13))
    assert uno_dir == target_uno.parent


def test_macos_uno_link_dirs_resources_only_with_framework_pyuno(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(lo_paths.sys, "platform", "darwin")
    monkeypatch.setattr(lo_paths, "_DARWIN_CASK_ROOTS", ())
    app = tmp_path / "Applications" / "LibreOffice.app"
    monkeypatch.setattr(lo_paths.Path, "home", lambda: tmp_path)
    if Path("/Applications/LibreOffice.app/Contents/Resources/uno.py").is_file():
        pytest.skip("a real /Applications/LibreOffice.app takes precedence")
    _touch(app / "Contents" / "Resources" / "uno.py")
    _touch(app / "Contents" / "Frameworks" / "pyuno.so")
    _touch(app / "Contents" / "MacOS" / "soffice")
    uno_dir, extra = lo_paths.uno_link_dirs()
    assert uno_dir == app / "Contents" / "Resources"
    assert extra is None


def test_windows_uno_link_dirs_registry(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(lo_paths.sys, "platform", "win32")
    monkeypatch.setattr(lo_paths.shutil, "which", lambda _name: None)
    prog = tmp_path / "Custom" / "program"
    _touch(prog / "uno.py")
    _touch(prog / "soffice.exe")
    monkeypatch.setattr(lo_paths, "_registry_install_path", lambda: prog)
    uno_dir, extra = lo_paths.uno_link_dirs()
    assert uno_dir == prog
    assert extra is None


def test_windows_uno_link_dirs_path_soffice(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(lo_paths.sys, "platform", "win32")
    prog = tmp_path / "Portable" / "program"
    _touch(prog / "soffice.exe")
    _touch(prog / "uno.py")
    monkeypatch.setattr(lo_paths, "_registry_install_path", lambda: None)
    monkeypatch.setattr(lo_paths.shutil, "which", lambda name: str(prog / name) if name == "soffice.exe" else None)
    uno_dir, extra = lo_paths.uno_link_dirs()
    assert uno_dir == prog
    assert extra is None
