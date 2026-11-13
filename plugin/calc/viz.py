# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Calc chat tool for trusted visualization helpers."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from plugin.calc.analysis import run_calc_data_tool
from plugin.calc.python.venv import insert_plots_and_report
from plugin.framework.i18n import _
from plugin.framework.tool import ToolBaseDummy
from plugin.scripting.helper_registry import names
from plugin.scripting.payload_codec import find_image_payloads

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
        # run_domain rejects a blank or unknown helper and missing data with VIZ_ERROR.
        _args, result = run_calc_data_tool(ctx, "viz", kwargs)
        if result.get("status") != "ok":
            return result

        images = find_image_payloads(result)
        if not images:
            return self._tool_error(_("Viz helper returned no image payload."), code="VIZ_ERROR")

        # Same result as run_venv_python_script: the viz result (image bytes stripped)
        # under "result", plus image_inserted / plot_error / message on Calc and
        # image_path(s) elsewhere.
        return insert_plots_and_report(ctx, images, {"result": result})
