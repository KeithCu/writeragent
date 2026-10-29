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
# here — those would be separate features (see docs/calc/sheet-filter.md §5).
#
# UNO filter descriptor exposes other properties (e.g. ``UseRegularExpressions``,
# ``IsCaseSensitive``, copy-to-output options). They are not wired through yet; add
# explicit kwargs + tests when needed rather than half-setting ``XPropertySet`` values.

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any
import uno

from plugin.calc.base import ToolCalcSheetBase
from plugin.calc.bridge import CalcBridge
from plugin.calc.calc_utils import query_interface as _query_interface
from plugin.calc.sheet_filter_criteria import (
    FILTER_OPERATOR2_LABELS,
    filter_operator2_code_to_name,
    parse_sheet_filter_criterion,
)
from plugin.framework.errors import UnoObjectError, uno_read

if TYPE_CHECKING:
    from plugin.framework.tool import ToolContext

log = logging.getLogger("writeragent.calc")

_FILTER_CONNECTION_AND: int = 0
_FILTER_CONNECTION_OR: int = 1

_RANGE_PARAM_SCHEMA: dict[str, Any] = {
    "type": "array",
    "items": {"type": "string"},
    "description": 'Range to filter (e.g. ["A1:D20"]).',
}
_HAS_HEADER_PARAM_SCHEMA: dict[str, Any] = {
    "type": "boolean",
    "description": "First row is headers only (default true).",
}


def _connection_name(cv: Any) -> str:
    """Pure helper to decode FilterConnection to 'AND' or 'OR'."""
    if cv is None:
        return "AND"
    val = getattr(cv, "value", None)
    if val in ("OR", "AND"):
        return str(val)
    s = str(cv)
    if s.endswith(".OR") or s == "1":
        return "OR"
    if s.endswith(".AND") or s == "0":
        return "AND"
    try:
        return "OR" if int(cv) == 1 else "AND"
    except (ValueError, TypeError):
        return "AND"


def _field_to_dict(ff: Any, idx: int) -> dict[str, Any]:
    out: dict[str, Any] = {"index": idx}
    op = uno_read(lambda: int(ff.Operator), None)
    if op is not None:
        out["operator"] = filter_operator2_code_to_name(op)
        out["operator_code"] = op

    field = uno_read(lambda: int(ff.Field), None)
    if field is not None:
        out["field"] = field

    cv = uno_read(lambda: ff.Connection, None)
    out["connection"] = _connection_name(cv)

    is_num = uno_read(lambda: bool(ff.IsNumeric), None)
    if is_num is True:
        out["is_numeric"] = True
        num_val = uno_read(lambda: ff.NumericValue, None)
        if num_val is not None:
            out["value"] = num_val
    elif is_num is False:
        out["is_numeric"] = False
        str_val = uno_read(lambda: ff.StringValue, None)
        if str_val is not None:
            out["value"] = str_val
    return out


def _build_filter_fields2(uno_obj: Any, criteria: list[dict[str, Any]], range_width: int | None = None) -> tuple[Any, ...]:
    fields: list[Any] = []
    for i, c_raw in enumerate(criteria):
        c = parse_sheet_filter_criterion(c_raw, i == 0)
        if range_width is not None and c.field >= range_width:
            raise UnoObjectError(
                f"Filter criterion field index {c.field} is out of bounds (range has {range_width} columns)."
            )
        st = uno_obj.createUnoStruct("com.sun.star.sheet.TableFilterField2")
        st.Field = c.field
        st.Operator = c.operator
        st.Connection = _FILTER_CONNECTION_OR if c.connection == 1 else _FILTER_CONNECTION_AND
        st.IsNumeric = c.is_numeric
        st.NumericValue = c.numeric_value
        st.StringValue = c.string_value
        fields.append(st)
    return tuple(fields)


def _first_range_name(kwargs: dict[str, Any]) -> str:
    range_arg = kwargs.get("range")
    if not isinstance(range_arg, list) or len(range_arg) != 1 or not isinstance(range_arg[0], str) or not range_arg[0].strip():
        raise UnoObjectError("Pass exactly one range.")
    return range_arg[0].strip()


def _get_filterable_for_range(ctx: Any, range_name: str) -> Any:
    bridge = CalcBridge(ctx.doc)
    cell_range = bridge.resolve_range_or_address(range_name)
    xf = _query_interface(cell_range, "com.sun.star.sheet.XSheetFilterable")
    if xf is None:
        raise UnoObjectError("This range does not support filtering (XSheetFilterable missing).")
    return xf


def _filter_descriptor(
    ctx: Any,
    range_name: str,
    *,
    empty: bool,
    has_header: bool | None,
    missing_msg: str,
) -> tuple[Any, Any, Any]:
    xf = _get_filterable_for_range(ctx, range_name)
    fd = xf.createFilterDescriptor(empty)
    if has_header is not None:
        ps = _query_interface(fd, "com.sun.star.beans.XPropertySet")
        if ps is None:
            raise UnoObjectError("Cannot set ContainsHeader on this filter descriptor.")
        ps.setPropertyValue("ContainsHeader", has_header)
    fd2 = _query_interface(fd, "com.sun.star.sheet.XSheetFilterDescriptor2")
    if fd2 is None:
        raise UnoObjectError(missing_msg)
    return xf, fd, fd2


def _run_filter(
    ctx: ToolContext,
    kwargs: dict[str, Any],
    *,
    criteria: list[dict[str, Any]] | None = None,
) -> tuple[str, int]:
    range_name = _first_range_name(kwargs)
    has_header = bool(kwargs.get("has_header", True))

    bridge = CalcBridge(ctx.doc)
    cell_range = bridge.resolve_range_or_address(range_name)
    ra = cell_range.getRangeAddress()
    range_width = ra.EndColumn - ra.StartColumn + 1

    xf = _query_interface(cell_range, "com.sun.star.sheet.XSheetFilterable")
    if xf is None:
        raise UnoObjectError("This range does not support filtering (XSheetFilterable missing).")

    fd = xf.createFilterDescriptor(True)
    ps = _query_interface(fd, "com.sun.star.beans.XPropertySet")
    if ps is None:
        raise UnoObjectError("Cannot set ContainsHeader on this filter descriptor.")
    ps.setPropertyValue("ContainsHeader", has_header)

    fd2 = _query_interface(fd, "com.sun.star.sheet.XSheetFilterDescriptor2")
    if fd2 is None:
        raise UnoObjectError("XSheetFilterDescriptor2 is not available (need LibreOffice with TableFilterField2 support).")

    fields = _build_filter_fields2(uno, criteria or [], range_width=range_width)
    fd2.setFilterFields2(fields)

    xf.filter(fd)
    return range_name, len(fields)


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
        "field": {"type": "integer", "description": "0-based column index within range (leftmost column = 0)."},
        "operator": {"type": "string", "enum": list(FILTER_OPERATOR2_LABELS), "description": "LibreOffice FilterOperator2 (see enum)."},
        "value": {"type": "string", "description": ("Omit for EMPTY/NOT_EMPTY. Numeric string for TOP_*/BOTTOM_* operators. Otherwise set is_numeric when comparing numbers.")},
        "is_numeric": {"type": "boolean", "description": "If true, value is NumericValue; else StringValue."},
        "connection": {"type": "string", "enum": ["AND", "OR"], "description": ("Combines with previous criterion only. Omit on first item. Case-insensitive AND/OR.")},
    },
    "required": ["field", "operator"],
}


class ApplySheetFilter(ToolCalcSheetBase):
    """Apply a standard sheet filter (AutoFilter-style) on a cell range."""

    name: str | None = "apply_sheet_filter"
    intent: str | None = "edit"
    description: str = "Hide rows that do not match a standard Calc filter (not conditional formatting). delegate_to_specialized_calc_toolset(domain='sheets'). One column per criterion; chain with connection (AND default) after the first."
    parameters: dict[str, Any] | None = {
        "type": "object",
        "description": "See criteria for AND/OR chaining.",
        "properties": {
            "range": _RANGE_PARAM_SCHEMA,
            "has_header": _HAS_HEADER_PARAM_SCHEMA,
            "criteria": {"type": "array", "items": _CRITERION_ITEM_SCHEMA, "description": _CRITERIA_ARRAY_DESCRIPTION, "minItems": 1},
        },
        "required": ["range", "criteria"],
    }
    is_mutation: bool | None = True

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        criteria = kwargs.get("criteria")
        if not isinstance(criteria, list) or not criteria:
            raise UnoObjectError("criteria must be a non-empty list of filter conditions.")

        range_name, count = _run_filter(ctx, kwargs, criteria=criteria)
        log.info("Sheet filter applied on %s (%d conditions).", range_name.upper(), count)
        return {"status": "ok", "range": range_name, "criteria_count": count}


class ClearSheetFilter(ToolCalcSheetBase):
    """Remove the standard filter from a range (show all rows again)."""

    name: str | None = "clear_sheet_filter"
    intent: str | None = "edit"
    description: str = "Remove the active standard sheet filter on a range so all rows show again. Use the same range (and has_header) as apply_sheet_filter. delegate_to_specialized_calc_toolset(domain='sheets')."
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "range": _RANGE_PARAM_SCHEMA,
            "has_header": _HAS_HEADER_PARAM_SCHEMA,
        },
        "required": ["range"],
    }
    is_mutation: bool | None = True

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        range_name, _count = _run_filter(ctx, kwargs, criteria=None)
        log.info("Sheet filter cleared on %s.", range_name.upper())
        return {"status": "ok", "range": range_name, "cleared": True}


class GetSheetFilter(ToolCalcSheetBase):
    """Read back current filter criteria for a range (round-trip debugging)."""

    name: str | None = "get_sheet_filter"
    intent: str | None = "navigate"
    description: str = "Return active filter criteria and has_header for a range, or empty if none. delegate_to_specialized_calc_toolset(domain='sheets')."
    parameters: dict[str, Any] | None = {"type": "object", "properties": {"range": _RANGE_PARAM_SCHEMA}, "required": ["range"]}

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        range_name = _first_range_name(kwargs)

        _xf, fd, fd2 = _filter_descriptor(
            ctx,
            range_name,
            empty=False,
            has_header=None,
            missing_msg="XSheetFilterDescriptor2 is not available.",
        )

        fields_seq = fd2.getFilterFields2()
        crit: list[dict[str, Any]] = [] if fields_seq is None else [
            _field_to_dict(fields_seq[i], i) for i in range(len(fields_seq))
        ]

        ps = _query_interface(fd, "com.sun.star.beans.XPropertySet")
        ch = uno_read(lambda: bool(ps.getPropertyValue("ContainsHeader")), None) if ps is not None else None

        return {"status": "ok", "range": range_name, "has_header": ch, "criteria": crit, "count": len(crit)}
