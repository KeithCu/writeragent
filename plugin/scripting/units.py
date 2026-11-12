# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Units helper host RPC and document egress (LO host).

Compute is lazy-loaded from ``plugin.scripting.venv.units`` via ``__getattr__``.
"""

from __future__ import annotations

from typing import Any

from plugin.framework.doc_type import is_calc, is_writer
from plugin.framework.errors import ToolExecutionError
from plugin.framework.i18n import _
from plugin.scripting._lazy_venv import install_lazy_exports
from plugin.scripting.helper_domain import ScriptFacts
from plugin.scripting.helper_registry import names

from plugin.scripting.calc_range import ensure_rectangular_2d

_UNITS_VENV_EXPORTS = names("units") | {"run_units"}

install_lazy_exports(globals(), "units", _UNITS_VENV_EXPORTS)


# --- Egress ---

def extract_units_insert_kwargs(ctx: Any, code: str) -> dict[str, Any]:
    """Extract units insertion kwargs (e.g. output_style) from script code."""
    spec = ScriptFacts.from_source(code).first_run_spec("run_units")
    body_params = spec.get("params") if spec else None
    if not isinstance(body_params, dict):
        return {}
    raw = body_params.get("output_style")
    style = "" if raw is None else str(raw).strip()
    return {"output_style": style} if style else {}


def units_display_text(result: dict[str, Any]) -> str:
    # Vector formatted is a list of cells. Join non-blank pieces so Writer does not insert a Python repr or ", ," gaps. Keep #VALUE!.
    formatted = result.get("formatted")
    if isinstance(formatted, (list, tuple)) and formatted:
        return ", ".join(part for part in (str(item).strip() for item in formatted) if part)
    return str(formatted or result.get("text") or "").strip()


def format_units_for_calc(result: dict[str, Any], *, output_style: str | None = None) -> list[list[Any]]:
    """Turn a units helper result into a row-major grid for sheet egress."""
    if result.get("status") == "error":
        code = str(result.get("code") or "ERROR")
        message = str(result.get("message") or "Units helper failed.")
        return [[f"Units error ({code})"], [message]]

    if "values" in result and isinstance(result["values"], list):
        return ensure_rectangular_2d(result["values"])

    helper = str(result.get("helper") or "units")
    formatted = units_display_text(result)
    # formatted: one cell. detailed: key/value rows. Unknown style uses the helper default.
    if output_style in ("formatted", "detailed"):
        style = output_style
    elif helper in ("convert_quantity", "parse_quantity"):
        style = "formatted"
    else:
        style = "detailed"

    head = formatted or helper
    if style == "formatted":
        return [[head]]

    rows: list[list[Any]] = [[head]]
    magnitude = result.get("magnitude")
    if magnitude is not None:
        rows.append(["Magnitude", magnitude])
    units_text = str(result.get("units") or "").strip()
    if units_text:
        rows.append(["Units", units_text])
    for label, key in (
        ("Compatible", "compatible"),
        ("Dimensionality A", "dimensionality_a"),
        ("Dimensionality B", "dimensionality_b"),
    ):
        value = result.get(key)
        if value is not None:
            rows.append([label, value])
    return rows


def insert_units_result_into_writer(ctx: Any, doc: Any, result: dict[str, Any]) -> None:
    """Insert formatted units text at the Writer selection."""
    if result.get("status") == "error":
        code = str(result.get("code") or "UNITS_ERROR")
        message = str(result.get("message") or _("Units helper failed."))
        raise ToolExecutionError(message, code=code, details={"units_result": result})

    text = units_display_text(result)
    if not text:
        raise ToolExecutionError(
            _("Units helper returned no formatted text."),
            code="UNITS_ERROR",
            details={"units_result": result},
        )

    from plugin.writer.html_import import insert_content_at_position

    insert_content_at_position(doc, ctx, text, "selection")


def insert_units_result_into_doc(
    ctx: Any,
    doc: Any,
    result: dict[str, Any],
    *,
    output_style: str | None = None,
) -> int:
    """Insert a units helper result into Writer or Calc."""
    if is_writer(doc):
        insert_units_result_into_writer(ctx, doc, result)
        return 1
    if is_calc(doc):
        from plugin.calc.tabular_egress import insert_tabular_result_into_calc

        grid = format_units_for_calc(result, output_style=output_style)
        return insert_tabular_result_into_calc(doc, ctx, grid)
    raise ToolExecutionError(_("Unsupported document type for units insertion."), code="UNITS_ERROR")
