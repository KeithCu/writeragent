# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Preserve constants and normalize existing PY cells onto an output sheet."""

from __future__ import annotations

from typing import Any

from plugin.calc.address_utils import parse_address
from plugin.calc.spreadsheet_import.models import OutputSheetModel, SheetModel, output_cell_from_record


def _safe_number_format(cell: Any) -> int | None:
    value = cell.getPropertyValue("NumberFormat")
    return int(value) if value is not None else None


def enrich_number_formats(sheet: Any, model: SheetModel) -> None:
    """Fill ``number_format`` on ingested cells (constants only)."""
    for addr, record in model.cells.items():
        if record.type != "constant":
            continue
        col, row = parse_address(addr)
        fmt = _safe_number_format(sheet.getCellByPosition(col, row))
        if fmt is not None:
            record.number_format = fmt


def build_output_model(model: SheetModel) -> OutputSheetModel:
    """Build a preserve output grid: constants unchanged, PY normalized, other formulas pass-through."""
    cells = {addr: output_cell_from_record(record) for addr, record in model.cells.items()}
    return OutputSheetModel(sheet_name=model.sheet_name, used_range=model.used_range, cells=cells)


def apply_output_to_sheet(target_sheet: Any, output: OutputSheetModel) -> None:
    """Write *output* onto *target_sheet* (per-cell value or formula, then number formats). Skip empty cells; the target must be fresh or already cleared."""
    for address, oc in output.cells.items():
        value = oc.value
        if not oc.formula and oc.number_format is None and (value is None or (isinstance(value, str) and value == "")):
            continue
        col, row = parse_address(address)
        cell = target_sheet.getCellByPosition(col, row)
        # Bugfix: every value was str()'d and then float()'d, so text
        # "00123" became setValue(123.0). The string grid also treated
        # text that starts with "=" or "{" as a formula. bool is an int,
        # and str(True) is the text "True"; CellManipulator._classify_write_cell
        # already stores bools as 1.0/0.0 so PY formulas see numbers.
        if oc.formula:
            cell.setFormula(oc.formula)
        elif isinstance(value, bool):
            cell.setValue(1.0 if value else 0.0)
        elif isinstance(value, (int, float)):
            cell.setValue(value)
        elif value is not None and value != "":
            cell.setString(value if isinstance(value, str) else str(value))
        if oc.number_format is not None:
            cell.setPropertyValue("NumberFormat", oc.number_format)
