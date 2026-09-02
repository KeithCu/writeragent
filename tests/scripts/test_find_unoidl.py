# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for scripts/find_unoidl.sh (shared unoidl-write / types.rdb finder)."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
FIND_UNOIDL = PROJECT_ROOT / "scripts" / "find_unoidl.sh"
REBUILD_LIBREPY = PROJECT_ROOT / "scripts" / "rebuild_librepy_rdb.sh"
REBUILD_XPROMPT = PROJECT_ROOT / "scripts" / "rebuild_xprompt_rdb.sh"
_FINDER_FUNCS = ("_find_unoidl_write", "_resolve_existing", "_find_type_rdbs")


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


def _touch_executable(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _run_finder(
    snippet: str,
    *,
    env_updates: dict[str, str] | None = None,
    env_unset: tuple[str, ...] = (),
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    bash = _bash_executable()
    assert bash is not None
    env = os.environ.copy()
    for key in env_unset:
        env.pop(key, None)
    if env_updates:
        env.update(env_updates)
    helper = _bash_path(FIND_UNOIDL)
    script = f'''
source "{helper}"
{snippet}
'''
    return subprocess.run(
        [bash, "-c", script],
        check=check,
        capture_output=True,
        text=True,
        env=env,
    )


@pytest.mark.skipif(_bash_executable() is None, reason="bash required for find_unoidl.sh tests")
@pytest.mark.parametrize("script", (FIND_UNOIDL, REBUILD_LIBREPY, REBUILD_XPROMPT))
def test_bash_n_is_clean(script: Path) -> None:
    bash = _bash_executable()
    assert bash is not None
    proc = subprocess.run(
        [bash, "-n", str(script)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr


def test_rebuild_scripts_source_shared_finder_and_do_not_redefine_it() -> None:
    for script in (REBUILD_LIBREPY, REBUILD_XPROMPT):
        text = script.read_text(encoding="utf-8")
        assert 'source "$(cd "$(dirname "$0")" && pwd)/find_unoidl.sh"' in text
        for func in _FINDER_FUNCS:
            assert f"{func}()" not in text


def test_rebuild_scripts_keep_caller_skip_vs_fail() -> None:
    librepy = REBUILD_LIBREPY.read_text(encoding="utf-8")
    assert 'skip: unoidl-write not found; using committed $RDB_PYTHON' in librepy
    assert "libreoffice-dev" in librepy
    xprompt = REBUILD_XPROMPT.read_text(encoding="utf-8")
    assert "using committed $RDB_PYTHON and $RDB_PROMPT" in xprompt
    assert "install libreoffice-fresh-sdk" in xprompt


def test_helper_defines_finder_functions() -> None:
    text = FIND_UNOIDL.read_text(encoding="utf-8")
    for func in _FINDER_FUNCS:
        assert f"{func}()" in text


@pytest.mark.skipif(_bash_executable() is None, reason="bash required for find_unoidl.sh tests")
def test_find_unoidl_write_prefers_path(tmp_path: Path) -> None:
    fake = tmp_path / "bin" / "unoidl-write"
    _touch_executable(fake)
    # Empty OO_SDK_HOME so PATH is the only early hit.
    proc = _run_finder(
        "_find_unoidl_write",
        env_updates={"PATH": _bash_path(fake.parent), "OO_SDK_HOME": ""},
    )
    assert _path_from_bash_output(proc.stdout.strip()) == fake.resolve()


@pytest.mark.skipif(_bash_executable() is None, reason="bash required for find_unoidl.sh tests")
def test_find_unoidl_write_uses_oo_sdk_home(tmp_path: Path) -> None:
    sdk = tmp_path / "sdk"
    fake = sdk / "bin" / "unoidl-write"
    _touch_executable(fake)
    empty_path = tmp_path / "empty-path"
    empty_path.mkdir()
    proc = _run_finder(
        "_find_unoidl_write",
        env_updates={"PATH": _bash_path(empty_path), "OO_SDK_HOME": _bash_path(sdk)},
    )
    assert _path_from_bash_output(proc.stdout.strip()) == fake.resolve()


@pytest.mark.skipif(_bash_executable() is None, reason="bash required for find_unoidl.sh tests")
def test_find_type_rdbs_linux_program_layout(tmp_path: Path) -> None:
    root = tmp_path / "libreoffice"
    binary = root / "sdk" / "bin" / "unoidl-write"
    ure = root / "program" / "types.rdb"
    office = root / "program" / "types" / "offapi.rdb"
    _touch_executable(binary)
    ure.parent.mkdir(parents=True, exist_ok=True)
    office.parent.mkdir(parents=True, exist_ok=True)
    ure.write_text("ure", encoding="utf-8")
    office.write_text("office", encoding="utf-8")

    proc = _run_finder(f'_find_type_rdbs "{_bash_path(binary)}"')
    lines = [line for line in proc.stdout.splitlines() if line]
    assert [_path_from_bash_output(line) for line in lines] == [ure.resolve(), office.resolve()]


@pytest.mark.skipif(_bash_executable() is None, reason="bash required for find_unoidl.sh tests")
def test_find_type_rdbs_macos_resources_layout(tmp_path: Path) -> None:
    root = tmp_path / "LibreOffice.app" / "Contents"
    binary = root / "sdk" / "bin" / "unoidl-write"
    ure = root / "Resources" / "types.rdb"
    office = root / "Resources" / "types" / "offapi.rdb"
    _touch_executable(binary)
    ure.parent.mkdir(parents=True, exist_ok=True)
    office.parent.mkdir(parents=True, exist_ok=True)
    ure.write_text("ure", encoding="utf-8")
    office.write_text("office", encoding="utf-8")

    proc = _run_finder(f'_find_type_rdbs "{_bash_path(binary)}"')
    lines = [line for line in proc.stdout.splitlines() if line]
    assert [_path_from_bash_output(line) for line in lines] == [ure.resolve(), office.resolve()]


@pytest.mark.skipif(_bash_executable() is None, reason="bash required for find_unoidl.sh tests")
def test_find_type_rdbs_missing_pair_fails(tmp_path: Path) -> None:
    binary = tmp_path / "sdk" / "bin" / "unoidl-write"
    _touch_executable(binary)
    proc = _run_finder(
        f'_find_type_rdbs "{_bash_path(binary)}"',
        check=False,
    )
    assert proc.returncode != 0
    assert proc.stdout.strip() == ""


@pytest.mark.skipif(_bash_executable() is None, reason="bash required for find_unoidl.sh tests")
def test_helper_is_idempotent_when_sourced_twice() -> None:
    proc = _run_finder(
        f'''
source "{_bash_path(FIND_UNOIDL)}"
type _find_unoidl_write >/dev/null
type _find_type_rdbs >/dev/null
echo ok
'''
    )
    assert proc.stdout.strip() == "ok"
