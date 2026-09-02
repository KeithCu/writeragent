# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""CLI guards for run_eval_multi (no paid API)."""
from __future__ import annotations

import sys
from pathlib import Path

_PO = Path(__file__).resolve().parents[2] / "scripts" / "prompt_optimization"
if str(_PO) not in sys.path:
    sys.path.insert(0, str(_PO))

import run_eval_multi  # noqa: E402


def test_out_path_relative_is_cwd(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    args = type("A", (), {"out": "scripts/prompt_optimization/eval_results_17task.csv"})()
    got = run_eval_multi._out_path(args)
    assert got == tmp_path / "scripts/prompt_optimization/eval_results_17task.csv"
    # A Unix-only abs path is not drive-absolute on Windows (Path.cwd() / p
    # becomes C:/tmp/eval.csv). Use a real filesystem abs path like _out_path.
    abs_out = str(tmp_path / "abs_eval.csv")
    abs_args = type("A", (), {"out": abs_out})()
    assert run_eval_multi._out_path(abs_args) == Path(abs_out).resolve()


def test_nitro_student_reuses_base_catalog_pricing() -> None:
    cfg = run_eval_multi._model_config_for_id(
        "openai/gpt-oss-120b:nitro", allow_unknown=False
    )
    base = run_eval_multi._model_config_for_id(
        "openai/gpt-oss-120b", allow_unknown=False
    )
    assert cfg.openrouter_id == "openai/gpt-oss-120b:nitro"
    assert cfg.input_cost_per_million == base.input_cost_per_million
    assert cfg.output_cost_per_million == base.output_cost_per_million


def test_refuse_catalog_sweep_without_models(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        sys, "argv", ["run_eval_multi.py", "--student", "scripted"]
    )
    assert run_eval_multi.main() == 1
    err = capsys.readouterr().err
    assert "--models" in err
    assert "--yes-all-models" in err


def test_generate_golds_passes_task_id_and_kind_prompt(monkeypatch, tmp_path) -> None:
    """Draw/Calc golds used to run as Writer (no task_id, writer system prompt)."""
    import llm_chat_eval
    from eval_prompts import get_eval_system_prompt

    captured: list[dict] = []

    def fake_run(**kwargs):
        captured.append(kwargs)
        return '{"status": "ok"}', {"total_tokens": 1}, None, []

    monkeypatch.setattr(llm_chat_eval, "run_llm_chat_eval", fake_run)
    monkeypatch.setattr(run_eval_multi, "SCRIPT_DIR", tmp_path)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_eval_multi.py",
            "--generate-golds",
            "-e",
            "flowchart_gen",
            "--student",
            "scripted",
        ],
    )
    assert run_eval_multi.main() == 0
    assert captured
    assert captured[0]["task_id"] == "flowchart_gen"
    assert captured[0]["system_prompt"] == get_eval_system_prompt("flowchart_gen")
    assert captured[0]["system_prompt"] != get_eval_system_prompt("table_from_mess")
    golds = (tmp_path / "gold_standards.json").read_text(encoding="utf-8")
    assert "flowchart_gen" in golds


def _pareto_row(**overrides: object) -> dict:
    row: dict = {
        "openrouter_id": "model/a",
        "pricing_known": True,
        "n_examples": 17,
        "n_error": 0,
        "avg_correctness": 0.90,
        "avg_cost_per_example": 0.01,
    }
    row.update(overrides)
    return row


def test_pareto_strict_dominance() -> None:
    cheap = _pareto_row(openrouter_id="cheap", avg_correctness=0.95, avg_cost_per_example=0.001)
    expensive = _pareto_row(
        openrouter_id="expensive", avg_correctness=0.80, avg_cost_per_example=0.01
    )
    run_eval_multi.annotate_pareto_status([cheap, expensive])
    assert cheap["pareto_status"] == run_eval_multi.PARETO_FRONTIER
    assert expensive["pareto_status"] == run_eval_multi.PARETO_DOMINATED


def test_pareto_quality_cost_tradeoff_both_frontier() -> None:
    cheap = _pareto_row(openrouter_id="oss", avg_correctness=0.971, avg_cost_per_example=0.00054)
    better = _pareto_row(openrouter_id="grok", avg_correctness=0.982, avg_cost_per_example=0.04653)
    run_eval_multi.annotate_pareto_status([cheap, better])
    assert cheap["pareto_status"] == run_eval_multi.PARETO_FRONTIER
    assert better["pareto_status"] == run_eval_multi.PARETO_FRONTIER


def test_pareto_equal_cost_higher_correctness_dominates() -> None:
    worse = _pareto_row(openrouter_id="worse", avg_correctness=0.80, avg_cost_per_example=0.01)
    better = _pareto_row(openrouter_id="better", avg_correctness=0.90, avg_cost_per_example=0.01)
    run_eval_multi.annotate_pareto_status([worse, better])
    assert better["pareto_status"] == run_eval_multi.PARETO_FRONTIER
    assert worse["pareto_status"] == run_eval_multi.PARETO_DOMINATED


def test_pareto_equal_correctness_cheaper_dominates() -> None:
    cheap = _pareto_row(openrouter_id="cheap", avg_correctness=0.90, avg_cost_per_example=0.001)
    dear = _pareto_row(openrouter_id="dear", avg_correctness=0.90, avg_cost_per_example=0.01)
    run_eval_multi.annotate_pareto_status([cheap, dear])
    assert cheap["pareto_status"] == run_eval_multi.PARETO_FRONTIER
    assert dear["pareto_status"] == run_eval_multi.PARETO_DOMINATED


def test_pareto_exact_ties_remain_frontier() -> None:
    a = _pareto_row(openrouter_id="a", avg_correctness=0.90, avg_cost_per_example=0.01)
    b = _pareto_row(openrouter_id="b", avg_correctness=0.90, avg_cost_per_example=0.01)
    run_eval_multi.annotate_pareto_status([a, b])
    assert a["pareto_status"] == run_eval_multi.PARETO_FRONTIER
    assert b["pareto_status"] == run_eval_multi.PARETO_FRONTIER


def test_pareto_unknown_pricing_and_failed_runs_unavailable() -> None:
    unknown = _pareto_row(openrouter_id="unknown", pricing_known=False)
    failed = _pareto_row(openrouter_id="failed", n_error=17, avg_correctness=0.0)
    empty = _pareto_row(openrouter_id="empty", n_examples=0, n_error=1)
    ok = _pareto_row(openrouter_id="ok")
    run_eval_multi.annotate_pareto_status([unknown, failed, empty, ok])
    assert unknown["pareto_status"] == run_eval_multi.PARETO_UNAVAILABLE
    assert failed["pareto_status"] == run_eval_multi.PARETO_UNAVAILABLE
    assert empty["pareto_status"] == run_eval_multi.PARETO_UNAVAILABLE
    assert ok["pareto_status"] == run_eval_multi.PARETO_FRONTIER


def test_pareto_write_results_includes_status(tmp_path: Path) -> None:
    import json

    cheap = _pareto_row(openrouter_id="cheap", avg_correctness=0.95, avg_cost_per_example=0.001)
    dear = _pareto_row(openrouter_id="dear", avg_correctness=0.80, avg_cost_per_example=0.02)
    summaries = [cheap, dear]
    run_eval_multi.annotate_pareto_status(summaries)
    out = tmp_path / "pareto.json"
    run_eval_multi._write_results(out, summaries)
    loaded = json.loads(out.read_text(encoding="utf-8"))
    assert loaded[0]["n_examples"] == 17
    assert {row["openrouter_id"]: row["pareto_status"] for row in loaded} == {
        "cheap": run_eval_multi.PARETO_FRONTIER,
        "dear": run_eval_multi.PARETO_DOMINATED,
    }


def test_pareto_successive_fronts_chain() -> None:
    a = _pareto_row(openrouter_id="a", avg_correctness=0.95, avg_cost_per_example=0.001)
    b = _pareto_row(openrouter_id="b", avg_correctness=0.85, avg_cost_per_example=0.005)
    c = _pareto_row(openrouter_id="c", avg_correctness=0.75, avg_cost_per_example=0.01)
    run_eval_multi.annotate_pareto_fronts([a, b, c])
    assert a["pareto_front"] == 1
    assert b["pareto_front"] == 2
    assert c["pareto_front"] == 3


def test_pareto_tradeoff_and_dominated_cheap_model() -> None:
    cheap = _pareto_row(openrouter_id="oss", avg_correctness=0.971, avg_cost_per_example=0.00054)
    better = _pareto_row(openrouter_id="grok", avg_correctness=0.982, avg_cost_per_example=0.04653)
    worse = _pareto_row(openrouter_id="solar", avg_correctness=0.682, avg_cost_per_example=0.00065)
    run_eval_multi.annotate_pareto_fronts([cheap, better, worse])
    assert cheap["pareto_front"] == 1
    assert better["pareto_front"] == 1
    assert worse["pareto_front"] >= 2


def test_pareto_f1_distance_zero_and_ordering() -> None:
    cheap = _pareto_row(openrouter_id="oss", avg_correctness=0.971, avg_cost_per_example=0.00054)
    better = _pareto_row(openrouter_id="grok", avg_correctness=0.982, avg_cost_per_example=0.04653)
    near = _pareto_row(openrouter_id="near", avg_correctness=0.90, avg_cost_per_example=0.002)
    far = _pareto_row(openrouter_id="far", avg_correctness=0.60, avg_cost_per_example=0.02)
    summaries = [cheap, better, near, far]
    run_eval_multi.annotate_pareto_fronts(summaries)
    distances = run_eval_multi.pareto_f1_distances(summaries)
    assert distances[id(cheap)] == 0.0
    assert distances[id(better)] == 0.0
    assert distances[id(near)] < distances[id(far)]


def test_pareto_tradeoff_score_f1_is_one() -> None:
    cheap = _pareto_row(openrouter_id="oss", avg_correctness=0.971, avg_cost_per_example=0.00054)
    better = _pareto_row(openrouter_id="grok", avg_correctness=0.982, avg_cost_per_example=0.04653)
    summaries = [cheap, better]
    run_eval_multi.annotate_pareto_fronts(summaries)
    scores = run_eval_multi.pareto_tradeoff_scores(summaries)
    assert scores[id(cheap)] == 1.0
    assert scores[id(better)] == 1.0


def test_pareto_tradeoff_score_decreases_with_distance() -> None:
    cheap = _pareto_row(openrouter_id="oss", avg_correctness=0.971, avg_cost_per_example=0.00054)
    better = _pareto_row(openrouter_id="grok", avg_correctness=0.982, avg_cost_per_example=0.04653)
    near = _pareto_row(openrouter_id="near", avg_correctness=0.90, avg_cost_per_example=0.002)
    far = _pareto_row(openrouter_id="far", avg_correctness=0.60, avg_cost_per_example=0.02)
    summaries = [cheap, better, near, far]
    run_eval_multi.annotate_pareto_fronts(summaries)
    distances = run_eval_multi.pareto_f1_distances(summaries)
    scores = run_eval_multi.pareto_tradeoff_scores(summaries)
    assert scores[id(near)] > scores[id(far)]
    for row_id, dist in distances.items():
        assert scores[row_id] == max(0.0, 1.0 - dist)
