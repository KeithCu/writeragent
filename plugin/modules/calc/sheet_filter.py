# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Calc standard filter (AutoFilter-style) via UNO ``XSheetFilterable`` /
# ``TableFilterField2`` / ``FilterOperator2``. Not conditional formatting.

from __future__ import annotations

import logging
from typing import Any

from plugin.framework.calc_filter_constants import (
    FILTER_OPERATOR2_LABELS,
    filter_operator2_code_to_name,
    filter_operator2_name_to_code,
)
from plugin.framework.errors import ToolExecutionError, UnoObjectError
from plugin.modules.calc.base import ToolCalcSheetFilterBase
from plugin.modules.calc.bridge import CalcBridge

logger = logging.getLogger("writeragent.calc")

_FILTER_OP_NUMERIC_ONLY = frozenset(
    {"TOP_VALUES", "TOP_PERCENT", "BOTTOM_VALUES", "BOTTOM_PERCENT"},
)
_FILTER_OP_NO_VALUE = frozenset({"EMPTY", "NOT_EMPTY"})


def _query_interface(obj: Any, typename: str) -> Any:
    import uno

    return obj.queryInterface(uno.getTypeByName(typename))


def _filter_connection_code(name: str | None) -> int:
    """FilterConnection.AND / .OR are 0 / 1 in published LibreOffice IDL."""
    if not name or name.upper() == "AND":
        return 0
    if name.upper() == "OR":
        return 1
    raise UnoObjectError(f"Invalid filter connection: {name!r} (use AND or OR).")


def _resolve_operator_code(operator: str) -> int:
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


def _field_to_dict(ff: Any, idx: int) -> dict[str, Any]:
    out: dict[str, Any] = {"index": idx}
    try:
        op = int(ff.Operator)
        out["operator"] = filter_operator2_code_to_name(op)
        out["operator_code"] = op
    except Exception:
        pass
    try:
        out["field"] = int(ff.Field)
    except Exception:
        pass
    try:
        out["connection"] = "OR" if int(ff.Connection) == 1 else "AND"
    except Exception:
        out["connection"] = "AND"
    try:
        if ff.IsNumeric:
            out["is_numeric"] = True
            out["value"] = ff.NumericValue
        else:
            out["is_numeric"] = False
            out["value"] = ff.StringValue
    except Exception:
        pass
    return out


def _parse_criterion(
    raw: dict[str, Any],
    is_first: bool,
) -> tuple[int, int, int, bool, float, str]:
    """Return Field, Operator, Connection, IsNumeric, NumericValue, StringValue."""
    if "field" not in raw:
        raise UnoObjectError("Each criterion needs 'field' (0-based column index within range).")
    field = int(raw["field"])
    op_name = str(raw.get("operator", "")).strip()
    if not op_name:
        raise UnoObjectError("Each criterion needs 'operator' (FilterOperator2 name).")
    op_code = _resolve_operator_code(op_name)
    op_label = filter_operator2_code_to_name(op_code)
    if not is_first and raw.get("connection") is not None:
        conn = _filter_connection_code(str(raw["connection"]))
    else:
        conn = _filter_connection_code("AND")

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


def _build_filter_fields2(uno: Any, criteria: list[dict[str, Any]]) -> tuple[Any, ...]:
    fields: list[Any] = []
    for i, c in enumerate(criteria):
        field, op_code, conn, is_num, num_val, str_val = _parse_criterion(c, i == 0)

        st = uno.createUnoStruct("com.sun.star.sheet.TableFilterField2")
        st.Field = field
        st.Operator = op_code
        st.Connection = conn
        st.IsNumeric = is_num
        st.NumericValue = float(num_val)
        st.StringValue = str_val
        fields.append(st)
    return tuple(fields)


def _get_filterable_for_range(ctx: Any, range_name: str) -> tuple[Any, Any]:
    bridge = CalcBridge(ctx.doc)
    sheet = bridge.get_active_sheet()
    cell_range = bridge.get_cell_range(sheet, range_name)
    xf = _query_interface(cell_range, "com.sun.star.sheet.XSheetFilterable")
    if xf is None:
        raise UnoObjectError("This range does not support filtering (XSheetFilterable missing).")
    return xf, cell_range


_CRITERION_ITEM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "field": {
            "type": "integer",
            "description": "0-based column index within range_name (0 = leftmost column).",
        },
        "operator": {
            "type": "string",
            "enum": list(FILTER_OPERATOR2_LABELS),
            "description": "UNO FilterOperator2 name (see docs/calc-sheet-filter.md).",
        },
        "value": {
            "type": "string",
            "description": (
                "Filter value. Omitted for EMPTY/NOT_EMPTY. Numeric string for comparisons "
                "when is_numeric is true. For TOP_VALUES/TOP_PERCENT/BOTTOM_* use a number string."
            ),
        },
        "is_numeric": {
            "type": "boolean",
            "description": "If true, value is applied as NumericValue (for numeric comparisons).",
        },
        "connection": {
            "type": "string",
            "enum": ["AND", "OR"],
            "description": "How this row connects to the previous criterion (first row ignores).",
        },
    },
    "required": ["field", "operator"],
}


class ApplySheetFilter(ToolCalcSheetFilterBase):
    """Apply a standard sheet filter (AutoFilter-style) on a cell range."""

    name = "apply_sheet_filter"
    intent = "edit"
    description = (
        "Apply a Calc standard filter to a data range using UNO FilterOperator2 "
        "(CONTAINS, BEGINS_WITH, GREATER, TOP_VALUES, etc.). "
        "This is sheet filtering (hide non-matching rows), not conditional formatting. "
        "Use delegate_to_specialized_calc_toolset(domain='sheet_filter'). "
        "See docs/calc-sheet-filter.md."
    )
    parameters = {
        "type": "object",
        "properties": {
            "range_name": {
                "type": "string",
                "description": "Data range to filter (e.g. 'A1:D20'), usually including a header row.",
            },
            "contains_header": {
                "type": "boolean",
                "description": "If true, the first row of the range is treated as headers (not filtered as data).",
            },
            "criteria": {
                "type": "array",
                "items": _CRITERION_ITEM_SCHEMA,
                "description": "List of filter conditions (TableFilterField2).",
            },
        },
        "required": ["range_name", "criteria"],
    }
    is_mutation = True

    def execute(self, ctx, **kwargs):
        import uno

        range_name = kwargs["range_name"]
        criteria = kwargs["criteria"]
        if not isinstance(criteria, list) or not criteria:
            raise UnoObjectError("criteria must be a non-empty list of filter conditions.")

        contains_header = bool(kwargs.get("contains_header", True))

        try:
            xf, _cell_range = _get_filterable_for_range(ctx, range_name)
            fd = xf.createFilterDescriptor(True)
            ps = _query_interface(fd, "com.sun.star.beans.XPropertySet")
            if ps is not None:
                ps.setPropertyValue("ContainsHeader", contains_header)

            fd2 = _query_interface(fd, "com.sun.star.sheet.XSheetFilterDescriptor2")
            if fd2 is None:
                raise UnoObjectError(
                    "XSheetFilterDescriptor2 is not available (need LibreOffice with TableFilterField2 support)."
                )
            fields = _build_filter_fields2(uno, criteria)
            fd2.setFilterFields2(fields)
            xf.filter(fd)

            logger.info("Sheet filter applied on %s (%d conditions).", range_name.upper(), len(fields))
            return {
                "status": "ok",
                "range_name": range_name,
                "criteria_count": len(fields),
            }
        except UnoObjectError:
            raise
        except Exception as e:
            logger.error("apply_sheet_filter: %s", e)
            raise ToolExecutionError(str(e)) from e


class ClearSheetFilter(ToolCalcSheetFilterBase):
    """Remove the standard filter from a range (show all rows again)."""

    name = "clear_sheet_filter"
    intent = "edit"
    description = (
        "Clear the standard sheet filter on the given range (same range previously passed to "
        "apply_sheet_filter). Restores all rows visible for that filter region."
    )
    parameters = {
        "type": "object",
        "properties": {
            "range_name": {
                "type": "string",
                "description": "Same data range used for apply_sheet_filter.",
            },
            "contains_header": {
                "type": "boolean",
                "description": "Should match the apply_sheet_filter setting (default true).",
            },
        },
        "required": ["range_name"],
    }
    is_mutation = True

    def execute(self, ctx, **kwargs):
        range_name = kwargs["range_name"]
        contains_header = bool(kwargs.get("contains_header", True))

        try:
            xf, _cell_range = _get_filterable_for_range(ctx, range_name)
            fd = xf.createFilterDescriptor(True)
            ps = _query_interface(fd, "com.sun.star.beans.XPropertySet")
            if ps is not None:
                ps.setPropertyValue("ContainsHeader", contains_header)

            fd2 = _query_interface(fd, "com.sun.star.sheet.XSheetFilterDescriptor2")
            if fd2 is None:
                raise UnoObjectError("XSheetFilterDescriptor2 is not available.")
            fd2.setFilterFields2(())
            xf.filter(fd)

            logger.info("Sheet filter cleared on %s.", range_name.upper())
            return {"status": "ok", "range_name": range_name, "cleared": True}
        except UnoObjectError:
            raise
        except Exception as e:
            logger.error("clear_sheet_filter: %s", e)
            raise ToolExecutionError(str(e)) from e


class GetSheetFilter(ToolCalcSheetFilterBase):
    """Read back current filter criteria for a range (round-trip debugging)."""

    name = "get_sheet_filter"
    intent = "navigate"
    description = (
        "Return the current standard filter criteria on a range (from a non-empty "
        "createFilterDescriptor(False) / getFilterFields2), or an empty list if none."
    )
    parameters = {
        "type": "object",
        "properties": {
            "range_name": {
                "type": "string",
                "description": "Data range that may have an active standard filter.",
            },
        },
        "required": ["range_name"],
    }

    def execute(self, ctx, **kwargs):
        range_name = kwargs["range_name"]

        try:
            xf, _cell_range = _get_filterable_for_range(ctx, range_name)
            fd = xf.createFilterDescriptor(False)
            fd2 = _query_interface(fd, "com.sun.star.sheet.XSheetFilterDescriptor2")
            if fd2 is None:
                raise UnoObjectError("XSheetFilterDescriptor2 is not available.")

            fields_seq = fd2.getFilterFields2()
            crit: list[dict[str, Any]] = []
            if fields_seq is not None:
                n = len(fields_seq)
                for i in range(n):
                    crit.append(_field_to_dict(fields_seq[i], i))

            ch = None
            ps = _query_interface(fd, "com.sun.star.beans.XPropertySet")
            if ps is not None:
                try:
                    ch = bool(ps.getPropertyValue("ContainsHeader"))
                except Exception:
                    pass

            return {
                "status": "ok",
                "range_name": range_name,
                "contains_header": ch,
                "criteria": crit,
                "count": len(crit),
            }
        except UnoObjectError:
            raise
        except Exception as e:
            logger.error("get_sheet_filter: %s", e)
            raise ToolExecutionError(str(e)) from e
