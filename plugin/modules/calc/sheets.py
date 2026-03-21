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

        result = manipulator.list_sheets()
        return {"status": "ok", "result": result}
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

        result = manipulator.switch_sheet(sheet_name)
        return {"status": "ok", "message": result}
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

        result = manipulator.create_sheet(sheet_name, position=position)
        return {"status": "ok", "message": result}
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

        result = analyzer.get_sheet_summary(sheet_name=sheet_name)
        return {"status": "ok", "result": result}
