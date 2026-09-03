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


def test_makefile_darwin_lo_python_uses_bundled_interpreter() -> None:
    """macOS must not fall through to the project venv (CI 33447724981)."""
    text = MAKEFILE.read_text(encoding="utf-8")
    darwin = text.split("ifeq ($(UNAME_S),Darwin)", 1)[1].split("else", 1)[0]
    assert "Contents/Resources/python" in darwin
    assert "LibreOfficePython.framework" in darwin
    assert "Versions/Current/bin/python3" in darwin
    assert "Caskroom/libreoffice" in darwin
    assert "URE_BOOTSTRAP" in darwin
    assert "UNO_PATH" in darwin
    assert "echo $(PYTHON)" not in darwin
    # Official wrapper before the raw framework python3 (CI 33708366478).
    assert darwin.find("Contents/Resources/python") < darwin.find(
        "LibreOfficePython.framework/Versions/Current/bin/python3"
    )


def test_makefile_check_lo_python_probes_officehelper_import() -> None:
    """Non-empty LO_PYTHON is not enough — brew-cask framework python3 failed import."""
    text = MAKEFILE.read_text(encoding="utf-8")
    recipe = text.split("_check-lo-python:", 1)[1].split("test-uno:", 1)[0]
    assert "import officehelper, uno" in recipe
    assert "LO_PYTHON_ENV" in recipe
    assert "LO_PYTHON_UNSET" in recipe
    assert "echo $(PYTHON)" not in recipe
    for target in ("test-uno:", "test-mock-sidebar:", "test-visible:"):
        block = text.split(target, 1)[1].split("\n\n", 1)[0]
        assert "LO_PYTHON_ENV" in block
        assert "LO_PYTHON_UNSET" in block


def test_makefile_lo_python_probe_does_not_silently_use_venv() -> None:
    """The old `|| echo $(PYTHON)` launched .venv and segfaulted on macOS."""
    text = MAKEFILE.read_text(encoding="utf-8")
    assert "echo $(PYTHON)" not in text
    assert "_check-lo-python" in text
    assert "test-uno: _check-lo-python" in text


def test_makefile_linux_lo_python_prefers_usr_bin() -> None:
    """Ubuntu CI: setup-python shadows PATH python3; python3-uno is /usr/bin/python3."""
    text = MAKEFILE.read_text(encoding="utf-8")
    darwin = text.split("ifeq ($(UNAME_S),Darwin)", 1)[1].split("else", 1)[0]
    linux = text.split("ifeq ($(UNAME_S),Darwin)", 1)[1].split("else", 1)[1]
    assert '/usr/bin/python3 -c "import uno"' in linux
    assert "echo /usr/bin/python3" in linux
    assert "/usr/bin/python3" not in darwin


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
