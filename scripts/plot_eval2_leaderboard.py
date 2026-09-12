#!/usr/bin/env python3
# WriterAgent - eval-2 headed leaderboard plots
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Write eval-2 headed task×model SVGs from a results JSON.

Separate from the 17-task string-harness Pareto (`plot_pareto.py` /
``docs/eval/pareto-*.svg``). Reads only the eval-2 headed results file —
no OpenRouter, no ``benchmark_results.json`` merge.

Heatmap / coverage stay HAPPY / NOT_HAPPY. Optional per-result cost
and oracle-partial fields rank comparable cells (HAPPY first, then
higher partial, then lower cost). Missing cost or partial stays empty.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RESULTS = REPO_ROOT / "docs" / "eval" / "eval-2" / "eval2_benchmark_results.json"
DEFAULT_OUT_DIR = REPO_ROOT / "docs" / "eval" / "eval-2"

SCHEMA_VERSION = 1
PRODUCT_BARS = frozenset({"HAPPY", "NOT_HAPPY"})
ORACLES = frozenset({"PASS", "FAIL"})
TASK_STATUSES = frozenset({"Ready", "Headed-ready"})
MODEL_ROLES = frozenset({"gate", "headed", "catalog"})
PARKED_SLOT = 7

# Okabe–Ito (same family as plot_pareto.py) plus an honest empty fill.
COLOR_HAPPY = "#009E73"
COLOR_NOT_HAPPY = "#D55E00"
COLOR_EMPTY = "#F1F3F4"
COLOR_INK = "#202124"
COLOR_MUTED = "#5f6368"
COLOR_LINE = "#dadce0"

HEATMAP_NAME = "eval2-heatmap.svg"
COVERAGE_NAME = "eval2-coverage.svg"
COST_NAME = "eval2-cost.svg"
PARTIAL_NAME = "eval2-partial.svg"

FOOTNOTE = (
    "Source: headed eval-2 autopsy notes, not a catalog sweep. "
    "Empty = no in-repo stamp. Metrics are product HAPPY / oracle PASS|FAIL "
    "(not string-harness hard_pass_rate)."
)


class Eval2ResultsError(ValueError):
    """Results JSON failed the headed scoreboard schema."""


@dataclass(frozen=True)
class Eval2Task:
    id: str
    slot: int
    slug: str
    title: str
    status: str


@dataclass(frozen=True)
class Eval2Model:
    openrouter_id: str
    display_name: str
    role: str


@dataclass(frozen=True)
class Eval2Result:
    task_id: str
    model: str
    product_bar: str | None
    oracle: str | None
    oracle_note: str
    stamp: str
    source: str
    run_artifacts_committed: bool
    patches: str | None
    total_tokens: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_cost_usd: float | None = None
    wall_time_s: float | None = None
    intelligence_per_dollar: float | None = None
    oracle_passed: bool | None = None
    oracle_failure_count: int | None = None
    oracle_check_count: int | None = None
    oracle_failures: tuple[str, ...] | None = None
    afc_s_flags: int | None = None
    afc_r_required: int | None = None
    husk_cells: int | None = None
    scored_cells: int | None = None
    partial_score: float | None = None


@dataclass(frozen=True)
class HappyCostRow:
    """One HAPPY cell that recorded a positive USD cost (no invented numbers)."""

    task: Eval2Task
    model: Eval2Model
    result: Eval2Result
    cost_usd: float
    intelligence_per_dollar: float


@dataclass(frozen=True)
class RankedRow:
    """One scored cell in HAPPY → partial → cost order (missing metrics sort last)."""

    task: Eval2Task
    model: Eval2Model
    result: Eval2Result
    partial_score: float | None
    cost_usd: float | None


@dataclass(frozen=True)
class Eval2Board:
    schema_version: int
    updated: str
    gate_model: str
    source_of_truth: str
    notes: str
    tasks: tuple[Eval2Task, ...]
    models: tuple[Eval2Model, ...]
    results: tuple[Eval2Result, ...]

    def result_for(self, task_id: str, model: str) -> Eval2Result | None:
        for row in self.results:
            if row.task_id == task_id and row.model == model:
                return row
        return None

    def scored_count(self, model: str) -> int:
        return sum(1 for row in self.results if row.model == model and row.product_bar)

    def happy_count(self, model: str) -> int:
        return sum(1 for row in self.results if row.model == model and row.product_bar == "HAPPY")

    def not_happy_count(self, model: str) -> int:
        return sum(1 for row in self.results if row.model == model and row.product_bar == "NOT_HAPPY")


def _require_str(row: Mapping[str, Any], key: str, *, ctx: str) -> str:
    value = row.get(key)
    if not isinstance(value, str) or not value.strip():
        raise Eval2ResultsError(f"{ctx}: {key!r} must be a non-empty string")
    return value


def _optional_str(row: Mapping[str, Any], key: str) -> str:
    value = row.get(key)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise Eval2ResultsError(f"{key!r} must be a string or null")
    return value


def _optional_nonneg_int(row: Mapping[str, Any], key: str) -> int | None:
    if key not in row or row[key] is None:
        return None
    value = row[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise Eval2ResultsError(f"{key!r} must be a non-negative int or null")
    if value < 0:
        raise Eval2ResultsError(f"{key!r} must be >= 0")
    return value


def _optional_nonneg_float(row: Mapping[str, Any], key: str) -> float | None:
    if key not in row or row[key] is None:
        return None
    value = row[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise Eval2ResultsError(f"{key!r} must be a non-negative number or null")
    if value < 0:
        raise Eval2ResultsError(f"{key!r} must be >= 0")
    return float(value)


def _optional_bool(row: Mapping[str, Any], key: str) -> bool | None:
    if key not in row or row[key] is None:
        return None
    value = row[key]
    if not isinstance(value, bool):
        raise Eval2ResultsError(f"{key!r} must be a boolean or null")
    return value


def _optional_str_list(row: Mapping[str, Any], key: str) -> tuple[str, ...] | None:
    if key not in row or row[key] is None:
        return None
    value = row[key]
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise Eval2ResultsError(f"{key!r} must be an array of strings or null")
    return tuple(value)


def _optional_positive_int(row: Mapping[str, Any], key: str) -> int | None:
    value = _optional_nonneg_int(row, key)
    if value == 0:
        raise Eval2ResultsError(f"{key!r} must be >= 1")
    return value


def _optional_unit_interval(row: Mapping[str, Any], key: str) -> float | None:
    value = _optional_nonneg_float(row, key)
    if value is not None and value > 1.0:
        raise Eval2ResultsError(f"{key!r} must be between 0 and 1")
    return value


def compute_intelligence_per_dollar(
    product_bar: str | None, total_cost_usd: float | None
) -> float | None:
    """HAPPY successes per USD. None when not HAPPY or cost is missing/zero.

    Distinct from string-harness C²/$ (correctness² / avg $/task). Eval-2
    product bar is binary, so this is ``1 / total_cost_usd``.
    """
    if product_bar != "HAPPY" or total_cost_usd is None or total_cost_usd <= 0:
        return None
    return 1.0 / total_cost_usd


def compute_partial_score(
    oracle_passed: bool | None,
    oracle_failure_count: int | None,
    oracle_check_count: int | None,
) -> float | None:
    """Oracle quality in [0, 1]. None when the ratio cannot be formed honestly.

    ``1`` when the fail-closed oracle passed. Otherwise
    ``1 - failure_count / check_count`` only when both counts were recorded
    and ``check_count > 0``. Do not invent a denominator from failure text.
    """
    if oracle_passed is True:
        return 1.0
    if (
        oracle_failure_count is None
        or oracle_check_count is None
        or oracle_check_count <= 0
    ):
        return None
    return max(0.0, min(1.0, 1.0 - (oracle_failure_count / oracle_check_count)))


def resolve_oracle_passed(oracle: str | None, stored: bool | None) -> bool | None:
    if stored is not None:
        return stored
    if oracle == "PASS":
        return True
    if oracle == "FAIL":
        return False
    return None


def _enum_or_none(value: object, allowed: frozenset[str], *, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or value not in allowed:
        raise Eval2ResultsError(f"{field} must be one of {sorted(allowed)} or null, got {value!r}")
    return value


def _parse_task(raw: object, *, seen_ids: set[str], seen_slots: set[int]) -> Eval2Task:
    if not isinstance(raw, Mapping):
        raise Eval2ResultsError("each task must be an object")
    task_id = _require_str(raw, "id", ctx="task")
    if task_id in seen_ids:
        raise Eval2ResultsError(f"duplicate task id {task_id!r}")
    seen_ids.add(task_id)
    slot = raw.get("slot")
    if not isinstance(slot, int) or isinstance(slot, bool):
        raise Eval2ResultsError(f"task {task_id!r}: slot must be an int")
    if slot == PARKED_SLOT:
        raise Eval2ResultsError("slot 7 is PARKED and must not appear on the scoreboard")
    if slot in seen_slots:
        raise Eval2ResultsError(f"duplicate slot {slot}")
    seen_slots.add(slot)
    status = _require_str(raw, "status", ctx=f"task {task_id}")
    if status not in TASK_STATUSES:
        raise Eval2ResultsError(f"task {task_id!r}: status {status!r} is not Ready/Headed-ready")
    return Eval2Task(
        id=task_id,
        slot=slot,
        slug=_require_str(raw, "slug", ctx=f"task {task_id}"),
        title=_require_str(raw, "title", ctx=f"task {task_id}"),
        status=status,
    )


def _parse_model(raw: object, *, seen: set[str]) -> Eval2Model:
    if not isinstance(raw, Mapping):
        raise Eval2ResultsError("each model must be an object")
    mid = _require_str(raw, "openrouter_id", ctx="model")
    if mid in seen:
        raise Eval2ResultsError(f"duplicate model {mid!r}")
    seen.add(mid)
    role = _require_str(raw, "role", ctx=f"model {mid}")
    if role not in MODEL_ROLES:
        raise Eval2ResultsError(f"model {mid!r}: role {role!r} is not gate/headed/catalog")
    return Eval2Model(
        openrouter_id=mid,
        display_name=_require_str(raw, "display_name", ctx=f"model {mid}"),
        role=role,
    )


def _parse_result(
    raw: object,
    *,
    task_ids: set[str],
    model_ids: set[str],
    seen_pairs: set[tuple[str, str]],
) -> Eval2Result:
    if not isinstance(raw, Mapping):
        raise Eval2ResultsError("each result must be an object")
    task_id = _require_str(raw, "task_id", ctx="result")
    model = _require_str(raw, "model", ctx="result")
    if task_id not in task_ids:
        raise Eval2ResultsError(f"result task_id {task_id!r} is not in tasks")
    if model not in model_ids:
        raise Eval2ResultsError(f"result model {model!r} is not in models")
    pair = (task_id, model)
    if pair in seen_pairs:
        raise Eval2ResultsError(f"duplicate result for {task_id} × {model}")
    seen_pairs.add(pair)
    product_bar = _enum_or_none(raw.get("product_bar"), PRODUCT_BARS, field="product_bar")
    oracle = _enum_or_none(raw.get("oracle"), ORACLES, field="oracle")
    if product_bar is None and oracle is None:
        raise Eval2ResultsError(
            f"{task_id} × {model}: omit empty cells instead of storing a null/null result"
        )
    total_cost_usd = _optional_nonneg_float(raw, "total_cost_usd")
    stored_ipd = _optional_nonneg_float(raw, "intelligence_per_dollar")
    oracle_failures = _optional_str_list(raw, "oracle_failures")
    stored_fail_count = _optional_nonneg_int(raw, "oracle_failure_count")
    if stored_fail_count is not None:
        oracle_failure_count = stored_fail_count
    elif oracle_failures is not None:
        oracle_failure_count = len(oracle_failures)
    else:
        oracle_failure_count = None
    oracle_check_count = _optional_positive_int(raw, "oracle_check_count")
    oracle_passed = resolve_oracle_passed(oracle, _optional_bool(raw, "oracle_passed"))
    stored_partial = _optional_unit_interval(raw, "partial_score")
    return Eval2Result(
        task_id=task_id,
        model=model,
        product_bar=product_bar,
        oracle=oracle,
        oracle_note=_optional_str(raw, "oracle_note"),
        stamp=_optional_str(raw, "stamp"),
        source=_optional_str(raw, "source"),
        run_artifacts_committed=bool(raw.get("run_artifacts_committed", False)),
        patches=raw.get("patches") if isinstance(raw.get("patches"), str) else None,
        total_tokens=_optional_nonneg_int(raw, "total_tokens"),
        input_tokens=_optional_nonneg_int(raw, "input_tokens"),
        output_tokens=_optional_nonneg_int(raw, "output_tokens"),
        total_cost_usd=total_cost_usd,
        wall_time_s=_optional_nonneg_float(raw, "wall_time_s"),
        intelligence_per_dollar=stored_ipd
        if stored_ipd is not None
        else compute_intelligence_per_dollar(product_bar, total_cost_usd),
        oracle_passed=oracle_passed,
        oracle_failure_count=oracle_failure_count,
        oracle_check_count=oracle_check_count,
        oracle_failures=oracle_failures,
        afc_s_flags=_optional_nonneg_int(raw, "afc_s_flags"),
        afc_r_required=_optional_nonneg_int(raw, "afc_r_required"),
        husk_cells=_optional_nonneg_int(raw, "husk_cells"),
        scored_cells=_optional_nonneg_int(raw, "scored_cells"),
        partial_score=stored_partial
        if stored_partial is not None
        else compute_partial_score(oracle_passed, oracle_failure_count, oracle_check_count),
    )


def load_eval2_board(path: Path) -> Eval2Board:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise Eval2ResultsError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise Eval2ResultsError(f"{path} must be a JSON object")
    version = payload.get("schema_version")
    if version != SCHEMA_VERSION:
        raise Eval2ResultsError(f"schema_version must be {SCHEMA_VERSION}, got {version!r}")
    raw_tasks = payload.get("tasks")
    raw_models = payload.get("models")
    raw_results = payload.get("results")
    if not isinstance(raw_tasks, list) or not raw_tasks:
        raise Eval2ResultsError("tasks must be a non-empty array")
    if not isinstance(raw_models, list) or not raw_models:
        raise Eval2ResultsError("models must be a non-empty array")
    if not isinstance(raw_results, list):
        raise Eval2ResultsError("results must be an array (use [] when nothing is scored)")
    seen_ids: set[str] = set()
    seen_slots: set[int] = set()
    tasks = tuple(_parse_task(item, seen_ids=seen_ids, seen_slots=seen_slots) for item in raw_tasks)
    seen_models: set[str] = set()
    models = tuple(_parse_model(item, seen=seen_models) for item in raw_models)
    gate_model = _require_str(payload, "gate_model", ctx="board")
    if gate_model not in seen_models:
        raise Eval2ResultsError(f"gate_model {gate_model!r} must appear in models")
    seen_pairs: set[tuple[str, str]] = set()
    results = tuple(
        _parse_result(item, task_ids=seen_ids, model_ids=seen_models, seen_pairs=seen_pairs)
        for item in raw_results
    )
    return Eval2Board(
        schema_version=SCHEMA_VERSION,
        updated=_require_str(payload, "updated", ctx="board"),
        gate_model=gate_model,
        source_of_truth=_require_str(payload, "source_of_truth", ctx="board"),
        notes=_optional_str(payload, "notes"),
        tasks=tasks,
        models=models,
        results=results,
    )


def cell_label(row: Eval2Result | None) -> str:
    """Short matrix token: HAPPY / oracle FAIL, or an em dash when unscored."""
    if row is None or (row.product_bar is None and row.oracle is None):
        return "—"
    bits: list[str] = []
    if row.product_bar:
        bits.append(row.product_bar)
    if row.oracle:
        bits.append(f"oracle {row.oracle}")
    return " / ".join(bits)


def render_matrix_markdown(board: Eval2Board) -> str:
    headers = ["#", "Task", *(model.display_name for model in board.models)]
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join(["---"] * len(headers)) + " |",
    ]
    for task in board.tasks:
        cells = [str(task.slot), task.title]
        for model in board.models:
            cells.append(cell_label(board.result_for(task.id, model.openrouter_id)))
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"


def happy_cost_rows(board: Eval2Board) -> tuple[HappyCostRow, ...]:
    """HAPPY cells with recorded cost > 0, cheapest success first within a task."""
    task_by_id = {task.id: task for task in board.tasks}
    model_by_id = {model.openrouter_id: model for model in board.models}
    rows: list[HappyCostRow] = []
    for result in board.results:
        if result.product_bar != "HAPPY":
            continue
        cost = result.total_cost_usd
        if cost is None or cost <= 0:
            continue
        ipd = result.intelligence_per_dollar
        if ipd is None:
            ipd = compute_intelligence_per_dollar(result.product_bar, cost)
        if ipd is None:
            continue
        rows.append(
            HappyCostRow(
                task=task_by_id[result.task_id],
                model=model_by_id[result.model],
                result=result,
                cost_usd=cost,
                intelligence_per_dollar=ipd,
            )
        )
    # Among HAPPY+cost: higher recorded partial first, then cheaper.
    rows.sort(
        key=lambda row: (
            row.task.slot,
            *_partial_sort_tuple(row.result.partial_score),
            row.cost_usd,
            row.model.openrouter_id,
        )
    )
    return tuple(rows)


def render_cost_markdown(board: Eval2Board) -> str:
    """Per-task HAPPY cost ranking. Empty when no stamp recorded USD."""
    rows = happy_cost_rows(board)
    if not rows:
        return "No HAPPY cell has recorded `total_cost_usd` — cost chart stays empty.\n"
    lines = [
        "| # | Task | Model | Cost (USD) | Tokens | Wall (s) | Successes/$ |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in rows:
        tokens = "—" if row.result.total_tokens is None else str(row.result.total_tokens)
        wall = "—" if row.result.wall_time_s is None else f"{row.result.wall_time_s:g}"
        lines.append(
            f"| {row.task.slot} | {row.task.title} | {row.model.display_name} | "
            f"{row.cost_usd:.4f} | {tokens} | {wall} | "
            f"{row.intelligence_per_dollar:.2f} |"
        )
    return "\n".join(lines) + "\n"


def _partial_sort_tuple(score: float | None) -> tuple[int, float]:
    """Missing partial sorts last; recorded scores sort high-to-low."""
    if score is None:
        return (1, 0.0)
    return (0, -score)


def _cost_sort_tuple(cost: float | None) -> tuple[int, float]:
    if cost is None:
        return (1, 0.0)
    return (0, cost)


def has_recorded_partial(result: Eval2Result) -> bool:
    """True when a stamp recorded oracle quality — not merely HAPPY/NOT."""
    return any(
        value is not None
        for value in (
            result.partial_score,
            result.oracle_failure_count,
            result.oracle_check_count,
            result.afc_s_flags,
            result.afc_r_required,
            result.husk_cells,
            result.scored_cells,
        )
    ) or result.oracle_failures is not None


def rank_sort_key(row: RankedRow) -> tuple[object, ...]:
    """HAPPY first, then higher partial, then lower cost; missing metrics last."""
    if row.result.product_bar == "HAPPY":
        bar = 0
    elif row.result.product_bar == "NOT_HAPPY":
        bar = 1
    else:
        bar = 2
    return (
        row.task.slot,
        bar,
        *_partial_sort_tuple(row.partial_score),
        *_cost_sort_tuple(row.cost_usd),
        row.model.openrouter_id,
    )


def ranked_rows(board: Eval2Board) -> tuple[RankedRow, ...]:
    task_by_id = {task.id: task for task in board.tasks}
    model_by_id = {model.openrouter_id: model for model in board.models}
    rows = [
        RankedRow(
            task=task_by_id[result.task_id],
            model=model_by_id[result.model],
            result=result,
            partial_score=result.partial_score,
            cost_usd=result.total_cost_usd if result.total_cost_usd else None,
        )
        for result in board.results
    ]
    rows.sort(key=rank_sort_key)
    return tuple(rows)


def _format_partial(score: float | None) -> str:
    if score is None:
        return "—"
    return f"{score:.2f}"


def _format_fails_checks(result: Eval2Result) -> str:
    fails = result.oracle_failure_count
    checks = result.oracle_check_count
    if fails is None and checks is None:
        return "—"
    fail_s = "—" if fails is None else str(fails)
    check_s = "—" if checks is None else str(checks)
    return f"{fail_s}/{check_s}"


def _format_afc_sr(result: Eval2Result) -> str:
    if result.afc_s_flags is None and result.afc_r_required is None:
        return "—"
    s_text = "—" if result.afc_s_flags is None else str(result.afc_s_flags)
    r_text = "—" if result.afc_r_required is None else str(result.afc_r_required)
    return f"S={s_text} R={r_text}"


def render_partial_markdown(board: Eval2Board) -> str:
    """HAPPY → partial → cost ranking. Empty extras stay em-dash — no invented ratios."""
    rows = ranked_rows(board)
    if not rows:
        return "No scored cells.\n"
    if not any(has_recorded_partial(row.result) for row in rows):
        return (
            "No cell has recorded oracle partial (failures/checks or AFC S/R) — "
            "partial chart stays empty of ratios except oracle PASS → 1.\n"
        )
    lines = [
        "| # | Task | Model | Bar | Partial | Fails/checks | AFC S/R | Cost |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in rows:
        if not has_recorded_partial(row.result) and row.cost_usd is None:
            continue
        cost = "—" if row.cost_usd is None else f"{row.cost_usd:.4f}"
        bar = row.result.product_bar or "—"
        lines.append(
            f"| {row.task.slot} | {row.task.title} | {row.model.display_name} | "
            f"{bar} | {_format_partial(row.partial_score)} | "
            f"{_format_fails_checks(row.result)} | {_format_afc_sr(row.result)} | "
            f"{cost} |"
        )
    return "\n".join(lines) + "\n"


def _esc(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _svg_wrap(body: str, *, width: int, height: int, title: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img">\n'
        f"  <title>{_esc(title)}</title>\n"
        f'  <rect width="{width}" height="{height}" fill="#ffffff"/>\n'
        f"{body}</svg>\n"
    )


def write_heatmap_svg(board: Eval2Board, out_path: Path) -> Path:
    label_w = 210
    head_h = 72
    cell_w = 150
    cell_h = 58
    left = 24
    top = 56
    n_models = len(board.models)
    n_tasks = len(board.tasks)
    width = left + label_w + n_models * cell_w + 24
    height = top + head_h + n_tasks * cell_h + 64
    parts: list[str] = [
        f'  <text x="{left}" y="28" font-family="DejaVu Sans, sans-serif" '
        f'font-size="16" font-weight="700" fill="{COLOR_INK}">'
        f"Eval-2 headed scoreboard (product bar × oracle)</text>\n",
        f'  <text x="{left}" y="46" font-family="DejaVu Sans, sans-serif" '
        f'font-size="11" fill="{COLOR_MUTED}">'
        f"Gate {_esc(board.gate_model)} · updated {_esc(board.updated)} · "
        f"empty cells have no headed stamp</text>\n",
    ]
    for col, model in enumerate(board.models):
        x = left + label_w + col * cell_w + cell_w / 2
        suffix = " (gate)" if model.role == "gate" else ""
        parts.append(
            f'  <text x="{x:.1f}" y="{top + 28}" text-anchor="middle" '
            f'font-family="DejaVu Sans, sans-serif" font-size="11" font-weight="700" '
            f'fill="{COLOR_INK}">{_esc(model.display_name + suffix)}</text>\n'
        )
        parts.append(
            f'  <text x="{x:.1f}" y="{top + 44}" text-anchor="middle" '
            f'font-family="DejaVu Sans, sans-serif" font-size="9" fill="{COLOR_MUTED}">'
            f"{_esc(model.openrouter_id)}</text>\n"
        )
    for row_i, task in enumerate(board.tasks):
        y = top + head_h + row_i * cell_h
        parts.append(
            f'  <text x="{left + label_w - 10}" y="{y + 26}" text-anchor="end" '
            f'font-family="DejaVu Sans, sans-serif" font-size="12" fill="{COLOR_INK}">'
            f"{task.slot}. {_esc(task.title)}</text>\n"
        )
        parts.append(
            f'  <text x="{left + label_w - 10}" y="{y + 42}" text-anchor="end" '
            f'font-family="DejaVu Sans, sans-serif" font-size="9" fill="{COLOR_MUTED}">'
            f"{_esc(task.slug)}</text>\n"
        )
        for col, model in enumerate(board.models):
            x = left + label_w + col * cell_w
            row = board.result_for(task.id, model.openrouter_id)
            if row is None or row.product_bar is None:
                fill = COLOR_EMPTY
                bar_text = "no data"
                oracle_text = "—"
                ink = COLOR_MUTED
            else:
                fill = COLOR_HAPPY if row.product_bar == "HAPPY" else COLOR_NOT_HAPPY
                bar_text = row.product_bar
                ink = "#ffffff"
                if row.oracle is None:
                    oracle_text = "oracle unknown"
                else:
                    oracle_text = f"oracle {row.oracle}"
            parts.append(
                f'  <rect x="{x + 4:.1f}" y="{y + 6:.1f}" width="{cell_w - 8}" '
                f'height="{cell_h - 12}" rx="6" fill="{fill}" stroke="{COLOR_LINE}"/>\n'
            )
            parts.append(
                f'  <text x="{x + cell_w / 2:.1f}" y="{y + 26:.1f}" text-anchor="middle" '
                f'font-family="DejaVu Sans, sans-serif" font-size="11" font-weight="700" '
                f'fill="{ink}">{_esc(bar_text)}</text>\n'
            )
            parts.append(
                f'  <text x="{x + cell_w / 2:.1f}" y="{y + 42:.1f}" text-anchor="middle" '
                f'font-family="DejaVu Sans, sans-serif" font-size="10" '
                f'fill="{ink}">{_esc(oracle_text)}</text>\n'
            )
    parts.append(
        f'  <text x="{left}" y="{height - 18}" font-family="DejaVu Sans, sans-serif" '
        f'font-size="10" fill="{COLOR_MUTED}">{_esc(FOOTNOTE)}</text>\n'
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        _svg_wrap(
            "".join(parts),
            width=width,
            height=height,
            title="Eval-2 headed task × model heatmap",
        ),
        encoding="utf-8",
    )
    return out_path


def write_coverage_svg(board: Eval2Board, out_path: Path) -> Path:
    """Stacked HAPPY / NOT_HAPPY / no-data counts — honest when most cells are empty."""
    n_tasks = len(board.tasks)
    left = 56
    top = 64
    bar_w = 88
    gap = 36
    plot_h = 260
    width = max(640, left + len(board.models) * (bar_w + gap) + 40)
    height = top + plot_h + 90
    parts: list[str] = [
        f'  <text x="24" y="28" font-family="DejaVu Sans, sans-serif" '
        f'font-size="16" font-weight="700" fill="{COLOR_INK}">'
        f"Eval-2 headed coverage (Ready tasks scored)</text>\n",
        f'  <text x="24" y="46" font-family="DejaVu Sans, sans-serif" '
        f'font-size="11" fill="{COLOR_MUTED}">'
        f"{n_tasks} Ready / Headed-ready rows. Catalog peers stay gray until a stamp lands."
        f"</text>\n",
    ]
    for col, model in enumerate(board.models):
        x = left + col * (bar_w + gap)
        happy = board.happy_count(model.openrouter_id)
        not_happy = board.not_happy_count(model.openrouter_id)
        empty = n_tasks - happy - not_happy
        # Stack from the axis: empty, then NOT_HAPPY, then HAPPY on top.
        scale = plot_h / n_tasks
        y = top + plot_h
        for count, fill, label in (
            (empty, COLOR_EMPTY, "no data"),
            (not_happy, COLOR_NOT_HAPPY, "NOT_HAPPY"),
            (happy, COLOR_HAPPY, "HAPPY"),
        ):
            if count <= 0:
                continue
            h = count * scale
            y -= h
            parts.append(
                f'  <rect x="{x:.1f}" y="{y:.1f}" width="{bar_w}" height="{h:.1f}" '
                f'fill="{fill}" stroke="{COLOR_LINE}" data-segment="{_esc(label)}"/>\n'
            )
        parts.append(
            f'  <text x="{x + bar_w / 2:.1f}" y="{top + plot_h + 20}" text-anchor="middle" '
            f'font-family="DejaVu Sans, sans-serif" font-size="11" font-weight="700" '
            f'fill="{COLOR_INK}">{_esc(model.display_name)}</text>\n'
        )
        scored = happy + not_happy
        parts.append(
            f'  <text x="{x + bar_w / 2:.1f}" y="{top + plot_h + 36}" text-anchor="middle" '
            f'font-family="DejaVu Sans, sans-serif" font-size="10" fill="{COLOR_MUTED}">'
            f"{happy} HAPPY / {scored} scored</text>\n"
        )
    # Legend
    legend_y = height - 28
    for i, (fill, label) in enumerate(
        ((COLOR_HAPPY, "HAPPY"), (COLOR_NOT_HAPPY, "NOT_HAPPY"), (COLOR_EMPTY, "no data"))
    ):
        lx = 24 + i * 110
        parts.append(
            f'  <rect x="{lx}" y="{legend_y - 10}" width="12" height="12" fill="{fill}" '
            f'stroke="{COLOR_LINE}"/>\n'
        )
        parts.append(
            f'  <text x="{lx + 18}" y="{legend_y}" font-family="DejaVu Sans, sans-serif" '
            f'font-size="11" fill="{COLOR_INK}">{label}</text>\n'
        )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        _svg_wrap(
            "".join(parts),
            width=width,
            height=height,
            title="Eval-2 headed coverage by model",
        ),
        encoding="utf-8",
    )
    return out_path


def write_cost_svg(board: Eval2Board, out_path: Path) -> Path:
    """Bar of recorded USD for HAPPY cells. Missing cost stays empty — no invented bars."""
    rows = happy_cost_rows(board)
    left = 24
    top = 64
    width = 820
    if not rows:
        height = 140
        parts = [
            f'  <text x="{left}" y="28" font-family="DejaVu Sans, sans-serif" '
            f'font-size="16" font-weight="700" fill="{COLOR_INK}">'
            f"Eval-2 headed cost for results (HAPPY cells)</text>\n",
            f'  <text x="{left}" y="46" font-family="DejaVu Sans, sans-serif" '
            f'font-size="11" fill="{COLOR_MUTED}">'
            f"HAPPY first; among HAPPY, lower recorded USD ranks higher. "
            f"Successes/$ = 1 / total_cost_usd (not string-harness C²/$).</text>\n",
            f'  <rect x="{left}" y="64" width="{width - 48}" height="40" rx="6" '
            f'fill="{COLOR_EMPTY}" stroke="{COLOR_LINE}"/>\n',
            f'  <text x="{width / 2:.1f}" y="89" text-anchor="middle" '
            f'font-family="DejaVu Sans, sans-serif" font-size="12" fill="{COLOR_MUTED}">'
            f"No HAPPY cell has recorded total_cost_usd yet</text>\n",
        ]
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            _svg_wrap(
                "".join(parts),
                width=width,
                height=height,
                title="Eval-2 headed cost for results (empty — no recorded USD)",
            ),
            encoding="utf-8",
        )
        return out_path

    label_w = 280
    bar_max_w = 360
    row_h = 28
    group_gap = 18
    max_cost = max(row.cost_usd for row in rows)
    # Group by task so AFC (and later tasks) rank among their HAPPY models.
    groups: list[tuple[Eval2Task, list[HappyCostRow]]] = []
    for row in rows:
        if not groups or groups[-1][0].id != row.task.id:
            groups.append((row.task, [row]))
        else:
            groups[-1][1].append(row)
    n_bars = len(rows)
    height = top + n_bars * row_h + len(groups) * group_gap + 56
    parts = [
        f'  <text x="{left}" y="28" font-family="DejaVu Sans, sans-serif" '
        f'font-size="16" font-weight="700" fill="{COLOR_INK}">'
        f"Eval-2 headed cost for results (HAPPY cells)</text>\n",
        f'  <text x="{left}" y="46" font-family="DejaVu Sans, sans-serif" '
        f'font-size="11" fill="{COLOR_MUTED}">'
        f"HAPPY first; among HAPPY, lower recorded USD ranks higher. "
        f"Successes/$ = 1 / total_cost_usd. NOT_HAPPY and missing cost omitted.</text>\n",
    ]
    y = top
    for task, group in groups:
        parts.append(
            f'  <text x="{left}" y="{y}" font-family="DejaVu Sans, sans-serif" '
            f'font-size="12" font-weight="700" fill="{COLOR_INK}">'
            f"{task.slot}. {_esc(task.title)}</text>\n"
        )
        y += 8
        for row in group:
            y += row_h
            bar_w = (row.cost_usd / max_cost) * bar_max_w if max_cost > 0 else 0.0
            x = left + label_w
            parts.append(
                f'  <text x="{x - 8:.1f}" y="{y - 6:.1f}" text-anchor="end" '
                f'font-family="DejaVu Sans, sans-serif" font-size="11" fill="{COLOR_INK}">'
                f"{_esc(row.model.display_name)}</text>\n"
            )
            parts.append(
                f'  <rect x="{x:.1f}" y="{y - 18:.1f}" width="{bar_w:.1f}" height="16" '
                f'rx="3" fill="{COLOR_HAPPY}" stroke="{COLOR_LINE}" '
                f'data-cost-usd="{row.cost_usd:.6f}"/>\n'
            )
            parts.append(
                f'  <text x="{x + bar_w + 8:.1f}" y="{y - 6:.1f}" '
                f'font-family="DejaVu Sans, sans-serif" font-size="10" fill="{COLOR_MUTED}">'
                f"${row.cost_usd:.4f} · {row.intelligence_per_dollar:.2f} succ/$</text>\n"
            )
        y += group_gap
    parts.append(
        f'  <text x="{left}" y="{height - 16}" font-family="DejaVu Sans, sans-serif" '
        f'font-size="10" fill="{COLOR_MUTED}">'
        f"Recorded USD only. Empty cost stays empty — do not invent run costs.</text>\n"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        _svg_wrap(
            "".join(parts),
            width=width,
            height=height,
            title="Eval-2 headed cost for results (HAPPY cells with recorded USD)",
        ),
        encoding="utf-8",
    )
    return out_path


def write_partial_svg(board: Eval2Board, out_path: Path) -> Path:
    """Coarse HAPPY/partial/cost view. No bar unless a ratio or oracle PASS exists."""
    rows = [row for row in ranked_rows(board) if has_recorded_partial(row.result)]
    left = 24
    width = 860
    if not rows:
        height = 140
        parts = [
            f'  <text x="{left}" y="28" font-family="DejaVu Sans, sans-serif" '
            f'font-size="16" font-weight="700" fill="{COLOR_INK}">'
            f"Eval-2 headed partial / cost (recorded oracle quality)</text>\n",
            f'  <text x="{left}" y="46" font-family="DejaVu Sans, sans-serif" '
            f'font-size="11" fill="{COLOR_MUTED}">'
            f"HAPPY first, then higher 1 − fails/checks, then lower USD. "
            f"No invented denominators.</text>\n",
            f'  <rect x="{left}" y="64" width="{width - 48}" height="40" rx="6" '
            f'fill="{COLOR_EMPTY}" stroke="{COLOR_LINE}"/>\n',
            f'  <text x="{width / 2:.1f}" y="89" text-anchor="middle" '
            f'font-family="DejaVu Sans, sans-serif" font-size="12" fill="{COLOR_MUTED}">'
            f"No cell has recorded oracle partial yet</text>\n",
        ]
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            _svg_wrap(
                "".join(parts),
                width=width,
                height=height,
                title="Eval-2 headed partial / cost (empty — no recorded oracle quality)",
            ),
            encoding="utf-8",
        )
        return out_path

    label_w = 280
    bar_max_w = 280
    row_h = 28
    group_gap = 18
    top = 64
    groups: list[tuple[Eval2Task, list[RankedRow]]] = []
    for row in rows:
        if not groups or groups[-1][0].id != row.task.id:
            groups.append((row.task, [row]))
        else:
            groups[-1][1].append(row)
    height = top + len(rows) * row_h + len(groups) * group_gap + 56
    parts = [
        f'  <text x="{left}" y="28" font-family="DejaVu Sans, sans-serif" '
        f'font-size="16" font-weight="700" fill="{COLOR_INK}">'
        f"Eval-2 headed partial / cost (recorded oracle quality)</text>\n",
        f'  <text x="{left}" y="46" font-family="DejaVu Sans, sans-serif" '
        f'font-size="11" fill="{COLOR_MUTED}">'
        f"HAPPY first; among comparable, higher partial then lower USD. "
        f"Bars only when PASS or fails/checks recorded. Coarse — not C²/$.</text>\n",
    ]
    y = top
    for task, group in groups:
        parts.append(
            f'  <text x="{left}" y="{y}" font-family="DejaVu Sans, sans-serif" '
            f'font-size="12" font-weight="700" fill="{COLOR_INK}">'
            f"{task.slot}. {_esc(task.title)}</text>\n"
        )
        y += 8
        for row in group:
            y += row_h
            fill = (
                COLOR_HAPPY
                if row.result.product_bar == "HAPPY"
                else COLOR_NOT_HAPPY
                if row.result.product_bar == "NOT_HAPPY"
                else COLOR_EMPTY
            )
            x = left + label_w
            parts.append(
                f'  <text x="{x - 8:.1f}" y="{y - 6:.1f}" text-anchor="end" '
                f'font-family="DejaVu Sans, sans-serif" font-size="11" fill="{COLOR_INK}">'
                f"{_esc(row.model.display_name)}</text>\n"
            )
            extras: list[str] = []
            if row.partial_score is not None:
                bar_w = row.partial_score * bar_max_w
                parts.append(
                    f'  <rect x="{x:.1f}" y="{y - 18:.1f}" width="{bar_w:.1f}" '
                    f'height="16" rx="3" fill="{fill}" stroke="{COLOR_LINE}" '
                    f'data-partial-score="{row.partial_score:.2f}"/>\n'
                )
                extras.append(_format_partial(row.partial_score))
                label_x = x + bar_w + 8
            else:
                extras.append("no ratio")
                label_x = x + 8
            fails_checks = _format_fails_checks(row.result)
            if fails_checks != "—":
                extras.append(fails_checks)
            afc = _format_afc_sr(row.result)
            if afc != "—":
                extras.append(afc)
            if row.cost_usd is not None:
                extras.append(f"${row.cost_usd:.4f}")
            parts.append(
                f'  <text x="{label_x:.1f}" y="{y - 6:.1f}" '
                f'font-family="DejaVu Sans, sans-serif" font-size="10" fill="{COLOR_MUTED}">'
                f"{_esc(' · '.join(extras))}</text>\n"
            )
        y += group_gap
    parts.append(
        f'  <text x="{left}" y="{height - 16}" font-family="DejaVu Sans, sans-serif" '
        f'font-size="10" fill="{COLOR_MUTED}">'
        f"partial = 1 when oracle PASS; else 1 − fails/checks. "
        f"Do not invent check counts.</text>\n"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        _svg_wrap(
            "".join(parts),
            width=width,
            height=height,
            title="Eval-2 headed partial / cost (recorded oracle quality)",
        ),
        encoding="utf-8",
    )
    return out_path


def write_eval2_svgs(board: Eval2Board, out_dir: Path) -> list[Path]:
    written = [
        write_heatmap_svg(board, out_dir / HEATMAP_NAME),
        write_coverage_svg(board, out_dir / COVERAGE_NAME),
        write_cost_svg(board, out_dir / COST_NAME),
        write_partial_svg(board, out_dir / PARTIAL_NAME),
    ]
    return written


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--in",
        dest="in_path",
        type=Path,
        default=DEFAULT_RESULTS,
        help="Eval-2 headed results JSON (not string-harness benchmark_results.json).",
    )
    parser.add_argument(
        "--out-dir",
        dest="out_dir",
        type=Path,
        default=DEFAULT_OUT_DIR,
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Validate JSON only; do not write SVGs.",
    )
    parser.add_argument(
        "--print-matrix",
        action="store_true",
        help="Print the markdown task×model table to stdout.",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.in_path.name == "benchmark_results.json":
        raise Eval2ResultsError(
            "refusing string-harness benchmark_results.json — use eval2_benchmark_results.json"
        )
    board = load_eval2_board(args.in_path)
    if args.print_matrix:
        sys.stdout.write(render_matrix_markdown(board))
        sys.stdout.write("\n")
        sys.stdout.write(render_cost_markdown(board))
        sys.stdout.write("\n")
        sys.stdout.write(render_partial_markdown(board))
    if args.check:
        print(f"OK {args.in_path} ({len(board.results)} scored cells, gate={board.gate_model})")
        return 0
    for path in write_eval2_svgs(board, args.out_dir):
        print(f"Wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
