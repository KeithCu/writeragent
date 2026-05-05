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


from plugin.framework.errors import ToolExecutionError, UnoObjectError, safe_json_loads
from plugin.modules.calc import CalcError
from plugin.modules.calc.address_utils import parse_address

logger = logging.getLogger("writeragent.calc")


# ── Helper ─────────────────────────────────────────────────────────────


def _parse_formula_or_values_string(s: str):
    """Parse *formula_or_values* when it arrives as a JSON string or as a
    raw semicolon-separated string.

    The AI often sends formula_or_values as a JSON-encoded string (e.g.
    ``'["Name"; "Category"; "Value"]'``) or as a raw string like
    ``'Name;Category;Value'``.  Without this, write_formula_range would
    write the whole string as one value per cell.  We normalise
    LibreOffice-style semicolon separators and return a flat list.

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
                reader = csv.reader(
                    io.StringIO(s),
                    delimiter=delimiter,
                    skipinitialspace=True,
                )
                rows = list(reader)
                if rows:
                    if len(rows) == 1:
                        # 1D row
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

    def _get_cell(self, address: str):
        """Return the cell object for *address*."""
        col, row = parse_address(address)
        sheet = self.bridge.get_active_sheet()
        return self.bridge.get_cell(sheet, col, row)

    def _apply_style_properties(
        self,
        obj,
        bold,
        italic,
        bg_color,
        font_color,
        font_size,
        h_align,
        v_align,
        wrap_text,
        border_color,
    ):
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
            align_map = {
                "left": LEFT,
                "center": CENTER,
                "right": RIGHT,
                "justify": BLOCK,
                "standard": STANDARD,
            }
            if h_align.lower() in align_map:
                obj.setPropertyValue("HoriJustify", align_map[h_align.lower()])

        if v_align is not None:
            align_map = {
                "top": TOP,
                "center": CENTER,
                "bottom": BOTTOM,
                "standard": STANDARD,
            }
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

            import sys

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
                    raise CalcError(
                        f"Formula error in {cell_address}: {self._get_error_name(error_code)}",
                        code="CALC_FORMULA_ERROR",
                        details={"address": cell_address, "error_code": error_code, "error_name": self._get_error_name(error_code)},
                    ) from e
            else:
                raise CalcError(f"Unknown cell type: {cell_type}", code="CALC_UNKNOWN_CELL_TYPE", details={"address": cell_address, "type": cell_type})

        except CalcError:
            # Re-raise our calc errors
            raise
        except Exception as e:
            # Wrap other exceptions
            raise CalcError(f"Failed to get cell value: {str(e)}", code="CALC_CELL_VALUE_ERROR", details={"address": cell_address, "original_error": str(e), "error_type": type(e).__name__}) from e

    def write_formula(self, address: str, formula: str) -> str:
        """Write formula, text, or number to a cell.

        If the value starts with ``=`` it is written as a formula.  If it
        can be converted to a number it is written as a numeric value.
        Otherwise it is written as text.

        Args:
            address: Cell address (e.g. "A1").
            formula: Content to write (e.g. "=SUM(A1:A10)", "Header", "42").

        Returns:
            Description of the written value.
        """
        try:
            cell = self._get_cell(address)

            if formula.startswith("="):
                cell.setFormula(formula)
                logger.info("Cell %s <- formula '%s' written.", address.upper(), formula)
                return f"Formula written to cell {address}: {formula}"
            else:
                try:
                    num = float(formula)
                    cell.setValue(num)
                    logger.info("Cell %s <- number %s written.", address.upper(), formula)
                    return f"Number written to cell {address}: {formula}"
                except ValueError:
                    cell.setString(formula)
                    logger.info("Cell %s <- text '%s' written.", address.upper(), formula)
                    return f"Text written to cell {address}: {formula}"
        except Exception as e:
            logger.error("Formula writing error (%s): %s", address, str(e))
            raise ToolExecutionError(str(e)) from e

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
                self._set_range_style(
                    address_or_range,
                    bold=bold,
                    italic=italic,
                    bg_color=bg_color,
                    font_color=font_color,
                    font_size=font_size,
                    h_align=h_align,
                    v_align=v_align,
                    wrap_text=wrap_text,
                    border_color=border_color,
                )
                if number_format:
                    self._set_range_number_format(address_or_range, number_format)
                logger.info("Range %s style updated.", address_or_range.upper())
            else:
                cell = self._get_cell(address_or_range)
                self._apply_style_properties(
                    cell,
                    bold,
                    italic,
                    bg_color,
                    font_color,
                    font_size,
                    h_align,
                    v_align,
                    wrap_text,
                    border_color,
                )
                if number_format:
                    self._set_number_format(address_or_range, number_format)
                logger.info("Cell %s style updated.", address_or_range.upper())
        except Exception as e:
            logger.error("Style application error (%s): %s", address_or_range, str(e))
            raise ToolExecutionError(str(e)) from e

    def _set_range_style(
        self,
        range_str,
        bold=None,
        italic=None,
        bg_color=None,
        font_color=None,
        font_size=None,
        h_align=None,
        v_align=None,
        wrap_text=None,
        border_color=None,
    ):
        sheet = self.bridge.get_active_sheet()
        cell_range = self.bridge.get_cell_range(sheet, range_str)
        self._apply_style_properties(
            cell_range,
            bold,
            italic,
            bg_color,
            font_color,
            font_size,
            h_align,
            v_align,
            wrap_text,
            border_color,
        )

    def _set_range_number_format(self, range_str: str, format_str: str):
        sheet = self.bridge.get_active_sheet()
        cell_range = self.bridge.get_cell_range(sheet, range_str)
        doc = self.bridge.get_active_document()
        formats = doc.getNumberFormats()
        locale = doc.getPropertyValue("CharLocale")
        format_id = formats.queryKey(format_str, locale, False)
        if format_id == -1:
            format_id = formats.addNew(format_str, locale)
        cell_range.setPropertyValue("NumberFormat", format_id)

    def _set_number_format(self, address: str, format_str: str):
        cell = self._get_cell(address)
        doc = self.bridge.get_active_document()
        formats = doc.getNumberFormats()
        locale = doc.getPropertyValue("CharLocale")
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
            sheet = self.bridge.get_active_sheet()
            cell_range = self.bridge.get_cell_range(sheet, range_str)
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
            sheet = self.bridge.get_active_sheet()
            cell_range = self.bridge.get_cell_range(sheet, range_str)
            cell_range.merge(True)
            logger.info("Range %s merged.", range_str.upper())

            if center:
                cell_range.setPropertyValue("HoriJustify", CENTER)
                cell_range.setPropertyValue("VertJustify", V_CENTER)
        except Exception as e:
            logger.error("Cell merge error (%s): %s", range_str, str(e))
            raise ToolExecutionError(str(e)) from e

    def sort_range(
        self,
        range_str: str,
        sort_column: int = 0,
        ascending: bool = True,
        has_header: bool = True,
    ):
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
            sheet = self.bridge.get_active_sheet()
            cell_range = self.bridge.get_cell_range(sheet, range_str)

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
            logger.info(
                "Range %s sorted %s by column %d.",
                range_str.upper(),
                direction,
                sort_column,
            )
            return f"Range {range_str} sorted {direction} by column {sort_column}."
        except Exception as e:
            logger.error("Sort error (%s): %s", range_str, str(e))
            raise ToolExecutionError(str(e)) from e

    def write_formula_range(self, range_str: str, formula_or_values):
        """Write formula(s) or value(s) to a cell range.

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

            sheet = self.bridge.get_active_sheet()
            start, end = self.bridge.parse_range_string(range_str)

            num_rows = end[1] - start[1] + 1
            num_cols = end[0] - start[0] + 1
            total_cells = num_rows * num_cols

            # Normalise string-as-array from AI callers.
            if isinstance(formula_or_values, str):
                parsed = _parse_formula_or_values_string(formula_or_values)
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

                        range_str = f"{self.bridge._index_to_column(start[0])}{start[1]}:{self.bridge._index_to_column(end[0])}{end[1]}"

                    # Pad rows to ensure uniform width, and flatten into 1D
                    flat_vals = []
                    for r in formula_or_values:
                        row_vals = list(r)
                        if num_cols > len(row_vals):
                            row_vals.extend([""] * (num_cols - len(row_vals)))
                        flat_vals.extend(row_vals[:num_cols])
                    formula_or_values = flat_vals

                if len(formula_or_values) != total_cells:
                    raise UnoObjectError(
                        f"Array has {len(formula_or_values)} values but range has "
                        f"{total_cells} cells. Use a single string to fill the whole "
                        "range, or an array with exactly that many values for "
                        "cell-by-cell control."
                    )
                values = formula_or_values
            else:
                values = [formula_or_values] * total_cells

            data_array = []
            formula_array = []
            has_formulas = False

            cell_idx = 0
            for row in range(start[1], end[1] + 1):
                data_row: list[str | int | float] = []
                formula_row = []
                for col in range(start[0], end[0] + 1):
                    value = values[cell_idx]

                    if isinstance(value, str):
                        if value.startswith("="):
                            data_row.append("")
                            formula_row.append(value)
                            has_formulas = True
                        else:
                            try:
                                num = float(value)
                                data_row.append(num)
                                formula_row.append("")
                            except ValueError:
                                data_row.append(value)
                                formula_row.append("")
                    elif isinstance(value, (int, float)):
                        data_row.append(value)
                        formula_row.append("")
                    else:
                        data_row.append(str(value))
                        formula_row.append("")

                    cell_idx += 1
                data_array.append(tuple(data_row))
                formula_array.append(tuple(formula_row))

            cell_range = sheet.getCellRangeByPosition(start[0], start[1], end[0], end[1])

            if not has_formulas:
                cell_range.setDataArray(tuple(data_array))
            else:
                string_formulas = []
                cell_idx = 0
                for row in range(start[1], end[1] + 1):
                    row_data = []
                    for col in range(start[0], end[0] + 1):
                        value = values[cell_idx]
                        if value is None:
                            row_data.append("")
                        else:
                            row_data.append(str(value))
                        cell_idx += 1
                    string_formulas.append(tuple(row_data))
                cell_range.setFormulaArray(tuple(string_formulas))

            logger.info(
                "Range %s filled with %d values.",
                range_str.upper(),
                len(values),
            )
            return f"Range {range_str} filled with {len(values)} values."
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
            logger.info(
                "%d column(s) deleted starting from column %s.",
                count,
                col_letter.upper(),
            )
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
