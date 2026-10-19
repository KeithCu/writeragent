# WriterAgent — tests for scripts/fix_uno_import.py
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for UNO site-packages link resolution, probing, and atomic writing."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import pytest

from scripts import fix_uno_import


def _make_fake_venv(tmp_path: Path, *, py_ver: str = "3.13") -> tuple[Path, Path, Path]:
    venv_dir = tmp_path / "fake_venv"
    venv_dir.mkdir(parents=True, exist_ok=True)
    (venv_dir / "pyvenv.cfg").write_text(f"version = {py_ver}.0\n", encoding="utf-8")
    py_bin = venv_dir / "bin" / "python"
    py_bin.parent.mkdir(parents=True, exist_ok=True)
    py_bin.write_text("", encoding="utf-8")
    site_packages = venv_dir / "lib" / f"python{py_ver}" / "site-packages"
    site_packages.mkdir(parents=True, exist_ok=True)
    return venv_dir, py_bin, site_packages


def test_missing_directory(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    no_dir = tmp_path / "no_such_venv"

    # --check without -q
    rc = fix_uno_import.main(["--venv", str(no_dir), "--check"])
    assert rc == 1
    err = capsys.readouterr().err
    assert f"Error: Virtual environment not found at {no_dir}" in err

    # --check with -q
    rc = fix_uno_import.main(["--venv", str(no_dir), "--check", "-q"])
    assert rc == 1
    err = capsys.readouterr().err
    assert f"Error: Virtual environment not found at {no_dir}" in err

    # apply without -q
    rc = fix_uno_import.main(["--venv", str(no_dir)])
    assert rc == 1
    err = capsys.readouterr().err
    assert f"Error: Virtual environment not found at {no_dir}" in err

    # apply with -q
    rc = fix_uno_import.main(["--venv", str(no_dir), "-q"])
    assert rc == 1
    err = capsys.readouterr().err
    assert f"Error: Virtual environment not found at {no_dir}" in err


def test_directory_without_python(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    empty_dir = tmp_path / "empty_venv"
    empty_dir.mkdir()
    rc = fix_uno_import.main(["--venv", str(empty_dir)])
    assert rc == 1
    err = capsys.readouterr().err
    assert f"Error: No venv python found in {empty_dir}" in err


def test_current_uno_pth_unmodified(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # POSIX .pth layout; the Windows staging copy has its own test below.
    monkeypatch.setattr(fix_uno_import.sys, "platform", "linux")
    venv_dir, _py_bin, site_packages = _make_fake_venv(tmp_path)
    (site_packages / "types_unopy-1.0.dist-info").mkdir()
    uno_dir = tmp_path / "lo_uno"
    prog_dir = tmp_path / "lo_prog"

    monkeypatch.setattr(fix_uno_import, "find_system_uno", lambda **_kw: (str(uno_dir), str(prog_dir)))
    monkeypatch.setattr(fix_uno_import, "uno_import_works", lambda _py: True)

    pth_file = site_packages / "uno.pth"
    expected_lines = f"# Added by scripts/fix_uno_import.py\n{uno_dir}\n{prog_dir}\n"
    pth_file.write_text(expected_lines, encoding="utf-8")
    initial_mtime = pth_file.stat().st_mtime_ns

    rc = fix_uno_import.main(["--venv", str(venv_dir)])
    assert rc == 0
    assert pth_file.stat().st_mtime_ns == initial_mtime
    assert pth_file.read_text(encoding="utf-8") == expected_lines


def test_stale_uno_pth_overwritten(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # POSIX .pth layout; the Windows staging copy has its own test below.
    monkeypatch.setattr(fix_uno_import.sys, "platform", "linux")
    venv_dir, _py_bin, site_packages = _make_fake_venv(tmp_path)
    (site_packages / "types_unopy-1.0.dist-info").mkdir()
    uno_dir = tmp_path / "lo_uno"
    prog_dir = tmp_path / "lo_prog"

    monkeypatch.setattr(fix_uno_import, "find_system_uno", lambda **_kw: (str(uno_dir), str(prog_dir)))
    monkeypatch.setattr(fix_uno_import, "uno_import_works", lambda _py: True)

    pth_file = site_packages / "uno.pth"
    pth_file.write_text("# Added by scripts/fix_uno_import.py\n/old/stale/path\n", encoding="utf-8")

    rc = fix_uno_import.main(["--venv", str(venv_dir)])
    assert rc == 0
    expected_lines = f"# Added by scripts/fix_uno_import.py\n{uno_dir}\n{prog_dir}\n"
    assert pth_file.read_text(encoding="utf-8") == expected_lines


def test_missing_uno_pth_check_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    venv_dir, _py_bin, site_packages = _make_fake_venv(tmp_path)
    uno_dir = tmp_path / "lo_uno"
    prog_dir = tmp_path / "lo_prog"

    monkeypatch.setattr(fix_uno_import, "find_system_uno", lambda **_kw: (str(uno_dir), str(prog_dir)))
    monkeypatch.setattr(fix_uno_import, "uno_import_works", lambda _py: True)

    rc = fix_uno_import.main(["--venv", str(venv_dir), "--check"])
    assert rc == 1
    assert not (site_packages / "uno.pth").exists()


def test_no_uno_found_and_import_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    venv_dir, _py_bin, _site_packages = _make_fake_venv(tmp_path)
    monkeypatch.setattr(fix_uno_import, "find_system_uno", lambda **_kw: (None, None))
    monkeypatch.setattr(fix_uno_import, "uno_import_works", lambda _py: False)

    rc = fix_uno_import.main(["--venv", str(venv_dir)])
    assert rc == 1
    err = capsys.readouterr().err
    assert "Error: Could not find system uno.py. Please ensure LibreOffice python-uno is installed." in err


def test_no_uno_found_but_import_already_works(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    venv_dir, _py_bin, site_packages = _make_fake_venv(tmp_path)
    monkeypatch.setattr(fix_uno_import, "find_system_uno", lambda **_kw: (None, None))
    monkeypatch.setattr(fix_uno_import, "uno_import_works", lambda _py: True)

    pth_file = site_packages / "uno.pth"
    pth_file.write_text("# preexisting\n", encoding="utf-8")

    rc = fix_uno_import.main(["--venv", str(venv_dir)])
    assert rc == 0
    # Left existing pth file intact
    assert pth_file.read_text(encoding="utf-8") == "# preexisting\n"


def test_timeout_expired_does_not_hang(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    venv_dir, _py_bin, _site_packages = _make_fake_venv(tmp_path)

    def fake_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd=["python"], timeout=20)

    monkeypatch.setattr(fix_uno_import.subprocess, "run", fake_run)
    monkeypatch.setattr(fix_uno_import, "find_system_uno", lambda **_kw: (None, None))

    rc = fix_uno_import.main(["--venv", str(venv_dir)])
    assert rc == 1
    err = capsys.readouterr().err
    assert "timed out after 20s" in err


def test_missing_dist_info_does_not_spawn_uv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    venv_dir, _py_bin, site_packages = _make_fake_venv(tmp_path)
    uno_dir = tmp_path / "lo_uno"
    prog_dir = tmp_path / "lo_prog"

    monkeypatch.setattr(fix_uno_import, "find_system_uno", lambda **_kw: (str(uno_dir), str(prog_dir)))
    monkeypatch.setattr(fix_uno_import, "uno_import_works", lambda _py: True)

    spawned_cmds: list[list[str]] = []

    def record_run(cmd: list[str], *_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        spawned_cmds.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(fix_uno_import.subprocess, "run", record_run)

    # Ensure no dist-info exists in site_packages
    assert not list(site_packages.glob("types_unopy*.dist-info"))

    rc = fix_uno_import.main(["--venv", str(venv_dir), "-q"])
    assert rc == 0
    err = capsys.readouterr().err
    assert "types-unopy is not installed. Run: uv sync --frozen --dev" in err

    # uv pip was not invoked
    for cmd in spawned_cmds:
        assert cmd[0] != "uv"


def test_oserror_during_write_cleans_tmp_and_preserves_pth(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    venv_dir, _py_bin, site_packages = _make_fake_venv(tmp_path)
    uno_dir = tmp_path / "lo_uno"
    prog_dir = tmp_path / "lo_prog"

    monkeypatch.setattr(fix_uno_import, "find_system_uno", lambda **_kw: (str(uno_dir), str(prog_dir)))
    monkeypatch.setattr(fix_uno_import, "uno_import_works", lambda _py: True)

    pth_file = site_packages / "uno.pth"
    pth_file.write_text("original content", encoding="utf-8")

    def failing_replace(_src: str, _dst: str) -> None:
        raise OSError("Disk full simulation")

    monkeypatch.setattr(fix_uno_import.os, "replace", failing_replace)

    rc = fix_uno_import.main(["--venv", str(venv_dir)])
    assert rc == 1
    assert pth_file.read_text(encoding="utf-8") == "original content"
    assert list(site_packages.glob("uno.pth.*.tmp")) == []


def test_stored_paths_are_absolute(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # POSIX .pth layout; the Windows staging copy has its own test below.
    monkeypatch.setattr(fix_uno_import.sys, "platform", "linux")
    venv_dir, _py_bin, site_packages = _make_fake_venv(tmp_path)
    (site_packages / "types_unopy-1.0.dist-info").mkdir()

    monkeypatch.setattr(fix_uno_import, "find_system_uno", lambda **_kw: ("rel/uno", "rel/prog"))
    monkeypatch.setattr(fix_uno_import, "uno_import_works", lambda _py: True)

    rc = fix_uno_import.main(["--venv", str(venv_dir)])
    assert rc == 0
    pth_file = site_packages / "uno.pth"
    lines = pth_file.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "# Added by scripts/fix_uno_import.py"
    assert os.path.isabs(lines[1])
    assert os.path.isabs(lines[2])


def test_duplicate_lo_program_omitted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    # POSIX .pth layout; the Windows staging copy has its own test below.
    monkeypatch.setattr(fix_uno_import.sys, "platform", "linux")
    venv_dir, _py_bin, site_packages = _make_fake_venv(tmp_path)
    (site_packages / "types_unopy-1.0.dist-info").mkdir()
    uno_dir = tmp_path / "lo_program"

    monkeypatch.setattr(fix_uno_import, "find_system_uno", lambda **_kw: (str(uno_dir), str(uno_dir)))
    monkeypatch.setattr(fix_uno_import, "uno_import_works", lambda _py: True)

    rc = fix_uno_import.main(["--venv", str(venv_dir)])
    assert rc == 0
    captured = capsys.readouterr()
    path_lines = [ln.strip() for ln in captured.out.splitlines() if ln.strip().startswith("- ")]
    assert len(path_lines) == 1
    assert path_lines[0] == f"- {uno_dir}"

    lines = (site_packages / "uno.pth").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert lines[1] == str(uno_dir)


def test_windows_pth_uses_copied_uno_py_not_program_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(fix_uno_import.sys, "platform", "win32")
    program = tmp_path / "program"
    program.mkdir()
    (program / "uno.py").write_text("# uno\n")
    venv = tmp_path / "venv"
    stage = fix_uno_import._windows_uno_py_dir(str(venv))
    assert fix_uno_import._venv_uno_paths(str(venv), str(program), str(program)) == (stage, None)
    assert not fix_uno_import._windows_uno_py_current(stage, str(program))
    fix_uno_import._copy_windows_uno_py(stage, str(program))
    assert fix_uno_import._windows_uno_py_current(stage, str(program))
