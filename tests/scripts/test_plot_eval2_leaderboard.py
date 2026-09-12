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
    assert "hard_pass_rate" not in _SCHEMA.read_text(encoding="utf-8")
    assert {task.slot for task in board.tasks} == {1, 2, 3, 4, 5, 6, 8, 9, 10}
    assert all(task.slot != 7 for task in board.tasks)
    assert {model.openrouter_id for model in board.models} >= {
        "google/gemini-3.8-flash",
        "openai/gpt-oss-120b",
        "openai/gpt-oss-20b",
        "x-ai/grok-4.6",
        "meta/muse-spark-1.3-contributor",
    }


def test_seed_cells_match_autopsy_and_leave_unknowns_empty() -> None:
    board = pel.load_eval2_board(_RESULTS)
    gemini = "google/gemini-3.8-flash"
    oss = "openai/gpt-oss-120b"

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

    # No invented gpt-oss scores for Gemini-only Writer/AFC stamps.
    for task_id in ("tenant-retention", "cadaver-proposal", "afc"):
        assert board.result_for(task_id, oss) is None

    # Catalog peers stay empty until a real stamp lands.
    for mid in (
        "openai/gpt-oss-20b",
        "x-ai/grok-4.6",
        "meta/muse-spark-1.3-contributor",
    ):
        assert board.scored_count(mid) == 0
        assert board.happy_count(mid) == 0


def test_every_seed_source_points_at_an_in_repo_doc() -> None:
    payload = json.loads(_RESULTS.read_text(encoding="utf-8"))
    for row in payload["results"]:
        source = str(row.get("source") or "")
        assert source, row
        first = source.split(";")[0].strip().split()[0]
        path = _REPO / first
        assert path.is_file(), first
        assert row.get("run_artifacts_committed") is False


def test_scoreboard_markdown_matches_seed_matrix() -> None:
    board = pel.load_eval2_board(_RESULTS)
    matrix = pel.render_matrix_markdown(board)
    text = _SCOREBOARD.read_text(encoding="utf-8")
    assert "hard_pass_rate" not in text
    assert "google/gemini-3.8-flash" in text
    assert "not" in text.lower() and "string" in text.lower()
    assert "[`docs/eval/benchmarks.md`](../benchmarks.md)" in text
    assert "eval2-heatmap.svg" in text
    assert "eval2-coverage.svg" in text
    assert "Catalog-wide" in text or "catalog" in text.lower()
    assert "sweep has **not** happened" in text
    for line in matrix.strip().splitlines():
        if line.startswith("| # |"):
            continue
        assert line in text, line
    readme = _README.read_text(encoding="utf-8")
    assert "[`benchmarks.md`](benchmarks.md)" in readme
    assert "string-pack Pareto" in readme
    heatmap = (_EVAL2 / pel.HEATMAP_NAME).read_text(encoding="utf-8")
    coverage = (_EVAL2 / pel.COVERAGE_NAME).read_text(encoding="utf-8")
    assert "no data" in heatmap
    assert "HAPPY" in heatmap
    assert "0 HAPPY / 0 scored" in coverage


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
    assert "hard_pass_rate" not in heat
    assert "pareto" not in heat.lower()
    assert "google/gemini-3.8-flash" in heat
    assert "Tenant Retention" in heat
    assert "no data" in cov
    assert "0 HAPPY / 0 scored" in cov
    assert pel.HEATMAP_NAME != "pareto-fronts.svg"
    assert pel.COVERAGE_NAME != "pareto-distance.svg"


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
    assert (out_dir / pel.HEATMAP_NAME).is_file()
    assert (out_dir / pel.COVERAGE_NAME).is_file()


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
