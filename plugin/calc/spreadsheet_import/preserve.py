# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Preserve constants and normalize existing PY cells onto an output sheet."""

from __future__ import annotations

from typing import Any

from plugin.calc.address_utils import format_address, parse_address, parse_range_string
from plugin.calc.spreadsheet_import.extract import extract_py_cells
from plugin.calc.spreadsheet_import.ingest import ingest_sheet
from plugin.calc.spreadsheet_import.models import OutputSheetModel, SheetModel, output_cell_from_record


def _safe_number_format(cell: Any) -> int | None:
    value = cell.getPropertyValue("NumberFormat")
    return int(value) if value is not None else None


def enrich_number_formats(sheet: Any, model: SheetModel, *, enrich_all: bool = False) -> SheetModel:
    """Fill ``number_format`` on ingested cells (constants only by default)."""
    for addr, record in model.cells.items():
        if not enrich_all and record.type != "constant":
            continue
        col, row = parse_address(addr)
        fmt = _safe_number_format(sheet.getCellByPosition(col, row))
        if fmt is not None:
            record.number_format = fmt
    return model


def build_output_model(model: SheetModel) -> OutputSheetModel:
    """Build a preserve output grid: constants unchanged, PY normalized, other formulas pass-through."""
    py_extracts = extract_py_cells(model)
    py_by_addr = {item.address: item for item in py_extracts}
    cells = {addr: output_cell_from_record(record, py_by_addr) for addr, record in model.cells.items()}
    return OutputSheetModel(sheet_name=model.sheet_name, used_range=model.used_range, cells=cells, py_extracts=py_extracts)


def apply_output_to_sheet(target_sheet: Any, output: OutputSheetModel) -> None:
    """Write *output* onto *target_sheet* (per-cell value or formula, then number formats)."""
    (start_col, start_row), (end_col, end_row) = parse_range_string(output.used_range)
    for row in range(start_row, end_row + 1):
        for col in range(start_col, end_col + 1):
            oc = output.cells[format_address(col, row)]
            cell = target_sheet.getCellByPosition(col, row)
            # Bugfix: every value was str()'d and then float()'d, so text
            # "00123" became setValue(123.0). The string grid also treated
            # text that starts with "=" or "{" as a formula. bool is an int,
            # and str(True) is the text "True"; CellManipulator._classify_write_cell
            # already stores bools as 1.0/0.0 so PY formulas see numbers.
            value = oc.value
            if oc.formula:
                cell.setFormula(oc.formula)
            elif isinstance(value, bool):
                cell.setValue(1.0 if value else 0.0)
            elif isinstance(value, (int, float)):
                cell.setValue(value)
            elif value is None or value == "":
                cell.setString("")
            else:
                cell.setString(value if isinstance(value, str) else str(value))
            if oc.number_format is not None:
                cell.setPropertyValue("NumberFormat", oc.number_format)


def preserve_sheet_to_new_sheet(doc: Any, source_sheet: Any, *, target_name: str = "PythonImport") -> OutputSheetModel:
    """Ingest *source_sheet*, preserve/normalize, and write to a new sheet in *doc*."""
    model = ingest_sheet(source_sheet)
    enrich_number_formats(source_sheet, model)
    output = build_output_model(model)

    sheets = doc.getSheets()
    if sheets.hasByName(target_name):
        sheets.remove(sheets.getByName(target_name))
    sheets.insertNewByName(target_name, sheets.getCount())
    target_sheet = sheets.getByName(target_name)
    apply_output_to_sheet(target_sheet, output)
    output.sheet_name = target_name
    return output
