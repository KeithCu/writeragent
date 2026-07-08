# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Forecast → viz auto-plot mapping for forecast_data."""

from __future__ import annotations

from typing import Any

from plugin.calc.viz_auto_plot import task_hint_implies_plot
from plugin.scripting.viz import HELPER_NAMES

AUTO_PLOT_FORECAST_HELPERS = frozenset({"forecast_time_series"})


def should_auto_plot(*, helper: str, auto_plot: bool, task_hint: str | None) -> bool:
    if helper not in AUTO_PLOT_FORECAST_HELPERS:
        return False
    return bool(auto_plot) or task_hint_implies_plot(task_hint)


def _forecast_table(forecast_result: dict[str, Any]) -> dict[str, Any] | None:
    tables = forecast_result.get("tables")
    if not isinstance(tables, list):
        return None
    for table in tables:
        if isinstance(table, dict) and table.get("name") == "forecast":
            return table
    return None


def merge_forecast_plot_data(
    history_data: Any,
    forecast_result: dict[str, Any],
    forecast_params: dict[str, Any] | None,
) -> list[list[Any]] | None:
    """Merge historical range data with forecast table rows for band plotting."""
    import pandas as pd

    forecast_table = _forecast_table(forecast_result)
    if forecast_table is None:
        return None

    columns = forecast_table.get("columns")
    rows = forecast_table.get("rows")
    if not isinstance(columns, list) or not isinstance(rows, list) or not columns or not rows:
        return None

    params = dict(forecast_params or {})
    date_col = str(params.get("date_col", "Date"))
    value_col = str(params.get("value_col", "Value"))

    if hasattr(history_data, "columns"):
        hist_df = history_data.copy()
    else:
        from plugin.scripting.venv.coerce import coerce_to_dataframe

        hist_df = coerce_to_dataframe(history_data, headers=True).df

    if date_col not in hist_df.columns or value_col not in hist_df.columns:
        return None

    fc_df = pd.DataFrame(rows, columns=pd.Index([str(c) for c in columns]))
    has_lower = "lower" in fc_df.columns
    has_upper = "upper" in fc_df.columns

    plot_cols = ["date", value_col, "forecast"]
    if has_lower:
        plot_cols.append("lower")
    if has_upper:
        plot_cols.append("upper")

    hist_rows: list[list[Any]] = []
    for _, row in hist_df[[date_col, value_col]].iterrows():
        entry: list[Any] = [row[date_col], row[value_col], None]
        if has_lower:
            entry.append(None)
        if has_upper:
            entry.append(None)
        hist_rows.append(entry)

    fc_rows: list[list[Any]] = []
    for _, row in fc_df.iterrows():
        entry = [row.get("date"), None, row.get("forecast")]
        if has_lower:
            entry.append(row.get("lower"))
        if has_upper:
            entry.append(row.get("upper"))
        fc_rows.append(entry)

    return [plot_cols, *hist_rows, *fc_rows]


def build_viz_request(
    forecast_helper: str,
    *,
    forecast_result: dict[str, Any],
    forecast_params: dict[str, Any] | None,
) -> tuple[str, dict[str, Any]] | None:
    """Return (viz_helper, viz_params) for a completed forecast result."""
    if forecast_helper != "forecast_time_series":
        return None

    forecast_table = _forecast_table(forecast_result)
    if forecast_table is None:
        return None

    params = dict(forecast_params or {})
    value_col = str(params.get("value_col", "Value"))
    columns = forecast_table.get("columns") or []
    viz_params: dict[str, Any] = {
        "date_col": "date",
        "value_col": value_col,
        "forecast_col": "forecast",
    }
    if "lower" in columns:
        viz_params["lower_col"] = "lower"
    if "upper" in columns:
        viz_params["upper_col"] = "upper"
    return "time_series_plot", viz_params


def run_auto_plot_after_forecast(
    uno_ctx: Any,
    doc: Any,
    *,
    forecast_helper: str,
    forecast_result: dict[str, Any],
    forecast_params: dict[str, Any] | None,
    data_range: str | None,
    auto_plot: bool,
    task_hint: str | None,
) -> dict[str, Any] | None:
    """Run time_series_plot with merged history + forecast when auto-plot triggers."""
    if forecast_result.get("status") != "ok":
        return None
    if not should_auto_plot(helper=forecast_helper, auto_plot=auto_plot, task_hint=task_hint):
        return None
    request = build_viz_request(
        forecast_helper,
        forecast_result=forecast_result,
        forecast_params=forecast_params,
    )
    if request is None:
        return None
    viz_helper, viz_params = request
    if viz_helper not in HELPER_NAMES:
        return None

    from plugin.scripting.forecast import calc_tool_context
    from plugin.calc.python.venv import _resolve_python_data

    tool_ctx = calc_tool_context(uno_ctx, doc)
    py_data, err = _resolve_python_data(tool_ctx, data_range=data_range, data=None)
    if err or py_data is None:
        return None

    merged = merge_forecast_plot_data(py_data, forecast_result, forecast_params)
    if merged is None:
        return None

    from plugin.scripting.viz import run_trusted_viz

    return run_trusted_viz(
        uno_ctx,
        doc,
        helper=viz_helper,
        params=viz_params,
        data=merged,
        data_range=None,
        task_hint=task_hint,
    )
