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


def merge_forecast_plot_data(history_data: Any, forecast_result: dict[str, Any], forecast_params: dict[str, Any] | None) -> list[list[Any]] | None:
    """Merge historical range data with forecast table rows for band plotting."""
    forecast_table = _forecast_table(forecast_result)
    if forecast_table is None:
        return None

    fc_columns = forecast_table.get("columns")
    fc_rows_raw = forecast_table.get("rows")
    if not isinstance(fc_columns, list) or not isinstance(fc_rows_raw, list) or not fc_columns or not fc_rows_raw:
        return None
    fc_columns_str = [str(c) for c in fc_columns]

    params = dict(forecast_params or {})
    date_col = str(params.get("date_col", "Date"))
    value_col = str(params.get("value_col", "Value"))

    hist_headers = []
    hist_records = []

    if hasattr(history_data, "columns"):
        # history_data is a DataFrame (only happens if tests pass one in)
        hist_headers = list(history_data.columns)
        hist_records = history_data.values.tolist()
    else:
        from plugin.scripting.payload_codec import is_calc_range_payload

        grid = history_data
        if is_calc_range_payload(history_data):
            # Materialize the envelope to its rectangular values first.
            # _resolve_python_data returns a calc_range dict, and
            # coerce_to_dataframe wraps an unrecognized dict as a 1×1 cell,
            # so the Date and Value columns disappear and auto-plot drops
            # the history.
            from plugin.scripting.calc_range import materialize_calc_range

            grid = materialize_calc_range(history_data).values

        if isinstance(grid, list) and len(grid) > 0 and isinstance(grid[0], list):
            hist_headers = [str(c) for c in grid[0]]
            hist_records = grid[1:]

    if date_col not in hist_headers or value_col not in hist_headers:
        return None

    date_idx = hist_headers.index(date_col)
    val_idx = hist_headers.index(value_col)

    has_lower = "lower" in fc_columns_str
    has_upper = "upper" in fc_columns_str

    fc_date_idx = fc_columns_str.index("date") if "date" in fc_columns_str else -1
    fc_forecast_idx = fc_columns_str.index("forecast") if "forecast" in fc_columns_str else -1
    fc_lower_idx = fc_columns_str.index("lower") if has_lower else -1
    fc_upper_idx = fc_columns_str.index("upper") if has_upper else -1

    if fc_date_idx == -1 or fc_forecast_idx == -1:
        return None

    plot_cols = ["date", value_col, "forecast"]
    if has_lower:
        plot_cols.append("lower")
    if has_upper:
        plot_cols.append("upper")

    hist_rows: list[list[Any]] = []
    for rec in hist_records:
        if len(rec) <= max(date_idx, val_idx):
            continue
        entry: list[Any] = [rec[date_idx], rec[val_idx], None]
        if has_lower:
            entry.append(None)
        if has_upper:
            entry.append(None)
        hist_rows.append(entry)

    fc_rows: list[list[Any]] = []
    for row in fc_rows_raw:
        if not isinstance(row, list) or len(row) <= max(fc_date_idx, fc_forecast_idx):
            # If it's a dict, handle it gracefully
            if isinstance(row, dict):
                entry = [row.get("date"), None, row.get("forecast")]
                if has_lower:
                    entry.append(row.get("lower"))
                if has_upper:
                    entry.append(row.get("upper"))
                fc_rows.append(entry)
            continue

        entry = [row[fc_date_idx], None, row[fc_forecast_idx]]
        if has_lower:
            entry.append(row[fc_lower_idx] if len(row) > fc_lower_idx else None)
        if has_upper:
            entry.append(row[fc_upper_idx] if len(row) > fc_upper_idx else None)
        fc_rows.append(entry)

    return [plot_cols, *hist_rows, *fc_rows]


def build_viz_request(forecast_helper: str, *, forecast_result: dict[str, Any], forecast_params: dict[str, Any] | None) -> tuple[str, dict[str, Any]] | None:
    """Return (viz_helper, viz_params) for a completed forecast result."""
    if forecast_helper != "forecast_time_series":
        return None

    forecast_table = _forecast_table(forecast_result)
    if forecast_table is None:
        return None

    params = dict(forecast_params or {})
    value_col = str(params.get("value_col", "Value"))
    columns = forecast_table.get("columns") or []
    viz_params: dict[str, Any] = {"date_col": "date", "value_col": value_col, "forecast_col": "forecast"}
    if "lower" in columns:
        viz_params["lower_col"] = "lower"
    if "upper" in columns:
        viz_params["upper_col"] = "upper"
    return "time_series_plot", viz_params


def run_auto_plot_after_forecast(uno_ctx: Any, doc: Any, *, forecast_helper: str, forecast_result: dict[str, Any], forecast_params: dict[str, Any] | None, data_range: str | None, auto_plot: bool, task_hint: str | None) -> dict[str, Any] | None:
    """Run time_series_plot with merged history + forecast when auto-plot triggers."""
    if forecast_result.get("status") != "ok":
        return None
    if not should_auto_plot(helper=forecast_helper, auto_plot=auto_plot, task_hint=task_hint):
        return None
    request = build_viz_request(forecast_helper, forecast_result=forecast_result, forecast_params=forecast_params)
    if request is None:
        return None
    viz_helper, viz_params = request
    if viz_helper not in HELPER_NAMES:
        return None

    # calc_tool_context lives on plugin.calc.analysis_runner, the same
    # import helper_domain, quant, viz, and python_runner use. Importing it
    # from plugin.scripting.forecast raises AttributeError after a
    # successful forecast.
    from plugin.calc.analysis_runner import calc_tool_context
    from plugin.calc.calc_addin_data import _resolve_python_data
    from plugin.framework.queue_executor import execute_on_main_thread
    from plugin.framework.thread_guard import on_main_thread

    def _read_history() -> tuple[Any, str | None]:
        # Sheet read is UNO. Viz IPC (run_trusted_viz) stays on the caller.
        tool_ctx = calc_tool_context(uno_ctx, doc)
        return _resolve_python_data(tool_ctx, data_range=data_range, data=None)

    if on_main_thread():
        py_data, err = _read_history()
    else:
        py_data, err = execute_on_main_thread(_read_history)
    if err or py_data is None:
        return None

    merged = merge_forecast_plot_data(py_data, forecast_result, forecast_params)
    if merged is None:
        return None

    from plugin.scripting.viz import run_trusted_viz

    return run_trusted_viz(uno_ctx, doc, helper=viz_helper, params=viz_params, data=merged, data_range=None, task_hint=task_hint)
