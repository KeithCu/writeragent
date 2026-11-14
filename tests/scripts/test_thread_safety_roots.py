# WriterAgent - Tests for Thread Safety Roots
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for scripts/thread_safety_roots.py."""

from __future__ import annotations

from pathlib import Path
import re

import pytest

from scripts.thread_safety_roots import (
    SYNC_HOST_ENTRYPOINTS,
    sync_host_entrypoint_regex,
    write_opengrep_rules,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SEMGREP_TEMPLATE = _REPO_ROOT / "tests" / "semgrep" / "uno_thread_safety.yml"


def test_sync_host_entrypoints_have_definitions_in_plugin() -> None:
    # Semgrep yellow roots must resolve to a definition under plugin/, not stale names
    # after helpers are inlined or renamed. Verify each declared root against plugin/ sources
    # instead of duplicating a frozen name list in this test.
    plugin_dir = _REPO_ROOT / "plugin"
    def_re = re.compile(r"^\s*(?:async\s+)?def\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(", re.MULTILINE)
    defined_names: set[str] = set()
    for py_file in plugin_dir.rglob("*.py"):
        parts = set(py_file.parts)
        if "contrib" in parts or "lib" in parts:
            continue
        defined_names.update(def_re.findall(py_file.read_text(encoding="utf-8")))

    assert SYNC_HOST_ENTRYPOINTS
    missing = SYNC_HOST_ENTRYPOINTS - defined_names
    assert not missing, f"Entrypoints missing function definitions in plugin/: {missing}"


def test_sync_host_entrypoint_regex_matching() -> None:
    regex = sync_host_entrypoint_regex()
    for name in SYNC_HOST_ENTRYPOINTS:
        assert re.fullmatch(regex, name) is not None, f"Expected {name} to match regex"

    invalid_names = [
        "execute_python_addin_impl",
        "python_extra",
        "mypy",
        "prompt2",
    ]
    for bad_name in invalid_names:
        assert re.fullmatch(regex, bad_name) is None, f"Expected {bad_name} not to match regex"


def test_template_contains_token_once_and_no_bare_pipe() -> None:
    content = _SEMGREP_TEMPLATE.read_text(encoding="utf-8")
    assert content.count("__SYNC_HOST_ENTRYPOINT_REGEX__") == 1
    assert "execute_python_addin|" not in content


def test_write_opengrep_rules(tmp_path: Path) -> None:
    dest_path = tmp_path / "rendered" / "uno_thread_safety.yml"

    write_opengrep_rules(_SEMGREP_TEMPLATE, dest_path)
    assert dest_path.is_file()

    rendered = dest_path.read_text(encoding="utf-8")
    expected_regex = sync_host_entrypoint_regex()
    assert f"regex: {expected_regex}" in rendered

    bad_template = tmp_path / "bad_template.yml"
    bad_template.write_text("no token here", encoding="utf-8")
    with pytest.raises(SystemExit):
        write_opengrep_rules(bad_template, tmp_path / "out1.yml")

    bad_template2 = tmp_path / "bad_template2.yml"
    bad_template2.write_text(
        "__SYNC_HOST_ENTRYPOINT_REGEX__ and __SYNC_HOST_ENTRYPOINT_REGEX__",
        encoding="utf-8",
    )
    with pytest.raises(SystemExit):
        write_opengrep_rules(bad_template2, tmp_path / "out2.yml")
