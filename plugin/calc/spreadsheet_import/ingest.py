# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Ingest Calc sheet snapshots into :class:`SheetModel`."""

from __future__ import annotations

import re
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

from plugin.calc.address_utils import format_address
from plugin.calc.python.formula_edit import normalize_formula_string
from plugin.calc.spreadsheet_import.extract import is_py_formula_text
from plugin.calc.spreadsheet_import.graph import (
    attach_graph_to_model,
    extract_cell_refs,
    filter_refs_to_scope,
    is_calc_error_display,
)
from plugin.calc.spreadsheet_import.models import CellRecord, CellType, SheetModel

_PROMPT_HEAD_RE = re.compile(r"^=\s*PROMPT\s*\(", re.IGNORECASE)


def _format_used_range(start_col: int, start_row: int, end_col: int, end_row: int) -> str:
    start = format_address(start_col, start_row)
    end = format_address(end_col, end_row)
    if start == end:
        return start
    return f"{start}:{end}"


def classify_cell(raw_val: Any, raw_formula: str) -> tuple[CellType, Any, str | None, str | None]:
    """Classify one cell from bulk ``getDataArray`` / ``getFormulaArray`` entries.

    Returns ``(type, value, formula, error_code)``.
    """
    formula: str | None = None
    error_code: str | None = None

    raw_text = str(raw_formula) if raw_formula else ""
    if raw_text.startswith("=") or (raw_text.startswith("{") and raw_text.endswith("}")):
        formula = raw_text
        value = raw_val

        # Array formulas keep ``{=…}`` wrapper; normalize strips braces.
        if formula.startswith("{") and formula.endswith("}"):
            return "array_formula", value, formula, None

        normalized = normalize_formula_string(formula)
        if is_py_formula_text(formula):
            return "py_formula", value, formula, None
        if _PROMPT_HEAD_RE.match(normalized):
            return "prompt", value, formula, None

        error_code = is_calc_error_display(value)
        if error_code is not None:
            return "error", value, formula, error_code
        return "formula", value, formula, None

    if isinstance(raw_val, float):
        return "constant", raw_val, None, None
    if isinstance(raw_val, str) and raw_val:
        return "constant", raw_val, None, None
    if isinstance(raw_val, bool):
        return "constant", raw_val, None, None
    if raw_val is not None and raw_val != "":
        return "constant", raw_val, None, None
    return "empty", None, None, None


def ingest_from_arrays(
    *,
    sheet_name: str,
    start_col: int,
    start_row: int,
    data_array: Sequence[Sequence[Any]],
    formula_array: Sequence[Sequence[str]],
) -> SheetModel:
    """Build a :class:`SheetModel` from mocked or UNO-fetched 2D arrays."""
    if len(data_array) != len(formula_array):
        raise ValueError("data_array and formula_array row counts differ")

    end_row = start_row + len(data_array) - 1
    row_widths = [len(row) for row in data_array]
    if not row_widths:
        used_range = format_address(start_col, start_row)
        model = SheetModel(sheet_name=sheet_name, used_range=used_range, cells={})
        return attach_graph_to_model(model)

    end_col = start_col + max(row_widths) - 1
    used_range = _format_used_range(start_col, start_row, end_col, end_row)
    cells: dict[str, CellRecord] = {}

    for row_idx, (data_row, formula_row) in enumerate(zip(data_array, formula_array, strict=True)):
        if len(formula_row) != len(data_row):
            raise ValueError(f"row {row_idx}: data and formula column counts differ")
        abs_row = start_row + row_idx
        for col_idx, (raw_val, raw_formula) in enumerate(zip(data_row, formula_row, strict=True)):
            abs_col = start_col + col_idx
            address = format_address(abs_col, abs_row)
            cell_type, value, formula, error_code = classify_cell(raw_val, raw_formula)
            cells[address] = CellRecord(
                address=address,
                type=cell_type,
                value=value,
                formula=formula,
                number_format=None,
                precedents=[],
                error_code=error_code,
            )

    scope = frozenset(cells)
    for cell in cells.values():
        if cell.formula:
            refs = extract_cell_refs(cell.formula)
            # Drop self-references (e.g. ``=A1+1`` in A1) and out-of-scope refs.
            refs = [r for r in refs if r != cell.address]
            cell.precedents = filter_refs_to_scope(refs, scope)

    model = SheetModel(sheet_name=sheet_name, used_range=used_range, cells=cells)
    return attach_graph_to_model(model)


def _used_range_address(sheet) -> Any:
    """Return ``RangeAddress`` for the sheet used area (same pattern as SheetAnalyzer)."""
    cursor = sheet.createCursor()
    cursor.gotoStartOfUsedArea(False)
    cursor.gotoEndOfUsedArea(True)
    return cursor.getRangeAddress()


def ingest_sheet(sheet, *, range_addr: Any | None = None) -> SheetModel:
    """Ingest an open Calc sheet via bulk ``getDataArray`` / ``getFormulaArray``."""
    addr = range_addr if range_addr is not None else _used_range_address(sheet)
    sheet_name = sheet.getName() if hasattr(sheet, "getName") else ""

    cell_range = sheet.getCellRangeByPosition(
        addr.StartColumn,
        addr.StartRow,
        addr.EndColumn,
        addr.EndRow,
    )
    data_array = cell_range.getDataArray()
    formula_array = cell_range.getFormulaArray()

    return ingest_from_arrays(
        sheet_name=sheet_name,
        start_col=addr.StartColumn,
        start_row=addr.StartRow,
        data_array=data_array,
        formula_array=formula_array,
    )


def used_range_string_from_address(addr) -> str:
    """Format a UNO ``RangeAddress`` as an A1 range string."""
    return _format_used_range(addr.StartColumn, addr.StartRow, addr.EndColumn, addr.EndRow)
