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
    assert "BLOCKED" in schema["$defs"]["product_bar"]["enum"]
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
        "nvidia/nemotron-3.5-lightning",
        "inception/mercury-2.5-preview",
        "x-ai/grok-4.6",
        "meta/muse-glimmer-30b",
        "meta/muse-spark-1.3-contributor",
        "poolside/laguna-s-2.1",
        "poolside/laguna-xs-2.1",
        "qwen/qwen3.8-27b",
        "qwen/qwen3.8-flash",
        "z-ai/glm-5.3-flash",
        "upstage/solar-pro4",
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
    nemo = "nvidia/nemotron-3.5-lightning"
    mercury = "inception/mercury-2.5-preview"
    grok = "x-ai/grok-4.6"
    glimmer = "meta/muse-glimmer-30b"
    spark = "meta/muse-spark-1.3-contributor"
    laguna = "poolside/laguna-s-2.1"
    laguna_xs = "poolside/laguna-xs-2.1"
    qwen27 = "qwen/qwen3.8-27b"
    qwen_flash = "qwen/qwen3.8-flash"
    glm_flash = "z-ai/glm-5.3-flash"
    solar = "upstage/solar-pro4"

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

    # Seventh catalog AFC stamp (Scrolly headed 20260912-0310).
    afc_nemo = board.result_for("afc", nemo)
    assert afc_nemo is not None
    assert afc_nemo.product_bar == "NOT_HAPPY"
    assert afc_nemo.oracle == "FAIL"
    assert afc_nemo.stamp == "20260912-0310-nemotron-3.5-lightning"
    assert afc_nemo.oracle_passed is False
    assert afc_nemo.oracle_failure_count == 2
    assert afc_nemo.oracle_failures == (
        "missing sheet 'Sample'",
        "missing sheet 'Sample Size Calculation'",
    )
    assert afc_nemo.oracle_check_count is None
    assert afc_nemo.partial_score is None
    assert afc_nemo.afc_s_flags == 0
    assert afc_nemo.afc_r_required is None
    assert afc_nemo.husk_cells == 0
    assert afc_nemo.scored_cells == 0
    assert afc_nemo.input_tokens == 760171
    assert afc_nemo.output_tokens == 3189
    assert afc_nemo.total_tokens == 763360
    assert afc_nemo.wall_time_s == 540
    assert afc_nemo.total_cost_usd == pytest.approx(0.04282)
    assert afc_nemo.intelligence_per_dollar is None
    assert "20260912-0310-nemotron-3.5-lightning" in afc_nemo.oracle_note
    assert "0.06145" in afc_nemo.oracle_note
    assert "second-send" in afc_nemo.oracle_note
    assert "Err:507" in afc_nemo.oracle_note

    # Eighth catalog AFC stamp (Scrolly headed 20260912-0330).
    afc_mercury = board.result_for("afc", mercury)
    assert afc_mercury is not None
    assert afc_mercury.product_bar == "HAPPY"
    assert afc_mercury.oracle == "PASS"
    assert afc_mercury.stamp == "20260912-0330-mercury-2.5-preview"
    assert afc_mercury.oracle_passed is True
    assert afc_mercury.oracle_failure_count == 0
    assert afc_mercury.oracle_failures == ()
    assert afc_mercury.oracle_check_count is None
    assert afc_mercury.partial_score == 1.0
    assert afc_mercury.afc_s_flags == 494
    assert afc_mercury.afc_r_required == 68
    assert afc_mercury.husk_cells == 2
    assert afc_mercury.scored_cells == 16680
    assert afc_mercury.input_tokens == 115096
    assert afc_mercury.output_tokens == 3554
    assert afc_mercury.total_tokens == 118650
    assert afc_mercury.wall_time_s == 479
    assert afc_mercury.total_cost_usd == pytest.approx(0.00468)
    assert afc_mercury.intelligence_per_dollar == pytest.approx(1.0 / 0.00468)
    assert "20260912-0330-mercury-2.5-preview" in afc_mercury.oracle_note
    assert "0.03144" in afc_mercury.oracle_note
    assert "near-full" in afc_mercury.oracle_note

    # Ninth catalog AFC stamp (Scrolly headed 20260912-0342).
    afc_grok = board.result_for("afc", grok)
    assert afc_grok is not None
    assert afc_grok.product_bar == "NOT_HAPPY"
    assert afc_grok.oracle == "FAIL"
    assert afc_grok.stamp == "20260912-0342-grok-4.6"
    assert afc_grok.oracle_passed is False
    assert afc_grok.oracle_failure_count == 2
    assert afc_grok.oracle_failures == (
        "Sample has no data rows",
        "R from Sample Size Calculation is missing or < 1",
    )
    assert afc_grok.oracle_check_count is None
    assert afc_grok.partial_score is None
    assert afc_grok.afc_s_flags == 0
    assert afc_grok.afc_r_required is None
    assert afc_grok.husk_cells == 0
    assert afc_grok.scored_cells == 0
    assert afc_grok.input_tokens == 28184
    assert afc_grok.output_tokens == 323
    assert afc_grok.total_tokens == 28507
    assert afc_grok.wall_time_s == 416
    assert afc_grok.total_cost_usd == pytest.approx(0.02375)
    assert afc_grok.intelligence_per_dollar is None
    assert "20260912-0342-grok-4.6" in afc_grok.oracle_note
    assert "0.05831" in afc_grok.oracle_note
    assert "SSC present" in afc_grok.oracle_note

    # Tenth catalog AFC stamp (Scrolly headed 20260912-0353).
    afc_glimmer = board.result_for("afc", glimmer)
    assert afc_glimmer is not None
    assert afc_glimmer.product_bar == "NOT_HAPPY"
    assert afc_glimmer.oracle == "FAIL"
    assert afc_glimmer.stamp == "20260912-0353-muse-glimmer-30b"
    assert afc_glimmer.oracle_passed is False
    assert afc_glimmer.oracle_failure_count == 1
    assert afc_glimmer.oracle_failures == (
        "R from Sample Size Calculation is missing or < 1",
    )
    assert afc_glimmer.oracle_check_count is None
    assert afc_glimmer.partial_score is None
    assert afc_glimmer.afc_s_flags == 0
    assert afc_glimmer.afc_r_required is None
    assert afc_glimmer.husk_cells == 0
    assert afc_glimmer.scored_cells == 729
    assert afc_glimmer.input_tokens == 2143441
    assert afc_glimmer.output_tokens == 16060
    assert afc_glimmer.total_tokens == 2159501
    assert afc_glimmer.wall_time_s == 155
    assert afc_glimmer.total_cost_usd == pytest.approx(0.15404)
    assert afc_glimmer.intelligence_per_dollar is None
    assert "20260912-0353-muse-glimmer-30b" in afc_glimmer.oracle_note
    assert "0.66230" in afc_glimmer.oracle_note
    assert "e0bb6321" in afc_glimmer.oracle_note
    assert "50 tool" in afc_glimmer.oracle_note

    # Eleventh catalog AFC stamp (Scrolly headed 20260912-0409).
    afc_spark = board.result_for("afc", spark)
    assert afc_spark is not None
    assert afc_spark.product_bar == "NOT_HAPPY"
    assert afc_spark.oracle == "FAIL"
    assert afc_spark.stamp == "20260912-0409-muse-spark-1.3-contributor"
    assert afc_spark.oracle_passed is False
    assert afc_spark.oracle_failure_count == 2
    assert afc_spark.oracle_failures == (
        "Sample has no data rows",
        "R from Sample Size Calculation is missing or < 1",
    )
    assert afc_spark.oracle_check_count is None
    assert afc_spark.partial_score is None
    assert afc_spark.afc_s_flags == 0
    assert afc_spark.afc_r_required is None
    assert afc_spark.husk_cells == 0
    assert afc_spark.scored_cells == 0
    assert afc_spark.input_tokens == 23560
    assert afc_spark.output_tokens == 550
    assert afc_spark.total_tokens == 24110
    assert afc_spark.wall_time_s == 241
    assert afc_spark.total_cost_usd == pytest.approx(0.00157)
    assert afc_spark.intelligence_per_dollar is None
    assert "20260912-0409-muse-spark-1.3-contributor" in afc_spark.oracle_note
    assert "0.00247" in afc_spark.oracle_note
    assert "6c6017b7" in afc_spark.oracle_note

    # Twelfth catalog AFC stamp (Scrolly headed 20260912-0419).
    afc_laguna = board.result_for("afc", laguna)
    assert afc_laguna is not None
    assert afc_laguna.product_bar == "NOT_HAPPY"
    assert afc_laguna.oracle == "FAIL"
    assert afc_laguna.stamp == "20260912-0419-laguna-s-2.1"
    assert afc_laguna.oracle_passed is False
    assert afc_laguna.oracle_failure_count == 1
    assert afc_laguna.oracle_failures == ("missing sheet 'Sample'",)
    assert afc_laguna.oracle_check_count is None
    assert afc_laguna.partial_score is None
    assert afc_laguna.afc_s_flags == 0
    assert afc_laguna.afc_r_required == 66
    assert afc_laguna.husk_cells == 0
    assert afc_laguna.scored_cells == 0
    assert afc_laguna.input_tokens == 166351
    assert afc_laguna.output_tokens == 35845
    assert afc_laguna.total_tokens == 202196
    assert afc_laguna.wall_time_s == 384
    assert afc_laguna.total_cost_usd == pytest.approx(0.01480)
    assert afc_laguna.intelligence_per_dollar is None
    assert "20260912-0419-laguna-s-2.1" in afc_laguna.oracle_note
    assert "0.02142" in afc_laguna.oracle_note
    assert "6c6017b7" in afc_laguna.oracle_note
    assert "finish_reason=length" in afc_laguna.oracle_note

    # Thirteenth catalog AFC stamp (Scrolly headed 20260912-0429).
    afc_laguna_xs = board.result_for("afc", laguna_xs)
    assert afc_laguna_xs is not None
    assert afc_laguna_xs.product_bar == "NOT_HAPPY"
    assert afc_laguna_xs.oracle == "FAIL"
    assert afc_laguna_xs.stamp == "20260912-0429-laguna-xs-2.1"
    assert afc_laguna_xs.oracle_passed is False
    assert afc_laguna_xs.oracle_failure_count == 2
    assert afc_laguna_xs.oracle_failures == (
        "missing sheet 'Sample Size Calculation'",
        "Sample has no data rows",
    )
    assert afc_laguna_xs.oracle_check_count is None
    assert afc_laguna_xs.partial_score is None
    assert afc_laguna_xs.afc_s_flags == 0
    assert afc_laguna_xs.afc_r_required is None
    assert afc_laguna_xs.husk_cells == 0
    assert afc_laguna_xs.scored_cells == 0
    assert afc_laguna_xs.input_tokens == 1280356
    assert afc_laguna_xs.output_tokens == 10168
    assert afc_laguna_xs.total_tokens == 1290524
    assert afc_laguna_xs.wall_time_s == 85
    assert afc_laguna_xs.total_cost_usd == pytest.approx(0.04341)
    assert afc_laguna_xs.intelligence_per_dollar is None
    assert "20260912-0429-laguna-xs-2.1" in afc_laguna_xs.oracle_note
    assert "0.07804" in afc_laguna_xs.oracle_note
    assert "6c6017b7" in afc_laguna_xs.oracle_note
    assert "Sample_Size_Calculation" in afc_laguna_xs.oracle_note

    # Fourteenth catalog AFC stamp (Scrolly headed 20260912-0443).
    afc_qwen27 = board.result_for("afc", qwen27)
    assert afc_qwen27 is not None
    assert afc_qwen27.product_bar == "NOT_HAPPY"
    assert afc_qwen27.oracle == "FAIL"
    assert afc_qwen27.stamp == "20260912-0443-qwen3.8-27b"
    assert afc_qwen27.oracle_passed is False
    assert afc_qwen27.oracle_failure_count == 2
    assert afc_qwen27.oracle_failures == (
        "missing sheet 'Sample'",
        "missing sheet 'Sample Size Calculation'",
    )
    assert afc_qwen27.oracle_check_count is None
    assert afc_qwen27.partial_score is None
    assert afc_qwen27.afc_s_flags == 0
    assert afc_qwen27.afc_r_required is None
    assert afc_qwen27.husk_cells == 0
    assert afc_qwen27.scored_cells == 0
    assert afc_qwen27.input_tokens == 2695380
    assert afc_qwen27.output_tokens == 67284
    assert afc_qwen27.total_tokens == 2762664
    assert afc_qwen27.wall_time_s == 720
    assert afc_qwen27.total_cost_usd == pytest.approx(1.42823)
    assert afc_qwen27.intelligence_per_dollar is None
    assert "20260912-0443-qwen3.8-27b" in afc_qwen27.oracle_note
    assert "1.31711" in afc_qwen27.oracle_note
    assert "6c6017b7" in afc_qwen27.oracle_note
    assert "finish_reason=tool_calls" in afc_qwen27.oracle_note
    assert "n=51" in afc_qwen27.oracle_note

    # Fifteenth catalog AFC stamp (Scrolly headed 20260912-0502).
    afc_qwen_flash = board.result_for("afc", qwen_flash)
    assert afc_qwen_flash is not None
    assert afc_qwen_flash.product_bar == "NOT_HAPPY"
    assert afc_qwen_flash.oracle == "FAIL"
    assert afc_qwen_flash.stamp == "20260912-0502-qwen3.8-flash"
    assert afc_qwen_flash.oracle_passed is False
    assert afc_qwen_flash.oracle_failure_count == 1
    assert afc_qwen_flash.oracle_failures == (
        "R from Sample Size Calculation is missing or < 1",
    )
    assert afc_qwen_flash.oracle_check_count is None
    assert afc_qwen_flash.partial_score is None
    assert afc_qwen_flash.afc_s_flags == 68
    assert afc_qwen_flash.afc_r_required is None
    assert afc_qwen_flash.husk_cells == 0
    assert afc_qwen_flash.scored_cells == 680
    assert afc_qwen_flash.input_tokens == 2194886
    assert afc_qwen_flash.output_tokens == 49253
    assert afc_qwen_flash.total_tokens == 2244139
    assert afc_qwen_flash.wall_time_s == 540
    assert afc_qwen_flash.total_cost_usd == pytest.approx(0.11382)
    assert afc_qwen_flash.intelligence_per_dollar is None
    assert "20260912-0502-qwen3.8-flash" in afc_qwen_flash.oracle_note
    assert "0.35238" in afc_qwen_flash.oracle_note
    assert "6c6017b7" in afc_qwen_flash.oracle_note
    assert "Err:508" in afc_qwen_flash.oracle_note
    assert "n=51" in afc_qwen_flash.oracle_note

    # Sixteenth catalog AFC stamp (Scrolly headed 20260912-0515).
    afc_glm_flash = board.result_for("afc", glm_flash)
    assert afc_glm_flash is not None
    assert afc_glm_flash.product_bar == "NOT_HAPPY"
    assert afc_glm_flash.oracle == "FAIL"
    assert afc_glm_flash.stamp == "20260912-0515-glm-5.3-flash"
    assert afc_glm_flash.oracle_passed is False
    assert afc_glm_flash.oracle_failure_count == 2
    assert afc_glm_flash.oracle_failures == (
        "missing sheet 'Sample'",
        "missing sheet 'Sample Size Calculation'",
    )
    assert afc_glm_flash.oracle_check_count is None
    assert afc_glm_flash.partial_score is None
    assert afc_glm_flash.afc_s_flags == 0
    assert afc_glm_flash.afc_r_required is None
    assert afc_glm_flash.husk_cells == 0
    assert afc_glm_flash.scored_cells == 0
    assert afc_glm_flash.input_tokens == 5404
    assert afc_glm_flash.output_tokens == 16384
    assert afc_glm_flash.total_tokens == 21788
    assert afc_glm_flash.wall_time_s == 155
    assert afc_glm_flash.total_cost_usd == pytest.approx(0.00900)
    assert afc_glm_flash.intelligence_per_dollar is None
    assert "20260912-0515-glm-5.3-flash" in afc_glm_flash.oracle_note
    assert "0.00450" in afc_glm_flash.oracle_note
    assert "6c6017b7" in afc_glm_flash.oracle_note
    assert "finish_reason=length" in afc_glm_flash.oracle_note
    assert "n=1" in afc_glm_flash.oracle_note
    assert "15160" in afc_glm_flash.oracle_note

    # Seventeenth catalog AFC stamp — infra BLOCKED, not a model fail.
    afc_solar = board.result_for("afc", solar)
    assert afc_solar is not None
    assert afc_solar.product_bar == "BLOCKED"
    assert afc_solar.product_bar != "NOT_HAPPY"
    assert afc_solar.oracle is None
    assert afc_solar.stamp == "20260912-0522-solar-pro4"
    assert afc_solar.oracle_passed is None
    assert afc_solar.oracle_failure_count is None
    assert afc_solar.oracle_failures is None
    assert afc_solar.oracle_check_count is None
    assert afc_solar.partial_score is None
    assert afc_solar.afc_s_flags is None
    assert afc_solar.afc_r_required is None
    assert afc_solar.husk_cells is None
    assert afc_solar.scored_cells is None
    assert afc_solar.input_tokens == 0
    assert afc_solar.output_tokens == 0
    assert afc_solar.total_tokens == 0
    assert afc_solar.wall_time_s == 1080
    assert afc_solar.total_cost_usd == pytest.approx(0.0)
    assert afc_solar.intelligence_per_dollar is None
    assert "20260912-0522-solar-pro4" in afc_solar.oracle_note
    assert "message_store" in afc_solar.oracle_note
    assert "6c6017b7" in afc_solar.oracle_note
    assert pel.cell_label(afc_solar) == "BLOCKED / unscored"
    assert not pel.has_recorded_partial(afc_solar)
    assert board.scored_count(solar) == 0
    assert board.blocked_count(solar) == 1
    assert board.not_happy_count(solar) == 0

    # No invented catalog AFC-only scores outside AFC.
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
        assert board.result_for(task_id, nemo) is None
        assert board.result_for(task_id, mercury) is None
        assert board.result_for(task_id, grok) is None
        assert board.result_for(task_id, glimmer) is None
        assert board.result_for(task_id, spark) is None
        assert board.result_for(task_id, laguna) is None
        assert board.result_for(task_id, laguna_xs) is None
        assert board.result_for(task_id, qwen27) is None
        assert board.result_for(task_id, qwen_flash) is None
        assert board.result_for(task_id, glm_flash) is None
        assert board.result_for(task_id, solar) is None

    # Other seed cells must not invent run costs or oracle-partial counts.
    recorded_afc = {
        ("afc", oss),
        ("afc", luna),
        ("afc", oss20),
        ("afc", lite),
        ("afc", gemma),
        ("afc", gemma26),
        ("afc", nemo),
        ("afc", mercury),
        ("afc", grok),
        ("afc", glimmer),
        ("afc", spark),
        ("afc", laguna),
        ("afc", laguna_xs),
        ("afc", qwen27),
        ("afc", qwen_flash),
        ("afc", glm_flash),
        ("afc", solar),
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
    happy_costs = pel.happy_cost_rows(board)
    assert len(happy_costs) == 1
    assert happy_costs[0].model.openrouter_id == mercury
    assert happy_costs[0].cost_usd == pytest.approx(0.00468)
    assert happy_costs[0].intelligence_per_dollar == pytest.approx(1.0 / 0.00468)
    afc = board.result_for("afc", gemini)
    assert afc is not None
    assert pel.has_recorded_partial(afc)
    assert pel.has_recorded_partial(afc_oss)
    assert pel.has_recorded_partial(afc_luna)
    assert pel.has_recorded_partial(afc_20b)
    assert pel.has_recorded_partial(afc_lite)
    assert pel.has_recorded_partial(afc_gemma)
    assert pel.has_recorded_partial(afc_gemma26)
    assert pel.has_recorded_partial(afc_nemo)
    assert pel.has_recorded_partial(afc_mercury)
    assert pel.has_recorded_partial(afc_grok)
    assert pel.has_recorded_partial(afc_glimmer)
    assert pel.has_recorded_partial(afc_spark)
    assert pel.has_recorded_partial(afc_laguna)
    assert pel.has_recorded_partial(afc_laguna_xs)
    assert pel.has_recorded_partial(afc_qwen27)
    assert pel.has_recorded_partial(afc_qwen_flash)
    assert pel.has_recorded_partial(afc_glm_flash)


_RECORDED_AFC_CATALOG = {
    ("afc", "openai/gpt-oss-120b"),
    ("afc", "openai/gpt-5.6-luna"),
    ("afc", "openai/gpt-oss-20b"),
    ("afc", "google/gemini-3.5-flash-lite"),
    ("afc", "google/gemma-4-31b-it"),
    ("afc", "google/gemma-4-26b-a4b-it"),
    ("afc", "nvidia/nemotron-3.5-lightning"),
    ("afc", "inception/mercury-2.5-preview"),
    ("afc", "x-ai/grok-4.6"),
    ("afc", "meta/muse-glimmer-30b"),
    ("afc", "meta/muse-spark-1.3-contributor"),
    ("afc", "poolside/laguna-s-2.1"),
    ("afc", "poolside/laguna-xs-2.1"),
    ("afc", "qwen/qwen3.8-27b"),
    ("afc", "qwen/qwen3.8-flash"),
    ("afc", "z-ai/glm-5.3-flash"),
    ("afc", "upstage/solar-pro4"),
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
    assert "0 HAPPY / 1 scored" in coverage
    assert "1 HAPPY / 1 scored" in coverage
    assert "0 HAPPY / 0 scored" in coverage
    assert "BLOCKED" in heatmap
    assert "Solar Pro 4" in heatmap
    assert "No HAPPY cell has recorded total_cost_usd yet" not in cost
    assert "data-cost-usd=\"0.004680\"" in cost
    assert "Mercury 2.5 Preview" in cost
    assert "213.68" in cost
    # Seed has one oracle PASS (AFC Gemini 3.8) → partial 1. Catalog AFC
    # cells recorded S/R + failure_count without a check-count ratio.
    assert "data-partial-score=\"1.00\"" in partial
    assert "Gemini 3.8 Flash" in partial
    assert "Gemini 3.5 Flash Lite" in partial
    assert "Gemma 4 31B" in partial
    assert "Gemma 4 26B A4B" in partial
    assert "Nemotron 3.5 Lightning" in partial
    assert "Mercury 2.5 Preview" in partial
    assert "Grok 4.6" in partial
    assert "Muse Glimmer 30B" in partial
    assert "Muse Spark 1.3" in partial
    assert "Laguna S 2.1" in partial
    assert "Laguna XS 2.1" in partial
    assert "Qwen3.8 27B" in partial
    assert "Qwen3.8 Flash" in partial
    assert "GLM 5.3 Flash" in partial
    assert "Solar Pro 4" not in partial
    assert "AFC Population" in partial
    assert "GPT-OSS 120B" in partial
    assert "GPT-5.6 Luna" in partial
    assert "GPT-OSS 20B" in partial
    assert "S=585 R=—" in partial
    assert "S=68 R=—" in partial
    assert "S=0 R=—" in partial
    assert "S=0 R=65" in partial
    assert "S=0 R=66" in partial
    assert "S=494 R=68" in partial
    assert "2/—" in partial
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
    assert "0 HAPPY / 1 scored" in cov
    assert "1 HAPPY / 1 scored" in cov
    assert "0 HAPPY / 0 scored" in cov
    assert "BLOCKED" in heat
    assert "Solar Pro 4" in heat
    assert pel.HEATMAP_NAME != "pareto-fronts.svg"
    assert pel.COVERAGE_NAME != "pareto-distance.svg"
    assert pel.COST_NAME != "pareto-fronts.svg"

    cost_svg = pel.write_cost_svg(board, tmp_path / "eval2-cost.svg")
    cost_text = cost_svg.read_text(encoding="utf-8")
    assert "No HAPPY cell has recorded total_cost_usd yet" not in cost_text
    assert "data-cost-usd=\"0.004680\"" in cost_text
    assert "Mercury 2.5 Preview" in cost_text
    assert "213.68" in cost_text
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
    assert "Nemotron 3.5 Lightning" in partial_text
    assert "Mercury 2.5 Preview" in partial_text
    assert "Grok 4.6" in partial_text
    assert "Muse Glimmer 30B" in partial_text
    assert "Muse Spark 1.3" in partial_text
    assert "Laguna S 2.1" in partial_text
    assert "Laguna XS 2.1" in partial_text
    assert "Qwen3.8 27B" in partial_text
    assert "Qwen3.8 Flash" in partial_text
    assert "GLM 5.3 Flash" in partial_text
    assert "Solar Pro 4" not in partial_text
    assert "S=0 R=66" in partial_text
    assert "S=494 R=68" in partial_text
    assert "2/—" in partial_text
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

    empty_src = tmp_path / "no-happy-cost.json"
    empty_src.write_text(json.dumps(_minimal_board_payload()), encoding="utf-8")
    empty_board = pel.load_eval2_board(empty_src)
    empty_svg = pel.write_cost_svg(empty_board, tmp_path / "empty-cost.svg").read_text(
        encoding="utf-8"
    )
    assert "No HAPPY cell has recorded total_cost_usd yet" in empty_svg
    assert "data-cost-usd=" not in empty_svg

    seed_cost = pel.write_cost_svg(pel.load_eval2_board(_RESULTS), tmp_path / "seed-cost.svg")
    seed_cost_text = seed_cost.read_text(encoding="utf-8")
    assert "data-cost-usd=\"0.004680\"" in seed_cost_text
    assert "Mercury 2.5 Preview" in seed_cost_text
    assert "213.68" in seed_cost_text
    assert "No HAPPY cell has recorded total_cost_usd yet" not in seed_cost_text


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
