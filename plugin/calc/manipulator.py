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

import csv
import io
import logging
import re
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from com.sun.star.awt import FontWeight
    from com.sun.star.awt.FontSlant import ITALIC, NONE
    from com.sun.star.table.CellHoriJustify import CENTER, LEFT, RIGHT, BLOCK, STANDARD
    from com.sun.star.table.CellVertJustify import CENTER as V_CENTER, TOP, BOTTOM
    from com.sun.star.table import BorderLine, TableSortField
else:
    try:
        from com.sun.star.awt import FontWeight
        from com.sun.star.awt.FontSlant import ITALIC, NONE
        from com.sun.star.table.CellHoriJustify import CENTER, LEFT, RIGHT, BLOCK, STANDARD
        from com.sun.star.table.CellVertJustify import CENTER as V_CENTER, TOP, BOTTOM
        from com.sun.star.table import BorderLine, TableSortField
    except ImportError:
        pass


from plugin.calc import CalcError
from plugin.calc.datetime_wire import (
    coalesce_temporal_apply_rects,
    duration_serial_from_iso,
    match_iso_duration,
    match_iso_temporal,
    should_preserve_temporal_format,
)
from plugin.calc.inspector import _format_category_from_type
from plugin.framework.errors import ToolExecutionError, UnoObjectError, safe_json_loads
from plugin.framework.uno_context import get_ctx

logger = logging.getLogger("writeragent.calc")


# ── Helper ─────────────────────────────────────────────────────────────


def _parse_formula_or_values_string(s: str, *, single_cell_range: bool = False):
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
                logger.debug("Failed to read sample csv: %s", e)

    return None


# ── Manipulator ────────────────────────────────────────────────────────


class CellManipulator:
    """Manages data writing and style application to cells."""

    def __init__(self, bridge):
        """
        Args:
            bridge: CalcBridge instance.
        """
        self.bridge = bridge

    # ── Internal helpers ───────────────────────────────────────────────

    def _is_valid_cell_address(self, address: str) -> bool:
        """Validate if a string is a valid cell address (e.g., A1)."""
        if not address:
            return False
        return bool(re.match(r"^[A-Za-z]+[1-9][0-9]*$", address.strip()))

    def _get_error_name(self, error_code: int) -> str:
        """Get a human-readable name for a Calc error code."""
        # Common LibreOffice Calc error codes
        # 501: Invalid character
        # 502: Invalid argument
        # 503: #NUM!
        # 504: Error in parameter list
        # 508: Error: Pair missing
        # 509: Missing operator
        # 510: Missing variable
        # 511: Missing variable
        # 512: Formula overflow
        # 513: String overflow
        # 514: Internal overflow
        # 516: Internal syntax error
        # 517: Internal syntax error
        # 518: Internal syntax error
        # 519: #VALUE!
        # 520: Internal syntax error
        # 521: #NULL!
        # 522: Circular reference
        # 523: The calculation process does not converge
        # 524: #REF!
        # 525: #NAME?
        # 526: Internal syntax error
        # 527: Internal overflow
        # 532: #DIV/0!
        # 533: Nested arrays are not supported
        # 538: Error: Array or matrix size
        # 539: Unsupported inline array content

        errors = {
            501: "Invalid character",
            502: "Invalid argument",
            503: "#NUM!",
            504: "Error in parameter list",
            508: "Error: Pair missing",
            509: "Missing operator",
            510: "Missing variable",
            511: "Missing variable",
            512: "Formula overflow",
            513: "String overflow",
            514: "Internal overflow",
            516: "Internal syntax error",
            517: "Internal syntax error",
            518: "Internal syntax error",
            519: "#VALUE!",
            520: "Internal syntax error",
            521: "#NULL!",
            522: "Circular reference",
            523: "The calculation process does not converge",
            524: "#REF!",
            525: "#NAME?",
            526: "Internal syntax error",
            527: "Internal overflow",
            532: "#DIV/0!",
            533: "Nested arrays are not supported",
            538: "Error: Array or matrix size",
            539: "Unsupported inline array content",
        }
        return errors.get(error_code, f"Unknown error ({error_code})")

    def _apply_style_properties(self, obj, bold, italic, bg_color, font_color, font_size, h_align, v_align, wrap_text, border_color):
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
            align_map = {"left": LEFT, "center": CENTER, "right": RIGHT, "justify": BLOCK, "standard": STANDARD}
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

    def _apply_borders(self, obj, color: int):
        """Apply borders to a cell or range object."""

        line = BorderLine()
        setattr(line, "Color", color)
        line.OuterLineWidth = 50  # 1/100 mm; 50 == 0.5 mm

        obj.setPropertyValue("TopBorder", line)
        obj.setPropertyValue("BottomBorder", line)
        obj.setPropertyValue("LeftBorder", line)
        obj.setPropertyValue("RightBorder", line)

    # ── Write operations ───────────────────────────────────────────────

    def safe_get_cell_value(self, sheet, cell_address):
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
            except Exception:
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
                    # Formula error
                    error_code = cell.getError()
                    raise CalcError(f"Formula error in {cell_address}: {self._get_error_name(error_code)}", code="CALC_FORMULA_ERROR", details={"address": cell_address, "error_code": error_code, "error_name": self._get_error_name(error_code)}) from e
            else:
                raise CalcError(f"Unknown cell type: {cell_type}", code="CALC_UNKNOWN_CELL_TYPE", details={"address": cell_address, "type": cell_type})

        except CalcError:
            # Re-raise our calc errors
            raise
        except Exception as e:
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
    ):
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
                logger.info("Range %s style updated.", address_or_range.upper())
            else:
                cell = self.bridge.get_cell_by_address(address_or_range)
                self._apply_style_properties(cell, bold, italic, bg_color, font_color, font_size, h_align, v_align, wrap_text, border_color)
                if number_format:
                    self._set_number_format(address_or_range, number_format)
                logger.info("Cell %s style updated.", address_or_range.upper())
        except Exception as e:
            logger.error("Style application error (%s): %s", address_or_range, str(e))
            raise ToolExecutionError(str(e)) from e

    def _set_range_style(self, range_str, bold=None, italic=None, bg_color=None, font_color=None, font_size=None, h_align=None, v_align=None, wrap_text=None, border_color=None):
        cell_range = self.bridge.resolve_range_or_address(range_str)
        self._apply_style_properties(cell_range, bold, italic, bg_color, font_color, font_size, h_align, v_align, wrap_text, border_color)

    @staticmethod
    def _resolve_document_locale(doc):
        """Return document CharLocale, or en-US when Language is empty/unusable (M2)."""
        import uno

        try:
            locale = doc.getPropertyValue("CharLocale")
            language = getattr(locale, "Language", None) or ""
            if language:
                return locale
        except Exception:
            logger.debug("CharLocale unavailable; using en-US fallback", exc_info=True)
        return uno.createUnoStruct("com.sun.star.lang.Locale", Language="en", Country="US", Variant="")

    @staticmethod
    def _apply_number_format_key(target, format_key: int) -> None:
        """Set NumberFormat from an integer registry key (detected keys, not format strings)."""
        target.setPropertyValue("NumberFormat", int(format_key))

    def _set_range_number_format(self, range_str: str, format_str: str):
        cell_range = self.bridge.resolve_range_or_address(range_str)
        doc = self.bridge.get_active_document()
        formats = doc.getNumberFormats()
        locale = self._resolve_document_locale(doc)
        format_id = formats.queryKey(format_str, locale, False)
        if format_id == -1:
            format_id = formats.addNew(format_str, locale)
        cell_range.setPropertyValue("NumberFormat", format_id)

    def _set_number_format(self, address: str, format_str: str):
        cell = self.bridge.get_cell_by_address(address)
        doc = self.bridge.get_active_document()
        formats = doc.getNumberFormats()
        locale = self._resolve_document_locale(doc)
        format_id = formats.queryKey(format_str, locale, False)
        if format_id == -1:
            format_id = formats.addNew(format_str, locale)
        cell.setPropertyValue("NumberFormat", format_id)

    # ── Range operations ───────────────────────────────────────────────

    def clear_range(self, range_str: str):
        """Clear all content in a cell range.

        Args:
            range_str: Cell range (e.g. "A1:D10").
        """
        try:
            cell_range = self.bridge.resolve_range_or_address(range_str)
            # CellFlags: VALUE=1, DATETIME=2, STRING=4, FORMULA=16 -> 23
            cell_range.clearContents(23)
            logger.info("Range %s cleared.", range_str.upper())
        except Exception as e:
            logger.error("Range clear error (%s): %s", range_str, str(e))
            raise ToolExecutionError(str(e)) from e

    def merge_cells(self, range_str: str, center: bool = True):
        """Merge a cell range.

        Args:
            range_str: Cell range to merge (e.g. "A1:D1").
            center: Centre content after merging.
        """
        try:
            cell_range = self.bridge.resolve_range_or_address(range_str)
            cell_range.merge(True)
            logger.info("Range %s merged.", range_str.upper())

            if center:
                cell_range.setPropertyValue("HoriJustify", CENTER)
                cell_range.setPropertyValue("VertJustify", V_CENTER)
        except Exception as e:
            logger.error("Cell merge error (%s): %s", range_str, str(e))
            raise ToolExecutionError(str(e)) from e

    def sort_range(self, range_str: str, sort_column: int = 0, ascending: bool = True, has_header: bool = True):
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

            import uno  # noqa: F401 – needed in UNO context

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
            logger.info("Range %s sorted %s by column %d.", range_str.upper(), direction, sort_column)
            return f"Range {range_str} sorted {direction} by column {sort_column}."
        except Exception as e:
            logger.error("Sort error (%s): %s", range_str, str(e))
            raise ToolExecutionError(str(e)) from e

    def _make_number_formatter(self, doc):
        """Attach a NumberFormatter to *doc* once per write invocation."""
        uno_ctx = get_ctx()
        smgr = uno_ctx.getServiceManager()
        formatter = smgr.createInstanceWithContext("com.sun.star.util.NumberFormatter", uno_ctx)
        formatter.attachNumberFormatsSupplier(doc)
        return formatter

    def _resolve_elapsed_format_key(self, formats, locale) -> int:
        """Built-in ``[HH]:MM:SS`` key (formatindex 43), with queryKey/addNew fallback."""
        try:
            return int(formats.getFormatIndex(43, locale))
        except Exception:
            logger.debug("getFormatIndex(43) failed; falling back to queryKey", exc_info=True)
        key = formats.queryKey("[HH]:MM:SS", locale, False)
        if key == -1:
            key = formats.addNew("[HH]:MM:SS", locale)
        return int(key)

    def _classify_write_cell(self, value, formatter, std_key, *, elapsed_format_key: int | None = None):
        """Classify one write input into data/formula/meta for the ISO write path.

        Returns ``(data_value, formula_or_empty, meta)`` where *meta* keys are:
        ``kind`` (formula|forced_text|temporal|number|text|empty),
        ``input_category``, ``detected_key``, ``restore_format`` (S29).
        """
        meta: dict = {"kind": "empty", "input_category": None, "detected_key": None, "restore_format": False}

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
                logger.debug("Duration convert failed for %r: %s", stripped, e)
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
                # NotNumericException and peers → ordinary text fallback (S4).
                if "NotNumeric" not in type(e).__name__:
                    logger.debug("ISO convert failed for %r: %s", stripped, e)
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

    def _apply_temporal_format_runs(self, sheet, start, decisions):
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

    def write_formula_range(self, range_str: str, formula_or_values):
        """Write formula(s) or value(s) to a cell range.

        ISO date/time strings matching the wire gate become Calc serials with
        category-compatible NumberFormat preservation (see docs/calc-date-time-handling.md).

        Args:
            range_str: Cell range (e.g. "A1:A10", "B2:D2").
            formula_or_values: Single formula/value for all cells, or a
                list/array of values for each cell.

        Returns:
            Summary of the operation.
        """
        try:
            # Handle empty values as a clear_range operation
            is_empty = formula_or_values is None or formula_or_values == "" or formula_or_values == [] or formula_or_values == "[]" or formula_or_values == "{}"
            if is_empty:
                self.clear_range(range_str)
                return f"Range {range_str} cleared."

            cell_range = self.bridge.resolve_range_or_address(range_str)
            addr = cell_range.getRangeAddress()
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

                        range_str = f"{self.bridge._index_to_column(start[0])}{start[1] + 1}:{self.bridge._index_to_column(end[0])}{end[1] + 1}"

                    # Pad rows to ensure uniform width, and flatten into 1D
                    flat_vals = []
                    for r in formula_or_values:
                        row_vals = list(r)
                        if num_cols > len(row_vals):
                            row_vals.extend([""] * (num_cols - len(row_vals)))
                        flat_vals.extend(row_vals[:num_cols])
                    formula_or_values = flat_vals

                if len(formula_or_values) != total_cells:
                    raise UnoObjectError(f"Array has {len(formula_or_values)} values but range has {total_cells} cells. Use a single string to fill the whole range, or an array with exactly that many values for cell-by-cell control.")
                values = formula_or_values
            else:
                values = [formula_or_values] * total_cells

            doc = self.bridge.get_active_document()
            formats = doc.getNumberFormats()
            locale = self._resolve_document_locale(doc)
            std_key = formats.getStandardIndex(locale)
            # Lazy setup: one pass over values for ISO date/time vs PT duration candidates.
            needs_formatter = False
            needs_duration = False
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

            data_array: list[list] = []
            formula_cells: list[tuple[int, int, str]] = []  # (col, row, formula)
            # Per-cell meta in row-major order matching values
            cell_metas: list[dict] = []
            counts = {"date": 0, "time": 0, "datetime": 0, "duration": 0, "text": 0, "formula": 0, "number": 0}

            cell_idx = 0
            for row in range(start[1], end[1] + 1):
                data_row: list = []
                for col in range(start[0], end[0] + 1):
                    data_val, formula, meta = self._classify_write_cell(values[cell_idx], formatter, std_key, elapsed_format_key=elapsed_format_key)
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
                        except Exception:
                            key = 0
                        if meta["restore_format"]:
                            s29_snapshots.append((col, row, key))
                        if meta["kind"] == "temporal":
                            if key not in category_cache:
                                try:
                                    props = formats.getByKey(key)
                                    category_cache[key] = _format_category_from_type(props.getPropertyValue("Type"))
                                except Exception:
                                    category_cache[key] = None
                            dest_categories[(col, row)] = category_cache[key]
                    cell_idx += 1

            cell_range.setDataArray(tuple(tuple(r) for r in data_array))

            for col, row, key in s29_snapshots:
                sheet.getCellByPosition(col, row).setPropertyValue("NumberFormat", key)

            for col, row, formula in formula_cells:
                sheet.getCellByPosition(col, row).setFormula(formula)

            format_warning = ""
            if any_temporal:
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
                                row_dec.append(("apply", meta["detected_key"]))
                        elif data_val == "" and meta["kind"] == "empty":
                            row_dec.append("empty")
                        else:
                            row_dec.append(None)
                        cell_idx += 1
                    decisions.append(row_dec)
                try:
                    self._apply_temporal_format_runs(sheet, start, decisions)
                except Exception:
                    logger.exception("Date/time format pass failed for range %s", range_str)
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
            msg = f"Range {range_str} filled with {n_vals} {values_word}{detail}{format_warning}."
            logger.info("%s", msg)
            return msg
        except Exception as e:
            logger.error("Range formula write error (%s): %s", range_str, str(e))
            raise ToolExecutionError(str(e)) from e

    # ── Chart ──────────────────────────────────────────────────────────

    # ── Structure operations ───────────────────────────────────────────

    def delete_rows(self, row_num: int, count: int = 1):
        """Delete rows starting at *row_num* (1-based)."""
        try:
            sheet = self.bridge.get_active_sheet()
            rows = sheet.getRows()
            rows.removeByIndex(row_num - 1, count)
            logger.info("%d row(s) deleted starting from row %d.", count, row_num)
            return f"{count} row(s) deleted starting from row {row_num}."
        except Exception as e:
            logger.error("Row deletion error: %s", str(e))
            raise ToolExecutionError(str(e)) from e

    def delete_columns(self, col_letter: str, count: int = 1):
        """Delete columns starting at *col_letter*."""
        try:
            sheet = self.bridge.get_active_sheet()
            columns = sheet.getColumns()
            col_index = self.bridge._column_to_index(col_letter.upper())
            columns.removeByIndex(col_index, count)
            logger.info("%d column(s) deleted starting from column %s.", count, col_letter.upper())
            return f"{count} column(s) deleted starting from column {col_letter.upper()}."
        except Exception as e:
            logger.error("Column deletion error: %s", str(e))
            raise ToolExecutionError(str(e)) from e

    def delete_structure(self, structure_type: str, start, count: int = 1):
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
            raise UnoObjectError(f"Invalid structure_type: {structure_type}. Must be 'rows' or 'columns'.")
