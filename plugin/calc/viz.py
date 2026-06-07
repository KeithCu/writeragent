# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Calc chat tool for trusted visualization helpers."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from plugin.calc.base import ToolCalcAnalysisBase
from plugin.framework.errors import ToolExecutionError
from plugin.framework.queue_executor import execute_on_main_thread
from plugin.scripting.viz_common import HELPER_NAMES
from plugin.scripting.viz_egress import insert_viz_result_into_doc

if TYPE_CHECKING:
    from plugin.framework.tool import ToolContext

_VIZ_HELPERS = ", ".join(sorted(HELPER_NAMES))


class PlotDataTool(ToolCalcAnalysisBase):
    """Run trusted matplotlib/seaborn plot helpers on spreadsheet data."""

    name = "plot_data"
    description = (
        "Run a trusted visualization helper on spreadsheet data. "
        f"Helpers: {_VIZ_HELPERS}. "
        "Use data_range (A1 address string). On Calc, the chart inserts on the active sheet automatically."
    )
    parameters = {
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
    long_running = True

    def get_parameters(self, doc_type: str | None = None) -> dict | None:
        import copy
        from typing import cast

        p = copy.deepcopy(self.parameters)
        if p and "properties" in p:
            props = cast("dict[str, Any]", p["properties"])
            props.pop("data", None)
        return p

    def is_async(self) -> bool:
        return True

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        helper = str(kwargs.get("helper") or "").strip()
        if not helper:
            return self._tool_error("helper is required")
        if helper not in HELPER_NAMES:
            return self._tool_error(f"Unknown helper {helper!r}")

        data_range = kwargs.get("data_range")
        if not (data_range and str(data_range).strip()) and kwargs.get("data") is None:
            return self._tool_error("Provide data_range")

        if getattr(ctx, "active_domain", None) == "analysis" and kwargs.get("data") is not None:
            return self._tool_error(
                "analysis domain requires data_range (A1 address string) only for plot_data."
            )

        dr = str(data_range).strip() if data_range else None
        params = kwargs.get("params") if isinstance(kwargs.get("params"), dict) else None
        headers = bool(kwargs.get("headers", True)) if "headers" in kwargs else True
        task_hint = str(kwargs["task_hint"]) if kwargs.get("task_hint") else None

        from plugin.scripting.viz_runner import run_trusted_viz
        from plugin.scripting.image_payload import write_image_payload_to_temp
        from plugin.scripting.viz_egress import extract_image_payload

        def _run() -> dict[str, Any]:
            return run_trusted_viz(
                ctx.ctx,
                ctx.doc,
                helper=helper,
                params=params,
                data_range=dr,
                data=kwargs.get("data"),
                headers=headers,
                task_hint=task_hint,
            )

        try:
            result = execute_on_main_thread(_run)
        except ToolExecutionError as exc:
            return self._tool_error(str(exc), code=getattr(exc, "code", "VIZ_ERROR"))
        except Exception as exc:
            return self._tool_error(f"Failed to run plot: {exc}")

        if result.get("status") != "ok":
            return result

        out: dict[str, Any] = dict(result)
        if ctx.doc_type == "calc":

            def _insert() -> None:
                insert_viz_result_into_doc(ctx.ctx, ctx.doc, result)

            try:
                execute_on_main_thread(_insert)
                out["image_inserted"] = True
                out["message"] = "Plot inserted on active sheet"
            except Exception as exc:
                out["plot_error"] = str(exc)
        else:
            payload = extract_image_payload(result)
            if payload is not None:
                out["image_path"] = write_image_payload_to_temp(payload)

        return out
