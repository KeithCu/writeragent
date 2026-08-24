# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Guard the unit-pytest / live-LibreOffice split used by ``make pytest``."""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MAKEFILE = REPO_ROOT / "Makefile"
PYPROJECT = REPO_ROOT / "pyproject.toml"

# Exact command documented in the Makefile comment and ``make pytest``.
PYTEST_UNIT_CMD = (
    '$(PYTHON) -m pytest tests -m "not slow and not integration" '
    "--ignore-glob='*_uno.py'"
)


def _makefile_text() -> str:
    return MAKEFILE.read_text(encoding="utf-8")


def test_makefile_documents_exact_pytest_unit_command() -> None:
    text = _makefile_text()
    assert f"Exact command: {PYTEST_UNIT_CMD}" in text
    assert "PYTEST_UNIT =" in text
    assert '-m "not slow and not integration"' in text
    assert '--ignore-glob="*_uno.py"' in text


def test_makefile_pytest_target_is_unit_only_no_xdist() -> None:
    text = _makefile_text()
    pytest_block = re.search(
        r"^pytest:\n(?:\t.*\n)+",
        text,
        re.MULTILINE,
    )
    assert pytest_block is not None, "missing Makefile pytest: target"
    body = pytest_block.group(0)
    assert "$(PYTEST_UNIT)" in body
    assert "testing_runner" not in body
    assert re.search(r"(^|\s)-n(\s|$)", body) is None
    assert "xdist" not in body
    assert "pytest-xdist" not in text


def test_makefile_test_run_is_pytest_then_serial_testing_runner() -> None:
    text = _makefile_text()
    test_run = re.search(
        r"^test-run:\n(?:\t.*\n)+",
        text,
        re.MULTILINE,
    )
    assert test_run is not None, "missing Makefile test-run: target"
    body = test_run.group(0)
    assert "$(MAKE) pytest" in body
    assert "plugin.testing_runner" in body
    assert re.search(r"(^|\s)-n(\s|$)", body) is None
    pytest_at = body.index("$(MAKE) pytest")
    runner_at = body.index("plugin.testing_runner")
    assert pytest_at < runner_at


def test_pyproject_addopts_ignore_uno_glob() -> None:
    text = PYPROJECT.read_text(encoding="utf-8")
    assert "--ignore-glob=*_uno.py" in text
    assert "--ignore=tests/uno" in text


def test_uno_suffix_files_exist_for_native_runner() -> None:
    uno_files = list((REPO_ROOT / "tests").rglob("*_uno.py"))
    assert uno_files, "expected native *_uno.py suites under tests/"
    names = {path.name for path in uno_files}
    assert "test_document_uno.py" not in names
    assert "test_linebreak_uno.py" not in names
