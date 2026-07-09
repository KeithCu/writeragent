# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Format trusted analysis helper results for multi-cell Calc sheet egress."""

from __future__ import annotations

from typing import Any

from plugin.calc.tabular_egress import (
    calc_anchor_from_selection,
    format_tabular_helper_for_calc,
    insert_tabular_result_into_calc,
)
from plugin.scripting.analysis import HELPER_NAMES

__all__ = [
    "calc_anchor_from_selection",
    "format_analysis_for_calc",
    "insert_analysis_result_into_calc",
    "is_analysis_result",
]


def is_analysis_result(value: Any) -> bool:
    """True when *value* matches the compact analysis helper result contract."""
    if not isinstance(value, dict):
        return False
    if "status" not in value:
        return False
    helper = value.get("helper")
    if isinstance(helper, str) and helper in HELPER_NAMES:
        return True
    if value.get("status") == "error":
        code = str(value.get("code") or "")
        return code == "ANALYSIS_ERROR" or "ANALYSIS" in code or code == "MISSING_PARAM"
    return False


def format_analysis_for_calc(result: dict[str, Any]) -> list[list[Any]]:
    """Turn an analysis helper result dict into a row-major grid for ``write_formula_range``."""
    return format_tabular_helper_for_calc(
        result,
        domain_label="Analysis",
        default_helper="analysis",
        failed_message="Analysis failed.",
        metadata_keys=("n_rows", "n_cols", "numeric_cols", "categorical_cols", "datetime_cols"),
    )


def insert_analysis_result_into_calc(
    doc: Any,
    uno_ctx: Any,
    result: dict[str, Any],
    *,
    start_col: int | None = None,
    start_row: int | None = None,
) -> int:
    """Write formatted analysis output starting at *start_col*/*start_row* (or selection). Returns row count."""
    grid = format_analysis_for_calc(result)
    return insert_tabular_result_into_calc(
        doc,
        uno_ctx,
        grid,
        start_col=start_col,
        start_row=start_row,
    )
