# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Forecast → viz auto-plot mapping for forecast_data."""

from __future__ import annotations

import logging
from typing import Any

from plugin.framework.errors import reraise_if_disposed

log = logging.getLogger(__name__)


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

    date_col = str((forecast_params or {}).get("date_col", "Date"))
    value_col = str((forecast_params or {}).get("value_col", "Value"))

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

    if not (isinstance(grid, list) and grid and isinstance(grid[0], list)):
        return None
    hist_headers = [str(c) for c in grid[0]]
    hist_records = grid[1:]
    if date_col not in hist_headers or value_col not in hist_headers:
        return None

    date_idx = hist_headers.index(date_col)
    val_idx = hist_headers.index(value_col)

    # First match, same as list.index. A repeated name must not switch to the later column.
    idx: dict[str, int] = {}
    for i, col in enumerate(fc_columns_str):
        if col not in idx:
            idx[col] = i
    if "date" not in idx or "forecast" not in idx:
        return None
    extra = [c for c in ("lower", "upper") if c in idx]
    plot_cols = ["date", value_col, "forecast", *extra]

    hist_rows: list[list[Any]] = []
    for rec in hist_records:
        if len(rec) <= max(date_idx, val_idx):
            continue
        hist_rows.append([rec[date_idx], rec[val_idx], None] + [None] * len(extra))

    fc_rows: list[list[Any]] = []
    for row in fc_rows_raw:
        if not isinstance(row, list) or len(row) <= max(idx["date"], idx["forecast"]):
            continue
        fc_rows.append(
            [
                row[idx["date"]],
                None,
                row[idx["forecast"]],
                *(row[idx[c]] if len(row) > idx[c] else None for c in extra),
            ]
        )

    return [plot_cols, *hist_rows, *fc_rows]


def run_auto_plot_after_forecast(uno_ctx: Any, doc: Any, *, forecast_helper: str, forecast_result: dict[str, Any], forecast_params: dict[str, Any] | None, data_range: str | None, task_hint: str | None) -> dict[str, Any] | None:
    """Run time_series_plot with merged history + forecast."""
    try:
        if _forecast_table(forecast_result) is None:
            return {"status": "error", "message": f"No plot mapping for forecast helper {forecast_helper!r}"}

        from plugin.calc.calc_addin_data import _resolve_python_data
        from plugin.framework.queue_executor import run_on_main

        # Sheet read is UNO. Viz IPC (run_trusted_viz) stays on the caller.
        py_data, err = run_on_main(lambda: _resolve_python_data(doc, data_range=data_range, data=None))
        if err:
            return {"status": "error", "message": f"Could not read forecast history: {err}"}
        if py_data is None:
            return {"status": "error", "message": "Could not read forecast history"}

        merged = merge_forecast_plot_data(py_data, forecast_result, forecast_params)
        if merged is None:
            return {"status": "error", "message": "Could not merge forecast history with the forecast table"}

        header = merged[0]
        viz_params: dict[str, Any] = {"date_col": "date", "value_col": header[1], "forecast_col": "forecast"}
        if "lower" in header:
            viz_params["lower_col"] = "lower"
        if "upper" in header:
            viz_params["upper_col"] = "upper"

        from plugin.scripting.viz import run_trusted_viz

        return run_trusted_viz(uno_ctx, doc, helper="time_series_plot", params=viz_params, data=merged, data_range=None, task_hint=task_hint)
    except Exception as exc:
        reraise_if_disposed(exc, object_type="calc")
        log.exception("Forecast auto-plot failed")
        return {"status": "error", "message": f"Auto-plot failed: {exc}"}
