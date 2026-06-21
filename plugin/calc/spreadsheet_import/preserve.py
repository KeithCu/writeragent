# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Preserve constants and normalize existing PY cells onto an output sheet."""

from __future__ import annotations

from plugin.calc.address_utils import format_address, parse_address, parse_range_string
from plugin.calc.spreadsheet_import.extract import extract_py_cells
from plugin.calc.spreadsheet_import.ingest import ingest_sheet
from plugin.calc.spreadsheet_import.models import (
    CellRecord,
    OutputCell,
    OutputSheetModel,
    PyCellExtract,
    SheetModel,
)


def _iter_range_addresses(used_range: str) -> list[str]:
    (start_col, start_row), (end_col, end_row) = parse_range_string(used_range)
    addresses: list[str] = []
    for row in range(start_row, end_row + 1):
        for col in range(start_col, end_col + 1):
            addresses.append(format_address(col, row))
    return addresses


def _safe_number_format(cell) -> int | None:
    try:
        value = cell.getPropertyValue("NumberFormat")
        return int(value) if value is not None else None
    except Exception:
        return None


def enrich_number_formats(sheet, model: SheetModel, *, enrich_all: bool = False) -> SheetModel:
    """Fill ``number_format`` on ingested cells (constants only by default)."""
    for addr, record in model.cells.items():
        if not enrich_all and record.type != "constant":
            continue
        col, row = parse_address(addr)
        fmt = _safe_number_format(sheet.getCellByPosition(col, row))
        if fmt is not None:
            record.number_format = fmt
    return model


def _output_cell_from_record(cell: CellRecord, py_by_addr: dict[str, PyCellExtract]) -> OutputCell:
    if cell.type == "empty":
        return OutputCell(address=cell.address, value=None, formula=None, number_format=None)
    if cell.type == "constant":
        return OutputCell(
            address=cell.address,
            value=cell.value,
            formula=None,
            number_format=cell.number_format,
        )
    if cell.type == "py_formula":
        extract = py_by_addr.get(cell.address)
        formula = extract.normalized_formula if extract is not None else cell.formula
        return OutputCell(address=cell.address, value=None, formula=formula, number_format=None)
    if cell.formula:
        return OutputCell(address=cell.address, value=None, formula=cell.formula, number_format=None)
    return OutputCell(address=cell.address, value=cell.value, formula=None, number_format=None)


def build_output_model(model: SheetModel) -> OutputSheetModel:
    """Build a preserve output grid: constants unchanged, PY normalized, other formulas pass-through."""
    py_extracts = extract_py_cells(model)
    py_by_addr = {item.address: item for item in py_extracts}
    cells = {addr: _output_cell_from_record(record, py_by_addr) for addr, record in model.cells.items()}
    return OutputSheetModel(
        sheet_name=model.sheet_name,
        used_range=model.used_range,
        cells=cells,
        py_extracts=py_extracts,
    )


def apply_output_to_sheet(target_sheet, output: OutputSheetModel) -> None:
    """Write *output* onto *target_sheet* (bulk array write + number formats)."""
    (start_col, start_row), (end_col, end_row) = parse_range_string(output.used_range)

    formula_rows: list[tuple[str, ...]] = []
    format_cells: list[tuple[int, int, int]] = []

    for row in range(start_row, end_row + 1):
        row_data: list[str] = []
        for col in range(start_col, end_col + 1):
            addr = format_address(col, row)
            oc = output.cells[addr]
            if oc.formula:
                row_data.append(oc.formula)
            elif oc.value is None or oc.value == "":
                row_data.append("")
            else:
                row_data.append(str(oc.value))
            if oc.number_format is not None:
                format_cells.append((col, row, oc.number_format))
        formula_rows.append(tuple(row_data))

    # Write explicitly per-cell. Constants must be native numeric values (not text)
    # because vectorized PY formulas on the same output block will reference them as data.
    # Using setValue for numbers + setFormula for the generated PY cells guarantees
    # correct types independent of bulk array quirks with mixed formula/value rows.
    for r, row_entries in enumerate(formula_rows):
        for c, entry in enumerate(row_entries):
            cell = target_sheet.getCellByPosition(start_col + c, start_row + r)
            if isinstance(entry, str) and entry and (entry.startswith("=") or entry.startswith("{")):
                cell.setFormula(entry)
            elif entry == "" or entry is None:
                cell.setString("")
            else:
                try:
                    val = float(entry) if isinstance(entry, str) else entry
                    cell.setValue(val)
                except Exception:
                    cell.setString(str(entry))

    for col, row, fmt_id in format_cells:
        target_sheet.getCellByPosition(col, row).setPropertyValue("NumberFormat", fmt_id)

    # Write array/vectorized formulas
    if hasattr(output, "array_formulas") and output.array_formulas:
        for range_str, formula in output.array_formulas.items():
            try:
                cell_range = target_sheet.getCellRangeByName(range_str)
                cell_range.setArrayFormula(formula)
            except Exception as e:
                # Fallback to writing individually if setArrayFormula fails
                import logging
                logging.getLogger("writeragent.calc").error(
                    "Failed to set array formula %s on %s: %s", formula, range_str, e
                )



def preserve_sheet_to_new_sheet(
    doc,
    source_sheet,
    *,
    target_name: str = "PythonImport",
) -> OutputSheetModel:
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
