# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Pure JSON → UNO-field parsing for Calc standard filter (``TableFilterField2``),
# plus stable ``FilterOperator2`` code labels (no UNO import at module level).
# Colocated with ``sheet_filter.py`` in this package.

"""LibreOffice ``FilterOperator2`` labels and sheet-filter criterion parsing (no UNO import at top level)."""

from __future__ import annotations

from typing import Any

from plugin.framework.deal_shim import DEAL_MAX_CMD_ARGS, DEAL_MAX_TOKEN, ascii_bounded, deal
from plugin.framework.errors import UnoObjectError

_FILTER_OPERATOR2_CODE_NAMES: tuple[str, ...] = (
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

_NAME_TO_CODE: dict[str, int] = {name: idx for idx, name in enumerate(_FILTER_OPERATOR2_CODE_NAMES)}

# Stable tuple of all FilterOperator2 names (for tool JSON schemas).
FILTER_OPERATOR2_LABELS: tuple[str, ...] = _FILTER_OPERATOR2_CODE_NAMES


@deal.pre(lambda code: isinstance(code, int) and -8 <= code < 32)
def filter_operator2_code_to_name(code: int) -> str:
    """Map UNO ``FilterOperator2`` *code* (long) to a stable string label."""
    if 0 <= code < len(_FILTER_OPERATOR2_CODE_NAMES):
        return _FILTER_OPERATOR2_CODE_NAMES[code]
    return str(int(code))


@deal.pre(lambda name: isinstance(name, str) and ascii_bounded(name, DEAL_MAX_TOKEN))
def filter_operator2_name_to_code(name: str) -> int | None:
    """Resolve case-insensitive operator name to code, or ``None`` if unknown."""
    key = name.strip().upper().replace("-", "_")
    return _NAME_TO_CODE.get(key)


_FILTER_OP_NUMERIC_ONLY = frozenset({"TOP_VALUES", "TOP_PERCENT", "BOTTOM_VALUES", "BOTTOM_PERCENT"})
_FILTER_OP_NO_VALUE = frozenset({"EMPTY", "NOT_EMPTY"})


@deal.pre(lambda name: name is None or ascii_bounded(name, DEAL_MAX_TOKEN))
@deal.post(lambda result: result in (0, 1))
@deal.raises(UnoObjectError)
def filter_connection_code(name: str | None) -> int:
    """Map ``AND`` / ``OR`` to UNO ``FilterConnection`` (0 / 1 per IDL)."""
    if not name or name.upper() == "AND":
        return 0
    if name.upper() == "OR":
        return 1
    raise UnoObjectError(f"Invalid filter connection: {name!r} (use AND or OR).")


@deal.pre(lambda operator: ascii_bounded(operator, DEAL_MAX_TOKEN))
@deal.post(lambda result: isinstance(result, int) and result >= 0)
@deal.raises(UnoObjectError)
def resolve_filter_operator_code(operator: str) -> int:
    """Resolve ``FilterOperator2`` name to numeric code (table + optional UNO enum)."""
    code = filter_operator2_name_to_code(operator)
    if code is not None:
        return code
    try:
        from com.sun.star.sheet import FilterOperator2 as FO2

        op_u = operator.strip().upper().replace("-", "_")
        if hasattr(FO2, op_u):
            return int(getattr(FO2, op_u))
    except Exception:
        pass
    raise UnoObjectError(f"Unknown filter operator: {operator!r}")


@deal.pre(
    lambda raw, is_first: isinstance(raw, dict)
    and len(raw) <= DEAL_MAX_CMD_ARGS
    and (not isinstance(raw.get("operator"), str) or ascii_bounded(raw.get("operator"), DEAL_MAX_TOKEN))
    and (not isinstance(raw.get("connection"), str) or ascii_bounded(raw.get("connection"), DEAL_MAX_TOKEN))
)
@deal.post(lambda result: isinstance(result, tuple) and len(result) == 6)
@deal.raises(UnoObjectError)
def parse_sheet_filter_criterion(raw: dict[str, Any], is_first: bool) -> tuple[int, int, int, bool, float, str]:
    """Return ``Field``, ``Operator``, ``Connection``, ``IsNumeric``, ``NumericValue``, ``StringValue``.

    ``Connection`` on the first ``TableFilterField2`` is always AND in UNO; any
    ``connection`` key on the first JSON object is ignored so callers match LO behavior.
    Later rows: missing ``connection`` defaults to AND; ``OR`` links this row to the
    previous condition only (linear chain — not arbitrary parentheses).
    """
    # crosshair: off  # nested dict Any (field/value) still combinatoric (cover-all 33337516899: 86k lines, 1559 examples). Doable later with a closed criterion schema.
    if "field" not in raw:
        raise UnoObjectError("Each criterion needs 'field' (0-based column index within range).")
    try:
        field = int(raw["field"])
    except (TypeError, ValueError) as e:
        # Empty/non-numeric field must stay UnoObjectError (@deal.raises); bare int('') is ValueError.
        raise UnoObjectError(f"Invalid filter 'field' (need 0-based column index): {raw['field']!r}") from e
    op_name = str(raw.get("operator", "")).strip()
    if not op_name:
        raise UnoObjectError("Each criterion needs 'operator' (FilterOperator2 name).")
    op_code = resolve_filter_operator_code(op_name)
    op_label = filter_operator2_code_to_name(op_code)
    if not is_first and raw.get("connection") is not None:
        conn = filter_connection_code(str(raw["connection"]))
    else:
        conn = filter_connection_code("AND")

    if op_label in _FILTER_OP_NO_VALUE:
        return field, op_code, conn, False, 0.0, ""

    if op_label in _FILTER_OP_NUMERIC_ONLY:
        v = raw.get("value")
        if v is None or str(v).strip() == "":
            raise UnoObjectError(f"Operator {op_label} requires numeric 'value'.")
        return field, op_code, conn, True, float(v), ""

    v = raw.get("value")
    if v is None:
        raise UnoObjectError(f"Operator {op_label} requires 'value'.")
    if raw.get("is_numeric") is True:
        return field, op_code, conn, True, float(v), ""
    return field, op_code, conn, False, 0.0, str(v)
