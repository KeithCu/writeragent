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


def test_sync_host_entrypoints_constant() -> None:
    expected = frozenset({
        "execute_python_addin",
        "_execute_python_addin_impl",
        "execute_prompt_addin",
        "_execute_prompt_addin_impl",
        "py",
        "python",
        "prompt",
        "session_key",
        "_notify_thread_violation",
    })
    assert SYNC_HOST_ENTRYPOINTS == expected


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
    template_path = Path("tests/semgrep/uno_thread_safety.yml")
    content = template_path.read_text(encoding="utf-8")
    assert content.count("__SYNC_HOST_ENTRYPOINT_REGEX__") == 1
    assert "execute_python_addin|" not in content


def test_write_opengrep_rules(tmp_path: Path) -> None:
    template_path = Path("tests/semgrep/uno_thread_safety.yml")
    dest_path = tmp_path / "rendered" / "uno_thread_safety.yml"

    write_opengrep_rules(template_path, dest_path)
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


def test_checkers_no_longer_define_local_sets() -> None:
    lint_src = Path("scripts/lint_thread_safety.py").read_text(encoding="utf-8")
    assert "SYNC_ADDIN_FUNCTIONS" not in lint_src

    deadlocks_src = Path("scripts/analyze_thread_deadlocks.py").read_text(encoding="utf-8")
    assert "SYNC_HOST_ENTRYPOINTS = " not in deadlocks_src
