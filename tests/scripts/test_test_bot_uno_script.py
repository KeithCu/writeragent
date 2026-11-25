# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for scripts/test_bot_uno.py."""

from __future__ import annotations

import subprocess
from pathlib import Path

from scripts import test_bot_uno as tbu


def _touch(root: Path, rel: str, text: str = "x = 1\n") -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


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
    _touch(root, "tests/writer/test_base_uno.py")
    _git(root, "init", "-b", "master")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "base")


def test_list_branch_uno_files_selects_committed_and_dirty_changes(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _touch(tmp_path, "tests/writer/test_new_uno.py")
    _git(tmp_path, "checkout", "-b", "feature")
    _git(tmp_path, "add", "tests/writer/test_new_uno.py")
    _git(tmp_path, "commit", "-m", "add uno")
    _touch(tmp_path, "tests/writer/test_base_uno.py", "x = 2\n")
    _touch(tmp_path, "tests/writer/test_dirty_uno.py")
    _touch(tmp_path, "tests/writer/test_plain.py")
    _touch(tmp_path, "plugin/base.py", "x = 2\n")
    _touch(tmp_path, "tests/chatbot/test_mock_llm_sidebar_uno.py")
    selected = tbu.list_branch_uno_files(tmp_path)
    assert selected == [
        "tests/writer/test_base_uno.py",
        "tests/writer/test_dirty_uno.py",
        "tests/writer/test_new_uno.py",
    ]


def test_list_branch_uno_files_omits_deleted_uno_tests(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _git(tmp_path, "checkout", "-b", "feature")
    _git(tmp_path, "rm", "tests/writer/test_base_uno.py")
    _git(tmp_path, "commit", "-m", "remove uno")
    assert tbu.list_branch_uno_files(tmp_path) == []


def test_main_print_files_and_skip_when_probe_missing(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("SKIP_UNO", raising=False)
    _init_repo(tmp_path)
    _touch(tmp_path, "tests/writer/test_new_uno.py")
    _git(tmp_path, "checkout", "-b", "feature")
    _git(tmp_path, "add", "tests/writer/test_new_uno.py")
    _git(tmp_path, "commit", "-m", "add uno")
    monkeypatch.setattr(tbu, "is_main_checkout", lambda repo_root, main=None: False)
    printed: list[str] = []

    def _fake_print(*args: object, **kwargs: object) -> None:
        printed.append(" ".join(str(a) for a in args))

    monkeypatch.setattr("builtins.print", _fake_print)
    assert tbu.main(["--print-files"], repo_root=tmp_path) == 0
    assert printed == ["tests/writer/test_new_uno.py"]

    monkeypatch.setenv("UNO_PROBE", str(tmp_path / "missing-uno-probe.sh"))
    printed.clear()
    assert tbu.main([], repo_root=tmp_path) == 0
    assert any("uno-probe.sh not found" in line for line in printed)


def test_main_skip_uno_env(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("SKIP_UNO", "1")
    monkeypatch.setattr(tbu, "is_main_checkout", lambda repo_root, main=None: False)
    printed: list[str] = []
    monkeypatch.setattr("builtins.print", lambda *a, **k: printed.append(" ".join(str(x) for x in a)))
    assert tbu.main([], repo_root=tmp_path) == 0
    assert printed == ["test-bot: SKIP_UNO=1 — skipping isolated UNO tests"]


def test_list_branch_uno_files_selects_stem_and_1hop_uno_tests_and_caps_large_expansions(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    _init_repo(tmp_path)
    _touch(tmp_path, "plugin/doc/text_helpers.py", "def clean():\n    return ''\n")
    _touch(tmp_path, "plugin/writer/search.py", "from plugin.doc.text_helpers import clean\n")
    _touch(tmp_path, "tests/doc/test_text_helpers_uno.py", "x = 1\n")
    _touch(tmp_path, "tests/writer/test_search_uno.py", "x = 1\n")
    _touch(tmp_path, "tests/writer/test_uses_search_uno.py", "import plugin.writer.search\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "add doc and writer modules")

    _git(tmp_path, "checkout", "-b", "feature")
    _touch(tmp_path, "plugin/doc/text_helpers.py", "def clean():\n    return 'v2'\n")
    _git(tmp_path, "add", "plugin/doc/text_helpers.py")
    _git(tmp_path, "commit", "-m", "edit text_helpers")

    assert tbu.list_branch_uno_files(tmp_path) == [
        "tests/doc/test_text_helpers_uno.py",
        "tests/writer/test_search_uno.py",
    ]

    monkeypatch.setattr(tbu, "UNO_MAX_EXPANDED_FILES", 1)
    assert tbu.list_branch_uno_files(tmp_path) == ["tests/doc/test_text_helpers_uno.py"]
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "exceed cap (1)" in captured.err


def test_main_scales_timeout_honours_env_and_distinguishes_124_and_75(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    monkeypatch.delenv("SKIP_UNO", raising=False)
    monkeypatch.delenv("UNO_PROBE_TIMEOUT", raising=False)
    monkeypatch.setattr(tbu, "is_main_checkout", lambda repo_root, main=None: False)
    monkeypatch.setattr(
        tbu,
        "list_branch_uno_files",
        lambda repo_root: [f"tests/writer/test_f{idx}_uno.py" for idx in range(15)],
    )
    probe = tmp_path / "uno-probe.sh"
    probe.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    probe.chmod(0o755)
    monkeypatch.setenv("UNO_PROBE", str(probe))

    seen_cmds: list[list[str]] = []

    def _run(rc: int):
        return lambda cmd, **_: (seen_cmds.append(cmd), subprocess.CompletedProcess(cmd, rc))[1]

    monkeypatch.setattr(tbu.subprocess, "run", _run(124))
    assert tbu.main([], repo_root=tmp_path) == 124
    assert seen_cmds[0][:3] == [str(probe), "--timeout", "375"]
    assert "timed out after 375s (exit 124)" in capsys.readouterr().err

    monkeypatch.setattr(tbu.subprocess, "run", _run(75))
    assert tbu.main([], repo_root=tmp_path) == 75
    assert "all slots busy (exit 75)" in capsys.readouterr().err

    monkeypatch.setenv("UNO_PROBE_TIMEOUT", "900")
    monkeypatch.setattr(tbu.subprocess, "run", _run(0))
    assert tbu.main([], repo_root=tmp_path) == 0
    assert "--timeout" not in seen_cmds[2]
