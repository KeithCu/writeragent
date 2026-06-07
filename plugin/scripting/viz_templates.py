# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Built-in Run Python Script templates for trusted viz helpers."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from plugin.scripting.viz_common import HELPER_NAMES

VIZ_HEADER_PREFIX = "# writeragent:viz"
_VIZ_HEADER_RE = re.compile(
    r"^\s*#\s*writeragent:viz\s+helper=(\w+)\s+params=(\{.*\})\s*$",
    re.MULTILINE,
)

_SHIPPED_TEMPLATES = frozenset({"quick_plot", "correlation_heatmap", "time_series_plot"})

_DEFAULT_PARAMS: dict[str, dict[str, Any]] = {
    "quick_plot": {},
    "correlation_heatmap": {"method": "pearson"},
    "time_series_plot": {"date_col": "Date", "value_col": "Amount"},
}

_HELPER_DESCRIPTIONS: dict[str, str] = {
    "quick_plot": "Auto line/bar chart from numeric columns in the data range.",
    "correlation_heatmap": "Heatmap of pairwise correlations (matplotlib/seaborn).",
    "time_series_plot": "Line plot for date_col vs value_col.",
}


@dataclass(frozen=True)
class VizScriptMeta:
    helper: str
    params: dict[str, Any]


def _template_body(helper: str, params: dict[str, Any]) -> str:
    params_json = json.dumps(params, separators=(",", ":"))
    desc = _HELPER_DESCRIPTIONS.get(helper, helper)
    return (
        f"{VIZ_HEADER_PREFIX} helper={helper} params={params_json}\n"  # nosec
        f"# {desc}\n"
        f"# Set the data range in the toolbar (or select cells), then Run.\n"
        f"from plugin.scripting.viz import run_viz\n\n"
        f"result = run_viz(\n"
        f'    {{"helper": "{helper}", "params": {params_json}}},\n'
        f"    data,\n"
        f"    {{}},\n"
        f")\n"
    )


def get_viz_script_templates() -> dict[str, str]:
    """Return built-in viz helper scripts keyed by helper name."""
    return {
        helper: _template_body(helper, dict(_DEFAULT_PARAMS.get(helper, {})))
        for helper in sorted(_SHIPPED_TEMPLATES)
        if helper in HELPER_NAMES
    }


def parse_viz_script_header(code: str) -> VizScriptMeta | None:
    """Parse the machine-readable header from a built-in or copied viz script."""
    if not code or VIZ_HEADER_PREFIX not in code:
        return None
    match = _VIZ_HEADER_RE.search(code)
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
    return VizScriptMeta(helper=helper, params=params)
