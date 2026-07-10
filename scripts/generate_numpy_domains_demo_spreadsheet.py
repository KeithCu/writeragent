#!/usr/bin/env python3
# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Generate manual NumPy domains demo ODS for LibreOffice Calc.

Covers Analysis, Viz, Math, Quant, Optimize, Units helpers plus Goal Seek/Solver.

Usage (from repo root):
    python scripts/generate_numpy_domains_demo_spreadsheet.py
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from odf.opendocument import OpenDocumentSpreadsheet
from odf.table import Table, TableCell, TableRow
from odf.text import P

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests.calc.numpy_domains_demo_cases import (  # noqa: E402
    DOMAIN_SHEET_ORDER,
    DomainDemoCase,
    DomainName,
    MatplotlibDemoBlock,
    cases_for_domain,
    goal_seek_solver_layout,
    matplotlib_demo_blocks,
)

DEFAULT_OUT = REPO_ROOT / "tests" / "fixtures"
_MAX_INPUT_COLS = 9
# Native ODS: semicolon argument separators (LibreOffice locale default).
_ARG_SEP = ";"
_CALC_PYTHON_FN = "PYTHON"
# LibreOffice portable add-in token (matches CalcAddIns + addin_librepy registration).
_CALC_PYTHON_ADDIN_FN = "ORG.EXTENSION.WRITERAGENT.PYTHONFUNCTION.PYTHON"

COL_TEST_ID = 0
COL_DOMAIN = 1
COL_HELPER = 2
COL_DESCRIPTION = 3
COL_INPUT_START = 4

COL_PARAMS = COL_INPUT_START + _MAX_INPUT_COLS
COL_EXPECTED = COL_PARAMS + 1
COL_PYTHON = COL_EXPECTED + 1
COL_SCRIPT_HINT = COL_PYTHON + 1
COL_CHAT = COL_SCRIPT_HINT + 1
COL_NOTES = COL_CHAT + 1
_LAYOUT_WIDTH = COL_NOTES + 1


@dataclass(frozen=True)
class BlockLayout:
    max_input_cols: int = _MAX_INPUT_COLS

    @property
    def width(self) -> int:
        return _LAYOUT_WIDTH

    def header(self) -> list[str]:
        col_names = [f"col_{i + 1}" for i in range(self.max_input_cols)]
        return [
            "test_id",
            "domain",
            "helper",
            "description",
            *col_names,
            "params",
            "expected_scalar",
            "python_formula",
            "script_hint",
            "chat_prompt",
            "notes",
        ]


def calc_col_letter(col_index: int) -> str:
    if col_index < 26:
        return chr(ord("A") + col_index)
    first = col_index // 26 - 1
    second = col_index % 26
    return chr(ord("A") + first) + chr(ord("A") + second)


def grid_dimensions(grid: list[list[Any]]) -> tuple[int, int]:
    if not grid:
        return 0, 0
    return len(grid), max(len(r) for r in grid)


def data_range_a1(top_row: int, nrows: int, ncols: int, *, col_start: int = COL_INPUT_START) -> str:
    if nrows <= 0 or ncols <= 0:
        return ""
    col_start_letter = calc_col_letter(col_start)
    col_end_letter = calc_col_letter(col_start + ncols - 1)
    if nrows == 1 and ncols == 1:
        return f"{col_start_letter}{top_row}"
    if nrows == 1:
        return f"{col_start_letter}{top_row}:{col_end_letter}{top_row}"
    if ncols == 1:
        return f"{col_start_letter}{top_row}:{col_start_letter}{top_row + nrows - 1}"
    return f"{col_start_letter}{top_row}:{col_end_letter}{top_row + nrows - 1}"


def cell_sheet_value(val: Any) -> int | float | str | None:
    if val is None:
        return None
    if val is True:
        return 1
    if val is False:
        return 0
    if isinstance(val, (int, float, str)):
        return val
    return str(val)


def _python_formula(expr: str | None, data_range: str) -> str:
    if not expr:
        return ""
    code_one_line = " ".join(expr.split())
    escaped = code_one_line.replace('"', '""')
    if not data_range:
        return f'={_CALC_PYTHON_FN}("{escaped}")'
    return f'={_CALC_PYTHON_FN}("{escaped}"{_ARG_SEP}{data_range})'


def _substitute_data_range(text: str | None, data_range: str, sheet_name: str) -> str | None:
    if not text:
        return text
    full_range = f"{sheet_name}.{data_range}" if data_range else ""
    return text.replace("<DATA_RANGE>", full_range)


def block_rows(
    layout: BlockLayout,
    case: DomainDemoCase,
    block_top: int,
    *,
    sheet_name: str,
) -> list[list[Any]]:
    grid = case.input_grid or []
    nrows, ncols = grid_dimensions(grid)
    data_top = block_top + 1 if nrows else block_top
    data_range = data_range_a1(data_top, nrows, ncols)
    width = layout.width

    header: list[Any] = [None] * width
    header[COL_TEST_ID] = case.id
    header[COL_DOMAIN] = case.domain
    header[COL_HELPER] = case.helper
    header[COL_DESCRIPTION] = case.description
    header[COL_PARAMS] = json.dumps(case.params, separators=(",", ":")) if case.params else "{}"
    header[COL_EXPECTED] = case.expected_scalar
    header[COL_PYTHON] = _python_formula(case.python_expr, data_range)
    header[COL_SCRIPT_HINT] = case.script_hint
    header[COL_CHAT] = _substitute_data_range(case.chat_prompt, data_range, sheet_name)
    note_parts = [case.notes] if case.notes else []
    if case.requires_package:
        note_parts.append(f"requires: {case.requires_package}")
    if case.requires_network:
        note_parts.append("requires: network")
    note_parts.append(f"check_mode: {case.check_mode}")
    header[COL_NOTES] = "; ".join(p for p in note_parts if p)

    lines: list[list[Any]] = [header]
    for dr in range(nrows):
        row: list[Any] = [None] * width
        row[COL_TEST_ID] = f"row_{dr + 1}"
        if dr < len(grid):
            for c, val in enumerate(grid[dr]):
                if c < ncols:
                    row[COL_INPUT_START + c] = cell_sheet_value(val)
        lines.append(row)

    lines.append([None] * width)
    return lines


def matplotlib_block_rows(
    layout: BlockLayout,
    block: MatplotlibDemoBlock,
    block_top: int,
) -> list[list[Any]]:
    grid = block.input_grid or []
    nrows, ncols = grid_dimensions(grid)
    data_top = block_top + 1 if nrows else block_top
    data_range = data_range_a1(data_top, nrows, ncols)
    width = layout.width

    header: list[Any] = [None] * width
    header[COL_TEST_ID] = block.id
    header[COL_DOMAIN] = "viz"
    header[COL_HELPER] = "matplotlib_raw"
    header[COL_DESCRIPTION] = block.description
    header[COL_EXPECTED] = block.expected_scalar
    header[COL_PYTHON] = _python_formula(block.python_expr, data_range)
    header[COL_SCRIPT_HINT] = "N/A — inline =PYTHON() matplotlib"
    header[COL_NOTES] = block.notes

    lines: list[list[Any]] = [header]
    for dr in range(nrows):
        row: list[Any] = [None] * width
        row[COL_TEST_ID] = f"row_{dr + 1}"
        if dr < len(grid):
            for c, val in enumerate(grid[dr]):
                row[COL_INPUT_START + c] = cell_sheet_value(val)
        lines.append(row)
    lines.append([None] * width)
    return lines


def build_domain_sheet_rows(
    domain: DomainName,
    *,
    sheet_name: str,
    extra_matplotlib: bool = False,
) -> tuple[BlockLayout, list[list[Any]]]:
    layout = BlockLayout()
    cases = cases_for_domain(domain)
    out: list[list[Any]] = [layout.header()]
    excel_row = 2
    out.append([f"__section__:{domain}"] + [None] * (layout.width - 1))
    excel_row += 1

    for case in cases:
        block = block_rows(layout, case, excel_row, sheet_name=sheet_name)
        out.extend(block)
        excel_row += len(block)

    if extra_matplotlib:
        out.append([f"__section__:matplotlib_raw"] + [None] * (layout.width - 1))
        excel_row += 1
        for mblock in matplotlib_demo_blocks():
            block = matplotlib_block_rows(layout, mblock, excel_row)
            out.extend(block)
            excel_row += len(block)

    return layout, out


def _is_empty_cell(val: Any) -> bool:
    return val is None or val == ""


def _ods_formula(calc_formula: str) -> str:
    """OpenFormula for Calc; PYTHON calls use the fully qualified add-in name."""
    if calc_formula.startswith(f"={_CALC_PYTHON_FN}("):
        body = f"{_CALC_PYTHON_ADDIN_FN}(" + calc_formula[len(f"={_CALC_PYTHON_FN}(") :]
        return f"of:={body}"
    return f"of:{calc_formula}"


def _make_ods_cell(val: Any, *, span_cols: int = 1) -> TableCell:
    if isinstance(val, str) and val.startswith("__section__:"):
        label = f"[{val.removeprefix('__section__:')}]"
        cell = TableCell(valuetype="string", numbercolumnsspanned=span_cols)
        cell.addElement(P(text=label))
        return cell
    if _is_empty_cell(val):
        return TableCell()
    if isinstance(val, str) and val.startswith("="):
        return TableCell(valuetype="formula", formula=_ods_formula(val))
    if isinstance(val, bool):
        return TableCell(valuetype="boolean", booleanvalue=str(val).lower())
    if isinstance(val, int):
        return TableCell(valuetype="float", value=float(val))
    if isinstance(val, float):
        return TableCell(valuetype="float", value=val)
    cell = TableCell(valuetype="string")
    cell.addElement(P(text=str(val)))
    return cell


def _ods_row_from_values(row: list[Any], layout: BlockLayout) -> TableRow:
    tr = TableRow()
    if isinstance(row[0], str) and row[0].startswith("__section__:"):
        tr.addElement(_make_ods_cell(row[0], span_cols=layout.width))
        return tr
    for val in row:
        tr.addElement(_make_ods_cell(val))
    return tr


def _add_ods_table(doc: OpenDocumentSpreadsheet, name: str, rows: list[list[Any]], layout: BlockLayout) -> None:
    table = Table(name=name)
    for row in rows:
        table.addElement(_ods_row_from_values(row, layout))
    doc.spreadsheet.addElement(table)


def _write_ods_sheet_from_rows(doc: OpenDocumentSpreadsheet, name: str, rows: list[list[Any]], layout: BlockLayout) -> None:
    _add_ods_table(doc, name, rows, layout)


def _readme_lines() -> list[str]:
    return [
        "NumPy Domains Demo Workbook",
        "",
        "Open this .ods file in LibreOffice Calc (formulas use fully qualified =PYTHON add-in names).",
        "",
        "SETUP",
        "1. Settings → Python — set scripting.python_venv_path to your venv.",
        "2. Install packages (Settings → Python Test shows groups):",
        "   Analysis: uv pip install numpy pandas scipy scikit-learn statsmodels ydata-profiling pandas-montecarlo",
        "   Viz: uv pip install matplotlib seaborn",
        "   Math: uv pip install sympy",
        "   Quant: uv pip install yfinance pandas-ta quantstats pyportfolioopt",
        "   Optimize: scipy (usually with analysis stack)",
        "   Forecast: statsmodels (usually with analysis stack)",
        "   Units: uv pip install pint",
        "3. Deploy / restart WriterAgent so =PYTHON() and Run Python Script are registered.",
        "",
        "SHEETS",
        "• analysis, forecast, viz, math, quant, optimize, units — one block per helper",
        "• goal_seek_solver — native Calc Goal Seek / Solver (chat/MCP only)",
        "",
        "HOW TO TEST",
        "• scalar / formatted_cell: Ctrl+Shift+F9 — compare python_formula column to expected_scalar",
        "• visual: Run Python Script → Viz Helpers — chart image should appear on sheet",
        "• grid_egress / math: Run Python Script — multi-cell report or LO Math object",
        "• chat_prompt: paste into WriterAgent chat or MCP (where provided)",
        "• quant fetch_historical_data: requires internet",
        "",
        "REGENERATE",
        "python scripts/generate_numpy_domains_demo_spreadsheet.py",
        "",
        "See tests/fixtures/numpy_domains_demo.README.md and docs/numpy-domains.md",
    ]


def _write_readme_ods(doc: OpenDocumentSpreadsheet) -> None:
    layout = BlockLayout()
    rows = [[line] + [None] * (layout.width - 1) for line in _readme_lines()]
    _add_ods_table(doc, "readme", rows, layout)


def _goal_seek_solver_rows() -> tuple[BlockLayout, list[list[Any]]]:
    blocks = goal_seek_solver_layout()
    layout = BlockLayout()
    width = layout.width
    rows: list[list[Any]] = []

    max_data_row = 0
    for block in blocks:
        for _label, _col, r, _val in block.cells:
            max_data_row = max(max_data_row, r)

    for r in range(max_data_row + 1):
        row: list[Any] = [None] * width
        for block in blocks:
            for label, col, cell_r, val in block.cells:
                if cell_r == r:
                    row[col] = val
                    row[5] = label
        rows.append(row)

    rows.append([None] * width)
    rows.append([None] * width)

    doc_start = len(rows)
    headers = ["block_id", "description", "expected", "chat_prompt", "notes"]
    header_row: list[Any] = [None] * width
    for col_idx, h in enumerate(headers):
        header_row[col_idx] = h
    rows.append(header_row)

    for block in blocks:
        block_row: list[Any] = [None] * width
        block_row[0] = block.id
        block_row[1] = block.description
        block_row[2] = block.expected
        block_row[3] = block.chat_prompt
        block_row[4] = block.notes
        rows.append(block_row)

    hint_row: list[Any] = [None] * width
    hint_row[7] = "Goal Seek: A1=variable, B1=A1^2 → target 100 → |x|≈10"
    if doc_start > 0:
        rows[0] = list(rows[0])
        rows[0][7] = hint_row[7]
    solver_hint_row: list[Any] = [None] * width
    solver_hint_row[7] = "Solver: max 3x+5y s.t. x+y<=10 (see A3:D3)"
    if len(rows) > 2:
        rows[2] = list(rows[2])
        rows[2][7] = solver_hint_row[7]

    return layout, rows


def _write_goal_seek_solver_ods(doc: OpenDocumentSpreadsheet) -> None:
    layout, rows = _goal_seek_solver_rows()
    _add_ods_table(doc, "goal_seek_solver", rows, layout)


def write_workbook(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = OpenDocumentSpreadsheet()
    _write_readme_ods(doc)

    for domain in DOMAIN_SHEET_ORDER:
        sheet_name = domain
        layout, rows = build_domain_sheet_rows(
            domain,
            sheet_name=sheet_name,
            extra_matplotlib=(domain == "viz"),
        )
        _write_ods_sheet_from_rows(doc, sheet_name, rows, layout)

    _write_goal_seek_solver_ods(doc)
    doc.save(str(path))


def _write_readme_md(path: Path) -> None:
    path.write_text(
        """# NumPy domains demo workbook (manual)

Open **`tests/fixtures/numpy_domains_demo.ods`** in LibreOffice Calc.

Native ODS uses fully qualified `ORG.EXTENSION.WRITERAGENT.PYTHONFUNCTION.PYTHON()` formulas and semicolon argument separators (LibreOffice does not lowercase custom add-ins on ODS open the way it does for imported XLSX).

## Setup

1. **Settings → Python** — configure `scripting.python_venv_path`.
2. Install venv packages (see **readme** sheet or Settings → Python **Test**).
3. Deploy / restart WriterAgent.

## Sheets

| Sheet | Helpers |
|-------|---------|
| `analysis` | 14 trusted analysis helpers + `analyze_data` chat |
| `forecast` | `forecast_time_series`, `decompose_time_series` + `forecast_data` chat |
| `viz` | `quick_plot`, `correlation_heatmap`, `time_series_plot` + raw matplotlib block |
| `math` | SymPy: solve, simplify, integrate, differentiate |
| `quant` | yfinance / pandas-ta / quantstats / pyportfolioopt (RPS only) |
| `optimize` | scipy LP, portfolio, scheduling + `optimize_data` chat |
| `units` | pint converters (RPS only) |
| `goal_seek_solver` | Native `calc_goal_seek` / `calc_solver` via chat |

## Quick start

1. Open the workbook; read the **readme** tab.
2. On **analysis** (or optimize/math): **Ctrl+Shift+F9** — eyeball `python_formula` vs `expected_scalar`.
3. On **viz**: select data range → **Tools → Run Python Script… → Viz Helpers**.
4. On **math/units/quant/optimize/forecast**: use matching **Run Python Script** helper section.
5. Paste **chat_prompt** cells into WriterAgent chat where provided.

Replace `<DATA_RANGE>` in chat prompts with the actual `Sheet.col` range from each block header row.

## Regenerate

```bash
python scripts/generate_numpy_domains_demo_spreadsheet.py
```

Cases: `tests/calc/numpy_domains_demo_cases.py`.
""",
        encoding="utf-8",
    )


def generate_all(output_dir: Path) -> None:
    ods_path = output_dir / "numpy_domains_demo.ods"
    write_workbook(ods_path)
    _write_readme_md(output_dir / "numpy_domains_demo.README.md")
    print(f"Wrote numpy_domains_demo.ods + README to {output_dir}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate NumPy domains demo ODS")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()
    generate_all(args.output_dir)


if __name__ == "__main__":
    main()
