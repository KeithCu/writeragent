# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for scripts.fix_uno_import."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

try:
    from scripts import fix_uno_import as fix_mod
except ImportError:
    pytest.skip("scripts module not available", allow_module_level=True)


def test_needs_uno_fix_when_pth_missing(tmp_path: Path):
    venv = tmp_path / ".venv"
    site = venv / "lib" / "python3.13" / "site-packages"
    site.mkdir(parents=True)
    (venv / "bin").mkdir()
    py = venv / "bin" / "python"
    py.write_text("#!/bin/sh\n")
    py.chmod(0o755)
    assert fix_mod.needs_uno_fix(str(venv)) is True


def test_resolve_venv_paths_supports_windows_layout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(fix_mod.sys, "platform", "win32")
    venv = tmp_path / ".venv"
    site = venv / "Lib" / "site-packages"
    site.mkdir(parents=True)
    scripts = venv / "Scripts"
    scripts.mkdir()
    py = scripts / "python.exe"
    py.write_text("")

    site_packages, venv_python, pth_file = fix_mod.resolve_venv_paths(str(venv))

    assert Path(site_packages) == site
    assert Path(venv_python) == py
    assert Path(pth_file) == site / "uno.pth"


def test_needs_uno_fix_false_when_import_works(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    venv = tmp_path / ".venv"
    site = venv / "lib" / "python3.13" / "site-packages"
    site.mkdir(parents=True)
    (venv / "bin").mkdir()
    py = venv / "bin" / "python"
    py.write_text("#!/bin/sh\n")
    py.chmod(0o755)

    uno_path, lo_program = "/fake/uno", "/fake/lo"
    monkeypatch.setattr(fix_mod, "find_system_uno", lambda **_: (uno_path, lo_program))
    monkeypatch.setattr(fix_mod, "_venv_python_version", lambda _: (3, 13))
    monkeypatch.setattr(fix_mod, "uno_import_works", lambda _: True)

    pth = site / "uno.pth"
    with pth.open("w", encoding="utf-8") as f:
        for line in fix_mod._expected_pth_lines(uno_path, lo_program):
            f.write(f"{line}\n")

    assert fix_mod.needs_uno_fix(str(venv)) is False


def test_ensure_uno_import_skips_when_ok(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    venv = tmp_path / ".venv"
    site = venv / "lib" / "python3.13" / "site-packages"
    site.mkdir(parents=True)
    (venv / "bin").mkdir()
    py = venv / "bin" / "python"
    py.write_text("#!/bin/sh\n")
    py.chmod(0o755)

    uno_path, lo_program = "/fake/uno", None
    monkeypatch.setattr(fix_mod, "find_system_uno", lambda **_: (uno_path, lo_program))
    monkeypatch.setattr(fix_mod, "_venv_python_version", lambda _: (3, 13))
    monkeypatch.setattr(fix_mod, "uno_import_works", lambda _: True)

    pth = site / "uno.pth"
    with pth.open("w", encoding="utf-8") as f:
        for line in fix_mod._expected_pth_lines(uno_path, lo_program):
            f.write(f"{line}\n")

    applied = fix_mod.ensure_uno_import(str(venv))
    assert applied is False
    assert "no fix needed" in capsys.readouterr().out


def test_find_system_uno_supports_windows_program_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(fix_mod.sys, "platform", "win32")
    monkeypatch.setattr(fix_mod.shutil, "which", lambda _: None)
    monkeypatch.setenv("PROGRAMFILES", str(tmp_path / "Program Files"))
    monkeypatch.delenv("ProgramW6432", raising=False)
    monkeypatch.delenv("PROGRAMFILES(X86)", raising=False)

    program = tmp_path / "Program Files" / "LibreOffice" / "program"
    program.mkdir(parents=True)
    (program / "uno.py").write_text("# fake LibreOffice uno.py\n")
    (program / "pyuno.pyd").write_text("")

    uno_path, lo_program = fix_mod.find_system_uno()

    assert Path(uno_path) == program
    assert Path(lo_program) == program
    assert fix_mod._expected_pth_lines(uno_path, lo_program) == ["# Added by scripts/fix_uno_import.py", uno_path]


def test_uno_import_accepts_windows_pyuno_bridge_failure(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(fix_mod.sys, "platform", "win32")

    def fake_run(*args, **kwargs):
        raise subprocess.CalledProcessError(1, args[0], stderr="ImportError: DLL load failed while importing pyuno")

    monkeypatch.setattr(fix_mod.subprocess, "run", fake_run)

    assert fix_mod.uno_import_works("python.exe") is True


def test_uno_import_preserves_macos_pyuno_bridge_failure(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(fix_mod.sys, "platform", "darwin")

    def fake_run(*args, **kwargs):
        raise subprocess.CalledProcessError(1, args[0], stderr="ModuleNotFoundError: No module named 'pyuno'")

    monkeypatch.setattr(fix_mod.subprocess, "run", fake_run)

    assert fix_mod.uno_import_works("python") is True


def test_cli_check_exits_when_fix_needed(tmp_path: Path):
    venv = tmp_path / ".venv"
    site = venv / "lib" / "python3.13" / "site-packages"
    site.mkdir(parents=True)
    (venv / "bin").mkdir()

    result = subprocess.run(
        [sys.executable, str(Path(__file__).resolve().parents[1] / "scripts" / "fix_uno_import.py"), "--check"],
        cwd=tmp_path,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 1
