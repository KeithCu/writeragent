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

from plugin.framework.deal_shim import deal
from plugin.framework.errors import ToolExecutionError

# Stable table of FilterOperator2 labels (for tool JSON schemas); the IDL code is the index.
FILTER_OPERATOR2_LABELS: tuple[str, ...] = (
    "EMPTY",
    "NOT_EMPTY",
    "EQUAL",
    "NOT_EQUAL",
    "GREATER",
    "GREATER_EQUAL",
    "LESS",
    "LESS_EQUAL",
    "TOP_VALUES",
    "TOP_PERCENT",
    "BOTTOM_VALUES",
    "BOTTOM_PERCENT",
    "CONTAINS",
    "DOES_NOT_CONTAIN",
    "BEGINS_WITH",
    "DOES_NOT_BEGIN_WITH",
    "ENDS_WITH",
    "DOES_NOT_END_WITH",
)
_NAME_TO_CODE: dict[str, int] = {name: i for i, name in enumerate(FILTER_OPERATOR2_LABELS)}


class FilterCriterion(NamedTuple):
    field: int
    operator: int
    connection: int
    is_numeric: bool
    numeric_value: float
    string_value: str


def _deal_filter_op_name_ok_pytest(name: object) -> bool:
    # The contract checks only the type: an unknown or non-ASCII name must reach the body so the model gets INVALID_PARAM, not PreContractError.
    return isinstance(name, str)


_deal_filter_op_name_ok = _deal_filter_op_name_ok_pytest


def _deal_filter_connection_ok_pytest(name: object) -> bool:
    # Same as operator names: a bad connection is INVALID_PARAM.
    return name is None or isinstance(name, str)


_deal_filter_connection_ok = _deal_filter_connection_ok_pytest


def _normalize_operator_name(name: str) -> str:
    return name.strip().upper().replace("-", "_")


_OPS_NO_VALUE = frozenset(("EMPTY", "NOT_EMPTY"))
_OPS_FORCE_NUMERIC = frozenset(("TOP_VALUES", "TOP_PERCENT", "BOTTOM_VALUES", "BOTTOM_PERCENT"))


@deal.pre(lambda name: _deal_filter_connection_ok(name))
@deal.post(lambda result: result in (0, 1))
@deal.raises(ToolExecutionError)
def filter_connection_code(name: str | None) -> int:
    """Map ``AND`` / ``OR`` to UNO ``FilterConnection`` (0 / 1 per IDL)."""
    if not name:
        return 0
    upper = name.upper()
    if upper == "AND":
        return 0
    if upper == "OR":
        return 1
    raise ToolExecutionError(f"Invalid filter connection: {name!r} (use AND or OR).", code="INVALID_PARAM")


@deal.pre(lambda operator: _deal_filter_op_name_ok(operator))
@deal.post(lambda result: isinstance(result, int) and result >= 0)
@deal.raises(ToolExecutionError)
def resolve_filter_operator_code(operator: str) -> int:
    """Resolve ``FilterOperator2`` name to numeric code."""
    code = _NAME_TO_CODE.get(_normalize_operator_name(operator))
    if code is None:
        raise ToolExecutionError(f"Unknown filter operator: {operator!r}", code="INVALID_PARAM")
    return code


# Accepts every input: parse_sheet_filter_criterion's body raises ToolExecutionError(..., code="INVALID_PARAM") for a bad criterion, which @deal.raises declares.
def _deal_filter_criterion_ok(*args: Any, **kwargs: Any) -> bool:
    return True


@deal.pre(lambda raw, is_first: _deal_filter_criterion_ok(raw))
@deal.post(lambda result: isinstance(result, FilterCriterion))
@deal.raises(ToolExecutionError)
def parse_sheet_filter_criterion(raw: Any, is_first: bool) -> FilterCriterion:
    """Return ``FilterCriterion`` (Field, Operator, Connection, IsNumeric, NumericValue, StringValue).

    ``Connection`` on the first ``TableFilterField2`` is always AND in UNO; any
    ``connection`` key on the first JSON object is ignored so callers match LO behavior.
    Later rows: missing ``connection`` defaults to AND; ``OR`` links this row to the
    previous condition only (linear chain — not arbitrary parentheses).
    """
    # crosshair: off
    if not isinstance(raw, dict):
        raise ToolExecutionError("Each criterion must be an object.", code="INVALID_PARAM")

    if "field" not in raw:
        raise ToolExecutionError(
            "Each criterion needs 'field' (0-based column index within range).", code="INVALID_PARAM"
        )
    raw_field = raw["field"]
    if isinstance(raw_field, bool):
        raise ToolExecutionError(
            f"Invalid filter 'field' (need 0-based column index): {raw_field!r}", code="INVALID_PARAM"
        )
    try:
        if (isinstance(raw_field, float) and not raw_field.is_integer()) or (
            isinstance(raw_field, str) and "." in raw_field
        ):
            raise ToolExecutionError(
                f"Invalid filter 'field' (need integer column index): {raw_field!r}", code="INVALID_PARAM"
            )
        field = int(raw_field)
    except (TypeError, ValueError) as e:
        raise ToolExecutionError(
            f"Invalid filter 'field' (need 0-based column index): {raw_field!r}", code="INVALID_PARAM"
        ) from e

    if field < 0:
        raise ToolExecutionError(
            f"Invalid filter 'field' (column index cannot be negative): {field}", code="INVALID_PARAM"
        )

    op_name = str(raw.get("operator", "")).strip()
    if not op_name:
        raise ToolExecutionError("Each criterion needs 'operator' (FilterOperator2 name).", code="INVALID_PARAM")
    op_code = resolve_filter_operator_code(op_name)
    op_label = _normalize_operator_name(op_name)
    conn = filter_connection_code(
        None if (is_first or raw.get("connection") is None) else str(raw["connection"])
    )

    if op_label in _OPS_NO_VALUE:
        return FilterCriterion(field, op_code, conn, False, 0.0, "")

    v = raw.get("value")
    if isinstance(v, (list, dict)):
        raise ToolExecutionError(f"Invalid filter 'value': {v!r}", code="INVALID_PARAM")

    force = op_label in _OPS_FORCE_NUMERIC
    if force:
        if v is None or str(v).strip() == "" or isinstance(v, bool):
            raise ToolExecutionError(f"Operator {op_label} requires numeric 'value'.", code="INVALID_PARAM")
    elif v is None:
        raise ToolExecutionError(f"Operator {op_label} requires 'value'.", code="INVALID_PARAM")

    is_num = raw.get("is_numeric")
    if force or is_num is True or (is_num is None and isinstance(v, (int, float)) and not isinstance(v, bool)):
        try:
            fv = float(v)
        except (TypeError, ValueError) as e:
            msg = (
                f"Operator {op_label} requires a valid numeric 'value', got {v!r}."
                if force
                else f"Requires a valid numeric 'value', got {v!r}."
            )
            raise ToolExecutionError(msg, code="INVALID_PARAM") from e
        if not math.isfinite(fv):
            msg = (
                f"Operator {op_label} requires a finite numeric 'value', got {v!r}."
                if force
                else f"Numeric 'value' cannot be NaN or infinity, got {v!r}."
            )
            raise ToolExecutionError(msg, code="INVALID_PARAM")
        return FilterCriterion(field, op_code, conn, True, fv, "")

    return FilterCriterion(field, op_code, conn, False, 0.0, str(v))
