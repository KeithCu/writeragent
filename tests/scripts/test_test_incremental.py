# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for scripts/test_incremental.py."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from scripts import test_incremental as tst
from scripts.typecheck_incremental import EXIT_FALLBACK_FULL


def _touch(root: Path, rel: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x = 1\n", encoding="utf-8")


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _init_repo(root: Path) -> None:
    _touch(root, "plugin/base.py")
    _touch(root, "tests/test_base.py")
    _git(root, "init", "-b", "master")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "base")


@pytest.mark.parametrize(
    ("changed_path", "expected_reason"),
    [
        ("pyproject.toml", "config"),
        ("Makefile", "config"),
        ("tests/conftest.py", "conftest"),
        ("tests/writer/conftest.py", "conftest"),
        ("tests/uno_fakes.py", "shared test helper"),
        ("tests/framework/stream_test_helpers.py", "shared test helper"),
    ],
)
def test_find_full_fallback_trigger_matches_config_conftest_and_helpers(
    changed_path: str, expected_reason: str
) -> None:
    hit = tst.find_full_fallback_trigger([changed_path])
    assert hit == (expected_reason, changed_path)


def test_find_full_fallback_trigger_ignores_normal_tests_uno_and_semgrep() -> None:
    assert (
        tst.find_full_fallback_trigger(
            [
                "plugin/a/foo.py",
                "tests/a/test_foo.py",
                "tests/a/test_foo_uno.py",
                "tests/__init__.py",
                "tests/semgrep/uno_thread_safety.ok.py",
                "README.md",
            ]
        )
        is None
    )


def test_select_pytest_files_maps_modules_and_excludes_uno(tmp_path: Path) -> None:
    for rel in (
        "plugin/a/b/foo.py",
        "plugin/pkg/__init__.py",
        "tests/a/test_foo.py",
        "tests/a/test_foo_edge.py",
        "tests/a/test_foo_uno.py",
        "tests/a/test_foobar.py",
        "tests/pkg/test_pkg.py",
        "tests/other/test_direct.py",
        "tests/other/test_direct_uno.py",
    ):
        _touch(tmp_path, rel)

    selected = tst.select_pytest_files(
        [
            "plugin/a/b/foo.py",
            "plugin/pkg/__init__.py",
            "tests/other/test_direct.py",
            "tests/other/test_direct_uno.py",
            "tests/other/test_deleted.py",
            "README.md",
        ],
        repo_root=tmp_path,
    )
    assert selected == [
        "tests/a/test_foo.py",
        "tests/a/test_foo_edge.py",
        "tests/other/test_direct.py",
        "tests/pkg/test_pkg.py",
    ]


def test_main_exits_zero_when_no_changed_files(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _init_repo(tmp_path)

    rc = tst.main(["--", sys.executable, "-c", "raise SystemExit(9)"], repo_root=tmp_path)
    assert rc == 0
    assert "no changed files" in capsys.readouterr().out


def test_main_exits_zero_when_nothing_maps(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _init_repo(tmp_path)
    _touch(tmp_path, "plugin/untested_mod.py")
    _touch(tmp_path, "tests/test_only_uno.py")

    rc = tst.main(["--", sys.executable, "-c", "raise SystemExit(9)"], repo_root=tmp_path)
    assert rc == 0
    assert "no matching pytest files" in capsys.readouterr().out


def test_main_returns_fallback_code_on_conftest_change(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _init_repo(tmp_path)
    _touch(tmp_path, "tests/conftest.py")

    rc = tst.main(["--", sys.executable, "-c", "raise SystemExit(9)"], repo_root=tmp_path)
    assert rc == EXIT_FALLBACK_FULL
    assert "falling back to full pytest" in capsys.readouterr().out


def test_main_runs_pytest_command_on_selected_files_only(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _init_repo(tmp_path)
    _touch(tmp_path, "plugin/alpha.py")
    _touch(tmp_path, "tests/test_alpha.py")
    _touch(tmp_path, "tests/test_alpha_extra.py")
    _touch(tmp_path, "tests/test_alpha_uno.py")
    _git(tmp_path, "add", "plugin/alpha.py", "tests/test_alpha.py", "tests/test_alpha_extra.py", "tests/test_alpha_uno.py")
    _git(tmp_path, "commit", "-m", "committed alpha")

    # Dirty change to plugin/alpha.py (unstaged) and tests/test_beta.py (untracked)
    (tmp_path / "plugin/alpha.py").write_text("x = 2\n", encoding="utf-8")
    _touch(tmp_path, "tests/test_beta.py")

    marker = tmp_path / "seen.txt"
    rc = tst.main(
        [
            "--",
            sys.executable,
            "-c",
            f"import pathlib, sys; pathlib.Path({str(marker)!r}).write_text('\\n'.join(sys.argv[1:]))",
        ],
        repo_root=tmp_path,
    )
    assert rc == 0
    assert marker.read_text(encoding="utf-8").splitlines() == [
        "tests/test_alpha.py",
        "tests/test_alpha_extra.py",
        "tests/test_beta.py",
    ]
    out = capsys.readouterr().out
    assert "running pytest on 3 file(s)" in out


def test_select_uno_files_maps_modules_and_excludes_non_uno_and_user_profile(tmp_path: Path) -> None:
    for rel in (
        "plugin/a/b/foo.py",
        "plugin/pkg/__init__.py",
        "tests/a/test_foo.py",
        "tests/a/test_foo_uno.py",
        "tests/a/test_foo_edge_uno.py",
        "tests/a/test_foobar_uno.py",
        "tests/pkg/test_pkg_uno.py",
        "tests/other/test_direct.py",
        "tests/other/test_direct_uno.py",
        "tests/chatbot/test_mock_llm_sidebar_uno.py",
        "tests/demo/test_demo_video_uno.py",
    ):
        _touch(tmp_path, rel)

    selected = tst.select_uno_files(
        [
            "plugin/a/b/foo.py",
            "plugin/pkg/__init__.py",
            "tests/other/test_direct.py",
            "tests/other/test_direct_uno.py",
            "tests/chatbot/test_mock_llm_sidebar_uno.py",
            "tests/demo/test_demo_video_uno.py",
            "tests/other/test_deleted_uno.py",
            "README.md",
        ],
        repo_root=tmp_path,
    )
    assert selected == [
        "tests/a/test_foo_edge_uno.py",
        "tests/a/test_foo_uno.py",
        "tests/other/test_direct_uno.py",
        "tests/pkg/test_pkg_uno.py",
    ]


def test_main_uno_exits_zero_when_nothing_maps(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _init_repo(tmp_path)
    _touch(tmp_path, "plugin/untested_mod.py")
    _touch(tmp_path, "tests/test_only_unit.py")

    rc = tst.main(["--uno", "--", sys.executable, "-c", "raise SystemExit(9)"], repo_root=tmp_path)
    assert rc == 0
    assert "no matching UNO files" in capsys.readouterr().out


def test_main_uno_returns_fallback_code_on_conftest_change(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _init_repo(tmp_path)
    _touch(tmp_path, "tests/conftest.py")

    rc = tst.main(["--uno", "--", sys.executable, "-c", "raise SystemExit(9)"], repo_root=tmp_path)
    assert rc == EXIT_FALLBACK_FULL
    assert "falling back to full UNO" in capsys.readouterr().out


def test_main_uno_runs_command_on_selected_files_only(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _init_repo(tmp_path)
    _touch(tmp_path, "plugin/alpha.py")
    _touch(tmp_path, "tests/test_alpha.py")
    _touch(tmp_path, "tests/test_alpha_uno.py")
    _touch(tmp_path, "tests/test_alpha_extra_uno.py")
    _git(tmp_path, "add", "plugin/alpha.py", "tests/test_alpha.py", "tests/test_alpha_uno.py", "tests/test_alpha_extra_uno.py")
    _git(tmp_path, "commit", "-m", "committed alpha")

    (tmp_path / "plugin/alpha.py").write_text("x = 2\n", encoding="utf-8")
    _touch(tmp_path, "tests/test_beta_uno.py")

    marker = tmp_path / "seen_uno.txt"
    rc = tst.main(
        [
            "--uno",
            "--",
            sys.executable,
            "-c",
            f"import pathlib, sys; pathlib.Path({str(marker)!r}).write_text('\\n'.join(sys.argv[1:]))",
        ],
        repo_root=tmp_path,
    )
    assert rc == 0
    assert marker.read_text(encoding="utf-8").splitlines() == [
        "tests/test_alpha_extra_uno.py",
        "tests/test_alpha_uno.py",
        "tests/test_beta_uno.py",
    ]
    assert "running UNO on 3 file(s)" in capsys.readouterr().out


def test_main_uno_print_filter_outputs_space_separated_files(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _init_repo(tmp_path)
    _touch(tmp_path, "plugin/alpha.py")
    _touch(tmp_path, "tests/test_alpha_uno.py")
    _touch(tmp_path, "tests/test_beta_uno.py")

    rc = tst.main(["--uno", "--print-filter"], repo_root=tmp_path)
    assert rc == 0
    captured = capsys.readouterr()
    assert captured.out.strip() == "tests/test_alpha_uno.py tests/test_beta_uno.py"
    assert "running UNO on 2 file(s)" in captured.err


def test_main_uno_print_filter_empty_stdout_when_nothing_maps(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _init_repo(tmp_path)
    _touch(tmp_path, "tests/test_only_unit.py")

    rc = tst.main(["--uno", "--print-filter"], repo_root=tmp_path)
    assert rc == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "no matching UNO files" in captured.err
