# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Calc optimization tool: trusted helpers for OR/optimization tasks."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from plugin.calc.analysis import run_calc_data_tool, write_anchor_result
from plugin.framework.tool import ToolBaseDummy
from plugin.scripting.helper_registry import names, specs
from plugin.scripting.optimize import insert_optimize_result_into_calc

if TYPE_CHECKING:
    from plugin.framework.tool import ToolContext

_OPTIMIZE_SPECS = specs("optimize")
_OPTIMIZE_DATA_HELPERS = ", ".join(sorted(names("optimize")))
_PARAM_TEXT = " ".join(f"{s.name}: {s.param_help}" for s in sorted(_OPTIMIZE_SPECS, key=lambda s: s.name) if s.param_help)


class OptimizeDataTool(ToolBaseDummy):
    """Run trusted optimization helpers on sheet data via the venv worker."""

    name: str | None = "optimize_data"
    description: str = (
        "Run a trusted optimization helper on spreadsheet data. "
        f"Helpers: {_OPTIMIZE_DATA_HELPERS}. "
        "Use data_range (A1 address string, e.g. 'Sheet1.A1:D1000') for bulk data. "
        "The host extracts and shapes the data before it reaches the optimization code. "
        "This tool is intended for the analysis/optimization domain; pass range addresses only. "
        f"Parameter reference: {_PARAM_TEXT}"
    )
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "helper": {"type": "string", "description": "Optimization helper name (e.g. optimize_portfolio, linear_programming)."},
            "params": {"type": "object", "description": _PARAM_TEXT},
            "data_range": {"type": "string", "description": "A1 range address (e.g. 'Sheet1.A1:D1000'). This is the only way to supply data. The host extracts the values out-of-band."},
            "output_range": {"type": "string", "description": "Optional A1 anchor cell to write formatted results (Calc only)."},
            "headers": {"type": "boolean", "description": "First row contains column names (default true)."},
            "task_hint": {"type": "string", "description": "Optional hint echoed in result context."},
        },
        "required": ["helper"],
    }
    long_running: bool = True

    def is_async(self) -> bool:
        return True

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        args, result = run_calc_data_tool(ctx, "optimize", kwargs)
        return write_anchor_result(ctx, result, args.output_range, insert_optimize_result_into_calc)
