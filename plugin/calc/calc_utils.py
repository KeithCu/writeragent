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

import logging
from typing import Any, cast
import uno
from com.sun.star.awt import Point

from plugin.calc.address_utils import parse_address, split_sheet_prefix
from plugin.calc.bridge import CalcBridge
from plugin.calc.datetime_wire import format_category_from_type
from plugin.framework.errors import ToolExecutionError, is_disposal, uno_read

log = logging.getLogger(__name__)

# Import CellContentType members from com.sun.star.table.CellContentType, not com.sun.star.table:
# live PyUNO exposes enums as submodules; int fallbacks must still compare equal to uno.Enum from getType().
try:
    from com.sun.star.table.CellContentType import EMPTY, FORMULA, TEXT, VALUE
except (ImportError, AttributeError):
    EMPTY = cast("Any", 0)
    VALUE = cast("Any", 1)
    TEXT = cast("Any", 2)
    FORMULA = cast("Any", 3)


def range_args(raw: Any, *, exactly_one: bool = False) -> list[Any]:
    """Normalize tool ``range`` kwargs to a list of range strings."""
    if exactly_one:
        if not isinstance(raw, list) or len(raw) != 1 or not isinstance(raw[0], str) or not raw[0].strip():
            raise ToolExecutionError("Pass exactly one range.", code="INVALID_PARAM")
        return [raw[0].strip()]
    if not raw:
        return []
    if isinstance(raw, str):
        return [raw]
    return list(raw)


def query_interface(obj: Any, typename: str) -> Any:
    """PyUNO requires ``uno.getTypeByName`` for ``queryInterface``; imported IDL classes fail."""
    if obj is None or not hasattr(obj, "queryInterface"):
        return None
    return obj.queryInterface(uno.getTypeByName(typename))


def resolve_sheet_and_cell(doc: Any, address: str) -> tuple[Any, int, int] | None:
    """Resolve *address* (e.g. 'A1' or 'Sheet1.B2') to ``(sheet, col, row)`` for an open Calc document.

    This is the "or None" variant of ``CalcBridge.resolve_cell`` for program-built
    A1 addresses (cell links, dependency chain, goal seek) and does not take a named range.
    """
    text = (address or "").strip()
    if not text or doc is None:
        return None
    sheet_name, cell_part = split_sheet_prefix(text)
    try:
        col, row = parse_address(cell_part)
    except ValueError:
        return None

    sheet = uno_read(lambda: CalcBridge(doc).sheet_or_active(sheet_name), None)
    if sheet is None:
        return None
    return sheet, col, row


def get_cell_geometry(sheet: Any, cell: Any) -> tuple[Any, Any]:
    """Return (Position, Size) for *cell*, collapsing merged areas to get correct coordinates.

    Standard ``cell.Position`` / ``cell.Size`` return the top-left sub-cell geometry
    when cells are merged, which is wrong for overlay placement.  This helper detects
    the merge and asks for the full merged area's geometry instead.
    """
    if getattr(cell, "IsMerged", False):
        cell = sheet.createCursorByRange(cell)
        cell.collapseToMergedArea()
    return cell.Position, cell.Size


def sheet_is_rtl(sheet: Any) -> bool:
    """True when sheet writing direction is right-to-left (TableLayout == 1)."""
    try:
        return int(sheet.TableLayout) == 1
    except (AttributeError, TypeError, ValueError):
        return False


def selection_top_left_cell(controller: Any, sheet: Any) -> Any | None:
    """Return the top-left cell of *controller*'s selection on *sheet*, or None.

    Shape selections (``SvxShapeCollection``) and multi-range selections
    (``ScCellRangesObj``) have no ``getRangeAddress`` and return None. A closed
    document raises.
    """

    def _read() -> Any:
        addr = controller.getSelection().getRangeAddress()
        return sheet.getCellByPosition(addr.StartColumn, addr.StartRow)

    return uno_read(_read, None)


def anchor_shape_at_cell(sheet: Any, shape: Any, cell: Any) -> None:
    """Anchor *shape* to *cell* with Excel's "Move but don't size with cells" semantics.

    Sets ``Anchor`` to *cell* and ``ResizeWithCell`` to False, which LibreOffice
    exports as OOXML ``twoCellAnchor editAs="oneCell"`` (``xcl97rec.cxx``
    ``GetEditAs``). Uses ``get_cell_geometry`` so merged blocks resolve to the
    merged area's coordinates, and mirrors horizontally on RTL sheets
    (``ScDocument::GetMMRect`` / ``shapeuno.cxx`` "Anchor") so the shape's right
    edge aligns with the cell's right edge.
    """
    pos, size = get_cell_geometry(sheet, cell)
    shape.setPropertyValue("Anchor", cell)
    shape.setPropertyValue("ResizeWithCell", False)
    x = int(pos.X)
    if sheet_is_rtl(sheet):
        x += int(size.Width) - int(shape.getSize().Width)
    shape.setPosition(Point(x, int(pos.Y)))


def _runtime_formula_result_value() -> int:
    """``FormulaResult.VALUE`` on this LibreOffice, else 1.

    A hardcoded ``1`` misses builds where ``VALUE`` is ``0``. The enum
    module is not in the type stubs, so a ``from com.sun.star...`` import
    fails ``ty``. ``uno.getConstantByName`` is the string lookup other
    Calc code uses for the same constants.
    """
    try:
        # String name, not an import: FormulaResult is absent from the type
        # stubs, and ty rejects int() on the untyped constant. The runtime
        # value is a plain int (0 on some builds, 1 on others).
        raw = uno.getConstantByName("com.sun.star.sheet.FormulaResult.VALUE")
        if type(raw) is not int:
            raise TypeError("FormulaResult.VALUE is not an int")
        return raw
    except Exception:
        return 1


def formula_cell_result(cell: Any) -> Any:
    """Return a formula cell's result, keeping numbers as floats.

    Non-zero ``getValue()`` is always numeric. Zero uses ``FormulaResult.VALUE``
    on this runtime (0 or 1 depending on build); text formulas keep ``getString()``.
    """
    numeric = cell.getValue()
    if isinstance(numeric, bool) or not isinstance(numeric, (int, float)):
        as_float = None
    else:
        as_float = float(numeric)
    if as_float is not None and as_float != 0.0:
        return as_float
    kind = getattr(cell, "FormulaResultType2", None)
    if not isinstance(kind, int):
        kind = getattr(cell, "FormulaResultType", None)
    runtime_val = _runtime_formula_result_value()
    try:
        kind_int = int(kind) if kind is not None else None
    except (TypeError, ValueError):
        kind_int = None
    value_codes = {runtime_val} if runtime_val in (0, 1) else {0, 1}
    if as_float is not None and kind_int in value_codes:
        return as_float
    return cell.getString()


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


def classify_array_cell(raw_value: Any, raw_formula: str, is_formula: bool) -> tuple[Any, str, str | None]:
    """(value, type, formula) of one cell from its getDataArray / getFormulaArray entries.

    is_formula must come from the cell type (getType, or a CellFlags.FORMULA query):
    the formula text cannot tell a formula from a text cell typed as '=abc', and
    array-formula cells read '{=...}'. A formula whose result is an error reads None,
    because getDataArray gives void for it (rangeseq.cxx ScRangeToSequence::FillMixedArray);
    error_detect reports its token.
    """
    if is_formula:
        return raw_value, "formula", raw_formula
    if isinstance(raw_value, float):
        return raw_value, "value", None
    if isinstance(raw_value, str) and raw_value:
        return raw_value, "text", None
    return None, "empty", None


def resolve_document_locale(doc: Any) -> Any:
    """Return document CharLocale, or en-US when Language is empty or unreadable."""
    try:
        locale = doc.getPropertyValue("CharLocale")
        language = getattr(locale, "Language", None) or ""
        if language:
            return locale
    except Exception as e:
        if is_disposal(e):
            raise
        log.debug("CharLocale unavailable; using en-US fallback", exc_info=True)
    return uno.createUnoStruct("com.sun.star.lang.Locale", Language="en", Country="US", Variant="")


def elapsed_format_key(formats: Any, locale: Any) -> int:
    """Built-in ``[HH]:MM:SS`` key (formatindex 43), with queryKey/addNew fallback."""
    try:
        return int(formats.getFormatIndex(43, locale))
    except Exception as e:
        if is_disposal(e):
            raise
        log.debug("getFormatIndex(43) failed; falling back to queryKey", exc_info=True)
    key = formats.queryKey("[HH]:MM:SS", locale, False)
    if key == -1:
        key = formats.addNew("[HH]:MM:SS", locale)
    return int(key)


def standard_temporal_format_keys(formats: Any, locale: Any) -> dict[str, int]:
    """Standard date, time, datetime, and duration number-format keys for ``locale``.

    These are the keys a ``=PY()`` spill neighbour gets when its destination has no
    date/time format; the chat write path does not use them because it detects a key
    per value and inherits column templates.
    """
    return {
        "date": int(formats.getStandardFormat(2, locale)),
        "time": int(formats.getStandardFormat(4, locale)),
        "datetime": int(formats.getStandardFormat(6, locale)),
        "duration": elapsed_format_key(formats, locale),
    }


class NumberFormatCategories:
    """The date/time category and FormatString of each number-format key, read once per key.

    One read or write meets the same few NumberFormat keys on many cells; this
    keeps one getByKey per key for the whole operation. A read that raises is
    not remembered, so each caller's own guard decides what a failure means
    (the write path treats it as "no category"; the spill writer and the
    single-cell read let it propagate).
    """

    def __init__(self, formats: Any) -> None:
        self._formats: Any = formats  # the document's XNumberFormats (doc.getNumberFormats())
        self._seen: dict[int, tuple[str | None, str | None]] = {}

    def lookup(self, key: int) -> tuple[str | None, str | None]:
        """(format_category_from_type(Type), FormatString as str or None) for *key*."""
        if key not in self._seen:
            props = self._formats.getByKey(key)
            code = props.getPropertyValue("FormatString")
            self._seen[key] = (format_category_from_type(props.getPropertyValue("Type")), None if code is None else str(code))
        return self._seen[key]

    def category(self, key: int) -> str | None:
        """Just the date / time / datetime category of *key*, or None."""
        return self.lookup(key)[0]
