# WriterAgent tests for scripts/plot_eval2_leaderboard.py
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Eval-2 headed scoreboard stays separate from the string-pack Pareto."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

_REPO = Path(__file__).resolve().parents[2]
_SCRIPTS = _REPO / "scripts"
_EVAL2 = _REPO / "docs" / "eval" / "eval-2"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import plot_eval2_leaderboard as pel  # noqa: E402

_RESULTS = _EVAL2 / "eval2_benchmark_results.json"
_SCHEMA = _EVAL2 / "eval2_benchmark_results.schema.json"
_SCOREBOARD = _EVAL2 / "benchmarks.md"
_README = _EVAL2 / "README.md"


def _minimal_board_payload() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "updated": "2026-09-12",
        "gate_model": "google/gemini-3.8-flash",
        "source_of_truth": "docs/eval/eval-2/headed-failure-autopsy.md",
        "tasks": [
            {
                "id": "afc",
                "slot": 3,
                "slug": "afc-sample-83d10b06",
                "title": "AFC Population",
                "status": "Ready",
            }
        ],
        "models": [
            {
                "openrouter_id": "google/gemini-3.8-flash",
                "display_name": "Gemini 3.8 Flash",
                "role": "gate",
            },
            {
                "openrouter_id": "openai/gpt-oss-20b",
                "display_name": "GPT-OSS 20B",
                "role": "catalog",
            },
        ],
        "results": [
            {
                "task_id": "afc",
                "model": "google/gemini-3.8-flash",
                "product_bar": "HAPPY",
                "oracle": "PASS",
            }
        ],
    }


def test_seed_json_loads_and_matches_schema_enums() -> None:
    board = pel.load_eval2_board(_RESULTS)
    assert board.schema_version == 1
    assert board.gate_model == "google/gemini-3.8-flash"
    assert board.source_of_truth == "docs/eval/eval-2/headed-failure-autopsy.md"
    assert (_REPO / board.source_of_truth).is_file()
    schema = json.loads(_SCHEMA.read_text(encoding="utf-8"))
    assert schema["properties"]["schema_version"]["const"] == 1
    assert "hard_pass_rate" not in schema["properties"]
    assert "hard_pass_rate" not in schema["$defs"]["result"]["properties"]
    result_props = schema["$defs"]["result"]["properties"]
    for key in (
        "total_tokens",
        "input_tokens",
        "output_tokens",
        "total_cost_usd",
        "wall_time_s",
        "intelligence_per_dollar",
        "oracle_passed",
        "oracle_failure_count",
        "oracle_check_count",
        "oracle_failures",
        "afc_s_flags",
        "afc_r_required",
        "husk_cells",
        "scored_cells",
        "partial_score",
    ):
        assert key in result_props
        assert key not in schema["$defs"]["result"]["required"]
    assert {task.slot for task in board.tasks} == {1, 2, 3, 4, 5, 6, 8, 9, 10}
    assert all(task.slot != 7 for task in board.tasks)
    assert {model.openrouter_id for model in board.models} >= {
        "google/gemini-3.8-flash",
        "openai/gpt-oss-120b",
        "openai/gpt-5.6-luna",
        "openai/gpt-oss-20b",
        "google/gemini-3.5-flash-lite",
        "google/gemma-4-31b-it",
        "google/gemma-4-26b-a4b-it",
        "x-ai/grok-4.6",
        "meta/muse-spark-1.3-contributor",
    }


def test_seed_cells_match_autopsy_and_leave_unknowns_empty() -> None:
    board = pel.load_eval2_board(_RESULTS)
    gemini = "google/gemini-3.8-flash"
    oss = "openai/gpt-oss-120b"
    luna = "openai/gpt-5.6-luna"
    oss20 = "openai/gpt-oss-20b"
    lite = "google/gemini-3.5-flash-lite"
    gemma = "google/gemma-4-31b-it"
    gemma26 = "google/gemma-4-26b-a4b-it"

    tenant = board.result_for("tenant-retention", gemini)
    assert tenant is not None
    assert tenant.product_bar == "HAPPY"
    assert tenant.oracle == "FAIL"

    cadaver = board.result_for("cadaver-proposal", gemini)
    assert cadaver is not None
    assert cadaver.product_bar == "HAPPY"
    assert cadaver.oracle == "FAIL"

    afc = board.result_for("afc", gemini)
    assert afc is not None
    assert afc.product_bar == "HAPPY"
    assert afc.oracle == "PASS"

    floor_g = board.result_for("writer-calc-peer-write", gemini)
    assert floor_g is not None
    assert floor_g.product_bar == "NOT_HAPPY"
    assert floor_g.oracle == "FAIL"
    assert floor_g.patches and "not PR" in floor_g.patches

    gmp = board.result_for("gmp-change-control", oss)
    assert gmp is not None
    assert gmp.product_bar == "HAPPY"
    assert gmp.oracle == "FAIL"

    floor_o = board.result_for("writer-calc-peer-write", oss)
    assert floor_o is not None
    assert floor_o.product_bar == "NOT_HAPPY"

    for task_id in (
        "calc-primary-model",
        "draw-primary",
        "reverse-tenant",
        "long-writer-pack",
    ):
        row = board.result_for(task_id, oss)
        assert row is not None
        assert row.product_bar == "NOT_HAPPY"
        assert row.oracle == "FAIL"

    # No invented Gemini scores for overnight gpt-oss-only siblings.
    for task_id in (
        "gmp-change-control",
        "calc-primary-model",
        "draw-primary",
        "reverse-tenant",
        "long-writer-pack",
    ):
        assert board.result_for(task_id, gemini) is None

    # No invented gpt-oss scores for Gemini-only Writer stamps.
    for task_id in ("tenant-retention", "cadaver-proposal"):
        assert board.result_for(task_id, oss) is None

    # First catalog AFC stamp (Scrolly headed 20260912-0142).
    afc_oss = board.result_for("afc", oss)
    assert afc_oss is not None
    assert afc_oss.product_bar == "NOT_HAPPY"
    assert afc_oss.oracle == "FAIL"
    assert afc_oss.stamp == "20260912-0142-gpt-oss-120b"
    assert afc_oss.oracle_passed is False
    assert afc_oss.oracle_failure_count == 1
    assert afc_oss.oracle_failures == (
        "R from Sample Size Calculation is missing or < 1",
    )
    assert afc_oss.oracle_check_count is None
    assert afc_oss.partial_score is None
    assert afc_oss.afc_s_flags == 585
    assert afc_oss.afc_r_required is None
    assert afc_oss.husk_cells == 0
    assert afc_oss.scored_cells == 15159
    assert afc_oss.input_tokens == 108266
    assert afc_oss.output_tokens == 6994
    assert afc_oss.total_tokens == 115260
    assert afc_oss.wall_time_s == 1290
    assert afc_oss.total_cost_usd == pytest.approx(0.0052)
    assert afc_oss.intelligence_per_dollar is None
    assert "20260912-0142-gpt-oss-120b" in afc_oss.oracle_note

    # Second catalog AFC stamp (Scrolly headed 20260912-0150).
    afc_luna = board.result_for("afc", luna)
    assert afc_luna is not None
    assert afc_luna.product_bar == "NOT_HAPPY"
    assert afc_luna.oracle == "FAIL"
    assert afc_luna.stamp == "20260912-0150-gpt-5.6-luna"
    assert afc_luna.oracle_passed is False
    assert afc_luna.oracle_failure_count == 1
    assert afc_luna.oracle_failures == (
        "R from Sample Size Calculation is missing or < 1",
    )
    assert afc_luna.oracle_check_count is None
    assert afc_luna.partial_score is None
    assert afc_luna.afc_s_flags == 68
    assert afc_luna.afc_r_required is None
    assert afc_luna.husk_cells == 0
    assert afc_luna.scored_cells == 810
    assert afc_luna.input_tokens == 562909
    assert afc_luna.output_tokens == 10467
    assert afc_luna.total_tokens == 573376
    assert afc_luna.wall_time_s == 406
    assert afc_luna.total_cost_usd == pytest.approx(0.0419)
    assert afc_luna.intelligence_per_dollar is None
    assert "20260912-0150-gpt-5.6-luna" in afc_luna.oracle_note
    assert "0.1252" in afc_luna.oracle_note

    # Third catalog AFC stamp (Scrolly headed 20260912-0202, :nitro).
    afc_20b = board.result_for("afc", oss20)
    assert afc_20b is not None
    assert afc_20b.product_bar == "NOT_HAPPY"
    assert afc_20b.oracle == "FAIL"
    assert afc_20b.stamp == "20260912-0202-gpt-oss-20b-nitro"
    assert afc_20b.oracle_passed is False
    assert afc_20b.oracle_failure_count == 1
    assert afc_20b.oracle_failures == (
        "R from Sample Size Calculation is missing or < 1",
    )
    assert afc_20b.oracle_check_count is None
    assert afc_20b.partial_score is None
    assert afc_20b.afc_s_flags == 0
    assert afc_20b.afc_r_required is None
    assert afc_20b.husk_cells == 0
    assert afc_20b.scored_cells == 800
    assert afc_20b.input_tokens == 15316
    assert afc_20b.output_tokens == 236
    assert afc_20b.total_tokens == 15552
    assert afc_20b.wall_time_s == 309
    assert afc_20b.total_cost_usd == pytest.approx(0.00122)
    assert afc_20b.intelligence_per_dollar is None
    assert "20260912-0202-gpt-oss-20b-nitro" in afc_20b.oracle_note
    assert "0.00049" in afc_20b.oracle_note
    assert "nitro" in afc_20b.oracle_note.lower()

    # Fourth catalog AFC stamp (Scrolly headed 20260912-0216).
    afc_lite = board.result_for("afc", lite)
    assert afc_lite is not None
    assert afc_lite.product_bar == "NOT_HAPPY"
    assert afc_lite.oracle == "FAIL"
    assert afc_lite.stamp == "20260912-0216-gemini-3.5-flash-lite"
    assert afc_lite.oracle_passed is False
    assert afc_lite.oracle_failure_count == 1
    assert afc_lite.oracle_failures == (
        "R from Sample Size Calculation is missing or < 1",
    )
    assert afc_lite.oracle_check_count is None
    assert afc_lite.partial_score is None
    assert afc_lite.afc_s_flags == 0
    assert afc_lite.afc_r_required is None
    assert afc_lite.husk_cells == 0
    assert afc_lite.scored_cells == 648
    assert afc_lite.input_tokens == 26111
    assert afc_lite.output_tokens == 150
    assert afc_lite.total_tokens == 26261
    assert afc_lite.wall_time_s == 338
    assert afc_lite.total_cost_usd == pytest.approx(0.00629)
    assert afc_lite.intelligence_per_dollar is None
    assert "20260912-0216-gemini-3.5-flash-lite" in afc_lite.oracle_note
    assert "0.00821" in afc_lite.oracle_note
    assert "Required Sar" in afc_lite.oracle_note
    assert "73" in afc_lite.oracle_note

    # Fifth catalog AFC stamp (Scrolly headed 20260912-0224).
    afc_gemma = board.result_for("afc", gemma)
    assert afc_gemma is not None
    assert afc_gemma.product_bar == "NOT_HAPPY"
    assert afc_gemma.oracle == "FAIL"
    assert afc_gemma.stamp == "20260912-0224-gemma-4-31b-it"
    assert afc_gemma.oracle_passed is False
    assert afc_gemma.oracle_failure_count == 1
    assert afc_gemma.oracle_failures == (
        "R from Sample Size Calculation is missing or < 1",
    )
    assert afc_gemma.oracle_check_count is None
    assert afc_gemma.partial_score is None
    assert afc_gemma.afc_s_flags == 0
    assert afc_gemma.afc_r_required is None
    assert afc_gemma.husk_cells == 81
    assert afc_gemma.scored_cells == 810
    assert afc_gemma.input_tokens == 146941
    assert afc_gemma.output_tokens == 2394
    assert afc_gemma.total_tokens == 149335
    assert afc_gemma.wall_time_s == 392
    assert afc_gemma.total_cost_usd == pytest.approx(0.10841)
    assert afc_gemma.intelligence_per_dollar is None
    assert "20260912-0224-gemma-4-31b-it" in afc_gemma.oracle_note
    assert "0.01404" in afc_gemma.oracle_note
    assert "Err:508" in afc_gemma.oracle_note

    # Sixth catalog AFC stamp (Scrolly headed 20260912-0240).
    afc_gemma26 = board.result_for("afc", gemma26)
    assert afc_gemma26 is not None
    assert afc_gemma26.product_bar == "NOT_HAPPY"
    assert afc_gemma26.oracle == "FAIL"
    assert afc_gemma26.stamp == "20260912-0240-gemma-4-26b-a4b-it"
    assert afc_gemma26.oracle_passed is False
    assert afc_gemma26.oracle_failure_count == 1
    assert afc_gemma26.oracle_failures == (
        "S=0 flag=1 in column K is < R=65",
    )
    assert afc_gemma26.oracle_check_count is None
    assert afc_gemma26.partial_score is None
    assert afc_gemma26.afc_s_flags == 0
    assert afc_gemma26.afc_r_required == 65
    assert afc_gemma26.husk_cells == 0
    assert afc_gemma26.scored_cells == 70
    assert afc_gemma26.input_tokens == 688661
    assert afc_gemma26.output_tokens == 5586
    assert afc_gemma26.total_tokens == 694247
    assert afc_gemma26.wall_time_s == 886
    assert afc_gemma26.total_cost_usd == pytest.approx(0.04316)
    assert afc_gemma26.intelligence_per_dollar is None
    assert "20260912-0240-gemma-4-26b-a4b-it" in afc_gemma26.oracle_note
    assert "0.05011" in afc_gemma26.oracle_note
    assert "10 <<" in afc_gemma26.oracle_note or "sample_data_rows=10" in afc_gemma26.oracle_note

    # No invented Luna / 20b / Flash Lite / Gemma scores outside AFC.
    for task_id in (
        "tenant-retention",
        "cadaver-proposal",
        "gmp-change-control",
        "writer-calc-peer-write",
        "calc-primary-model",
        "draw-primary",
        "reverse-tenant",
        "long-writer-pack",
    ):
        assert board.result_for(task_id, luna) is None
        assert board.result_for(task_id, oss20) is None
        assert board.result_for(task_id, lite) is None
        assert board.result_for(task_id, gemma) is None
        assert board.result_for(task_id, gemma26) is None

    # Remaining catalog peers stay empty until a real stamp lands.
    for mid in (
        "x-ai/grok-4.6",
        "meta/muse-spark-1.3-contributor",
    ):
        assert board.scored_count(mid) == 0
        assert board.happy_count(mid) == 0

    # Other seed cells must not invent run costs or oracle-partial counts.
    recorded_afc = {
        ("afc", oss),
        ("afc", luna),
        ("afc", oss20),
        ("afc", lite),
        ("afc", gemma),
        ("afc", gemma26),
    }
    for row in board.results:
        if (row.task_id, row.model) in recorded_afc:
            continue
        assert row.total_tokens is None
        assert row.input_tokens is None
        assert row.output_tokens is None
        assert row.total_cost_usd is None
        assert row.wall_time_s is None
        assert row.intelligence_per_dollar is None
        assert row.oracle_failure_count is None
        assert row.oracle_check_count is None
        assert row.oracle_failures is None
        assert row.afc_s_flags is None
        assert row.afc_r_required is None
        assert row.husk_cells is None
        assert row.scored_cells is None
        if row.oracle == "PASS":
            assert row.oracle_passed is True
            assert row.partial_score == 1.0
        else:
            assert row.oracle_passed is False
            assert row.partial_score is None
    assert pel.happy_cost_rows(board) == ()
    afc = board.result_for("afc", gemini)
    assert afc is not None
    assert pel.has_recorded_partial(afc)
    assert pel.has_recorded_partial(afc_oss)
    assert pel.has_recorded_partial(afc_luna)
    assert pel.has_recorded_partial(afc_20b)
    assert pel.has_recorded_partial(afc_lite)
    assert pel.has_recorded_partial(afc_gemma)
    assert pel.has_recorded_partial(afc_gemma26)


_RECORDED_AFC_CATALOG = {
    ("afc", "openai/gpt-oss-120b"),
    ("afc", "openai/gpt-5.6-luna"),
    ("afc", "openai/gpt-oss-20b"),
    ("afc", "google/gemini-3.5-flash-lite"),
    ("afc", "google/gemma-4-31b-it"),
    ("afc", "google/gemma-4-26b-a4b-it"),
}
_OPTIONAL_COST_PARTIAL_KEYS = (
    "total_tokens",
    "input_tokens",
    "output_tokens",
    "total_cost_usd",
    "wall_time_s",
    "intelligence_per_dollar",
    "oracle_passed",
    "oracle_failure_count",
    "oracle_check_count",
    "oracle_failures",
    "afc_s_flags",
    "afc_r_required",
    "husk_cells",
    "scored_cells",
    "partial_score",
)


def test_every_seed_source_points_at_an_in_repo_doc() -> None:
    payload = json.loads(_RESULTS.read_text(encoding="utf-8"))
    for row in payload["results"]:
        source = str(row.get("source") or "")
        assert source, row
        first = source.split(";")[0].strip().split()[0]
        path = _REPO / first
        assert path.is_file(), first
        assert row.get("run_artifacts_committed") is False
        if (row.get("task_id"), row.get("model")) in _RECORDED_AFC_CATALOG:
            continue
        for key in _OPTIONAL_COST_PARTIAL_KEYS:
            assert key not in row or row[key] is None, row


def test_scoreboard_markdown_matches_seed_matrix() -> None:
    board = pel.load_eval2_board(_RESULTS)
    matrix = pel.render_matrix_markdown(board)
    text = _SCOREBOARD.read_text(encoding="utf-8")
    assert "| Hard pass |" not in text
    assert "product_bar" in text or "Product bar" in text
    assert "google/gemini-3.8-flash" in text
    assert "not" in text.lower() and "string" in text.lower()
    assert "[`docs/eval/benchmarks.md`](../benchmarks.md)" in text
    assert "eval2-heatmap.svg" in text
    assert "eval2-coverage.svg" in text
    assert "eval2-cost.svg" in text
    assert "eval2-partial.svg" in text
    assert "HAPPY first" in text
    assert "partial_score`²" in text or "partial_score²" in text
    assert "1 − oracle_failure_count / oracle_check_count" in text
    assert "headed sibling" in text.lower()
    assert "Different benchmark" in text
    assert "One filled task still counts" in text
    assert "do **not** invent run costs" in text.lower()
    assert "Catalog-wide" in text or "catalog" in text.lower()
    assert "sweep has **not** happened" in text
    for line in matrix.strip().splitlines():
        if line.startswith("| # |"):
            continue
        assert line in text, line
    readme = _README.read_text(encoding="utf-8")
    assert "[`benchmarks.md`](benchmarks.md)" in readme
    assert "string-pack Pareto" in readme
    assert "Different benchmark from the 17-task string pack" in readme
    assert "One filled task" in readme
    heatmap = (_EVAL2 / pel.HEATMAP_NAME).read_text(encoding="utf-8")
    coverage = (_EVAL2 / pel.COVERAGE_NAME).read_text(encoding="utf-8")
    cost = (_EVAL2 / pel.COST_NAME).read_text(encoding="utf-8")
    partial = (_EVAL2 / pel.PARTIAL_NAME).read_text(encoding="utf-8")
    assert "no data" in heatmap
    assert "HAPPY" in heatmap
    assert "0 HAPPY / 0 scored" in coverage
    assert "No HAPPY cell has recorded total_cost_usd yet" in cost
    assert "data-cost-usd=" not in cost
    # Seed has one oracle PASS (AFC Gemini 3.8) → partial 1. Catalog AFC
    # cells recorded S/R + failure_count without a check-count ratio.
    assert "data-partial-score=\"1.00\"" in partial
    assert "Gemini 3.8 Flash" in partial
    assert "Gemini 3.5 Flash Lite" in partial
    assert "Gemma 4 31B" in partial
    assert "Gemma 4 26B A4B" in partial
    assert "AFC Population" in partial
    assert "GPT-OSS 120B" in partial
    assert "GPT-5.6 Luna" in partial
    assert "GPT-OSS 20B" in partial
    assert "S=585 R=—" in partial
    assert "S=68 R=—" in partial
    assert "S=0 R=—" in partial
    assert "S=0 R=65" in partial
    assert "no ratio" in partial
    assert "data-partial-score=\"0." not in partial


def test_plot_writes_distinct_svgs_with_honest_empty_cells(tmp_path: Path) -> None:
    board = pel.load_eval2_board(_RESULTS)
    heatmap = pel.write_heatmap_svg(board, tmp_path / "eval2-heatmap.svg")
    coverage = pel.write_coverage_svg(board, tmp_path / "eval2-coverage.svg")
    heat = heatmap.read_text(encoding="utf-8")
    cov = coverage.read_text(encoding="utf-8")
    assert heat.startswith("<?xml")
    assert "HAPPY" in heat
    assert "NOT_HAPPY" in heat
    assert "no data" in heat
    assert "sibling of the string pack" in heat
    assert "hard_pass_rate" in heat
    assert "pareto-fronts" not in heat
    assert "google/gemini-3.8-flash" in heat
    assert "Tenant Retention" in heat
    assert "no data" in cov
    assert "0 HAPPY / 0 scored" in cov
    assert pel.HEATMAP_NAME != "pareto-fronts.svg"
    assert pel.COVERAGE_NAME != "pareto-distance.svg"
    assert pel.COST_NAME != "pareto-fronts.svg"

    cost_svg = pel.write_cost_svg(board, tmp_path / "eval2-cost.svg")
    cost_text = cost_svg.read_text(encoding="utf-8")
    assert "No HAPPY cell has recorded total_cost_usd yet" in cost_text
    assert "data-cost-usd=" not in cost_text
    assert "$0." not in cost_text
    assert pel.PARTIAL_NAME != "pareto-fronts.svg"
    partial_svg = pel.write_partial_svg(board, tmp_path / "eval2-partial.svg")
    partial_text = partial_svg.read_text(encoding="utf-8")
    assert 'data-partial-score="1.00"' in partial_text
    assert "0.50" not in partial_text
    assert "S=585 R=—" in partial_text
    assert "S=68 R=—" in partial_text
    assert "S=0 R=—" in partial_text
    assert "S=0 R=65" in partial_text
    assert "GPT-5.6 Luna" in partial_text
    assert "GPT-OSS 20B" in partial_text
    assert "Gemini 3.5 Flash Lite" in partial_text
    assert "Gemma 4 31B" in partial_text
    assert "Gemma 4 26B A4B" in partial_text
    assert "no ratio" in partial_text


def test_cli_check_and_refuse_string_harness_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    payload = _minimal_board_payload()
    src = tmp_path / "eval2_benchmark_results.json"
    src.write_text(json.dumps(payload), encoding="utf-8")
    assert pel.main(["--in", str(src), "--check"]) == 0
    out = capsys.readouterr().out
    assert "OK" in out
    assert "1 scored cells" in out

    forbidden = tmp_path / "benchmark_results.json"
    forbidden.write_text("[]", encoding="utf-8")
    with pytest.raises(pel.Eval2ResultsError, match="string-harness"):
        pel.main(["--in", str(forbidden), "--check"])


def test_cli_writes_svgs_and_print_matrix(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    payload = _minimal_board_payload()
    src = tmp_path / "eval2_benchmark_results.json"
    src.write_text(json.dumps(payload), encoding="utf-8")
    out_dir = tmp_path / "charts"
    assert pel.main(["--in", str(src), "--out-dir", str(out_dir), "--print-matrix"]) == 0
    printed = capsys.readouterr().out
    assert "| 3 | AFC Population | HAPPY / oracle PASS | — |" in printed
    assert "No HAPPY cell has recorded `total_cost_usd`" in printed
    assert "| 3 | AFC Population | Gemini 3.8 Flash | HAPPY | 1.00 |" in printed
    assert (out_dir / pel.HEATMAP_NAME).is_file()
    assert (out_dir / pel.COVERAGE_NAME).is_file()
    assert (out_dir / pel.COST_NAME).is_file()
    assert (out_dir / pel.PARTIAL_NAME).is_file()


def test_loader_rejects_parked_slot_and_null_null_cells(tmp_path: Path) -> None:
    payload = _minimal_board_payload()
    payload["tasks"].append(
        {
            "id": "writer-headed-template",
            "slot": 7,
            "slug": "writer-headed-template",
            "title": "Parked letterhead",
            "status": "Ready",
        }
    )
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(pel.Eval2ResultsError, match="PARKED"):
        pel.load_eval2_board(path)

    payload = _minimal_board_payload()
    payload["results"].append(
        {
            "task_id": "afc",
            "model": "openai/gpt-oss-20b",
            "product_bar": None,
            "oracle": None,
        }
    )
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(pel.Eval2ResultsError, match="omit empty"):
        pel.load_eval2_board(path)


def test_loader_rejects_unknown_task_and_wrong_schema(tmp_path: Path) -> None:
    payload = _minimal_board_payload()
    payload["schema_version"] = 2
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(pel.Eval2ResultsError, match="schema_version"):
        pel.load_eval2_board(path)

    payload = _minimal_board_payload()
    payload["results"][0]["task_id"] = "not-a-task"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(pel.Eval2ResultsError, match="not in tasks"):
        pel.load_eval2_board(path)


def test_plot_module_does_not_import_string_harness() -> None:
    source = Path(pel.__file__).read_text(encoding="utf-8")
    assert "benchmark_results.json" in source  # refusal path
    assert "from run_eval_multi" not in source
    assert "import plot_pareto" not in source
    assert "merge_benchmark_results" not in source


def test_intelligence_per_dollar_is_partial_squared_over_cost() -> None:
    assert pel.compute_intelligence_per_dollar("HAPPY", 0.25, 1.0) == 4.0
    assert pel.compute_intelligence_per_dollar("HAPPY", 0.25, 0.5) == 1.0
    assert pel.compute_intelligence_per_dollar("HAPPY", 0.25, None) is None
    assert pel.compute_intelligence_per_dollar("HAPPY", 0.0, 1.0) is None
    assert pel.compute_intelligence_per_dollar("HAPPY", None, 1.0) is None
    assert pel.compute_intelligence_per_dollar("NOT_HAPPY", 0.25, 1.0) is None
    assert pel.compute_intelligence_per_dollar(None, 0.25, 1.0) is None


def test_loader_accepts_optional_cost_fields_and_computes_ipd(tmp_path: Path) -> None:
    payload = _minimal_board_payload()
    payload["results"][0].update(
        {
            "total_tokens": 12000,
            "input_tokens": 8000,
            "output_tokens": 4000,
            "total_cost_usd": 0.5,
            "wall_time_s": 90,
        }
    )
    path = tmp_path / "with_cost.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    board = pel.load_eval2_board(path)
    row = board.results[0]
    assert row.total_tokens == 12000
    assert row.input_tokens == 8000
    assert row.output_tokens == 4000
    assert row.total_cost_usd == 0.5
    assert row.wall_time_s == 90.0
    assert row.intelligence_per_dollar == 2.0
    ranked = pel.happy_cost_rows(board)
    assert len(ranked) == 1
    assert ranked[0].cost_usd == 0.5
    assert ranked[0].intelligence_per_dollar == 2.0
    table = pel.render_cost_markdown(board)
    assert "| 3 | AFC Population | Gemini 3.8 Flash | 0.5000 | 12000 | 90 | 2.00 |" in table


def test_loader_does_not_compute_ipd_for_not_happy_cost(tmp_path: Path) -> None:
    payload = _minimal_board_payload()
    payload["results"][0]["product_bar"] = "NOT_HAPPY"
    payload["results"][0]["oracle"] = "FAIL"
    payload["results"][0]["total_cost_usd"] = 0.01
    path = tmp_path / "not_happy_cost.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    board = pel.load_eval2_board(path)
    assert board.results[0].total_cost_usd == 0.01
    assert board.results[0].intelligence_per_dollar is None
    assert pel.happy_cost_rows(board) == ()
    assert "stays empty" in pel.render_cost_markdown(board)


def test_loader_rejects_negative_or_bool_cost_fields(tmp_path: Path) -> None:
    payload = _minimal_board_payload()
    payload["results"][0]["total_cost_usd"] = -0.1
    path = tmp_path / "bad_cost.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(pel.Eval2ResultsError, match="total_cost_usd"):
        pel.load_eval2_board(path)

    payload = _minimal_board_payload()
    payload["results"][0]["total_tokens"] = True
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(pel.Eval2ResultsError, match="total_tokens"):
        pel.load_eval2_board(path)


def test_cost_chart_ranks_happy_by_lower_cost_and_skips_empty(
    tmp_path: Path,
) -> None:
    payload = _minimal_board_payload()
    # Cheaper HAPPY ranks first on AFC; the seed board (no costs) stays empty.
    payload["results"][0].update({"total_cost_usd": 0.20, "total_tokens": 4000})
    payload["results"].append(
        {
            "task_id": "afc",
            "model": "openai/gpt-oss-20b",
            "product_bar": "HAPPY",
            "oracle": "PASS",
            "total_cost_usd": 0.05,
            "total_tokens": 2500,
        }
    )
    payload["models"].append(
        {
            "openrouter_id": "x-ai/grok-4.6",
            "display_name": "Grok 4.6",
            "role": "catalog",
        }
    )
    payload["results"].append(
        {
            "task_id": "afc",
            "model": "x-ai/grok-4.6",
            "product_bar": "HAPPY",
            "oracle": "PASS",
            "total_tokens": 9999,
        }
    )
    path = tmp_path / "ranked.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    board = pel.load_eval2_board(path)
    ranked = pel.happy_cost_rows(board)
    assert [row.model.openrouter_id for row in ranked] == [
        "openai/gpt-oss-20b",
        "google/gemini-3.8-flash",
    ]
    svg = pel.write_cost_svg(board, tmp_path / "eval2-cost.svg").read_text(encoding="utf-8")
    assert "data-cost-usd=" in svg
    assert "0.0500" in svg
    assert "0.2000" in svg
    assert "GPT-OSS 20B" in svg
    assert "Gemini 3.8 Flash" in svg
    assert "AFC Population" in svg
    assert "No HAPPY cell has recorded total_cost_usd yet" not in svg
    # HAPPY without recorded USD must not invent a bar from tokens.
    assert "Grok 4.6" not in svg
    assert "9999" not in svg

    empty_board = pel.load_eval2_board(_RESULTS)
    empty_svg = pel.write_cost_svg(empty_board, tmp_path / "empty-cost.svg").read_text(
        encoding="utf-8"
    )
    assert "No HAPPY cell has recorded total_cost_usd yet" in empty_svg
    assert "data-cost-usd=" not in empty_svg


def test_partial_score_only_when_pass_or_both_counts() -> None:
    assert pel.compute_partial_score(True, None, None) == 1.0
    assert pel.compute_partial_score(True, 2, 6) == 1.0
    assert pel.compute_partial_score(False, 2, 6) == pytest.approx(4 / 6)
    assert pel.compute_partial_score(False, 0, 6) == 1.0
    assert pel.compute_partial_score(False, 6, 6) == 0.0
    assert pel.compute_partial_score(False, 2, None) is None
    assert pel.compute_partial_score(False, None, 6) is None
    assert pel.compute_partial_score(None, 2, 6) == pytest.approx(4 / 6)


def test_loader_accepts_oracle_partial_and_ranks_happy_then_quality_then_cost(
    tmp_path: Path,
) -> None:
    payload = _minimal_board_payload()
    payload["results"][0].update(
        {
            "total_cost_usd": 0.40,
            "afc_s_flags": 65,
            "afc_r_required": 2,
            "husk_cells": 0,
            "scored_cells": 80,
        }
    )
    payload["results"].append(
        {
            "task_id": "afc",
            "model": "openai/gpt-oss-20b",
            "product_bar": "HAPPY",
            "oracle": "FAIL",
            "oracle_failures": ["S=10 flag=1 in column K is < R=40"],
            "oracle_check_count": 6,
            "afc_s_flags": 10,
            "afc_r_required": 40,
            "total_cost_usd": 0.05,
        }
    )
    payload["models"].append(
        {
            "openrouter_id": "x-ai/grok-4.6",
            "display_name": "Grok 4.6",
            "role": "catalog",
        }
    )
    payload["results"].append(
        {
            "task_id": "afc",
            "model": "x-ai/grok-4.6",
            "product_bar": "NOT_HAPPY",
            "oracle": "FAIL",
            "oracle_failures": ["Sample has no data rows"],
            "oracle_check_count": 6,
            "total_cost_usd": 0.01,
        }
    )
    path = tmp_path / "partial.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    board = pel.load_eval2_board(path)
    gemini = board.result_for("afc", "google/gemini-3.8-flash")
    oss = board.result_for("afc", "openai/gpt-oss-20b")
    grok = board.result_for("afc", "x-ai/grok-4.6")
    assert gemini is not None and oss is not None and grok is not None
    assert gemini.partial_score == 1.0
    assert gemini.afc_s_flags == 65
    assert oss.oracle_failure_count == 1
    assert oss.partial_score == pytest.approx(5 / 6)
    assert grok.partial_score == pytest.approx(5 / 6)
    assert grok.product_bar == "NOT_HAPPY"
    ranked = pel.ranked_rows(board)
    assert [row.model.openrouter_id for row in ranked] == [
        "google/gemini-3.8-flash",
        "openai/gpt-oss-20b",
        "x-ai/grok-4.6",
    ]
    table = pel.render_partial_markdown(board)
    assert "| 3 | AFC Population | Gemini 3.8 Flash | HAPPY | 1.00 |" in table
    assert "S=65 R=2" in table
    assert "S=10 R=40" in table
    svg = pel.write_partial_svg(board, tmp_path / "eval2-partial.svg").read_text(
        encoding="utf-8"
    )
    assert 'data-partial-score="1.00"' in svg
    assert 'data-partial-score="0.83"' in svg
    assert "Grok 4.6" in svg
    assert "No cell has recorded oracle partial yet" not in svg


def test_loader_does_not_invent_partial_ratio_without_check_count(tmp_path: Path) -> None:
    payload = _minimal_board_payload()
    payload["results"][0]["product_bar"] = "NOT_HAPPY"
    payload["results"][0]["oracle"] = "FAIL"
    payload["results"][0]["oracle_failures"] = ["missing Harborview Flats"]
    path = tmp_path / "no_denom.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    board = pel.load_eval2_board(path)
    row = board.results[0]
    assert row.oracle_failure_count == 1
    assert row.oracle_check_count is None
    assert row.partial_score is None
    svg = pel.write_partial_svg(board, tmp_path / "eval2-partial.svg").read_text(
        encoding="utf-8"
    )
    assert "no ratio" in svg
    assert "data-partial-score=" not in svg


def test_loader_rejects_bad_partial_fields(tmp_path: Path) -> None:
    payload = _minimal_board_payload()
    payload["results"][0]["partial_score"] = 1.5
    path = tmp_path / "bad_partial.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(pel.Eval2ResultsError, match="partial_score"):
        pel.load_eval2_board(path)

    payload = _minimal_board_payload()
    payload["results"][0]["oracle_check_count"] = 0
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(pel.Eval2ResultsError, match="oracle_check_count"):
        pel.load_eval2_board(path)

    payload = _minimal_board_payload()
    payload["results"][0]["oracle_failures"] = [1]
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(pel.Eval2ResultsError, match="oracle_failures"):
        pel.load_eval2_board(path)
