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

from plugin.calc.address_utils import parse_address, split_sheet_prefix
from plugin.calc.bridge import CalcBridge
from plugin.framework.errors import UnoObjectError, uno_read

# What was wrong: CellContentType was imported from com.sun.star.table, which is not an attribute
# in live PyUNO (enum types are packages/modules in LibreOffice's python bridge). The resulting
# ImportError caused EMPTY, VALUE, TEXT, FORMULA to fall back to integers (0, 1, 2, 3), but
# cell.getType() returns uno.Enum instances in live LibreOffice. Since uno.Enum != int,
# cell_value_and_type() fell through to "unknown", breaking date/time/duration enrichment.
# How it happened: Refactor in cf457aa16 (#1984) and 48565691d (#1992) altered the enum import.
# Why this change: Directly import enum members from com.sun.star.table.CellContentType so
# live PyUNO binds genuine uno.Enum objects while mock/fallback environments retain int fallbacks.
try:
    from com.sun.star.table.CellContentType import EMPTY, FORMULA, TEXT, VALUE
except (ImportError, AttributeError):
    EMPTY = cast("Any", 0)
    VALUE = cast("Any", 1)
    TEXT = cast("Any", 2)
    FORMULA = cast("Any", 3)


def resolve_sheet(doc: Any, sheet_name: str | None = None) -> Any:
    """Return the target sheet (by name or active)."""
    if sheet_name:
        return CalcBridge(doc).get_sheet(sheet_name)
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
    text = (address or "").strip()
    if not text or doc is None:
        return None
    sheet_name, cell_part = split_sheet_prefix(text)
    try:
        col, row = parse_address(cell_part)
    except ValueError:
        return None

    sheet = uno_read(lambda: resolve_sheet(doc, sheet_name), None)
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
    return geometry_target.Position, geometry_target.Size


def get_cell_geometry_target(sheet: Any, cell: Any) -> Any:
    """Return the UNO object whose Position/Size should drive placement.

    For merged cells this is a collapsed cursor/range over the full merged area;
    otherwise this is the original cell.
    """
    if getattr(cell, "IsMerged", False):
        cursor = sheet.createCursorByRange(cell)
        cursor.collapseToMergedArea()
        return cursor
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
    # FormulaResultType2 holds FormulaResult (VALUE=1). FormulaResultType does not
    # identify a numeric zero on real LibreOffice (=1-1 came back as "0").
    kind = getattr(cell, "FormulaResultType2", None)
    if not isinstance(kind, int):
        kind = getattr(cell, "FormulaResultType", None)
    if _formula_result_is_value(kind) and as_float is not None:
        return as_float
    text = cell.getString()
    return text


def cell_value_and_type(cell: Any, *, empty_as_none: bool = True, formula_error_as_string: bool = True) -> tuple[Any, str]:
    kind = cell.getType()
    if kind == EMPTY:
        val = None if empty_as_none else cell.getString()
        return val, "empty"
    if kind == VALUE:
        return cell.getValue(), "value"
    if kind == TEXT:
        return cell.getString(), "text"
    if kind == FORMULA:
        if formula_error_as_string and cell.getError() != 0:
            val = cell.getString()
        else:
            val = formula_cell_result(cell)
        return val, "formula"
    return cell.getString(), "unknown"


def cell_type_name(cell_type: Any) -> str:
    """Return a human-readable name for a UNO cell content type."""
    if cell_type == EMPTY:
        return "empty"
    if cell_type == VALUE:
        return "value"
    if cell_type == TEXT:
        return "text"
    if cell_type == FORMULA:
        return "formula"
    return "unknown"


_formula_cell_result = formula_cell_result
