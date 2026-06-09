# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Emit ``=PY()`` formulas and build converted output models."""

from __future__ import annotations

from plugin.calc.python_formula_edit import rebuild_python_formula_with_data
from plugin.calc.spreadsheet_import.extract import extract_py_cells
from plugin.calc.spreadsheet_import.models import (
    ConversionReport,
    OutputCell,
    OutputSheetModel,
    PyCellExtract,
    SheetModel,
    TodoCell,
)
from plugin.calc.spreadsheet_import.preserve import _output_cell_from_record
from plugin.calc.spreadsheet_import.translate import translate_formula


def emit_py_formula(code: str, data_ranges: list[str]) -> str:
    """Build canonical ``=PY("…"; ranges…)``."""
    return rebuild_python_formula_with_data(code, data_ranges)


def _circular_addresses(model: SheetModel) -> set[str]:
    addrs: set[str] = set()
    for group in model.circular_groups:
        addrs.update(group)
    return addrs


def build_converted_output_model(model: SheetModel) -> tuple[OutputSheetModel, ConversionReport]:
    """Translate formula cells to ``=PY()``; preserve constants and normalized PY."""
    report = ConversionReport()
    circular = _circular_addresses(model)
    py_extracts = extract_py_cells(model)
    py_by_addr = {item.address: item for item in py_extracts}

    cells: dict[str, OutputCell] = {}
    for addr in sorted(model.cells):
        record = model.cells[addr]
        if record.type == "empty":
            cells[addr] = OutputCell(address=addr, value=None, formula=None, number_format=None)
            continue
        if record.type == "constant":
            cells[addr] = _output_cell_from_record(record, py_by_addr)
            continue
        if record.type == "py_formula":
            report.normalized_py.append(addr)
            cells[addr] = _output_cell_from_record(record, py_by_addr)
            continue
        if record.type == "prompt":
            report.pass_through.append(addr)
            report.skipped.append(TodoCell(address=addr, reason="PROMPT"))
            cells[addr] = _output_cell_from_record(record, py_by_addr)
            continue
        if record.type == "array_formula":
            report.pass_through.append(addr)
            report.skipped.append(TodoCell(address=addr, reason="ARRAY_FORMULA"))
            cells[addr] = _output_cell_from_record(record, py_by_addr)
            continue
        if addr in circular:
            report.pass_through.append(addr)
            report.skipped.append(TodoCell(address=addr, reason="CIRCULAR_REF"))
            cells[addr] = _output_cell_from_record(record, py_by_addr)
            continue
        if record.type not in ("formula", "error") or not record.formula:
            cells[addr] = _output_cell_from_record(record, py_by_addr)
            continue

        translation = translate_formula(record.formula)
        if translation.ok and translation.code and translation.data_ranges is not None:
            cells[addr] = OutputCell(
                address=addr,
                value=None,
                formula=emit_py_formula(translation.code, translation.data_ranges),
                number_format=None,
            )
            report.converted.append(addr)
        else:
            reason = translation.reason or "UNSUPPORTED_FUNCTION"
            report.pass_through.append(addr)
            report.skipped.append(TodoCell(address=addr, reason=reason))
            cells[addr] = _output_cell_from_record(record, py_by_addr)

    output = OutputSheetModel(
        sheet_name=model.sheet_name,
        used_range=model.used_range,
        cells=cells,
        py_extracts=py_extracts,
    )
    return output, report
