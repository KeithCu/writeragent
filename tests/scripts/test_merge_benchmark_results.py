# WriterAgent tests for scripts/prompt_optimization/merge_benchmark_results.py
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

_PO = Path(__file__).resolve().parents[2] / "scripts" / "prompt_optimization"
if str(_PO) not in sys.path:
    sys.path.insert(0, str(_PO))

import merge_benchmark_results as mbr  # noqa: E402


def _make_summary(mid: str, hard_pass: float = 0.8, correctness: float = 0.8, cost: float = 0.001) -> dict[str, Any]:
    return {
        "openrouter_id": mid,
        "display_name": mid,
        "context_window_tokens": 128000,
        "input_cost_per_million": 0.1,
        "output_cost_per_million": 0.2,
        "pricing_known": True,
        "avg_correctness": correctness,
        "avg_agent_score": correctness,
        "hard_pass_rate": hard_pass,
        "document_pass_rate": hard_pass,
        "avg_quality": 0.9,
        "n_judged": 5,
        "n_error": 0,
        "n_examples": 17,
        "avg_metric_score": correctness,
        "total_tokens": 10000,
        "total_cost_usd": cost * 17,
        "avg_cost_per_example": cost,
        "intelligence_per_dollar_correctness": (correctness**2) / cost,
        "intelligence_per_dollar_metric": (correctness**2) / cost,
    }


def test_merge_summaries_preserves_untouched_and_replaces_updated() -> None:
    base = [
        _make_summary("model/a", hard_pass=1.0),
        _make_summary("model/b", hard_pass=0.5),
        _make_summary("model/c", hard_pass=0.7),
    ]
    update = [
        _make_summary("model/b", hard_pass=0.9),
        _make_summary("model/d", hard_pass=0.85),
    ]

    merged = mbr.merge_summaries(base, update)
    by_id = {r["openrouter_id"]: r for r in merged}

    assert len(merged) == 4
    assert set(by_id.keys()) == {"model/a", "model/b", "model/c", "model/d"}
    assert by_id["model/a"]["hard_pass_rate"] == 1.0
    assert by_id["model/b"]["hard_pass_rate"] == 0.9  # replaced
    assert by_id["model/c"]["hard_pass_rate"] == 0.7  # preserved
    assert by_id["model/d"]["hard_pass_rate"] == 0.85  # added


def test_merge_summaries_drops_superseded() -> None:
    base = [
        _make_summary("inception/mercury-2", hard_pass=0.82),
        _make_summary("meta/muse-spark-1.2-contributor", hard_pass=0.88),
        _make_summary("x-ai/grok-4.6", hard_pass=1.0),
    ]
    update = [
        _make_summary("inception/mercury-2.5-preview", hard_pass=0.95),
        _make_summary("meta/muse-spark-1.3-contributor", hard_pass=0.92),
    ]

    merged = mbr.merge_summaries(
        base,
        update,
        drop_ids=["inception/mercury-2", "meta/muse-spark-1.2-contributor"],
    )
    by_id = {r["openrouter_id"]: r for r in merged}

    assert "inception/mercury-2" not in by_id
    assert "meta/muse-spark-1.2-contributor" not in by_id
    assert "x-ai/grok-4.6" in by_id
    assert "inception/mercury-2.5-preview" in by_id
    assert "meta/muse-spark-1.3-contributor" in by_id

    # Ranking order: grok (1.0), mercury-2.5 (0.95), muse-1.3 (0.92)
    assert [r["openrouter_id"] for r in merged] == [
        "x-ai/grok-4.6",
        "inception/mercury-2.5-preview",
        "meta/muse-spark-1.3-contributor",
    ]


def test_merge_details() -> None:
    base_details = [
        {"model_id": "model/a", "task_id": "t1"},
        {"model_id": "model/b", "task_id": "t1", "old": True},
        {"model_id": "model/retire", "task_id": "t1"},
    ]
    update_details = [
        {"model_id": "model/b", "task_id": "t1", "new": True},
        {"model_id": "model/c", "task_id": "t1"},
    ]

    merged = mbr.merge_details(base_details, update_details, replaced_or_dropped_ids=["model/b", "model/retire"])
    assert len(merged) == 3
    assert merged[0] == {"model_id": "model/a", "task_id": "t1"}
    assert merged[1] == {"model_id": "model/b", "task_id": "t1", "new": True}
    assert merged[2] == {"model_id": "model/c", "task_id": "t1"}


def test_format_markdown_table() -> None:
    rows = [
        _make_summary("model/top", hard_pass=1.0, correctness=0.98, cost=0.001),
        _make_summary("model/mid", hard_pass=0.8, correctness=0.85, cost=0.002),
    ]
    table = mbr.format_markdown_table(rows)
    assert "| Rank | Model | Hard pass |" in table
    assert "| 1 | model/top | 1.000 |" in table
    assert "| 2 | model/mid | 0.800 |" in table


def test_main_cli_end_to_end(tmp_path: Path) -> None:
    base_file = tmp_path / "base.json"
    base_details = tmp_path / "base_details.json"
    update_file = tmp_path / "update.json"
    update_details = tmp_path / "update_details.json"
    out_file = tmp_path / "out.json"
    out_details = tmp_path / "out_details.json"

    base_file.write_text(json.dumps([_make_summary("m1"), _make_summary("m_drop")]), encoding="utf-8")
    base_details.write_text(json.dumps([{"model_id": "m1"}, {"model_id": "m_drop"}]), encoding="utf-8")
    update_file.write_text(json.dumps([_make_summary("m2")]), encoding="utf-8")
    update_details.write_text(json.dumps([{"model_id": "m2"}]), encoding="utf-8")

    code = mbr.main([
        "-b", str(base_file),
        "-u", str(update_file),
        "-o", str(out_file),
        "-d", "m_drop",
        "--markdown",
    ])
    assert code == 0
    assert out_file.exists()
    assert out_details.exists()

    result_summaries = json.loads(out_file.read_text(encoding="utf-8"))
    ids = [r["openrouter_id"] for r in result_summaries]
    assert "m_drop" not in ids
    assert "m1" in ids
    assert "m2" in ids

    result_details = json.loads(out_details.read_text(encoding="utf-8"))
    detail_ids = [d["model_id"] for d in result_details]
    assert "m_drop" not in detail_ids
    assert "m1" in detail_ids
    assert "m2" in detail_ids


def test_format_readme_value_table() -> None:
    rows = [
        _make_summary("model/low_val", correctness=0.6, cost=0.01),  # val = 0.36 / 0.01 = 36
        _make_summary("model/high_val", correctness=0.9, cost=0.001),  # val = 0.81 / 0.001 = 810
    ]
    tbl = mbr.format_readme_value_table(rows)
    assert "| Model | Correctness<br>avg task score (0–1) | Value<br>Correctness² ÷ $/task |" in tbl
    lines = tbl.splitlines()
    assert "model/high_val" in lines[2]
    assert "model/low_val" in lines[3]


def test_update_doc_tables(tmp_path: Path) -> None:
    # Set up mock files
    bench_dir = tmp_path / "docs" / "eval"
    bench_dir.mkdir(parents=True)
    bench_file = bench_dir / "benchmarks.md"
    bench_file.write_text(
        "# Heading\n\n| Rank | Model | Hard pass |\n| ---- | ----- | --------- |\n| 1 | old | 0.5 |\n\nFooter",
        encoding="utf-8",
    )

    po_dir = tmp_path / "scripts" / "prompt_optimization"
    po_dir.mkdir(parents=True)
    po_readme = po_dir / "README.md"
    po_readme.write_text(
        "# PO\n\n| Rank | Model | Hard pass |\n| ---- | ----- | --------- |\n| 1 | old | 0.5 |\n\nFooter",
        encoding="utf-8",
    )

    root_readme = tmp_path / "README.md"
    root_readme.write_text(
        "# Root\n\n| Model | Correctness<br>avg task score (0–1) | Value<br>Correctness² ÷ $/task |\n| ----- | ----- | ----- |\n| old | 0.5 | 10 |\n\nFooter",
        encoding="utf-8",
    )

    rows = [_make_summary("new/model", hard_pass=0.99, correctness=0.99, cost=0.001)]
    updated = mbr.update_doc_tables(rows, tmp_path)
    assert len(updated) == 3

    assert "new/model" in bench_file.read_text(encoding="utf-8")
    assert "new/model" in po_readme.read_text(encoding="utf-8")
    assert "new/model" in root_readme.read_text(encoding="utf-8")

