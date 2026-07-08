# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Calc forecasting tool: trusted helpers for time-series tasks."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from plugin.calc.base import ToolCalcAnalysisBase
from plugin.framework.errors import ToolExecutionError
from plugin.scripting.forecast import HELPER_NAMES

if TYPE_CHECKING:
    from plugin.framework.tool import ToolContext

logger = logging.getLogger("writeragent.calc")

_FORECAST_DATA_HELPERS = ", ".join(sorted(HELPER_NAMES))


class ForecastDataTool(ToolCalcAnalysisBase):
    """Run trusted forecasting helpers on sheet data via the venv worker."""

    name = "forecast_data"
    description = (
        "Run a trusted time-series forecasting helper on spreadsheet data. "
        f"Helpers: {_FORECAST_DATA_HELPERS}. "
        "Use data_range (A1 address string, e.g. 'Sheet1.A1:D1000') for bulk data. "
        "The host extracts and shapes the data before it reaches the forecasting code. "
        "This tool is intended for the analysis domain; pass range addresses only."
    )
    parameters = {
        "type": "object",
        "properties": {
            "helper": {"type": "string", "description": "Forecast helper name (e.g. forecast_time_series, decompose_time_series)."},
            "params": {"type": "object", "description": "Helper-specific parameters."},
            "data_range": {"type": "string", "description": "A1 range address to forecast (e.g. 'Sheet1.A1:D1000')."},
            "output_range": {"type": "string", "description": "Optional A1 anchor cell to write formatted results (Calc only)."},
            "headers": {"type": "boolean", "description": "First row contains column names (default true)."},
            "task_hint": {"type": "string", "description": "Optional hint echoed in result context."},
            "auto_plot": {"type": "boolean", "description": "When true (or task_hint mentions chart/plot), insert a forecast band chart on Calc after table egress (default false)."},
        },
        "required": ["helper"],
    }
    long_running = True

    def get_parameters(self, doc_type: str | None = None) -> dict | None:
        import copy
        from typing import cast

        p = copy.deepcopy(self.parameters)
        if p and "properties" in p:
            props = cast("dict[str, Any]", p["properties"])
            props.pop("data", None)
            if "data_range" in props:
                props["data_range"]["description"] = (
                    "A1 range address (e.g. 'Sheet1.A1:D1000'). This is the only way "
                    "to supply data. The host extracts the values out-of-band."
                )
        return p

    def is_async(self) -> bool:
        return True

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        helper = str(kwargs.get("helper") or "").strip()
        if not helper:
            return self._tool_error("helper is required")

        data_range = kwargs.get("data_range")
        data = kwargs.get("data")

        if getattr(ctx, "active_domain", None) in ("analysis", "forecast") and data is not None:
            return self._tool_error(
                "analysis/forecast domain requires data_range (A1 address string) only. "
                "Do not pass raw data values — the host must resolve the range out-of-band."
            )

        if not (data_range and str(data_range).strip()) and data is None:
            return self._tool_error("Provide data_range or data")

        from plugin.scripting.forecast import run_trusted_forecast, insert_forecast_result_into_calc
        from plugin.calc.address_utils import parse_address
        from plugin.framework.queue_executor import execute_on_main_thread

        dr = str(data_range).strip() if data_range else None
        params = kwargs.get("params") if isinstance(kwargs.get("params"), dict) else None
        headers = bool(kwargs.get("headers", True)) if "headers" in kwargs else True
        task_hint = str(kwargs["task_hint"]) if kwargs.get("task_hint") else None
        output_range = str(kwargs["output_range"]).strip() if kwargs.get("output_range") else None

        def _run() -> dict[str, Any]:
            return run_trusted_forecast(
                ctx.ctx,
                ctx.doc,
                helper=helper,
                params=params,
                data_range=dr,
                data=data,
                headers=headers,
                task_hint=task_hint,
            )

        try:
            result = execute_on_main_thread(_run)
        except ToolExecutionError as exc:
            return self._tool_error(str(exc), code=getattr(exc, "code", "FORECAST_ERROR"))
        except Exception as exc:
            return self._tool_error(f"Failed to run forecast: {exc}")

        if output_range and result.get("status") == "ok":
            def _write() -> None:
                cell_part = output_range.rsplit(".", 1)[-1] if output_range else output_range
                col, row = parse_address(cell_part)
                insert_forecast_result_into_calc(ctx.doc, ctx.ctx, result, start_col=col, start_row=row)

            try:
                execute_on_main_thread(_write)
            except Exception as exc:
                return self._tool_error(f"Forecast succeeded but sheet write failed: {exc}")

        if result.get("status") == "ok" and ctx.doc_type == "calc":
            auto_plot = bool(kwargs.get("auto_plot", False))
            from plugin.calc.forecast_auto_plot import run_auto_plot_after_forecast, should_auto_plot
            from plugin.scripting.viz import insert_viz_result_into_doc

            plot_result = None
            if should_auto_plot(helper=helper, auto_plot=auto_plot, task_hint=task_hint):

                def _auto_plot() -> dict[str, Any] | None:
                    return run_auto_plot_after_forecast(
                        ctx.ctx,
                        ctx.doc,
                        forecast_helper=helper,
                        forecast_result=result,
                        forecast_params=params,
                        data_range=dr,
                        auto_plot=auto_plot,
                        task_hint=task_hint,
                    )

                plot_result = execute_on_main_thread(_auto_plot)
            if plot_result is not None:
                result = dict(result)
                result["plot"] = plot_result
                if plot_result.get("status") == "ok":

                    def _insert_plot() -> None:
                        insert_viz_result_into_doc(ctx.ctx, ctx.doc, plot_result)

                    try:
                        execute_on_main_thread(_insert_plot)
                        result["image_inserted"] = True
                    except Exception as exc:
                        result["plot_error"] = str(exc)
                else:
                    result["plot_error"] = plot_result.get("message")

        return result
