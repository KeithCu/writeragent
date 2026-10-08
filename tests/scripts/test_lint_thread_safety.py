# WriterAgent - Tests for Thread Safety AST Linter & Cross-File Taint
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for scripts/lint_thread_safety.py."""

from pathlib import Path

from scripts.lint_thread_safety import (
    scan_cross_file_callgraph,
)
import pytest


def test_clean_plugin_scan() -> None:
    """The production plugin codebase must report 0 thread safety violations."""
    findings = scan_cross_file_callgraph(Path("plugin"))
    assert findings == [], f"Unexpected violations in plugin/: {findings}"


def test_detects_direct_off_main_uno(tmp_path: Path) -> None:
    """A background function directly calling a UNO source must be flagged."""
    worker_file = tmp_path / "worker.py"
    worker_file.write_text(
        """
from plugin.framework.thread_guard import background
from plugin.framework.uno_context import get_active_document

@background
def bad_worker():
    doc = get_active_document()
""",
        encoding="utf-8",
    )

    findings = scan_cross_file_callgraph(tmp_path)
    assert len(findings) == 1
    assert findings[0].rule_id == "uno-off-main-thread-callgraph"
    assert "bad_worker" in findings[0].message
    assert "get_active_document" in findings[0].message


def test_detects_cross_file_off_main_uno(tmp_path: Path) -> None:
    """A call chain crossing multiple files from @background to UNO must be detected."""
    file_a = tmp_path / "mod_a.py"
    file_b = tmp_path / "mod_b.py"
    file_c = tmp_path / "mod_c.py"

    file_a.write_text(
        """
from plugin.framework.thread_guard import background
from mod_b import step_b

@background
def start_job():
    step_b()
""",
        encoding="utf-8",
    )

    file_b.write_text(
        """
from mod_c import step_c

def step_b():
    step_c()
""",
        encoding="utf-8",
    )

    file_c.write_text(
        """
def step_c():
    from plugin.framework.uno_context import get_desktop
    dt = get_desktop()
""",
        encoding="utf-8",
    )

    findings = scan_cross_file_callgraph(tmp_path)
    assert len(findings) == 1
    f = findings[0]
    assert f.rule_id == "uno-off-main-thread-callgraph"
    assert "start_job -> step_b -> step_c -> get_desktop" in f.message


@pytest.mark.parametrize(
    "value, value_2",
    [
        # Calls dispatched via execute_on_main_thread or post_to_main_thread are safe.
        pytest.param("""
from plugin.framework.thread_guard import background
from plugin.framework.queue_executor import execute_on_main_thread
from mod_b import step_b

@background
def safe_job():
    execute_on_main_thread(step_b)
""", """
from plugin.framework.uno_context import get_active_document

def step_b():
    return get_active_document()
""", id="test_marshaled_call_is_sanitized"),
        # A UNO call guarded by on_main_thread() check must not be flagged.
        pytest.param("""
from plugin.framework.thread_guard import background
from mod_b import step_b

@background
def guarded_job():
    step_b()
""", """
from plugin.framework.thread_guard import on_main_thread
from plugin.framework.uno_context import get_desktop

def step_b():
    if not on_main_thread():
        return None
    return get_desktop()
""", id="test_guarded_branch_is_sanitized"),
    ],
)
def test_marshaled_call_is_sanitized(tmp_path: Path, value, value_2) -> None:
    file_a = tmp_path / "mod_a.py"
    file_b = tmp_path / "mod_b.py"

    file_a.write_text(
        value,
        encoding="utf-8",
    )

    file_b.write_text(
        value_2,
        encoding="utf-8",
    )

    findings = scan_cross_file_callgraph(tmp_path)
    assert findings == []

def test_run_in_background_target_detected(tmp_path: Path) -> None:
    """Functions passed as target to run_in_background are treated as background roots."""
    file_a = tmp_path / "mod_a.py"
    file_b = tmp_path / "mod_b.py"

    file_a.write_text(
        """
from plugin.framework.worker_pool import run_in_background
from mod_b import bg_worker

def launch():
    run_in_background(bg_worker)
""",
        encoding="utf-8",
    )

    file_b.write_text(
        """
from plugin.framework.uno_context import get_ctx

def bg_worker():
    ctx = get_ctx()
""",
        encoding="utf-8",
    )

    findings = scan_cross_file_callgraph(tmp_path)
    assert len(findings) == 1
    assert "bg_worker" in findings[0].message
    assert "get_ctx" in findings[0].message
