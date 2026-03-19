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

import logging

from plugin.framework.tool_base import ToolBase
from plugin.modules.calc.bridge import CalcBridge
from plugin.modules.calc.manipulator import CellManipulator

logger = logging.getLogger("writeragent.calc")


class ListConditionalFormats(ToolBase):
    """List conditional formatting rules on a cell range."""

    name = "list_conditional_formats"
    intent = "navigate"
    description = (
        "List conditional formatting rules on a Calc cell range. "
        "Returns operator, formulas, and applied cell style for each rule."
    )
    parameters = {
        "type": "object",
        "properties": {
            "range_name": {
                "type": "string",
                "description": "Cell range (e.g. 'A1:D10'). If omitted, scans used area.",
            },
        },
        "required": [],
    }
    doc_types = ["calc"]

    def execute(self, ctx, **kwargs):
        bridge = CalcBridge(ctx.doc)
        manipulator = CellManipulator(bridge)
        range_str = kwargs.get("range_name")
        try:
            rules = manipulator.list_conditional_formats(range_str)
            return {
                "status": "ok",
                "range_name": range_str or "(used area)",
                "rules": rules,
                "count": len(rules),
            }
        except Exception as e:
            logger.exception("list_conditional_formats failed")
            return self._tool_error(str(e))


class AddConditionalFormat(ToolBase):
    """Add a conditional formatting rule to a cell range."""

    name = "add_conditional_format"
    intent = "edit"
    description = (
        "Add a conditional formatting rule to a Calc cell range. "
        "Applies a cell style when the condition is met. "
        "Operators: EQUAL, NOT_EQUAL, GREATER, GREATER_EQUAL, LESS, "
        "LESS_EQUAL, BETWEEN, NOT_BETWEEN, FORMULA."
    )
    parameters = {
        "type": "object",
        "properties": {
            "range_name": {
                "type": "string",
                "description": "Cell range to apply the rule to (e.g. 'A1:D10').",
            },
            "operator": {
                "type": "string",
                "enum": [
                    "EQUAL", "NOT_EQUAL", "GREATER", "GREATER_EQUAL",
                    "LESS", "LESS_EQUAL", "BETWEEN", "NOT_BETWEEN", "FORMULA",
                ],
                "description": "Condition operator.",
            },
            "formula1": {
                "type": "string",
                "description": (
                    "First formula/value. For FORMULA operator, this is the "
                    "condition formula (e.g. 'A1>100'). For value operators, "
                    "the comparison value (e.g. '50')."
                ),
            },
            "formula2": {
                "type": "string",
                "description": "Second formula/value (only for BETWEEN/NOT_BETWEEN).",
            },
            "style_name": {
                "type": "string",
                "description": (
                    "Cell style to apply when condition is true. "
                    "Use list_styles with family='CellStyles' to see available styles."
                ),
            },
        },
        "required": ["range_name", "operator", "formula1", "style_name"],
    }
    doc_types = ["calc"]
    is_mutation = True

    def execute(self, ctx, **kwargs):
        bridge = CalcBridge(ctx.doc)
        manipulator = CellManipulator(bridge)
        try:
            count = manipulator.add_conditional_format(
                kwargs["range_name"],
                kwargs["operator"],
                kwargs["formula1"],
                kwargs["style_name"],
                kwargs.get("formula2", ""),
            )
            return {
                "status": "ok",
                "range_name": kwargs["range_name"],
                "rule_count": count,
            }
        except Exception as e:
            logger.exception("add_conditional_format failed")
            return self._tool_error(str(e))


class RemoveConditionalFormat(ToolBase):
    """Remove a conditional formatting rule from a cell range."""

    name = "remove_conditional_format"
    intent = "edit"
    description = (
        "Remove a conditional formatting rule from a Calc cell range by index. "
        "Use list_conditional_formats to see current rules and their indices."
    )
    parameters = {
        "type": "object",
        "properties": {
            "range_name": {
                "type": "string",
                "description": "Cell range (e.g. 'A1:D10').",
            },
            "rule_index": {
                "type": "integer",
                "description": "0-based index of the rule to remove.",
            },
        },
        "required": ["range_name", "rule_index"],
    }
    doc_types = ["calc"]
    is_mutation = True

    def execute(self, ctx, **kwargs):
        bridge = CalcBridge(ctx.doc)
        manipulator = CellManipulator(bridge)
        try:
            if manipulator.remove_conditional_format(kwargs["range_name"], kwargs["rule_index"]):
                return {"status": "ok", "range_name": kwargs["range_name"], "removed_index": kwargs["rule_index"]}
            else:
                return self._tool_error(f"Rule index {kwargs['rule_index']} not found on {kwargs['range_name']}.")
        except Exception as e:
            logger.exception("remove_conditional_format failed")
            return self._tool_error(str(e))


class ClearConditionalFormats(ToolBase):
    """Clear all conditional formatting from a cell range."""

    name = "clear_conditional_formats"
    intent = "edit"
    description = "Remove all conditional formatting rules from a Calc cell range."
    parameters = {
        "type": "object",
        "properties": {
            "range_name": {
                "type": "string",
                "description": "Cell range (e.g. 'A1:D10').",
            },
        },
        "required": ["range_name"],
    }
    doc_types = ["calc"]
    is_mutation = True

    def execute(self, ctx, **kwargs):
        bridge = CalcBridge(ctx.doc)
        manipulator = CellManipulator(bridge)
        try:
            manipulator.clear_conditional_formats(kwargs["range_name"])
            return {"status": "ok", "range_name": kwargs["range_name"], "cleared": True}
        except Exception as e:
            logger.exception("clear_conditional_formats failed")
            return self._tool_error(str(e))
