# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Preserve constants, normalize existing PY cells, enrich number formats, and write output onto the target sheet."""

from __future__ import annotations

from typing import Any

from plugin.calc.address_utils import parse_address
from plugin.calc.python.formula_edit import PyFormula
from plugin.calc.spreadsheet_import.models import CellRecord, OutputCell, OutputSheetModel, SheetModel


def formula_output_cell(address: str, formula: str | None) -> OutputCell:
    """Create an ``OutputCell`` holding a formula with no cached value or number format."""
    return OutputCell(address=address, value=None, formula=formula, number_format=None)


def output_cell_from_record(cell: CellRecord) -> OutputCell:
    """Convert *cell* to an ``OutputCell`` under the preserve rule.

    A constant keeps its value and number format; an existing ``=PY()``/``=PYTHON()``
    is rewritten as canonical ``=PY("…"; ranges…)``, and one that does not parse keeps
    its text; every other formula passes through.
    """
    if cell.type == "constant":
        return OutputCell(address=cell.address, value=cell.value, formula=None, number_format=cell.number_format)
    formula = cell.formula
    if cell.type == "py_formula" and formula:
        parsed = PyFormula.parse(formula)
        if parsed is not None:
            formula = PyFormula.compose(parsed.code, parsed.data_args())
    return formula_output_cell(cell.address, formula)


def enrich_number_formats(sheet: Any, model: SheetModel) -> None:
    """Fill ``number_format`` on ingested cells (constants only)."""
    for addr, record in model.cells.items():
        if record.type != "constant":
            continue
        col, row = parse_address(addr)
        value = sheet.getCellByPosition(col, row).getPropertyValue("NumberFormat")
        if value is not None:
            record.number_format = int(value)


def apply_output_to_sheet(target_sheet: Any, output: OutputSheetModel) -> None:
    """Write *output* onto *target_sheet* (per-cell value or formula, then number formats). Skip empty cells; the target must be fresh or already cleared."""
    for address, oc in output.cells.items():
        value = oc.value
        blank = value in (None, "")
        if not oc.formula and oc.number_format is None and blank:
            continue
        col, row = parse_address(address)
        cell = target_sheet.getCellByPosition(col, row)
        # Do not str()/float() preserved values: leading zeros and formula-looking text stay as written.
        # Bools store 1.0/0.0 (setValue), not the text "True", the same as CellManipulator._classify_write_cell.
        if oc.formula:
            cell.setFormula(oc.formula)
        elif isinstance(value, bool):
            cell.setValue(1.0 if value else 0.0)
        elif isinstance(value, (int, float)):
            cell.setValue(value)
        elif not blank:
            cell.setString(value if isinstance(value, str) else str(value))
        if oc.number_format is not None:
            cell.setPropertyValue("NumberFormat", oc.number_format)
