# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Makefile invariants for `make release` stripped-tree UNO tests."""

from __future__ import annotations

import shutil
import subprocess
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


def test_makefile_darwin_lo_python_uses_bundled_interpreter() -> None:
    """macOS must not fall through to the project venv (CI 33447724981)."""
    text = MAKEFILE.read_text(encoding="utf-8")
    darwin = text.split("ifeq ($(UNAME_S),Darwin)", 1)[1].split("else", 1)[0]
    assert "LibreOfficePython.framework" in darwin
    assert "Versions/Current/bin/python3" in darwin
    assert "Caskroom/libreoffice" in darwin
    assert "echo $(PYTHON)" not in darwin


def test_makefile_lo_python_probe_does_not_silently_use_venv() -> None:
    """The old `|| echo $(PYTHON)` launched .venv and segfaulted on macOS."""
    text = MAKEFILE.read_text(encoding="utf-8")
    assert "echo $(PYTHON)" not in text
    assert "_check-lo-python" in text
    assert "test-uno: _check-lo-python" in text


def test_makefile_linux_lo_python_prefers_usr_bin() -> None:
    """Ubuntu CI: setup-python shadows PATH python3; python3-uno is /usr/bin/python3."""
    text = MAKEFILE.read_text(encoding="utf-8")
    assert '/usr/bin/python3 -c "import uno"' in text
    assert "echo /usr/bin/python3" in text


def test_makefile_ci_debug_max_worker_restart_is_opt_in() -> None:
    text = MAKEFILE.read_text(encoding="utf-8")
    assert "ifeq ($(WRITERAGENT_CI_DEBUG),1)" in text
    assert "--max-worker-restart=0" in text
    unit_line = [ln for ln in text.splitlines() if ln.startswith("PYTEST_UNIT")][0]
    assert "$(PYTEST_CI_DEBUG_FLAGS)" in unit_line


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
    assert "LibreOfficePython.framework" in combined
    assert "plugin.testing_runner" not in combined
