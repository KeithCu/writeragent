# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared analysis constants — importable without pandas/scipy on the LO host."""
from __future__ import annotations

HELPER_NAMES = frozenset(
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
