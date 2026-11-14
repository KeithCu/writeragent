# SPDX-License-Identifier: GPL-3.0-or-later
"""Write a DAG conversion report into the open Calc document on OnLoadFinished: script-bank sheets, ;-separated =PY formulas, and a spill clear before each rewrite."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from plugin.calc.excel_py_convert.models import find_sheet_ci
from plugin.calc.excel_py_convert.parse_excel_ooxml import row_col_1based, a1_span_bounds
from plugin.calc.excel_py_convert.script_bank import (
    CODE_SHEET_NOTE,
    CODE_SHEET_NOTE_CELL,
    uno_sheet_is_bank,
    bank_defined_name_for,
    collect_script_bank,
)
from plugin.calc.python.spill import SPILL_CLEAR_FLAGS
from plugin.framework.errors import best_effort

if TYPE_CHECKING:
    from plugin.calc.excel_py_convert.models import ConversionReport

log = logging.getLogger(__name__)


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
    except Exception as exc:
        return [f"script bank collect failed: {exc}"]

    sheets = doc.getSheets()
    sheet_names = {str(n): str(n) for n in sheets.getElementNames()}
    for code_sheet in bank:
        if sheets.hasByName(code_sheet):
            existing_sh = sheets.getByName(code_sheet)
            if not uno_sheet_is_bank(doc, code_sheet, existing_sh):
                errors.append(f"Bank sheet collision: existing sheet {code_sheet!r} is not a script bank sheet")

    planned_cells: list[tuple[Any, str, str, str]] = []
    for cell in report.cells:
        if not cell.converted or not cell.converted_code:
            continue
        name = find_sheet_ci(sheet_names, cell.sheet)
        if name is None:
            errors.append(f"unmapped sheet {cell.sheet!r} for cell {cell.cell}")
            continue
        sheet = sheets.getByName(name)
        # to_dag stores the ;-separated =PY formula on every converted cell, so this writes exactly what the report says.
        formula = cell.dag_formula
        if not formula:
            errors.append(f"{cell.sheet}!{cell.cell}: converted cell has no dag_formula")
            continue
        array_ref = cell.array_ref or ""
        if array_ref:
            bounds = a1_span_bounds(array_ref)
            a_row, a_col = row_col_1based(cell.cell)
            if bounds is None or not (
                bounds[0][0] <= a_col <= bounds[1][0]
                and bounds[0][1] <= a_row <= bounds[1][1]
            ):
                errors.append(f"{cell.sheet}!{cell.cell}: invalid array_ref {array_ref!r}")
                continue
            (c1, r1), (c2, r2) = bounds
            if c1 == c2 and r1 == r2:
                array_ref = ""
        planned_cells.append((sheet, cell.cell, formula, array_ref))

    if errors:
        return errors

    # 2. Write with rollback tracking
    journal: list[tuple[str, str, Any]] = []
    named_ranges = getattr(doc, "NamedRanges", None)

    def _rollback() -> list[str]:
        failed: list[str] = []
        for label, detail, fn in reversed(journal):
            try:
                fn()
            except Exception as exc:
                log.warning("excel_py apply: rollback %s failed for %s: %s", label, detail, exc)
                failed.append(f"rollback incomplete: {label} {detail}: {exc}")
        return failed

    try:
        for code_sheet, sheet_bank in sorted(bank.items()):
            if not sheets.hasByName(code_sheet):
                sheets.insertNewByName(code_sheet, sheets.getCount())
                journal.append(("remove sheet", code_sheet, lambda sh_name=code_sheet: sheets.removeByName(sh_name) if sheets.hasByName(sh_name) else None))
            bank_sheet = sheets.getByName(code_sheet)

            if named_ranges is not None:
                expected_def = bank_defined_name_for(code_sheet)
                if not named_ranges.hasByName(expected_def):
                    # pyuno; only the bank defined-name path needs a UNO struct.
                    import uno

                    addr = uno.createUnoStruct("com.sun.star.table.CellAddress")
                    named_ranges.addNewByName(expected_def, f"'{code_sheet}'.$A$1", addr, 0)
                    journal.append(("removeByName", expected_def, lambda name=expected_def, nr=named_ranges: nr.removeByName(name) if nr.hasByName(name) else None))

            if CODE_SHEET_NOTE_CELL not in sheet_bank:
                note_cell = bank_sheet.getCellRangeByName(CODE_SHEET_NOTE_CELL)
                prev_note = note_cell.getString()
                journal.append(("setString", CODE_SHEET_NOTE_CELL, lambda c=note_cell, prev=prev_note: c.setString(prev)))
                note_cell.setString(CODE_SHEET_NOTE)

            for a1, code in sorted(sheet_bank.items()):
                cell_obj = bank_sheet.getCellRangeByName(a1)
                prev_code = cell_obj.getString()
                journal.append(("setString", a1, lambda c=cell_obj, prev=prev_code: c.setString(prev)))
                cell_obj.setString(code)
    except Exception as exc:
        errors = [f"script bank write failed: {exc}"]
        errors.extend(_rollback())
        best_effort(doc.calculateAll, "calculateAll after Excel PY apply rollback")
        return errors

    for sheet, coord, formula, array_ref in planned_cells:
        try:
            uno_cell = sheet.getCellRangeByName(coord)
            prev_formula = uno_cell.getFormula()
            journal.append(("setFormula", coord, lambda c=uno_cell, prev=prev_formula: c.setFormula(prev)))
            if array_ref:
                sheet.getCellRangeByName(array_ref).clearContents(SPILL_CLEAR_FLAGS)
            uno_cell.setFormula(formula)
        except Exception as exc:
            errors.append(f"{sheet.getName()}!{coord}: {exc}")
            errors.extend(_rollback())
            best_effort(doc.calculateAll, "calculateAll after Excel PY apply rollback")
            return errors

    best_effort(doc.calculateAll, "calculateAll after Excel PY apply")
    return []
