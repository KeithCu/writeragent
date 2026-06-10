# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Chat tool for trusted Pint unit conversion helpers."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

from plugin.calc.base import ToolCalcPythonBase
from plugin.framework.errors import ToolExecutionError
from plugin.framework.queue_executor import execute_on_main_thread
from plugin.scripting.units import HELPER_NAMES, insert_units_result_into_doc

if TYPE_CHECKING:
    from plugin.framework.tool import ToolContext

_UNITS_HELPERS = ", ".join(sorted(HELPER_NAMES))


class UnitsTool(ToolCalcPythonBase):
    """Run trusted Pint unit helpers (convert, parse, format, dimensionality check)."""

    name = "units"
    specialized_cross_cutting: ClassVar[bool] = True
    description = (
        "Run a trusted Pint unit conversion helper. "
        f"Helpers: {_UNITS_HELPERS}. "
        "On Writer, the result inserts as formatted text at the selection. "
        "On Calc, results write to the active sheet."
    )
    parameters = {
        "type": "object",
        "properties": {
            "helper": {
                "type": "string",
                "description": "Units helper name (e.g. convert_quantity, parse_quantity, check_dimensionality).",
            },
            "params": {"type": "object", "description": "Helper-specific parameters (value, from_unit, to_unit, quantity, …)."},
            "task_hint": {"type": "string", "description": "Optional hint echoed in result context."},
        },
        "required": ["helper"],
    }
    long_running = True

    def is_async(self) -> bool:
        return True

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        helper = str(kwargs.get("helper") or "").strip()
        if not helper:
            return self._tool_error("helper is required")
        if helper not in HELPER_NAMES:
            return self._tool_error(f"Unknown helper {helper!r}")

        params = kwargs.get("params") if isinstance(kwargs.get("params"), dict) else None
        task_hint = str(kwargs["task_hint"]) if kwargs.get("task_hint") else None

        from plugin.scripting.units import run_trusted_units

        def _run() -> dict[str, Any]:
            return run_trusted_units(
                ctx.ctx,
                ctx.doc,
                helper=helper,
                params=params,
                task_hint=task_hint,
            )

        try:
            result = execute_on_main_thread(_run)
        except ToolExecutionError as exc:
            return self._tool_error(str(exc), code=getattr(exc, "code", "UNITS_ERROR"))
        except Exception as exc:
            return self._tool_error(f"Failed to run units helper: {exc}")

        if result.get("status") != "ok":
            return result

        out: dict[str, Any] = dict(result)

        def _insert() -> None:
            insert_units_result_into_doc(ctx.ctx, ctx.doc, result)

        try:
            execute_on_main_thread(_insert)
            out["units_inserted"] = True
            if ctx.doc_type == "calc":
                out["message"] = "Units result written to active sheet"
            else:
                out["message"] = "Units result inserted in document"
        except Exception as exc:
            out["insert_error"] = str(exc)

        return out
