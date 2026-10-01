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
"""General utilities for Calc tools."""

from __future__ import annotations

from typing import Any, Tuple, cast
import uno

from plugin.framework.errors import UnoObjectError


def resolve_sheet(doc: Any, sheet_name: str | None = None) -> Any:
    """Return the target sheet (by name or active)."""
    if sheet_name:
        sheets = doc.getSheets()
        if not sheets.hasByName(sheet_name):
            raise UnoObjectError("Sheet not found: %s" % sheet_name)
        return cast("Any", sheets.getByName(sheet_name))
    controller = doc.getCurrentController()
    if hasattr(controller, "getActiveSheet"):
        active = controller.getActiveSheet()
        if active is not None:
            return cast("Any", active)
    return cast("Any", doc.getSheets().getByIndex(0))


def query_interface(obj: Any, typename: str) -> Any:
    """PyUNO requires ``uno.getTypeByName`` for ``queryInterface``; imported IDL classes fail."""
    if obj is None or not hasattr(obj, "queryInterface"):
        return None
    return obj.queryInterface(uno.getTypeByName(typename))


def resolve_sheet_and_cell(doc: Any, address: str) -> tuple[Any, int, int] | None:
    """Resolve *address* (e.g. 'A1' or 'Sheet1.B2') to ``(sheet, col, row)`` for an open Calc document."""
    from plugin.calc.address_utils import parse_address, split_sheet_prefix

    text = (address or "").strip()
    if not text or doc is None:
        return None
    sheet_name, cell_part = split_sheet_prefix(text)
    try:
        col, row = parse_address(cell_part)
    except ValueError:
        return None

    try:
        sheet = resolve_sheet(doc, sheet_name)
    except Exception:
        return None
    if sheet is None:
        return None
    return sheet, col, row


def resolve_cell_address(doc: Any, address: str) -> Any:
    """Convert a cell address string (e.g. 'A1' or 'Sheet1.A1') to a UNO CellAddress struct.

    Raises:
        UnoObjectError: When sheet cannot be found or address is invalid.
    """
    resolved = resolve_sheet_and_cell(doc, address)
    if resolved is None:
        raise UnoObjectError(f"Cannot resolve cell address '{address}'.")
    sheet, col, row = resolved
    cell = sheet.getCellByPosition(col, row)
    return cell.getCellAddress()


def get_cell_geometry(sheet: Any, cell: Any) -> Tuple[Any, Any]:
    """Return (Position, Size) for *cell*, collapsing merged areas to get correct coordinates.

    Standard ``cell.Position`` / ``cell.Size`` return the top-left sub-cell geometry
    when cells are merged, which is wrong for overlay placement.  This helper detects
    the merge and asks for the full merged area's geometry instead.
    """
    geometry_target = get_cell_geometry_target(sheet, cell)
    try:
        return geometry_target.Position, geometry_target.Size
    except Exception:
        return cell.Position, cell.Size


def get_cell_geometry_target(sheet: Any, cell: Any) -> Any:
    """Return the UNO object whose Position/Size should drive placement.

    For merged cells this is a collapsed cursor/range over the full merged area;
    otherwise this is the original cell.
    """
    try:
        if getattr(cell, "IsMerged", False):
            cursor = sheet.createCursorByRange(cell)
            cursor.collapseToMergedArea()
            return cursor
    except Exception:
        pass
    return cell
