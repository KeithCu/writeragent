# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Units helper templates, host RPC, and document egress (LO host).

Compute is lazy-loaded from ``plugin.scripting.venv.units`` via ``__getattr__``.
"""

from __future__ import annotations

from typing import Any

from plugin.doc.doc_type import is_calc, is_writer
from plugin.framework.errors import ToolExecutionError
from plugin.framework.i18n import _
from plugin.scripting._lazy_venv import install_lazy_exports
from plugin.scripting.helper_domain import (
    DomainFacadeConfig,
    is_status_helper_result,
    make_template_api,
)

from plugin.scripting.calc_functions_common import UNITS_HELPER_NAMES as HELPER_NAMES

_SHIPPED_TEMPLATES = frozenset({"convert_quantity", "parse_quantity", "check_dimensionality"})

_DEFAULT_PARAMS: dict[str, dict[str, Any]] = {
    "convert_quantity": {"value": "data", "from": "m/s", "to": "km/h"},
    "parse_quantity": {"quantity": "data"},
    "check_dimensionality": {"quantity_a": "10 m/s", "quantity_b": "5 km/h"},
}

_HELPER_DESCRIPTIONS: dict[str, str] = {
    "convert_quantity": "Convert a numeric value between units.",
    "parse_quantity": "Parse a quantity string (e.g. '10 m/s') into magnitude and units.",
    "format_quantity": "Format a magnitude and unit string for display.",
    "check_dimensionality": "Check whether two quantities or units are dimensionally compatible.",
}

_UNITS_VENV_EXPORTS = HELPER_NAMES | {"run_units"}

install_lazy_exports(globals(), "units", _UNITS_VENV_EXPORTS)

OUTPUT_STYLES = frozenset({"formatted", "detailed"})
_FORMATTED_DEFAULT_HELPERS = frozenset({"convert_quantity", "parse_quantity"})


# --- Templates ---


_API = make_template_api(
    DomainFacadeConfig(
        tag="units",
        helper_names=HELPER_NAMES,
        default_params=_DEFAULT_PARAMS,
        descriptions=_HELPER_DESCRIPTIONS,
        import_module="writeragent.scripting.units",
        run_name="run_units",
        shipped_templates=_SHIPPED_TEMPLATES,
        # convert_quantity takes value, from_unit, to_unit positionally (``from``
        # is not a keyword argument). parse/check are keyword-only.
        positional_args={
            "convert_quantity": ("value", "from", "to"),
        },
    )
)

get_units_script_templates = _API.get_templates
parse_units_script_header = _API.parse_header


# --- Runner ---


# --- Egress ---

def resolve_output_style(helper: str, output_style: str | None) -> str:
    """Resolve Calc egress layout: formatted (single cell) or detailed (key-value grid)."""
    if output_style in OUTPUT_STYLES:
        return output_style
    if helper in _FORMATTED_DEFAULT_HELPERS:
        return "formatted"
    return "detailed"


def split_helper_params(params: dict[str, Any] | None) -> tuple[dict[str, Any], str | None]:
    """Strip egress-only keys before dispatching to Pint helpers."""
    if not isinstance(params, dict):
        return {}, None
    clean = dict(params)
    raw_style = clean.pop("output_style", None)
    output_style = str(raw_style).strip() if raw_style is not None else None
    if output_style == "":
        output_style = None
    return clean, output_style


def extract_units_insert_kwargs(ctx: Any, code: str) -> dict[str, Any]:
    """Extract units insertion kwargs (e.g. output_style) from script code."""
    from plugin.scripting.helper_domain import parse_run_import_call_params

    body_params = parse_run_import_call_params(code, run_name="run_units")
    if body_params is not None:
        _unused, output_style = split_helper_params(body_params)
        if output_style is not None:
            return {"output_style": output_style}
    return {}


# Ordinary dicts with formatted and magnitude are not units results:
# match HELPER_NAMES and UNITS_ERROR through is_status_helper_result.
def is_units_result(value: Any) -> bool:
    """True when *value* matches the compact units helper result contract."""
    return is_status_helper_result(value, HELPER_NAMES, frozenset({"UNITS_ERROR"}))


def units_display_text(result: dict[str, Any]) -> str:
    # str of the vector formatted list is a Python repr, and that repr was
    # inserted into Writer and shown on the Run Python Script status line.
    # A blank cell from map_over_range is "", and joining it produced
    # 36 km/h, , 72 km/h in Writer and on the Run Python Script status line.
    # Empty pieces are dropped. #VALUE! stays.
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
        from plugin.scripting.calc_range import ensure_rectangular_2d
        return ensure_rectangular_2d(result["values"])

    helper = str(result.get("helper") or "units")
    formatted = units_display_text(result)
    style = resolve_output_style(helper, output_style)

    if style == "formatted":
        return [[formatted]] if formatted else [[helper]]

    rows: list[list[Any]] = []
    if formatted:
        rows.append([formatted])
    else:
        rows.append([helper])
    magnitude = result.get("magnitude")
    if magnitude is not None:
        rows.append(["Magnitude", magnitude])
    units = str(result.get("units") or "").strip()
    if units:
        rows.append(["Units", units])
    compatible = result.get("compatible")
    if compatible is not None:
        rows.append(["Compatible", compatible])
    dimensionality_a = result.get("dimensionality_a")
    dimensionality_b = result.get("dimensionality_b")
    if dimensionality_a is not None:
        rows.append(["Dimensionality A", dimensionality_a])
    if dimensionality_b is not None:
        rows.append(["Dimensionality B", dimensionality_b])
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

    from plugin.writer.format import insert_content_at_position

    insert_content_at_position(doc, ctx, text, "selection")


def insert_units_result_into_calc(
    doc: Any,
    ctx: Any,
    result: dict[str, Any],
    *,
    output_style: str | None = None,
) -> int:
    """Write units result rows on the active Calc sheet."""
    from plugin.calc.tabular_egress import insert_tabular_result_into_calc

    grid = format_units_for_calc(result, output_style=output_style)
    return insert_tabular_result_into_calc(doc, ctx, grid)


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
        return insert_units_result_into_calc(doc, ctx, result, output_style=output_style)
    raise ToolExecutionError(_("Unsupported document type for units insertion."), code="UNITS_ERROR")
