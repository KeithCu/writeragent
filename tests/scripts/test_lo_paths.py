# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for scripts/lo_paths.sh profile and cache discovery."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
LO_PATHS = PROJECT_ROOT / "scripts" / "lo_paths.sh"


def _bash_executable() -> str | None:
    if sys.platform == "win32":
        for candidate in (
            Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Git" / "bin" / "bash.exe",
            Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Git" / "bin" / "bash.exe",
        ):
            if candidate.is_file():
                return str(candidate)
    return shutil.which("bash")


def _bash_path(path: Path) -> str:
    resolved = path.resolve()
    text = resolved.as_posix()
    if sys.platform == "win32" and len(text) >= 2 and text[1] == ":":
        return "/" + text[0].lower() + text[2:]
    return text


def _path_from_bash_output(text: str) -> Path:
    if sys.platform == "win32" and len(text) >= 3 and text[0] == "/" and text[2] == "/":
        drive = text[1].upper()
        remainder = text[3:].replace("/", os.sep)
        return Path(f"{drive}:{os.sep}{remainder}")
    return Path(text)


def _run_lo_paths(home: Path, func: str) -> str:
    bash = _bash_executable()
    assert bash is not None
    env = os.environ.copy()
    env["HOME"] = _bash_path(home)
    lo_paths = _bash_path(LO_PATHS)
    script = f'''
source "{lo_paths}"
{func}
'''
    result = subprocess.run(
        [bash, "-c", script],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    return result.stdout.strip()


@pytest.mark.skipif(_bash_executable() is None, reason="bash required for lo_paths.sh tests")
def test_lo_user_conf_dir_uses_macos_library_path_when_present(tmp_path: Path) -> None:
    mac_conf = tmp_path / "Library" / "Application Support" / "LibreOffice" / "4"
    mac_conf.mkdir(parents=True)

    got = _run_lo_paths(tmp_path, "lo_user_conf_dir")
    assert _path_from_bash_output(got) == mac_conf.resolve()


@pytest.mark.skipif(_bash_executable() is None, reason="bash required for lo_paths.sh tests")
def test_lo_user_conf_dir_falls_back_to_linux_xdg(tmp_path: Path) -> None:
    linux_conf = tmp_path / ".config" / "libreoffice" / "4"
    linux_conf.mkdir(parents=True)

    got = _run_lo_paths(tmp_path, "lo_user_conf_dir")
    assert _path_from_bash_output(got) == linux_conf.resolve()


@pytest.mark.skipif(_bash_executable() is None, reason="bash required for lo_paths.sh tests")
def test_find_unopkg_cache_dir_discovers_macos_uno_packages(tmp_path: Path) -> None:
    cache = (
        tmp_path
        / "Library"
        / "Application Support"
        / "LibreOffice"
        / "4"
        / "user"
        / "uno_packages"
    )
    cache.mkdir(parents=True)

    got = _run_lo_paths(tmp_path, "find_unopkg_cache_dir")
    assert _path_from_bash_output(got) == cache.resolve()


@pytest.mark.skipif(_bash_executable() is None, reason="bash required for lo_paths.sh tests")
def test_find_unopkg_cache_dir_discovers_linux_uno_packages(tmp_path: Path) -> None:
    cache = tmp_path / ".config" / "libreoffice" / "4" / "user" / "uno_packages"
    cache.mkdir(parents=True)

    got = _run_lo_paths(tmp_path, "find_unopkg_cache_dir")
    assert _path_from_bash_output(got) == cache.resolve()
