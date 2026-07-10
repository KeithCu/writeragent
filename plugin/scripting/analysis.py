# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Analysis helper templates and constants (host / LO process).

Compute is lazy-loaded from ``plugin.scripting.venv.analysis`` via ``__getattr__``.
"""

from __future__ import annotations

from typing import Any

from plugin.scripting._lazy_venv import make_getattr, venv_attr
from plugin.scripting.helper_domain import (
    HelperScriptMeta,
    build_helper_script_template,
    header_prefix,
    parse_helper_script_header,
)

# --- Constants & Common ---

from plugin.scripting.calc_functions_common import (
    ANALYSIS_HELPER_NAMES as HELPER_NAMES,
    ANALYSIS_MAX_TABLE_ROWS as MAX_TABLE_ROWS,
)

ANALYSIS_HEADER_PREFIX = header_prefix("analysis")

_DEFAULT_PARAMS: dict[str, dict[str, Any]] = {
    "describe_data": {},
    "kpi_summary": {"metrics": ["Column1"]},
    "detect_outliers": {},
    "quick_stats": {},
    "format_currency": {},
    "format_percent": {},
    "clean_and_prepare": {},
    "pivot_aggregate": {"index": "Category", "values": "Amount"},
    "group_summary": {"by": "Region", "metrics": ["Sales"]},
    "compare_periods": {"date_col": "Date", "value_col": "Amount"},
    "correlation_matrix": {},
    "run_regression": {"target": "Y", "features": ["X1", "X2"]},
    "cluster_numeric": {},
    "monte_carlo": {},
}

_HELPER_DESCRIPTIONS: dict[str, str] = {
    "describe_data": "Extended EDA and column quality summary",
    "kpi_summary": "Mean/min/max/sum for selected numeric columns",
    "detect_outliers": "Flag outlier rows (IQR, z-score, or isolation forest)",
    "quick_stats": "Compact metric card for numeric columns",
    "format_currency": "Format values as currency strings",
    "format_percent": "Format values as percentage strings",
    "clean_and_prepare": "Light dedupe and imputation",
    "pivot_aggregate": "Pivot table aggregate",
    "group_summary": "Group-by aggregates",
    "compare_periods": "Period-over-period change (YoY/QoQ/MoM)",
    "correlation_matrix": "Pairwise correlations and top pairs",
    "run_regression": "OLS linear regression (R² and coefficients)",
    "cluster_numeric": "KMeans clustering on numeric columns",
    "monte_carlo": "Resampling simulation on a numeric series",
}


_ANALYSIS_VENV_EXPORTS = frozenset(
    {
        "QuickStats",
        "clean_and_prepare",
        "cluster_numeric",
        "compare_periods",
        "correlation_matrix",
        "describe_data",
        "detect_outliers",
        "format_currency",
        "format_percent",
        "group_summary",
        "kpi_summary",
        "monte_carlo",
        "pivot_aggregate",
        "run_analysis",
        "run_regression",
        "CoerceResult",
        "coerce_to_dataframe",
    }
)


def _analysis_venv_extra(name: str) -> Any:
    if name in frozenset({"CoerceResult", "coerce_to_dataframe"}):
        return venv_attr("coerce", name)
    raise AttributeError(f"module 'plugin.scripting.analysis' has no attribute {name!r}")


__getattr__ = make_getattr("analysis", _ANALYSIS_VENV_EXPORTS - frozenset({"CoerceResult", "coerce_to_dataframe"}), fallback=_analysis_venv_extra)


# --- Templates ---

AnalysisScriptMeta = HelperScriptMeta


def _template_body(helper: str, params: dict[str, Any]) -> str:
    return build_helper_script_template(
        tag="analysis",
        helper=helper,
        params=params,
        description=_HELPER_DESCRIPTIONS.get(helper, helper),
        style="run_import",
        import_module="writeragent.scripting.analysis",
        run_name="run_analysis",
        data_expr="data",
        context_expr="{}",
        extra_comment_lines=("# Set the data range in the toolbar (or select cells), then Run.",),
    )


def get_analysis_script_templates() -> dict[str, str]:
    """Return built-in analysis helper scripts keyed by helper name."""
    return {helper: _template_body(helper, dict(_DEFAULT_PARAMS.get(helper, {}))) for helper in sorted(HELPER_NAMES)}


def parse_analysis_script_header(code: str) -> AnalysisScriptMeta | None:
    """Parse the machine-readable header from a built-in or copied analysis script."""
    return parse_helper_script_header(code, tag="analysis", helper_names=HELPER_NAMES)
