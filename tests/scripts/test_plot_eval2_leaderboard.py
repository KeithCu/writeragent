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
from xml.etree import ElementTree as ET

import pytest

_REPO = Path(__file__).resolve().parents[2]
_SCRIPTS = _REPO / "scripts"
_EVAL2 = _REPO / "docs" / "eval" / "eval-2"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import plot_eval2_leaderboard as pel  # noqa: E402

_RESULTS = _EVAL2 / "eval2_benchmark_results.json"
_SCHEMA = _EVAL2 / "eval2_benchmark_results.schema.json"


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


def _check_object(
    obj: dict[str, Any],
    definition: dict[str, Any],
    defs: dict[str, Any],
    where: str,
) -> None:
    props = definition["properties"]
    extra = set(obj) - set(props)
    assert not extra, f"{where}: keys not in schema: {sorted(extra)}"
    missing = set(definition["required"]) - set(obj)
    assert not missing, f"{where}: required keys missing: {sorted(missing)}"
    for key, value in obj.items():
        spec = props[key]
        if "$ref" in spec:
            spec = defs[spec["$ref"].rsplit("/", 1)[-1]]
        if "enum" in spec:
            assert value in spec["enum"], f"{where}.{key}: {value!r} not in {spec['enum']}"


def test_seed_json_loads_and_conforms_to_schema() -> None:
    board = pel.load_eval2_board(_RESULTS)
    assert (_REPO / board.source_of_truth).is_file()
    schema = json.loads(_SCHEMA.read_text(encoding="utf-8"))
    payload = json.loads(_RESULTS.read_text(encoding="utf-8"))
    defs = schema["$defs"]
    assert schema["properties"]["schema_version"]["const"] == pel.SCHEMA_VERSION
    assert pel.PRODUCT_BARS == {v for v in defs["product_bar"]["enum"] if v is not None}
    assert pel.ORACLES == {v for v in defs["oracle"]["enum"] if v is not None}
    assert pel.TASK_STATUSES == set(defs["task"]["properties"]["status"]["enum"])
    assert pel.MODEL_ROLES == set(defs["model"]["properties"]["role"]["enum"])
    _check_object(payload, schema, defs, "board")
    for name, rows in (
        ("task", payload["tasks"]),
        ("model", payload["models"]),
        ("result", payload["results"]),
    ):
        for index, row in enumerate(rows):
            _check_object(row, defs[name], defs, f"{name}[{index}]")


def test_every_seed_source_points_at_an_in_repo_doc() -> None:
    payload = json.loads(_RESULTS.read_text(encoding="utf-8"))
    slug = {t["id"]: t["slug"] for t in payload["tasks"]}
    for row in payload["results"]:
        source = str(row.get("source") or "")
        assert source, row
        first = source.split(";")[0].strip().split()[0]
        path = _REPO / first
        assert path.is_file(), first
        committed = row.get("run_artifacts_committed")
        assert committed is False or committed is True
        if committed is True:
            stamp = str(row.get("stamp") or "")
            assert stamp, row
            run_dir = _EVAL2 / slug[row["task_id"]] / "runs" / stamp
            assert run_dir.is_dir(), run_dir
            for name in (
                "notes.txt",
                "score.txt",
                "status.txt",
                "prompt_used.txt",
                "final_workbook.ods",
                "writeragent_debug.log",
            ):
                assert (run_dir / name).is_file(), run_dir / name
        else:
            assert committed is False


def _text_nodes(svg: str) -> list[str]:
    root = ET.fromstring(svg)
    return ["".join(el.itertext()) for el in root.iter("{http://www.w3.org/2000/svg}text")]


def test_heatmap_and_coverage_svgs_render_each_cell_state(tmp_path: Path) -> None:
    payload = _minimal_board_payload()
    payload["tasks"].append(
        {
            "id": "tenant-retention",
            "slot": 1,
            "slug": "tenant-retention-2026",
            "title": "Tenant Retention",
            "status": "Ready",
        }
    )
    payload["models"].append(
        {
            "openrouter_id": "x-ai/grok-4.6",
            "display_name": "Grok 4.6",
            "role": "catalog",
        }
    )
    payload["results"].extend(
        [
            {
                "task_id": "tenant-retention",
                "model": "google/gemini-3.8-flash",
                "product_bar": "NOT_HAPPY",
                "oracle": "FAIL",
            },
            {
                "task_id": "afc",
                "model": "x-ai/grok-4.6",
                "product_bar": "NOT_HAPPY",
                "oracle": "FAIL",
            },
            {
                "task_id": "tenant-retention",
                "model": "openai/gpt-oss-20b",
                "product_bar": "BLOCKED",
                "oracle": None,
            },
        ]
    )
    src = tmp_path / "board.json"
    src.write_text(json.dumps(payload), encoding="utf-8")
    board = pel.load_eval2_board(src)

    heat = pel.write_heatmap_svg(board, tmp_path / pel.HEATMAP_NAME).read_text(encoding="utf-8")
    cov = pel.write_coverage_svg(board, tmp_path / pel.COVERAGE_NAME).read_text(encoding="utf-8")
    heat_text = "\n".join(_text_nodes(heat))
    cov_text = "\n".join(_text_nodes(cov))

    for expected in (
        "HAPPY",
        "oracle PASS",
        "NOT_HAPPY",
        "oracle FAIL",
        "BLOCKED",
        "unscored",
        "no data",
        "Gemini 3.8 Flash (gate)",
        "GPT-OSS 20B",
        "Grok 4.6",
        "3. AFC Population",
        "1. Tenant Retention",
    ):
        assert expected in heat_text, expected

    for expected in ("1 HAPPY / 2 scored", "0 HAPPY / 1 scored", "0 HAPPY / 0 scored"):
        assert expected in cov_text, expected

    names = {pel.HEATMAP_NAME, pel.COVERAGE_NAME, pel.COST_NAME, pel.PARTIAL_NAME}
    assert len(names) == 4
    assert all(not name.startswith("pareto-") for name in names)


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


def test_has_recorded_partial_only_for_recorded_quality(tmp_path: Path) -> None:
    payload = {
        "schema_version": 1,
        "updated": "2026-09-12",
        "gate_model": "g/a",
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
            {"openrouter_id": "g/a", "display_name": "Gate", "role": "gate"},
            {"openrouter_id": "c/b", "display_name": "Bare", "role": "catalog"},
            {"openrouter_id": "c/c", "display_name": "Fails", "role": "catalog"},
            {"openrouter_id": "c/d", "display_name": "Blocked", "role": "catalog"},
        ],
        "results": [
            {"task_id": "afc", "model": "g/a", "product_bar": "HAPPY", "oracle": "PASS"},
            {"task_id": "afc", "model": "c/b", "product_bar": "NOT_HAPPY", "oracle": "FAIL"},
            {
                "task_id": "afc",
                "model": "c/c",
                "product_bar": "NOT_HAPPY",
                "oracle": "FAIL",
                "oracle_failures": ["x"],
            },
            {"task_id": "afc", "model": "c/d", "product_bar": "BLOCKED", "oracle": None},
        ],
    }
    path = tmp_path / "partial.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    board = pel.load_eval2_board(path)
    by_model = {r.model: r for r in board.results}
    assert pel.has_recorded_partial(by_model["g/a"]) is True
    assert pel.has_recorded_partial(by_model["c/b"]) is False
    assert pel.has_recorded_partial(by_model["c/c"]) is True
    assert pel.has_recorded_partial(by_model["c/d"]) is False
