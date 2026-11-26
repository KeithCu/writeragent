# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for scripts/prompt_optimization/eval_ab.py (no network)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from tests.scripts.script_test_support import git

_PO = Path(__file__).resolve().parents[2] / "scripts" / "prompt_optimization"
if str(_PO) not in sys.path:
    sys.path.insert(0, str(_PO))

import eval_ab  # noqa: E402


def _init_repo(root: Path) -> Path:
    (root / "plugin").mkdir(parents=True, exist_ok=True)
    (root / "plugin" / "a.py").write_text("x = 1\n", encoding="utf-8")
    (root / ".gitignore").write_text("*.pyc\n", encoding="utf-8")
    git(root, "init", "-b", "master")
    git(root, "config", "user.name", "Test")
    git(root, "config", "user.email", "test@example.com")
    git(root, "add", ".")
    git(root, "commit", "-m", "base")
    return root


def _write_repeat_files(
    suite_dir: Path,
    side: str,
    cache_key: str,
    rep: int,
    details: list[dict],
) -> None:
    suite_dir.mkdir(parents=True, exist_ok=True)
    summary_path, details_path = eval_ab.result_paths(suite_dir, side, cache_key, rep)
    summary_path.write_text(json.dumps([{"openrouter_id": "m1"}]), encoding="utf-8")
    details_path.write_text(json.dumps(details), encoding="utf-8")


def test_parse_models_resolves_aliases_and_rejects_empty() -> None:
    assert eval_ab.parse_models("inception/mercury-2.5, m1, m1") == [
        "inception/mercury-2.5-preview",
        "m1",
    ]
    with pytest.raises(ValueError, match="MODELS must not be empty"):
        eval_ab.parse_models(" , ")


def test_resolve_suite_tasks_groups_and_overrides() -> None:
    calc_tasks = eval_ab.resolve_suite_tasks("calc")
    assert set(calc_tasks) == {
        "data_sorting",
        "tax_column",
        "paste_table",
        "formula_table",
        "py_refuse_overlap",
        "py_no_bulk_read",
    }

    writer_tasks = eval_ab.resolve_suite_tasks("writer")
    assert "table_from_mess" in writer_tasks
    assert "data_sorting" not in writer_tasks

    all_tasks = eval_ab.resolve_suite_tasks("all")
    assert len(all_tasks) >= len(calc_tasks) + len(writer_tasks)

    override = eval_ab.resolve_suite_tasks("calc", "tax_column, data_sorting")
    assert override == ["tax_column", "data_sorting"]

    with pytest.raises(ValueError, match="Unknown suite"):
        eval_ab.resolve_suite_tasks("nonexistent_suite")

    with pytest.raises(ValueError, match="Unknown task_id"):
        eval_ab.resolve_suite_tasks("calc", "not_a_real_task")


def test_tally_and_format_ab_report_verdicts() -> None:
    models = ["m1", "m2"]
    tasks = ["t_oracle", "t_judged"]

    a_details = [
        {"model_id": "m1", "task_id": "t_oracle", "hard_pass": True, "judge_score": None},
        {"model_id": "m1", "task_id": "t_oracle", "hard_pass": False, "judge_score": None},
        {"model_id": "m1", "task_id": "t_judged", "hard_pass": True, "judge_score": 0.70},
        {"model_id": "m2", "task_id": "t_oracle", "hard_pass": False, "judge_score": None},
        {"model_id": "m2", "task_id": "t_judged", "hard_pass": True, "judge_score": 0.80},
    ]
    b_better_details = [
        {"model_id": "m1", "task_id": "t_oracle", "hard_pass": True, "judge_score": None},
        {"model_id": "m1", "task_id": "t_oracle", "hard_pass": True, "judge_score": None},
        {"model_id": "m1", "task_id": "t_judged", "hard_pass": True, "judge_score": 0.75},
        {"model_id": "m2", "task_id": "t_oracle", "hard_pass": True, "judge_score": None},
        {"model_id": "m2", "task_id": "t_judged", "hard_pass": True, "judge_score": 0.80},
    ]

    tally = eval_ab.tally_ab_results(a_details, b_better_details, models, tasks)
    assert tally["verdict"] == "B better"
    report = eval_ab.format_ab_report(
        tally, suite="calc", repeats=2, ref="feature", a_source="aaa", b_source="bbb"
    )
    assert "A=feature@aaa" in report
    assert "B=working-tree@bbb" in report
    assert "origin/master" not in report
    assert "B better" in report
    assert "t_oracle" in report
    assert "n/a" in report
    assert "0.750" in report

    # Worse pass rate -> B worse
    b_worse_details = [
        {**row, "hard_pass": False} for row in a_details
    ]
    assert eval_ab.tally_ab_results(a_details, b_worse_details, models, tasks)["verdict"] == "B worse"

    # Equal pass rate, higher quality -> B better; identical -> B same
    b_tie_higher_q = [
        {**row, "judge_score": (row["judge_score"] + 0.05) if row["judge_score"] is not None else None}
        for row in a_details
    ]
    assert eval_ab.tally_ab_results(a_details, b_tie_higher_q, models, tasks)["verdict"] == "B better"
    assert eval_ab.tally_ab_results(a_details, a_details, models, tasks)["verdict"] == "B same"


def test_working_tree_id_tracks_eval_sources(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    head = eval_ab.resolve_commit(repo, "HEAD")
    assert len(head) == 40
    clean_id = eval_ab.working_tree_id(repo)
    assert clean_id == head[:12]

    # Editing a tracked file under plugin/ changes the id while keeping HEAD prefix.
    (repo / "plugin" / "a.py").write_text("x = 2\n", encoding="utf-8")
    dirty_id = eval_ab.working_tree_id(repo)
    assert dirty_id != clean_id
    assert dirty_id.startswith(f"{head[:12]}+")

    # Reverting restores the clean id.
    (repo / "plugin" / "a.py").write_text("x = 1\n", encoding="utf-8")
    assert eval_ab.working_tree_id(repo) == clean_id

    # Untracked files outside SOURCE_PATHS and gitignored files do not affect the id.
    (repo / "notes.md").write_text("scratch\n", encoding="utf-8")
    (repo / "plugin" / "x.pyc").write_bytes(b"\x00\x01")
    assert eval_ab.working_tree_id(repo) == clean_id

    # Untracked files under plugin/ change the id, and content edits change it again.
    new_file = repo / "plugin" / "new.py"
    new_file.write_text("y = 1\n", encoding="utf-8")
    untracked_id_1 = eval_ab.working_tree_id(repo)
    assert untracked_id_1 != clean_id
    assert untracked_id_1.startswith(f"{head[:12]}+")

    new_file.write_text("y = 2\n", encoding="utf-8")
    untracked_id_2 = eval_ab.working_tree_id(repo)
    assert untracked_id_2 != untracked_id_1


def test_run_config_id_order_insensitive() -> None:
    base = eval_ab.run_config_id(["m1", "m2"], ["t1", "t2"])
    reordered = eval_ab.run_config_id(["m2", "m1"], ["t2", "t1"])
    assert base == reordered
    assert base != eval_ab.run_config_id(["m1", "m3"], ["t1", "t2"])
    assert base != eval_ab.run_config_id(["m1", "m2"], ["t1", "t3"])


def test_is_repeat_done_and_resume_skips_completed(monkeypatch, tmp_path, capsys) -> None:
    suite_dir = tmp_path / "calc"
    assert not eval_ab.is_repeat_done(suite_dir, "A", "k", 1)

    # Summary alone without details is not done.
    suite_dir.mkdir(parents=True, exist_ok=True)
    summary_only, details_missing = eval_ab.result_paths(suite_dir, "A", "k", 1)
    summary_only.write_text("[]", encoding="utf-8")
    assert not eval_ab.is_repeat_done(suite_dir, "A", "k", 1)

    # Corrupt details JSON is not done.
    details_missing.write_text("{bad json", encoding="utf-8")
    assert not eval_ab.is_repeat_done(suite_dir, "A", "k", 1)

    # Pre-populate A and B cleanly.
    r1_rows = [
        {"model_id": "m1", "task_id": "data_sorting", "hard_pass": True, "judge_score": None}
    ]
    _write_repeat_files(suite_dir, "A", "k", 1, r1_rows)
    _write_repeat_files(suite_dir, "B", "k", 1, r1_rows)
    assert eval_ab.is_repeat_done(suite_dir, "A", "k", 1)
    assert eval_ab.is_repeat_done(suite_dir, "B", "k", 1)

    # End-to-end resume checks use a separate repo and out_dir/calc.
    repo = _init_repo(tmp_path / "repo")
    out_dir = tmp_path / "out"
    calc_dir = out_dir / "calc"
    worktree_refs: list[str] = []

    monkeypatch.setattr(eval_ab, "REPO_ROOT", repo)
    monkeypatch.setattr(eval_ab, "run_generate_manifest", lambda root: None)

    def fake_ensure_worktree(repo_root: Path, worktree_dir: Path, ref: str = "origin/master") -> Path:
        worktree_refs.append(ref)
        return tmp_path / "wt-a"

    def fake_run_one_eval(
        root: Path, *, models, tasks, out_path: Path, jobs=1, lo_workers=2, backend="auto"
    ) -> None:
        assert jobs == 1
        assert lo_workers == 2
        assert backend == "auto"
        side = "A" if out_path.name.startswith("A-") else "B"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps([{"openrouter_id": models[0]}]), encoding="utf-8")
        eval_ab.details_path_for(out_path).write_text(
            json.dumps([
                {
                    "model_id": models[0],
                    "task_id": tasks[0],
                    "hard_pass": side == "B",
                    "judge_score": None,
                }
            ]),
            encoding="utf-8",
        )

    monkeypatch.setattr(eval_ab, "ensure_side_a_worktree", fake_ensure_worktree)
    monkeypatch.setattr(eval_ab, "run_one_eval", fake_run_one_eval)

    base_args = [
        "--suite", "calc",
        "--models", "m1",
        "--repeats", "1",
        "--ref", "master",
        "--out-dir", str(out_dir),
    ]

    # (a) First run executes both A and B, checking out the resolved 40-char SHA.
    assert eval_ab.main([*base_args, "--tasks", "data_sorting"]) == 0
    out_a = capsys.readouterr().out
    assert "[run] A-r1" in out_a
    assert "[run] B-r1" in out_a
    assert "B better" in out_a
    assert len(worktree_refs) == 1
    assert len(worktree_refs[0]) == 40
    sha = eval_ab.resolve_commit(repo, "master")
    assert worktree_refs[0] == sha
    assert f"A=master@{sha[:12]}" in out_a
    files_after_a = {p.name for p in calc_dir.glob("*-r1.json")}
    assert len(files_after_a) == 2

    # (b) Identical rerun skips both A and B and writes no new files.
    assert eval_ab.main([*base_args, "--tasks", "data_sorting"]) == 0
    out_b = capsys.readouterr().out
    assert "[skip] A-r1" in out_b
    assert "[skip] B-r1" in out_b
    assert {p.name for p in calc_dir.glob("*-r1.json")} == files_after_a

    # (c) Editing plugin/a.py invalidates only B.
    (repo / "plugin" / "a.py").write_text("x = 2\n", encoding="utf-8")
    assert eval_ab.main([*base_args, "--tasks", "data_sorting"]) == 0
    out_c = capsys.readouterr().out
    assert "[skip] A-r1" in out_c
    assert "[run] B-r1" in out_c
    assert f"B=working-tree@{sha[:12]}+" in out_c
    files_after_c = {p.name for p in calc_dir.glob("*-r1.json")}
    assert len(files_after_c - files_after_a) == 1
    assert next(iter(files_after_c - files_after_a)).startswith("B-")

    # (d) Changing --tasks invalidates both A and B.
    assert eval_ab.main([*base_args, "--tasks", "tax_column"]) == 0
    out_d = capsys.readouterr().out
    assert "[run] A-r1" in out_d
    assert "[run] B-r1" in out_d
    files_after_d = {p.name for p in calc_dir.glob("*-r1.json")}
    assert len(files_after_d - files_after_c) == 2

    # (e) Committing the edit moves master and B's HEAD, so rerunning original tasks runs both A and B.
    git(repo, "add", "plugin/a.py")
    git(repo, "commit", "-m", "second")
    assert eval_ab.main([*base_args, "--tasks", "data_sorting"]) == 0
    out_e = capsys.readouterr().out
    assert "[run] A-r1" in out_e
    assert "[run] B-r1" in out_e
    files_after_e = {p.name for p in calc_dir.glob("*-r1.json")}
    assert len(files_after_e - files_after_d) == 2

    # Bad --ref exits with code 2 and a clear git error.
    assert eval_ab.main([
        "--suite", "calc",
        "--models", "m1",
        "--repeats", "1",
        "--ref", "nonexistent-ref-xyz",
        "--out-dir", str(out_dir),
    ]) == 2
    assert "ERROR: git failed:" in capsys.readouterr().err


def test_main_aborts_if_working_tree_changes_mid_run(monkeypatch, tmp_path, capsys) -> None:
    repo = _init_repo(tmp_path / "repo")
    out_dir = tmp_path / "out"

    monkeypatch.setattr(eval_ab, "REPO_ROOT", repo)
    monkeypatch.setattr(eval_ab, "run_generate_manifest", lambda root: None)
    monkeypatch.setattr(
        eval_ab,
        "ensure_side_a_worktree",
        lambda repo_root, worktree_dir, ref="origin/master": tmp_path / "wt-a",
    )

    def fake_run_one_eval(
        root: Path, *, models, tasks, out_path: Path, jobs=1, lo_workers=2, backend="auto"
    ) -> None:
        # Simulate editing a tracked source file while Side A is running.
        (repo / "plugin" / "a.py").write_text("x = 99\n", encoding="utf-8")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text("[]", encoding="utf-8")
        eval_ab.details_path_for(out_path).write_text("[]", encoding="utf-8")

    monkeypatch.setattr(eval_ab, "run_one_eval", fake_run_one_eval)

    rc = eval_ab.main([
        "--suite", "calc",
        "--tasks", "data_sorting",
        "--models", "m1",
        "--repeats", "1",
        "--ref", "master",
        "--out-dir", str(out_dir),
    ])
    assert rc == 2
    assert "working tree changed during the run" in capsys.readouterr().err
