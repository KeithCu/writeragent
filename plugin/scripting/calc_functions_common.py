# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared calc_functions constants — importable without numpy on the LO host."""
from __future__ import annotations

# --- Scripting Domain Constants ---

ANALYSIS_HELPER_NAMES = frozenset(
    {
        "describe_data",
        "kpi_summary",
        "detect_outliers",
        "quick_stats",
        "format_currency",
        "format_percent",
        "clean_and_prepare",
        "pivot_aggregate",
        "group_summary",
        "compare_periods",
        "correlation_matrix",
        "run_regression",
        "cluster_numeric",
        "monte_carlo",
    }
)
ANALYSIS_MAX_TABLE_ROWS = 50
# Cleaned sheets, pivots, and decompositions are the user's data, not an LLM
# summary. 50 rows used to drop the rest with only a truncated flag the sheet
# egress buried. 200 matches the DuckDB result cap and still bounds a chat reply.
ANALYSIS_DATA_TABLE_ROWS = 200
ANALYSIS_MAX_COLS = 40

VIZ_HELPER_NAMES = frozenset(
    {
        "quick_plot",
        "plot_data",
        "correlation_heatmap",
        "time_series_plot",
    }
)

SYMBOLIC_HELPER_NAMES = frozenset(
    {
        "solve_equation",
        "symbolic_simplify",
        "integrate",
        "differentiate",
        "latex_to_math_object",
    }
)

UNITS_HELPER_NAMES = frozenset(
    {
        "convert_quantity",
        "parse_quantity",
        "format_quantity",
        "check_dimensionality",
    }
)

TEXT_ANALYTICS_HELPER_NAMES = frozenset(
    {
        "full",
        "readability",
        "entities",
        "key_phrases",
        "topics",
        "sentiment",
        "diagnostics",
        "check",
    }
)

QUANT_HELPER_NAMES = frozenset(
    {
        "fetch_historical_data",
        "technical_analysis",
        "portfolio_tearsheet",
        "efficient_frontier",
    }
)

FORECAST_HELPER_NAMES = frozenset(
    {
        "forecast_time_series",
        "decompose_time_series",
        "anomaly_detection_time_series",
    }
)
OPTIMIZE_HELPER_NAMES = frozenset(
    {
        "optimize_portfolio",
        "linear_programming",
        "solve_scheduling_problem",
    }
)

