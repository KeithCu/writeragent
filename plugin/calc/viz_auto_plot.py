# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Analysis → viz auto-plot mapping for analyze_data."""

from __future__ import annotations

import re
from typing import Any

from plugin.framework.errors import reraise_if_disposed
from plugin.framework.queue_executor import main
from plugin.scripting.helper_domain import run_domain
from plugin.scripting.viz import insert_viz_result_into_doc

AUTO_PLOT_ANALYSIS_HELPERS = frozenset({"run_regression", "cluster_numeric", "monte_carlo", "correlation_matrix"})

_VIZ_HINT_RE = re.compile(r"\b(chart|plot|visual|graph|distribution|histogram|heatmap)\b", re.I)


def task_hint_implies_plot(task_hint: str | None) -> bool:
    return bool(task_hint and _VIZ_HINT_RE.search(task_hint))


def should_auto_plot(*, helper: str, auto_plot: bool, task_hint: str | None) -> bool:
    return helper in AUTO_PLOT_ANALYSIS_HELPERS and (auto_plot or task_hint_implies_plot(task_hint))


def apply_auto_plot_result(
    result: dict[str, Any],
    plot_result: dict[str, Any] | None,
    uno_ctx: Any,
    doc: Any,
) -> dict[str, Any]:
    """Attach plot result, run main-thread image insert, and record image_inserted, and plot_error on failure."""
    if plot_result is None:
        return result

    out = dict(result)
    out["plot"] = plot_result
    if plot_result.get("status") == "ok":
        # The plot rides on another tool's result: a failed insert must leave the
        # analysis status ok, and a closed document must propagate.
        # insert_plots_and_report (python/venv.py) does neither, so this keeps
        # its own short tail with the same keys (image_inserted, plot_error).
        try:
            main.call(lambda: insert_viz_result_into_doc(uno_ctx, doc, plot_result))
            out["image_inserted"] = True
        except Exception as exc:
            # Propagate document disposal; ordinary insertion errors are recorded on the result.
            reraise_if_disposed(exc, object_type="calc")
            out["image_inserted"] = False
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


def run_auto_plot_after_analysis(uno_ctx: Any, doc: Any, *, analysis_helper: str, analysis_result: dict[str, Any], analysis_params: dict[str, Any] | None, data_range: str | None, task_hint: str | None) -> dict[str, Any] | None:
    """Run a viz helper; return viz result or None."""
    request = build_viz_request(analysis_helper, analysis_result=analysis_result, analysis_params=analysis_params)
    if request is None:
        return None
    viz_helper, viz_params = request
    return run_domain("viz", uno_ctx, doc, helper=viz_helper, params=viz_params, data_range=data_range, task_hint=task_hint)
