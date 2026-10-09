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


# Used when FormulaResult.VALUE cannot be read (unit tests, or a mock enum).
# Live builds disagree: some expose VALUE as 0, some as 1.
_FORMULA_RESULT_VALUE_FALLBACK = 1
_formula_result_value_code: int | None = None


def _numeric_formula_value(numeric: Any) -> float | None:
    """Float for a real number from ``getValue()``. Booleans are not numbers here."""
    if isinstance(numeric, bool) or not isinstance(numeric, (int, float)):
        return None
    return float(numeric)


def _runtime_formula_result_value() -> int:
    """``FormulaResult.VALUE`` on this LibreOffice, else 1.

    A hardcoded ``1`` misses builds where ``VALUE`` is ``0``. The enum
    module is not in the type stubs, so a ``from com.sun.star...`` import
    fails ``ty``. ``uno.getConstantByName`` is the string lookup other
    Calc code uses for the same constants.
    """
    global _formula_result_value_code
    if _formula_result_value_code is not None:
        return _formula_result_value_code
    try:
        import uno

        # String name, not an import: FormulaResult is absent from the type
        # stubs, and ty rejects int() on the untyped constant. The runtime
        # value is a plain int (0 on some builds, 1 on others).
        raw = uno.getConstantByName("com.sun.star.sheet.FormulaResult.VALUE")
        if type(raw) is not int:
            raise TypeError("FormulaResult.VALUE is not an int")
        code = raw
    except Exception:
        return _FORMULA_RESULT_VALUE_FALLBACK
    _formula_result_value_code = code
    return code


def _formula_result_is_value(kind: Any) -> bool:
    if kind is None:
        return False
    try:
        val = int(kind)
        runtime_val = _runtime_formula_result_value()
        # On all LibreOffice builds, FormulaResult.VALUE is 0 or 1.
        # If the runtime constant is 0 or 1, match that. If uno returned
        # an invalid code (e.g. from an untyped mock), fall back to 0 or 1.
        if runtime_val in (0, 1):
            return val == runtime_val
        return val in (0, 1)
    except (TypeError, ValueError):
        return False


def formula_cell_result(cell: Any) -> Any:
    """Return a formula cell's result, keeping numbers as floats.

    Any non-zero ``getValue()`` is a number, and numeric zero is
    ``FormulaResult.VALUE`` from this runtime. Text stays ``getString()``.
    Comparing ``FormulaResultType`` to a hardcoded ``1`` fails where
    ``VALUE`` is ``0``, and ``getValue() != 0`` is false for zero, so
    ``=2+3`` came back as the text ``"5"`` and ``=1-1`` as ``"0"``. The
    contract for a numeric formula is ``getValue()``, a float.
    """
    numeric = cell.getValue()
    as_float = _numeric_formula_value(numeric)
    # Non-zero does not need the enum. A mismatched VALUE code must not
    # replace the float with the display string.
    if as_float is not None and as_float != 0.0:
        return as_float
    if _formula_result_is_value(getattr(cell, "FormulaResultType", None)) and as_float is not None:
        return as_float
    text = cell.getString()
    return text


_formula_cell_result = formula_cell_result
