# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for scripts/typecheck_incremental.py."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from scripts import typecheck_incremental as ti


def _touch(root: Path, rel: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x = 1\n", encoding="utf-8")


def test_filter_files_for_tool_respects_scopes_and_existing_py(tmp_path: Path) -> None:
    candidates = [
        "plugin/main.py",
        "plugin/with spaces/mod.py",
        "compute_service/server.py",
        "plugin/contrib/vendor.py",
        "plugin/lib/pkg.py",
        "plugin/tests/test_helper.py",
        "plugin/scripting/venv/text_analytics.py",
        "plugin/scripting/venv/quant.py",
        "plugin/docs.md",
        "plugin/deleted.py",
        "tests/scripts/test_other.py",
    ]
    for rel in candidates:
        if rel != "plugin/deleted.py":
            _touch(tmp_path, rel)

    assert ti.filter_files_for_tool("basedpyright", candidates, tmp_path) == [
        "compute_service/server.py",
        "plugin/main.py",
        "plugin/with spaces/mod.py",
    ]
    assert ti.filter_files_for_tool("mypy", candidates, tmp_path) == [
        "compute_service/server.py",
        "plugin/main.py",
        "plugin/scripting/venv/quant.py",
        "plugin/with spaces/mod.py",
    ]
    assert ti.filter_files_for_tool("bandit", candidates, tmp_path) == [
        "plugin/main.py",
        "plugin/scripting/venv/quant.py",
        "plugin/with spaces/mod.py",
    ]
    assert ti.filter_files_for_tool("opengrep", candidates, tmp_path) == [
        "plugin/main.py",
        "plugin/scripting/venv/quant.py",
        "plugin/scripting/venv/text_analytics.py",
        "plugin/tests/test_helper.py",
        "plugin/with spaces/mod.py",
    ]
    assert ti.filter_files_for_tool("pyspector", candidates, tmp_path) == [
        "plugin/main.py",
        "plugin/with spaces/mod.py",
    ]


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def test_get_changed_files_returns_none_without_origin_master(tmp_path: Path) -> None:
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.name", "Test")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _touch(tmp_path, "plugin/a.py")
    _git(tmp_path, "add", "plugin/a.py")
    _git(tmp_path, "commit", "-m", "init")

    assert ti.get_changed_files(tmp_path) is None


def test_get_changed_files_collects_branch_staged_unstaged_and_untracked(tmp_path: Path) -> None:
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.name", "Test")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _touch(tmp_path, "plugin/base.py")
    _touch(tmp_path, "plugin/staged.py")
    _touch(tmp_path, "plugin/unstaged.py")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "base")
    _git(tmp_path, "update-ref", "refs/remotes/origin/master", "HEAD")

    _touch(tmp_path, "plugin/committed branch.py")
    _git(tmp_path, "add", "plugin/committed branch.py")
    _git(tmp_path, "commit", "-m", "branch change")

    _touch(tmp_path, "plugin/staged.py")
    (tmp_path / "plugin/staged.py").write_text("x = 2\n", encoding="utf-8")
    _git(tmp_path, "add", "plugin/staged.py")

    (tmp_path / "plugin/unstaged.py").write_text("x = 3\n", encoding="utf-8")
    _touch(tmp_path, "plugin/untracked.py")

    changed = ti.get_changed_files(tmp_path)
    assert changed == [
        "plugin/committed branch.py",
        "plugin/staged.py",
        "plugin/unstaged.py",
        "plugin/untracked.py",
    ]


def test_chunk_files_splits_at_max_chars() -> None:
    files = [f"plugin/mod_{i:02d}.py" for i in range(6)]
    chunks = ti.chunk_files(files, max_chars=36)
    assert [item for chunk in chunks for item in chunk] == files
    assert len(chunks) == 3


def test_build_invocations_full_and_incremental() -> None:
    assert ti.build_invocations("mypy", ["py", "-m", "mypy"], None) == [["py", "-m", "mypy"]]
    assert ti.build_invocations("bandit", ["py", "-m", "bandit"], None) == [
        ["py", "-m", "bandit", "-r", "plugin"]
    ]
    assert ti.build_invocations("opengrep", ["og", "scan"], None) == [["og", "scan", "plugin"]]
    assert ti.build_invocations("pyspector", ["py", "rp.py"], None) == [
        ["py", "rp.py", "scan", "plugin", "--ai", "-c", "pyspector.toml", "--msg=False"]
    ]

    assert ti.build_invocations("pyspector", ["py", "rp.py"], ["plugin/a.py", "plugin/b.py"]) == [
        ["py", "rp.py", "scan", "plugin/a.py", "--ai", "-c", "pyspector.toml", "--msg=False"],
        ["py", "rp.py", "scan", "plugin/b.py", "--ai", "-c", "pyspector.toml", "--msg=False"],
    ]


def test_main_skips_when_no_files_in_scope(capsys: pytest.CaptureFixture[str]) -> None:
    with (
        patch.object(ti, "get_changed_files", return_value=["README.md"]),
        patch.object(ti.subprocess, "run") as mock_run,
    ):
        rc = ti.main(["--tool", "mypy", "--", "python", "-m", "mypy"])

    assert rc == 0
    mock_run.assert_not_called()
    assert "mypy: no changed files in scope — skipped" in capsys.readouterr().out


def test_main_falls_back_to_full_when_merge_base_missing(capsys: pytest.CaptureFixture[str]) -> None:
    with (
        patch.object(ti, "get_changed_files", return_value=None),
        patch.object(ti.subprocess, "run") as mock_run,
    ):
        mock_run.return_value.returncode = 0
        rc = ti.main(["--tool", "bandit", "--", "python", "-m", "bandit", "-c", "pyproject.toml"])

    assert rc == 0
    mock_run.assert_called_once()
    cmd = mock_run.call_args.args[0]
    assert cmd == ["python", "-m", "bandit", "-c", "pyproject.toml", "-r", "plugin"]
    assert "origin/master merge-base not found" in capsys.readouterr().out
