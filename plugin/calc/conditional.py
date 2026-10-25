# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2024 John Balis
# Copyright (c) 2026 KeithCu (modifications and relicensing)
# Copyright (c) 2026 LibreCalc AI Assistant (Calc integration features, originally MIT)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
"""Calc conditional formatting tools."""

from __future__ import annotations

import difflib
import logging
from typing import TYPE_CHECKING, Any

from plugin.framework.errors import UnoObjectError
from plugin.framework.uno_context import create_property_value
from plugin.calc.address_utils import format_range
from plugin.calc.base import ToolCalcConditionalBase
from plugin.calc.bridge import CalcBridge
from plugin.calc.calc_utils import query_interface as _query_interface
from plugin.calc.cells import _range_args

from com.sun.star.lang import IllegalArgumentException
from com.sun.star.table import CellAddress

if TYPE_CHECKING:
    from plugin.framework.tool import ToolContext

log = logging.getLogger("writeragent.calc")

_OPERATORS: tuple[tuple[str, int], ...] = (
    ("NONE", 0),
    ("EQUAL", 1),
    ("NOT_EQUAL", 2),
    ("GREATER", 3),
    ("GREATER_EQUAL", 4),
    ("LESS", 5),
    ("LESS_EQUAL", 6),
    ("BETWEEN", 7),
    ("NOT_BETWEEN", 8),
    ("FORMULA", 9),
    ("DUPLICATE", 10),
    ("NOT_DUPLICATE", 11),
)
_CODE_TO_NAME: dict[int, str] = {code: name for name, code in _OPERATORS}
_NAME_TO_CODE: dict[str, int] = {name: code for name, code in _OPERATORS}
_SCHEMA_OPERATORS: list[str] = [name for name, _ in _OPERATORS if name != "NONE"]


def condition_operator_code_to_name(code: int) -> str:
    """Map UNO condition operator *code* (long) to a stable string label."""
    return _CODE_TO_NAME.get(code, str(int(code)))


def _entry_to_dict(entry: Any, idx: int) -> dict[str, Any]:
    """Convert a conditional entry to a readable dict."""
    result: dict[str, Any] = {"index": idx}
    op_name: str | None = None
    op_code: int | None = None
    xc2 = _query_interface(entry, "com.sun.star.sheet.XSheetCondition2")
    if xc2 is not None:
        op_code = int(xc2.getConditionOperator())
        op_name = condition_operator_code_to_name(op_code)
    if op_name is None:
        op = entry.getOperator()
        op_name = str(op.value) if hasattr(op, "value") else str(op)
    if op_name:
        result["operator"] = op_name
    if op_code is not None:
        result["operator_code"] = op_code
    f1 = entry.getFormula1()
    if f1:
        result["formula1"] = f1
    f2 = entry.getFormula2()
    if op_name in ("BETWEEN", "NOT_BETWEEN") and f2 is not None:
        result["formula2"] = f2
    sn = entry.getStyleName()
    if sn:
        result["style"] = sn

    return result


def _ensure_table_conditional_format(ctx: Any, cell_range: Any) -> Any:
    """Return ``TableConditionalFormat`` for *cell_range*, creating it if missing."""
    formats = cell_range.getPropertyValue("ConditionalFormat")
    if formats is not None:
        return formats
    sm = ctx.ctx.getServiceManager()
    if sm is None:
        raise UnoObjectError("Cannot create conditional format: no service manager.")
    created = sm.createInstanceWithContext("com.sun.star.sheet.TableConditionalFormat", ctx.ctx)
    if created is None:
        raise UnoObjectError("Failed to create com.sun.star.sheet.TableConditionalFormat.")
    cell_range.setPropertyValue("ConditionalFormat", created)
    return created


class ListConditionalFormats(ToolCalcConditionalBase):
    """List conditional formatting rules on a cell range."""

    name: str | None = "list_conditional_formats"
    intent: str | None = "navigate"
    description: str = "List conditional formatting rules on a Calc cell range. Returns operator, formulas, and applied cell style for each rule. Extended LibreOffice operators (e.g. DUPLICATE) use operator_code when present."
    parameters: dict[str, Any] | None = {"type": "object", "properties": {"range": {"type": "array", "items": {"type": "string"}, "description": 'Cell range (e.g. ["A1:D10"]). If omitted, scans used area.'}}, "required": []}

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        bridge = CalcBridge(ctx.doc)
        ranges = _range_args(kwargs.get("range"))
        if len(ranges) > 1:
            return self._tool_error("Pass exactly one range.")

        try:
            sheet = bridge.get_active_sheet()
            if ranges:
                range_str = ranges[0]
                cell_range = bridge.resolve_range_or_address(range_str)
                resolved_range_name = range_str
            else:
                cursor = sheet.createCursor()
                cursor.gotoStartOfUsedArea(False)
                cursor.gotoEndOfUsedArea(True)
                cell_range = cursor
                addr = cell_range.getRangeAddress()
                resolved_range_name = format_range(addr.StartColumn, addr.StartRow, addr.EndColumn, addr.EndRow)

            formats = cell_range.getPropertyValue("ConditionalFormat")
            if formats is None or formats.getCount() == 0:
                rules: list[dict[str, Any]] = []
            else:
                rules = []
                for i in range(formats.getCount()):
                    entry = formats.getByIndex(i)
                    rules.append(_entry_to_dict(entry, i))

            return {"status": "ok", "range": resolved_range_name, "rules": rules, "count": len(rules)}
        except IllegalArgumentException as e:
            log.exception("List conditional formats failed")
            return self._tool_error(f"Failed to list conditional formats: {str(e)}", code="CONDITIONAL_FORMAT_ERROR")


class AddConditionalFormat(ToolCalcConditionalBase):
    """Add a conditional formatting rule to a cell range."""

    name: str | None = "add_conditional_format"
    intent: str | None = "edit"
    description: str = (
        "Add a conditional formatting rule to a Calc cell range. "
        "Applies a cell style when the condition is met. "
        "Operators: EQUAL, NOT_EQUAL, GREATER, GREATER_EQUAL, LESS, "
        "LESS_EQUAL, BETWEEN, NOT_BETWEEN, FORMULA, DUPLICATE, NOT_DUPLICATE. "
        "DUPLICATE and NOT_DUPLICATE highlight duplicate or unique values in the range; "
        "formula1 may be omitted or empty for those. "
        "Use formula2 for BETWEEN and NOT_BETWEEN."
    )
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "range": {"type": "array", "items": {"type": "string"}, "description": 'Cell range to apply the rule to (e.g. ["A1:D10"]).'},
            "operator": {"type": "string", "enum": _SCHEMA_OPERATORS, "description": "Condition operator."},
            "formula1": {"type": "string", "description": ("First formula/value. For FORMULA, the condition (e.g. 'A1>100'). For value comparisons, the threshold (e.g. '50'). Omit or leave empty for DUPLICATE / NOT_DUPLICATE.")},
            "formula2": {"type": "string", "description": "Second value (required for BETWEEN and NOT_BETWEEN)."},
            "style": {"type": "string", "description": ("Cell style to apply when condition is true. Use style_list with family='CellStyles' to see available styles.")},
        },
        "required": ["range", "operator", "style"],
    }
    is_mutation: bool | None = True

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        bridge = CalcBridge(ctx.doc)
        ranges = _range_args(kwargs.get("range"))
        if len(ranges) != 1:
            return self._tool_error("Pass exactly one range.")
        range_str = ranges[0]

        operator = kwargs["operator"]
        style_name = kwargs["style"]
        formula1 = kwargs.get("formula1") or ""
        formula2 = kwargs.get("formula2") or ""

        try:
            op_upper = operator.upper()
            op_code = _NAME_TO_CODE.get(op_upper)
            if op_code is None or op_upper == "NONE":
                return self._tool_error(f"Unknown condition operator: {operator}", code="INVALID_OPERATOR")

            if op_upper in ("BETWEEN", "NOT_BETWEEN") and not formula2.strip():
                return self._tool_error("formula2 is required for BETWEEN and NOT_BETWEEN.", code="MISSING_FORMULA")
            if op_upper not in ("DUPLICATE", "NOT_DUPLICATE") and not formula1.strip():
                return self._tool_error("formula1 is required for this operator.", code="MISSING_FORMULA")

            cell_styles = ctx.doc.getStyleFamilies().getByName("CellStyles")
            if not cell_styles.hasByName(style_name):
                available = list(cell_styles.getElementNames())
                matches = difflib.get_close_matches(style_name, available, n=3)
                match_str = f" Close matches: {', '.join(matches)}" if matches else ""
                return self._tool_error(f"Unknown cell style '{style_name}'.{match_str}", code="INVALID_STYLE")

            cell_range = bridge.resolve_range_or_address(range_str)
            ra = cell_range.getRangeAddress()
            addr = CellAddress()
            addr.Sheet = ra.Sheet
            addr.Column = ra.StartColumn
            addr.Row = ra.StartRow

            props = [
                create_property_value("Operator", op_code),
                create_property_value("Formula1", formula1),
                create_property_value("SourcePosition", addr),
            ]
            if formula2:
                props.append(create_property_value("Formula2", formula2))
            props.append(create_property_value("StyleName", style_name))

            formats = _ensure_table_conditional_format(ctx, cell_range)
            formats.addNew(tuple(props))
            cell_range.setPropertyValue("ConditionalFormat", formats)

            log.info("Conditional format added to %s.", range_str.upper())
            count = formats.getCount()

            return {"status": "ok", "range": range_str, "rule_count": count}
        except IllegalArgumentException as e:
            log.exception("Add conditional format failed")
            return self._tool_error(f"Failed to add conditional format: {str(e)}", code="CONDITIONAL_FORMAT_ERROR")


class RemoveConditionalFormats(ToolCalcConditionalBase):
    """Remove or clear conditional formatting rules from a cell range."""

    name: str | None = "remove_conditional_formats"
    intent: str | None = "edit"
    description: str = "Remove a conditional formatting rule from a Calc cell range by index, or clear all rules if no index is provided. Use list_conditional_formats to see current rules and their indices."
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {"range": {"type": "array", "items": {"type": "string"}, "description": 'Cell range (e.g. ["A1:D10"]).'}, "rule_index": {"type": "integer", "description": "0-based index of the rule to remove. If omitted, all rules are cleared."}},
        "required": ["range"],
    }
    is_mutation: bool | None = True

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        bridge = CalcBridge(ctx.doc)
        ranges = _range_args(kwargs.get("range"))
        if len(ranges) != 1:
            return self._tool_error("Pass exactly one range.")
        range_str = ranges[0]
        index = kwargs.get("rule_index")

        try:
            cell_range = bridge.resolve_range_or_address(range_str)
            formats = cell_range.getPropertyValue("ConditionalFormat")

            if index is not None:
                if formats is None or formats.getCount() == 0:
                    return self._tool_error(f"No conditional formats on {range_str}.")
                if 0 <= index < formats.getCount():
                    formats.removeByIndex(index)
                    cell_range.setPropertyValue("ConditionalFormat", formats)
                    return {"status": "ok", "range": range_str, "removed_index": index}
                return self._tool_error(f"Rule index {index} not found on {range_str}.")
            if formats is not None:
                formats.clear()
                cell_range.setPropertyValue("ConditionalFormat", formats)
            return {"status": "ok", "range": range_str, "cleared": True}

        except IllegalArgumentException as e:
            log.exception("Remove conditional formats failed")
            return self._tool_error(f"Failed to remove conditional formats: {str(e)}", code="CONDITIONAL_FORMAT_ERROR")
