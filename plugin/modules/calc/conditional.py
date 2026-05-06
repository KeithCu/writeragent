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

import logging
from typing import Any

from plugin.framework.errors import ToolExecutionError, UnoObjectError
from plugin.modules.calc.base import ToolCalcConditionalBase
from plugin.modules.calc.bridge import CalcBridge

logger = logging.getLogger("writeragent.calc")

# LibreOffice ``ConditionOperator`` / ``ConditionOperator2`` code labels (no UNO import).
_CONDITION_OPERATOR_CODE_NAMES: tuple[str, ...] = ("NONE", "EQUAL", "NOT_EQUAL", "GREATER", "GREATER_EQUAL", "LESS", "LESS_EQUAL", "BETWEEN", "NOT_BETWEEN", "FORMULA", "DUPLICATE", "NOT_DUPLICATE")


def condition_operator_code_to_name(code: int) -> str:
    """Map UNO condition operator *code* (long) to a stable string label."""
    if 0 <= code < len(_CONDITION_OPERATOR_CODE_NAMES):
        return _CONDITION_OPERATOR_CODE_NAMES[code]
    return str(int(code))


def _query_interface(obj: Any, typename: str) -> Any:
    """PyUNO requires ``uno.getTypeByName`` for ``queryInterface``; imported IDL classes fail."""
    import uno

    return obj.queryInterface(uno.getTypeByName(typename))


def _entry_to_dict(entry: Any, idx: int) -> dict[str, Any]:
    """Convert a conditional entry to a readable dict."""
    result: dict[str, Any] = {"index": idx}
    op_name: str | None = None
    op_code: int | None = None
    try:
        xc2 = _query_interface(entry, "com.sun.star.sheet.XSheetCondition2")
        if xc2 is not None:
            op_code = int(xc2.getConditionOperator())
            op_name = condition_operator_code_to_name(op_code)
    except Exception:
        pass
    if op_name is None:
        try:
            op = entry.getOperator()
            op_name = str(op.value) if hasattr(op, "value") else str(op)
        except Exception:
            pass
    if op_name:
        result["operator"] = op_name
    if op_code is not None:
        result["operator_code"] = op_code
    try:
        f1 = entry.getFormula1()
        if f1:
            result["formula1"] = f1
    except Exception:
        pass
    try:
        f2 = entry.getFormula2()
        if f2 and f2 != "0":
            result["formula2"] = f2
    except Exception:
        pass
    try:
        sn = entry.getStyleName()
        if sn:
            result["style_name"] = sn
    except Exception:
        pass

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

    name = "list_conditional_formats"
    intent = "navigate"
    description = "List conditional formatting rules on a Calc cell range. Returns operator, formulas, and applied cell style for each rule. Extended LibreOffice operators (e.g. DUPLICATE) use operator_code when present."
    parameters = {"type": "object", "properties": {"range_name": {"type": "string", "description": "Cell range (e.g. 'A1:D10'). If omitted, scans used area."}}, "required": []}

    def execute(self, ctx, **kwargs):
        bridge = CalcBridge(ctx.doc)
        range_str = kwargs.get("range_name")

        try:
            sheet = bridge.get_active_sheet()
            if range_str:
                cell_range = bridge.get_cell_range(sheet, range_str)
            else:
                cursor = sheet.createCursor()
                cursor.gotoStartOfUsedArea(False)
                cursor.gotoEndOfUsedArea(True)
                cell_range = cursor

            formats = cell_range.getPropertyValue("ConditionalFormat")
            if formats is None or formats.getCount() == 0:
                rules = []
            else:
                rules = []
                for i in range(formats.getCount()):
                    entry = formats.getByIndex(i)
                    rules.append(_entry_to_dict(entry, i))

            return {"status": "ok", "range_name": range_str or "(used area)", "rules": rules, "count": len(rules)}
        except Exception as e:
            logger.error("List conditional formats error: %s", str(e))
            raise ToolExecutionError(str(e)) from e


class AddConditionalFormat(ToolCalcConditionalBase):
    """Add a conditional formatting rule to a cell range."""

    name = "add_conditional_format"
    intent = "edit"
    description = (
        "Add a conditional formatting rule to a Calc cell range. "
        "Applies a cell style when the condition is met. "
        "Operators: EQUAL, NOT_EQUAL, GREATER, GREATER_EQUAL, LESS, "
        "LESS_EQUAL, BETWEEN, NOT_BETWEEN, FORMULA, DUPLICATE, NOT_DUPLICATE. "
        "DUPLICATE and NOT_DUPLICATE highlight duplicate or unique values in the range; "
        "formula1 may be omitted or empty for those. "
        "Use formula2 for BETWEEN and NOT_BETWEEN."
    )
    parameters = {
        "type": "object",
        "properties": {
            "range_name": {"type": "string", "description": "Cell range to apply the rule to (e.g. 'A1:D10')."},
            "operator": {"type": "string", "enum": ["EQUAL", "NOT_EQUAL", "GREATER", "GREATER_EQUAL", "LESS", "LESS_EQUAL", "BETWEEN", "NOT_BETWEEN", "FORMULA", "DUPLICATE", "NOT_DUPLICATE"], "description": "Condition operator."},
            "formula1": {"type": "string", "description": ("First formula/value. For FORMULA, the condition (e.g. 'A1>100'). For value comparisons, the threshold (e.g. '50'). Omit or leave empty for DUPLICATE / NOT_DUPLICATE.")},
            "formula2": {"type": "string", "description": "Second value (required for BETWEEN and NOT_BETWEEN)."},
            "style_name": {"type": "string", "description": ("Cell style to apply when condition is true. Use list_styles with family='CellStyles' to see available styles.")},
        },
        "required": ["range_name", "operator", "style_name"],
    }
    is_mutation = True

    def execute(self, ctx, **kwargs):
        bridge = CalcBridge(ctx.doc)
        range_str = kwargs["range_name"]
        operator = kwargs["operator"]
        style_name = kwargs["style_name"]
        formula1 = kwargs.get("formula1") or ""
        formula2 = kwargs.get("formula2") or ""

        try:
            from com.sun.star.beans import PropertyValue
            from com.sun.star.sheet.ConditionOperator import BETWEEN, EQUAL, FORMULA, GREATER, GREATER_EQUAL, LESS, LESS_EQUAL, NONE, NOT_BETWEEN, NOT_EQUAL

            try:
                from com.sun.star.sheet import ConditionOperator2 as CO2

                dup_a = int(CO2.DUPLICATE)
                dup_b = int(CO2.NOT_DUPLICATE)
            except Exception:
                dup_a, dup_b = 10, 11

            op_upper = operator.upper()
            op_map = {"NONE": NONE, "EQUAL": EQUAL, "NOT_EQUAL": NOT_EQUAL, "GREATER": GREATER, "GREATER_EQUAL": GREATER_EQUAL, "LESS": LESS, "LESS_EQUAL": LESS_EQUAL, "BETWEEN": BETWEEN, "NOT_BETWEEN": NOT_BETWEEN, "FORMULA": FORMULA, "DUPLICATE": dup_a, "NOT_DUPLICATE": dup_b}

            op_val = op_map.get(op_upper)
            if op_val is None:
                raise UnoObjectError(f"Unknown condition operator: {operator}")

            if op_upper in ("BETWEEN", "NOT_BETWEEN") and not formula2.strip():
                raise UnoObjectError("formula2 is required for BETWEEN and NOT_BETWEEN.")
            if op_upper not in ("DUPLICATE", "NOT_DUPLICATE") and not formula1.strip():
                raise UnoObjectError("formula1 is required for this operator.")

            sheet = bridge.get_active_sheet()
            cell_range = bridge.get_cell_range(sheet, range_str)

            props = []
            pv = PropertyValue()
            pv.Name = "Operator"
            pv.Value = op_val
            props.append(pv)

            pv = PropertyValue()
            pv.Name = "Formula1"
            pv.Value = formula1
            props.append(pv)

            if formula2:
                pv = PropertyValue()
                pv.Name = "Formula2"
                pv.Value = formula2
                props.append(pv)

            pv = PropertyValue()
            pv.Name = "StyleName"
            pv.Value = style_name
            props.append(pv)

            formats = _ensure_table_conditional_format(ctx, cell_range)
            formats.addNew(tuple(props))
            cell_range.setPropertyValue("ConditionalFormat", formats)

            logger.info("Conditional format added to %s.", range_str.upper())
            count = formats.getCount()

            return {"status": "ok", "range_name": range_str, "rule_count": count}
        except Exception as e:
            logger.error("Add conditional format error: %s", str(e))
            raise ToolExecutionError(str(e)) from e


class RemoveConditionalFormats(ToolCalcConditionalBase):
    """Remove or clear conditional formatting rules from a cell range."""

    name = "remove_conditional_formats"
    intent = "edit"
    description = "Remove a conditional formatting rule from a Calc cell range by index, or clear all rules if no index is provided. Use list_conditional_formats to see current rules and their indices."
    parameters = {"type": "object", "properties": {"range_name": {"type": "string", "description": "Cell range (e.g. 'A1:D10')."}, "rule_index": {"type": "integer", "description": "0-based index of the rule to remove. If omitted, all rules are cleared."}}, "required": ["range_name"]}
    is_mutation = True

    def execute(self, ctx, **kwargs):
        bridge = CalcBridge(ctx.doc)
        range_str = kwargs["range_name"]
        index = kwargs.get("rule_index")

        try:
            sheet = bridge.get_active_sheet()
            cell_range = bridge.get_cell_range(sheet, range_str)
            formats = cell_range.getPropertyValue("ConditionalFormat")

            if index is not None:
                if formats is None or formats.getCount() == 0:
                    return self._tool_error(f"No conditional formats on {range_str}.")
                if 0 <= index < formats.getCount():
                    formats.removeByIndex(index)
                    cell_range.setPropertyValue("ConditionalFormat", formats)
                    return {"status": "ok", "range_name": range_str, "removed_index": index}
                return self._tool_error(f"Rule index {index} not found on {range_str}.")
            if formats is not None:
                formats.clear()
                cell_range.setPropertyValue("ConditionalFormat", formats)
            return {"status": "ok", "range_name": range_str, "cleared": True}

        except Exception as e:
            logger.error("Remove conditional formats error: %s", str(e))
            raise ToolExecutionError(str(e)) from e
