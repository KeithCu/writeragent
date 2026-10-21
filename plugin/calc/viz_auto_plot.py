# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Analysis → viz auto-plot mapping for analyze_data."""

from __future__ import annotations

import re
from typing import Any, Callable

AUTO_PLOT_ANALYSIS_HELPERS = frozenset({"run_regression", "cluster_numeric", "monte_carlo", "correlation_matrix"})

_VIZ_HINT_RE = re.compile(r"\b(chart|plot|visual|graph|distribution|histogram|heatmap)\b", re.I)


def task_hint_implies_plot(task_hint: str | None) -> bool:
    if not task_hint or not str(task_hint).strip():
        return False
    return _VIZ_HINT_RE.search(str(task_hint)) is not None


def helper_should_auto_plot(helper: str, allowed: frozenset[str], *, auto_plot: bool, task_hint: str | None) -> bool:
    if helper not in allowed:
        return False
    return bool(auto_plot) or task_hint_implies_plot(task_hint)


def should_auto_plot(*, helper: str, auto_plot: bool, task_hint: str | None) -> bool:
    return helper_should_auto_plot(helper, AUTO_PLOT_ANALYSIS_HELPERS, auto_plot=auto_plot, task_hint=task_hint)


def apply_auto_plot_result(
    result: dict[str, Any],
    plot_result: dict[str, Any] | None,
    insert: Callable[[dict[str, Any]], Any],
) -> dict[str, Any]:
    """Attach plot result, run main-thread image insert, and record image_inserted or plot_error."""
    if plot_result is None:
        return result
    from plugin.framework.queue_executor import execute_on_main_thread

    out = dict(result)
    out["plot"] = plot_result
    if plot_result.get("status") == "ok":
        try:
            execute_on_main_thread(lambda: insert(plot_result))
            out["image_inserted"] = True
        except Exception as exc:
            out["plot_error"] = str(exc)
    else:
        out["plot_error"] = plot_result.get("message")
    return out


def build_viz_request(analysis_helper: str, *, analysis_result: dict[str, Any], analysis_params: dict[str, Any] | None) -> tuple[str, dict[str, Any]] | None:
    """Return (viz_helper, viz_params) for a completed analysis result."""
    params = dict(analysis_params or {})
    if analysis_helper == "correlation_matrix":
        return "correlation_heatmap", {"method": params.get("method", "pearson")}
    if analysis_helper == "monte_carlo":
        return "plot_data", {"spec": {"chart_type": "histogram", "title": "Monte Carlo distribution"}}
    if analysis_helper == "run_regression":
        target = params.get("target")
        features = params.get("features") or []
        x_col = features[0] if isinstance(features, list) and features else None
        if not target or not x_col:
            return None
        return "plot_data", {"spec": {"chart_type": "scatter", "x": x_col, "y": target, "title": f"Regression: {target} vs {x_col}"}}
    if analysis_helper == "cluster_numeric":
        columns = params.get("columns")
        if isinstance(columns, list) and len(columns) >= 2:
            x_col, y_col = columns[0], columns[1]
        else:
            metadata = analysis_result.get("metadata")
            if not isinstance(metadata, dict):
                return None
            numeric_raw = metadata.get("numeric_cols")
            if not isinstance(numeric_raw, list) or len(numeric_raw) < 2:
                return None
            x_col, y_col = numeric_raw[0], numeric_raw[1]
        return "plot_data", {"spec": {"chart_type": "scatter", "x": x_col, "y": y_col, "title": "Cluster view"}}
    return None


def run_auto_plot_after_analysis(uno_ctx: Any, doc: Any, *, analysis_helper: str, analysis_result: dict[str, Any], analysis_params: dict[str, Any] | None, data_range: str | None, auto_plot: bool, task_hint: str | None) -> dict[str, Any] | None:
    """Run a viz helper when auto-plot triggers; return viz result or None."""
    if analysis_result.get("status") != "ok":
        return None
    if not should_auto_plot(helper=analysis_helper, auto_plot=auto_plot, task_hint=task_hint):
        return None
    request = build_viz_request(analysis_helper, analysis_result=analysis_result, analysis_params=analysis_params)
    if request is None:
        return None
    viz_helper, viz_params = request
    from plugin.scripting.viz import run_trusted_viz

    return run_trusted_viz(uno_ctx, doc, helper=viz_helper, params=viz_params, data_range=data_range, task_hint=task_hint)
