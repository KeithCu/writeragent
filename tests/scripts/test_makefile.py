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

    text = MAKEFILE.read_text(encoding="utf-8")
    assert '$(MAKE) -C "$(PROJECT_ROOT)" lo-kill' in text


def test_makefile_lo_python_recipes_call_lo_paths() -> None:
    """_check-lo-python calls check-lo-python; the five UNO recipes call run-lo-python."""
    text = MAKEFILE.read_text(encoding="utf-8")
    check_recipe = text.split("_check-lo-python:", 1)[1].split("test-uno:", 1)[0]
    assert "check-lo-python" in check_recipe
    assert "echo $(PYTHON)" not in check_recipe

    five_recipes = (
        "test-uno:",
        "test-uno-soak:",
        "test-mock-sidebar:",
        "test-visible:",
        "lo-test-threadguard-visible:",
    )
    for target in five_recipes:
        block = text.split(target, 1)[1].split("\n\n", 1)[0]
        assert "run-lo-python" in block, f"{target} missing run-lo-python"

    mock_sidebar = text.split("test-mock-sidebar:", 1)[1].split("\n\n", 1)[0]
    assert "--set WRITERAGENT_UNO_TEST_TIMEOUT=120" in mock_sidebar

    threadguard_visible = text.split("lo-test-threadguard-visible:", 1)[1].split("\n\n", 1)[0]
    assert "--set WRITERAGENT_UNO_THREAD_GUARD=1" in threadguard_visible


def test_makefile_env_u_virtual_env_and_unset_gone() -> None:
    """LO_PYTHON_UNSET and env -u VIRTUAL_ENV are removed in favor of lo_paths.py."""
    text = MAKEFILE.read_text(encoding="utf-8")
    assert "env -u VIRTUAL_ENV" not in text
    assert "LO_PYTHON_UNSET" not in text
    assert "ifeq ($(origin LO_PYTHON),undefined)" in text
    assert "export LO_PYTHON_ENV" in text


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


def test_makefile_compile_translations_uses_python_script() -> None:
    """Windows CI skipped msgfmt silently; compile must not depend on GNU find."""
    text = MAKEFILE.read_text(encoding="utf-8")
    recipe = text.split("compile-translations:", 1)[1].split("compile-translations-core:", 1)[0]
    assert "compile_translations.py" in recipe
    assert "command -v msgfmt" not in recipe


def test_makefile_gha_and_ci_debug_disable_worker_restart() -> None:
    """PR CI must pass --max-worker-restart=0 without requiring ci_debug (#823)."""
    text = MAKEFILE.read_text(encoding="utf-8")
    assert "filter true 1,$(GITHUB_ACTIONS)" in text
    assert "ifeq ($(WRITERAGENT_CI_DEBUG),1)" in text
    assert "--max-worker-restart=0" in text
    unit_line = [ln for ln in text.splitlines() if ln.startswith("PYTEST_UNIT")][0]
    assert "$(PYTEST_CI_DEBUG_FLAGS)" in unit_line
    assert "WRITERAGENT_PYTEST_PROGRESS" not in unit_line


@pytest.mark.skipif(shutil.which("make") is None, reason="make not on PATH")
def test_test_uno_empty_lo_python_fails_loudly() -> None:
    """Empty LO_PYTHON must name the search paths, not launch testing_runner."""
    proc = subprocess.run(
        ["make", "-C", str(PROJECT_ROOT), "test-uno", "LO_PYTHON="],
        check=False,
        capture_output=True,
        text=True,
    )
    combined = proc.stdout + proc.stderr
    assert proc.returncode != 0, combined
    assert "LO_PYTHON is empty" in combined
    assert "Contents/Resources/python" in combined
    assert "LibreOfficePython.framework" in combined
    assert "/usr/bin/python3" in combined
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


def test_makefile_translation_and_clean_one_line_recipes() -> None:
    text = MAKEFILE.read_text(encoding="utf-8")
    assert 'clean: clean-native\n\t"$(PYTHON)" scripts/clean.py' in text
    assert 'extract-strings:\n\t"$(PYTHON)" scripts/extract_strings.py' in text
    assert 'refresh-pot:\n\t"$(PYTHON)" scripts/extract_strings.py --pot-only' in text
    assert 'merge-translations:\n\t"$(PYTHON)" scripts/extract_strings.py --merge-only' in text
    assert 'auto-translate:\n\t"$(PYTHON)" scripts/extract_strings.py --auto-translate' in text

