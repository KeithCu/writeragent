"""Cell manipulator — writing data and formatting LibreOffice Calc cells.

Ported from core/calc_manipulator.py for the plugin framework.
UNO imports are deferred to method bodies.
"""

import csv
import io
import json
import logging
import re

from plugin.modules.calc.address_utils import parse_address

logger = logging.getLogger("localwriter.calc")


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
                if char == ';' and not in_quotes:
                    normalized_list.append(',')
                else:
                    normalized_list.append(char)
                if char == '\\' and not escaped:
                    escaped = True
                else:
                    escaped = False

            normalized = "".join(normalized_list)
            data = json.loads(normalized)
            if isinstance(data, list):
                flat = []
                for item in data:
                    if isinstance(item, list):
                        flat.extend(item)
                    else:
                        flat.append(item)
                return flat
        except (json.JSONDecodeError, TypeError):
            pass

    # Case 2: Raw semicolon-separated string e.g. "Name;Age;Country"
    # Only if it is not a formula (starting with =) and not a single value.
    if ";" in s and not s_strip.startswith("="):
        try:
            reader = csv.reader(
                io.StringIO(s), delimiter=";", skipinitialspace=True,
            )
            rows = list(reader)
            if rows:
                return [val.strip() for val in rows[0]]
        except Exception:
            pass

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

    def _get_cell(self, address: str):
        """Return the cell object for *address*."""
        col, row = parse_address(address)
        sheet = self.bridge.get_active_sheet()
        return self.bridge.get_cell(sheet, col, row)

    def _apply_style_properties(
        self, obj, bold, italic, bg_color, font_color, font_size,
        h_align, v_align, wrap_text, border_color,
    ):
        """Apply common style properties to a cell or range object."""
        if bold is not None:
            from com.sun.star.awt.FontWeight import BOLD, NORMAL
            obj.setPropertyValue("CharWeight", BOLD if bold else NORMAL)

        if italic is not None:
            from com.sun.star.awt.FontSlant import ITALIC, NONE
            obj.setPropertyValue("CharPosture", ITALIC if italic else NONE)

        if bg_color is not None:
            obj.setPropertyValue("CellBackColor", bg_color)

        if font_color is not None:
            obj.setPropertyValue("CharColor", font_color)

        if font_size is not None:
            obj.setPropertyValue("CharHeight", font_size)

        if h_align is not None:
            from com.sun.star.table.CellHoriJustify import (
                LEFT, CENTER, RIGHT, BLOCK, STANDARD,
            )
            align_map = {
                "left": LEFT, "center": CENTER, "right": RIGHT,
                "justify": BLOCK, "standard": STANDARD,
            }
            if h_align.lower() in align_map:
                obj.setPropertyValue("HoriJustify", align_map[h_align.lower()])

        if v_align is not None:
            from com.sun.star.table.CellVertJustify import (
                TOP, CENTER, BOTTOM, STANDARD,
            )
            align_map = {
                "top": TOP, "center": CENTER, "bottom": BOTTOM,
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
        from com.sun.star.table import BorderLine

        line = BorderLine()
        line.Color = color
        line.OuterLineWidth = 50  # 1/100 mm; 50 == 0.5 mm

        obj.setPropertyValue("TopBorder", line)
        obj.setPropertyValue("BottomBorder", line)
        obj.setPropertyValue("LeftBorder", line)
        obj.setPropertyValue("RightBorder", line)

    # ── Write operations ───────────────────────────────────────────────

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
            raise

    # ── Style operations ───────────────────────────────────────────────

    def set_cell_style(
        self,
        address_or_range: str,
        bold: bool = None,
        italic: bool = None,
        bg_color: int = None,
        font_color: int = None,
        font_size: float = None,
        h_align: str = None,
        v_align: str = None,
        wrap_text: bool = None,
        border_color: int = None,
        number_format: str = None,
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
                    bold=bold, italic=italic, bg_color=bg_color,
                    font_color=font_color, font_size=font_size,
                    h_align=h_align, v_align=v_align,
                    wrap_text=wrap_text, border_color=border_color,
                )
                if number_format:
                    self._set_range_number_format(address_or_range, number_format)
                logger.info("Range %s style updated.", address_or_range.upper())
            else:
                cell = self._get_cell(address_or_range)
                self._apply_style_properties(
                    cell, bold, italic, bg_color, font_color, font_size,
                    h_align, v_align, wrap_text, border_color,
                )
                if number_format:
                    self._set_number_format(address_or_range, number_format)
                logger.info("Cell %s style updated.", address_or_range.upper())
        except Exception as e:
            logger.error("Style application error (%s): %s", address_or_range, str(e))
            raise

    def _set_range_style(
        self, range_str, bold=None, italic=None, bg_color=None,
        font_color=None, font_size=None, h_align=None, v_align=None,
        wrap_text=None, border_color=None,
    ):
        sheet = self.bridge.get_active_sheet()
        cell_range = self.bridge.get_cell_range(sheet, range_str)
        self._apply_style_properties(
            cell_range, bold, italic, bg_color, font_color, font_size,
            h_align, v_align, wrap_text, border_color,
        )

    def _set_range_number_format(self, range_str: str, format_str: str):
        sheet = self.bridge.get_active_sheet()
        start, end = self.bridge.parse_range_string(range_str)
        doc = self.bridge.get_active_document()
        formats = doc.getNumberFormats()
        locale = doc.getPropertyValue("CharLocale")
        format_id = formats.queryKey(format_str, locale, False)
        if format_id == -1:
            format_id = formats.addNew(format_str, locale)
        for row in range(start[1], end[1] + 1):
            for col in range(start[0], end[0] + 1):
                cell = sheet.getCellByPosition(col, row)
                cell.setPropertyValue("NumberFormat", format_id)

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
            raise

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
                from com.sun.star.table.CellHoriJustify import CENTER
                from com.sun.star.table.CellVertJustify import CENTER as V_CENTER
                cell_range.setPropertyValue("HoriJustify", CENTER)
                cell_range.setPropertyValue("VertJustify", V_CENTER)
        except Exception as e:
            logger.error("Cell merge error (%s): %s", range_str, str(e))
            raise

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
            from com.sun.star.table import TableSortField

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
                range_str.upper(), direction, sort_column,
            )
            return f"Range {range_str} sorted {direction} by column {sort_column}."
        except Exception as e:
            logger.error("Sort error (%s): %s", range_str, str(e))
            raise

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
                if len(formula_or_values) != total_cells:
                    raise ValueError(
                        f"Array length {len(formula_or_values)} doesn't match "
                        f"range size {total_cells}"
                    )
                values = formula_or_values
            else:
                values = [formula_or_values] * total_cells

            cell_idx = 0
            for row in range(start[1], end[1] + 1):
                for col in range(start[0], end[0] + 1):
                    cell = sheet.getCellByPosition(col, row)
                    value = values[cell_idx]

                    if isinstance(value, str):
                        if value.startswith("="):
                            cell.setFormula(value)
                        else:
                            try:
                                num = float(value)
                                cell.setValue(num)
                            except ValueError:
                                cell.setString(value)
                    elif isinstance(value, (int, float)):
                        cell.setValue(value)
                    else:
                        cell.setString(str(value))

                    cell_idx += 1

            logger.info(
                "Range %s filled with %d values.", range_str.upper(), len(values),
            )
            return f"Range {range_str} filled with {len(values)} values."
        except Exception as e:
            logger.error("Range formula write error (%s): %s", range_str, str(e))
            raise

    def import_csv_from_string(self, csv_data: str, target_cell: str = "A1"):
        """Import CSV data into the sheet starting at *target_cell*.

        Automatically detects comma vs semicolon delimiter.

        Args:
            csv_data: CSV content as a string.
            target_cell: Starting cell (e.g. "A1").

        Returns:
            Summary string.
        """
        try:
            delimiter = ","
            first_line = csv_data.split('\n')[0] if csv_data else ""
            if ";" in first_line and "," not in first_line:
                delimiter = ";"

            col_start, row_start = parse_address(target_cell)
            reader = csv.reader(io.StringIO(csv_data), delimiter=delimiter)
            rows = list(reader)
            if not rows:
                return "No data to import."

            sheet = self.bridge.get_active_sheet()
            total_rows = len(rows)
            total_cols = max(len(r) for r in rows) if rows else 0

            for r_idx, row_data in enumerate(rows):
                for c_idx, cell_value in enumerate(row_data):
                    col = col_start + c_idx
                    row = row_start + r_idx
                    cell = sheet.getCellByPosition(col, row)
                    try:
                        num = float(cell_value)
                        cell.setValue(num)
                    except ValueError:
                        cell.setString(cell_value)

            range_imported = (
                f"{target_cell}:"
                f"{self.bridge._index_to_column(col_start + total_cols - 1)}"
                f"{row_start + total_rows}"
            )
            logger.info("CSV imported to range %s.", range_imported)
            return f"Imported {total_rows} rows, {total_cols} cols to {range_imported}."
        except Exception as e:
            logger.error("CSV import error: %s", str(e))
            raise

    # ── Chart ──────────────────────────────────────────────────────────

    def create_chart(
        self,
        data_range: str,
        chart_type: str,
        title: str = None,
        position: str = None,
        has_header: bool = True,
    ):
        """Create a chart from data.

        Args:
            data_range: Range for chart data (e.g. "A1:B10").
            chart_type: Chart type (bar, line, pie, scatter, column).
            title: Chart title.
            position: Cell where chart is placed (e.g. "E1").
            has_header: Whether first row/column is a label.

        Returns:
            Description string.
        """
        try:
            sheet = self.bridge.get_active_sheet()
            cell_range = self.bridge.get_cell_range(sheet, data_range)
            range_address = cell_range.getRangeAddress()

            if position:
                pos_cell = self._get_cell(position)
                pos_x = pos_cell.Position.X
                pos_y = pos_cell.Position.Y
            else:
                pos_x = 10000
                pos_y = 1000

            from com.sun.star.awt import Rectangle

            rect = Rectangle()
            rect.X = pos_x
            rect.Y = pos_y
            rect.Width = 12000
            rect.Height = 8000

            charts = sheet.getCharts()
            chart_name = f"Chart_{len(charts)}"

            type_map = {
                "bar": "com.sun.star.chart.BarDiagram",
                "column": "com.sun.star.chart.BarDiagram",
                "line": "com.sun.star.chart.LineDiagram",
                "pie": "com.sun.star.chart.PieDiagram",
                "scatter": "com.sun.star.chart.XYDiagram",
            }
            chart_service = type_map.get(chart_type, "com.sun.star.chart.BarDiagram")

            charts.addNewByName(
                chart_name, rect, (range_address,), has_header, has_header,
            )

            chart = charts.getByName(chart_name).getEmbeddedObject()
            diagram = chart.createInstance(chart_service)
            chart.setDiagram(diagram)

            if chart_type == "bar" and hasattr(diagram, "Vertical"):
                diagram.Vertical = True
            elif chart_type == "column" and hasattr(diagram, "Vertical"):
                diagram.Vertical = False

            if title:
                chart.setPropertyValue("HasMainTitle", True)
                chart_title = chart.getTitle()
                chart_title.setPropertyValue("String", title)

            logger.info("Chart created: %s (%s)", chart_name, chart_type)
            return f"{chart_type} type chart created."
        except Exception as e:
            logger.error("Chart creation error: %s", str(e))
            raise

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
            raise

    def delete_columns(self, col_letter: str, count: int = 1):
        """Delete columns starting at *col_letter*."""
        try:
            sheet = self.bridge.get_active_sheet()
            columns = sheet.getColumns()
            col_index = self.bridge._column_to_index(col_letter.upper())
            columns.removeByIndex(col_index, count)
            logger.info(
                "%d column(s) deleted starting from column %s.",
                count, col_letter.upper(),
            )
            return f"{count} column(s) deleted starting from column {col_letter.upper()}."
        except Exception as e:
            logger.error("Column deletion error: %s", str(e))
            raise

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
            raise ValueError(
                f"Invalid structure_type: {structure_type}. "
                f"Must be 'rows' or 'columns'."
            )

    # ── Sheet management ───────────────────────────────────────────────

    def list_sheets(self):
        """List all sheet names in the workbook.

        Returns:
            List of sheet name strings.
        """
        try:
            doc = self.bridge.get_active_document()
            sheets = doc.getSheets()
            sheet_names = []
            for i in range(sheets.getCount()):
                sheet = sheets.getByIndex(i)
                sheet_names.append(sheet.getName())
            logger.info("Sheets listed: %s", sheet_names)
            return sheet_names
        except Exception as e:
            logger.error("Sheet listing error: %s", str(e))
            raise

    def switch_sheet(self, sheet_name: str):
        """Switch to the specified sheet.

        Args:
            sheet_name: Name of the sheet to activate.

        Returns:
            Confirmation string.
        """
        try:
            doc = self.bridge.get_active_document()
            sheets = doc.getSheets()
            if not sheets.hasByName(sheet_name):
                raise ValueError(f"No sheet found named '{sheet_name}'.")
            sheet = sheets.getByName(sheet_name)
            controller = doc.getCurrentController()
            controller.setActiveSheet(sheet)
            logger.info("Switched to sheet: %s", sheet_name)
            return f"Switched to sheet '{sheet_name}'."
        except Exception as e:
            logger.error("Sheet switch error (%s): %s", sheet_name, str(e))
            raise

    def create_sheet(self, sheet_name: str, position: int = None):
        """Create a new sheet.

        Args:
            sheet_name: New sheet name.
            position: 0-based position (appended to end if None).

        Returns:
            Confirmation string.
        """
        try:
            doc = self.bridge.get_active_document()
            sheets = doc.getSheets()
            if position is None:
                position = sheets.getCount()
            sheets.insertNewByName(sheet_name, position)
            logger.info("New sheet created: %s (position: %d)", sheet_name, position)
            return f"New sheet named '{sheet_name}' created."
        except Exception as e:
            logger.error("Sheet creation error (%s): %s", sheet_name, str(e))
            raise
