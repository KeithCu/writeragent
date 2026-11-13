# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared Calc sheet egress for trusted helper results with tabular layouts."""

from __future__ import annotations

from typing import Any, Callable

from plugin.calc.address_utils import format_address, format_range, qualify_a1
from plugin.calc.bridge import CalcBridge
from plugin.calc.manipulator import CellManipulator
from plugin.calc.python.addin_values import to_calc_compatible
from plugin.doc.compound_undo import UndoStepOrRollback
from plugin.framework.errors import CalcError
from plugin.framework.i18n import _


def pad_grid(grid: list[list[Any]]) -> list[list[Any]]:
    """Pad ragged rows with "" so write_formula_range gets a rectangular array."""
    width = max((len(row) for row in grid), default=0)
    return [list(row) + [""] * (width - len(row)) for row in grid]


def append_blank(rows: list[list[Any]]) -> None:
    if rows and rows[-1]:
        rows.append([])


def append_key_value_block(rows: list[list[Any]], title: str, mapping: dict[str, Any]) -> None:
    if not mapping:
        return
    append_blank(rows)
    rows.append([title])
    rows.append(["Key", "Value"])
    for key, val in mapping.items():
        if isinstance(val, (dict, list)):
            rows.append([str(key), str(val)])
        else:
            rows.append([str(key), to_calc_compatible(val)])


def append_table_rows(
    rows: list[list[Any]],
    columns: Any,
    table_rows: Any,
    cell: Callable[[Any], Any],
) -> None:
    """Append column headers and data rows using the provided cell coercion function."""
    if isinstance(columns, list) and columns:
        rows.append([str(c) for c in columns])
    if isinstance(table_rows, list):
        for row in table_rows:
            if isinstance(row, list):
                rows.append([cell(c) for c in row])
            else:
                rows.append([cell(row)])


def append_truncation_note(rows: list[list[Any]], table: dict[str, Any]) -> None:
    if table.get("truncated"):
        total = table.get("total_rows")
        rows.append([f"(showing first rows; {total} total)" if total is not None else "(truncated)"])


def error_grid(result: dict[str, Any], domain_label: str, failed_message: str) -> list[list[Any]]:
    code = str(result.get("code") or "ERROR")
    message = str(result.get("message") or failed_message)
    return [[f"{domain_label} error ({code})"], [message]]


def append_flag_lines(rows: list[list[Any]], lines: list[str]) -> None:
    append_blank(rows)
    rows.append(["Flags"])
    for line in lines:
        rows.append([line])


def append_no_tabular_output(rows: list[list[Any]]) -> None:
    if len(rows) == 1:
        rows.append(["(no tabular output)"])


def format_tabular_helper_for_calc(result: dict[str, Any], *, domain_label: str, default_helper: str, failed_message: str, metadata_keys: tuple[str, ...] = ("n_rows", "n_cols", "numeric_cols")) -> list[list[Any]]:
    """Turn a tabular helper result dict into a row-major grid for ``write_formula_range``."""
    rows: list[list[Any]] = []

    if result.get("status") == "error":
        return error_grid(result, domain_label, failed_message)

    helper = str(result.get("helper") or default_helper)
    raw_ctx = result.get("context")
    ctx: dict[str, Any] = raw_ctx if isinstance(raw_ctx, dict) else {}
    range_a1 = str(ctx.get("range_a1") or "").strip()
    title = f"{helper} — {range_a1}" if range_a1 else helper
    rows.append([title])

    metrics = result.get("metrics")
    if isinstance(metrics, dict):
        append_key_value_block(rows, "Metrics", metrics)

    flags = result.get("flags")
    if isinstance(flags, list) and flags:
        append_flag_lines(rows, [str(item) for item in flags])

    tables = result.get("tables")
    if isinstance(tables, list):
        for table in tables:
            if not isinstance(table, dict):
                continue
            append_blank(rows)
            rows.append([str(table.get("name") or "table")])
            append_table_rows(rows, table.get("columns"), table.get("rows"), to_calc_compatible)
            append_truncation_note(rows, table)

    metadata = result.get("metadata")
    if isinstance(metadata, dict):
        subset = {k: metadata[k] for k in metadata_keys if k in metadata}
        if subset:
            append_key_value_block(rows, "Metadata", subset)

    append_no_tabular_output(rows)
    return rows


def insert_tabular_result_into_calc(
    doc: Any,
    grid: list[list[Any]],
    *,
    sheet_name: str | None = None,
    start_col: int | None = None,
    start_row: int | None = None,
) -> int:
    """Write *grid* starting at *start_col*/*start_row* (or selection). Returns row count."""
    if start_col is None or start_row is None:
        controller = doc.getCurrentController()
        selection = controller.getSelection()
        if selection is not None and hasattr(selection, "getRangeAddress"):
            addr = selection.getRangeAddress()
            col, row = int(addr.StartColumn), int(addr.StartRow)
        else:
            col, row = 0, 0
        start_col = col if start_col is None else start_col
        start_row = row if start_row is None else start_row

    bridge = CalcBridge(doc)
    manipulator = CellManipulator(bridge)

    addr = format_address(start_col, start_row)
    if sheet_name:
        addr = qualify_a1(sheet_name, addr)

    grid = pad_grid(grid)
    if grid and grid[0]:
        rows, cols = len(grid), len(grid[0])
        # Refuse to overwrite user data, same as _write_array_formula in manipulator.py.
        sheet = bridge.sheet_or_active(sheet_name)
        occupied: list[tuple[int, int]] = []
        for r in range(start_row, start_row + rows):
            for c in range(start_col, start_col + cols):
                cell = sheet.getCellByPosition(c, r)
                formula = cell.getFormula()
                text = cell.getString()
                if (isinstance(formula, str) and formula) or (isinstance(text, str) and text):
                    occupied.append((c, r))
        if occupied:
            block_name = format_range(start_col, start_row, start_col + cols - 1, start_row + rows - 1, collapse_single=True)
            block_c, block_r = occupied[0]
            raise CalcError(
                "The result needs %s, but %d cell(s) there are not empty (first: %s). Nothing was written; clear them or start elsewhere."
                % (block_name, len(occupied), format_address(block_c, block_r))
            )

    # Group the entire grid into one Ctrl+Z step so a failed write removes any partial cells.
    with UndoStepOrRollback(doc, _("WriterAgent: Insert result")):
        manipulator.write_formula_range(addr, grid, data_text=True)
    return len(grid)
