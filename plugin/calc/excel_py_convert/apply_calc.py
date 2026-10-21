# SPDX-License-Identifier: GPL-3.0-or-later
"""In-place UNO apply of a DAG report on OnLoadFinished, ; separators, spill clear before rewrite."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from plugin.calc.excel_py_convert.script_bank import (
    CODE_SHEET_NOTE,
    CODE_SHEET_NOTE_CELL,
    _uno_sheet_is_bank,
    bank_defined_name_for,
    collect_script_bank,
    formula_for_converted_cell,
    report_safety_warnings,
)
from plugin.framework.errors import best_effort

if TYPE_CHECKING:
    from plugin.calc.excel_py_convert.models import ConversionReport

log = logging.getLogger(__name__)


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
    from plugin.calc.excel_py_convert.parse_excel_ooxml import col_index_to_letters

    if c1 == c2 and r1 == r2:
        return f"{col_index_to_letters(c1)}{r1}"
    return f"{col_index_to_letters(c1)}{r1}:{col_index_to_letters(c2)}{r2}"


def _clear_range_object(rng: Any) -> None:
    try:
        rng.clearContents(511)
    except Exception:
        rng.clearContents(0)


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
    """Write script bank + converted formulas onto *doc*. Returns error strings.

    Preflights all sheets and formulas before mutating. On failure during write,
    restores modified formulas and bank strings, and removes added defined names and sheets.
    """
    errors: list[str] = []

    # 1. Preflight: collect bank, verify existing bank sheets, resolve sheets, build formulas
    try:
        bank, bank_warnings = collect_script_bank(report)
        for w in bank_warnings:
            log.warning("excel_py apply: %s", w)
        for w in report_safety_warnings(report):
            log.warning("excel_py apply: %s", w)
    except Exception as exc:
        return [f"script bank collect failed: {exc}"]

    sheets = doc.getSheets()
    for code_sheet in bank:
        if sheets.hasByName(code_sheet):
            existing_sh = sheets.getByName(code_sheet)
            if not _uno_sheet_is_bank(doc, code_sheet, existing_sh):
                errors.append(f"Bank sheet collision: existing sheet {code_sheet!r} is not a script bank sheet")

    planned_cells: list[tuple[Any, str, str, str]] = []
    for cell in report.cells:
        if not cell.converted or not cell.converted_code:
            continue
        sheet = _resolve_sheet(doc, cell.sheet)
        if sheet is None:
            errors.append(f"unmapped sheet {cell.sheet!r} for cell {cell.cell}")
            continue
        try:
            formula = formula_for_converted_cell(cell, separator=";")
        except Exception as exc:
            errors.append(f"{cell.sheet}!{cell.cell}: {exc}")
            continue
        planned_cells.append((sheet, cell.cell, formula, cell.array_ref or ""))

    if errors:
        return errors

    # 2. Write with rollback tracking
    inserted_sheets: list[str] = []
    added_defined_names: list[str] = []
    overwritten_bank_strings: list[tuple[Any, str, str]] = []
    overwritten_formulas: list[tuple[Any, str, str]] = []

    def _rollback() -> None:
        for sh, a1, prev_f in reversed(overwritten_formulas):
            try:
                sh.getCellRangeByName(a1).setFormula(prev_f)
            except Exception:
                pass
        for sh, a1, prev_s in reversed(overwritten_bank_strings):
            try:
                sh.getCellRangeByName(a1).setString(prev_s)
            except Exception:
                pass
        named_ranges = getattr(doc, "NamedRanges", None)
        if named_ranges is not None and hasattr(named_ranges, "removeByName"):
            for name in reversed(added_defined_names):
                try:
                    if hasattr(named_ranges, "hasByName") and named_ranges.hasByName(name):
                        named_ranges.removeByName(name)
                except Exception:
                    pass
        if hasattr(sheets, "removeByName"):
            for sh_name in reversed(inserted_sheets):
                try:
                    if sheets.hasByName(sh_name):
                        sheets.removeByName(sh_name)
                except Exception:
                    pass

    try:
        for code_sheet, sheet_bank in sorted(bank.items()):
            if not sheet_bank:
                continue
            if not sheets.hasByName(code_sheet):
                sheets.insertNewByName(code_sheet, sheets.getCount())
                inserted_sheets.append(code_sheet)
            bank_sheet = sheets.getByName(code_sheet)

            named_ranges = getattr(doc, "NamedRanges", None)
            if named_ranges is not None:
                expected_def = bank_defined_name_for(code_sheet)
                try:
                    if hasattr(named_ranges, "hasByName") and not named_ranges.hasByName(expected_def):
                        import uno

                        addr = uno.createUnoStruct("com.sun.star.table.CellAddress")
                        named_ranges.addNewByName(expected_def, f"'{code_sheet}'.$A$1", addr, 0)
                        added_defined_names.append(expected_def)
                except Exception:
                    pass

            if CODE_SHEET_NOTE_CELL not in sheet_bank:
                note_cell = bank_sheet.getCellRangeByName(CODE_SHEET_NOTE_CELL)
                prev_note = note_cell.getString() if hasattr(note_cell, "getString") else ""
                overwritten_bank_strings.append((bank_sheet, CODE_SHEET_NOTE_CELL, prev_note))
                note_cell.setString(CODE_SHEET_NOTE)

            for a1, code in sorted(sheet_bank.items()):
                cell_obj = bank_sheet.getCellRangeByName(a1)
                prev_code = cell_obj.getString() if hasattr(cell_obj, "getString") else ""
                overwritten_bank_strings.append((bank_sheet, a1, prev_code))
                cell_obj.setString(code)
    except Exception as exc:
        _rollback()
        return [f"script bank write failed: {exc}"]

    for sheet, coord, formula, array_ref in planned_cells:
        try:
            if array_ref:
                _clear_spill_uno(sheet, coord, array_ref)
            uno_cell = sheet.getCellRangeByName(coord)
            prev_formula = uno_cell.getFormula() if hasattr(uno_cell, "getFormula") else ""
            overwritten_formulas.append((sheet, coord, prev_formula))
            uno_cell.setFormula(formula)
        except Exception as exc:
            errors.append(f"{sheet.getName() if hasattr(sheet, 'getName') else 'Sheet'}!{coord}: {exc}")
            _rollback()
            return errors

    best_effort(doc.calculateAll, "calculateAll after Excel PY apply")
    return []
