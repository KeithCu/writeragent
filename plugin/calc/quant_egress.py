# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Format trusted quant helper results for multi-cell Calc sheet egress."""

from __future__ import annotations

from typing import Any

from plugin.calc.python.function import to_calc_compatible
from plugin.calc.tabular_egress import (
    _append_blank,
    _append_flag_lines,
    _append_key_value_block,
    _append_no_tabular_output,
    _append_table_rows,
    _error_grid,
    insert_tabular_result_into_calc,
)
from plugin.scripting.calc_functions_common import QUANT_HELPER_NAMES
from plugin.scripting.helper_domain import is_status_helper_result


def is_quant_result(value: Any) -> bool:
    """True when *value* matches the compact quant helper result contract."""
    # No host-side error code: every quant error carries helper= or a helper name.
    return is_status_helper_result(value, QUANT_HELPER_NAMES, frozenset())


def format_quant_for_calc(result: dict[str, Any]) -> list[list[Any]]:
    """Turn a quant helper result dict into a row-major grid for ``write_formula_range``."""
    rows: list[list[Any]] = []

    if result.get("status") == "error":
        return _error_grid(result, "Quant", "Quant failed.")

    helper = str(result.get("helper") or "quant")
    rows.append([f"Quant Result: {helper}"])

    metrics = result.get("metrics")
    if isinstance(metrics, dict):
        _append_key_value_block(rows, "Portfolio Metrics", metrics)

    warning = result.get("warning")
    if isinstance(warning, str) and warning.strip():
        _append_flag_lines(rows, [warning.strip()])

    weights = result.get("weights")
    if isinstance(weights, dict):
        _append_key_value_block(rows, "Optimized Weights", weights)

    table = result.get("table")
    if isinstance(table, dict) and (table.get("columns") or table.get("rows")):
        _append_blank(rows)
        _append_table_rows(rows, table.get("columns"), table.get("rows"), to_calc_compatible)

    _append_no_tabular_output(rows)
    return rows


def insert_quant_result_into_calc(doc: Any, uno_ctx: Any, result: dict[str, Any], *, start_col: int | None = None, start_row: int | None = None) -> int:
    """Write formatted quant output starting at *start_col*/*start_row* (or selection). Returns row count."""
    grid = format_quant_for_calc(result)
    return insert_tabular_result_into_calc(doc, uno_ctx, grid, start_col=start_col, start_row=start_row)
