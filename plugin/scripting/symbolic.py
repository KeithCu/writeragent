# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Symbolic helper templates, host RPC, and document egress (LO host).

Compute is lazy-loaded from ``plugin.scripting.venv.symbolic`` via ``__getattr__``.
"""

from __future__ import annotations

from typing import Any

from plugin.doc.doc_type import is_calc, is_writer
from plugin.framework.errors import ToolExecutionError
from plugin.framework.i18n import _
from plugin.scripting._lazy_venv import install_lazy_dir, make_getattr
from plugin.scripting.calc_functions_common import SYMBOLIC_HELPER_NAMES as HELPER_NAMES
from plugin.scripting.client import run_symbolic as client_run_symbolic
from plugin.scripting.helper_domain import (
    DomainFacadeConfig,
    header_prefix,
    is_status_helper_result,
    make_template_api,
    supports_calc_or_writer_manual,
)

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
        "integrate",
        "integrate_helper",
        "latex_to_math_object",
        "run_symbolic",
        "solve_equation",
        "symbolic_simplify",
    }
)

__getattr__ = make_getattr("symbolic", _SYMBOLIC_VENV_EXPORTS)
install_lazy_dir(globals(), _SYMBOLIC_VENV_EXPORTS)


# --- Templates ---

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

get_math_script_templates = _API.get_templates
parse_math_script_header = _API.parse_header


# --- Runner ---

supports_symbolic_manual = supports_calc_or_writer_manual


def run_trusted_symbolic(
    uno_ctx: Any,
    doc: Any,
    *,
    helper: str,
    params: dict[str, Any] | None = None,
    task_hint: str | None = None,
    doc_type: str | None = None,
) -> dict[str, Any]:
    """Run a trusted symbolic helper in the user venv."""
    name = str(helper or "").strip()
    if not name:
        raise ToolExecutionError("helper is required", code="SYMBOLIC_ERROR")
    if name not in HELPER_NAMES:
        raise ToolExecutionError(f"Unknown helper {name!r}", code="SYMBOLIC_ERROR")
    if doc_type:
        is_valid = doc_type in ("calc", "writer")
    else:
        from plugin.framework.thread_guard import on_main_thread
        from plugin.framework.queue_executor import execute_on_main_thread

        def _check_doc() -> bool:
            return is_calc(doc) or is_writer(doc)

        is_valid = _check_doc() if on_main_thread() else execute_on_main_thread(_check_doc)

    if not is_valid:
        raise ToolExecutionError("Symbolic helpers require a Writer or Calc document.", code="SYMBOLIC_ERROR")

    spec: dict[str, Any] = {"helper": name}
    if isinstance(params, dict) and params:
        spec["params"] = params

    context: dict[str, Any] = {}
    if task_hint:
        context["task_hint"] = str(task_hint)

    return client_run_symbolic(uno_ctx, spec, None, context=context or None)


# --- Egress ---

# A truthy 'latex' key is not enough: ordinary dicts are not symbolic
# helper results. Match HELPER_NAMES and SYMBOLIC_ERROR through
# is_status_helper_result.
def is_symbolic_result(value: Any) -> bool:
    """True when *value* matches the compact symbolic helper result contract."""
    return is_status_helper_result(value, HELPER_NAMES, frozenset({"SYMBOLIC_ERROR"}))


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
    from plugin.calc.tabular_egress import insert_tabular_result_into_calc

    return insert_tabular_result_into_calc(doc, ctx, format_symbolic_for_calc(result))


def insert_symbolic_result_into_doc(ctx: Any, doc: Any, result: dict[str, Any], *, display_block: bool = False) -> int:
    """Insert a symbolic helper result into Writer or Calc."""
    if is_writer(doc):
        insert_symbolic_result_into_writer(ctx, doc, result, display_block=display_block)
        return 1
    if is_calc(doc):
        return insert_symbolic_result_into_calc(doc, ctx, result)
    raise ToolExecutionError(_("Unsupported document type for symbolic insertion."), code="SYMBOLIC_ERROR")

