# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Format trusted quant helper results for multi-cell Calc sheet egress."""

from __future__ import annotations

from typing import Any

from plugin.calc.python.function import to_calc_compatible
from plugin.calc.tabular_egress import insert_tabular_result_into_calc
from plugin.scripting.calc_functions_common import QUANT_HELPER_NAMES
from plugin.scripting.helper_domain import is_status_helper_result


def is_quant_result(value: Any) -> bool:
    """True when *value* matches the compact quant helper result contract.

    ``and`` binds tighter than ``or``, so the old check treated every
    ``fetch_*`` string as a quant helper even when it was not in
    ``QUANT_HELPER_NAMES``.
    """
    return is_status_helper_result(value, QUANT_HELPER_NAMES, frozenset({"QUANT_ERROR"}))


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


def format_quant_for_calc(result: dict[str, Any]) -> list[list[Any]]:
    """Turn a quant helper result dict into a row-major grid for ``write_formula_range``."""
    rows: list[list[Any]] = []

    if result.get("status") == "error":
        code = str(result.get("code") or "ERROR")
        message = str(result.get("message") or "Quant failed.")
        return [[f"Quant error ({code})"], [message]]

    helper = str(result.get("helper") or "quant")
    rows.append([f"Quant Result: {helper}"])

    metrics = result.get("metrics")
    if isinstance(metrics, dict) and metrics:
        _append_key_value_block(rows, "Portfolio Metrics", metrics)

    weights = result.get("weights")
    if isinstance(weights, dict) and weights:
        _append_key_value_block(rows, "Optimized Weights", weights)

    table = result.get("table")
    if isinstance(table, dict):
        _append_blank(rows)
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

    if len(rows) == 1:
        rows.append(["(no tabular output)"])
    return rows


def insert_quant_result_into_calc(doc: Any, uno_ctx: Any, result: dict[str, Any], *, start_col: int | None = None, start_row: int | None = None) -> int:
    """Write formatted quant output starting at *start_col*/*start_row* (or selection). Returns row count."""
    grid = format_quant_for_calc(result)
    return insert_tabular_result_into_calc(doc, uno_ctx, grid, start_col=start_col, start_row=start_row)
