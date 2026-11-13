# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
# SPDX-License-Identifier: GPL-3.0-or-later
"""Calc forecasting tool: trusted helpers for time-series tasks."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from plugin.framework.tool import ToolBaseDummy
from plugin.scripting.helper_registry import names, specs

if TYPE_CHECKING:
    from plugin.framework.tool import ToolContext

_FORECAST_SPECS = specs("forecast")
_FORECAST_DATA_HELPERS = ", ".join(sorted(names("forecast")))
_PARAM_TEXT = " ".join(f"{s.name}: {s.param_help}" for s in sorted(_FORECAST_SPECS, key=lambda s: s.name) if s.param_help)


class ForecastDataTool(ToolBaseDummy):
    """Run trusted forecasting helpers on sheet data via the venv worker."""

    name: str | None = "forecast_data"
    description: str = (
        "Run a trusted time-series forecasting helper on spreadsheet data. "
        f"Helpers: {_FORECAST_DATA_HELPERS}. "
        "Use data_range (A1 address string, e.g. 'Sheet1.A1:D1000') for bulk data. "
        "The host extracts and shapes the data before it reaches the forecasting code. "
        "This tool is intended for the analysis domain; pass range addresses only. "
        f"Parameter reference: {_PARAM_TEXT}"
    )
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "helper": {"type": "string", "description": "Forecast helper name (e.g. forecast_time_series, decompose_time_series)."},
            "params": {"type": "object", "description": _PARAM_TEXT},
            "data_range": {"type": "string", "description": "A1 range address (e.g. 'Sheet1.A1:D1000'). This is the only way to supply data. The host extracts the values out-of-band."},
            "output_range": {"type": "string", "description": "Optional A1 anchor cell to write formatted results (Calc only)."},
            "headers": {"type": "boolean", "description": "First row contains column names (default true)."},
            "task_hint": {"type": "string", "description": "Optional hint echoed in result context."},
            "auto_plot": {"type": "boolean", "description": "When true (or task_hint mentions chart/plot), insert a forecast band chart on Calc after table egress (default false)."},
        },
        "required": ["helper"],
    }
    long_running: bool = True

    def is_async(self) -> bool:
        return True

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        from plugin.calc.analysis import run_calc_data_tool, write_anchor_result
        from plugin.scripting.forecast import insert_forecast_result_into_calc

        args, result = run_calc_data_tool(ctx, "forecast", kwargs)
        result = write_anchor_result(ctx, result, args.output_range, insert_forecast_result_into_calc)

        if result.get("status") == "ok" and ctx.doc_type == "calc":
            auto_plot = bool(kwargs.get("auto_plot", False))
            from plugin.calc.forecast_auto_plot import run_auto_plot_after_forecast
            from plugin.calc.viz_auto_plot import apply_auto_plot_result, task_hint_implies_plot

            plot_result = None
            if args.helper == "forecast_time_series" and (auto_plot or task_hint_implies_plot(args.task_hint)):
                # Call run_auto_plot_after_forecast on this worker. It
                # marshals the sheet read itself. Wrapping it in
                # main.call runs the matplotlib IPC on the UI
                # thread. insert_viz_result_into_doc below stays on main.
                plot_result = run_auto_plot_after_forecast(
                    ctx.ctx,
                    ctx.doc,
                    forecast_helper=args.helper,
                    forecast_result=result,
                    forecast_params=args.params,
                    data_range=args.data_range,
                    task_hint=args.task_hint,
                )
            result = apply_auto_plot_result(result, plot_result, ctx.ctx, ctx.doc)

        return result
