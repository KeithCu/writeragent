# SPDX-License-Identifier: GPL-3.0-or-later
"""Apply a DAG conversion report to an open Calc document via UNO.

Used when openpyxl is unavailable in the LibreOffice host. Parks code on a visible
``py_code_<Sheet>`` bank sheet when code is longer than 1000 characters (one per
source worksheet) at the caller A1; shorter scripts stay inline. Formulas use
Calc ``;`` separators. Spill ranges are cleared before rewrite.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from plugin.calc.excel_py_convert.script_bank import collect_script_bank, formula_for_converted_cell, report_safety_warnings, write_script_bank_uno
from plugin.framework.errors import best_effort

if TYPE_CHECKING:
    from plugin.calc.excel_py_convert.models import ConversionReport, ConvertedCell

log = logging.getLogger(__name__)


def _calc_formula_for_cell(cell: ConvertedCell) -> str:
    return formula_for_converted_cell(cell, separator=";", use_script_bank=True)


def _resolve_sheet(doc: Any, sheet_name: str) -> Any | None:
    sheets = doc.getSheets()
    if sheets.hasByName(sheet_name):
        return sheets.getByName(sheet_name)
    for i in range(sheets.getCount()):
        sh = sheets.getByIndex(i)
        if str(sh.getName()).lower() == sheet_name.lower():
            return sh
    return None


def _rect_to_a1(c1: int, r1: int, c2: int, r2: int) -> str:
    from plugin.calc.excel_py_convert.parse_excel_ooxml import _col_index_to_str

    if c1 == c2 and r1 == r2:
        return f"{_col_index_to_str(c1)}{r1}"
    return f"{_col_index_to_str(c1)}{r1}:{_col_index_to_str(c2)}{r2}"


def _clear_range_object(rng: Any) -> None:
    if hasattr(rng, "clearContents"):
        try:
            rng.clearContents(511)
        except Exception:
            rng.clearContents(0)
    else:
        rng.setFormula("")
        rng.setString("")


def _clear_spill_uno(sheet: Any, anchor: str, array_ref: str) -> None:
    if not array_ref:
        return
    from plugin.calc.excel_py_convert.parse_excel_ooxml import _col_row
    from plugin.calc.excel_py_convert.script_bank import a1_span_bounds

    bounds = a1_span_bounds(array_ref)
    if bounds is None:
        return
    (c1, r1), (c2, r2) = bounds
    if c1 == c2 and r1 == r2:
        return

    a_row, a_col = _col_row(anchor)

    anchor_cell = sheet.getCellRangeByName(anchor)
    prev_formula = anchor_cell.getFormula() if hasattr(anchor_cell, "getFormula") else ""
    try:
        rng = sheet.getCellRangeByName(array_ref)
        _clear_range_object(rng)
        anchor_cell.setFormula(prev_formula)
    except Exception:
        rects: list[tuple[int, int, int, int]] = []
        if r1 < a_row:
            rects.append((c1, r1, c2, a_row - 1))
        if r2 > a_row:
            rects.append((c1, a_row + 1, c2, r2))
        if c1 < a_col:
            rects.append((c1, a_row, a_col - 1, a_row))
        if c2 > a_col:
            rects.append((a_col + 1, a_row, c2, a_row))
        for rc1, rr1, rc2, rr2 in rects:
            try:
                r_ref = _rect_to_a1(rc1, rr1, rc2, rr2)
                _clear_range_object(sheet.getCellRangeByName(r_ref))
            except Exception:
                pass


def apply_dag_formulas_to_calc_doc(doc: Any, report: ConversionReport) -> list[str]:
    """Write script bank + converted formulas onto *doc*. Returns error strings."""
    errors: list[str] = []
    bank, bank_warnings = collect_script_bank(report)
    for w in bank_warnings:
        log.warning("excel_py apply: %s", w)
    for w in report_safety_warnings(report):
        log.warning("excel_py apply: %s", w)
    try:
        write_script_bank_uno(doc, bank)
    except Exception as exc:
        return [f"script bank write failed: {exc}"]

    for cell in report.cells:
        if not cell.converted or not cell.converted_code:
            continue
        sheet = _resolve_sheet(doc, cell.sheet)
        if sheet is None:
            errors.append(f"unmapped sheet {cell.sheet!r} for cell {cell.cell}")
            continue
        try:
            if cell.array_ref:
                _clear_spill_uno(sheet, cell.cell, cell.array_ref)
            uno_cell = sheet.getCellRangeByName(cell.cell)
            uno_cell.setFormula(_calc_formula_for_cell(cell))
        except Exception as exc:
            errors.append(f"{cell.sheet}!{cell.cell}: {exc}")
    if not errors and hasattr(doc, "calculateAll"):
        best_effort(doc.calculateAll, "calculateAll after Excel PY apply")
    return errors
