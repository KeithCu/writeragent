# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Chat tool for trusted SymPy symbolic math helpers."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

from plugin.calc.base import ToolCalcPythonBase
from plugin.framework.errors import ToolExecutionError
from plugin.framework.queue_executor import execute_on_main_thread
from plugin.scripting.symbolic_common import HELPER_NAMES
from plugin.scripting.symbolic_egress import insert_symbolic_result_into_doc

if TYPE_CHECKING:
    from plugin.framework.tool import ToolContext

_SYMBOLIC_HELPERS = ", ".join(sorted(HELPER_NAMES))


class SymbolicMathTool(ToolCalcPythonBase):
    """Run trusted SymPy symbolic helpers (solve, simplify, integrate, differentiate)."""

    name = "symbolic_math"
    specialized_cross_cutting: ClassVar[bool] = True
    description = (
        "Run a trusted SymPy symbolic math helper. "
        f"Helpers: {_SYMBOLIC_HELPERS}. "
        "On Writer, the result inserts as a Math object when LaTeX conversion succeeds. "
        "On Calc, results write to the active sheet."
    )
    parameters = {
        "type": "object",
        "properties": {
            "helper": {
                "type": "string",
                "description": "Symbolic helper name (e.g. solve_equation, symbolic_simplify, integrate).",
            },
            "params": {"type": "object", "description": "Helper-specific parameters (expression, equation, variable, …)."},
            "task_hint": {"type": "string", "description": "Optional hint echoed in result context."},
            "display_block": {"type": "boolean", "description": "Writer only: insert as display (block) math."},
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
        display_block = bool(kwargs.get("display_block", False))

        from plugin.scripting.symbolic_runner import run_trusted_symbolic

        def _run() -> dict[str, Any]:
            return run_trusted_symbolic(
                ctx.ctx,
                ctx.doc,
                helper=helper,
                params=params,
                task_hint=task_hint,
            )

        try:
            result = execute_on_main_thread(_run)
        except ToolExecutionError as exc:
            return self._tool_error(str(exc), code=getattr(exc, "code", "SYMBOLIC_ERROR"))
        except Exception as exc:
            return self._tool_error(f"Failed to run symbolic helper: {exc}")

        if result.get("status") != "ok":
            return result

        out: dict[str, Any] = dict(result)

        def _insert() -> None:
            insert_symbolic_result_into_doc(ctx.ctx, ctx.doc, result, display_block=display_block)

        try:
            execute_on_main_thread(_insert)
            out["math_inserted"] = True
            if ctx.doc_type == "calc":
                out["message"] = "Symbolic result written to active sheet"
            else:
                out["message"] = "Math formula inserted in document"
        except Exception as exc:
            out["insert_error"] = str(exc)

        return out
