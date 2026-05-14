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
from plugin.framework.tool import ToolBase
from plugin.calc.base import ToolCalcSheetBase
from plugin.calc.bridge import CalcBridge
from plugin.calc.analyzer import SheetAnalyzer

logger = logging.getLogger("writeragent.calc")


class ListSheets(ToolCalcSheetBase):
    """List all sheet names in the workbook."""

    name = "list_sheets"
    description = "Lists all sheet names in the workbook."
    parameters = {"type": "object", "properties": {}}
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
    parameters = {"type": "object", "properties": {"sheet_name": {"type": "string", "description": "Name of the sheet to switch to"}}, "required": ["sheet_name"]}
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
    parameters = {"type": "object", "properties": {"sheet_name": {"type": "string", "description": "New sheet name"}, "position": {"type": "integer", "description": ("Sheet position (0-based). Appended to end if not specified.")}}, "required": ["sheet_name"]}
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


class RenameSheet(ToolCalcSheetBase):
    """Rename an existing sheet."""

    name = "rename_sheet"
    intent = "edit"
    description = "Renames an existing sheet."
    parameters = {"type": "object", "properties": {"old_name": {"type": "string", "description": "Current name of the sheet"}, "new_name": {"type": "string", "description": "New name for the sheet"}}, "required": ["old_name", "new_name"]}
    is_mutation = True

    def execute(self, ctx, **kwargs):
        bridge = CalcBridge(ctx.doc)
        old_name = kwargs["old_name"]
        new_name = kwargs["new_name"]

        try:
            doc = bridge.get_active_document()
            sheets = doc.getSheets()
            if not sheets.hasByName(old_name):
                raise UnoObjectError(f"No sheet found named '{old_name}'.")
            sheet = sheets.getByName(old_name)
            sheet.setName(new_name)
            logger.info("Sheet renamed from '%s' to '%s'.", old_name, new_name)
            return {"status": "ok", "message": f"Sheet renamed to '{new_name}'."}
        except Exception as e:
            logger.error("Sheet rename error (%s): %s", old_name, str(e))
            raise ToolExecutionError(str(e)) from e


class DeleteSheet(ToolCalcSheetBase):
    """Delete an existing sheet."""

    name = "delete_sheet"
    intent = "edit"
    description = "Deletes an existing sheet by name."
    parameters = {"type": "object", "properties": {"sheet_name": {"type": "string", "description": "Name of the sheet to delete"}}, "required": ["sheet_name"]}
    is_mutation = True

    def execute(self, ctx, **kwargs):
        bridge = CalcBridge(ctx.doc)
        sheet_name = kwargs["sheet_name"]

        try:
            doc = bridge.get_active_document()
            sheets = doc.getSheets()
            if not sheets.hasByName(sheet_name):
                raise UnoObjectError(f"No sheet found named '{sheet_name}'.")
            if sheets.getCount() <= 1:
                return self._tool_error("Cannot delete the only sheet in the document.")
            sheets.removeByName(sheet_name)
            logger.info("Sheet deleted: %s", sheet_name)
            return {"status": "ok", "message": f"Sheet '{sheet_name}' deleted."}
        except Exception as e:
            logger.error("Sheet deletion error (%s): %s", sheet_name, str(e))
            raise ToolExecutionError(str(e)) from e


class ProtectSheet(ToolCalcSheetBase):
    """Protect or unprotect a sheet."""

    name = "protect_sheet"
    intent = "edit"
    description = "Protects or unprotects a sheet. When protected, cells cannot be edited unless they are explicitly unlocked."
    parameters = {"type": "object", "properties": {"sheet_name": {"type": "string", "description": "Sheet name (active sheet if empty)"}, "protect": {"type": "boolean", "description": "True to protect, False to unprotect (default: True)"}}, "required": []}
    is_mutation = True

    def execute(self, ctx, **kwargs):
        bridge = CalcBridge(ctx.doc)
        sheet_name = kwargs.get("sheet_name")
        should_protect = kwargs.get("protect", True)

        try:
            doc = bridge.get_active_document()
            if sheet_name:
                sheets = doc.getSheets()
                if not sheets.hasByName(sheet_name):
                    raise UnoObjectError(f"No sheet found named '{sheet_name}'.")
                sheet = sheets.getByName(sheet_name)
            else:
                sheet = bridge.get_active_sheet()

            if should_protect:
                sheet.protect("")
                msg = f"Sheet '{sheet.getName()}' is now protected."
            else:
                sheet.unprotect("")
                msg = f"Sheet '{sheet.getName()}' is now unprotected."

            logger.info(msg)
            return {"status": "ok", "message": msg}
        except Exception as e:
            logger.error("Sheet protection error: %s", str(e))
            raise ToolExecutionError(str(e)) from e


class GetSheetSummary(ToolBase):
    """Return a summary of a sheet."""

    name = "get_sheet_summary"
    description = "Returns a comprehensive summary of the active or specified sheet: used area, column headers, charts, merged cells, annotations, and shapes."
    parameters = {"type": "object", "properties": {"sheet_name": {"type": "string", "description": "Sheet name (active sheet if empty)"}}, "required": []}
    uno_services = ["com.sun.star.sheet.SpreadsheetDocument"]
    tier = "extended"
    is_mutation = False

    def execute(self, ctx, **kwargs):
        bridge = CalcBridge(ctx.doc)
        analyzer = SheetAnalyzer(bridge)
        sheet_name = kwargs.get("sheet_name")

        result = analyzer.get_sheet_summary(sheet_name=sheet_name)
        return {"status": "ok", "result": result}
