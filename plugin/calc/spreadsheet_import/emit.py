# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Emit ``=PY()`` formulas and build converted output models."""

from __future__ import annotations

import logging

from plugin.calc.python.formula_edit import PyFormula
from plugin.calc.spreadsheet_import.models import (
    CellRecord,
    ConversionReport,
    OutputCell,
    OutputSheetModel,
    ReportCode,
    SheetModel,
    TodoCell,
    TranslationResult,
)
from plugin.calc.spreadsheet_import.preserve import formula_output_cell, output_cell_from_record
from plugin.calc.spreadsheet_import.range_clip import clip_workbook_data_ranges
from plugin.calc.spreadsheet_import.translate import translate_formula
from plugin.calc.spreadsheet_import.vectorize import detect_vectorized_columns

log = logging.getLogger(__name__)


def emit_py_formula(code: str, data_ranges: list[str], *, sheet_bounds: dict[str, tuple[int, int]] | None = None, current_sheet: str | None = None) -> str:
    """Build canonical ``=PY("…"; ranges…)``."""
    clipped = clip_workbook_data_ranges(data_ranges, sheet_bounds=sheet_bounds, current_sheet=current_sheet)
    return PyFormula.compose(code, clipped)


def _translate_or_none(addr: str, formula: str) -> TranslationResult | None:
    try:
        return translate_formula(formula, cell_addr=addr, functions_only=True)
    except Exception:
        # parse/_emit recurse the AST; RecursionError escapes translate_formula
        # (it catches only ValueError/TypeError/IndexError/TranslationError).
        # Per-cell failure must not abort the whole sheet—keep the formula and report this cell.
        log.exception("build_converted_output_model: cell %s failed", addr)
        return None


def build_converted_output_model(model: SheetModel, *, vectorize: bool, sheet_bounds: dict[str, tuple[int, int]] | None = None) -> tuple[OutputSheetModel, ConversionReport]:
    """Translate formula cells to ``=PY()``; preserve constants and normalized PY."""
    report = ConversionReport()
    circular = {addr for group in model.circular_groups for addr in group}

    translations = {
        addr: _translate_or_none(addr, rec.formula)
        for addr, rec in sorted(model.cells.items())
        if rec.type in ("formula", "error") and rec.formula and addr not in circular
    }
    cells: dict[str, OutputCell] = {}

    for run in (detect_vectorized_columns(model, translations) if vectorize else []):
        try:
            group_formulas = {
                addr: emit_py_formula(run.code, [run.column, str(idx)], sheet_bounds=sheet_bounds, current_sheet=model.sheet_name)
                for idx, addr in enumerate(run.cells)
            }
        except Exception:
            log.exception("build_converted_output_model: vector group at %s failed", run.cells[0])
            continue
        for addr, formula in group_formulas.items():
            cells[addr] = formula_output_cell(addr, formula)
        report.converted.extend(group_formulas)

    def _pass_through(addr: str, record: CellRecord, reason: ReportCode | None) -> None:
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

        translation = translations[addr]
        if translation is None:
            _pass_through(addr, record, "EMIT_ERROR")
            continue
        try:
            if translation.has_function is False:
                # A formula with no function call (=A1*2, =B3) stays native Calc; it is not a skip.
                _pass_through(addr, record, None)
            elif translation.ok and translation.code and translation.data_ranges is not None:
                cells[addr] = formula_output_cell(addr, emit_py_formula(translation.code, translation.data_ranges, sheet_bounds=sheet_bounds, current_sheet=model.sheet_name))
                report.converted.append(addr)
            else:
                _pass_through(addr, record, translation.reason or "UNSUPPORTED_FUNCTION")
        except Exception:
            log.exception("build_converted_output_model: cell %s failed", addr)
            _pass_through(addr, record, "EMIT_ERROR")

    output = OutputSheetModel(sheet_name=model.sheet_name, used_range=model.used_range, cells=cells)
    return output, report
