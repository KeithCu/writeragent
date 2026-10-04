# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Format trusted analysis helper results for multi-cell Calc sheet egress."""

from __future__ import annotations

from typing import Any

from plugin.calc.tabular_egress import calc_anchor_from_selection, format_tabular_helper_for_calc, insert_tabular_result_into_calc
from plugin.scripting.analysis import HELPER_NAMES
from plugin.scripting.helper_domain import is_status_helper_result

__all__ = ["calc_anchor_from_selection", "format_analysis_for_calc", "insert_analysis_result_into_calc", "is_analysis_result"]


def is_analysis_result(value: Any) -> bool:
    """True when *value* matches the compact analysis helper result contract."""
    return is_status_helper_result(
        value,
        HELPER_NAMES,
        frozenset({"ANALYSIS_ERROR", "MISSING_PARAM"}),
    )


def format_analysis_for_calc(result: dict[str, Any]) -> list[list[Any]]:
    """Turn an analysis helper result dict into a row-major grid for ``write_formula_range``."""
    return format_tabular_helper_for_calc(result, domain_label="Analysis", default_helper="analysis", failed_message="Analysis failed.", metadata_keys=("n_rows", "n_cols", "numeric_cols", "categorical_cols", "datetime_cols"))


def insert_analysis_result_into_calc(doc: Any, uno_ctx: Any, result: dict[str, Any], *, sheet_name: str | None = None, start_col: int | None = None, start_row: int | None = None) -> int:
    """Write formatted analysis output starting at *start_col*/*start_row* (or selection). Returns row count."""
    # Invariant: If Stop happens during a document mutation (inserting a picture, plot, or text),
    # let that mutation finish. Do not guard right before the write.
    grid = format_analysis_for_calc(result)
    return insert_tabular_result_into_calc(doc, uno_ctx, grid, sheet_name=sheet_name, start_col=start_col, start_row=start_row)
