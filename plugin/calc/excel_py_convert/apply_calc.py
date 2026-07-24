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

from plugin.calc.excel_py_convert.script_bank import (
    collect_script_bank,
    formula_for_converted_cell,
    iter_a1_span,
    report_safety_warnings,
    write_script_bank_uno,
)

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


def _clear_spill_uno(sheet: Any, anchor: str, array_ref: str) -> None:
    if not array_ref:
        return
    cells = iter_a1_span(array_ref)
    if len(cells) <= 1:
        return
    for coord in cells:
        if coord == anchor:
            continue
        try:
            cell = sheet.getCellRangeByName(coord)
            cell.setFormula("")
            if hasattr(cell, "setString"):
                cell.setString("")
        except Exception:
            continue


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
    if not errors:
        try:
            if hasattr(doc, "calculateAll"):
                doc.calculateAll()
        except Exception:
            log.debug("calculateAll after Excel PY apply failed", exc_info=True)
    return errors
