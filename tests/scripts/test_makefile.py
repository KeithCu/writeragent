# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Makefile invariants for `make release` stripped-tree UNO tests."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MAKEFILE = PROJECT_ROOT / "Makefile"


@pytest.mark.skipif(shutil.which("make") is None, reason="make not on PATH")
def test_lo_kill_when_cwd_has_no_makefile(tmp_path: Path) -> None:
    """`make release` runs `make -f $checkout/Makefile test-uno` from a
    stripped /tmp tree (no Makefile). lo-kill must resolve via PROJECT_ROOT
    (the Makefile's directory), not CURDIR.

    Dry-run lo-kill only: `make -n test-uno` still executes recipe lines that
    contain $(MAKE) (GNU make recursive-make exception).
    """
    proc = subprocess.run(
        [
            "make",
            "-n",
            "-C",
            str(tmp_path),
            "-f",
            str(MAKEFILE),
            "lo-kill",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    combined = proc.stdout + proc.stderr
    assert proc.returncode == 0, combined
    assert "No rule to make target" not in combined
    assert "kill-libreoffice" in combined


def _make_dry_run(target: str, **env_overrides: str) -> str:
    env = {k: v for k, v in os.environ.items() if k not in {"GITHUB_ACTIONS", "WRITERAGENT_CI_DEBUG"}}
    env.update(env_overrides)
    proc = subprocess.run(
        ["make", "-n", "-C", str(PROJECT_ROOT), target],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return proc.stdout


@pytest.mark.skipif(shutil.which("make") is None, reason="make not on PATH")
def test_make_rdb_core_runs_bash_script_not_ps1() -> None:
    """GHA 33777144412: make rdb-core used rebuild_librepy_rdb.ps1 (missing)."""
    out = _make_dry_run("rdb-core")
    recipes = "\n".join(ln for ln in out.splitlines() if not ln.lstrip().startswith("#"))
    assert "rebuild_librepy_rdb.sh" in recipes
    assert ".ps1" not in recipes


@pytest.mark.skipif(shutil.which("make") is None, reason="make not on PATH")
def test_make_compile_translations_runs_python_script_not_msgfmt() -> None:
    """Windows CI skipped msgfmt silently; compile must not depend on GNU find."""
    out = _make_dry_run("compile-translations")
    assert "compile_translations.py" in out
    assert "msgfmt" not in out


@pytest.mark.skipif(shutil.which("make") is None, reason="make not on PATH")
@pytest.mark.parametrize(
    ("env_overrides", "expect_flag"),
    [
        ({}, False),
        ({"GITHUB_ACTIONS": "true"}, True),
        ({"WRITERAGENT_CI_DEBUG": "1"}, True),
    ],
)
def test_make_pytest_disables_worker_restart_on_github_or_ci_debug(
    env_overrides: dict[str, str], expect_flag: bool
) -> None:
    """PR CI must pass --max-worker-restart=0 without requiring ci_debug (#823)."""
    out = _make_dry_run("pytest", **env_overrides)
    assert ("--max-worker-restart=0" in out) is expect_flag


@pytest.mark.skipif(sys.platform != "linux", reason="Linux /usr/bin/python3 probe")
def test_check_lo_python_ignores_path_python3_without_uno(tmp_path: Path) -> None:
    """33456719039: PATH python3 is setup-python 3.13; probe must still pick /usr/bin."""
    probe = subprocess.run(
        ["/usr/bin/python3", "-c", "import uno"],
        check=False,
        capture_output=True,
    )
    if probe.returncode != 0:
        pytest.skip("python3-uno not installed on /usr/bin/python3")
    fake = tmp_path / "python3"
    fake.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    fake.chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = f"{tmp_path}{os.pathsep}{env.get('PATH', '')}"
    proc = subprocess.run(
        ["make", "-C", str(PROJECT_ROOT), "_check-lo-python"],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    combined = proc.stdout + proc.stderr
    assert proc.returncode == 0, combined
    assert "LO_PYTHON=/usr/bin/python3" in combined


@pytest.mark.skipif(shutil.which("make") is None, reason="make not on PATH")
def test_test_uno_empty_lo_python_fails_loudly() -> None:
    """Empty LO_PYTHON must fail loudly, not launch testing_runner."""
    proc = subprocess.run(
        ["make", "-C", str(PROJECT_ROOT), "test-uno", "LO_PYTHON="],
        check=False,
        capture_output=True,
        text=True,
    )
    combined = proc.stdout + proc.stderr
    assert proc.returncode != 0, combined
    assert "LO_PYTHON is empty" in combined
    assert "plugin.testing_runner" not in combined


@pytest.mark.skipif(shutil.which("make") is None, reason="make not on PATH")
def test_check_lo_python_rejects_interpreter_without_officehelper(tmp_path: Path) -> None:
    """Do not launch testing_runner when the selected python cannot import UNO.

    Uses a stub executable, not the project .venv: hosting pyuno in that
    CPython segfaults on macOS (CI 33447724981).
    """
    fake = tmp_path / "not-lo-python"
    fake.write_text("#!/bin/sh\necho 'stub interpreter: no officehelper' >&2\nexit 1\n", encoding="utf-8")
    fake.chmod(0o755)
    proc = subprocess.run(
        ["make", "-C", str(PROJECT_ROOT), "_check-lo-python", f"LO_PYTHON={fake}"],
        check=False,
        capture_output=True,
        text=True,
    )
    combined = proc.stdout + proc.stderr
    assert proc.returncode != 0, combined
    assert "cannot import officehelper" in combined
    assert "project .venv" in combined
    assert "-m plugin.testing_runner" not in combined

