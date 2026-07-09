# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Symbolic helper templates, host RPC, and document egress (LO host).

Compute is lazy-loaded from ``plugin.scripting.venv.symbolic`` via ``__getattr__``.
"""

from __future__ import annotations

from typing import Any

from plugin.doc.document_helpers import is_calc, is_writer
from plugin.scripting._lazy_venv import make_getattr
from plugin.scripting.client import run_symbolic as client_run_symbolic
from plugin.scripting.helper_domain import (
    HelperScriptMeta,
    build_helper_script_template,
    header_prefix,
    parse_helper_script_header,
)
from plugin.framework.errors import ToolExecutionError
from plugin.framework.i18n import _

from plugin.scripting.calc_functions_common import SYMBOLIC_HELPER_NAMES as HELPER_NAMES

MATH_HEADER_PREFIX = header_prefix("math")

_SHIPPED_TEMPLATES = frozenset({"solve_equation", "symbolic_simplify", "integrate"})

_DEFAULT_PARAMS: dict[str, dict[str, Any]] = {
    "solve_equation": {"equation": "x**2 - 4", "variable": "x"},
    "symbolic_simplify": {"expression": "(x + 1)**2 - x**2 - 2*x"},
    "integrate": {"expression": "sin(x)", "variable": "x"},
}

_HELPER_DESCRIPTIONS: dict[str, str] = {
    "solve_equation": "Solve an equation for a variable (use = or expression equal to zero).",
    "symbolic_simplify": "Simplify a symbolic expression.",
    "integrate": "Integrate an expression (add lower/upper for definite integrals).",
}

_SYMBOLIC_VENV_EXPORTS = frozenset(
    {
        "differentiate",
        "integrate_helper",
        "latex_to_math_object",
        "run_symbolic",
        "solve_equation",
        "symbolic_simplify",
    }
)

__getattr__ = make_getattr("symbolic", _SYMBOLIC_VENV_EXPORTS)


# --- Templates ---

from plugin.scripting.helper_domain import DomainFacadeConfig, make_template_api

MathScriptMeta = HelperScriptMeta

_API = make_template_api(
    DomainFacadeConfig(
        tag="math",
        helper_names=HELPER_NAMES,
        default_params=_DEFAULT_PARAMS,
        descriptions=_HELPER_DESCRIPTIONS,
        import_module="writeragent.scripting.symbolic",
        run_name="run_symbolic",
        shipped_templates=_SHIPPED_TEMPLATES,
        data_expr="None",
    )
)

_template_body = _API.template_body
get_math_script_templates = _API.get_templates
parse_math_script_header = _API.parse_header


# --- Runner ---

def supports_symbolic_manual(doc: Any) -> bool:
    """True when Run Python Script should expose Math Helpers for *doc*."""
    if doc is None:
        return False
    try:
        return is_writer(doc) or is_calc(doc)
    except Exception:
        return False


def run_trusted_symbolic(
    uno_ctx: Any,
    doc: Any,
    *,
    helper: str,
    params: dict[str, Any] | None = None,
    task_hint: str | None = None,
) -> dict[str, Any]:
    """Run a trusted symbolic helper in the user venv."""
    name = str(helper or "").strip()
    if not name:
        raise ToolExecutionError("helper is required", code="SYMBOLIC_ERROR")
    if name not in HELPER_NAMES:
        raise ToolExecutionError(f"Unknown helper {name!r}", code="SYMBOLIC_ERROR")
    if not is_calc(doc) and not is_writer(doc):
        raise ToolExecutionError("Symbolic helpers require a Writer or Calc document.", code="SYMBOLIC_ERROR")

    spec: dict[str, Any] = {"helper": name}
    if isinstance(params, dict) and params:
        spec["params"] = params

    context: dict[str, Any] = {}
    if task_hint:
        context["task_hint"] = str(task_hint)

    return client_run_symbolic(uno_ctx, spec, None, context=context or None)


# --- Egress ---

def is_symbolic_result(value: Any) -> bool:
    """True when *value* matches the compact symbolic helper result contract."""
    if not isinstance(value, dict):
        return False
    if "status" not in value:
        return False
    helper = value.get("helper")
    if isinstance(helper, str) and helper in HELPER_NAMES:
        return True
    return bool(value.get("latex"))


def format_symbolic_for_calc(result: dict[str, Any]) -> list[list[Any]]:
    """Turn a symbolic helper result into a row-major grid for sheet egress."""
    if result.get("status") == "error":
        code = str(result.get("code") or "ERROR")
        message = str(result.get("message") or "Symbolic helper failed.")
        return [[f"Symbolic error ({code})"], [message]]

    helper = str(result.get("helper") or "symbolic")
    rows: list[list[Any]] = [[helper]]
    latex = str(result.get("latex") or "").strip()
    text = str(result.get("text") or latex).strip()
    if latex:
        rows.append(["LaTeX", latex])
    if text and text != latex:
        rows.append(["Text", text])
    solutions = result.get("solutions")
    if isinstance(solutions, list) and solutions:
        rows.append(["Solutions"])
        for sol in solutions:
            rows.append([str(sol)])
    return rows


def insert_symbolic_result_into_writer(ctx: Any, doc: Any, result: dict[str, Any], *, display_block: bool = False) -> None:
    """Insert symbolic LaTeX as a Writer Math OLE object at the selection."""
    if result.get("status") == "error":
        code = str(result.get("code") or "SYMBOLIC_ERROR")
        message = str(result.get("message") or _("Symbolic helper failed."))
        raise ToolExecutionError(message, code=code, details={"symbolic_result": result})

    latex = str(result.get("latex") or "").strip()
    if not latex:
        raise ToolExecutionError(
            _("Symbolic helper returned no LaTeX."),
            code="SYMBOLIC_ERROR",
            details={"symbolic_result": result},
        )

    from plugin.writer.math.math_mml_convert import convert_latex_to_starmath, insert_writer_math_formula

    conv = convert_latex_to_starmath(ctx, latex, display_block=display_block)
    if not conv.ok or not conv.starmath:
        err = conv.error_message or "conversion_failed"
        raise ToolExecutionError(
            _("Failed to convert LaTeX to Writer Math: {error}").format(error=err),
            code="SYMBOLIC_ERROR",
            details={"latex": latex},
        )

    controller = doc.getCurrentController()
    if controller is None:
        raise ToolExecutionError(_("No active document view."), code="SYMBOLIC_ERROR")
    view_cursor = controller.getViewCursor()
    insert_writer_math_formula(doc, view_cursor, conv.starmath, display_block=display_block)


def insert_symbolic_result_into_calc(doc: Any, ctx: Any, result: dict[str, Any]) -> int:
    """Write symbolic result rows on the active Calc sheet."""
    from plugin.calc.analysis_egress import calc_anchor_from_selection
    from plugin.calc.address_utils import index_to_column
    from plugin.calc.bridge import CalcBridge
    from plugin.calc.manipulator import CellManipulator

    grid = format_symbolic_for_calc(result)
    col, row = calc_anchor_from_selection(doc)
    bridge = CalcBridge(doc)
    manipulator = CellManipulator(bridge)
    addr = f"{index_to_column(col)}{row + 1}"
    manipulator.write_formula_range(addr, grid)
    return len(grid)


def insert_symbolic_result_into_doc(ctx: Any, doc: Any, result: dict[str, Any], *, display_block: bool = False) -> None:
    """Insert a symbolic helper result into Writer or Calc."""
    if is_writer(doc):
        insert_symbolic_result_into_writer(ctx, doc, result, display_block=display_block)
        return
    if is_calc(doc):
        insert_symbolic_result_into_calc(doc, ctx, result)
        return
    raise ToolExecutionError(_("Unsupported document type for symbolic insertion."), code="SYMBOLIC_ERROR")


def try_insert_symbolic_result(ctx: Any, doc: Any, result_data: Any, *, display_block: bool = False) -> bool:
    """Insert symbolic results when present. Returns True if insertion ran."""
    if not is_symbolic_result(result_data):
        return False
    insert_symbolic_result_into_doc(ctx, doc, result_data, display_block=display_block)
    return True

