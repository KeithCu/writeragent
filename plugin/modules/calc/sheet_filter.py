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
#
# Scope (intentionally minimal — map UNO, do not invent semantics):
# - Build ``TableFilterField2`` rows from JSON: ``field``, ``operator``, ``value``,
#   optional ``is_numeric``, and ``connection`` (``FilterConnection`` AND/OR vs the
#   *previous* row). This matches Calc's Standard Filter: a single left-associative chain.
# - Set ``ContainsHeader`` on the filter descriptor; call ``setFilterFields2`` / ``filter``.
#
# We do *not* implement helper columns, boolean expression trees, or multi-pass workflows
# here — those would be separate features (see docs/calc-sheet-filter.md §5).
#
# UNO filter descriptor exposes other properties (e.g. ``UseRegularExpressions``,
# ``IsCaseSensitive``, copy-to-output options). They are not wired through yet; add
# explicit kwargs + tests when needed rather than half-setting ``XPropertySet`` values.

from __future__ import annotations

import logging
import uno
from typing import Any

from plugin.modules.calc.sheet_filter_criteria import FILTER_OPERATOR2_LABELS, filter_operator2_code_to_name, parse_sheet_filter_criterion
from plugin.framework.errors import ToolExecutionError, UnoObjectError
from plugin.modules.calc.base import ToolCalcSheetFilterBase
from plugin.modules.calc.bridge import CalcBridge

logger = logging.getLogger("writeragent.calc")


def _query_interface(obj: Any, typename: str) -> Any:

    return obj.queryInterface(uno.getTypeByName(typename))


def _field_to_dict(ff: Any, idx: int, uno_mod: Any) -> dict[str, Any]:
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
        cv = ff.Connection
        try:
            if cv == uno_mod.getConstantByName("com.sun.star.sheet.FilterConnection.OR"):
                out["connection"] = "OR"
            elif cv == uno_mod.getConstantByName("com.sun.star.sheet.FilterConnection.AND"):
                out["connection"] = "AND"
            else:
                out["connection"] = "OR" if int(cv) == 1 else "AND"
        except Exception:
            out["connection"] = "OR" if int(cv) == 1 else "AND"
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


def _build_filter_fields2(uno: Any, criteria: list[dict[str, Any]]) -> tuple[Any, ...]:
    try:
        fc_and = uno.getConstantByName("com.sun.star.sheet.FilterConnection.AND")
        fc_or = uno.getConstantByName("com.sun.star.sheet.FilterConnection.OR")
    except Exception:
        fc_and, fc_or = 0, 1

    fields: list[Any] = []
    for i, c in enumerate(criteria):
        field, op_code, conn, is_num, num_val, str_val = parse_sheet_filter_criterion(c, i == 0)

        st = uno.createUnoStruct("com.sun.star.sheet.TableFilterField2")
        st.Field = field
        st.Operator = op_code
        st.Connection = fc_or if conn == 1 else fc_and
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


_CRITERIA_ARRAY_DESCRIPTION = (
    "Non-empty ordered conditions; combined left-to-right ((c1) conn2 c2) conn3… like Calc Standard "
    "Filter—not arbitrary parentheses. First item: omit connection (ignored). From item 2: omit "
    "or AND (default) vs previous, or OR. "
    'Examples: [{"field":0,"operator":"EQUAL","value":"X"},{"field":1,"operator":"GREATER","value":"10","is_numeric":true}]; '
    '[{"field":1,"operator":"CONTAINS","value":"a"},{"field":3,"operator":"CONTAINS","value":"b","connection":"OR"}].'
)

_CRITERION_ITEM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "field": {"type": "integer", "description": "0-based column index within range_name (leftmost column = 0)."},
        "operator": {"type": "string", "enum": list(FILTER_OPERATOR2_LABELS), "description": "LibreOffice FilterOperator2 (see enum)."},
        "value": {"type": "string", "description": ("Omit for EMPTY/NOT_EMPTY. Numeric string for TOP_*/BOTTOM_* operators. Otherwise set is_numeric when comparing numbers.")},
        "is_numeric": {"type": "boolean", "description": "If true, value is NumericValue; else StringValue."},
        "connection": {"type": "string", "enum": ["AND", "OR"], "description": ("Combines with previous criterion only. Omit on first item. Case-insensitive AND/OR.")},
    },
    "required": ["field", "operator"],
}


class ApplySheetFilter(ToolCalcSheetFilterBase):
    """Apply a standard sheet filter (AutoFilter-style) on a cell range."""

    name = "apply_sheet_filter"
    intent = "edit"
    description = "Hide rows that do not match a standard Calc filter (not conditional formatting). delegate_to_specialized_calc_toolset(domain='sheet_filter'). One column per criterion; chain with connection (AND default) after the first."
    parameters = {
        "type": "object",
        "description": "See criteria for AND/OR chaining.",
        "properties": {
            "range_name": {"type": "string", "description": "Range to filter (e.g. 'A1:D20')."},
            "contains_header": {"type": "boolean", "description": "First row is headers only (default true)."},
            "criteria": {"type": "array", "items": _CRITERION_ITEM_SCHEMA, "description": _CRITERIA_ARRAY_DESCRIPTION, "minItems": 1},
        },
        "required": ["range_name", "criteria"],
    }
    is_mutation = True

    def execute(self, ctx, **kwargs):

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
                raise UnoObjectError("XSheetFilterDescriptor2 is not available (need LibreOffice with TableFilterField2 support).")
            fields = _build_filter_fields2(uno, criteria)
            fd2.setFilterFields2(fields)
            xf.filter(fd)

            logger.info("Sheet filter applied on %s (%d conditions).", range_name.upper(), len(fields))
            return {"status": "ok", "range_name": range_name, "criteria_count": len(fields)}
        except UnoObjectError:
            raise
        except Exception as e:
            logger.error("apply_sheet_filter: %s", e)
            raise ToolExecutionError(str(e)) from e


class ClearSheetFilter(ToolCalcSheetFilterBase):
    """Remove the standard filter from a range (show all rows again)."""

    name = "clear_sheet_filter"
    intent = "edit"
    description = "Remove the active standard sheet filter on a range so all rows show again. Use the same range_name (and contains_header) as apply_sheet_filter. delegate_to_specialized_calc_toolset(domain='sheet_filter')."
    parameters = {
        "type": "object",
        "properties": {"range_name": {"type": "string", "description": "Same data range string used when applying the filter (e.g. 'A1:D20')."}, "contains_header": {"type": "boolean", "description": "Should match apply_sheet_filter (default true)."}},
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
    description = "Return active filter criteria and contains_header for a range, or empty if none. delegate_to_specialized_calc_toolset(domain='sheet_filter')."
    parameters = {"type": "object", "properties": {"range_name": {"type": "string", "description": "Same range as apply_sheet_filter."}}, "required": ["range_name"]}

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
                    crit.append(_field_to_dict(fields_seq[i], i, uno))

            ch = None
            ps = _query_interface(fd, "com.sun.star.beans.XPropertySet")
            if ps is not None:
                try:
                    ch = bool(ps.getPropertyValue("ContainsHeader"))
                except Exception:
                    pass

            return {"status": "ok", "range_name": range_name, "contains_header": ch, "criteria": crit, "count": len(crit)}
        except UnoObjectError:
            raise
        except Exception as e:
            logger.error("get_sheet_filter: %s", e)
            raise ToolExecutionError(str(e)) from e
