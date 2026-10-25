# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Analysis helper templates and constants (host / LO process).

Compute is lazy-loaded from ``plugin.scripting.venv.analysis`` via ``__getattr__``.
"""

from __future__ import annotations

from typing import Any

from plugin.scripting._lazy_venv import install_lazy_exports
from plugin.scripting.helper_domain import (
    DomainFacadeConfig,
    make_template_api,
)

# --- Constants & Common ---

from plugin.scripting.calc_functions_common import (
    ANALYSIS_HELPER_NAMES as HELPER_NAMES,
)

_HELPER_META: dict[str, tuple[dict[str, Any], str]] = {
    "describe_data": ({}, "Extended EDA and column quality summary"),
    "kpi_summary": ({"metrics": ["Column1"]}, "Mean/min/max/sum for selected numeric columns"),
    "detect_outliers": ({}, "Flag outlier rows (IQR, z-score, or isolation forest)"),
    "quick_stats": ({}, "Compact metric card for numeric columns"),
    "format_currency": ({"symbol": "$", "decimals": 2}, "Format values as currency strings"),
    "format_percent": ({"decimals": 1}, "Format values as percentage strings"),
    "clean_and_prepare": ({}, "Light dedupe and imputation"),
    "pivot_aggregate": ({"index": "Category", "values": "Amount"}, "Pivot table aggregate"),
    "group_summary": ({"by": "Region", "metrics": ["Sales"]}, "Group-by aggregates"),
    "compare_periods": ({"date_col": "Date", "value_col": "Amount", "agg": "sum"}, "Period-over-period change (YoY/QoQ/MoM); agg is sum, mean, or last"),
    "correlation_matrix": ({}, "Pairwise correlations and top pairs"),
    "run_regression": ({"target": "Y", "features": ["X1", "X2"]}, "OLS linear regression (coefficients, standard errors, p-values, intervals)"),
    "cluster_numeric": ({}, "KMeans on standardized numeric columns, with a row-to-cluster table"),
    "monte_carlo": ({}, "Order-shuffle simulation: bust and goal from the cumulative path"),
}

_DEFAULT_PARAMS: dict[str, dict[str, Any]] = {name: params for name, (params, _description) in _HELPER_META.items()}
_HELPER_DESCRIPTIONS: dict[str, str] = {name: description for name, (_params, description) in _HELPER_META.items()}


_ANALYSIS_VENV_EXPORTS = HELPER_NAMES | {"QuickStats", "run_analysis"}


_ANALYSIS_COERCE_EXPORTS = frozenset({"CoerceResult", "coerce_to_dataframe", "grid_to_dataframe"})

install_lazy_exports(
    globals(),
    "analysis",
    _ANALYSIS_VENV_EXPORTS,
    extra={"coerce": _ANALYSIS_COERCE_EXPORTS},
)


# --- Templates ---


_API = make_template_api(
    DomainFacadeConfig(
        tag="analysis",
        helper_names=HELPER_NAMES,
        default_params=_DEFAULT_PARAMS,
        descriptions=_HELPER_DESCRIPTIONS,
        import_module="writeragent.scripting.analysis",
        run_name="run_analysis",
        leading_data=True,
    )
)

get_analysis_script_templates = _API.get_templates
parse_analysis_script_header = _API.parse_header
