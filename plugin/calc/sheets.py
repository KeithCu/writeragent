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

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from plugin.framework.errors import is_real_disposal
from plugin.framework.prompts import get_sheets_create_completion_instruction
from plugin.framework.tool import ToolBase

if TYPE_CHECKING:
    from plugin.framework.tool import ToolContext

from plugin.calc.base import ToolCalcSheetBase
from plugin.calc.bridge import CalcBridge, filter_agent_sheet_names, is_agent_visible_sheet
from plugin.calc.analyzer import SheetAnalyzer

log = logging.getLogger("writeragent.calc")

_INVALID_SHEET_CHARS = set("[]*?:/\\")


def _sheet_name_error(doc: Any, name: Any, *, exclude: str | None = None) -> str | None:
    """Validate sheet name for create or rename.

    Returns an error message string if invalid, or None if valid.
    Rejects empty/whitespace names, invalid characters ([]*?:/\\),
    leading underscores (reserved for internal sheets), and case-insensitive
    duplicates (ignoring `exclude`).
    """
    if not isinstance(name, str) or not name.strip():
        return "Sheet name cannot be empty."
    for ch in name:
        if ch in _INVALID_SHEET_CHARS:
            return f"Sheet name cannot contain '{ch}'."
    if name.startswith("_"):
        return "Sheet names starting with '_' are reserved for internal sheets."
    sheets = doc.getSheets()
    target_lower = name.casefold()
    exclude_lower = exclude.casefold() if exclude is not None else None
    for existing in sheets.getElementNames():
        if exclude_lower is not None and existing.casefold() == exclude_lower:
            continue
        if existing.casefold() == target_lower:
            return f"A sheet named '{existing}' already exists in the document."
    return None


class ListSheets(ToolCalcSheetBase):
    """List all sheet names in the workbook."""

    name: str | None = "list_sheets"
    description: str = "Lists user-visible sheet names in the workbook (omits LibreOffice internal tabs)."
    parameters: dict[str, Any] | None = {"type": "object", "properties": {}}
    is_mutation: bool | None = False

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        doc = ctx.doc
        sheet_names = filter_agent_sheet_names(doc.getSheets().getElementNames())
        log.info("Sheets listed: %s", sheet_names)
        return {"status": "ok", "result": sheet_names}


class SwitchSheet(ToolCalcSheetBase):
    """Switch to a specified sheet."""

    name: str | None = "switch_sheet"
    intent: str | None = "edit"
    description: str = "Switches to the specified sheet (makes it active)."
    parameters: dict[str, Any] | None = {"type": "object", "properties": {"sheet": {"type": "string", "description": "Name of the sheet to switch to"}}, "required": ["sheet"]}
    is_mutation: bool | None = True

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        sheet_name = kwargs["sheet"]

        doc = ctx.doc
        sheet = CalcBridge(doc).get_sheet(sheet_name)
        controller = doc.getCurrentController()
        controller.setActiveSheet(sheet)
        log.info("Switched to sheet: %s", sheet_name)
        result = f"Switched to sheet '{sheet_name}'."
        return {"status": "ok", "message": result}


class CreateSheet(ToolCalcSheetBase):
    """Create a new sheet."""

    name: str | None = "create_sheet"
    intent: str | None = "edit"
    description: str = "Creates a new empty sheet (tab exists; no cells copied). create_sheet is not Sample/deliverable populate — after create, write_formula_range with source to copy a block onto the new sheet."
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {"sheet": {"type": "string", "description": ("Exact new sheet title from the request; preserve spaces (do not snake_case or invent aliases).")}, "position": {"type": "integer", "description": "Sheet position (0-based). Appended to end if not specified."}},
        "required": ["sheet"],
    }
    is_mutation: bool | None = True

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        sheet_name = kwargs["sheet"]
        position = kwargs.get("position")

        doc = ctx.doc
        err = _sheet_name_error(doc, sheet_name)
        if err:
            return self._tool_error(err)

        sheets = doc.getSheets()
        count = sheets.getCount()
        if position is None:
            position = count
        elif not isinstance(position, int) or isinstance(position, bool) or position < 0 or position > count:
            return self._tool_error(f"Position {position} is out of range (0..{count}).")

        sheets.insertNewByName(sheet_name, position)
        log.info("New sheet created: %s (position: %d)", sheet_name, position)
        result = (
            f"New sheet named '{sheet_name}' created; no cells copied. "
            f"Not activated: qualify writes with the sheet name (e.g. '{sheet_name}'.A1)."
        )
        return {
            "status": "ok",
            "sheet": sheet_name,
            "message": result,
            "instruction": get_sheets_create_completion_instruction(),
        }


class RenameSheet(ToolCalcSheetBase):
    """Rename an existing sheet."""

    name: str | None = "rename_sheet"
    intent: str | None = "edit"
    description: str = "Renames an existing sheet."
    parameters: dict[str, Any] | None = {"type": "object", "properties": {"old_name": {"type": "string", "description": "Current name of the sheet"}, "new_name": {"type": "string", "description": "New name for the sheet"}}, "required": ["old_name", "new_name"]}
    is_mutation: bool | None = True

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        old_name = kwargs["old_name"]
        new_name = kwargs["new_name"]

        doc = ctx.doc
        sheet = CalcBridge(doc).get_sheet(old_name)
        err = _sheet_name_error(doc, new_name, exclude=old_name)
        if err:
            return self._tool_error(err)
        sheet.setName(new_name)
        if sheet.getName() != new_name:
            return self._tool_error(f"LibreOffice did not rename '{old_name}' (invalid name or read-only document).")
        from plugin.calc.python.sheet_modify import migrate_sheet_rename

        migrate_sheet_rename(doc, old_name, new_name)
        log.info("Sheet renamed from '%s' to '%s'.", old_name, new_name)
        return {"status": "ok", "message": f"Sheet renamed to '{new_name}'."}


class DeleteSheet(ToolCalcSheetBase):
    """Delete an existing sheet."""

    name: str | None = "delete_sheet"
    intent: str | None = "edit"
    description: str = "Deletes an existing sheet by name."
    parameters: dict[str, Any] | None = {"type": "object", "properties": {"sheet": {"type": "string", "description": "Name of the sheet to delete"}}, "required": ["sheet"]}
    is_mutation: bool | None = True

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        sheet_name = kwargs["sheet"]

        doc = ctx.doc
        CalcBridge(doc).get_sheet(sheet_name)
        if sheet_name.startswith("_"):
            return self._tool_error(f"Cannot delete internal sheet '{sheet_name}'.")
        sheets = doc.getSheets()
        visible_names = [n for n in sheets.getElementNames() if not n.startswith("_")]
        if len(visible_names) <= 1:
            return self._tool_error("Cannot delete the only sheet in the document.")
        sheets.removeByName(sheet_name)
        log.info("Sheet deleted: %s", sheet_name)
        return {"status": "ok", "message": f"Sheet '{sheet_name}' deleted."}


class ProtectSheet(ToolCalcSheetBase):
    """Protect or unprotect a sheet."""

    name: str | None = "protect_sheet"
    intent: str | None = "edit"
    description: str = "Protects or unprotects a sheet. When protected, cells cannot be edited unless they are explicitly unlocked."
    parameters: dict[str, Any] | None = {"type": "object", "properties": {"sheet": {"type": "string", "description": "Sheet name (active sheet if empty)"}, "protect": {"type": "boolean", "description": "True to protect, False to unprotect (default: True)"}}, "required": []}
    is_mutation: bool | None = True

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        sheet_name = kwargs.get("sheet")
        should_protect = kwargs.get("protect", True)

        doc = ctx.doc
        bridge = CalcBridge(doc)
        if sheet_name:
            sheet = bridge.get_sheet(sheet_name)
        else:
            sheet = bridge.get_active_sheet()

        actual_name = sheet.getName()
        is_currently_protected = sheet.isProtected()
        if should_protect and is_currently_protected:
            return {"status": "ok", "message": f"Sheet '{actual_name}' is already protected."}
        if not should_protect and not is_currently_protected:
            return {"status": "ok", "message": f"Sheet '{actual_name}' is already unprotected."}

        if should_protect:
            try:
                sheet.protect("")
            except Exception as e:
                if is_real_disposal(e):
                    raise
                return self._tool_error(f"Could not protect sheet '{actual_name}': {e}")
        else:
            try:
                sheet.unprotect("")
            except Exception as e:
                if is_real_disposal(e):
                    raise
                return self._tool_error(f"Sheet '{actual_name}' is password-protected; unprotect it in LibreOffice.")

        if sheet.isProtected() == is_currently_protected:
            target_state = "protected" if should_protect else "unprotected"
            return self._tool_error(f"Failed to set sheet '{actual_name}' to {target_state}.")

        if should_protect:
            msg = f"Sheet '{actual_name}' is now protected."
        else:
            msg = f"Sheet '{actual_name}' is now unprotected."

        log.info("%s", msg)
        return {"status": "ok", "message": msg}


class GetSheetSummary(ToolBase):
    """Return a summary of a sheet."""

    name: str | None = "get_sheet_summary"
    tier: str = "core"
    description: str = "Returns a comprehensive summary of the active or specified sheet: used area, column headers, charts, merged cells, annotations, and shapes."
    parameters: dict[str, Any] | None = {"type": "object", "properties": {"sheet": {"type": "string", "description": "Sheet name (active sheet if empty)"}}, "required": []}
    uno_services: list[str] | None = ["com.sun.star.sheet.SpreadsheetDocument"]
    is_mutation: bool | None = False

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        bridge = CalcBridge(ctx.doc)
        analyzer = SheetAnalyzer(bridge)
        sheet_name = kwargs.get("sheet")
        # Leading '_' tabs are LO internals (xlsx→ods ``__Anonymous_Sheet_DB__*``).
        # Omit from agent summaries; do not delete the sheet.
        if sheet_name and not is_agent_visible_sheet(sheet_name):
            return self._tool_error(f"No user-visible sheet named '{sheet_name}'.")

        result = analyzer.get_sheet_summary(sheet_name=sheet_name)
        if not is_agent_visible_sheet(result.get("sheet_name", "")):
            return self._tool_error("Active sheet is a LibreOffice internal tab; specify a user-visible sheet.")
        return {"status": "ok", "result": result}
