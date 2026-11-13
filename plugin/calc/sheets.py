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

from plugin.framework.errors import ToolExecutionError, is_disposal
from plugin.framework.prompts import SHEETS_CREATED_NOT_POPULATED_INSTRUCTION
from plugin.framework.tool import ToolBase

if TYPE_CHECKING:
    from plugin.framework.tool import ToolContext

from plugin.calc.base import ToolCalcSheetBase
from plugin.calc.bridge import CalcBridge, filter_agent_sheet_names, is_agent_visible_sheet
from plugin.calc.analyzer import SheetAnalyzer

log = logging.getLogger(__name__)

_INVALID_SHEET_CHARS = set("[]*?:/\\")


def _sheet_name_error(doc: Any, name: str, *, exclude: str | None = None) -> str | None:
    """Validate sheet name for create or rename.

    Returns an error message string if invalid, or None if valid.
    Rejects empty/whitespace names, invalid characters ([]*?:/\\),
    leading underscores (reserved for internal sheets), and case-insensitive
    duplicates (ignoring `exclude`).
    """
    if not name.strip():
        return "Sheet name cannot be empty."
    for ch in name:
        if ch in _INVALID_SHEET_CHARS:
            return f"Sheet name cannot contain '{ch}'."
    if name.startswith("_"):
        return "Sheet names starting with '_' are reserved for internal sheets."
    target_cf = name.casefold()
    exclude_cf = exclude.casefold() if exclude is not None else None
    for existing in doc.getSheets().getElementNames():
        folded = existing.casefold()
        if folded == target_cf and folded != exclude_cf:
            return f"A sheet named '{existing}' already exists in the document."
    return None


def _user_sheet(doc: Any, name: str, verb: str) -> Any:
    """Return sheet *name* for a sheet verb, refusing LibreOffice-internal tabs.

    A missing name raises CalcBridge.get_sheet's UnoObjectError, which lists the
    visible sheets. A leading-underscore tab (xlsx import leftovers such as
    __Anonymous_Sheet_DB__0) exists but is hidden from the agent, so every verb
    refuses it with the same sentence.
    """
    sheet = CalcBridge(doc).get_sheet(name)
    if not is_agent_visible_sheet(name):
        raise ToolExecutionError(f"Cannot {verb} internal sheet '{name}'.", code="INVALID_PARAM")
    return sheet


def _insert_position(sheets: Any, position: int | None) -> int:
    """0-based index for a new tab. Omitted appends; 0..count is valid (count appends).

    prepare_tool_call has already checked that *position* is an integer; only the
    bound depends on the workbook.
    """
    count = sheets.getCount()
    if position is None:
        return count
    if position < 0 or position > count:
        raise ToolExecutionError(f"Position {position} is out of range (0..{count}).", code="INVALID_PARAM")
    return position


class ListSheets(ToolCalcSheetBase):
    """List all sheet names in the workbook."""

    name: str | None = "sheet_list"
    description: str = (
        "Lists user-visible sheets (omits leading-`_` internal tabs). "
        "Each entry is {name, protected, visible}."
    )
    parameters: dict[str, Any] | None = {"type": "object", "properties": {}}
    is_mutation: bool | None = False

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        doc = ctx.doc
        sheets = doc.getSheets()
        entries: list[dict[str, Any]] = []
        for name in filter_agent_sheet_names(sheets.getElementNames()):
            sheet = sheets.getByName(name)
            entries.append(
                {
                    "name": name,
                    "protected": bool(sheet.isProtected()),
                    "visible": bool(sheet.getPropertyValue("IsVisible")),
                }
            )
        log.info("Sheets listed: %s", [entry["name"] for entry in entries])
        return {"status": "ok", "result": entries}


class SwitchSheet(ToolCalcSheetBase):
    """Switch to a specified sheet."""

    name: str | None = "sheet_switch"
    description: str = "Switches to the specified sheet (makes it active)."
    parameters: dict[str, Any] | None = {"type": "object", "properties": {"sheet": {"type": "string", "description": "Name of the sheet to switch to"}}, "required": ["sheet"]}
    is_mutation: bool | None = True

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        sheet_name = kwargs["sheet"]

        doc = ctx.doc
        sheet = _user_sheet(doc, sheet_name, "switch to")
        controller = doc.getCurrentController()
        controller.setActiveSheet(sheet)
        log.info("Switched to sheet: %s", sheet_name)
        return {"status": "ok", "message": f"Switched to sheet '{sheet_name}'."}


class CreateSheet(ToolCalcSheetBase):
    """Create a new sheet."""

    name: str | None = "sheet_create"
    description: str = "Creates a new empty sheet (tab exists; no cells copied). sheet_create is not Sample/deliverable populate — after create, write_formula_range with source to copy a block onto the new sheet."
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
            return self._tool_error(err, code="INVALID_PARAM")

        sheets = doc.getSheets()
        position = _insert_position(sheets, position)

        sheets.insertNewByName(sheet_name, position)
        log.info("New sheet created: %s (position: %d)", sheet_name, position)
        return {
            "status": "ok",
            "sheet": sheet_name,
            "message": (
                f"New sheet named '{sheet_name}' created; no cells copied. "
                f"Not activated: qualify writes with the sheet name (e.g. '{sheet_name}'.A1)."
            ),
            "instruction": SHEETS_CREATED_NOT_POPULATED_INSTRUCTION,
        }


class RenameSheet(ToolCalcSheetBase):
    """Rename an existing sheet."""

    name: str | None = "sheet_rename"
    description: str = "Renames an existing sheet."
    parameters: dict[str, Any] | None = {"type": "object", "properties": {"old_name": {"type": "string", "description": "Current name of the sheet"}, "new_name": {"type": "string", "description": "New name for the sheet"}}, "required": ["old_name", "new_name"]}
    is_mutation: bool | None = True

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        old_name = kwargs["old_name"]
        new_name = kwargs["new_name"]

        doc = ctx.doc
        sheet = _user_sheet(doc, old_name, "rename")
        err = _sheet_name_error(doc, new_name, exclude=old_name)
        if err:
            return self._tool_error(err, code="INVALID_PARAM")
        sheet.setName(new_name)
        if sheet.getName() != new_name:
            return self._tool_error(f"LibreOffice did not rename '{old_name}' (invalid name or read-only document).")
        from plugin.calc.python.sheet_modify import migrate_sheet_rename

        migrate_sheet_rename(doc, old_name, new_name)
        log.info("Sheet renamed from '%s' to '%s'.", old_name, new_name)
        return {"status": "ok", "message": f"Sheet renamed to '{new_name}'."}


class DeleteSheet(ToolCalcSheetBase):
    """Delete an existing sheet."""

    name: str | None = "sheet_delete"
    description: str = "Deletes an existing sheet by name."
    parameters: dict[str, Any] | None = {"type": "object", "properties": {"sheet": {"type": "string", "description": "Name of the sheet to delete"}}, "required": ["sheet"]}
    is_mutation: bool | None = True

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        sheet_name = kwargs["sheet"]

        doc = ctx.doc
        _user_sheet(doc, sheet_name, "delete")
        sheets = doc.getSheets()
        visible_names = filter_agent_sheet_names(sheets.getElementNames())
        if len(visible_names) <= 1:
            return self._tool_error("Cannot delete the only sheet in the document.", code="INVALID_PARAM")
        sheets.removeByName(sheet_name)
        log.info("Sheet deleted: %s", sheet_name)
        return {"status": "ok", "message": f"Sheet '{sheet_name}' deleted."}


class ProtectSheet(ToolCalcSheetBase):
    """Protect or unprotect a sheet."""

    name: str | None = "sheet_protect"
    description: str = "Protects or unprotects a sheet. When protected, cells cannot be edited unless they are explicitly unlocked."
    parameters: dict[str, Any] | None = {"type": "object", "properties": {"sheet": {"type": "string", "description": "Sheet name (active sheet if empty)"}, "protect": {"type": "boolean", "description": "True to protect, False to unprotect (default: True)"}}, "required": []}
    is_mutation: bool | None = True

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        sheet_name = kwargs.get("sheet")
        should_protect = kwargs.get("protect", True)

        doc = ctx.doc
        bridge = CalcBridge(doc)
        sheet = bridge.sheet_or_active(sheet_name)

        actual_name = sheet.getName()
        is_currently_protected = sheet.isProtected()
        state = "protected" if should_protect else "unprotected"
        if should_protect == is_currently_protected:
            return {"status": "ok", "message": f"Sheet '{actual_name}' is already {state}."}

        try:
            if should_protect:
                sheet.protect("")
            else:
                sheet.unprotect("")
        except Exception as e:
            if is_disposal(e):
                raise
            if should_protect:
                return self._tool_error(f"Could not protect sheet '{actual_name}': {e}")
            return self._tool_error(f"Sheet '{actual_name}' is password-protected; unprotect it in LibreOffice.")

        if sheet.isProtected() == is_currently_protected:
            return self._tool_error(f"Failed to set sheet '{actual_name}' to {state}.")

        msg = f"Sheet '{actual_name}' is now {state}."

        log.info("%s", msg)
        return {"status": "ok", "message": msg}


class SheetCopy(ToolCalcSheetBase):
    """Copy a sheet to a new tab."""

    name: str | None = "sheet_copy"
    description: str = "Copies an existing sheet to a new tab (cells included; does not activate the copy)."
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "source": {"type": "string", "description": "Name of the sheet to copy."},
            "new_name": {"type": "string", "description": "Name for the new sheet tab."},
            "position": {"type": "integer", "description": "0-based index for the copy (appended if omitted)."},
        },
        "required": ["source", "new_name"],
    }
    is_mutation: bool | None = True

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        source = kwargs["source"]
        new_name = kwargs["new_name"]
        position = kwargs.get("position")

        doc = ctx.doc
        _user_sheet(doc, source, "copy")
        err = _sheet_name_error(doc, new_name)
        if err:
            return self._tool_error(err, code="INVALID_PARAM")

        sheets = doc.getSheets()
        position = _insert_position(sheets, position)

        sheets.copyByName(source, new_name, position)
        log.info("Sheet copied from '%s' to '%s' at position %d.", source, new_name, position)
        return {"status": "ok", "message": f"Sheet '{source}' copied to '{new_name}'."}


class SheetMove(ToolCalcSheetBase):
    """Move a sheet to a new position."""

    name: str | None = "sheet_move"
    description: str = "Moves a sheet to a new 0-based index in the workbook."
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "sheet": {"type": "string", "description": "Name of the sheet to move."},
            "position": {"type": "integer", "description": "New 0-based index (0..sheet count minus 1)."},
        },
        "required": ["sheet", "position"],
    }
    is_mutation: bool | None = True

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        sheet_name = kwargs["sheet"]
        position = kwargs["position"]

        doc = ctx.doc
        _user_sheet(doc, sheet_name, "move")

        sheets = doc.getSheets()
        count = sheets.getCount()
        # prepare_tool_call has already checked that position is an integer; only the bound depends on the workbook.
        if position < 0 or position >= count:
            return self._tool_error(f"Position {position} is out of range (0..{count - 1}).", code="INVALID_PARAM")

        sheets.moveByName(sheet_name, position)
        log.info("Sheet '%s' moved to position %d.", sheet_name, position)
        return {"status": "ok", "message": f"Sheet '{sheet_name}' moved to position {position}."}


class SheetHide(ToolCalcSheetBase):
    """Hide or show a sheet tab."""

    name: str | None = "sheet_hide"
    description: str = "Hides or shows a sheet tab (visible=false hides; default hides)."
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "sheet": {"type": "string", "description": "Name of the sheet."},
            "visible": {"type": "boolean", "description": "True to show the tab; false to hide (default: false)."},
        },
        "required": ["sheet"],
    }
    is_mutation: bool | None = True

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        sheet_name = kwargs["sheet"]
        visible = kwargs.get("visible", False)

        doc = ctx.doc
        sheet = _user_sheet(doc, sheet_name, "hide")

        if not visible:
            sheets = doc.getSheets()
            visible_count = sum(
                1
                for name in sheets.getElementNames()
                if sheets.getByName(name).getPropertyValue("IsVisible")
            )
            if sheet.getPropertyValue("IsVisible") and visible_count <= 1:
                return self._tool_error("Cannot hide the only visible sheet in the document.", code="INVALID_PARAM")

        sheet.setPropertyValue("IsVisible", visible)
        state = "visible" if visible else "hidden"
        log.info("Sheet '%s' is now %s.", sheet_name, state)
        return {"status": "ok", "message": f"Sheet '{sheet_name}' is now {state}."}


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
            return self._tool_error(f"No user-visible sheet named '{sheet_name}'.", code="INVALID_PARAM")

        result = analyzer.get_sheet_summary(sheet_name=sheet_name)
        if not is_agent_visible_sheet(result.get("sheet_name", "")):
            return self._tool_error("Active sheet is a LibreOffice internal tab; specify a user-visible sheet.")
        return {"status": "ok", "result": result}
