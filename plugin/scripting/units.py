# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Trusted venv unit conversion helpers — Pint only.

Includes execution templates, egress formatting, runner, and dispatch logic.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from plugin.doc.document_helpers import is_calc, is_writer
from plugin.framework.errors import ToolExecutionError
from plugin.framework.i18n import _
from plugin.scripting.client import run_units as client_run_units

# --- Constants & Common ---

HELPER_NAMES = frozenset(
    {
        "convert_quantity",
        "parse_quantity",
        "format_quantity",
        "check_dimensionality",
    }
)

UNITS_VENV_PIP_INSTALL = "pip install pint"

UNITS_HEADER_PREFIX = "# writeragent:units"
_UNITS_HEADER_RE = re.compile(
    r"^\s*#\s*writeragent:units\s+helper=(\w+)\s+params=(\{.*\})\s*$",
    re.MULTILINE,
)

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

_UREG: Any | None = None


# --- Templates ---

@dataclass(frozen=True)
class UnitsScriptMeta:
    helper: str
    params: dict[str, Any]


def _template_body(helper: str, params: dict[str, Any]) -> str:
    params_json = json.dumps(params, separators=(",", ":"))
    desc = _HELPER_DESCRIPTIONS.get(helper, helper)
    return (
        f"{UNITS_HEADER_PREFIX} helper={helper} params={params_json}\n"  # nosec
        f"# {desc}\n"
        f"# Edit params above, then Run.\n"
        f"from plugin.scripting.units import run_units\n\n"
        f"result = run_units(\n"
        f'    {{"helper": "{helper}", "params": {params_json}}},\n'
        f"    None,\n"
        f"    {{}},\n"
        f")\n"
    )


def get_units_script_templates() -> dict[str, str]:
    """Return built-in units helper scripts keyed by helper name."""
    return {
        helper: _template_body(helper, dict(_DEFAULT_PARAMS.get(helper, {})))
        for helper in sorted(_SHIPPED_TEMPLATES)
        if helper in HELPER_NAMES
    }


def parse_units_script_header(code: str) -> UnitsScriptMeta | None:
    """Parse the machine-readable header from a built-in or copied units script."""
    if not code or UNITS_HEADER_PREFIX not in code:
        return None
    match = _UNITS_HEADER_RE.search(code)
    if not match:
        return None
    helper = match.group(1)
    if helper not in HELPER_NAMES:
        return None
    try:
        params = json.loads(match.group(2))
    except json.JSONDecodeError:
        params = {}
    if not isinstance(params, dict):
        params = {}
    return UnitsScriptMeta(helper=helper, params=params)


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
    if isinstance(params, dict) and params:
        spec["params"] = params

    context: dict[str, Any] = {}
    if task_hint:
        context["task_hint"] = str(task_hint)

    return client_run_units(uno_ctx, spec, None, context=context or None)


# --- Egress ---

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


def format_units_for_calc(result: dict[str, Any]) -> list[list[Any]]:
    """Turn a units helper result into a row-major grid for sheet egress."""
    if result.get("status") == "error":
        code = str(result.get("code") or "ERROR")
        message = str(result.get("message") or "Units helper failed.")
        return [[f"Units error ({code})"], [message]]

    helper = str(result.get("helper") or "units")
    rows: list[list[Any]] = [[helper]]
    magnitude = result.get("magnitude")
    if magnitude is not None:
        rows.append(["Magnitude", magnitude])
    units = str(result.get("units") or "").strip()
    if units:
        rows.append(["Units", units])
    formatted = str(result.get("formatted") or result.get("text") or "").strip()
    if formatted:
        rows.append(["Formatted", formatted])
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


def insert_units_result_into_calc(doc: Any, ctx: Any, result: dict[str, Any]) -> int:
    """Write units result rows on the active Calc sheet."""
    from plugin.calc.analysis_egress import calc_anchor_from_selection
    from plugin.calc.address_utils import index_to_column
    from plugin.calc.bridge import CalcBridge
    from plugin.calc.manipulator import CellManipulator

    grid = format_units_for_calc(result)
    col, row = calc_anchor_from_selection(doc)
    bridge = CalcBridge(doc)
    manipulator = CellManipulator(bridge)
    addr = f"{index_to_column(col)}{row + 1}"
    manipulator.write_formula_range(addr, grid)
    return len(grid)


def insert_units_result_into_doc(ctx: Any, doc: Any, result: dict[str, Any]) -> None:
    """Insert a units helper result into Writer or Calc."""
    if is_writer(doc):
        insert_units_result_into_writer(ctx, doc, result)
        return
    if is_calc(doc):
        insert_units_result_into_calc(doc, ctx, result)
        return
    raise ToolExecutionError(_("Unsupported document type for units insertion."), code="UNITS_ERROR")


def try_insert_units_result(ctx: Any, doc: Any, result_data: Any) -> bool:
    """Insert units results when present. Returns True if insertion ran."""
    if not is_units_result(result_data):
        return False
    insert_units_result_into_doc(ctx, doc, result_data)
    return True


# --- Core Helper Implementations (Venv Execution Path) ---

def _error_result(code: str, message: str, *, helper: str | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"status": "error", "code": code, "message": message}
    if helper:
        out["helper"] = helper
    return out


def _ok_result(
    helper: str,
    *,
    magnitude: float | None = None,
    units: str = "",
    formatted: str = "",
    text: str = "",
    **extra: Any,
) -> dict[str, Any]:
    display = formatted or text
    out: dict[str, Any] = {
        "status": "ok",
        "helper": helper,
        "formatted": display,
        "text": display,
        "writer_cleanup_hints": [],
        **extra,
    }
    if magnitude is not None:
        out["magnitude"] = magnitude
    if units:
        out["units"] = units
    return out


def _require_pint(helper: str) -> Any | None:
    try:
        import pint
        return pint
    except ImportError:
        return None


def _missing_package(helper: str) -> dict[str, Any]:
    return _error_result(
        "MISSING_PACKAGE",
        f"pint is required for {helper}. Install: {UNITS_VENV_PIP_INSTALL}",
        helper=helper,
    )


def _get_ureg() -> Any:
    global _UREG
    if _UREG is None:
        pint = _require_pint("units")
        if pint is None:
            raise ValueError("MISSING_PACKAGE")
        _UREG = pint.UnitRegistry()
    return _UREG


def _quantity_payload(qty: Any, *, helper: str) -> dict[str, Any]:
    magnitude = float(qty.magnitude)
    units = str(qty.units)
    formatted = f"{qty.magnitude:g} {units}"
    return _ok_result(helper, magnitude=magnitude, units=units, formatted=formatted)


def _parse_quantity_value(ureg: Any, text: str, *, helper: str) -> Any:
    raw = str(text or "").strip()
    if not raw:
        raise ValueError("empty quantity")
    try:
        return ureg.Quantity(raw)
    except Exception as exc:
        raise ValueError(str(exc)) from exc


def _parse_unit_or_quantity(ureg: Any, text: str, *, helper: str, param: str) -> Any:
    raw = str(text or "").strip()
    if not raw:
        raise ValueError(f"empty {param}")
    try:
        return ureg.Quantity(raw) if any(ch.isdigit() for ch in raw) else ureg.Quantity(f"1 {raw}")
    except Exception:
        return _parse_quantity_value(ureg, raw, helper=helper)


def convert_quantity(*, value: str, from_unit: str, to_unit: str) -> dict[str, Any]:
    helper = "convert_quantity"
    if _require_pint(helper) is None:
        return _missing_package(helper)
    from_text = str(from_unit or "").strip()
    to_text = str(to_unit or "").strip()
    if not from_text or not to_text:
        return _error_result("MISSING_PARAM", "from_unit and to_unit are required", helper=helper)
    try:
        ureg = _get_ureg()
        qty = ureg.Quantity(f"{float(str(value or '0').strip())} {from_text}")
        converted = qty.to(to_text)
        return _quantity_payload(converted, helper=helper)
    except ValueError as exc:
        if str(exc) == "MISSING_PACKAGE":
            return _missing_package(helper)
        return _error_result("PARSE_ERROR", str(exc), helper=helper)
    except Exception as exc:
        return _error_result("UNITS_ERROR", str(exc), helper=helper)


def parse_quantity(*, quantity: str) -> dict[str, Any]:
    helper = "parse_quantity"
    if _require_pint(helper) is None:
        return _missing_package(helper)
    try:
        ureg = _get_ureg()
        qty = _parse_quantity_value(ureg, quantity, helper=helper)
        return _quantity_payload(qty, helper=helper)
    except ValueError as exc:
        if str(exc) == "MISSING_PACKAGE":
            return _missing_package(helper)
        return _error_result("PARSE_ERROR", str(exc), helper=helper)
    except Exception as exc:
        return _error_result("UNITS_ERROR", str(exc), helper=helper)


def format_quantity(*, magnitude: str, units: str, format_spec: str = "") -> dict[str, Any]:
    helper = "format_quantity"
    if _require_pint(helper) is None:
        return _missing_package(helper)
    units_text = str(units or "").strip()
    if not units_text:
        return _error_result("MISSING_PARAM", "units is required", helper=helper)
    try:
        ureg = _get_ureg()
        mag = float(str(magnitude or "0").strip())
        qty = ureg.Quantity(f"{mag} {units_text}")
        spec = str(format_spec or "").strip()
        formatted = f"{qty.magnitude:{spec}} {qty.units}" if spec else f"{qty.magnitude:g} {qty.units}"
        return _ok_result(helper, magnitude=mag, units=units_text, formatted=formatted)
    except ValueError as exc:
        if str(exc) == "MISSING_PACKAGE":
            return _missing_package(helper)
        return _error_result("PARSE_ERROR", str(exc), helper=helper)
    except Exception as exc:
        return _error_result("UNITS_ERROR", str(exc), helper=helper)


def check_dimensionality(
    *,
    quantity_a: str = "",
    quantity_b: str = "",
    unit_a: str = "",
    unit_b: str = "",
) -> dict[str, Any]:
    helper = "check_dimensionality"
    if _require_pint(helper) is None:
        return _missing_package(helper)
    left = str(quantity_a or unit_a or "").strip()
    right = str(quantity_b or unit_b or "").strip()
    if not left or not right:
        return _error_result("MISSING_PARAM", "quantity_a/quantity_b or unit_a/unit_b are required", helper=helper)
    try:
        ureg = _get_ureg()
        qty_a = _parse_unit_or_quantity(ureg, left, helper=helper, param="quantity_a")
        qty_b = _parse_unit_or_quantity(ureg, right, helper=helper, param="quantity_b")
        compatible = qty_a.dimensionality == qty_b.dimensionality
        dim_a = str(qty_a.dimensionality)
        dim_b = str(qty_b.dimensionality)
        text = "compatible" if compatible else "incompatible"
        return _ok_result(
            helper,
            formatted=text,
            compatible=compatible,
            dimensionality_a=dim_a,
            dimensionality_b=dim_b,
        )
    except ValueError as exc:
        if str(exc) == "MISSING_PACKAGE":
            return _missing_package(helper)
        return _error_result("PARSE_ERROR", str(exc), helper=helper)
    except Exception as exc:
        return _error_result("UNITS_ERROR", str(exc), helper=helper)


def _dispatch_helper(name: str, params: dict[str, Any]) -> dict[str, Any]:
    if name == "convert_quantity":
        return convert_quantity(
            value=str(params.get("value") or ""),
            from_unit=str(params.get("from_unit") or ""),
            to_unit=str(params.get("to_unit") or ""),
        )
    if name == "parse_quantity":
        return parse_quantity(quantity=str(params.get("quantity") or ""))
    if name == "format_quantity":
        return format_quantity(
            magnitude=str(params.get("magnitude") or ""),
            units=str(params.get("units") or ""),
            format_spec=str(params.get("format_spec") or ""),
        )
    if name == "check_dimensionality":
        return check_dimensionality(
            quantity_a=str(params.get("quantity_a") or ""),
            quantity_b=str(params.get("quantity_b") or ""),
            unit_a=str(params.get("unit_a") or ""),
            unit_b=str(params.get("unit_b") or ""),
        )
    return _error_result("UNKNOWN_HELPER", f"Unknown helper {name!r}", helper=name)


def run_units(
    spec: dict[str, Any] | str,
    data: Any = None,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Spec-driven dispatcher for trusted units helpers."""
    del data
    if isinstance(spec, str):
        spec_dict: dict[str, Any] = {"helper": spec}
    elif isinstance(spec, dict):
        spec_dict = spec
    else:
        return _error_result("INVALID_SPEC", "spec must be a dict or helper name")

    helper = str(spec_dict.get("helper") or "").strip()
    if not helper:
        return _error_result("MISSING_PARAM", "helper is required")
    if helper not in HELPER_NAMES:
        return _error_result("UNKNOWN_HELPER", f"Unknown helper {helper!r}", helper=helper)

    params = spec_dict.get("params")
    if params is None:
        params = {k: v for k, v in spec_dict.items() if k != "helper"}
    if not isinstance(params, dict):
        params = {}

    result = _dispatch_helper(helper, params)
    if result.get("status") == "ok" and context:
        for key in ("task_hint",):
            if key in context:
                result[key] = context[key]
    return result
