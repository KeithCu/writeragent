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
from typing import Any

from plugin.calc.address_utils import (
    index_to_column,
    parse_address,
    split_sheet_prefix,
)
from plugin.calc.base import ToolCalcRangeBase
from plugin.calc.bridge import CalcBridge
from plugin.framework.errors import UnoObjectError, suppress_disposed

log = logging.getLogger("writeragent.calc")

# NamedRangeFlag bitmask mappings (com.sun.star.sheet.NamedRangeFlag)
_FLAG_NAME_TO_BIT: dict[str, int] = {
    "filter_criteria": 1,
    "print_area": 2,
    "column_header": 4,
    "row_header": 8,
}

_BIT_TO_FLAG_NAME: dict[int, str] = {v: k for k, v in _FLAG_NAME_TO_BIT.items()}

# Gemini rejects oneOf; advertise the LLM-friendly form. _parse_flags still
# accepts a comma-string or integer bitmask if sent.
_FLAGS_SCHEMA: dict[str, Any] = {
    "description": (
        "Range type flags as an array of names: 'filter_criteria', 'print_area', "
        "'column_header', 'row_header'."
    ),
    "type": "array",
    "items": {
        "type": "string",
        "enum": list(_FLAG_NAME_TO_BIT.keys()),
    },
}


def _parse_flags(flags: list[str] | str | int | None) -> int:
    """Parse human-readable flags or integer bitmask into a NamedRangeFlag bitmask."""
    if flags is None:
        return 0
    if isinstance(flags, int):
        return flags
    if isinstance(flags, str):
        flag_list = [f.strip().lower() for f in flags.replace(",", " ").split() if f.strip()]
    elif isinstance(flags, (list, tuple)):
        flag_list = [str(f).strip().lower() for f in flags if str(f).strip()]
    else:
        return 0

    mask = 0
    for item in flag_list:
        if item in _FLAG_NAME_TO_BIT:
            mask |= _FLAG_NAME_TO_BIT[item]
        elif item.isdigit():
            mask |= int(item)
    return mask


def _format_flags(flag_mask: int) -> list[str]:
    """Format an integer NamedRangeFlag bitmask into human-readable strings."""
    result: list[str] = []
    for bit, name in _BIT_TO_FLAG_NAME.items():
        if flag_mask & bit:
            result.append(name)
    return result


def _resolve_container(doc: Any, scope: str | None) -> tuple[Any, str, Any | None]:
    """Resolve the NamedRanges container and effective scope name.

    Returns:
        (container, effective_scope_str, sheet_obj_or_None)
    """
    if scope is None or scope.strip() == "" or scope.strip().lower() == "global":
        if not hasattr(doc, "NamedRanges"):
            raise UnoObjectError("Document does not expose NamedRanges container.")
        return doc.NamedRanges, "global", None

    # Sheet-specific container
    clean_scope = scope.strip()
    if hasattr(doc, "getSheets"):
        sheets = doc.getSheets()
        if sheets.hasByName(clean_scope):
            sheet = sheets.getByName(clean_scope)
            if hasattr(sheet, "NamedRanges"):
                return sheet.NamedRanges, sheet.getName(), sheet
    raise UnoObjectError(f"No sheet found with name '{clean_scope}' for scoped named ranges.")


def _parse_base_address(doc: Any, base_cell: str | None, default_sheet_idx: int = 0) -> Any:
    """Create a com.sun.star.table.CellAddress structure for the base reference."""
    sheet_idx = default_sheet_idx
    col_idx = 0
    row_idx = 0

    if base_cell and base_cell.strip():
        prefix, address = split_sheet_prefix(base_cell.strip())
        if prefix and hasattr(doc, "getSheets"):
            sheets = doc.getSheets()
            for idx in range(sheets.getCount()):
                if sheets.getByIndex(idx).getName() == prefix:
                    sheet_idx = idx
                    break
        try:
            col_idx, row_idx = parse_address(address)
        except Exception:
            col_idx, row_idx = 0, 0

    try:
        from com.sun.star.table import CellAddress

        return CellAddress(Sheet=sheet_idx, Column=col_idx, Row=row_idx)
    except Exception:
        from types import SimpleNamespace

        return SimpleNamespace(Sheet=sheet_idx, Column=col_idx, Row=row_idx)


def _extract_range_info(nr: Any, scope: str, doc: Any = None) -> dict[str, Any]:
    """Extract structured metadata dictionary from an XNamedRange object."""
    name = nr.getName() if hasattr(nr, "getName") else str(nr)
    content = nr.getContent() if hasattr(nr, "getContent") else ""
    type_code = int(nr.getType()) if hasattr(nr, "getType") else 0
    flags = _format_flags(type_code)

    base_info: dict[str, Any] = {"sheet_index": 0, "column": 0, "row": 0, "address": "A1"}
    if hasattr(nr, "getReferencePosition"):
        try:
            pos = nr.getReferencePosition()
            s_idx = getattr(pos, "Sheet", 0)
            c_idx = getattr(pos, "Column", 0)
            r_idx = getattr(pos, "Row", 0)
            base_info = {
                "sheet_index": s_idx,
                "column": c_idx,
                "row": r_idx,
                "address": f"{index_to_column(c_idx)}{r_idx + 1}",
            }
        except Exception:
            pass

    info: dict[str, Any] = {
        "name": name,
        "scope": scope,
        "content": content,
        "type_code": type_code,
        "flags": flags,
        "base_position": base_info,
    }

    # If this named range refers to concrete cells, resolve coordinates
    if hasattr(nr, "getReferredCells"):
        try:
            cells = nr.getReferredCells()
            if cells is not None and hasattr(cells, "getRangeAddress"):
                addr = cells.getRangeAddress()
                sheet_name = ""
                if doc is not None and hasattr(doc, "getSheets"):
                    try:
                        sheet_name = doc.getSheets().getByIndex(addr.Sheet).getName()
                    except Exception:
                        pass
                start_str = f"{index_to_column(addr.StartColumn)}{addr.StartRow + 1}"
                end_str = f"{index_to_column(addr.EndColumn)}{addr.EndRow + 1}"
                range_str = f"{start_str}:{end_str}" if (addr.StartColumn != addr.EndColumn or addr.StartRow != addr.EndRow) else start_str
                info["referred_range"] = {
                    "sheet": sheet_name or f"Sheet{addr.Sheet + 1}",
                    "address": range_str,
                    "start_column": addr.StartColumn,
                    "start_row": addr.StartRow,
                    "end_column": addr.EndColumn,
                    "end_row": addr.EndRow,
                    "rows": addr.EndRow - addr.StartRow + 1,
                    "columns": addr.EndColumn - addr.StartColumn + 1,
                }
        except Exception:
            pass

    return info


class NamedRangeList(ToolCalcRangeBase):
    """List all named ranges in the workbook or for a specific sheet scope."""

    name = "named_range_list"
    intent = "navigate"
    description = (
        "Lists named ranges and their formulas/reference targets. "
        "Supports filtering by scope ('global', 'all', or a specific sheet name)."
    )
    parameters = {
        "type": "object",
        "properties": {
            "scope": {
                "type": "string",
                "description": "Scope to list: 'global' (default), 'all' (global + all sheets), or specific sheet name.",
            }
        },
    }
    is_mutation = False

    def execute(self, ctx, **kwargs):
        bridge = CalcBridge(ctx.doc)
        scope_arg = kwargs.get("scope", "global")
        if scope_arg is None or str(scope_arg).strip() == "":
            scope_arg = "global"
        scope_clean = str(scope_arg).strip()

        try:
            doc = bridge.get_active_document()
            result: list[dict[str, Any]] = []

            if scope_clean.lower() == "all":
                # Global
                if hasattr(doc, "NamedRanges"):
                    for name in doc.NamedRanges.getElementNames():
                        nr = doc.NamedRanges.getByName(name)
                        result.append(_extract_range_info(nr, "global", doc))
                # Sheets
                if hasattr(doc, "getSheets"):
                    sheets = doc.getSheets()
                    for idx in range(sheets.getCount()):
                        sheet = sheets.getByIndex(idx)
                        if hasattr(sheet, "NamedRanges"):
                            s_name = sheet.getName()
                            for name in sheet.NamedRanges.getElementNames():
                                nr = sheet.NamedRanges.getByName(name)
                                result.append(_extract_range_info(nr, s_name, doc))
            else:
                container, effective_scope, _ = _resolve_container(doc, scope_clean)
                for name in container.getElementNames():
                    nr = container.getByName(name)
                    result.append(_extract_range_info(nr, effective_scope, doc))

            log.info("Named ranges listed (scope=%s): count=%d", scope_clean, len(result))
            return {"status": "ok", "result": result}
        except Exception as e:
            log.exception("List named ranges failed")
            return self._tool_error(f"Failed to list named ranges: {str(e)}", code="NAMED_RANGE_ERROR")


class NamedRangeGetInfo(ToolCalcRangeBase):
    """Get detailed metadata for a specific named range."""

    name = "named_range_get_info"
    intent = "navigate"
    description = "Retrieves detailed metadata, reference coordinates, flags, and base address for a specific named range."
    parameters = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "The name of the defined range to inspect."},
            "scope": {
                "type": "string",
                "description": "Scope where the name is defined: 'global' (default) or sheet name. Omit to search global then active sheet.",
            },
        },
        "required": ["name"],
    }
    is_mutation = False

    def execute(self, ctx, **kwargs):
        bridge = CalcBridge(ctx.doc)
        name = kwargs["name"].strip()
        scope_arg = kwargs.get("scope")

        try:
            doc = bridge.get_active_document()

            if scope_arg is not None and str(scope_arg).strip():
                container, effective_scope, _ = _resolve_container(doc, str(scope_arg).strip())
                if not container.hasByName(name):
                    return self._tool_error(f"No named range found with name '{name}' in scope '{effective_scope}'.", code="NAMED_RANGE_NOT_FOUND")
                nr = container.getByName(name)
                info = _extract_range_info(nr, effective_scope, doc)
                return {"status": "ok", "result": info}

            # Search global first
            if hasattr(doc, "NamedRanges") and doc.NamedRanges.hasByName(name):
                nr = doc.NamedRanges.getByName(name)
                return {"status": "ok", "result": _extract_range_info(nr, "global", doc)}

            # Search active sheet
            with suppress_disposed("search active sheet named ranges", logger=log):
                active_sheet = bridge.get_active_sheet()
                if hasattr(active_sheet, "NamedRanges") and active_sheet.NamedRanges.hasByName(name):
                    nr = active_sheet.NamedRanges.getByName(name)
                    return {"status": "ok", "result": _extract_range_info(nr, active_sheet.getName(), doc)}

            # Search all sheets
            if hasattr(doc, "getSheets"):
                sheets = doc.getSheets()
                for idx in range(sheets.getCount()):
                    sheet = sheets.getByIndex(idx)
                    if hasattr(sheet, "NamedRanges") and sheet.NamedRanges.hasByName(name):
                        nr = sheet.NamedRanges.getByName(name)
                        return {"status": "ok", "result": _extract_range_info(nr, sheet.getName(), doc)}

            return self._tool_error(f"No named range found with name '{name}'.", code="NAMED_RANGE_NOT_FOUND")
        except Exception as e:
            log.exception("Get named range info failed for %s", name)
            return self._tool_error(f"Failed to get named range info: {str(e)}", code="NAMED_RANGE_ERROR")


class NamedRangeAdd(ToolCalcRangeBase):
    """Add a new named range to the workbook or specific sheet."""

    name = "named_range_add"
    intent = "edit"
    description = (
        "Defines a new named range or formula expression in the workbook (global) or specific sheet. "
        "Can specify base reference cell and type flags (e.g. 'print_area', 'filter_criteria')."
    )
    parameters = {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Name of the range (e.g. 'TaxRate', 'Q1Sales'). Must start with a letter/underscore with no spaces.",
            },
            "content": {
                "type": "string",
                "description": "The formula or cell range address it points to (e.g. '$Sheet1.$A$1:$B$5', '0.0825', 'SUM(A1:A10)').",
            },
            "scope": {
                "type": "string",
                "description": "Scope of the name: 'global' (default) or a specific sheet name (e.g. 'Sheet1').",
            },
            "base_cell": {
                "type": "string",
                "description": "Base cell reference for relative addresses (e.g. 'A1' or 'Sheet1.A1'). Defaults to A1 on sheet 0.",
            },
            "flags": _FLAGS_SCHEMA,
        },
        "required": ["name", "content"],
    }
    is_mutation = True

    def execute(self, ctx, **kwargs):
        bridge = CalcBridge(ctx.doc)
        name = kwargs["name"].strip()
        content = kwargs["content"].strip()
        scope = kwargs.get("scope")
        base_cell = kwargs.get("base_cell")
        flags_arg = kwargs.get("flags")

        try:
            doc = bridge.get_active_document()
            container, effective_scope, sheet_obj = _resolve_container(doc, scope)
            if container.hasByName(name):
                return self._tool_error(f"A named range with the name '{name}' already exists in scope '{effective_scope}'.", code="NAMED_RANGE_EXISTS")

            sheet_idx = 0
            if sheet_obj is not None and hasattr(doc, "getSheets"):
                sheets = doc.getSheets()
                for idx in range(sheets.getCount()):
                    if sheets.getByIndex(idx).getName() == sheet_obj.getName():
                        sheet_idx = idx
                        break

            pos = _parse_base_address(doc, base_cell, default_sheet_idx=sheet_idx)
            type_mask = _parse_flags(flags_arg)

            container.addNewByName(name, content, pos, type_mask)
            log.info("Named range added: [%s] %s -> %s (flags=%d)", effective_scope, name, content, type_mask)
            return {
                "status": "ok",
                "message": f"Named range '{name}' added successfully in scope '{effective_scope}' pointing to '{content}'.",
            }
        except Exception as e:
            log.exception("Add named range failed for %s", name)
            return self._tool_error(f"Failed to add named range: {str(e)}", code="NAMED_RANGE_ERROR")


class NamedRangeEdit(ToolCalcRangeBase):
    """Edit an existing named range: rename, change content, change base position, or change flags."""

    name = "named_range_edit"
    intent = "edit"
    description = (
        "Modifies an existing named range: rename, update formula/range content, change base reference position, or update flags."
    )
    parameters = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Current name of the range to edit."},
            "new_name": {"type": "string", "description": "New name for the range if renaming."},
            "content": {"type": "string", "description": "New formula or range address content."},
            "scope": {
                "type": "string",
                "description": "Scope where the named range exists: 'global' (default) or specific sheet name.",
            },
            "base_cell": {"type": "string", "description": "New base cell reference for relative coordinates."},
            "flags": _FLAGS_SCHEMA,
        },
        "required": ["name"],
    }
    is_mutation = True

    def execute(self, ctx, **kwargs):
        bridge = CalcBridge(ctx.doc)
        name = kwargs["name"].strip()
        new_name = kwargs.get("new_name")
        content = kwargs.get("content")
        scope = kwargs.get("scope")
        base_cell = kwargs.get("base_cell")
        flags_arg = kwargs.get("flags")

        try:
            doc = bridge.get_active_document()
            container, effective_scope, sheet_obj = _resolve_container(doc, scope)

            if not container.hasByName(name):
                return self._tool_error(f"No named range found with name '{name}' in scope '{effective_scope}'.", code="NAMED_RANGE_NOT_FOUND")

            nr = container.getByName(name)

            if content is not None:
                nr.setContent(content.strip())

            if flags_arg is not None:
                type_mask = _parse_flags(flags_arg)
                nr.setType(type_mask)

            if base_cell is not None:
                sheet_idx = 0
                if sheet_obj is not None and hasattr(doc, "getSheets"):
                    sheets = doc.getSheets()
                    for idx in range(sheets.getCount()):
                        if sheets.getByIndex(idx).getName() == sheet_obj.getName():
                            sheet_idx = idx
                            break
                pos = _parse_base_address(doc, base_cell, default_sheet_idx=sheet_idx)
                nr.setReferencePosition(pos)

            if new_name is not None and new_name.strip() and new_name.strip() != name:
                new_clean = new_name.strip()
                if container.hasByName(new_clean):
                    return self._tool_error(f"Cannot rename to '{new_clean}': a named range with that name already exists in scope '{effective_scope}'.", code="NAMED_RANGE_EXISTS")
                nr.setName(new_clean)
                final_name = new_clean
            else:
                final_name = name

            log.info("Named range edited: [%s] %s (new_name=%s)", effective_scope, name, new_name)
            return {
                "status": "ok",
                "message": f"Named range '{final_name}' updated successfully in scope '{effective_scope}'.",
            }
        except Exception as e:
            log.exception("Edit named range failed for %s", name)
            return self._tool_error(f"Failed to edit named range: {str(e)}", code="NAMED_RANGE_ERROR")


class NamedRangeDelete(ToolCalcRangeBase):
    """Delete an existing named range from the workbook or specific sheet."""

    name = "named_range_delete"
    intent = "edit"
    description = "Deletes an existing named range from global or sheet-specific scope."
    parameters = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Name of the range to delete."},
            "scope": {
                "type": "string",
                "description": "Scope of the named range: 'global' (default) or specific sheet name.",
            },
        },
        "required": ["name"],
    }
    is_mutation = True

    def execute(self, ctx, **kwargs):
        bridge = CalcBridge(ctx.doc)
        name = kwargs["name"].strip()
        scope = kwargs.get("scope")

        try:
            doc = bridge.get_active_document()
            container, effective_scope, _ = _resolve_container(doc, scope)

            if not container.hasByName(name):
                return self._tool_error(f"No named range found with the name '{name}' in scope '{effective_scope}'.", code="NAMED_RANGE_NOT_FOUND")

            container.removeByName(name)
            log.info("Named range deleted: [%s] %s", effective_scope, name)
            return {"status": "ok", "message": f"Named range '{name}' deleted successfully from scope '{effective_scope}'."}
        except Exception as e:
            log.exception("Delete named range failed for %s", name)
            return self._tool_error(f"Failed to delete named range: {str(e)}", code="NAMED_RANGE_ERROR")


class NamedRangeCreateFromTitles(ToolCalcRangeBase):
    """Batch-create named ranges from header row/column titles."""

    name = "named_range_create_from_titles"
    intent = "edit"
    description = (
        "Automatically creates multiple named ranges based on the content of title cells (headers) in a table range. "
        "Border specifies where headers are located ('top', 'bottom', 'left', 'right')."
    )
    parameters = {
        "type": "object",
        "properties": {
            "range": {
                "type": "array",
                "items": {"type": "string"},
                "description": "The table cell range containing both headers and data (e.g. [\"A1:D20\"] or [\"Sheet1.A1:D20\"]).",
            },
            "border": {
                "type": "string",
                "description": "Which edge contains the title labels: 'top' (default), 'bottom', 'left', or 'right'.",
                "enum": ["top", "bottom", "left", "right"],
            },
            "scope": {
                "type": "string",
                "description": "Container scope for creating the names: 'global' (default) or specific sheet name.",
            },
        },
        "required": ["range"],
    }
    is_mutation = True

    def execute(self, ctx, **kwargs):
        import uno
        bridge = CalcBridge(ctx.doc)
        range_str = kwargs["range"][0].strip()
        border_str = kwargs.get("border", "top")
        if border_str is None or str(border_str).strip() == "":
            border_str = "top"
        border_clean = str(border_str).strip().upper()
        scope = kwargs.get("scope")

        try:
            from com.sun.star.sheet.Border import BOTTOM, LEFT, RIGHT, TOP

            border_map: dict[str, Any] = {
                "TOP": TOP,
                "BOTTOM": BOTTOM,
                "LEFT": LEFT,
                "RIGHT": RIGHT,
            }
        except Exception:
            try:
                import uno

                border_map = {
                    "TOP": uno.Enum("com.sun.star.sheet.Border", "TOP"),
                    "BOTTOM": uno.Enum("com.sun.star.sheet.Border", "BOTTOM"),
                    "LEFT": uno.Enum("com.sun.star.sheet.Border", "LEFT"),
                    "RIGHT": uno.Enum("com.sun.star.sheet.Border", "RIGHT"),
                }
            except Exception:
                border_map = {
                    "TOP": 0,
                    "BOTTOM": 1,
                    "LEFT": 2,
                    "RIGHT": 3,
                }

        if border_clean not in border_map:
            return self._tool_error(f"Invalid border '{border_str}'. Supported borders: 'top', 'bottom', 'left', 'right'.", code="INVALID_BORDER")

        try:
            doc = bridge.get_active_document()
            sheet, address = bridge.resolve(range_str)
            cell_range = bridge.get_cell_range(sheet, address)
            range_addr = cell_range.getRangeAddress()

            container, effective_scope, _ = _resolve_container(doc, scope)
            container.addNewFromTitles(range_addr, border_map[border_clean])

            log.info("Named ranges created from titles: [%s] range=%s border=%s", effective_scope, range_str, border_clean)
            return {
                "status": "ok",
                "message": f"Named ranges created from titles along border '{border_str}' for range '{range_str}' in scope '{effective_scope}'.",
            }
        except Exception as e:
            log.exception("Create named ranges from titles failed for %s", range_str)
            return self._tool_error(f"Failed to create named ranges from titles: {str(e)}", code="NAMED_RANGE_ERROR")
