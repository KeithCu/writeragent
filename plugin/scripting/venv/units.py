# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Trusted venv units compute — runs in user venv worker."""

from __future__ import annotations

import logging
from typing import Any

# Local copy of small pure value from the host facade. The worker must not import
# from plugin.scripting.* (those modules pull in host-only code and are not guaranteed
# to exist or be compatible in the user's configured venv interpreter).
HELPER_NAMES = frozenset(
    {
        "convert_quantity",
        "parse_quantity",
        "format_quantity",
        "check_dimensionality",
    }
)

log = logging.getLogger(__name__)

_UREG: Any | None = None


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
        f"pint is required for {helper}.",
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


def _quantity_payload(qty: Any, *, helper: str, display_unit: str | None = None) -> dict[str, Any]:
    magnitude = float(qty.magnitude)
    units = str(qty.units)
    if display_unit:
        formatted = f"{qty.magnitude:g} {display_unit}"
    else:
        formatted = f"{qty.magnitude:g} {qty.units:~}"
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
        return _quantity_payload(converted, helper=helper, display_unit=to_text)
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

    # Inlined from host split_helper_params: strip the egress-only "output_style" key
    # (the host facade uses this for Calc sheet formatting; the worker does not need it).
    clean = dict(params)
    raw_style = clean.pop("output_style", None)
    output_style = str(raw_style).strip() if raw_style is not None else None
    if output_style == "":
        output_style = None
    clean_params, _output_style = clean, output_style
    result = _dispatch_helper(helper, clean_params)
    if result.get("status") == "ok" and context:
        for key in ("task_hint",):
            if key in context:
                result[key] = context[key]
    return result
