# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Calc chat tool for trusted visualization helpers."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from plugin.framework.tool import ToolBaseDummy
from plugin.framework.queue_executor import main
from plugin.scripting.helper_registry import names
from plugin.scripting.viz import insert_viz_result_into_doc

if TYPE_CHECKING:
    from plugin.framework.tool import ToolContext

_VIZ_HELPERS = ", ".join(sorted(names("viz")))


class PlotDataTool(ToolBaseDummy):
    """Run trusted matplotlib/seaborn plot helpers on spreadsheet data."""

    name: str | None = "plot_data"
    description: str = f"Run a trusted visualization helper on spreadsheet data. Helpers: {_VIZ_HELPERS}. Use data_range (A1 address string). On Calc, the chart inserts on the active sheet automatically."
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "helper": {"type": "string", "description": "Viz helper name (e.g. plot_data, quick_plot, correlation_heatmap)."},
            "params": {"type": "object", "description": "Helper-specific parameters."},
            "data_range": {"type": "string", "description": "A1 range address for input data."},
            "headers": {"type": "boolean", "description": "First row contains column names (default true)."},
            "task_hint": {"type": "string", "description": "Optional hint echoed in result context."},
        },
        "required": ["helper"],
    }
    long_running: bool = True

    def is_async(self) -> bool:
        return True

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        from plugin.calc.analysis import run_calc_data_tool
        from plugin.scripting.payload_codec import extract_image_payload, write_image_payload_to_temp

        # run_domain rejects a blank or unknown helper and missing data with VIZ_ERROR.
        result = run_calc_data_tool(ctx, "viz", kwargs)[1]

        if result.get("status") != "ok":
            return result

        out: dict[str, Any] = dict(result)
        if ctx.doc_type == "calc":

            def _insert() -> None:
                insert_viz_result_into_doc(ctx.ctx, ctx.doc, result)

            try:
                main.call(_insert)
            except Exception as exc:
                # Keep an egress failure as an error. image_egress raises
                # when the sheet or draw page is missing. Swallowing that
                # still produced image_inserted and "Plot inserted".
                out["status"] = "error"
                out["image_inserted"] = False
                out["plot_error"] = str(exc)
                out["message"] = f"Plot was not inserted: {exc}"
            else:
                out["image_inserted"] = True
                out["message"] = "Plot inserted on active sheet"
        else:
            payload = extract_image_payload(result)
            if payload is not None:
                out["image_path"] = write_image_payload_to_temp(payload)

        return out
