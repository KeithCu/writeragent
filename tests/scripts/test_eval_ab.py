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

_PO = Path(__file__).resolve().parents[2] / "scripts" / "prompt_optimization"
if str(_PO) not in sys.path:
    sys.path.insert(0, str(_PO))

import eval_ab  # noqa: E402


def _write_repeat_files(
    suite_dir: Path,
    side: str,
    rep: int,
    details: list[dict],
) -> None:
    suite_dir.mkdir(parents=True, exist_ok=True)
    summary_path, details_path = eval_ab.result_paths(suite_dir, side, rep)
    summary_path.write_text(json.dumps([{"openrouter_id": "m1"}]), encoding="utf-8")
    details_path.write_text(json.dumps(details), encoding="utf-8")


def test_resolve_suite_tasks_groups_and_overrides() -> None:
    calc_tasks = eval_ab.resolve_suite_tasks("calc")
    assert set(calc_tasks) == {
        "data_sorting",
        "tax_column",
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
    report = eval_ab.format_ab_report(tally, suite="calc", repeats=2)
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


def test_is_repeat_done_and_resume_skips_completed(monkeypatch, tmp_path, capsys) -> None:
    suite_dir = tmp_path / "calc"
    assert not eval_ab.is_repeat_done(suite_dir, "A", 1)

    # Summary alone without details is not done.
    suite_dir.mkdir(parents=True, exist_ok=True)
    summary_only, details_missing = eval_ab.result_paths(suite_dir, "A", 1)
    summary_only.write_text("[]", encoding="utf-8")
    assert not eval_ab.is_repeat_done(suite_dir, "A", 1)

    # Corrupt details JSON is not done.
    details_missing.write_text("{bad json", encoding="utf-8")
    assert not eval_ab.is_repeat_done(suite_dir, "A", 1)

    # Pre-populate A-r1 and B-r1 cleanly.
    r1_rows = [
        {"model_id": "m1", "task_id": "data_sorting", "hard_pass": True, "judge_score": None}
    ]
    _write_repeat_files(suite_dir, "A", 1, r1_rows)
    _write_repeat_files(suite_dir, "B", 1, r1_rows)
    assert eval_ab.is_repeat_done(suite_dir, "A", 1)
    assert eval_ab.is_repeat_done(suite_dir, "B", 1)

    calls: list[tuple[Path, Path]] = []

    monkeypatch.setattr(eval_ab, "run_generate_manifest", lambda root: None)
    monkeypatch.setattr(
        eval_ab,
        "ensure_side_a_worktree",
        lambda repo_root, worktree_dir, ref="origin/master": tmp_path / "wt-a",
    )

    def fake_run_one_eval(root: Path, *, models, tasks, out_path: Path, jobs=1, lo_workers=2, backend="auto"):
        assert jobs == 1
        assert lo_workers == 2
        assert backend == "auto"
        calls.append((root, out_path))
        side = "A" if out_path.name.startswith("A-") else "B"
        _write_repeat_files(
            out_path.parent,
            side,
            2,
            [{"model_id": models[0], "task_id": tasks[0], "hard_pass": side == "B", "judge_score": None}],
        )

    monkeypatch.setattr(eval_ab, "run_one_eval", fake_run_one_eval)

    rc = eval_ab.main([
        "--suite", "calc",
        "--tasks", "data_sorting",
        "--models", "m1",
        "--repeats", "2",
        "--out-dir", str(tmp_path),
    ])
    assert rc == 0
    assert [out.name for unused, out in calls] == ["A-r2.json", "B-r2.json"]

    out = capsys.readouterr().out
    assert "[skip] A-r1" in out
    assert "[skip] B-r1" in out
    assert "B better" in out
