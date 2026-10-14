# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Trusted venv units compute — runs in the user venv worker.

A blank RPC magnitude is a parse error. Numeric 0 is a real zero.
``=PY()`` blanks stay empty because ``map_over_range`` skips them first.
"""

from __future__ import annotations

import math
from typing import Any, Callable

from plugin.scripting.calc_functions_common import UNITS_HELPER_NAMES as HELPER_NAMES
from plugin.scripting.calc_range import ensure_rectangular_2d
from plugin.scripting.venv.coerce import error_result as _error_result
from plugin.scripting.venv.map_range import inspect_input, map_over_range

# Filled on first use. The venv worker handles one request at a time on the
# main thread (worker_harness.main), so this lazy registry has no lock.
_UREG: Any | None = None
_PINT: Any | None = None
_PINT_TRIED = False


class _PintMissing(Exception):
    """Pint is not installed. Not a ValueError, so it is not a parse error."""


class _FormatSpecError(Exception):
    """Invalid format specifier passed to format_quantity."""


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


def _require_pint() -> Any | None:
    """Import pint once. Later calls reuse the module or the recorded miss."""
    global _PINT, _PINT_TRIED
    if not _PINT_TRIED:
        try:
            import pint
        except ImportError:
            pint = None
        _PINT = pint
        _PINT_TRIED = True
    return _PINT


def _missing_package(helper: str) -> dict[str, Any]:
    return _error_result(
        "MISSING_PACKAGE",
        f"pint is required for {helper}.",
        helper=helper,
    )


def _get_ureg() -> Any:
    global _UREG
    if _UREG is None:
        pint = _require_pint()
        if pint is None:
            raise _PintMissing("pint is required")
        _UREG = pint.UnitRegistry()
    return _UREG


def _format_qty(qty: Any, spec: str = "") -> str:
    """Compact unit text. ``format_quantity`` used to spell the long name (``meter``).

    Parse and the payload helper already use Pint's compact form (``m``, ``km / h``).
    A format spec applies to the magnitude only.
    """
    # Compact dimensionless units are "", so the old f"{mag} {unit}" wrote a
    # trailing space into formatted and into Writer; a bad spec raised
    # ValueError and _guarded called that a parse error.
    unit = f"{qty.units:~}"
    if spec:
        try:
            mag_str = format(qty.magnitude, spec)
        except ValueError as exc:
            raise _FormatSpecError(str(exc)) from exc
    else:
        mag_str = f"{qty.magnitude:g}"
    if unit:
        return f"{mag_str} {unit}"
    return mag_str


def _quantity_payload(qty: Any, *, helper: str, display_unit: str | None = None) -> dict[str, Any]:
    magnitude = float(qty.magnitude)
    units = str(qty.units)
    # convert_quantity keeps the caller's spelling (``km/h``). Pint would write ``km / h``.
    if display_unit:
        formatted = f"{qty.magnitude:g} {display_unit}"
    else:
        formatted = _format_qty(qty)
    return _ok_result(helper, magnitude=magnitude, units=units, formatted=formatted)


def _is_blank_param(value: Any) -> bool:
    """True for omitted helper args. Ranges and arrays must not use ``!= ""`` (numpy truthiness)."""
    return value is None or (isinstance(value, str) and value == "")


def _is_missing_unit(value: Any) -> bool:
    # " " passed the None/"" guard, _required_unit_text raised, and
    # map_over_range stored #VALUE!.
    if _is_blank_param(value):
        return True
    return isinstance(value, str) and value.strip() == ""


def _aliases_agree(left: Any, right: Any) -> bool:
    if isinstance(left, str) and isinstance(right, str):
        return left.strip() == right.strip()
    if type(left) is type(right) and isinstance(left, (list, tuple)):
        if len(left) != len(right):
            return False
        return all(_aliases_agree(l_item, r_item) for l_item, r_item in zip(left, right))
    try:
        same = left == right
        return same is True
    except Exception:
        return False


def _resolve_unit_alias(primary: Any, alias: Any) -> tuple[Any, bool]:
    # What was wrong: convert_quantity kept from_unit over from, and
    # _dispatch_convert kept from over from_unit (same for to / to_unit).
    # How: each call site passed the keys to _first_nonblank in its own order.
    # Why this fixes it: both sites share this helper, and two non-blank aliases
    # that disagree are INVALID_SPEC instead of a silent pick.
    primary_blank = _is_blank_param(primary)
    alias_blank = _is_blank_param(alias)
    if primary_blank and alias_blank:
        return "", False
    if primary_blank:
        return alias, False
    if alias_blank:
        return primary, False
    if not _aliases_agree(primary, alias):
        return None, True
    return primary, False


def _finite_magnitude(value: Any, *, empty_message: str) -> float:
    """Float magnitude. A blank is an error, not zero.

    RPC used ``float(text or "0")``, so an empty magnitude became a successful
    ``0 km/h``. Numeric 0 still converts. Non-finite values are rejected because
    Pint accepts ``nan m`` as a quantity.
    """
    if value is None:
        raise ValueError(empty_message)
    if isinstance(value, str):
        text = value.strip()
        if text == "":
            raise ValueError(empty_message)
        try:
            num = float(text)
        except ValueError as exc:
            raise ValueError(str(exc)) from exc
    else:
        try:
            num = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(str(exc)) from exc
    if not math.isfinite(num):
        raise ValueError("magnitude must be finite")
    return num


def _required_unit_text(unit_text: Any) -> str:
    if unit_text is None:
        raise ValueError("missing units")
    text = str(unit_text).strip()
    if not text:
        raise ValueError("missing units")
    return text


def _unit_quantity(ureg: Any, magnitude: float, unit_text: Any) -> Any:
    """Build a quantity from a number and a unit. Do not string-format the number.

    ``UndefinedUnitError`` subclasses ``AttributeError``, so the RPC
    ``except ValueError`` used to report an unknown unit as ``UNITS_ERROR``.
    """
    text = _required_unit_text(unit_text)
    try:
        return ureg.Quantity(magnitude, text)
    except Exception as exc:
        raise ValueError(str(exc)) from exc


def _convert_to(qty: Any, to_text: Any) -> Any:
    text = _required_unit_text(to_text)
    try:
        return qty.to(text)
    except Exception as exc:
        # A dimension mismatch parsed both units. Keep it as UNITS_ERROR.
        # An unknown target unit is a parse error, same as an unknown source.
        if type(exc).__name__ == "DimensionalityError":
            raise
        raise ValueError(str(exc)) from exc


def _converted_quantity(val: Any, from_u: Any, to_u: Any) -> Any:
    num = _finite_magnitude(val, empty_message="empty value")
    ureg = _get_ureg()
    return _convert_to(_unit_quantity(ureg, num, from_u), to_u)


def _parse_quantity_value(ureg: Any, text: str) -> Any:
    raw = str(text or "").strip()
    if not raw:
        raise ValueError("empty quantity")
    try:
        return ureg.Quantity(raw)
    except Exception as exc:
        raise ValueError(str(exc)) from exc


def _parse_unit_or_quantity(ureg: Any, text: str, *, param: str) -> Any:
    raw = str(text or "").strip()
    if not raw:
        raise ValueError(f"empty {param}")
    try:
        return ureg.Quantity(raw) if any(ch.isdigit() for ch in raw) else ureg.Quantity(f"1 {raw}")
    except Exception:
        return _parse_quantity_value(ureg, raw)


def _scalar_convert(val: Any, from_u: Any, to_u: Any) -> float:
    """Convert one scalar. ``map_over_range`` turns a ValueError into ``#VALUE!``."""
    return float(_converted_quantity(val, from_u, to_u).magnitude)


def convert_quantity(
    value: Any,
    from_unit: Any = "",
    to_unit: Any = "",
    **kwargs: Any,
) -> Any:
    """Convert a numeric value or range between units.

    Direct/``=PY()`` returns a numeric magnitude float or a 1D/2D list of floats.
    ``from`` is a Python keyword, so ``**{"from": ..., "to": ...}`` is the alias.
    """
    helper = "convert_quantity"
    if _require_pint() is None:
        return _missing_package(helper)

    from_text, from_conflict = _resolve_unit_alias(from_unit, kwargs.get("from"))
    if from_conflict:
        return _error_result("INVALID_SPEC", "from and from_unit disagree", helper=helper)
    to_text, to_conflict = _resolve_unit_alias(to_unit, kwargs.get("to"))
    if to_conflict:
        return _error_result("INVALID_SPEC", "to and to_unit disagree", helper=helper)

    if _is_missing_unit(from_text) or _is_missing_unit(to_text):
        return _error_result("MISSING_PARAM", "from and to units are required", helper=helper)

    return map_over_range(
        _scalar_convert,
        value,
        from_text,
        to_text,
        handle_blanks=True,
    )


def _scalar_parse(quantity: Any) -> float:
    """Parse one quantity string into its numeric magnitude."""
    ureg = _get_ureg()
    qty = _parse_quantity_value(ureg, str(quantity if quantity is not None else ""))
    return float(qty.magnitude)


def parse_quantity(*, quantity: Any = "", **kwargs: Any) -> Any:
    """Parse a quantity string or a range of quantity strings into magnitudes."""
    del kwargs  # quantity is already keyword-only; extra keys must not TypeError
    helper = "parse_quantity"
    if _require_pint() is None:
        return _missing_package(helper)
    if _is_blank_param(quantity):
        return _error_result("MISSING_PARAM", "quantity is required", helper=helper)
    return map_over_range(
        _scalar_parse,
        quantity=quantity,
        handle_blanks=True,
    )


def _scalar_format(magnitude: Any, units: Any, format_spec: Any = "") -> str:
    """Format one magnitude. A blank magnitude raises; ``map_over_range`` never passes one."""
    num = _finite_magnitude(magnitude, empty_message="empty magnitude")
    ureg = _get_ureg()
    qty = _unit_quantity(ureg, num, units)
    spec = str(format_spec or "").strip()
    return _format_qty(qty, spec)


def format_quantity(
    *,
    magnitude: Any = "",
    units: Any = "",
    format_spec: Any = "",
    **kwargs: Any,
) -> Any:
    """Format magnitude and units for a scalar or a range."""
    del kwargs
    helper = "format_quantity"
    if _require_pint() is None:
        return _missing_package(helper)
    if _is_missing_unit(units):
        return _error_result("MISSING_PARAM", "units is required", helper=helper)
    return map_over_range(
        _scalar_format,
        magnitude=magnitude,
        units=units,
        format_spec=format_spec,
        handle_blanks=True,
    )


def check_dimensionality(
    *,
    quantity_a: str = "",
    quantity_b: str = "",
    unit_a: str = "",
    unit_b: str = "",
) -> dict[str, Any]:
    helper = "check_dimensionality"
    if _require_pint() is None:
        return _missing_package(helper)
    left = str(quantity_a or unit_a or "").strip()
    right = str(quantity_b or unit_b or "").strip()
    if not left or not right:
        return _error_result("MISSING_PARAM", "quantity_a/quantity_b or unit_a/unit_b are required", helper=helper)

    def _run() -> dict[str, Any]:
        ureg = _get_ureg()
        qty_a = _parse_unit_or_quantity(ureg, left, param="quantity_a")
        qty_b = _parse_unit_or_quantity(ureg, right, param="quantity_b")
        compatible = qty_a.dimensionality == qty_b.dimensionality
        text = "compatible" if compatible else "incompatible"
        return _ok_result(
            helper,
            formatted=text,
            compatible=compatible,
            dimensionality_a=str(qty_a.dimensionality),
            dimensionality_b=str(qty_b.dimensionality),
        )

    return _guarded(helper, _run)


def _guarded(helper: str, fn: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    """Scalar RPC errors. ValueError is a parse error. Other failures are UNITS_ERROR.

    Do not use this around ``map_over_range``. A broadcast ValueError is a shape
    mismatch, and per-cell failures are already ``#VALUE!`` inside an ok payload.
    """
    try:
        return fn()
    except _PintMissing:
        return _missing_package(helper)
    except _FormatSpecError as exc:
        return _error_result("INVALID_SPEC", str(exc), helper=helper)
    except ValueError as exc:
        return _error_result("PARSE_ERROR", str(exc), helper=helper)
    except Exception as exc:
        return _error_result("UNITS_ERROR", str(exc), helper=helper)


def _scalar_cell(value: Any) -> Any:
    inspected = inspect_input(value)
    if inspected.flat_items:
        return inspected.flat_items[0]
    return None


def _vector_payload(
    name: str,
    res: Any,
    text: str,
    fields: Callable[[list[Any]], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """One compact RPC payload for a range. ``text`` may include ``{n}``.

    ``fields`` sees the flattened cells once, so convert can attach magnitudes
    and the per-cell ``"{mag} {unit}"`` strings without walking the grid twice.
    """
    grid = ensure_rectangular_2d(res)
    flat = [cell for row in grid for cell in row]
    out: dict[str, Any] = {
        "status": "ok",
        "helper": name,
        "values": grid,
        "text": text.format(n=len(flat)),
        "writer_cleanup_hints": [],
    }
    if fields is not None:
        out.update(fields(flat))
    return out


def _target_unit_suffixes(raw_to: Any, n: int) -> list[str]:
    # str(raw_to) on a list is the Python repr, and that repr was written
    # into every formatted cell and into units.
    if n == 0:
        return []
    inspected = inspect_input(raw_to)
    if inspected.is_scalar:
        item = inspected.flat_items[0] if inspected.flat_items else ""
        suffix = str("" if item is None else item).strip()
        return [suffix] * n
    if len(inspected.flat_items) == n:
        return [str("" if item is None else item).strip() for item in inspected.flat_items]
    return [""] * n


def _convert_formatted(flat: list[Any], suffixes: list[str]) -> list[Any]:
    out: list[Any] = []
    for cell, suffix in zip(flat, suffixes):
        if isinstance(cell, (int, float)):
            out.append(f"{cell:g} {suffix}" if suffix else f"{cell:g}")
        else:
            out.append(str(cell))
    return out


def _dispatch_vector(name: str, call: Callable[[], Any], pack: Callable[[Any], dict[str, Any]]) -> dict[str, Any]:
    try:
        res = call()
    except Exception as exc:
        return _error_result("UNITS_ERROR", str(exc), helper=name)
    if isinstance(res, dict) and res.get("status") == "error":
        return res
    return pack(res)


def _dispatch_convert(params: dict[str, Any]) -> dict[str, Any]:
    name = "convert_quantity"
    if _require_pint() is None:
        return _missing_package(name)
    raw_from, from_conflict = _resolve_unit_alias(params.get("from_unit"), params.get("from"))
    if from_conflict:
        return _error_result("INVALID_SPEC", "from and from_unit disagree", helper=name)
    raw_to, to_conflict = _resolve_unit_alias(params.get("to_unit"), params.get("to"))
    if to_conflict:
        return _error_result("INVALID_SPEC", "to and to_unit disagree", helper=name)
    raw_val = params.get("value")
    if inspect_input(raw_val).is_scalar and inspect_input(raw_from).is_scalar and inspect_input(raw_to).is_scalar:
        from_text = "" if _is_blank_param(raw_from) else str(_scalar_cell(raw_from) or "").strip()
        to_text = "" if _is_blank_param(raw_to) else str(_scalar_cell(raw_to) or "").strip()
        if not from_text or not to_text:
            return _error_result("MISSING_PARAM", "from and to units are required", helper=name)

        def _run() -> dict[str, Any]:
            converted = _converted_quantity(_scalar_cell(raw_val), from_text, to_text)
            return _quantity_payload(converted, helper=name, display_unit=to_text)

        return _guarded(name, _run)

    def _pack(res: Any) -> dict[str, Any]:
        def _build_fields(flat: list[Any]) -> dict[str, Any]:
            suffixes = _target_unit_suffixes(raw_to, len(flat))
            fields_dict: dict[str, Any] = {
                "magnitudes": flat,
                "formatted": _convert_formatted(flat, suffixes),
            }
            if suffixes:
                first = suffixes[0]
                if all(s == first for s in suffixes):
                    if first:
                        fields_dict["units"] = first
                else:
                    fields_dict["units"] = suffixes
            return fields_dict

        return _vector_payload(
            name,
            res,
            "{n} quantities converted",
            _build_fields,
        )

    return _dispatch_vector(name, lambda: convert_quantity(raw_val, from_unit=raw_from, to_unit=raw_to), _pack)


def _dispatch_parse(params: dict[str, Any]) -> dict[str, Any]:
    name = "parse_quantity"
    if _require_pint() is None:
        return _missing_package(name)
    # What was wrong: parse_quantity("") and parse_quantity(quantity=None)
    # returned MISSING_PARAM, while run_units with quantity "" or None returned
    # PARSE_ERROR / empty quantity.
    # How: _dispatch_parse had no blank check, so _parse_quantity_value raised
    # and _guarded labeled it a parse error.
    # Why this fixes it: omitted quantity uses the same MISSING_PARAM check as
    # the direct function. Whitespace-only text is still parsed.
    if _is_blank_param(params.get("quantity")):
        return _error_result("MISSING_PARAM", "quantity is required", helper=name)
    raw_qty = params.get("quantity")
    if inspect_input(raw_qty).is_scalar:
        def _run() -> dict[str, Any]:
            cell = _scalar_cell(raw_qty)
            ureg = _get_ureg()
            qty = _parse_quantity_value(ureg, "" if cell is None else str(cell))
            return _quantity_payload(qty, helper=name)

        return _guarded(name, _run)

    def _pack(res: Any) -> dict[str, Any]:
        return _vector_payload(name, res, "{n} quantities parsed", lambda flat: {"magnitudes": flat})

    return _dispatch_vector(name, lambda: parse_quantity(quantity=raw_qty), _pack)


def _dispatch_format(params: dict[str, Any]) -> dict[str, Any]:
    name = "format_quantity"
    if _require_pint() is None:
        return _missing_package(name)
    raw_mag = params.get("magnitude")
    raw_units = params.get("units")
    raw_spec = str(params.get("format_spec") or "")
    if inspect_input(raw_mag).is_scalar and inspect_input(raw_units).is_scalar:
        units_text = str(_scalar_cell(raw_units) or "").strip()
        if not units_text:
            return _error_result("MISSING_PARAM", "units is required", helper=name)

        def _run() -> dict[str, Any]:
            num = _finite_magnitude(_scalar_cell(raw_mag), empty_message="empty magnitude")
            qty = _unit_quantity(_get_ureg(), num, units_text)
            spec = str(raw_spec or "").strip()
            return _ok_result(
                name,
                magnitude=float(qty.magnitude),
                units=str(qty.units),
                formatted=_format_qty(qty, spec),
            )

        return _guarded(name, _run)

    def _pack(res: Any) -> dict[str, Any]:
        return _vector_payload(name, res, "{n} quantities formatted", lambda flat: {"formatted": flat})

    return _dispatch_vector(
        name,
        lambda: format_quantity(magnitude=raw_mag, units=raw_units, format_spec=raw_spec),
        _pack,
    )


def _dispatch_check(params: dict[str, Any]) -> dict[str, Any]:
    return check_dimensionality(
        quantity_a=str(params.get("quantity_a") or ""),
        quantity_b=str(params.get("quantity_b") or ""),
        unit_a=str(params.get("unit_a") or ""),
        unit_b=str(params.get("unit_b") or ""),
    )


_DISPATCH: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "convert_quantity": _dispatch_convert,
    "parse_quantity": _dispatch_parse,
    "format_quantity": _dispatch_format,
    "check_dimensionality": _dispatch_check,
}


def _dispatch_helper(name: str, params: dict[str, Any]) -> dict[str, Any]:
    fn = _DISPATCH.get(name)
    if fn is None:
        return _error_result("UNKNOWN_HELPER", f"Unknown helper {name!r}", helper=name)
    return fn(params)


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
        # A list or string used to become {}. The helper then reported
        # MISSING_PARAM ("from and to units are required") instead of a bad spec.
        return _error_result("INVALID_SPEC", "params must be a dict", helper=helper)

    # output_style is host egress only. Pop it so it is not a helper argument.
    clean = dict(params)
    clean.pop("output_style", None)
    result = _dispatch_helper(helper, clean)
    if result.get("status") == "ok" and context and "task_hint" in context:
        result["task_hint"] = context["task_hint"]
    return result
