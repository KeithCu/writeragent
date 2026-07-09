# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared Calc sheet egress for trusted helper results with tabular layouts."""

from __future__ import annotations

from typing import Any

from plugin.calc.address_utils import index_to_column
from plugin.calc.bridge import CalcBridge
from plugin.calc.manipulator import CellManipulator
from plugin.calc.python.function import to_calc_compatible


def _cell(value: Any) -> Any:
    return to_calc_compatible(value)


def _append_blank(rows: list[list[Any]]) -> None:
    if rows and rows[-1]:
        rows.append([])


def _append_key_value_block(rows: list[list[Any]], title: str, mapping: dict[str, Any]) -> None:
    if not mapping:
        return
    _append_blank(rows)
    rows.append([title])
    rows.append(["Key", "Value"])
    for key, val in mapping.items():
        if isinstance(val, (dict, list)):
            rows.append([str(key), str(val)])
        else:
            rows.append([str(key), _cell(val)])


def calc_anchor_from_selection(doc: Any) -> tuple[int, int]:
    """Return (start_col, start_row) from the current Calc selection."""
    controller = doc.getCurrentController()
    selection = controller.getSelection()
    if selection is not None and hasattr(selection, "getRangeAddress"):
        addr = selection.getRangeAddress()
        return int(addr.StartColumn), int(addr.StartRow)
    return 0, 0


def format_tabular_helper_for_calc(
    result: dict[str, Any],
    *,
    domain_label: str,
    default_helper: str,
    failed_message: str,
    metadata_keys: tuple[str, ...] = ("n_rows", "n_cols", "numeric_cols"),
) -> list[list[Any]]:
    """Turn a tabular helper result dict into a row-major grid for ``write_formula_range``."""
    rows: list[list[Any]] = []

    if result.get("status") == "error":
        code = str(result.get("code") or "ERROR")
        message = str(result.get("message") or failed_message)
        return [[f"{domain_label} error ({code})"], [message]]

    helper = str(result.get("helper") or default_helper)
    raw_ctx = result.get("context")
    ctx: dict[str, Any] = raw_ctx if isinstance(raw_ctx, dict) else {}
    range_a1 = str(ctx.get("range_a1") or "").strip()
    title = f"{helper} — {range_a1}" if range_a1 else helper
    rows.append([title])

    metrics = result.get("metrics")
    if isinstance(metrics, dict) and metrics:
        _append_key_value_block(rows, "Metrics", metrics)

    flags = result.get("flags")
    if isinstance(flags, list) and flags:
        _append_blank(rows)
        rows.append(["Flags"])
        for item in flags:
            rows.append([str(item)])

    tables = result.get("tables")
    if isinstance(tables, list):
        for table in tables:
            if not isinstance(table, dict):
                continue
            _append_blank(rows)
            rows.append([str(table.get("name") or "table")])
            columns = table.get("columns")
            table_rows = table.get("rows")
            if isinstance(columns, list) and columns:
                rows.append([str(c) for c in columns])
            if isinstance(table_rows, list):
                for row in table_rows:
                    if isinstance(row, list):
                        rows.append([_cell(cell) for cell in row])
                    else:
                        rows.append([_cell(row)])
            if table.get("truncated"):
                total = table.get("total_rows")
                note = f"(showing first rows; {total} total)" if total is not None else "(truncated)"
                rows.append([note])

    metadata = result.get("metadata")
    if isinstance(metadata, dict) and metadata:
        subset = {k: metadata[k] for k in metadata_keys if k in metadata}
        if subset:
            _append_key_value_block(rows, "Metadata", subset)

    if len(rows) == 1:
        rows.append(["(no tabular output)"])
    return rows


def insert_tabular_result_into_calc(
    doc: Any,
    uno_ctx: Any,
    grid: list[list[Any]],
    *,
    start_col: int | None = None,
    start_row: int | None = None,
) -> int:
    """Write *grid* starting at *start_col*/*start_row* (or selection). Returns row count."""
    if start_col is None or start_row is None:
        col, row = calc_anchor_from_selection(doc)
        start_col = col if start_col is None else start_col
        start_row = row if start_row is None else start_row

    bridge = CalcBridge(doc)
    manipulator = CellManipulator(bridge)
    addr = f"{index_to_column(start_col)}{start_row + 1}"
    manipulator.write_formula_range(addr, grid)
    return len(grid)
