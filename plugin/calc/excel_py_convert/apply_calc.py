# SPDX-License-Identifier: GPL-3.0-or-later
"""In-place UNO apply of a DAG report on OnLoadFinished, ; separators, spill clear before rewrite."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from plugin.calc.excel_py_convert.parse_excel_ooxml import _col_row, a1_span_bounds
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

# CellFlags: VALUE|DATETIME|STRING|FORMULA|HARDATTR. Same mask as
# plugin/calc/python/function.py._SPILL_CLEAR_FLAGS: keeps comments, styles,
# and draw objects in the former spill area (511 would wipe them).
_SPILL_CLEAR_FLAGS = 1 | 2 | 4 | 16 | 32


def _resolve_sheet(doc: Any, sheet_name: str) -> Any | None:
    sheets = doc.getSheets()
    if sheets.hasByName(sheet_name):
        return sheets.getByName(sheet_name)
    for i in range(sheets.getCount()):
        sh = sheets.getByIndex(i)
        if str(sh.getName()).lower() == sheet_name.lower():
            return sh
    return None


def _clear_spill_uno(sheet: Any, array_ref: str) -> None:
    bounds = a1_span_bounds(array_ref)
    assert bounds is not None, array_ref
    (c1, r1), (c2, r2) = bounds
    if c1 == c2 and r1 == r2:
        return
    sheet.getCellRangeByName(array_ref).clearContents(_SPILL_CLEAR_FLAGS)


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
        array_ref = cell.array_ref or ""
        if array_ref:
            bounds = a1_span_bounds(array_ref)
            a_row, a_col = _col_row(cell.cell)
            if bounds is None or not (
                bounds[0][0] <= a_col <= bounds[1][0]
                and bounds[0][1] <= a_row <= bounds[1][1]
            ):
                errors.append(f"{cell.sheet}!{cell.cell}: invalid array_ref {array_ref!r}")
                continue
        planned_cells.append((sheet, cell.cell, formula, array_ref))

    if errors:
        return errors

    # 2. Write with rollback tracking
    inserted_sheets: list[str] = []
    added_defined_names: list[str] = []
    overwritten_bank_strings: list[tuple[Any, str, str]] = []
    overwritten_formulas: list[tuple[Any, str, str]] = []

    def _rollback() -> list[str]:
        failed: list[str] = []
        for sh, a1, prev_f in reversed(overwritten_formulas):
            try:
                sh.getCellRangeByName(a1).setFormula(prev_f)
            except Exception as exc:
                log.warning("excel_py apply: rollback setFormula failed for %s: %s", a1, exc)
                failed.append(f"rollback incomplete: setFormula {a1}: {exc}")
        for sh, a1, prev_s in reversed(overwritten_bank_strings):
            try:
                sh.getCellRangeByName(a1).setString(prev_s)
            except Exception as exc:
                log.warning("excel_py apply: rollback setString failed for %s: %s", a1, exc)
                failed.append(f"rollback incomplete: setString {a1}: {exc}")
        named_ranges = getattr(doc, "NamedRanges", None)
        if named_ranges is not None and hasattr(named_ranges, "removeByName"):
            for name in reversed(added_defined_names):
                try:
                    if hasattr(named_ranges, "hasByName") and named_ranges.hasByName(name):
                        named_ranges.removeByName(name)
                except Exception as exc:
                    log.warning("excel_py apply: rollback removeByName failed for %s: %s", name, exc)
                    failed.append(f"rollback incomplete: removeByName {name}: {exc}")
        if hasattr(sheets, "removeByName"):
            for sh_name in reversed(inserted_sheets):
                try:
                    if sheets.hasByName(sh_name):
                        sheets.removeByName(sh_name)
                except Exception as exc:
                    log.warning("excel_py apply: rollback remove sheet failed for %s: %s", sh_name, exc)
                    failed.append(f"rollback incomplete: remove sheet {sh_name}: {exc}")
        return failed

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
                if hasattr(named_ranges, "hasByName") and not named_ranges.hasByName(expected_def):
                    import uno

                    addr = uno.createUnoStruct("com.sun.star.table.CellAddress")
                    named_ranges.addNewByName(expected_def, f"'{code_sheet}'.$A$1", addr, 0)
                    added_defined_names.append(expected_def)

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
        errors = [f"script bank write failed: {exc}"]
        errors.extend(_rollback())
        best_effort(doc.calculateAll, "calculateAll after Excel PY apply rollback")
        return errors

    for sheet, coord, formula, array_ref in planned_cells:
        try:
            uno_cell = sheet.getCellRangeByName(coord)
            prev_formula = uno_cell.getFormula() if hasattr(uno_cell, "getFormula") else ""
            overwritten_formulas.append((sheet, coord, prev_formula))
            if array_ref:
                _clear_spill_uno(sheet, array_ref)
            uno_cell.setFormula(formula)
        except Exception as exc:
            errors.append(f"{sheet.getName() if hasattr(sheet, 'getName') else 'Sheet'}!{coord}: {exc}")
            errors.extend(_rollback())
            best_effort(doc.calculateAll, "calculateAll after Excel PY apply rollback")
            return errors

    best_effort(doc.calculateAll, "calculateAll after Excel PY apply")
    return []
