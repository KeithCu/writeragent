# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Units helper templates, host RPC, and document egress (LO host).

Compute is lazy-loaded from ``plugin.scripting.venv.units`` via ``__getattr__``.
"""

from __future__ import annotations

from typing import Any

from plugin.doc.document_helpers import is_calc, is_writer
from plugin.framework.errors import ToolExecutionError
from plugin.framework.i18n import _
from plugin.scripting._lazy_venv import make_getattr
from plugin.scripting.client import run_units as client_run_units
from plugin.scripting.helper_domain import (
    HelperScriptMeta,
    header_prefix,
)

from plugin.scripting.calc_functions_common import UNITS_HELPER_NAMES as HELPER_NAMES

UNITS_HEADER_PREFIX = header_prefix("units")

_SHIPPED_TEMPLATES = frozenset({"convert_quantity", "parse_quantity", "check_dimensionality"})

_DEFAULT_PARAMS: dict[str, dict[str, Any]] = {
    "convert_quantity": {"value": "10", "from_unit": "m/s", "to_unit": "km/h"},
    "parse_quantity": {"quantity": "10 m/s"},
    "check_dimensionality": {"quantity_a": "10 m/s", "quantity_b": "5 km/h"},
}

_HELPER_DESCRIPTIONS: dict[str, str] = {
    "convert_quantity": "Convert a numeric value between units.",
    "parse_quantity": "Parse a quantity string (e.g. '10 m/s') into magnitude and units.",
    "format_quantity": "Format a magnitude and unit string for display.",
    "check_dimensionality": "Check whether two quantities or units are dimensionally compatible.",
}

_UNITS_VENV_EXPORTS = frozenset(
    {
        "check_dimensionality",
        "convert_quantity",
        "format_quantity",
        "parse_quantity",
        "run_units",
    }
)

__getattr__ = make_getattr("units", _UNITS_VENV_EXPORTS)

OUTPUT_STYLES = frozenset({"formatted", "detailed"})
_FORMATTED_DEFAULT_HELPERS = frozenset({"convert_quantity", "parse_quantity"})
_EGRESS_PARAM_KEYS = frozenset({"output_style"})


# --- Templates ---

from plugin.scripting.helper_domain import DomainFacadeConfig, make_template_api

UnitsScriptMeta = HelperScriptMeta

_API = make_template_api(
    DomainFacadeConfig(
        tag="units",
        helper_names=HELPER_NAMES,
        default_params=_DEFAULT_PARAMS,
        descriptions=_HELPER_DESCRIPTIONS,
        import_module="writeragent.scripting.units",
        run_name="run_units",
        shipped_templates=_SHIPPED_TEMPLATES,
        data_expr="None",
    )
)

_template_body = _API.template_body
get_units_script_templates = _API.get_templates
parse_units_script_header = _API.parse_header


# --- Runner ---

def supports_units_manual(doc: Any) -> bool:
    """True when Run Python Script should expose Units Helpers for *doc*."""
    if doc is None:
        return False
    try:
        return is_writer(doc) or is_calc(doc)
    except Exception:
        return False


def run_trusted_units(
    uno_ctx: Any,
    doc: Any,
    *,
    helper: str,
    params: dict[str, Any] | None = None,
    task_hint: str | None = None,
) -> dict[str, Any]:
    """Run a trusted units helper in the user venv."""
    name = str(helper or "").strip()
    if not name:
        raise ToolExecutionError("helper is required", code="UNITS_ERROR")
    if name not in HELPER_NAMES:
        raise ToolExecutionError(f"Unknown helper {name!r}", code="UNITS_ERROR")
    if not is_calc(doc) and not is_writer(doc):
        raise ToolExecutionError("Units helpers require a Writer or Calc document.", code="UNITS_ERROR")

    spec: dict[str, Any] = {"helper": name}
    clean_params, _output_style = split_helper_params(params if isinstance(params, dict) else None)
    if clean_params:
        spec["params"] = clean_params

    context: dict[str, Any] = {}
    if task_hint:
        context["task_hint"] = str(task_hint)

    return client_run_units(uno_ctx, spec, None, context=context or None)


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


def is_units_result(value: Any) -> bool:
    """True when *value* matches the compact units helper result contract."""
    if not isinstance(value, dict):
        return False
    if "status" not in value:
        return False
    helper = value.get("helper")
    if isinstance(helper, str) and helper in HELPER_NAMES:
        return True
    return "formatted" in value and "magnitude" in value


def format_units_for_calc(result: dict[str, Any], *, output_style: str | None = None) -> list[list[Any]]:
    """Turn a units helper result into a row-major grid for sheet egress."""
    if result.get("status") == "error":
        code = str(result.get("code") or "ERROR")
        message = str(result.get("message") or "Units helper failed.")
        return [[f"Units error ({code})"], [message]]

    helper = str(result.get("helper") or "units")
    formatted = str(result.get("formatted") or result.get("text") or "").strip()
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

    text = str(result.get("formatted") or result.get("text") or "").strip()
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
    from plugin.calc.analysis_egress import calc_anchor_from_selection
    from plugin.calc.address_utils import index_to_column
    from plugin.calc.bridge import CalcBridge
    from plugin.calc.manipulator import CellManipulator

    helper = str(result.get("helper") or "")
    grid = format_units_for_calc(result, output_style=resolve_output_style(helper, output_style))
    col, row = calc_anchor_from_selection(doc)
    bridge = CalcBridge(doc)
    manipulator = CellManipulator(bridge)
    addr = f"{index_to_column(col)}{row + 1}"
    manipulator.write_formula_range(addr, grid)
    return len(grid)


def insert_units_result_into_doc(
    ctx: Any,
    doc: Any,
    result: dict[str, Any],
    *,
    output_style: str | None = None,
) -> None:
    """Insert a units helper result into Writer or Calc."""
    if is_writer(doc):
        insert_units_result_into_writer(ctx, doc, result)
        return
    if is_calc(doc):
        insert_units_result_into_calc(doc, ctx, result, output_style=output_style)
        return
    raise ToolExecutionError(_("Unsupported document type for units insertion."), code="UNITS_ERROR")


def try_insert_units_result(
    ctx: Any,
    doc: Any,
    result_data: Any,
    *,
    output_style: str | None = None,
) -> bool:
    """Insert units results when present. Returns True if insertion ran."""
    if not is_units_result(result_data):
        return False
    insert_units_result_into_doc(ctx, doc, result_data, output_style=output_style)
    return True
