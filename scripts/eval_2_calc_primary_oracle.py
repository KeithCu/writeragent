#!/usr/bin/env python3
# WriterAgent - eval-2 / Calc-primary model (branch profitability) oracle
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Soft structural scorer for the eval-2 Calc-primary model workbook.

Pass/fail is document-local. Chat Ready / STREAM_DONE is never consulted.
This is **not** the AFC Sample/SSC/column-K oracle. Miss modes are wrong
factor, pinned relative formula, and empty sheet_create tabs.

Usage:
  .venv/bin/python scripts/eval_2_calc_primary_oracle.py path/to/final_workbook.ods
  .venv/bin/python scripts/eval_2_headed.py --task calc-primary-model --score path/to/final_workbook.ods
"""
from __future__ import annotations

import argparse
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from eval_2_ods_oracle import (
    Cell,
    _count_scored_husks,
    _is_anon_db_sheet,
    _norm_sheet,
    _row_is_empty,
    cell_is_husk,
    read_workbook,
)

RAW_SHEET = "Raw Data"
MIN_SCHEDULE_SHEETS = 5
MIN_FORMULAS = 8
PIN_REPEAT = 4
BRANCH_NAMES = tuple(f"Branch {n}" for n in range(1, 11))
REGION_LETTERS = tuple("ABCDEFG")

_REL_A1 = re.compile(
    r"(?:(?<![.$])\$?[A-Za-z]{1,3}(?!\$)\d+|"
    r"\[\.(?!\$)[A-Za-z]{1,3}\d+\])"
)
_THEME_RES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("income", re.compile(r"income\s+statement|\bis\s+comparison\b|p\s*&\s*l|mom\s+var", re.I)),
    ("trend", re.compile(r"trend|trended|monthly\s+(?:is|income|2024)", re.I)),
    ("rank", re.compile(r"\brank(?:ing|s)?\b", re.I)),
    ("region", re.compile(r"\bregions?\b", re.I)),
    ("metrics", re.compile(r"\bmetrics?\b|efficiency|backlog\s+turn|\barpu\b", re.I)),
)
_FACTOR_RES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("Revenue (Units)", re.compile(r"revenue\s*\(\s*units\s*\)|rev(?:enue)?\s+units", re.I)),
    ("Project Backlog (Units)", re.compile(r"project\s+backlog", re.I)),
    ("Implementation Hours", re.compile(r"implementation\s+hours?", re.I)),
    ("Allocations", re.compile(r"shared\s+service\s+allocations|\ballocations\b", re.I)),
)
_LINE_RES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("Revenue", re.compile(r"\brevenue\b", re.I)),
    ("COGS", re.compile(r"\bcogs\b|cost\s+of\s+goods", re.I)),
    ("SG&A", re.compile(r"sg\s*&\s*a|\bsga\b", re.I)),
    ("ARPU", re.compile(r"\barpu\b|average\s+revenue\s+per\s+unit", re.I)),
    ("EBITDA", re.compile(r"\bebitda\b", re.I)),
    ("Gross Margin", re.compile(r"gross\s+margin|\bgm\s*%", re.I)),
)
_ARPU_LABEL = re.compile(r"\barpu\b|average\s+revenue\s+per\s+unit", re.I)
_ORDERS_ONLY = re.compile(r"\borders?\b", re.I)
_UNITS = re.compile(r"units?|rev\s+unit", re.I)
_FY2023 = re.compile(r"\b2023\b|fy\s*2023|m1\s*[–\-]\s*m12", re.I)
_FY2024 = re.compile(r"\b2024\b|fy\s*2024|m13\s*[–\-]\s*m24", re.I)
_M23 = re.compile(r"\bm23\b", re.I)
_M24 = re.compile(r"\bm24\b", re.I)
_M1 = re.compile(r"\bm1\b", re.I)
_M12 = re.compile(r"\bm12\b", re.I)
_M13 = re.compile(r"\bm13\b", re.I)


@dataclass
class OracleResult:
    """Fail-closed trial score. ``passed`` is true only when ``failures`` is empty."""

    passed: bool
    failures: list[str] = field(default_factory=list)
    sheet_names: list[str] = field(default_factory=list)
    schedule_sheets: int = 0
    formula_cells: int = 0
    husk_cells: int = 0
    scored_cells: int = 0
    pinned_columns: int = 0

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


def _is_raw_sheet(name: str) -> bool:
    return _norm_sheet(name) in {"raw data", "rawdata"}


def _visible_sheets(sheets: dict[str, list[list[Cell]]]) -> dict[str, list[list[Cell]]]:
    return {name: rows for name, rows in sheets.items() if not _is_anon_db_sheet(name)}


def _data_rows(rows: list[list[Cell]]) -> list[list[Cell]]:
    if not rows:
        return []
    body = rows[1:] if rows else []
    return [row for row in body if not _row_is_empty(row)]


def _workbook_text(sheets: dict[str, list[list[Cell]]]) -> str:
    parts: list[str] = []
    for name, rows in sheets.items():
        parts.append(name)
        for row in rows:
            for cell in row:
                parts.extend(cell.texts())
    return "\n".join(parts)


def _formula_cells(rows: list[list[Cell]]) -> list[Cell]:
    found: list[Cell] = []
    for row in rows:
        for cell in row:
            if cell.formula.strip() and not cell_is_husk(cell):
                found.append(cell)
    return found


def _pinned_relative_columns(rows: list[list[Cell]]) -> int:
    """Count columns where one relative formula is stamped unchanged."""
    if not rows:
        return 0
    width = max((len(row) for row in rows), default=0)
    pinned = 0
    for col in range(width):
        formulas = [
            row[col].formula.strip()
            for row in rows
            if col < len(row) and row[col].formula.strip()
        ]
        if len(formulas) < PIN_REPEAT:
            continue
        first = formulas[0]
        if not first or any(item != first for item in formulas):
            continue
        if _REL_A1.search(first):
            pinned += 1
    return pinned


def _arpu_uses_orders_not_units(rows: list[list[Cell]]) -> bool:
    """True when an ARPU-labeled row's formula uses Orders without units."""
    for row in rows:
        label = " ".join(str(cell.value or "") for cell in row)
        if not _ARPU_LABEL.search(label):
            continue
        for cell in row:
            formula = cell.formula
            if not formula:
                continue
            if _ORDERS_ONLY.search(formula) and not _UNITS.search(formula):
                return True
    return False


def score_sheets(sheets: dict[str, list[list[Cell]]]) -> OracleResult:
    """Apply the fail-closed model checks. Does not take a Ready argument."""
    failures: list[str] = []
    visible = _visible_sheets(sheets)
    names = list(visible)
    schedule = {
        name: rows for name, rows in visible.items() if not _is_raw_sheet(name)
    }
    populated = {
        name: rows for name, rows in schedule.items() if _data_rows(rows)
    }
    if len(populated) < MIN_SCHEDULE_SHEETS:
        failures.append(
            f"populated schedule sheets={len(populated)} < {MIN_SCHEDULE_SHEETS} "
            "(empty sheet_create is not the model)"
        )

    joined = _workbook_text(visible)
    missing_themes = [
        label for label, pattern in _THEME_RES if not pattern.search(joined)
    ]
    if missing_themes:
        failures.append(
            "missing component theme(s): " + ", ".join(missing_themes)
        )

    formula_cells = 0
    pinned_columns = 0
    schedule_rows: list[list[Cell]] = []
    for rows in schedule.values():
        formula_cells += len(_formula_cells(rows))
        pinned_columns += _pinned_relative_columns(rows)
        schedule_rows.extend(_data_rows(rows) or rows)
        if _arpu_uses_orders_not_units(rows):
            failures.append("ARPU formula uses Orders without Revenue Units")

    if formula_cells < MIN_FORMULAS:
        failures.append(
            f"formula cells={formula_cells} < {MIN_FORMULAS} "
            "(hardcoded / empty schedule)"
        )
    if pinned_columns:
        failures.append(
            f"pinned relative formula in {pinned_columns} column(s)"
        )

    missing_factors = [
        label for label, pattern in _FACTOR_RES if not pattern.search(joined)
    ]
    if missing_factors:
        failures.append("missing factor token(s): " + ", ".join(missing_factors))

    missing_lines = [
        label for label, pattern in _LINE_RES if not pattern.search(joined)
    ]
    if missing_lines:
        failures.append("missing model line(s): " + ", ".join(missing_lines))

    if not (_M1.search(joined) and _M12.search(joined) and _FY2023.search(joined)):
        failures.append("missing FY2023 period map (M1–M12 / 2023)")
    if not (_M13.search(joined) and _M24.search(joined) and _FY2024.search(joined)):
        failures.append("missing FY2024 period map (M13–M24 / 2024)")
    if not (_M23.search(joined) and _M24.search(joined)):
        failures.append("missing last-two-months anchors M23 / M24")

    missing_branches = [name for name in BRANCH_NAMES if name not in joined]
    if len(missing_branches) > 2:
        failures.append(
            f"ranking/model missing branches: {', '.join(missing_branches)}"
        )
    # Soft: the prompt's "Regions A through G", or labeled Region A…G.
    if not re.search(r"regions?\s*a\s*(?:through|–|-|to)\s*g", joined, re.I):
        found = [
            letter
            for letter in REGION_LETTERS
            if re.search(rf"region\s*{letter}\b", joined, re.I)
        ]
        if len(found) < 7:
            failures.append(
                "missing Regions A–G "
                f"(found {', '.join(found) or 'none'})"
            )

    husk_cells, scored_cells = _count_scored_husks(schedule_rows)
    if scored_cells and husk_cells * 2 >= scored_cells:
        failures.append(
            f"husk-dominated schedules ({husk_cells}/{scored_cells} scored cells)"
        )

    return OracleResult(
        passed=not failures,
        failures=failures,
        sheet_names=names,
        schedule_sheets=len(populated),
        formula_cells=formula_cells,
        husk_cells=husk_cells,
        scored_cells=scored_cells,
        pinned_columns=pinned_columns,
    )


def score_workbook(path: Path | str) -> OracleResult:
    workbook = Path(path)
    if not workbook.is_file():
        return OracleResult(passed=False, failures=[f"workbook not found: {workbook}"])
    try:
        sheets = read_workbook(workbook)
    except Exception as exc:
        return OracleResult(passed=False, failures=[f"cannot read workbook: {exc}"])
    return score_sheets(sheets)


def format_result(result: OracleResult) -> str:
    status = "PASS" if result.passed else "FAIL"
    lines = [
        status,
        f"  sheets: {', '.join(result.sheet_names) or '(none)'}",
        f"  schedule_sheets: {result.schedule_sheets}",
        f"  formulas: {result.formula_cells}",
        f"  pinned_columns: {result.pinned_columns}",
        f"  husks: {result.husk_cells}/{result.scored_cells} scored cells",
    ]
    for item in result.failures:
        lines.append(f"  - {item}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workbook", type=Path, help="Saved trial .ods (or .xlsx)")
    parser.add_argument("--json", action="store_true", help="Print the result as JSON")
    args = parser.parse_args(argv)
    result = score_workbook(args.workbook)
    if args.json:
        print(json.dumps(result.to_json(), indent=2))
    else:
        print(format_result(result))
    return 0 if result.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
