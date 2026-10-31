# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
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
"""Calc named range management tools (domain-first naming: named_range_*)."""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any

from plugin.calc.address_utils import MAX_COL, column_to_index, format_address, format_range_address, parse_address, split_sheet_prefix
from plugin.calc.base import ToolCalcRangeBase
from plugin.calc.bridge import CalcBridge, NamedRangeHit, find_named_range, is_agent_visible_sheet
from plugin.framework.errors import UnoObjectError, uno_read

from com.sun.star.lang import IllegalArgumentException

if TYPE_CHECKING:
    from plugin.framework.tool import ToolContext

log = logging.getLogger("writeragent.calc")

# NamedRangeFlag bitmask mappings (com.sun.star.sheet.NamedRangeFlag)
_FLAG_NAME_TO_BIT: dict[str, int] = {"filter_criteria": 1, "print_area": 2, "column_header": 4, "row_header": 8}
# Disjoint NamedRangeFlag bits. sum equals the OR-mask (15).
_FLAG_MASK = sum(_FLAG_NAME_TO_BIT.values())

# Gemini rejects oneOf; advertise the LLM-friendly form. _parse_flags still
# accepts a comma-string or integer bitmask if sent.
_FLAGS_SCHEMA: dict[str, Any] = {"description": ("Range type flags as an array of names: 'filter_criteria', 'print_area', 'column_header', 'row_header'."), "type": "array", "items": {"type": "string", "enum": list(_FLAG_NAME_TO_BIT.keys())}}


def _parse_flags(flags: list[str] | str | int | None) -> int:
    """Parse human-readable flags or integer bitmask into a NamedRangeFlag bitmask.

    Raises ValueError on unknown flag names, invalid types, or out-of-mask values.
    """
    if flags is None:
        return 0

    if isinstance(flags, int):
        mask = flags
    else:
        if isinstance(flags, str):
            tokens = flags.replace(",", " ").split()
        elif isinstance(flags, (list, tuple)):
            tokens = [str(raw) for raw in flags]
        else:
            raise ValueError(f"Invalid flags type: expected int, str, list, or tuple, got {type(flags).__name__}.")

        mask = 0
        for raw in tokens:
            item = raw.strip().lower()
            if not item:
                continue
            if item in _FLAG_NAME_TO_BIT:
                mask |= _FLAG_NAME_TO_BIT[item]
            elif item.isdigit():
                mask |= int(item)
            else:
                raise ValueError(f"Unknown flag '{item}'. Valid flags: {', '.join(_FLAG_NAME_TO_BIT)}.")

    if mask < 0 or mask & ~_FLAG_MASK:
        raise ValueError(f"Invalid flags mask {mask}: only bits 1, 2, 4, and 8 are allowed.")
    return mask


def _format_flags(flag_mask: int) -> list[str]:
    """Format an integer NamedRangeFlag bitmask into human-readable strings."""
    return [name for name, bit in _FLAG_NAME_TO_BIT.items() if flag_mask & bit]


# Messages from ScNamedRangesObj::addNewByName (sc/source/ui/unoobj/nameuno.cxx).
# addNewByName calls ScRangeData::IsNameValid (sc/source/core/tool/rangenam.cxx).
# XNamedRange.setName does not, so a rename used to install A1, Sales.2026, 1abc, or names with spaces.
_NAME_BAD_STRING = "Invalid name. It must start with a letter (excluding c, C, r, or R followed by a number) or underscore. Only letters, numbers, and underscores are permitted."
_NAME_CELL_REF = "Invalid name. Reference to a cell, or a range of cells not allowed."

# Column ceiling is Calc's jumbo XFD (index 16383). IsNameValid treats any
# ScAddress::Parse result other than ScRefFlags::ZERO as a cell, including a
# partial row, so a positive row with a column inside that ceiling is rejected.
_A1_CELL_NAME = re.compile(r"^([A-Za-z]{1,3})([0-9]+)$")
# R1C1 forms that use only name characters (brackets are not Name chars).
# RC is the current cell. Bare R or C is not a reference.
_R1C1_CELL_NAME = re.compile(r"^(?:R[0-9]*C[0-9]*|R[0-9]+|C[0-9]+)$", re.IGNORECASE)


def _defined_name_char_ok(ch: str, *, start: bool) -> bool:
    """Match ScCompiler::IsCharFlagAllConventions for CharName (start) or Name.

    CharName is a letter or '_'; Name also allows digits and '?' (Excel).
    '.' is Name too, but IsNameValid rejects it first. '_' and '?' are ASCII,
    so one expression covers non-ASCII too (CharClass::isLetterNumeric):
    letters may start a name, digits may not.
    """
    if start:
        return ch.isalpha() or ch == "_"
    return ch.isalnum() or ch in "_?"


def _is_calc_cell_ref_name(name: str) -> bool:
    """True when *name* is an A1 or R1C1 address under IsNameValid's parse check."""
    if _R1C1_CELL_NAME.match(name):
        return True
    match = _A1_CELL_NAME.match(name)
    if not match:
        return False
    row = int(match.group(2))
    # Row 0 does not parse. Any positive row counts: a partial parse is not ScRefFlags::ZERO.
    if row < 1:
        return False
    return column_to_index(match.group(1)) <= MAX_COL


def _defined_name_error(name: str) -> str | None:
    """Return Calc's invalid-name message, or None when IsNameValid would accept *name*."""
    if "." in name or not name or not _defined_name_char_ok(name[0], start=True) or any(not _defined_name_char_ok(ch, start=False) for ch in name[1:]):
        return _NAME_BAD_STRING
    if _is_calc_cell_ref_name(name):
        return _NAME_CELL_REF
    return None


def _resolve_container(doc: Any, scope: str | None) -> tuple[Any, str, Any | None]:
    """Resolve the NamedRanges container and effective scope name.

    Returns:
        (container, effective_scope_str, sheet_obj_or_None)
    """
    clean = (scope or "").strip()
    if not clean or clean.lower() == "global":
        return doc.NamedRanges, "global", None

    sheets = doc.getSheets()
    if sheets.hasByName(clean):
        sheet = sheets.getByName(clean)
        return sheet.NamedRanges, sheet.getName(), sheet
    raise UnoObjectError(f"No sheet found with name '{clean}' for scoped named ranges.")


def _resolve_named_range_target(
    tool: ToolCalcRangeBase,
    doc: Any,
    name: str,
    scope: str | None,
) -> NamedRangeHit | dict[str, Any]:
    """Return the resolved named range or a tool error dict.

    An explicit scope (including ``global``) uses only that container.
    An omitted scope uses :func:`find_named_range`.
    """
    scope_text = "" if scope is None else str(scope).strip()
    if scope_text:
        container, effective_scope, sheet_obj = _resolve_container(doc, scope_text)
        if not container.hasByName(name):
            return tool._tool_error(f"No named range found with name '{name}' in scope '{effective_scope}'.", code="NAMED_RANGE_NOT_FOUND")
        return NamedRangeHit(container, effective_scope, sheet_obj, container.getByName(name))

    found = find_named_range(doc, name)
    if found is None:
        return tool._tool_error(f"No named range found with name '{name}'.", code="NAMED_RANGE_NOT_FOUND")
    return found


def _parse_base_address(doc: Any, base_cell: str | None, sheet_obj: Any = None) -> Any:
    """Create a com.sun.star.table.CellAddress for the base reference.

    *sheet_obj* is the scope sheet. None means global, sheet 0. A prefix on
    *base_cell* overrides it. An unknown prefix is an error: sheet 0 would
    anchor a relative name on the wrong sheet.
    """
    if sheet_obj is None:
        sheet_idx = 0
    else:
        sheet_idx = sheet_obj.getRangeAddress().Sheet

    col_idx = 0
    row_idx = 0
    if base_cell and base_cell.strip():
        prefix, address = split_sheet_prefix(base_cell.strip())
        if prefix:
            sheets = doc.getSheets()
            if not sheets.hasByName(prefix):
                raise ValueError("No sheet named '%s'." % prefix)
            sheet_idx = sheets.getByName(prefix).getRangeAddress().Sheet
        col_idx, row_idx = parse_address(address)

    from com.sun.star.table import CellAddress

    return CellAddress(Sheet=sheet_idx, Column=col_idx, Row=row_idx)


def _extract_range_info(nr: Any, scope: str, doc: Any) -> dict[str, Any]:
    """Extract structured metadata dictionary from an XNamedRange object."""
    type_code = int(nr.getType())
    pos = nr.getReferencePosition()
    info: dict[str, Any] = {
        "name": nr.getName(),
        "scope": scope,
        "content": nr.getContent(),
        "type_code": type_code,
        "flags": _format_flags(type_code),
        "base_position": {
            "sheet_index": pos.Sheet,
            "column": pos.Column,
            "row": pos.Row,
            "address": format_address(pos.Column, pos.Row),
        },
    }

    # getReferredCells is null for a formula name. A sheet range has getRangeAddress.
    cells = uno_read(nr.getReferredCells, None)
    if cells is not None and hasattr(cells, "getRangeAddress"):
        addr = cells.getRangeAddress()
        range_str = format_range_address(addr, collapse_single=True)
        info["referred_range"] = {
            "sheet": doc.getSheets().getByIndex(addr.Sheet).getName(),
            "address": range_str,
            "start_column": addr.StartColumn,
            "start_row": addr.StartRow,
            "end_column": addr.EndColumn,
            "end_row": addr.EndRow,
            "rows": addr.EndRow - addr.StartRow + 1,
            "columns": addr.EndColumn - addr.StartColumn + 1,
        }
    return info


class NamedRangeList(ToolCalcRangeBase):
    """List all named ranges in the workbook or for a specific sheet scope."""

    name: str | None = "named_range_list"
    intent: str | None = "navigate"
    description: str = "Lists named ranges and their formulas/reference targets. Supports filtering by scope ('global', 'all', or a specific sheet name)."
    parameters: dict[str, Any] | None = {"type": "object", "properties": {"scope": {"type": "string", "description": "Scope to list: 'global' (default), 'all' (global + visible sheets; skips _-prefixed generated sheets), or a specific sheet name."}}}
    is_mutation: bool | None = False

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        scope_arg = kwargs.get("scope", "global")
        scope_clean = (str(scope_arg).strip() if scope_arg is not None else "") or "global"

        doc = ctx.doc
        result: list[dict[str, Any]] = []
        entries: list[tuple[Any, str]] = []

        if scope_clean.lower() == "all":
            entries.append((doc.NamedRanges, "global"))
            sheets = doc.getSheets()
            for idx in range(sheets.getCount()):
                sheet = sheets.getByIndex(idx)
                s_name = sheet.getName()
                if not is_agent_visible_sheet(s_name):
                    continue
                entries.append((sheet.NamedRanges, s_name))
        else:
            container, effective_scope, _sheet = _resolve_container(doc, scope_clean)
            entries.append((container, effective_scope))

        for container, effective_scope in entries:
            for name in container.getElementNames():
                nr = container.getByName(name)
                result.append(_extract_range_info(nr, effective_scope, doc))

        log.info("Named ranges listed (scope=%s): count=%d", scope_clean, len(result))
        return {"status": "ok", "result": result}


class NamedRangeGetInfo(ToolCalcRangeBase):
    """Get detailed metadata for a specific named range."""

    name: str | None = "named_range_get_info"
    intent: str | None = "navigate"
    description: str = "Retrieves detailed metadata, reference coordinates, flags, and base address for a specific named range."
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {"name": {"type": "string", "description": "The name of the defined range to inspect."}, "scope": {"type": "string", "description": "Omit to prefer the active sheet's local name over a same-spelled global name, then the workbook, then other visible sheets. 'global' or a sheet name forces that container, including a _-prefixed sheet."}},
        "required": ["name"],
    }
    is_mutation: bool | None = False

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        name = kwargs["name"].strip()
        doc = ctx.doc
        resolved = _resolve_named_range_target(self, doc, name, kwargs.get("scope"))
        if isinstance(resolved, dict):
            return resolved
        _container, effective_scope, _sheet, nr = resolved
        return {"status": "ok", "result": _extract_range_info(nr, effective_scope, doc)}


class NamedRangeAdd(ToolCalcRangeBase):
    """Add a new named range to the workbook or specific sheet."""

    name: str | None = "named_range_add"
    intent: str | None = "edit"
    description: str = "Defines a new named range or formula expression in the workbook (global) or specific sheet. Can specify base reference cell and type flags (e.g. 'print_area', 'filter_criteria')."
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Name of the range (e.g. 'TaxRate', 'Q1Sales'). Must start with a letter/underscore with no spaces."},
            "content": {"type": "string", "description": "The formula or cell range address it points to (e.g. '$Sheet1.$A$1:$B$5', '0.0825', 'SUM(A1:A10)')."},
            "scope": {"type": "string", "description": "Scope of the name: 'global' (default) or a specific sheet name (e.g. 'Sheet1')."},
            "base_cell": {"type": "string", "description": "Base cell reference for relative addresses (e.g. 'A1' or 'Sheet1.A1'). Defaults to A1 on sheet 0. An unknown sheet name is an error."},
            "flags": _FLAGS_SCHEMA,
        },
        "required": ["name", "content"],
    }
    is_mutation: bool | None = True

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        name = kwargs["name"].strip()
        content = kwargs["content"].strip()
        scope = kwargs.get("scope")
        base_cell = kwargs.get("base_cell")
        flags_arg = kwargs.get("flags")

        try:
            invalid = _defined_name_error(name)
            if invalid:
                return self._tool_error(invalid, code="INVALID_NAME")
            try:
                type_mask = _parse_flags(flags_arg)
            except ValueError as e:
                return self._tool_error(str(e), code="INVALID_FLAGS")

            doc = ctx.doc
            container, effective_scope, sheet_obj = _resolve_container(doc, scope)
            if container.hasByName(name):
                return self._tool_error(f"A named range with the name '{name}' already exists in scope '{effective_scope}'.", code="NAMED_RANGE_EXISTS")

            try:
                pos = _parse_base_address(doc, base_cell, sheet_obj)
            except ValueError as ve:
                return self._tool_error(f"Invalid base_cell address '{base_cell}': {ve}", code="INVALID_BASE_CELL")

            container.addNewByName(name, content, pos, type_mask)
            log.info("Named range added: [%s] %s -> %s (flags=%d)", effective_scope, name, content, type_mask)
            return {"status": "ok", "message": f"Named range '{name}' added successfully in scope '{effective_scope}' pointing to '{content}'."}
        except IllegalArgumentException as e:
            log.exception("Add named range failed for %s", name)
            return self._tool_error(f"Failed to add named range: {str(e)}", code="NAMED_RANGE_ERROR")


class NamedRangeEdit(ToolCalcRangeBase):
    """Edit an existing named range: rename, change content, change base position, or change flags."""

    name: str | None = "named_range_edit"
    intent: str | None = "edit"
    description: str = "Modifies an existing named range: rename, update formula/range content, change base reference position, or update flags."
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Current name of the range to edit."},
            "new_name": {"type": "string", "description": "New name if renaming. Must start with a letter or underscore; only letters, digits, and underscore; not a cell address (A1, R1C1) and not a name containing '.' or spaces."},
            "content": {"type": "string", "description": "New formula or range address content."},
            "scope": {"type": "string", "description": "Omit to resolve like named_range_get_info (active sheet shadows a same-spelled global name). 'global' or a sheet name forces that container."},
            "base_cell": {"type": "string", "description": "New base cell reference for relative coordinates (e.g. 'A1' or 'Sheet1.A1'). An unknown sheet name is an error."},
            "flags": _FLAGS_SCHEMA,
        },
        "required": ["name"],
    }
    is_mutation: bool | None = True

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        name = kwargs["name"].strip()
        new_name = kwargs.get("new_name")
        content = kwargs.get("content")
        scope = kwargs.get("scope")
        base_cell = kwargs.get("base_cell")
        flags_arg = kwargs.get("flags")

        try:
            doc = ctx.doc
            resolved = _resolve_named_range_target(self, doc, name, scope)
            if isinstance(resolved, dict):
                return resolved
            container, effective_scope, sheet_obj, nr = resolved

            old_name = nr.getName()
            new_clean = new_name.strip() if new_name is not None else ""
            if new_clean == old_name:
                new_clean = ""
            if new_clean:
                # setName does not run IsNameValid, so reject A1, Sales.2026,
                # 1abc, and names with spaces using the same messages as
                # addNewByName. Do not touch the range until the new name and
                # base cell are valid. Rename first and roll it back if a
                # later setter fails. Setting content, type, and reference
                # before setName leaves a partial edit when the rename fails.
                invalid = _defined_name_error(new_clean)
                if invalid:
                    return self._tool_error(invalid, code="INVALID_NAME")
                if container.hasByName(new_clean):
                    return self._tool_error(f"Cannot rename to '{new_clean}': a named range with that name already exists in scope '{effective_scope}'.", code="NAMED_RANGE_EXISTS")

            try:
                type_mask = _parse_flags(flags_arg) if flags_arg is not None else None
            except ValueError as e:
                return self._tool_error(str(e), code="INVALID_FLAGS")

            pos = None
            if base_cell is not None:
                try:
                    pos = _parse_base_address(doc, base_cell, sheet_obj)
                except ValueError as ve:
                    return self._tool_error(f"Invalid base_cell address '{base_cell}': {ve}", code="INVALID_BASE_CELL")

            pending: list[tuple[str, Any, Any, Any]] = []
            if content is not None:
                pending.append(("content", nr.setContent, nr.getContent(), content))
            if type_mask is not None:
                pending.append(("type", nr.setType, nr.getType(), type_mask))
            if pos is not None:
                pending.append(("position", nr.setReferencePosition, nr.getReferencePosition(), pos))

            if new_clean:
                nr.setName(new_clean)

            done: list[tuple[str, Any, Any]] = []
            try:
                for field, setter, old, new in pending:
                    setter(new.strip() if field == "content" else new)
                    done.append((field, setter, old))
            except Exception:
                for field, setter, old in reversed(done):
                    try:
                        setter(old)
                    except Exception:
                        log.exception("Rollback of named range %s field %s failed", old_name, field)
                if new_clean:
                    try:
                        nr.setName(old_name)
                    except Exception:
                        log.exception("Rollback of named range rename %s -> %s failed", old_name, new_clean)
                raise

            log.info("Named range edited: [%s] %s (new_name=%s)", effective_scope, name, new_name)
            return {"status": "ok", "message": f"Named range '{new_clean or old_name}' updated successfully in scope '{effective_scope}'."}
        except IllegalArgumentException as e:
            log.exception("Edit named range failed for %s", name)
            return self._tool_error(f"Failed to edit named range: {str(e)}", code="NAMED_RANGE_ERROR")


class NamedRangeDelete(ToolCalcRangeBase):
    """Delete an existing named range from the workbook or specific sheet."""

    name: str | None = "named_range_delete"
    intent: str | None = "edit"
    description: str = "Deletes an existing named range from global or sheet-specific scope."
    parameters: dict[str, Any] | None = {"type": "object", "properties": {"name": {"type": "string", "description": "Name of the range to delete."}, "scope": {"type": "string", "description": "Omit to resolve like named_range_get_info (active sheet shadows a same-spelled global name). 'global' or a sheet name forces that container."}}, "required": ["name"]}
    is_mutation: bool | None = True

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        name = kwargs["name"].strip()
        scope = kwargs.get("scope")

        try:
            doc = ctx.doc
            resolved = _resolve_named_range_target(self, doc, name, scope)
            if isinstance(resolved, dict):
                return resolved
            container, effective_scope, _sheet, nr = resolved

            container.removeByName(nr.getName())
            log.info("Named range deleted: [%s] %s", effective_scope, name)
            return {"status": "ok", "message": f"Named range '{name}' deleted successfully from scope '{effective_scope}'."}
        except IllegalArgumentException as e:
            log.exception("Delete named range failed for %s", name)
            return self._tool_error(f"Failed to delete named range: {str(e)}", code="NAMED_RANGE_ERROR")


class NamedRangeCreateFromTitles(ToolCalcRangeBase):
    """Batch-create named ranges from header row/column titles."""

    name: str | None = "named_range_create_from_titles"
    intent: str | None = "edit"
    description: str = "Automatically creates multiple named ranges based on the content of title cells (headers) in a table range. Border specifies where headers are located ('top', 'bottom', 'left', 'right')."
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "range": {"type": "array", "items": {"type": "string"}, "description": 'The table cell range containing both headers and data (e.g. ["A1:D20"] or ["Sheet1.A1:D20"]).'},
            "border": {"type": "string", "description": "Which edge contains the title labels: 'top' (default), 'bottom', 'left', or 'right'.", "enum": ["top", "bottom", "left", "right"]},
            "scope": {"type": "string", "description": "Container scope for creating the names: 'global' (default) or specific sheet name."},
        },
        "required": ["range"],
    }
    is_mutation: bool | None = True

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        bridge = CalcBridge(ctx.doc)
        range_arr = kwargs["range"]
        if not range_arr:
            return self._tool_error("range is required", code="INVALID_ARGUMENT")
        range_str = range_arr[0].strip()
        border_present = kwargs.get("border", "top")
        border_str = "top" if border_present is None or not str(border_present).strip() else border_present
        border_clean = str(border_str).strip().upper()
        scope = kwargs.get("scope")

        from com.sun.star.sheet.Border import BOTTOM, LEFT, RIGHT, TOP

        border_map: dict[str, Any] = {"TOP": TOP, "BOTTOM": BOTTOM, "LEFT": LEFT, "RIGHT": RIGHT}

        if border_clean not in border_map:
            return self._tool_error(f"Invalid border '{border_str}'. Supported borders: 'top', 'bottom', 'left', 'right'.", code="INVALID_BORDER")

        try:
            sheet, address = bridge.resolve(range_str)
            cell_range = bridge.get_cell_range(sheet, address)
            range_addr = cell_range.getRangeAddress()

            container, effective_scope, _ = _resolve_container(ctx.doc, scope)
            container.addNewFromTitles(range_addr, border_map[border_clean])

            log.info("Named ranges created from titles: [%s] range=%s border=%s", effective_scope, range_str, border_clean)
            return {"status": "ok", "message": f"Named ranges created from titles along border '{border_str}' for range '{range_str}' in scope '{effective_scope}'."}
        except IllegalArgumentException as e:
            log.exception("Create named ranges from titles failed for %s", range_str)
            return self._tool_error(f"Failed to create named ranges from titles: {str(e)}", code="NAMED_RANGE_ERROR")
