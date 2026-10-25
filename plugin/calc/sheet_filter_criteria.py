# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
"""LibreOffice ``FilterOperator2`` labels and sheet-filter criterion parsing (no UNO import at top level).

Pure JSON → UNO-field parsing for Calc standard filter (``TableFilterField2``),
plus stable ``FilterOperator2`` code labels (no UNO import at module level).
Colocated with ``sheet_filter.py`` in this package.
"""

from __future__ import annotations

import math
from typing import Any, NamedTuple

from plugin.framework.deal_shim import DEAL_MAX_CMD_ARGS, DEAL_MAX_TOKEN, _profile, ascii_bounded, deal
from plugin.framework.errors import UnoObjectError

# Stable table of all FilterOperator2 (name, code) pairs (for tool JSON schemas).
_FILTER_OPERATOR2: tuple[tuple[str, int], ...] = (
    ("EMPTY", 0),
    ("NOT_EMPTY", 1),
    ("EQUAL", 2),
    ("NOT_EQUAL", 3),
    ("GREATER", 4),
    ("GREATER_EQUAL", 5),
    ("LESS", 6),
    ("LESS_EQUAL", 7),
    ("TOP_VALUES", 8),
    ("TOP_PERCENT", 9),
    ("BOTTOM_VALUES", 10),
    ("BOTTOM_PERCENT", 11),
    ("CONTAINS", 12),
    ("DOES_NOT_CONTAIN", 13),
    ("BEGINS_WITH", 14),
    ("DOES_NOT_BEGIN_WITH", 15),
    ("ENDS_WITH", 16),
    ("DOES_NOT_END_WITH", 17),
)

FILTER_OPERATOR2_LABELS: tuple[str, ...] = tuple(name for name, _code in _FILTER_OPERATOR2)
_NAME_TO_CODE: dict[str, int] = {name: code for name, code in _FILTER_OPERATOR2}


class FilterCriterion(NamedTuple):
    field: int
    operator: int
    connection: int
    is_numeric: bool
    numeric_value: float
    string_value: str


# Dual-profile: Closed FilterOperator2 labels under CrossHair.
def _deal_filter_op_name_ok_pytest(name: object) -> bool:
    # An unknown or non-ASCII operator from the model must raise
    # UnoObjectError, not PreContractError. The body does that.
    # CrossHair stays on the closed FilterOperator2 labels.
    return isinstance(name, str)


def _deal_filter_op_name_ok_crosshair(name: object) -> bool:
    return isinstance(name, str) and name in FILTER_OPERATOR2_LABELS


_deal_filter_op_name_ok = _profile(_deal_filter_op_name_ok_crosshair, _deal_filter_op_name_ok_pytest)


def _deal_filter_connection_ok_pytest(name: object) -> bool:
    # Same as operator names: a bad connection is UnoObjectError.
    return name is None or isinstance(name, str)


def _deal_filter_connection_ok_crosshair(name: object) -> bool:
    return name is None or name in ("AND", "OR", "and", "or", "")


_deal_filter_connection_ok = _profile(_deal_filter_connection_ok_crosshair, _deal_filter_connection_ok_pytest)


def _deal_filter_code_ok_pytest(code: object) -> bool:
    # A code outside the small CrossHair window still has a string label.
    # The range cap raised PreContractError before ``str(int(code))``.
    return isinstance(code, int)


def _deal_filter_code_ok_crosshair(code: object) -> bool:
    return isinstance(code, int) and -8 <= code < 32


_deal_filter_code_ok = _profile(_deal_filter_code_ok_crosshair, _deal_filter_code_ok_pytest)


@deal.pre(lambda code: _deal_filter_code_ok(code))
def filter_operator2_code_to_name(code: int) -> str:
    """Map UNO ``FilterOperator2`` *code* (long) to a stable string label."""
    # crosshair: off
    if 0 <= code < len(FILTER_OPERATOR2_LABELS):
        return FILTER_OPERATOR2_LABELS[code]
    return str(int(code))


def _normalize_operator_name(name: str) -> str:
    return name.strip().upper().replace("-", "_")


@deal.pre(lambda name: _deal_filter_op_name_ok(name))
def filter_operator2_name_to_code(name: str) -> int | None:
    """Resolve case-insensitive operator name to code, or ``None`` if unknown."""
    return _NAME_TO_CODE.get(_normalize_operator_name(name))


_OP_VALUE_KIND = {
    "EMPTY": "none",
    "NOT_EMPTY": "none",
    "TOP_VALUES": "numeric",
    "TOP_PERCENT": "numeric",
    "BOTTOM_VALUES": "numeric",
    "BOTTOM_PERCENT": "numeric",
}


@deal.pre(lambda name: _deal_filter_connection_ok(name))
@deal.post(lambda result: result in (0, 1))
@deal.raises(UnoObjectError)
def filter_connection_code(name: str | None) -> int:
    """Map ``AND`` / ``OR`` to UNO ``FilterConnection`` (0 / 1 per IDL)."""
    if not name:
        return 0
    upper = name.upper()
    if upper == "AND":
        return 0
    if upper == "OR":
        return 1
    raise UnoObjectError(f"Invalid filter connection: {name!r} (use AND or OR).")


@deal.pre(lambda operator: _deal_filter_op_name_ok(operator))
@deal.post(lambda result: isinstance(result, int) and result >= 0)
@deal.raises(UnoObjectError)
def resolve_filter_operator_code(operator: str) -> int:
    """Resolve ``FilterOperator2`` name to numeric code."""
    code = filter_operator2_name_to_code(operator)
    if code is not None:
        return code
    raise UnoObjectError(f"Unknown filter operator: {operator!r}")


def _deal_filter_criterion_ok_pytest(raw: object) -> bool:
    return True


def _deal_filter_criterion_ok_crosshair(raw: object) -> bool:
    return (
        isinstance(raw, dict)
        and len(raw) <= DEAL_MAX_CMD_ARGS
        and (not isinstance(raw.get("operator"), str) or ascii_bounded(raw.get("operator"), DEAL_MAX_TOKEN))
        and (not isinstance(raw.get("connection"), str) or ascii_bounded(raw.get("connection"), DEAL_MAX_TOKEN))
    )


_deal_filter_criterion_ok = _profile(_deal_filter_criterion_ok_crosshair, _deal_filter_criterion_ok_pytest)


@deal.pre(lambda raw, is_first: _deal_filter_criterion_ok(raw))
@deal.post(lambda result: isinstance(result, FilterCriterion))
@deal.raises(UnoObjectError)
def parse_sheet_filter_criterion(raw: Any, is_first: bool) -> FilterCriterion:
    """Return ``FilterCriterion`` (Field, Operator, Connection, IsNumeric, NumericValue, StringValue).

    ``Connection`` on the first ``TableFilterField2`` is always AND in UNO; any
    ``connection`` key on the first JSON object is ignored so callers match LO behavior.
    Later rows: missing ``connection`` defaults to AND; ``OR`` links this row to the
    previous condition only (linear chain — not arbitrary parentheses).
    """
    # crosshair: off
    if not isinstance(raw, dict):
        raise UnoObjectError("Each criterion must be an object.")

    if "field" not in raw:
        raise UnoObjectError("Each criterion needs 'field' (0-based column index within range).")
    raw_field = raw["field"]
    if isinstance(raw_field, bool):
        raise UnoObjectError(f"Invalid filter 'field' (need 0-based column index): {raw_field!r}")
    try:
        if isinstance(raw_field, float):
            if not raw_field.is_integer():
                raise UnoObjectError(f"Invalid filter 'field' (need integer column index): {raw_field!r}")
            field = int(raw_field)
        elif isinstance(raw_field, str):
            if "." in raw_field:
                raise UnoObjectError(f"Invalid filter 'field' (need integer column index): {raw_field!r}")
            field = int(raw_field)
        else:
            field = int(raw_field)
    except (TypeError, ValueError) as e:
        raise UnoObjectError(f"Invalid filter 'field' (need 0-based column index): {raw_field!r}") from e

    if field < 0:
        raise UnoObjectError(f"Invalid filter 'field' (column index cannot be negative): {field}")

    op_name = str(raw.get("operator", "")).strip()
    if not op_name:
        raise UnoObjectError("Each criterion needs 'operator' (FilterOperator2 name).")
    op_code = resolve_filter_operator_code(op_name)
    op_label = filter_operator2_code_to_name(op_code)
    if not is_first and raw.get("connection") is not None:
        conn = filter_connection_code(str(raw["connection"]))
    else:
        conn = filter_connection_code("AND")

    kind = _OP_VALUE_KIND.get(op_label, "text")
    if kind == "none":
        return FilterCriterion(field, op_code, conn, False, 0.0, "")

    v = raw.get("value")
    if isinstance(v, (list, dict)):
        raise UnoObjectError(f"Invalid filter 'value': {v!r}")

    if kind == "numeric":
        if v is None or str(v).strip() == "" or isinstance(v, bool):
            raise UnoObjectError(f"Operator {op_label} requires numeric 'value'.")
        try:
            fv = float(v)
        except (TypeError, ValueError) as e:
            raise UnoObjectError(f"Operator {op_label} requires a valid numeric 'value', got {v!r}.") from e
        if math.isnan(fv) or math.isinf(fv):
            raise UnoObjectError(f"Operator {op_label} requires a finite numeric 'value', got {v!r}.")
        return FilterCriterion(field, op_code, conn, True, fv, "")

    if v is None:
        raise UnoObjectError(f"Operator {op_label} requires 'value'.")

    is_num = raw.get("is_numeric")
    if is_num is True or (is_num is None and isinstance(v, (int, float)) and not isinstance(v, bool)):
        try:
            fv = float(v)
        except (TypeError, ValueError) as e:
            raise UnoObjectError(f"Requires a valid numeric 'value', got {v!r}.") from e
        if math.isnan(fv) or math.isinf(fv):
            raise UnoObjectError(f"Numeric 'value' cannot be NaN or infinity, got {v!r}.")
        return FilterCriterion(field, op_code, conn, True, fv, "")

    return FilterCriterion(field, op_code, conn, False, 0.0, str(v))
