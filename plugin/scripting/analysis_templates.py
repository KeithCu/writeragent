# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Built-in Run Python Script templates for trusted analysis helpers."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from plugin.scripting.analysis_common import HELPER_NAMES

ANALYSIS_HEADER_PREFIX = "# writeragent:analysis"
_ANALYSIS_HEADER_RE = re.compile(
    r"^\s*#\s*writeragent:analysis\s+helper=(\w+)\s+params=(\{.*\})\s*$",
    re.MULTILINE,
)

# Default params for helpers that require structured kwargs (edit column names before Run).
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


@dataclass(frozen=True)
class AnalysisScriptMeta:
    helper: str
    params: dict[str, Any]


def _template_body(helper: str, params: dict[str, Any]) -> str:
    params_json = json.dumps(params, separators=(",", ":"))
    desc = _HELPER_DESCRIPTIONS.get(helper, helper)
    return (
        f"{ANALYSIS_HEADER_PREFIX} helper={helper} params={params_json}\n"  # nosec B608
        f"# {desc}\n"
        f"# Set the data range in the toolbar (or select cells), then Run.\n"
        f"from plugin.scripting.analysis import run_analysis\n\n"
        f"result = run_analysis(\n"
        f'    {{"helper": "{helper}", "params": {params_json}}},\n'
        f"    data,\n"
        f"    {{}},\n"
        f")\n"
    )


def get_analysis_script_templates() -> dict[str, str]:
    """Return built-in analysis helper scripts keyed by helper name."""
    return {helper: _template_body(helper, dict(_DEFAULT_PARAMS.get(helper, {}))) for helper in sorted(HELPER_NAMES)}


def parse_analysis_script_header(code: str) -> AnalysisScriptMeta | None:
    """Parse the machine-readable header from a built-in or copied analysis script."""
    if not code or ANALYSIS_HEADER_PREFIX not in code:
        return None
    match = _ANALYSIS_HEADER_RE.search(code)
    if not match:
        return None
    helper = match.group(1)
    if helper not in HELPER_NAMES:
        return None
    try:
        params = json.loads(match.group(2))
    except json.JSONDecodeError:
        params = {}
    if not isinstance(params, dict):
        params = {}
    return AnalysisScriptMeta(helper=helper, params=params)
