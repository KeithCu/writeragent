# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for scripts/typecheck_incremental.py."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import typecheck_incremental as ti

REPO_ROOT = Path(__file__).resolve().parents[2]


def _touch(root: Path, rel: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x = 1\n", encoding="utf-8")


def _copy_configs(root: Path) -> None:
    shutil.copy2(REPO_ROOT / "pyproject.toml", root / "pyproject.toml")
    shutil.copy2(REPO_ROOT / "pyspector.toml", root / "pyspector.toml")


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _init_repo_with_master(root: Path) -> None:
    _copy_configs(root)
    _touch(root, "plugin/base.py")
    _git(root, "init", "-b", "master")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "base")
    _git(root, "checkout", "-b", "feature")


def test_filter_files_for_tool_reads_real_configs_and_existing_py(tmp_path: Path) -> None:
    _copy_configs(tmp_path)
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
    assert ti.filter_files_for_tool(
        "opengrep",
        candidates,
        tmp_path,
        cli_excludes=("plugin/contrib", "plugin/lib"),
    ) == [
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


def test_get_changed_files_collects_only_staged_unstaged_and_untracked(tmp_path: Path) -> None:
    _init_repo_with_master(tmp_path)
    _git(tmp_path, "checkout", "master")
    _touch(tmp_path, "plugin/staged.py")
    _touch(tmp_path, "plugin/unstaged.py")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "more base")
    _git(tmp_path, "checkout", "-B", "feature", "master")

    _touch(tmp_path, "plugin/committed branch.py")
    _git(tmp_path, "add", "plugin/committed branch.py")
    _git(tmp_path, "commit", "-m", "branch change")

    (tmp_path / "plugin/staged.py").write_text("x = 2\n", encoding="utf-8")
    _git(tmp_path, "add", "plugin/staged.py")

    (tmp_path / "plugin/unstaged.py").write_text("x = 3\n", encoding="utf-8")
    _touch(tmp_path, "plugin/untracked.py")

    assert ti.get_changed_files(tmp_path) == [
        "plugin/staged.py",
        "plugin/unstaged.py",
        "plugin/untracked.py",
    ]


def test_get_changed_files_works_in_linked_worktree(tmp_path: Path) -> None:
    main_repo = tmp_path / "main"
    worktree = tmp_path / "wa-st-feature"
    main_repo.mkdir()
    _init_repo_with_master(main_repo)

    _git(main_repo, "worktree", "add", "-b", "wt-branch", str(worktree), "master")
    _touch(worktree, "plugin/wt_committed.py")
    _git(worktree, "add", "plugin/wt_committed.py")
    _git(worktree, "commit", "-m", "worktree commit")

    _touch(worktree, "plugin/wt_dirty.py")

    assert ti.get_changed_files(worktree) == [
        "plugin/wt_dirty.py",
    ]


def test_chunk_files_splits_at_max_chars() -> None:
    files = [f"plugin/mod_{i:02d}.py" for i in range(6)]
    chunks = ti.chunk_files(files, max_chars=36)
    assert [item for chunk in chunks for item in chunk] == files
    assert len(chunks) == 3


def test_build_invocations_substitutes_target_placeholder_or_appends() -> None:
    assert ti.build_invocations(["py", "-m", "mypy"], ["plugin/a.py", "plugin/b.py"]) == [
        ["py", "-m", "mypy", "plugin/a.py", "plugin/b.py"]
    ]
    assert ti.build_invocations(
        ["py", "rp.py", "scan", "{target}", "--ai"],
        ["plugin/a.py", "plugin/b.py"],
    ) == [
        ["py", "rp.py", "scan", "plugin/a.py", "--ai"],
        ["py", "rp.py", "scan", "plugin/b.py", "--ai"],
    ]


def test_main_exits_zero_when_no_changed_files(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _git(tmp_path, "init", "-b", "main")
    _git(tmp_path, "config", "user.name", "Test")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _touch(tmp_path, "plugin/a.py")
    _git(tmp_path, "add", "plugin/a.py")
    _git(tmp_path, "commit", "-m", "init")

    rc = ti.main(["--tool", "bandit", "--", sys.executable, "-c", "raise SystemExit(9)"], repo_root=tmp_path)
    assert rc == 0
    assert "bandit: no changed files — nothing to check" in capsys.readouterr().out


def test_main_returns_fallback_code_when_config_changed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _init_repo_with_master(tmp_path)
    (tmp_path / "pyproject.toml").write_text(
        (tmp_path / "pyproject.toml").read_text(encoding="utf-8") + "\n# edit\n",
        encoding="utf-8",
    )

    rc = ti.main(
        ["--tool", "mypy", "--config-path", "pyproject.toml", "--", sys.executable, "-m", "mypy"],
        repo_root=tmp_path,
    )
    assert rc == ti.EXIT_FALLBACK_FULL
    assert "config changed (pyproject.toml)" in capsys.readouterr().out


def test_main_returns_fallback_code_when_config_dir_file_changed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _init_repo_with_master(tmp_path)
    _touch(tmp_path, "tests/semgrep/new_rule.yml")

    rc = ti.main(
        [
            "--tool",
            "pyspector",
            "--root",
            "plugin",
            "--config-path",
            "tests/semgrep",
            "--",
            sys.executable,
            "-c",
            "pass",
        ],
        repo_root=tmp_path,
    )
    assert rc == ti.EXIT_FALLBACK_FULL
    assert "config changed (tests/semgrep/new_rule.yml)" in capsys.readouterr().out


def test_main_returns_fallback_code_when_exceeding_max_files(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _init_repo_with_master(tmp_path)
    _touch(tmp_path, "plugin/a.py")
    _touch(tmp_path, "plugin/b.py")

    rc = ti.main(
        ["--tool", "pyspector", "--max-files", "1", "--", sys.executable, "-c", "pass"],
        repo_root=tmp_path,
    )
    assert rc == ti.EXIT_FALLBACK_FULL
    assert "exceed threshold (1)" in capsys.readouterr().out


def test_main_skips_when_no_files_in_scope(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _init_repo_with_master(tmp_path)
    _touch(tmp_path, "README.md")

    rc = ti.main(["--tool", "mypy", "--", sys.executable, "-c", "raise SystemExit(9)"], repo_root=tmp_path)
    assert rc == 0
    assert "mypy: no changed files in scope — skipped" in capsys.readouterr().out


def test_main_runs_command_on_changed_files_in_real_repo(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _init_repo_with_master(tmp_path)
    _touch(tmp_path, "plugin/with space.py")
    marker = tmp_path / "seen.txt"

    rc = ti.main(
        [
            "--tool",
            "mypy",
            "--",
            sys.executable,
            "-c",
            f"import pathlib, sys; pathlib.Path({str(marker)!r}).write_text('\\n'.join(sys.argv[1:]))",
        ],
        repo_root=tmp_path,
    )
    assert rc == 0
    assert marker.read_text(encoding="utf-8") == "plugin/with space.py"
    out = capsys.readouterr().out
    assert "mypy: checking 1 changed file(s)" in out
