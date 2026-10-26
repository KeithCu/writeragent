# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Chat tool for trusted SymPy symbolic math helpers."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

from plugin.calc.base import ToolCalcPythonBase
from plugin.framework.errors import CalcError, ToolExecutionError
from plugin.framework.queue_executor import execute_on_main_thread
from plugin.scripting.symbolic import (
    HELPER_NAMES,
    insert_symbolic_result_into_doc,
    run_trusted_symbolic,
)

if TYPE_CHECKING:
    from plugin.framework.tool import ToolContext

_SYMBOLIC_HELPERS = ", ".join(sorted(HELPER_NAMES))

# Mirrors the venv helper signatures (plugin/scripting/venv/symbolic.py).
# The venv still owns required-key and default checks.
_HELPER_PARAM_KEYS: dict[str, frozenset[str]] = {
    "solve_equation": frozenset({"equation", "variable"}),
    "symbolic_simplify": frozenset({"expression"}),
    "integrate": frozenset({"expression", "variable", "lower", "upper"}),
    "differentiate": frozenset({"expression", "variable"}),
    "latex_to_math_object": frozenset({"latex", "expression"}),
}


class SymbolicMathTool(ToolCalcPythonBase):
    """Run trusted SymPy symbolic helpers (solve, simplify, integrate, differentiate)."""

    name: str | None = "symbolic_math"
    specialized_cross_cutting: ClassVar[bool] = True
    description: str = f"Run a trusted SymPy symbolic math helper. Helpers: {_SYMBOLIC_HELPERS}. On Writer, the result inserts as a Math object when LaTeX conversion succeeds. On Calc, results write to the active sheet."
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "helper": {"type": "string", "description": "Symbolic helper name (e.g. solve_equation, symbolic_simplify, integrate)."},
            "params": {"type": "object", "description": "Helper-specific parameters (expression, equation, variable, …)."},
            "task_hint": {"type": "string", "description": "Optional hint echoed in result context."},
            "display_block": {"type": "boolean", "description": "Writer only: insert as display (block) math."},
        },
        "required": ["helper"],
    }
    # Draw and Impress are doc_type "draw", and run_trusted_symbolic accepts
    # only Writer and Calc, so the tool was offered and then failed with
    # "Symbolic helpers require a Writer or Calc document." Draw and Impress
    # insert formulas with insert_math.
    uno_services: list[str] | None = [
        "com.sun.star.sheet.SpreadsheetDocument",
        "com.sun.star.text.TextDocument",
    ]
    long_running: bool = True

    def is_async(self) -> bool:
        return True

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        helper = kwargs["helper"].strip()
        if not helper:
            return self._tool_error("helper is required")
        if helper not in HELPER_NAMES:
            return self._tool_error(f"Unknown helper {helper!r}")

        params = kwargs.get("params")
        if params is not None and not isinstance(params, dict):
            return self._tool_error("params must be an object.", code="INVALID_PARAM")

        accepted = _HELPER_PARAM_KEYS.get(helper)
        if params and accepted is not None:
            unknown = sorted(set(params) - accepted)
            if unknown:
                return self._tool_error(
                    f"Unknown parameter(s) for {helper}: {', '.join(unknown)}. Accepted: {', '.join(sorted(accepted))}.",
                    code="INVALID_PARAM",
                )

        task_hint = str(kwargs["task_hint"]) if kwargs.get("task_hint") else None
        display_block = bool(kwargs.get("display_block", False))

        try:
            # Pass ToolContext (not ctx.ctx) so run_trusted_worker_action reads
            # stop_checker / send_cancellation; the UNO context has neither.
            result = run_trusted_symbolic(ctx, ctx.doc, helper=helper, params=params, task_hint=task_hint, doc_type=ctx.doc_type)
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
            # What was wrong: out["math_inserted"] was unconditionally True, even
            # on Calc where tabular_egress writes cells instead of inserting a Math object.
            # Why this change fixes it: set math_inserted only when doc_type != "calc".
            out["math_inserted"] = ctx.doc_type != "calc"
            if ctx.doc_type == "calc":
                out["message"] = "Symbolic result written to active sheet"
            else:
                out["message"] = "Math formula inserted in document"
        except CalcError as exc:
            # Occupied target cells: nothing was written; tell the model to pick an empty target.
            return self._tool_error(str(exc))
        except Exception as exc:
            out["insert_error"] = str(exc)

        return out
