"""Calc sheet management tools.

Each tool is a ToolBase subclass that instantiates CalcBridge and the
appropriate helper class per call using ``ctx.doc``.
"""

import logging

from plugin.framework.tool_base import ToolBase
from plugin.modules.calc.bridge import CalcBridge
from plugin.modules.calc.manipulator import CellManipulator
from plugin.modules.calc.analyzer import SheetAnalyzer

logger = logging.getLogger("writeragent.calc")


class ListSheets(ToolBase):
    """List all sheet names in the workbook."""

    name = "list_sheets"
    description = "Lists all sheet names in the workbook."
    parameters = {
        "type": "object",
        "properties": {},
    }
    doc_types = ["calc"]
    tier = "core"
    is_mutation = False

    def execute(self, ctx, **kwargs):
        bridge = CalcBridge(ctx.doc)
        manipulator = CellManipulator(bridge)

        try:
            result = manipulator.list_sheets()
            return {"status": "ok", "result": result}
        except Exception as e:
            logger.exception("list_sheets failed")
            return {"status": "error", "error": str(e)}


class SwitchSheet(ToolBase):
    """Switch to a specified sheet."""

    name = "switch_sheet"
    intent = "edit"
    description = "Switches to the specified sheet (makes it active)."
    parameters = {
        "type": "object",
        "properties": {
            "sheet_name": {
                "type": "string",
                "description": "Name of the sheet to switch to",
            },
        },
        "required": ["sheet_name"],
    }
    doc_types = ["calc"]
    is_mutation = True

    def execute(self, ctx, **kwargs):
        bridge = CalcBridge(ctx.doc)
        manipulator = CellManipulator(bridge)
        sheet_name = kwargs["sheet_name"]

        try:
            result = manipulator.switch_sheet(sheet_name)
            return {"status": "ok", "message": result}
        except Exception as e:
            logger.exception("switch_sheet failed")
            return {"status": "error", "error": str(e)}


class CreateSheet(ToolBase):
    """Create a new sheet."""

    name = "create_sheet"
    intent = "edit"
    description = "Creates a new sheet."
    parameters = {
        "type": "object",
        "properties": {
            "sheet_name": {
                "type": "string",
                "description": "New sheet name",
            },
            "position": {
                "type": "integer",
                "description": (
                    "Sheet position (0-based). Appended to end if not "
                    "specified."
                ),
            },
        },
        "required": ["sheet_name"],
    }
    doc_types = ["calc"]
    is_mutation = True

    def execute(self, ctx, **kwargs):
        bridge = CalcBridge(ctx.doc)
        manipulator = CellManipulator(bridge)
        sheet_name = kwargs["sheet_name"]
        position = kwargs.get("position")

        try:
            result = manipulator.create_sheet(sheet_name, position=position)
            return {"status": "ok", "message": result}
        except Exception as e:
            logger.exception("create_sheet failed")
            return {"status": "error", "error": str(e)}


class GetSheetSummary(ToolBase):
    """Return a summary of a sheet."""

    name = "get_sheet_summary"
    description = (
        "Returns a summary of the active or specified sheet (size, "
        "used cells, column headers, etc.)"
    )
    parameters = {
        "type": "object",
        "properties": {
            "sheet_name": {
                "type": "string",
                "description": "Sheet name (active sheet if empty)",
            },
        },
        "required": [],
    }
    doc_types = ["calc"]
    tier = "core"
    is_mutation = False

    def execute(self, ctx, **kwargs):
        bridge = CalcBridge(ctx.doc)
        analyzer = SheetAnalyzer(bridge)
        sheet_name = kwargs.get("sheet_name")

        try:
            result = analyzer.get_sheet_summary(sheet_name=sheet_name)
            return {"status": "ok", "result": result}
        except Exception as e:
            logger.exception("get_sheet_summary failed")
            return {"status": "error", "error": str(e)}


class CreateChart(ToolBase):
    """Create a chart from data."""

    name = "create_chart"
    intent = "edit"
    description = (
        "Creates a chart from data. Supports bar, column, line, pie, "
        "or scatter charts."
    )
    parameters = {
        "type": "object",
        "properties": {
            "data_range": {
                "type": "string",
                "description": "Range for chart data (e.g. A1:B10)",
            },
            "chart_type": {
                "type": "string",
                "enum": ["bar", "line", "pie", "scatter", "column"],
                "description": "Chart type",
            },
            "title": {
                "type": "string",
                "description": "Chart title",
            },
            "position": {
                "type": "string",
                "description": "Cell where chart will be placed (e.g. E1)",
            },
            "has_header": {
                "type": "boolean",
                "description": (
                    "Is first row/column a label? Default: true"
                ),
            },
        },
        "required": ["data_range", "chart_type"],
    }
    doc_types = ["calc"]
    is_mutation = True

    def execute(self, ctx, **kwargs):
        bridge = CalcBridge(ctx.doc)
        manipulator = CellManipulator(bridge)
        data_range = kwargs["data_range"]
        chart_type = kwargs["chart_type"]
        title = kwargs.get("title")
        position = kwargs.get("position")
        has_header = kwargs.get("has_header", True)

        try:
            result = manipulator.create_chart(
                data_range, chart_type,
                title=title, position=position, has_header=has_header,
            )
            return {"status": "ok", "message": result}
        except Exception as e:
            logger.exception("create_chart failed")
            return {"status": "error", "error": str(e)}
