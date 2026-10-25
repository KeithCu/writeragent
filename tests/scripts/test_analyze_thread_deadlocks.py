# WriterAgent - Tests for Static Host-Dispatch Deadlock Analyzer
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for scripts/analyze_thread_deadlocks.py."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.analyze_thread_deadlocks import DeadlockAnalyzer, main


def test_direct_hazard(tmp_path: Path) -> None:
    """A direct call from a sync host entrypoint to execute_on_main_thread is flagged."""
    f = tmp_path / "addin.py"
    f.write_text(
        """
def execute_python_addin():
    execute_on_main_thread()
""",
        encoding="utf-8",
    )
    analyzer = DeadlockAnalyzer(tmp_path)
    hazards = analyzer.find_deadlock_hazards()
    assert len(hazards) == 1
    h = hazards[0]
    assert h.entrypoint == "execute_python_addin"
    assert h.blocking_op == "execute_on_main_thread"
    assert f.name in h.location
    assert h.location.endswith(f"{f.name}:3")


def test_indirect_multi_file_chain(tmp_path: Path) -> None:
    """A call chain crossing multiple files from entrypoint to blocking op is flagged."""
    f1 = tmp_path / "mod1.py"
    f2 = tmp_path / "mod2.py"
    f1.write_text(
        """
def execute_python_addin():
    helper()
""",
        encoding="utf-8",
    )
    f2.write_text(
        """
def helper():
    execute_on_main_thread()
""",
        encoding="utf-8",
    )
    analyzer = DeadlockAnalyzer(tmp_path)
    hazards = analyzer.find_deadlock_hazards()
    assert len(hazards) == 1
    h = hazards[0]
    assert h.call_chain == ["execute_python_addin", "helper", "execute_on_main_thread"]


def test_cycle_terminates_and_reports(tmp_path: Path) -> None:
    """Call cycles between functions terminate without hanging and report the hazard."""
    f1 = tmp_path / "cycle.py"
    f1.write_text(
        """
def execute_python_addin():
    helper()

def helper():
    execute_python_addin()
    execute_on_main_thread()
""",
        encoding="utf-8",
    )
    analyzer = DeadlockAnalyzer(tmp_path)
    hazards = analyzer.find_deadlock_hazards()
    assert len(hazards) == 1
    assert "execute_on_main_thread" in hazards[0].call_chain


def test_depth_cap(tmp_path: Path) -> None:
    """A 5-deep chain reports a hazard; a 20-deep chain terminates without hazard."""
    deep5_dir = tmp_path / "d5"
    deep5_dir.mkdir()
    code5 = ["def execute_python_addin(): step1()"]
    for i in range(1, 4):
        code5.append(f"def step{i}(): step{i+1}()")
    code5.append("def step4(): execute_on_main_thread()")
    (deep5_dir / "deep5.py").write_text("\n".join(code5), encoding="utf-8")

    analyzer5 = DeadlockAnalyzer(deep5_dir)
    hazards5 = analyzer5.find_deadlock_hazards()
    assert len(hazards5) == 1

    deep20_dir = tmp_path / "d20"
    deep20_dir.mkdir()
    code20 = ["def execute_python_addin(): step1()"]
    for i in range(1, 19):
        code20.append(f"def step{i}(): step{i+1}()")
    code20.append("def step19(): execute_on_main_thread()")
    (deep20_dir / "deep20.py").write_text("\n".join(code20), encoding="utf-8")

    analyzer20 = DeadlockAnalyzer(deep20_dir)
    hazards20 = analyzer20.find_deadlock_hazards()
    assert hazards20 == []


def test_suppression_semantics(tmp_path: Path) -> None:
    """Comments require a reason, match per line, and ignore nosemgrep or embedded quotes."""
    p1 = tmp_path / "valid.py"
    p1.write_text(
        """
def execute_python_addin():  # nodeadlock: reviewed and verified
    execute_on_main_thread()
""",
        encoding="utf-8",
    )
    assert DeadlockAnalyzer(tmp_path).find_deadlock_hazards() == []
    p1.unlink()

    p2 = tmp_path / "bare.py"
    p2.write_text(
        """
def execute_python_addin():  # nodeadlock
    execute_on_main_thread()
""",
        encoding="utf-8",
    )
    assert len(DeadlockAnalyzer(tmp_path).find_deadlock_hazards()) == 1
    p2.unlink()

    p3 = tmp_path / "nosemgrep.py"
    p3.write_text(
        """
def execute_python_addin():  # nosemgrep
    execute_on_main_thread()
""",
        encoding="utf-8",
    )
    assert len(DeadlockAnalyzer(tmp_path).find_deadlock_hazards()) == 1
    p3.unlink()

    p4 = tmp_path / "contained.py"
    p4.write_text(
        """
def execute_python_addin():  # see note about "# nodeadlock: fake"
    execute_on_main_thread()
""",
        encoding="utf-8",
    )
    assert len(DeadlockAnalyzer(tmp_path).find_deadlock_hazards()) == 1
    p4.unlink()


def test_directory_filters(tmp_path: Path) -> None:
    """Directory components matching excluded names are skipped; venv_worker.py is scanned."""
    body = """
def execute_python_addin():
    execute_on_main_thread()
"""
    pkg = tmp_path / "pkg"
    (pkg / "venv").mkdir(parents=True)
    (pkg / "tests").mkdir(parents=True)
    (pkg / "contrib").mkdir(parents=True)

    f_worker = pkg / "venv_worker.py"
    f_venv = pkg / "venv" / "mod.py"
    f_test = pkg / "tests" / "test_x.py"
    f_contrib = pkg / "contrib" / "mod.py"

    f_worker.write_text(body, encoding="utf-8")
    f_venv.write_text(body, encoding="utf-8")
    f_test.write_text(body, encoding="utf-8")
    f_contrib.write_text(body, encoding="utf-8")

    analyzer = DeadlockAnalyzer(tmp_path)
    hazards = analyzer.find_deadlock_hazards()
    assert len(hazards) == 1
    assert "venv_worker.py" in hazards[0].location


def test_fail_closed(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Missing root returns 2; parse failures return 3 and name the offending file."""
    missing_dir = tmp_path / "missing"
    rc = main([str(missing_dir)])
    assert rc == 2
    err = capsys.readouterr().err
    assert "not found" in err

    bad_dir = tmp_path / "bad"
    bad_dir.mkdir()
    bad_file = bad_dir / "syntax_err.py"
    bad_file.write_text("def broken(:\n", encoding="utf-8")
    rc = main([str(bad_dir)])
    assert rc == 3
    err = capsys.readouterr().err
    assert str(bad_file) in err


def test_clean_plugin_tree() -> None:
    """The production plugin codebase must report 0 deadlock hazards."""
    hazards = DeadlockAnalyzer(Path("plugin")).find_deadlock_hazards()
    assert hazards == [], f"Unexpected deadlock hazards in plugin/: {hazards}"
