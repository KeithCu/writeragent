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
