#!/usr/bin/env python3
# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Generate manual =PYTHON() serialization test CSVs for LibreOffice Calc.

Each test is a small block: input grid, Calc oracle, ``#=PYTHON(...)``, and ``#=IF(...)`` PASS/FAIL.
Remove ``#`` to activate formulas.

Usage (from repo root):
    python scripts/generate_serialization_test_csv.py
"""
from __future__ import annotations

import argparse
import csv
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests.calc.serialization_cases import SHEET_ORDER, SerializationCase, all_serialization_cases  # noqa: E402

DEFAULT_OUT = REPO_ROOT / "tests" / "fixtures"
_FORMULA_COMMENT = "#"
_MIN_INPUT_COLS = 4

COL_TEST_ID = 0
COL_DESCRIPTION = 1
COL_TAGS = 2
COL_INPUT_START = 3


@dataclass(frozen=True)
class CsvLayout:
    """Column indices for one generated sheet (input width drives formula columns)."""

    max_input_cols: int

    @property
    def col_calc_oracle(self) -> int:
        return COL_INPUT_START + self.max_input_cols

    @property
    def col_python_formula(self) -> int:
        return self.col_calc_oracle + 1

    @property
    def col_compare(self) -> int:
        return self.col_python_formula + 1

    @property
    def col_expected(self) -> int:
        return self.col_compare + 1

    @property
    def col_notes(self) -> int:
        return self.col_expected + 1

    @property
    def width(self) -> int:
        return self.col_notes + 1

    def header(self) -> list[str]:
        input_names = [f"input_col_{i + 1}" for i in range(self.max_input_cols)]
        return [
            "test_id",
            "description",
            "tags",
            *input_names,
            "calc_oracle",
            "python_formula",
            "compare_pass_fail",
            "expected",
            "notes",
        ]


def max_input_cols_for_cases(cases: list[SerializationCase]) -> int:
    """Widest input grid across cases (minimum 4 columns for layout stability)."""
    widest = _MIN_INPUT_COLS
    for case in cases:
        _, ncols = grid_dimensions(case.input_grid)
        widest = max(widest, ncols)
    return widest


def calc_col_letter(col_index: int) -> str:
    """0-based column index → Calc letter (A, B, …)."""
    if col_index < 26:
        return chr(ord("A") + col_index)
    # AA, AB, … (not needed for current fixtures)
    first = col_index // 26 - 1
    second = col_index % 26
    return chr(ord("A") + first) + chr(ord("A") + second)


def grid_dimensions(grid: list[list[Any]]) -> tuple[int, int]:
    if not grid:
        return 0, 0
    return len(grid), max(len(r) for r in grid)


def data_range_a1(top_row: int, nrows: int, ncols: int) -> str:
    """Input grid starts at input_col_1 (Calc column D by default)."""
    if nrows <= 0 or ncols <= 0:
        return ""
    col_start = calc_col_letter(COL_INPUT_START)
    col_end = calc_col_letter(COL_INPUT_START + ncols - 1)
    if nrows == 1 and ncols == 1:
        return f"{col_start}{top_row}"
    if nrows == 1:
        return f"{col_start}{top_row}:{col_end}{top_row}"
    if ncols == 1:
        return f"{col_start}{top_row}:{col_start}{top_row + nrows - 1}"
    return f"{col_start}{top_row}:{col_end}{top_row + nrows - 1}"


def _calc_oracle_formula(
    case: SerializationCase, data_range: str, block_top: int, *, nrows: int, ncols: int
) -> str:
    if case.calc_oracle == "SUM":
        return f"=SUM({data_range})"
    if case.calc_oracle == "MAX":
        return f"=MAX({data_range})"
    if case.calc_oracle == "INDEX_FIRST":
        if nrows <= 1:
            return f"=INDEX({data_range};1)"
        return f"=INDEX({data_range};1;1)"
    if case.calc_oracle == "MULT2":
        return f"=INDEX({data_range};ROW()-{block_top - 1};COLUMN()-{COL_INPUT_START})*2"
    if case.calc_oracle == "IDENTITY":
        return f"=INDEX({data_range};ROW()-{block_top - 1};COLUMN()-{COL_INPUT_START})"
    if case.expected is not None and case.mode != "error":
        return str(case.expected)
    return ""


def _python_formula(case: SerializationCase, data_range: str, block_top: int) -> str:
    # Single-line code only — np/pd/sp/math are auto-imported by venv_sandbox (AUTO_IMPORTS).
    code_one_line = " ".join(case.code.split())
    escaped = code_one_line.replace('"', '""')
    if case.mode == "error" or not data_range:
        return f'{_FORMULA_COMMENT}=PYTHON("{escaped}")'
    if case.mode == "matrix_index":
        return f'{_FORMULA_COMMENT}=PYTHON("{escaped}";{data_range};ROW()-{block_top - 1})'
    return f'{_FORMULA_COMMENT}=PYTHON("{escaped}";{data_range})'


def cell_csv_value(val: Any) -> str:
    """Write a Calc input cell; bools as 1/0 (CSV import does not evaluate =TRUE())."""
    if val is None:
        return ""
    if val is True:
        return "1"
    if val is False:
        return "0"
    return str(val)


def _compare_formula(layout: CsvLayout, case: SerializationCase, formula_row: int) -> str:
    calc_col = calc_col_letter(layout.col_calc_oracle)
    py_col = calc_col_letter(layout.col_python_formula)
    if case.mode == "matrix_index":
        return (
            f"{_FORMULA_COMMENT}matrix: remove # from {py_col}, Ctrl+Shift+Enter over 4x4 block; "
            f"each cell should match {calc_col}"
        )
    calc_cell = f"{calc_col}{formula_row}"
    py_cell = f"{py_col}{formula_row}"
    if case.mode == "error":
        return f'{_FORMULA_COMMENT}=IF(LEFT({py_cell};6)="Error:";"PASS";"FAIL")'
    if isinstance(case.expected, str):
        exp = case.expected.replace('"', '""')
        return f'{_FORMULA_COMMENT}=IF({py_cell}="{exp}";"PASS";"FAIL")'
    return f'{_FORMULA_COMMENT}=IF(ABS({calc_cell}-{py_cell})<0.001;"PASS";"FAIL")'


def block_rows(layout: CsvLayout, case: SerializationCase, block_top: int) -> list[list[str]]:
    """One test block aligned with ``layout.header()``."""
    nrows, ncols = grid_dimensions(case.input_grid)
    data_top = block_top + 1 if nrows else block_top
    data_range = data_range_a1(data_top, nrows, ncols)
    width = layout.width

    header = [""] * width
    header[COL_TEST_ID] = case.id
    header[COL_DESCRIPTION] = case.description
    header[COL_TAGS] = ",".join(case.tags)
    header[layout.col_calc_oracle] = (
        _calc_oracle_formula(case, data_range, data_top, nrows=nrows, ncols=ncols)
        if data_range
        else _calc_oracle_formula(case, "", block_top, nrows=0, ncols=0)
    )
    header[layout.col_python_formula] = _python_formula(case, data_range, data_top)
    header[layout.col_compare] = _compare_formula(layout, case, block_top)
    header[layout.col_expected] = str(case.expected if case.expected is not None else case.expected_error_substr or "")
    header[layout.col_notes] = case.notes

    lines: list[list[str]] = [header]
    for dr in range(nrows):
        row = [""] * width
        row[COL_TEST_ID] = f"input_row_{dr + 1}"
        for c in range(ncols):
            val = case.input_grid[dr][c]
            row[COL_INPUT_START + c] = cell_csv_value(val)
        lines.append(row)

    lines.append([""] * width)
    return lines


def ordered_cases() -> list[SerializationCase]:
    """All cases in sheet order (normal → mixed → grid → nan → errors)."""
    by_sheet = {s: [] for s in SHEET_ORDER}
    for case in all_serialization_cases():
        by_sheet[case.sheet].append(case)
    out: list[SerializationCase] = []
    for sheet in SHEET_ORDER:
        out.extend(by_sheet[sheet])
    return out


def build_csv_rows(cases: list[SerializationCase]) -> tuple[CsvLayout, list[list[str]]]:
    layout = CsvLayout(max_input_cols=max_input_cols_for_cases(cases))
    out: list[list[str]] = [layout.header()]
    row = 2
    for case in cases:
        block = block_rows(layout, case, row)
        out.extend(block)
        row += len(block)
    return layout, out


def _write_readme(path: Path, layout: CsvLayout) -> None:
    first_input = calc_col_letter(COL_INPUT_START)
    last_input = calc_col_letter(COL_INPUT_START + layout.max_input_cols - 1)
    oracle_col = calc_col_letter(layout.col_calc_oracle)
    py_col = calc_col_letter(layout.col_python_formula)
    cmp_col = calc_col_letter(layout.col_compare)
    path.write_text(
        f"""# =PYTHON() serialization test sheet (manual)

Open **`tests/fixtures/serialization_tests.csv`** in LibreOffice Calc (**File → Open**).

## Setup

1. **Settings → Python** — set `scripting.python_venv_path` to a venv with NumPy.
2. Deploy / restart WriterAgent so `=PYTHON()` is registered.

## Layout (each test block)

| Column | Header name | Content |
|--------|-------------|---------|
| A | `test_id` | Case id (data rows use `input_row_N`) |
| B | `description` | What this test checks |
| C | `tags` | e.g. `split_grid`, `below_threshold`, `bool` |
| {first_input}–{last_input} | `input_col_1` … `input_col_{layout.max_input_cols}` | Input data (blank = empty Calc cell) |
| {oracle_col} | `calc_oracle` | Native Calc reference (`=SUM`, `=MAX`, `=INDEX`, …) |
| {py_col} | `python_formula` | `#=PYTHON("…"; range)` — **remove `#`** to activate |
| {cmp_col} | `compare_pass_fail` | `#=IF(…;"PASS";"FAIL")` — **remove `#`** to activate |
| … | `expected` | Expected value (reference) |
| … | `notes` | Extra hints |

Sections follow case order: **normal** → **mixed** → **grid** → **nan** → **errors** (see `tags` / descriptions).

## Quick start

1. Open `tests/fixtures/serialization_tests.csv`.
2. On a test block, remove `#` from **python_formula** (column {py_col}) and **compare_pass_fail** (column {cmp_col}).
3. Press **Ctrl+Shift+F9** (recalculate).
4. **compare_pass_fail** should show `PASS`.

Logical inputs:
- Generator writes **`1`** / **`0`** for bool cases (CSV import does not evaluate `=TRUE()` formulas).
- Text `"True"` would stay a string through the wire (pickle-faithful) and break `np.sum`.

## Oracles

- **SUM** — primary; touches every numeric/logical cell.
- **MAX** — one spot-check on 4×4 (answer 16).
- **INDEX** — first cell (text/unicode) or per-cell grid egress.

## Grid returns (grid section)

1. Select a 4×4 output area aligned with the input block.
2. Paste the formula from **python_formula** (column {py_col}, without `#`) as a **matrix formula** (`Ctrl+Shift+Enter`).
3. Each cell should match **calc_oracle** (column {oracle_col}).

## Error cases (errors section)

**compare_pass_fail** should show `PASS` when **python_formula** displays `Error: …`.

## Regenerate

```bash
python scripts/generate_serialization_test_csv.py
```

Cases live in `tests/calc/serialization_cases.py`.
""",
        encoding="utf-8",
    )


def write_combined_csv(path: Path, cases: list[SerializationCase]) -> CsvLayout:
    layout, rows = build_csv_rows(cases)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerows(rows)
    return layout


def generate_all(output_dir: Path) -> None:
    cases = ordered_cases()
    layout = write_combined_csv(output_dir / "serialization_tests.csv", cases)
    _write_readme(output_dir / "serialization_tests.README.md", layout)
    print(f"Wrote serialization_tests.csv ({len(cases)} cases, {layout.max_input_cols} input cols) + README to {output_dir}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate =PYTHON() serialization test CSVs")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()
    generate_all(args.output_dir)


if __name__ == "__main__":
    main()
