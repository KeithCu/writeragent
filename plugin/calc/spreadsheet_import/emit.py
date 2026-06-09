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


def build_converted_output_model(
    model: SheetModel,
    *,
    vectorize: bool = False,
) -> tuple[OutputSheetModel, ConversionReport]:
    """Translate formula cells to ``=PY()``; preserve constants and normalized PY."""
    from plugin.calc.spreadsheet_import.vectorize import detect_vectorized_columns, to_r1c1, vectorize_range

    report = ConversionReport()
    circular = _circular_addresses(model)
    py_extracts = extract_py_cells(model)
    py_by_addr = {item.address: item for item in py_extracts}

    vector_groups = detect_vectorized_columns(model) if vectorize else {}
    vector_handled_cells = set()
    array_formulas: dict[str, str] = {}

    for first_addr, group in vector_groups.items():
        first_record = model.cells[first_addr]
        if not first_record.formula:
            continue
        translation = translate_formula(first_record.formula, cell_addr=first_addr)
        if translation.ok and translation.code and translation.data_ranges is not None:
            last_addr = group[-1]
            vectorized_data_ranges = []
            for a1_range in translation.data_ranges:
                r1c1_range = to_r1c1(a1_range, first_addr)
                vec_range = vectorize_range(r1c1_range, first_addr, last_addr)
                vectorized_data_ranges.append(vec_range)

            array_formula = emit_py_formula(translation.code, vectorized_data_ranges)
            array_formulas[f"{first_addr}:{last_addr}"] = array_formula

            # Mark all cells in this group as handled
            for addr in group:
                vector_handled_cells.add(addr)
                report.converted.append(addr)

    cells: dict[str, OutputCell] = {}
    for addr in sorted(model.cells):
        if addr in vector_handled_cells:
            cells[addr] = OutputCell(address=addr, value=None, formula=None, number_format=None)
            continue

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

        translation = translate_formula(record.formula, cell_addr=addr)
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
        array_formulas=array_formulas,
    )
    return output, report

