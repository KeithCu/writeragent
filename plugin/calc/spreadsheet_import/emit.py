# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Emit ``=PY()`` formulas and build converted output models."""

from __future__ import annotations

import logging
import re

from plugin.calc.address_utils import parse_address
from plugin.calc.python.formula_edit import PyFormula
from plugin.calc.spreadsheet_import.models import (
    CellRecord,
    ConversionReport,
    OutputCell,
    OutputSheetModel,
    SheetModel,
    TodoCell,
    formula_output_cell,
    output_cell_from_record,
)
from plugin.calc.spreadsheet_import.range_clip import clip_workbook_data_ranges
from plugin.calc.spreadsheet_import.translate import translate_formula
from plugin.calc.spreadsheet_import.vectorize import (
    detect_vectorized_columns,
    translation_has_cross_sheet_ranges,
    vectorize_range,
)


log = logging.getLogger(__name__)


def emit_py_formula(code: str, data_ranges: list[str], *, sheet_bounds: dict[str, tuple[int, int]] | None = None, current_sheet: str | None = None) -> str:
    """Build canonical ``=PY("…"; ranges…)``."""
    clipped = clip_workbook_data_ranges(data_ranges, sheet_bounds=sheet_bounds, current_sheet=current_sheet)
    return PyFormula.compose(code, clipped)


def _vector_group_formulas(model: SheetModel, group: list[str], sheet_bounds: dict[str, tuple[int, int]] | None) -> dict[str, str]:
    """Return one ``=PY()`` per cell of a filled-down column group, or ``{}`` to leave the group
    to the per-cell pass.

    The first cell's formula is translated once; every cell then gets the same code over the whole
    column range, plus its own index in the group as the last data argument. The group stays per-cell
    when the formula calls no function, does not translate, reads another sheet's range, or uses
    ``calc.fmt`` (TEXT with a format), which formats one value and turns a column array into its
    printed text.
    """
    first_addr = group[0]
    formula = model.cells[first_addr].formula
    if not formula:
        return {}
    translation = translate_formula(formula, cell_addr=first_addr, functions_only=True)
    if not (translation.ok and translation.code and translation.data_ranges is not None):
        return {}
    if translation_has_cross_sheet_ranges(translation.data_ranges) or "calc.fmt(" in translation.code:
        return {}
    row_delta = parse_address(group[-1])[1] - parse_address(first_addr)[1]
    vectorized_data_ranges = [vectorize_range(a1_range, row_delta) for a1_range in translation.data_ranges]
    # Convert bare data references to np.asarray(data) to support element-wise operations
    code = translation.code
    if len(translation.data_ranges) == 1:
        code = re.sub(r"(?<!np\.asarray\()\bdata\b", "np.asarray(data)", code)
    else:
        for idx in range(len(translation.data_ranges)):
            code = re.sub(rf"(?<!np\.asarray\()\bdata\[{idx}\]\b", f"np.asarray(data[{idx}])", code)
    return {
        addr: emit_py_formula(code, vectorized_data_ranges + [str(idx)], sheet_bounds=sheet_bounds, current_sheet=model.sheet_name)
        for idx, addr in enumerate(group)
    }


def build_converted_output_model(model: SheetModel, *, vectorize: bool = False, sheet_bounds: dict[str, tuple[int, int]] | None = None) -> tuple[OutputSheetModel, ConversionReport]:
    """Translate formula cells to ``=PY()``; preserve constants and normalized PY."""
    report = ConversionReport()
    circular = {addr for group in model.circular_groups for addr in group}

    vector_groups = detect_vectorized_columns(model) if vectorize else {}
    cells: dict[str, OutputCell] = {}

    for first_addr, group in vector_groups.items():
        try:
            group_formulas = _vector_group_formulas(model, group, sheet_bounds)
        except Exception:
            log.exception("build_converted_output_model: vector group at %s failed", first_addr)
            continue
        for addr, formula in group_formulas.items():
            cells[addr] = formula_output_cell(addr, formula)
        report.converted.extend(group_formulas)

    def _pass_through(addr: str, record: CellRecord, reason: str | None) -> None:
        report.pass_through.append(addr)
        if reason is not None:
            report.skipped.append(TodoCell(address=addr, reason=reason))
        cells[addr] = output_cell_from_record(record)

    for addr in sorted(model.cells):
        if addr in cells:
            continue

        record = model.cells[addr]
        if record.type in ("py_formula", "empty", "constant") or not record.formula:
            cells[addr] = output_cell_from_record(record)
            continue
        if record.type == "prompt":
            _pass_through(addr, record, "PROMPT")
            continue
        if record.type == "array_formula":
            _pass_through(addr, record, "ARRAY_FORMULA")
            continue
        if addr in circular:
            _pass_through(addr, record, "CIRCULAR_REF")
            continue

        try:
            translation = translate_formula(record.formula, cell_addr=addr, functions_only=True)
            if translation.has_function is False:
                # A formula with no function call (=A1*2, =B3) stays native Calc; it is not a skip.
                _pass_through(addr, record, None)
            elif translation.ok and translation.code and translation.data_ranges is not None:
                cells[addr] = formula_output_cell(addr, emit_py_formula(translation.code, translation.data_ranges, sheet_bounds=sheet_bounds, current_sheet=model.sheet_name))
                report.converted.append(addr)
            else:
                _pass_through(addr, record, translation.reason or "UNSUPPORTED_FUNCTION")
        except Exception:
            # parse/_emit recurse the AST; RecursionError escapes translate_formula
            # (it catches only ValueError/TypeError/IndexError/TranslationError).
            # Per-cell failure must not abort the whole sheet—keep the formula and report this cell.
            log.exception("build_converted_output_model: cell %s failed", addr)
            _pass_through(addr, record, "EMIT_ERROR")

    output = OutputSheetModel(sheet_name=model.sheet_name, used_range=model.used_range, cells=cells)
    return output, report
