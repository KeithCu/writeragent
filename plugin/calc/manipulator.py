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
"""Cell manipulator — writing data and formatting LibreOffice Calc cells.

Ported from core/calc_manipulator.py for the plugin framework.
UNO imports are deferred to method bodies.
"""

from __future__ import annotations

import csv
import io
import logging
import re
import sys
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from com.sun.star.awt import FontWeight
    from com.sun.star.awt.FontSlant import ITALIC, NONE
    from com.sun.star.table.CellHoriJustify import CENTER, LEFT, RIGHT, BLOCK, STANDARD
    from com.sun.star.table.CellVertJustify import CENTER as V_CENTER, TOP, BOTTOM
    from com.sun.star.table import BorderLine, BorderLineStyle, TableSortField
else:
    try:
        from com.sun.star.awt import FontWeight
        from com.sun.star.awt.FontSlant import ITALIC, NONE
        from com.sun.star.table.CellHoriJustify import CENTER, LEFT, RIGHT, BLOCK, STANDARD
        from com.sun.star.table.CellVertJustify import CENTER as V_CENTER, TOP, BOTTOM
        from com.sun.star.table import BorderLine, BorderLineStyle, TableSortField
    except ImportError:
        pass


from plugin.calc import CalcError
from plugin.calc.address_utils import index_to_column
from plugin.calc.array_formula import MAX_ARRAY_CELLS, result_range, returns_array
from plugin.calc.formula_fill import expand_single_formula
from plugin.calc.datetime_wire import coalesce_temporal_apply_rects, duration_serial_from_iso, is_compatible_temporal_template, match_iso_duration, match_iso_temporal, should_preserve_temporal_format
from plugin.calc.error_detector import get_calc_error_name
from plugin.calc.inspector import _format_category_from_type
from plugin.framework.errors import safe_json_loads, is_disposed_exception
from plugin.framework.uno_context import get_ctx


log = logging.getLogger("writeragent.calc")


# ── Helper ─────────────────────────────────────────────────────────────


def _uno_range_address(cell_or_range: Any) -> Any:
    """Normalize a UNO cell or range to ``CellRangeAddress``.

    ``resolve_range_or_address`` returns an ``XCell`` for a single cell
    (``getCellAddress`` only) and an ``XCellRange`` for A1:B2 / named ranges
    (``getRangeAddress``). ``copyRange`` needs the latter shape.
    """
    if hasattr(cell_or_range, "getRangeAddress"):
        return cell_or_range.getRangeAddress()
    cell_addr = cell_or_range.getCellAddress()
    from com.sun.star.table import CellRangeAddress

    ra = CellRangeAddress()
    ra.Sheet = cell_addr.Sheet
    ra.StartColumn = cell_addr.Column
    ra.EndColumn = cell_addr.Column
    ra.StartRow = cell_addr.Row
    ra.EndRow = cell_addr.Row
    return ra


def _parse_formula_or_values_string(s: str, *, single_cell_range: bool = False) -> list[Any] | None:
    """Parse *formula_or_values* when it arrives as a JSON string or as a
    raw semicolon-separated string.

    The AI often sends formula_or_values as a JSON-encoded string (e.g.
    ``'["Name"; "Category"; "Value"]'``) or as a raw string like
    ``'Name;Category;Value'``.  Without this, write_formula_range would
    write the whole string as one value per cell.  We normalise
    LibreOffice-style semicolon separators and return a flat list.

    Args:
        s: Raw string from the tool caller.
        single_cell_range: True when ``write_formula_range`` targets exactly
            one cell (range corners coincide). Passed so Case 2 does not treat
            ordinary prose (commas, semicolons) as CSV columns for that cell.

    Returns:
        A flat list of values, or *None* if *s* should be treated as a
        single literal value.
    """
    if not isinstance(s, str):
        return None

    s_strip = s.strip()
    if not s_strip:
        return None

    # Case 1: JSON array e.g. ["a"; "b"] or ["a", "b"]
    if s_strip.startswith("["):
        try:
            # Replace semicolons NOT inside double quotes with commas.
            normalized_list = []
            in_quotes = False
            escaped = False
            for char in s_strip:
                if char == '"' and not escaped:
                    in_quotes = not in_quotes
                if char == ";" and not in_quotes:
                    normalized_list.append(",")
                else:
                    normalized_list.append(char)
                if char == "\\" and not escaped:
                    escaped = True
                else:
                    escaped = False

            normalized = "".join(normalized_list)
            data = safe_json_loads(normalized)
            if data is not None:
                if isinstance(data, list):
                    flat = []
                    for item in data:
                        if isinstance(item, list):
                            flat.extend(item)
                        else:
                            flat.append(item)
                    return flat
        except TypeError:
            pass

    # Case 2: Raw semicolon-separated string or multiline CSV
    # Only if it is not a formula (starting with =)
    if not s_strip.startswith("="):
        # Could be multiline CSV or single line with delimiter
        delimiter = ","
        first_line = s.split("\n")[0] if s else ""
        if ";" in first_line and "," not in first_line:
            delimiter = ";"

        # If it has a delimiter or is multiline, try to parse it
        if delimiter in s or "\n" in s:
            try:
                reader = csv.reader(io.StringIO(s), delimiter=delimiter, skipinitialspace=True)
                rows = list(reader)
                if rows:
                    if len(rows) == 1:
                        # Case 2a: single logical CSV row (possibly multiple fields).
                        #
                        # Bug we fix: csv.reader splits on the data delimiter, so a
                        # one-cell write like "Hello, world" becomes two fields and
                        # write_formula_range then errors ("Array has N values but range
                        # has 1 cells") or would mis-split if we padded silently.
                        #
                        # When *single_cell_range* is True, a single line with multiple
                        # fields is ambiguous (real CSV row vs sentence with commas /
                        # semicolons). We treat it as literal text for that one cell.
                        #
                        # Tradeoff: you cannot use a raw comma-separated *row* string
                        # to mean "many columns" into a single cell—use a contiguous
                        # range (e.g. A1:B1) or a JSON array for exact per-cell values.
                        #
                        # Multiline CSV is unchanged: len(rows) > 1 still returns a 2D
                        # list below and write_formula_range expands the anchor cell.
                        #
                        # Future: optional tool flag (csv_mode / literal_string);
                        # tighter heuristics (quoted fields, uniform column counts, tabs);
                        # JSON arrays remain the precise N-value path.
                        if single_cell_range and len(rows[0]) > 1:
                            return None
                        return [val.strip() for val in rows[0]]
                    else:
                        # 2D array representing multiline CSV
                        # We return it as a list of lists, but write_formula_range needs to flatten it
                        # Wait, if we return a 2D array, write_formula_range can process it and adjust its target range.
                        return [[val.strip() for val in row] for row in rows]
            except Exception as e:
                if is_disposed_exception(e):
                    raise
                log.debug("Failed to read sample csv: %s", e)

    return None


# ── Manipulator ────────────────────────────────────────────────────────


class CellManipulator:
    """Manages data writing and style application to cells."""

    bridge: Any

    def __init__(self, bridge: Any) -> None:
        """
        Args:
            bridge: CalcBridge instance.
        """
        self.bridge = bridge

    # ── Internal helpers ───────────────────────────────────────────────

    def _raise_if_intersects_array(self, sheet: Any, start_col: int, start_row: int, end_col: int, end_row: int, allow_contained: bool = False) -> None:
        '''Raise CalcError if the range intersects an array formula.

        If allow_contained is True, it's permissible for the range to fully enclose an array formula
        (e.g., when clearing a large area). It's an error only if the array formula is partially outside the range.
        '''
        cursor = sheet.createCursor()
        cursor.gotoEndOfUsedArea(False)
        used = cursor.getRangeAddress()

        c0, r0 = start_col, start_row
        c1, r1 = min(end_col, used.EndColumn), min(end_row, used.EndRow)

        if c0 > c1 or r0 > r1:
            return

        # Only formula cells can belong to an array, so probe just those
        # (CellFlags.FORMULA = 16) instead of every cell: a big clear stays cheap.
        formula_ranges = sheet.getCellRangeByPosition(c0, r0, c1, r1).queryContentCells(16)
        contained: list[Any] = []
        for fa in formula_ranges.getRangeAddresses():
            for r in range(fa.StartRow, fa.EndRow + 1):
                for c in range(fa.StartColumn, fa.EndColumn + 1):
                    if any(b.StartColumn <= c <= b.EndColumn and b.StartRow <= r <= b.EndRow for b in contained):
                        continue
                    existing = self._array_block(sheet, c, r)
                    if not existing:
                        continue
                    if allow_contained:
                        if existing.StartColumn >= start_col and existing.EndColumn <= end_col and existing.StartRow >= start_row and existing.EndRow <= end_row:
                            contained.append(existing)
                            continue

                    cell_name = f"{index_to_column(c)}{r + 1}"
                    arr_name = f"{index_to_column(existing.StartColumn)}{existing.StartRow + 1}:{index_to_column(existing.EndColumn)}{existing.EndRow + 1}"
                    raise CalcError(f"{cell_name} is part of array {arr_name}; edit or clear the whole array.")

    def _is_valid_cell_address(self, address: str) -> bool:
        """Validate if a string is a valid cell address (e.g., A1)."""
        if not address:
            return False
        return bool(re.match(r"^[A-Za-z]+[1-9][0-9]*$", address.strip()))

    def _get_error_name(self, error_code: int) -> str:
        """Get a human-readable name for a Calc error code."""
        return get_calc_error_name(error_code)

    def _apply_style_properties(self, obj: Any, bold: bool | None, italic: bool | None, bg_color: int | None, font_color: int | None, font_size: float | None, h_align: str | None, v_align: str | None, wrap_text: bool | None, border_color: int | None) -> None:
        """Apply common style properties to a cell or range object."""
        if bold is not None:
            FW = sys.modules.get("com.sun.star.awt.FontWeight", None)
            if FW is None:
                BOLD, NORMAL = FontWeight.BOLD, FontWeight.NORMAL
            else:
                BOLD, NORMAL = getattr(FW, "BOLD"), getattr(FW, "NORMAL")
            obj.setPropertyValue("CharWeight", BOLD if bold else NORMAL)

        if italic is not None:
            obj.setPropertyValue("CharPosture", ITALIC if italic else NONE)

        if bg_color is not None:
            obj.setPropertyValue("CellBackColor", bg_color)

        if font_color is not None:
            obj.setPropertyValue("CharColor", font_color)

        if font_size is not None:
            obj.setPropertyValue("CharHeight", font_size)

        if h_align is not None:
            align_map: dict[str, Any] = {"left": LEFT, "center": CENTER, "right": RIGHT, "justify": BLOCK, "standard": STANDARD}
            if h_align.lower() in align_map:
                obj.setPropertyValue("HoriJustify", align_map[h_align.lower()])

        if v_align is not None:
            align_map = {"top": TOP, "center": CENTER, "bottom": BOTTOM, "standard": STANDARD}
            if v_align.lower() in align_map:
                obj.setPropertyValue("VertJustify", align_map[v_align.lower()])

        if wrap_text is not None:
            obj.setPropertyValue("IsTextWrapped", wrap_text)

        if border_color is not None:
            self._apply_borders(obj, border_color)

    def _apply_borders(self, obj: Any, color: int) -> None:
        """Apply borders to a cell or range object."""
        try:
            import uno
            line = uno.createUnoStruct("com.sun.star.table.BorderLine2")
            setattr(line, "Color", color)
            setattr(line, "OuterLineWidth", 50)
            setattr(line, "LineStyle", BorderLineStyle.SOLID)

            obj.setPropertyValue("TopBorder2", line)
            obj.setPropertyValue("BottomBorder2", line)
            obj.setPropertyValue("LeftBorder2", line)
            obj.setPropertyValue("RightBorder2", line)
            return
        except Exception as e:
            if is_disposed_exception(e):
                raise
            pass

        line = BorderLine()
        setattr(line, "Color", color)
        line.OuterLineWidth = 50  # 1/100 mm; 50 == 0.5 mm

        obj.setPropertyValue("TopBorder", line)
        obj.setPropertyValue("BottomBorder", line)
        obj.setPropertyValue("LeftBorder", line)
        obj.setPropertyValue("RightBorder", line)

    # ── Write operations ───────────────────────────────────────────────

    def safe_get_cell_value(self, sheet: Any, cell_address: str) -> Any:
        """Safely get cell value with comprehensive error handling."""
        try:
            # Validate sheet
            if not sheet:
                raise CalcError("Sheet is None", code="CALC_SHEET_NULL", details={"operation": "get_cell_value"})

            # Validate cell address
            if not self._is_valid_cell_address(cell_address):
                raise CalcError(f"Invalid cell address: {cell_address}", code="CALC_INVALID_ADDRESS", details={"address": cell_address})

            # Get cell
            try:
                cell = sheet.getCellRangeByName(cell_address)
            except Exception as e:
                if is_disposed_exception(e):
                    raise
                cell = None
            if not cell:
                raise CalcError(f"Cell not found: {cell_address}", code="CALC_CELL_NOT_FOUND", details={"address": cell_address})

            # Get value with type handling
            cell_type = cell.getType()

            CCT = sys.modules.get("com.sun.star.table", None)
            if CCT is not None and hasattr(CCT, "CellContentType"):
                CCT = CCT.CellContentType

            # Also try to import for unmocked case
            if CCT is None:
                try:
                    from com.sun.star.table import CellContentType as CCT
                except ImportError:
                    pass

            if CCT is not None and cell_type == CCT.EMPTY:
                return None
            elif CCT is not None and cell_type == CCT.VALUE:
                return cell.getValue()
            elif CCT is not None and cell_type == CCT.TEXT:
                return cell.getString()
            elif CCT is not None and cell_type == CCT.FORMULA:
                try:
                    # In LibreOffice Calc, cell.getError() returns 0 if no error
                    error_code = cell.getError()
                    if error_code != 0:
                        raise Exception("Formula error")
                    return cell.getValue()
                except Exception as e:
                    if is_disposed_exception(e):
                        raise
                    # Formula error
                    error_code = cell.getError()
                    raise CalcError(f"Formula error in {cell_address}: {self._get_error_name(error_code)}", code="CALC_FORMULA_ERROR", details={"address": cell_address, "error_code": error_code, "error_name": self._get_error_name(error_code)}) from e
            else:
                raise CalcError(f"Unknown cell type: {cell_type}", code="CALC_UNKNOWN_CELL_TYPE", details={"address": cell_address, "type": cell_type})

        except CalcError:
            # Re-raise our calc errors
            raise
        except Exception as e:
            if is_disposed_exception(e):
                raise
            # Wrap other exceptions
            raise CalcError(f"Failed to get cell value: {str(e)}", code="CALC_CELL_VALUE_ERROR", details={"address": cell_address, "original_error": str(e), "error_type": type(e).__name__}) from e

    # ── Style operations ───────────────────────────────────────────────

    def set_cell_style(
        self,
        address_or_range: str,
        bold: bool | None = None,
        italic: bool | None = None,
        bg_color: int | None = None,
        font_color: int | None = None,
        font_size: float | None = None,
        h_align: str | None = None,
        v_align: str | None = None,
        wrap_text: bool | None = None,
        border_color: int | None = None,
        number_format: str | None = None,
    ) -> None:
        """Apply style to a cell or range.

        Delegates to range-specific helpers when the target contains ``:``.

        Args:
            address_or_range: Cell address or range (e.g. "A1" or "A1:D10").
            bold: Bold flag.
            italic: Italic flag.
            bg_color: Background colour (RGB int).
            font_color: Font colour (RGB int).
            font_size: Font size (points).
            h_align: Horizontal alignment ("left", "center", "right", "justify").
            v_align: Vertical alignment ("top", "center", "bottom").
            wrap_text: Wrap text flag.
            border_color: Border colour (RGB int).
            number_format: Number format string (e.g. "#,##0.00").
        """
        try:
            if ":" in address_or_range:
                self._set_range_style(address_or_range, bold=bold, italic=italic, bg_color=bg_color, font_color=font_color, font_size=font_size, h_align=h_align, v_align=v_align, wrap_text=wrap_text, border_color=border_color)
                if number_format:
                    self._set_range_number_format(address_or_range, number_format)
                log.info("Range %s style updated.", address_or_range.upper())
            else:
                cell = self.bridge.get_cell_by_address(address_or_range)
                self._apply_style_properties(cell, bold, italic, bg_color, font_color, font_size, h_align, v_align, wrap_text, border_color)
                if number_format:
                    self._set_number_format(address_or_range, number_format)
                log.info("Cell %s style updated.", address_or_range.upper())
        except Exception as e:
            if is_disposed_exception(e):
                raise
            log.exception("Style application failed for %s", address_or_range)
            raise CalcError(str(e)) from e

    def _set_range_style(
        self, range_str: str, bold: bool | None = None, italic: bool | None = None, bg_color: int | None = None, font_color: int | None = None, font_size: float | None = None, h_align: str | None = None, v_align: str | None = None, wrap_text: bool | None = None, border_color: int | None = None
    ) -> None:
        cell_range = self.bridge.resolve_range_or_address(range_str)
        self._apply_style_properties(cell_range, bold, italic, bg_color, font_color, font_size, h_align, v_align, wrap_text, border_color)

    @staticmethod
    def _resolve_document_locale(doc: Any) -> Any:
        """Return document CharLocale, or en-US when Language is empty/unusable (M2)."""
        import uno

        try:
            locale = doc.getPropertyValue("CharLocale")
            language = getattr(locale, "Language", None) or ""
            if language:
                return locale
        except Exception as e:
            if is_disposed_exception(e):
                raise
            log.debug("CharLocale unavailable; using en-US fallback", exc_info=True)
        return uno.createUnoStruct("com.sun.star.lang.Locale", Language="en", Country="US", Variant="")

    @staticmethod
    def _apply_number_format_key(target: Any, format_key: int) -> None:
        """Set NumberFormat from an integer registry key (detected keys, not format strings)."""
        target.setPropertyValue("NumberFormat", int(format_key))

    def _set_range_number_format(self, range_str: str, format_str: str) -> None:
        cell_range = self.bridge.resolve_range_or_address(range_str)
        doc = self.bridge.get_active_document()
        formats = doc.getNumberFormats()
        locale = self._resolve_document_locale(doc)
        format_id = formats.queryKey(format_str, locale, False)
        if format_id == -1:
            format_id = formats.addNew(format_str, locale)
        cell_range.setPropertyValue("NumberFormat", format_id)

    def _set_number_format(self, address: str, format_str: str) -> None:
        cell = self.bridge.get_cell_by_address(address)
        doc = self.bridge.get_active_document()
        formats = doc.getNumberFormats()
        locale = self._resolve_document_locale(doc)
        format_id = formats.queryKey(format_str, locale, False)
        if format_id == -1:
            format_id = formats.addNew(format_str, locale)
        cell.setPropertyValue("NumberFormat", format_id)

    # ── Range operations ───────────────────────────────────────────────

    def clear_range(self, range_str: str) -> None:
        """Clear all content in a cell range.

        Args:
            range_str: Cell range (e.g. "A1:D10").
        """
        try:
            cell_range = self.bridge.resolve_range_or_address(range_str)
            # clearContents on part of an array formula silently does nothing, so
            # refuse with a clear message unless the range covers the whole array.
            addr = _uno_range_address(cell_range)
            sheet = self.bridge.get_active_document().getSheets().getByIndex(addr.Sheet)
            self._raise_if_intersects_array(sheet, addr.StartColumn, addr.StartRow, addr.EndColumn, addr.EndRow, allow_contained=True)
            # CellFlags: VALUE=1, DATETIME=2, STRING=4, FORMULA=16 -> 23
            cell_range.clearContents(23)
            log.info("Range %s cleared.", range_str.upper())
        except CalcError:
            raise
        except Exception as e:
            if is_disposed_exception(e):
                raise
            log.exception("Range clear failed for %s", range_str)
            raise CalcError(str(e)) from e

    def merge_cells(self, range_str: str, center: bool = True) -> None:
        """Merge a cell range.

        Args:
            range_str: Cell range to merge (e.g. "A1:D1").
            center: Centre content after merging.
        """
        try:
            cell_range = self.bridge.resolve_range_or_address(range_str)
            cell_range.merge(True)
            log.info("Range %s merged.", range_str.upper())

            if center:
                cell_range.setPropertyValue("HoriJustify", CENTER)
                cell_range.setPropertyValue("VertJustify", V_CENTER)
        except Exception as e:
            if is_disposed_exception(e):
                raise
            log.exception("Cell merge failed for %s", range_str)
            raise CalcError(str(e)) from e

    def sort_range(self, range_str: str, sort_column: int = 0, ascending: bool = True, has_header: bool = True) -> str:
        """Sort a range.

        Args:
            range_str: Range to sort (e.g. "A1:D10").
            sort_column: 0-based column index within the range.
            ascending: True for ascending, False for descending.
            has_header: Whether the first row is a header.

        Returns:
            Description string.
        """
        try:
            cell_range = self.bridge.resolve_range_or_address(range_str)

            import uno  # noqa: F401  # pyright: ignore[reportUnusedImport] – needed in UNO context

            sort_desc = list(cell_range.createSortDescriptor())

            sort_field = TableSortField()
            sort_field.Field = sort_column
            sort_field.IsAscending = ascending
            sort_field.IsCaseSensitive = False

            for p in sort_desc:
                if p.Name == "SortFields":
                    p.Value = (sort_field,)
                elif p.Name == "ContainsHeader":
                    p.Value = has_header

            cell_range.sort(tuple(sort_desc))

            direction = "ascending" if ascending else "descending"
            log.info("Range %s sorted %s by column %d.", range_str.upper(), direction, sort_column)
            return f"Range {range_str} sorted {direction} by column {sort_column}."
        except Exception as e:
            if is_disposed_exception(e):
                raise
            log.exception("Sort failed for %s", range_str)
            raise CalcError(str(e)) from e

    def _make_number_formatter(self, doc: Any) -> Any:
        """Attach a NumberFormatter to *doc* once per write invocation.

        Chat tools pass a Layer-A guarded document. ``attachNumberFormatsSupplier``
        is called on the formatter (not through the doc proxy), so the proxy is
        not auto-unwrapped — hand it the raw UNO supplier or attach fails with
        an often-empty UNO message and ISO writes never run.
        """
        from plugin.framework.thread_guard import _unwrap_uno

        uno_ctx = _unwrap_uno(get_ctx())
        smgr = uno_ctx.getServiceManager()
        formatter = smgr.createInstanceWithContext("com.sun.star.util.NumberFormatter", uno_ctx)
        formatter.attachNumberFormatsSupplier(_unwrap_uno(doc))
        return formatter

    def _resolve_elapsed_format_key(self, formats: Any, locale: Any) -> int:
        """Built-in ``[HH]:MM:SS`` key (formatindex 43), with queryKey/addNew fallback."""
        try:
            return int(formats.getFormatIndex(43, locale))
        except Exception as e:
            if is_disposed_exception(e):
                raise
            log.debug("getFormatIndex(43) failed; falling back to queryKey", exc_info=True)
        key = formats.queryKey("[HH]:MM:SS", locale, False)
        if key == -1:
            key = formats.addNew("[HH]:MM:SS", locale)
        return int(key)

    def _classify_write_cell(self, value: Any, formatter: Any, std_key: Any, *, elapsed_format_key: int | None = None, literal_text: bool = False) -> tuple[Any, str, dict[str, Any]]:
        """Classify one write input into data/formula/meta for the ISO write path.

        Returns ``(data_value, formula_or_empty, meta)`` where *meta* keys are:
        ``kind`` (formula|forced_text|temporal|number|text|empty),
        ``input_category``, ``detected_key``, ``restore_format`` (S29).

        ``literal_text`` stores the characters as text: no live formula and no
        ``float()`` (OCR ids such as ``000123``).
        """
        meta: dict[str, Any] = {"kind": "empty", "input_category": None, "detected_key": None, "restore_format": False}

        if literal_text:
            if value is None or value == "":
                return "", "", meta
            meta["kind"] = "forced_text"
            return value if isinstance(value, str) else str(value), "", meta

        if value is None:
            return "", "", meta

        if isinstance(value, (int, float)) and not isinstance(value, bool):
            meta["kind"] = "number"
            return value, "", meta

        if not isinstance(value, str):
            meta["kind"] = "text"
            meta["restore_format"] = True
            return str(value), "", meta

        if value == "":
            return "", "", meta

        if value.startswith("="):
            meta["kind"] = "formula"
            return "", value, meta

        # Leading apostrophe forces literal text and leaves @ (S18) — no S29 restore.
        if value.startswith("'"):
            meta["kind"] = "forced_text"
            return value[1:], "", meta

        stripped = value.strip()
        # Duration is WriterAgent arithmetic (isodate); Calc's formatter does not parse PT….
        if match_iso_duration(stripped) and elapsed_format_key is not None:
            try:
                serial = duration_serial_from_iso(stripped)
            except Exception as e:
                if is_disposed_exception(e):
                    raise
                log.debug("Duration convert failed for %r: %s", stripped, e)
                meta["kind"] = "text"
                meta["restore_format"] = True
                return value, "", meta
            meta["kind"] = "temporal"
            meta["input_category"] = "duration"
            meta["detected_key"] = int(elapsed_format_key)
            return float(serial), "", meta

        input_category = match_iso_temporal(stripped)
        if input_category is not None and formatter is not None:
            try:
                detected_key = formatter.detectNumberFormat(std_key, stripped)
                serial = formatter.convertStringToNumber(std_key, stripped)
                meta["kind"] = "temporal"
                meta["input_category"] = input_category
                meta["detected_key"] = int(detected_key)
                return float(serial), "", meta
            except Exception as e:
                if is_disposed_exception(e):
                    raise
                # NotNumericException and peers → ordinary text fallback (S4).
                if "NotNumeric" not in type(e).__name__:
                    log.debug("ISO convert failed for %r: %s", stripped, e)
                meta["kind"] = "text"
                meta["restore_format"] = True
                return value, "", meta

        try:
            num = float(value)
            meta["kind"] = "number"
            return num, "", meta
        except ValueError:
            meta["kind"] = "text"
            meta["restore_format"] = True
            return value, "", meta

    def _find_column_temporal_templates(self, sheet: Any, formats: Any, columns_needing_templates: dict[int, str], scan_start_row: int, category_cache: dict[int, str | None], max_scan: int = 100) -> dict[int, int]:
        """Find inherited NumberFormat keys by scanning upward in each column (P1).

        Args:
            sheet: The active sheet.
            formats: doc.getNumberFormats().
            columns_needing_templates: {col_index: input_category} for columns needing template.
            scan_start_row: Row index to start scanning upward from (exclusive).
            category_cache: Shared key -> category cache (mutated in place).
            max_scan: Maximum rows to scan upward.

        Returns:
            {col_index: inherited_format_key} for columns where a template was found.

        Compatibility is stricter than M1 preserve (see ``is_compatible_temporal_template``):
        date does not inherit datetime; clock time skips elapsed FormatString templates.
        """
        column_templates: dict[int, int] = {}
        # FormatString beside Type so clock-time P1 can skip elapsed [HH]:… templates.
        format_code_cache: dict[int, object | None] = {}
        for col, input_category in columns_needing_templates.items():
            start_r = scan_start_row - 1
            min_r = max(0, scan_start_row - max_scan)
            for r in range(start_r, min_r - 1, -1):
                try:
                    cell = sheet.getCellByPosition(col, r)
                    key = int(cell.getPropertyValue("NumberFormat"))
                except Exception as e:
                    if is_disposed_exception(e):
                        raise
                    continue
                if key == 0:
                    continue
                if key not in category_cache or key not in format_code_cache:
                    try:
                        props = formats.getByKey(key)
                        if key not in category_cache:
                            category_cache[key] = _format_category_from_type(props.getPropertyValue("Type"))
                        if key not in format_code_cache:
                            format_code_cache[key] = props.getPropertyValue("FormatString")
                    except Exception as e:
                        if is_disposed_exception(e):
                            raise
                        if key not in category_cache:
                            category_cache[key] = None
                        if key not in format_code_cache:
                            format_code_cache[key] = None
                cat = category_cache[key]
                if is_compatible_temporal_template(input_category, cat, format_code_cache.get(key)):
                    column_templates[col] = key
                    break
        return column_templates

    def _apply_temporal_format_runs(self, sheet: Any, start: tuple[int, int], decisions: Any) -> int:
        """Apply detected NumberFormat keys as coalesced 2D rectangles (S8/S25).

        Resolves empty-cell bridging per row, finds horizontal apply runs, then
        vertically merges identical ``(c0, c1, key)`` spans before one range set
        per rectangle.
        """
        start_col, start_row = start
        rects = coalesce_temporal_apply_rects(decisions)
        applied = 0
        for r0, r1, c0, c1, key in rects:
            target = sheet.getCellRangeByPosition(start_col + c0, start_row + r0, start_col + c1, start_row + r1)
            self._apply_number_format_key(target, key)
            applied += (r1 - r0 + 1) * (c1 - c0 + 1)
        return applied

    def prepare_array_formula_if_needed(self, range_str: str, formula_or_values: Any, array: Any = None) -> tuple[int, int] | None:
        '''Run the array formula measure and occupancy check without committing.

        Returns (rows, cols) if an array formula was measured, else None.
        We use a hidden undo context, not lock(), because Calc still records API edits while
        XUndoManager is locked. This merges the probe's temporary actions cleanly without leaving
        a phantom Ctrl+Z step.
        '''
        if isinstance(formula_or_values, str) and returns_array(formula_or_values, array):
            cell_range = self.bridge.resolve_range_or_address(range_str)
            addr = _uno_range_address(cell_range)
            start = (addr.StartColumn, addr.StartRow)
            end = (addr.EndColumn, addr.EndRow)

            single_cell_range = start == end
            parsed = _parse_formula_or_values_string(formula_or_values, single_cell_range=single_cell_range)
            if parsed is not None:
                formula_or_values = parsed

            if isinstance(formula_or_values, str) and returns_array(formula_or_values, array):
                sheet = self.bridge.get_active_document().getSheets().getByIndex(addr.Sheet)
                doc = self.bridge.get_active_document()

                was_modified = doc.isModified()
                undo_mgr = doc.getUndoManager() if hasattr(doc, "getUndoManager") else None

                was_possible = False
                hidden_context_entered = False

                if undo_mgr:
                    was_possible = undo_mgr.isUndoPossible()
                    try:
                        undo_mgr.enterHiddenUndoContext()
                        hidden_context_entered = True
                    except Exception as e:
                        if "EmptyUndoStackException" in type(e).__name__:
                            pass
                        else:
                            raise
                try:
                    c2, r2 = end
                    rows, cols = self._measure_array(sheet, formula_or_values, avoid_col=c2, avoid_start_row=start[1], avoid_end_row=r2)
                    self._write_array_formula(sheet, formula_or_values, start, end, measure_only=True, premeasured_array_size=(rows, cols))
                    return (rows, cols)
                finally:
                    if hidden_context_entered and undo_mgr:
                        try:
                            undo_mgr.leaveUndoContext()
                        except Exception:
                            pass
                    elif undo_mgr and not was_possible:
                        try:
                            undo_mgr.clear()
                        except Exception:
                            pass

                    if not was_modified:
                        try:
                            doc.setModified(False)
                        except Exception:
                            pass
        return None

    def write_formula_range(self, range_str: str, formula_or_values: Any, array: Any = None, *, literal_text: bool = False, premeasured_array_size: tuple[int, int] | None = None) -> str | dict[str, Any]:
        """Write formula(s) or value(s) to a cell range.

        ISO date/time strings matching the wire gate become Calc serials with
        category-compatible NumberFormat preservation (see docs/calc/date-time-handling.md).

        Args:
            range_str: Cell range (e.g. "A1:A10", "B2:D2").
            formula_or_values: Single formula/value for all cells, or a
                list/array of values for each cell. A single ordinary
                formula into a 1×N or N×1 range fill-down/across with
                relative A1 adjust. ``=PY`` uses a span-peek (multi-row
                DataRange stays verbatim). A 2-D range plus one formula
                is an error — pass an explicit array instead.
                A top-level FILTER/SORT/UNIQUE (or ``array=True``) is
                entered with ``setArrayFormula`` so the whole result shows.
            array: Optional override. ``True`` forces an array formula
                (LET/XLOOKUP); ``False`` forces scalar ``setFormula``.
            literal_text: When true, every non-empty value is stored with
                ``setString`` (text). Skips formula detection and ``float()``.

        Returns:
            Summary string, or a dict with ``array_range`` / ``rows`` /
            ``cols`` when an array formula was written.
        """
        expand_overwrite_warning: str | None = None
        try:
            # Handle empty values as a clear_range operation
            is_empty = formula_or_values is None or formula_or_values == "" or formula_or_values == [] or formula_or_values == "[]" or formula_or_values == "{}"
            if is_empty:
                self.clear_range(range_str)
                return f"Range {range_str} cleared."

            cell_range = self.bridge.resolve_range_or_address(range_str)
            addr = _uno_range_address(cell_range)
            start = (addr.StartColumn, addr.StartRow)
            end = (addr.EndColumn, addr.EndRow)

            num_rows = addr.EndRow - addr.StartRow + 1
            num_cols = addr.EndColumn - addr.StartColumn + 1
            total_cells = num_rows * num_cols

            # True when this invocation writes exactly one cell (corners match).
            # Passed into string parsing so we do not run the CSV "single row → many fields" path.
            single_cell_range = start == end

            # Normalise string-as-array from AI callers.
            if isinstance(formula_or_values, str):
                parsed = _parse_formula_or_values_string(formula_or_values, single_cell_range=single_cell_range)
                if parsed is not None:
                    formula_or_values = parsed

            # LibreOffice does not spill. setFormula on =SORT(FILTER(...))
            # used to return ok and show one value (nelson-mcp afc8cbd8 / #2631).
            # Detect top-level array functions and enter setArrayFormula
            # instead of fill-down. =SUM(FILTER()) stays scalar.
            if not literal_text and isinstance(formula_or_values, str) and returns_array(formula_or_values, array):
                sheet = self.bridge.get_active_document().getSheets().getByIndex(addr.Sheet)
                return self._write_array_formula(sheet, formula_or_values, start, end, premeasured_array_size=premeasured_array_size)

            if isinstance(formula_or_values, (list, tuple)):
                if len(formula_or_values) > 0 and isinstance(formula_or_values[0], (list, tuple)):
                    rows_cnt = len(formula_or_values)
                    cols_cnt = max(len(r) for r in formula_or_values)

                    if total_cells == 1:
                        # Expand single cell target to fit the 2D array
                        end = (start[0] + cols_cnt - 1, start[1] + rows_cnt - 1)
                        num_rows = end[1] - start[1] + 1
                        num_cols = end[0] - start[0] + 1
                        total_cells = num_rows * num_cols

                        sheet = cell_range.getSpreadsheet()

                        non_empty_count = 0
                        for r in range(start[1], end[1] + 1):
                            for c in range(start[0], end[0] + 1):
                                cell = sheet.getCellByPosition(c, r)
                                if cell.getFormula() or cell.getString():
                                    non_empty_count += 1
                        if non_empty_count > 0:
                            expand_overwrite_warning = f"overwrote {non_empty_count} non-empty cell(s)"

                        range_str = f"{self.bridge._index_to_column(start[0])}{start[1] + 1}:{self.bridge._index_to_column(end[0])}{end[1] + 1}"

                    # Pad rows to ensure uniform width, and flatten into 1D
                    flat_vals = []
                    for r in formula_or_values:
                        row_vals = list(r)
                        if len(row_vals) > num_cols:
                            raise CalcError(f"Row has {len(row_vals)} columns but the target range has {num_cols}. The array is too wide.")
                        if num_cols > len(row_vals):
                            row_vals.extend([""] * (num_cols - len(row_vals)))
                        flat_vals.extend(row_vals)
                    formula_or_values = flat_vals

                if len(formula_or_values) != total_cells:
                    raise CalcError(f"Array has {len(formula_or_values)} values but range has {total_cells} cells. Use a single string to fill the whole range, or an array with exactly that many values for cell-by-cell control.")
                values = formula_or_values
            else:
                # Used to pin ``[formula] * n`` so J2:J5 + ``=H2`` wrote
                # identical ``=H2`` in every cell (AFC fill-down miss).
                # Ordinary 1-D formulas now adjust relative A1 refs; =PY
                # span-peeks DataRange (multi-row → verbatim). 2-D + one
                # formula is refused. LO fill/series can be revisited later.
                if not literal_text and isinstance(formula_or_values, str) and formula_or_values.startswith("=") and total_cells > 1:
                    values = expand_single_formula(formula_or_values, num_rows, num_cols)
                else:
                    values = [formula_or_values] * total_cells

            doc = self.bridge.get_active_document()
            formats = doc.getNumberFormats()
            locale = self._resolve_document_locale(doc)
            std_key = formats.getStandardIndex(locale)
            # Lazy setup: one pass over values for ISO date/time vs PT duration candidates.
            needs_formatter = False
            needs_duration = False
            # OCR grids must not be scanned as dates or numbers. literal_text
            # commits with setString below.
            if not literal_text:
                for v in values:
                    if not isinstance(v, str) or v.startswith("=") or v.startswith("'"):
                        continue
                    stripped = v.strip()
                    if not needs_formatter and match_iso_temporal(stripped):
                        needs_formatter = True
                    if not needs_duration and match_iso_duration(stripped):
                        needs_duration = True
                    if needs_formatter and needs_duration:
                        break
            formatter = self._make_number_formatter(doc) if needs_formatter else None
            elapsed_format_key = self._resolve_elapsed_format_key(formats, locale) if needs_duration else None

            data_array: list[list[Any]] = []
            formula_cells: list[tuple[int, int, str]] = []  # (col, row, formula)
            literal_text_cells: list[tuple[int, int, str]] = []
            # Per-cell meta in row-major order matching values
            cell_metas: list[dict[str, Any]] = []
            counts = {"date": 0, "time": 0, "datetime": 0, "duration": 0, "text": 0, "formula": 0, "number": 0}

            cell_idx = 0
            for row in range(start[1], end[1] + 1):
                data_row: list[Any] = []
                for col in range(start[0], end[0] + 1):
                    data_val, formula, meta = self._classify_write_cell(values[cell_idx], formatter, std_key, elapsed_format_key=elapsed_format_key, literal_text=literal_text)
                    if literal_text and meta["kind"] == "forced_text":
                        # setDataArray/SetString parses a leading '=' as a formula
                        # and float() would drop leading zeros. Commit text via setString.
                        literal_text_cells.append((col, row, str(data_val)))
                        data_val = ""
                    if formula:
                        formula_cells.append((col, row, formula))
                        counts["formula"] += 1
                    elif meta["kind"] == "temporal":
                        counts[meta["input_category"]] = counts.get(meta["input_category"], 0) + 1
                    elif meta["kind"] in ("text", "forced_text"):
                        counts["text"] += 1
                    elif meta["kind"] == "number":
                        counts["number"] += 1
                    data_row.append(data_val)
                    cell_metas.append(meta)
                    cell_idx += 1
                data_array.append(data_row)

            sheet = cell_range.getSpreadsheet()
            cell_range = sheet.getCellRangeByPosition(start[0], start[1], end[0], end[1])

            # Snapshot NumberFormat keys before setDataArray for ordinary text (S29) and
            # for temporal cells (M1 destination category). Floats keep format through commit.
            s29_snapshots: list[tuple[int, int, int]] = []  # col, row, key
            dest_categories: dict[tuple[int, int], str | None] = {}
            category_cache: dict[int, str | None] = {}
            any_temporal = any(m["kind"] == "temporal" for m in cell_metas)

            cell_idx = 0
            for row in range(start[1], end[1] + 1):
                for col in range(start[0], end[0] + 1):
                    meta = cell_metas[cell_idx]
                    if meta["restore_format"] or (any_temporal and meta["kind"] == "temporal"):
                        cell = sheet.getCellByPosition(col, row)
                        try:
                            key = int(cell.getPropertyValue("NumberFormat"))
                        except Exception as e:
                            if is_disposed_exception(e):
                                raise
                            key = 0
                        if meta["restore_format"]:
                            s29_snapshots.append((col, row, key))
                        if meta["kind"] == "temporal":
                            if key not in category_cache:
                                try:
                                    props = formats.getByKey(key)
                                    category_cache[key] = _format_category_from_type(props.getPropertyValue("Type"))
                                except Exception as e:
                                    if is_disposed_exception(e):
                                        raise
                                    category_cache[key] = None
                            dest_categories[(col, row)] = category_cache[key]
                    cell_idx += 1

            # We write cell by cell, not plain setDataArray, because LO's setDataArray
            # undo does not clear previously-empty cells; threshold 2000 trades undo
            # correctness for speed on big writes.
            UNDO_CLEAR_THRESHOLD = 2000
            use_set_data_array = True

            if total_cells <= UNDO_CLEAR_THRESHOLD:
                # Check if there's any empty cell in the target range
                has_empty = False
                try:
                    from com.sun.star.table import CellContentType
                    empty_type = CellContentType.EMPTY
                except ImportError:
                    empty_type = None

                for r in range(start[1], end[1] + 1):
                    for c in range(start[0], end[0] + 1):
                        cell = sheet.getCellByPosition(c, r)
                        if empty_type is not None and cell.getType() == empty_type:
                            has_empty = True
                            break
                        elif cell.getFormula() == "" and cell.getString() == "" and cell.getValue() == 0.0:
                            has_empty = True
                            break
                    if has_empty:
                        break

                if has_empty:
                    use_set_data_array = False

            if use_set_data_array:
                cell_range.setDataArray(tuple(tuple(r) for r in data_array))
            else:
                for r_idx, row in enumerate(range(start[1], end[1] + 1)):
                    for c_idx, col in enumerate(range(start[0], end[0] + 1)):
                        val = data_array[r_idx][c_idx]
                        if val == "":
                            sheet.getCellByPosition(col, row).setString("")
                        elif isinstance(val, (int, float)):
                            sheet.getCellByPosition(col, row).setValue(float(val))
                        else:
                            sheet.getCellByPosition(col, row).setString(str(val))

            for col, row, key in s29_snapshots:
                sheet.getCellByPosition(col, row).setPropertyValue("NumberFormat", key)

            formula_warning = ""
            for col, row, formula in formula_cells:
                cell = sheet.getCellByPosition(col, row)
                cell.setFormula(formula)
                if not formula_warning:
                    err = int(cell.Error)
                    if err != 0:
                        hint = " (hint: Calc uses ';' as the argument separator)" if err in (508, 525, 501) else ""
                        formula_warning = f"; warning: formula gives Err:{err}{hint}"

            for col, row, text_value in literal_text_cells:
                sheet.getCellByPosition(col, row).setString(text_value)

            format_warning = ""
            if any_temporal:
                # P1: collect columns needing templates (temporal cells with non-temporal destination)
                cols_needing: dict[int, str] = {}
                cell_idx = 0
                for row in range(start[1], end[1] + 1):
                    for col in range(start[0], end[0] + 1):
                        meta = cell_metas[cell_idx]
                        if meta["kind"] == "temporal" and dest_categories.get((col, row)) is None:
                            if col not in cols_needing:
                                cols_needing[col] = meta["input_category"]
                        cell_idx += 1

                column_templates: dict[int, int] = {}
                if cols_needing:
                    column_templates = self._find_column_temporal_templates(sheet, formats, cols_needing, start[1], category_cache)

                # Build row decisions: None | "empty" | ("apply", key) | ("preserve", None)
                decisions: list[list[tuple[str, int | None] | str | None]] = []
                cell_idx = 0
                for row in range(start[1], end[1] + 1):
                    row_dec: list[tuple[str, int | None] | str | None] = []
                    for col in range(start[0], end[0] + 1):
                        meta = cell_metas[cell_idx]
                        data_val = data_array[row - start[1]][col - start[0]]
                        if meta["kind"] == "formula":
                            row_dec.append(None)
                        elif meta["kind"] == "temporal":
                            dest_cat = dest_categories.get((col, row))
                            if should_preserve_temporal_format(meta["input_category"], float(data_val), dest_cat):
                                row_dec.append(("preserve", None))
                            else:
                                apply_key = column_templates.get(col, meta["detected_key"])
                                row_dec.append(("apply", apply_key))
                        elif data_val == "" and meta["kind"] == "empty":
                            row_dec.append("empty")
                        else:
                            row_dec.append(None)
                        cell_idx += 1
                    decisions.append(row_dec)
                try:
                    self._apply_temporal_format_runs(sheet, start, decisions)
                except Exception as e:
                    if is_disposed_exception(e):
                        raise
                    log.exception("Date/time format pass failed for range %s", range_str)
                    # S30: count cells that needed apply, not preserve-only temporals.
                    apply_n = sum(1 for row_dec in decisions for d in row_dec if isinstance(d, tuple) and d[0] == "apply")
                    if apply_n:
                        format_warning = f"; could not apply date/time formats to {apply_n} cells in {range_str}"
                    else:
                        format_warning = f"; date/time format pass failed for {range_str}"

            parts = []
            for singular, count_key in (("date", "date"), ("time", "time"), ("datetime", "datetime"), ("duration", "duration"), ("number", "number"), ("text", "text"), ("formula", "formula")):
                n = counts.get(count_key, 0)
                if n:
                    parts.append(f"{n} {singular}" + ("s" if n != 1 and singular != "text" else ""))
            detail = f" ({', '.join(parts)})" if parts else ""
            n_vals = len(values)
            values_word = "value" if n_vals == 1 else "values"
            msg = f"Range {range_str} filled with {n_vals} {values_word}{detail}{format_warning}{formula_warning}."

            if expand_overwrite_warning:
                msg = f"{msg} ({expand_overwrite_warning})"

            log.info("%s", msg)
            return msg
        except CalcError:
            # Occupied-target / formula-error refusals are the caller's input,
            # not a write-path fault.
            raise
        except Exception as e:
            if is_disposed_exception(e):
                raise
            # Check if this cell intersects an array formula
            try:
                cell_range = self.bridge.resolve_range_or_address(range_str)
                addr = _uno_range_address(cell_range)
                sheet = self.bridge.get_active_document().getSheets().getByIndex(addr.Sheet)
                self._raise_if_intersects_array(sheet, addr.StartColumn, addr.StartRow, addr.EndColumn, addr.EndRow, allow_contained=False)
            except CalcError:
                raise
            except Exception:
                pass

            # UNO often yields str(e) == ""; keep a usable message for the agent.
            msg = str(e) or getattr(e, "Message", None) or type(e).__name__
            log.exception("Range formula write failed for %s", range_str)
            raise CalcError(msg) from e

    @staticmethod
    def _array_probe_origin(used: Any, avoid_col: int, avoid_start_row: int, avoid_end_row: int) -> tuple[int, int]:
        """``(column, first_row)`` for the two-cell ``ROWS``/``COLUMNS`` probe.

        The probe sits two columns to the right of the used area and the
        write target, clamped to XFD (column 16383). When the used area
        already reaches XFD, that clamp used to land inside the used
        rectangle and ``setArrayFormula`` / ``clearContents`` overwrote
        rows 0–1 of the user's last column. Place the probe on the two
        rows under the used area instead.
        """
        max_col = 16383  # XFD
        max_row = 1_048_575
        col = min(max(int(used.EndColumn), avoid_col) + 2, max_col)
        row = 0
        overlaps_used = col <= int(used.EndColumn) and int(used.StartRow) <= 1 and int(used.EndRow) >= 0
        overlaps_target = col <= avoid_col and avoid_start_row <= 1 and avoid_end_row >= 0
        if overlaps_used or overlaps_target:
            row = max(int(used.EndRow), avoid_end_row) + 1
            if row + 1 > max_row:
                raise CalcError("Cannot measure the array formula: no free cells below the used area in column XFD.")
        return col, row

    def _measure_array(self, sheet: Any, formula: str, *, avoid_col: int, avoid_start_row: int = 0, avoid_end_row: int = 0) -> tuple[int, int]:
        """``(rows, columns)`` of *formula*'s result, measured by LibreOffice.

        ``=ROWS(expr)`` and ``=COLUMNS(expr)`` are entered as array formulas
        in two scratch cells to the right of the used area (and the target),
        read, then cleared. When that column would fall inside the used
        area (sheet already at XFD), the scratch cells move below it.
        Raises ``CalcError`` when the formula itself fails.
        """
        cursor = sheet.createCursor()
        cursor.gotoEndOfUsedArea(False)
        used = cursor.getRangeAddress()
        col, row0 = self._array_probe_origin(used, avoid_col, avoid_start_row, avoid_end_row)
        expr = formula[1:] if formula.startswith("=") else formula
        sizes: list[int] = []
        try:
            for row, fn in ((row0, "ROWS"), (row0 + 1, "COLUMNS")):
                probe = sheet.getCellRangeByPosition(col, row, col, row)
                probe.setArrayFormula("=%s(%s)" % (fn, expr))
                cell = sheet.getCellByPosition(col, row)
                err = int(cell.Error)
                if err:
                    shown = cell.getString()
                    # #NAME? (525) means an unknown function, not an empty FILTER.
                    if err == 525:
                        hint = " — unknown function name (this LibreOffice may lack TAKE/VSTACK etc.)"
                    else:
                        hint = " — e.g. FILTER with no matching row gives #CALC!"
                    raise CalcError("The formula returns an error (%s, code %d)%s." % (shown or "error", err, hint))
                sizes.append(int(round(cell.getValue())))
        finally:
            for row in (row0, row0 + 1):
                try:
                    probe = sheet.getCellRangeByPosition(col, row, col, row)
                    probe.setArrayFormula("")
                    probe.clearContents(23)
                except Exception as e:
                    if is_disposed_exception(e):
                        raise
                    pass
        return sizes[0], sizes[1]

    @staticmethod
    def _array_block(sheet: Any, col: int, row: int) -> Any | None:
        """Range address of the array formula covering (*col*, *row*), or None."""
        try:
            cursor = sheet.createCursorByRange(sheet.getCellRangeByPosition(col, row, col, row))
            cursor.collapseToCurrentArray()
            addr = cursor.getRangeAddress()
            rng = sheet.getCellRangeByPosition(addr.StartColumn, addr.StartRow, addr.EndColumn, addr.EndRow)
            if not rng.getArrayFormula():
                return None
            return addr
        except Exception as e:
            if is_disposed_exception(e):
                raise
            return None

    def _write_array_formula(self, sheet: Any, formula: str, start: tuple[int, int], end: tuple[int, int], measure_only: bool = False, premeasured_array_size: tuple[int, int] | None = None) -> dict[str, Any]:
        """Enter *formula* as an array formula so its whole result shows.

        From a single cell, the result range is sized from the result and
        must be empty except the origin (an array formula already anchored
        there is replaced). On an explicit range, that range is used as
        given; the answer says if the result is larger (rows cut) or
        smaller (#N/A padding).
        """
        c1, r1 = start
        c2, r2 = end
        explicit = (c1, r1) != (c2, r2)
        if premeasured_array_size:
            rows, cols = premeasured_array_size
        else:
            rows, cols = self._measure_array(sheet, formula, avoid_col=c2, avoid_start_row=r1, avoid_end_row=r2)

        def block_name(a: int, b: int, c: int, d: int) -> str:
            return "%s%d:%s%d" % (index_to_column(a), b + 1, index_to_column(c), d + 1)

        notes: list[str] = []
        if explicit:
            target = (c1, r1, c2, r2)
            height, width = r2 - r1 + 1, c2 - c1 + 1
            if height * width > MAX_ARRAY_CELLS:
                raise CalcError("The target range is %d x %d cells, over the %d-cell limit." % (height, width, MAX_ARRAY_CELLS))
            if rows > height or cols > width:
                notes.append("The result is %d x %d but the range is %d x %d: the rest is cut (result_does_not_fit)." % (rows, cols, height, width))
            elif rows < height or cols < width:
                notes.append("The result is %d x %d, smaller than the range: the extra cells show #N/A." % (rows, cols))
        else:
            if rows * cols > MAX_ARRAY_CELLS:
                raise CalcError("The result is %d x %d cells, over the %d-cell limit." % (rows, cols, MAX_ARRAY_CELLS))
            target = result_range(c1, r1, rows, cols)

        # Rewriting the same array formula in place is an update.
        # Cells inside the array being replaced are not occupied, and the
        # clear runs only after the new target is accepted. Clearing first
        # with setArrayFormula("") deletes the old array before an occupied
        # target cell can refuse the write.
        existing = self._array_block(sheet, c1, r1)
        replaced_box: tuple[int, int, int, int] | None = None
        if existing is not None and (existing.StartColumn, existing.StartRow) == (c1, r1):
            replaced_box = (int(existing.StartColumn), int(existing.StartRow), int(existing.EndColumn), int(existing.EndRow))

        t_c1, t_r1, t_c2, t_r2 = target
        occupied: list[tuple[int, int]] = []
        for row in range(t_r1, t_r2 + 1):
            for col in range(t_c1, t_c2 + 1):
                if (col, row) == (c1, r1):
                    continue
                if replaced_box is not None and replaced_box[0] <= col <= replaced_box[2] and replaced_box[1] <= row <= replaced_box[3]:
                    continue
                cell = sheet.getCellByPosition(col, row)
                if cell.getFormula() or cell.getString():
                    occupied.append((col, row))
        if occupied:
            block_c, block_r = occupied[0]
            raise CalcError("The result needs %s, but %d cell(s) there are not empty (first: %s). Nothing was written; clear them or start elsewhere." % (block_name(*target), len(occupied), "%s%d" % (index_to_column(block_c), block_r + 1)))

        if measure_only:
            return {}

        if replaced_box is not None:
            sheet.getCellRangeByPosition(*replaced_box).setArrayFormula("")

        rng = sheet.getCellRangeByPosition(*target)
        rng.setArrayFormula(formula)
        anchor = sheet.getCellByPosition(t_c1, t_r1)
        if int(anchor.Error):
            try:
                rng.setArrayFormula("")
                rng.clearContents(23)
            except Exception as e:
                if is_disposed_exception(e):
                    raise
                pass
            raise CalcError("The formula returns an error (%s, code %d)." % (anchor.getString() or "error", int(anchor.Error)))
        preview = [list(r) for r in rng.getDataArray()[:5]]
        payload: dict[str, Any] = {"message": "Array formula entered on %s (%d x %d)." % (block_name(*target), rows, cols), "array_range": block_name(*target), "rows": rows, "cols": cols, "preview": preview}
        if notes:
            payload["warning"] = " ".join(notes)
        log.info("%s", payload["message"])
        return payload

    def copy_formula_range(self, source_str: str, dest_str: str) -> dict[str, Any]:
        """Copy formulas and values from *source_str* onto *dest_str* start.

        Uses sheet ``copyRange`` (``XCellRangeMovement``) so the paste matches
        Calc: relative refs shift by the dest offset, ``$`` anchors stay.
        Dest is the top-left (or a range whose start is used); size is the
        source extent.
        """
        from com.sun.star.table import CellAddress

        try:
            source_obj = self.bridge.resolve_range_or_address(source_str)
            dest_obj = self.bridge.resolve_range_or_address(dest_str)
            source_ra = _uno_range_address(source_obj)
            dest_ra = _uno_range_address(dest_obj)
            rows = int(source_ra.EndRow) - int(source_ra.StartRow) + 1
            cols = int(source_ra.EndColumn) - int(source_ra.StartColumn) + 1

            dest_cell = CellAddress()
            dest_cell.Sheet = dest_ra.Sheet
            dest_cell.Column = dest_ra.StartColumn
            dest_cell.Row = dest_ra.StartRow

            dest_sheet = self.bridge.get_active_document().getSheets().getByIndex(dest_ra.Sheet)
            dest_sheet.copyRange(dest_cell, source_ra)

            msg = f"Copied {rows}×{cols} from {source_str} onto {dest_str}."
            log.info("%s", msg)
            return {"message": msg, "rows_copied": rows, "cols_copied": cols}
        except Exception as e:
            if is_disposed_exception(e):
                raise
            msg = str(e) or getattr(e, "Message", None) or type(e).__name__
            log.exception("Range copy failed from %s onto %s", source_str, dest_str)
            raise CalcError(msg) from e

    # ── Chart ──────────────────────────────────────────────────────────

    # ── Structure operations ───────────────────────────────────────────

    def delete_rows(self, row_num: int, count: int = 1) -> str:
        """Delete rows starting at *row_num* (1-based)."""
        try:
            sheet = self.bridge.get_active_sheet()
            rows = sheet.getRows()
            rows.removeByIndex(row_num - 1, count)
            log.info("%d row(s) deleted starting from row %d.", count, row_num)
            return f"{count} row(s) deleted starting from row {row_num}."
        except Exception as e:
            if is_disposed_exception(e):
                raise
            log.exception("Row deletion failed")
            raise CalcError(str(e)) from e

    def delete_columns(self, col_letter: str, count: int = 1) -> str:
        """Delete columns starting at *col_letter*."""
        try:
            sheet = self.bridge.get_active_sheet()
            columns = sheet.getColumns()
            col_index = self.bridge._column_to_index(col_letter.upper())
            columns.removeByIndex(col_index, count)
            log.info("%d column(s) deleted starting from column %s.", count, col_letter.upper())
            return f"{count} column(s) deleted starting from column {col_letter.upper()}."
        except Exception as e:
            if is_disposed_exception(e):
                raise
            log.exception("Column deletion failed")
            raise CalcError(str(e)) from e

    def delete_structure(self, structure_type: str, start: Any, count: int = 1) -> str:
        """Delete rows or columns.

        Args:
            structure_type: "rows" or "columns".
            start: For rows, row number (1-based); for columns, column letter.
            count: Number to delete.
        """
        if structure_type == "rows":
            return self.delete_rows(start, count)
        elif structure_type == "columns":
            return self.delete_columns(start, count)
        else:
            raise CalcError(f"Invalid structure_type: {structure_type}. Must be 'rows' or 'columns'.")
