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
from plugin.scripting._lazy_venv import install_lazy_exports
from plugin.scripting.helper_registry import names

_SYMBOLIC_VENV_EXPORTS = names("symbolic") | {"run_symbolic"}

install_lazy_exports(globals(), "symbolic", _SYMBOLIC_VENV_EXPORTS)


# --- Templates ---


def get_math_script_templates() -> dict[str, str]:
    from plugin.scripting.domain_registry import get_domain_wiring

    return get_domain_wiring("math").templates()


# --- Egress ---


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


def insert_symbolic_result_into_doc(ctx: Any, doc: Any, result: dict[str, Any], *, display_block: bool = False) -> int:
    """Insert a symbolic helper result into Writer or Calc."""
    if is_writer(doc):
        insert_symbolic_result_into_writer(ctx, doc, result, display_block=display_block)
        return 1
    if is_calc(doc):
        from plugin.calc.tabular_egress import insert_tabular_result_into_calc

        return insert_tabular_result_into_calc(doc, ctx, format_symbolic_for_calc(result))
    raise ToolExecutionError(_("Unsupported document type for symbolic insertion."), code="SYMBOLIC_ERROR")

