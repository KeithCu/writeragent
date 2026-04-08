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
"""Calc sheet management tools.

Each tool is a ToolBase subclass that instantiates CalcBridge and the
appropriate helper class per call using ``ctx.doc``.
"""

import logging

from plugin.framework.errors import ToolExecutionError, UnoObjectError
from plugin.modules.calc.base import ToolCalcSheetBase
from plugin.modules.calc.bridge import CalcBridge
from plugin.modules.calc.analyzer import SheetAnalyzer

logger = logging.getLogger("writeragent.calc")


class ListSheets(ToolCalcSheetBase):
    """List all sheet names in the workbook."""

    name = "list_sheets"
    description = "Lists all sheet names in the workbook."
    parameters = {
        "type": "object",
        "properties": {},
    }
    is_mutation = False

    def execute(self, ctx, **kwargs):
        bridge = CalcBridge(ctx.doc)
        try:
            doc = bridge.get_active_document()
            sheets = doc.getSheets()
            sheet_names = []
            for i in range(sheets.getCount()):
                sheet = sheets.getByIndex(i)
                sheet_names.append(sheet.getName())
            logger.info("Sheets listed: %s", sheet_names)
            return {"status": "ok", "result": sheet_names}
        except Exception as e:
            logger.error("Sheet listing error: %s", str(e))
            raise ToolExecutionError(str(e)) from e

class SwitchSheet(ToolCalcSheetBase):
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
    is_mutation = True

    def execute(self, ctx, **kwargs):
        bridge = CalcBridge(ctx.doc)
        sheet_name = kwargs["sheet_name"]

        try:
            doc = bridge.get_active_document()
            sheets = doc.getSheets()
            if not sheets.hasByName(sheet_name):
                raise UnoObjectError(f"No sheet found named '{sheet_name}'.")
            sheet = sheets.getByName(sheet_name)
            controller = doc.getCurrentController()
            controller.setActiveSheet(sheet)
            logger.info("Switched to sheet: %s", sheet_name)
            result = f"Switched to sheet '{sheet_name}'."
            return {"status": "ok", "message": result}
        except Exception as e:
            logger.error("Sheet switch error (%s): %s", sheet_name, str(e))
            raise ToolExecutionError(str(e)) from e

class CreateSheet(ToolCalcSheetBase):
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
    is_mutation = True

    def execute(self, ctx, **kwargs):
        bridge = CalcBridge(ctx.doc)
        sheet_name = kwargs["sheet_name"]
        position = kwargs.get("position")

        try:
            doc = bridge.get_active_document()
            sheets = doc.getSheets()
            if position is None:
                position = sheets.getCount()
            sheets.insertNewByName(sheet_name, position)
            logger.info("New sheet created: %s (position: %d)", sheet_name, position)
            result = f"New sheet named '{sheet_name}' created."
            return {"status": "ok", "message": result}
        except Exception as e:
            logger.error("Sheet creation error (%s): %s", sheet_name, str(e))
            raise ToolExecutionError(str(e)) from e

class GetSheetSummary(ToolCalcSheetBase):
    """Return a summary of a sheet."""

    name = "get_sheet_summary"
    description = (
        "Returns a comprehensive summary of the active or specified sheet: "
        "used area, column headers, charts, merged cells, annotations, and shapes."
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
    is_mutation = False

    def execute(self, ctx, **kwargs):
        bridge = CalcBridge(ctx.doc)
        analyzer = SheetAnalyzer(bridge)
        sheet_name = kwargs.get("sheet_name")

        result = analyzer.get_sheet_summary(sheet_name=sheet_name)
        return {"status": "ok", "result": result}
