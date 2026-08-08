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
"""Cell inspector — reads detailed information from LibreOffice Calc cells.

Ported from core/calc_inspector.py for the plugin framework.
"""

import datetime
import logging
import re

from plugin.calc.datetime_wire import is_elapsed_format_string, iso_duration_from_serial
from plugin.framework.errors import ToolExecutionError

try:
    from com.sun.star.table.CellContentType import EMPTY, VALUE, TEXT, FORMULA

    UNO_AVAILABLE = True
except ImportError:
    from typing import Any, cast

    EMPTY, VALUE, TEXT, FORMULA = cast("Any", 0), cast("Any", 1), cast("Any", 2), cast("Any", 3)
    UNO_AVAILABLE = False

logger = logging.getLogger("writeragent.calc")

_FORMULA_REF_RE = re.compile(r"\$?([A-Z]+)\$?(\d+)")
_CELL_FLAG_DATETIME = 2
_NUMBER_FORMAT_DEFINED = 1
_NUMBER_FORMAT_DATE = 2
_NUMBER_FORMAT_TIME = 4


def _format_category_from_type(format_type) -> str | None:
    """Map a UNO NumberFormat.Type to the date/time category exposed to tools."""
    try:
        base_type = int(format_type) & ~_NUMBER_FORMAT_DEFINED
    except (TypeError, ValueError):
        return None
    if base_type == (_NUMBER_FORMAT_DATE | _NUMBER_FORMAT_TIME):
        return "datetime"
    if base_type == _NUMBER_FORMAT_DATE:
        return "date"
    if base_type == _NUMBER_FORMAT_TIME:
        return "time"
    return None


def _iso8601_from_serial(value: float, category: str, null_date) -> str:
    """Convert a Calc day serial to ISO 8601 using the document's configured epoch."""
    base = datetime.datetime(int(null_date.Year), int(null_date.Month), int(null_date.Day))
    # Round to whole seconds so IEEE float noise does not leak as microseconds.
    converted = base + datetime.timedelta(seconds=round(float(value) * 86400.0))
    if category == "date":
        return converted.date().isoformat()
    if category == "time":
        # Clock times only. Elapsed formats (serials >= 1.0 under [HH]:…) are classified
        # as "duration" before this helper and emit via iso_duration_from_serial.
        # Calc TIME vs datetime edit/display heuristic:
        # https://lists.freedesktop.org/archives/libreoffice/2018-July/080606.html
        return converted.time().isoformat()
    if category == "datetime":
        return converted.isoformat(timespec="seconds")
    raise ValueError(f"Unsupported date/time format category: {category}")


class CellInspector:
    """Examines cell contents and properties."""

    def __init__(self, bridge):
        """
        Args:
            bridge: CalcBridge instance.
        """
        self.bridge = bridge

    # ── Internal helpers ───────────────────────────────────────────────

    @staticmethod
    def _cell_type_name(cell_type) -> str:
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

    @staticmethod
    def _safe_prop(cell, name, default=None):
        try:
            return cell.getPropertyValue(name)
        except Exception as e:
            logger.debug("_safe_prop exception for %s: %s", name, e)
            return default

    def _format_category(self, format_key, formats, cache: dict[int, str | None]) -> str | None:
        """Resolve one number-format key, caching across equal-format ranges.

        Elapsed formats report Type TIME but use bracketed units (``[HH]``, …).
        Those enrich as ``duration`` (``PT30H``) so values >= 1.0 are not truncated.
        """
        try:
            key = int(format_key)
        except (TypeError, ValueError):
            return None
        if key not in cache:
            props = formats.getByKey(key)
            category = _format_category_from_type(props.getPropertyValue("Type"))
            # DURATION bit never appears on elapsed formats; FormatString is the signal.
            if category == "time" and is_elapsed_format_string(props.getPropertyValue("FormatString")):
                category = "duration"
            cache[key] = category
        return cache[key]

    def _enrich_cell_format(self, info: dict, cell) -> None:
        """Rewrite LLM-facing date/time/duration cells to ISO in ``value``.

        Only used when ``include_format_info=True`` (tool path). Internal
        callers that need raw Calc serials must keep ``include_format_info=False``.
        """
        value = info.get("value")
        if not isinstance(value, (int, float)):
            return
        doc = self.bridge.get_active_document()
        formats = doc.getNumberFormats()
        category = self._format_category(self._safe_prop(cell, "NumberFormat"), formats, {})
        if category is None:
            return
        if category == "duration":
            info["value"] = iso_duration_from_serial(float(value))
        else:
            null_date = doc.getNumberFormatSettings().getPropertyValue("NullDate")
            info["value"] = _iso8601_from_serial(float(value), category, null_date)
        info["type"] = category
        info["format_category"] = category

    def _range_format_rows(self, cell_range, formula_array) -> tuple[dict[int, list[tuple[int, int, str]]], object | None]:
        """Return date/time column spans by row, or an empty map for the common fast path."""
        # Cheap native preflight: constant date/time values. Skip the Python formula walk
        # when these exist — getUniqueCellFormatRanges still covers date-formatted formulas.
        # queryContentCells usually returns an empty XSheetCellRanges, not None; treat any
        # UNO failure like "no date constants" and continue to the formula / format-group path.
        date_addresses: tuple = ()
        try:
            date_cells = cell_range.queryContentCells(_CELL_FLAG_DATETIME)
            if date_cells is not None:
                date_addresses = tuple(date_cells.getRangeAddresses() or ())
        except Exception:
            logger.debug("queryContentCells(DATETIME) preflight failed", exc_info=True)
        if not date_addresses:
            has_formula = any(isinstance(formula, str) and formula.startswith("=") for row in formula_array for formula in row)
            if not has_formula:
                return {}, None

        doc = self.bridge.get_active_document()
        formats = doc.getNumberFormats()
        cache: dict[int, str | None] = {}
        rows: dict[int, list[tuple[int, int, str]]] = {}

        # Calc dates are numeric VALUE cells, so the normal content type loses their
        # meaning. Grouping equal formats keeps this classification out of the per-cell
        # UNO loop while also covering formulas whose evaluated result is date-formatted.
        format_groups = cell_range.getUniqueCellFormatRanges()
        for group_idx in range(format_groups.getCount()):
            group = format_groups.getByIndex(group_idx)
            if group.getCount() == 0:
                continue
            representative = group.getByIndex(0)
            category = self._format_category(self._safe_prop(representative, "NumberFormat"), formats, cache)
            if category is None:
                continue
            for address in group.getRangeAddresses():
                for row in range(address.StartRow, address.EndRow + 1):
                    rows.setdefault(row, []).append((address.StartColumn, address.EndColumn, category))

        if not rows:
            return {}, None
        null_date = doc.getNumberFormatSettings().getPropertyValue("NullDate")
        return rows, null_date

    @staticmethod
    def _category_for_position(format_rows: dict[int, list[tuple[int, int, str]]], row: int, col: int) -> str | None:
        for start_col, end_col, category in format_rows.get(row, ()):
            if start_col <= col <= end_col:
                return category
        return None

    # ── Public API ─────────────────────────────────────────────────────

    def read_cell(self, address: str, *, include_format_info: bool = False) -> dict:
        """Read basic cell information.

        Args:
            address: Cell address (e.g. "A1").

        Returns:
            dict with keys: address, value, formula, type.
        """
        try:
            cell = self.bridge.resolve_range_or_address(address)
            if hasattr(cell, "getRangeAddress"):
                addr = cell.getRangeAddress()
                if addr.StartColumn != addr.EndColumn or addr.StartRow != addr.EndRow:
                    raise ValueError(f"Address '{address}' resolved to a cell range, not a single cell.")
                if hasattr(cell, "getCellByPosition") and not hasattr(cell, "getType"):
                    cell = cell.getCellByPosition(0, 0)
            cell_type = cell.getType()

            if cell_type == EMPTY:
                value = None
            elif cell_type == VALUE:
                value = cell.getValue()
            elif cell_type == TEXT:
                value = cell.getString()
            elif cell_type == FORMULA:
                value = cell.getValue() if cell.getValue() != 0 else cell.getString()
            else:
                value = cell.getString()

            formula = cell.getFormula() if cell_type == FORMULA else None

            info = {"address": address.upper(), "value": value, "formula": formula, "type": self._cell_type_name(cell_type)}
            if include_format_info:
                try:
                    self._enrich_cell_format(info, cell)
                except Exception:
                    # Format metadata must never turn a previously valid core read into an error.
                    logger.exception("Date/time format enrichment failed for cell %s; returning the raw value", address)
            return info
        except Exception as e:
            logger.error("Cell reading error (%s): %s", address, str(e))
            raise ToolExecutionError(str(e)) from e

    def get_cell_details(self, address: str) -> dict:
        """Return all detailed cell information.

        Args:
            address: Cell address (e.g. "A1").

        Returns:
            dict with keys: address, value, formula, formula_local, type,
            background_color, number_format, font_color, font_size, bold,
            italic, h_align, v_align, wrap_text.
        """
        try:
            cell = self.bridge.resolve_range_or_address(address)
            if hasattr(cell, "getRangeAddress"):
                addr = cell.getRangeAddress()
                if addr.StartColumn != addr.EndColumn or addr.StartRow != addr.EndRow:
                    raise ValueError(f"Address '{address}' resolved to a cell range, not a single cell.")
                if hasattr(cell, "getCellByPosition") and not hasattr(cell, "getType"):
                    cell = cell.getCellByPosition(0, 0)
            cell_type = cell.getType()

            if cell_type == EMPTY:
                value = None
            elif cell_type == VALUE:
                value = cell.getValue()
            elif cell_type == TEXT:
                value = cell.getString()
            elif cell_type == FORMULA:
                value = cell.getValue() if cell.getValue() != 0 else cell.getString()
            else:
                value = cell.getString()

            return {
                "address": address.upper(),
                "value": value,
                "formula": cell.getFormula(),
                "formula_local": self._safe_prop(cell, "FormulaLocal"),
                "type": self._cell_type_name(cell_type),
                "background_color": self._safe_prop(cell, "CellBackColor"),
                "number_format": self._safe_prop(cell, "NumberFormat"),
                "font_color": self._safe_prop(cell, "CharColor"),
                "font_size": self._safe_prop(cell, "CharHeight"),
                "bold": self._safe_prop(cell, "CharWeight"),
                "italic": self._safe_prop(cell, "CharPosture"),
                "h_align": self._safe_prop(cell, "HoriJustify"),
                "v_align": self._safe_prop(cell, "VertJustify"),
                "wrap_text": self._safe_prop(cell, "IsTextWrapped"),
            }
        except Exception as e:
            logger.error("Cell detailed reading error (%s): %s", address, str(e))
            raise ToolExecutionError(str(e)) from e

    def read_range(self, range_name: str, *, include_format_info: bool = False) -> list[list[dict]]:
        """Read values and formulas in a cell range.

        Args:
            range_name: Cell range (e.g. "A1:D10", "B2").

        Returns:
            2D list of dicts, each with keys: address, value, formula, type.
            When ``include_format_info`` is True, date/time-formatted numeric
            cells use an ISO string as ``value``, ``type`` of date/time/datetime,
            and ``format_category``; elapsed formats use ``PTnHnMnS`` with
            ``type`` / ``format_category`` ``duration``. Otherwise ``value``
            stays the raw serial.
        """
        try:
            cell_range = self.bridge.resolve_range_or_address(range_name)

            if hasattr(cell_range, "getRangeAddress"):
                addr = cell_range.getRangeAddress()
                if addr.StartColumn == addr.EndColumn and addr.StartRow == addr.EndRow:
                    cell_info = self.read_cell(range_name, include_format_info=include_format_info)
                    return [[cell_info]]
            else:
                cell_info = self.read_cell(range_name, include_format_info=include_format_info)
                return [[cell_info]]

            data_array = cell_range.getDataArray()
            formula_array = cell_range.getFormulaArray()
            format_rows: dict[int, list[tuple[int, int, str]]] = {}
            null_date = None
            if include_format_info:
                try:
                    format_rows, null_date = self._range_format_rows(cell_range, formula_array)
                except Exception:
                    # Some older/embedded Calc builds may not expose format-range queries.
                    # Preserve the old raw response instead of failing read_cell_range.
                    logger.exception("Date/time format enrichment failed for range %s; returning raw values", range_name)

            result = []
            for row_idx, row in enumerate(range(addr.StartRow, addr.EndRow + 1)):
                row_data = []
                for col_idx, col in enumerate(range(addr.StartColumn, addr.EndColumn + 1)):
                    # Extract raw data and formula strings from batch fetched arrays
                    raw_val = data_array[row_idx][col_idx]
                    raw_formula = formula_array[row_idx][col_idx]

                    value = raw_val
                    formula = None

                    if raw_formula and raw_formula.startswith("="):
                        cell_type = FORMULA
                        formula = raw_formula
                        # Keep value as raw_val which already contains the evaluated formula result
                    elif isinstance(raw_val, float):
                        cell_type = VALUE
                    elif isinstance(raw_val, str) and raw_val:
                        cell_type = TEXT
                    else:
                        cell_type = EMPTY
                        value = None

                    col_letter = self.bridge._index_to_column(col)
                    cell_address = f"{col_letter}{row + 1}"

                    cell_info = {"address": cell_address, "value": value, "formula": formula, "type": self._cell_type_name(cell_type)}
                    if null_date is not None and isinstance(value, (int, float)):
                        category = self._category_for_position(format_rows, row, col)
                        if category is not None:
                            # LLM path: ISO in value for round-trips; raw serials stay on include_format_info=False.
                            if category == "duration":
                                cell_info["value"] = iso_duration_from_serial(float(value))
                            else:
                                cell_info["value"] = _iso8601_from_serial(float(value), category, null_date)
                            cell_info["type"] = category
                            cell_info["format_category"] = category
                    row_data.append(cell_info)
                result.append(row_data)

            return result
        except Exception as e:
            logger.error("Range reading error (%s): %s", range_name, str(e))
            raise ToolExecutionError(str(e)) from e

    def get_all_formulas(self, sheet_name: str | None = None) -> list[dict]:
        """List all formulas in a sheet.

        Args:
            sheet_name: Sheet name (active sheet if None).

        Returns:
            List of dicts with keys: address, formula, value, precedents.
        """
        try:
            if sheet_name:
                doc = self.bridge.get_active_document()
                sheets = doc.getSheets()
                sheet = sheets.getByName(sheet_name)
            else:
                sheet = self.bridge.get_active_sheet()

            cursor = sheet.createCursor()
            cursor.gotoStartOfUsedArea(False)
            cursor.gotoEndOfUsedArea(True)

            addr = cursor.getRangeAddress()
            formulas = []

            cell_range = sheet.getCellRangeByPosition(addr.StartColumn, addr.StartRow, addr.EndColumn, addr.EndRow)
            # Result 7 means value, datetime, string. Here we query cells with formulas.
            # Using queryFormulaCells with 23 (1|2|4|16) to get all formula cells, or actually just 23 for all formula results
            formula_cells = cell_range.queryFormulaCells(23)

            if formula_cells:
                cells_collection = formula_cells.getCells()
                if cells_collection:
                    enum = cells_collection.createEnumeration()
                    while enum.hasMoreElements():
                        cell = enum.nextElement()
                        cell_addr = cell.getCellAddress()

                        col_letter = self.bridge._index_to_column(cell_addr.Column)
                        cell_address = f"{col_letter}{cell_addr.Row + 1}"

                        formula = cell.getFormula()
                        value = cell.getValue() if cell.getValue() != 0 else cell.getString()

                        refs = _FORMULA_REF_RE.findall(formula.upper())
                        precedents = list({f"{c}{r}" for c, r in refs})

                        formulas.append({"address": cell_address, "formula": formula, "value": value, "precedents": precedents})

            return formulas
        except Exception as e:
            logger.error("Formula listing error: %s", str(e))
            raise ToolExecutionError(str(e)) from e
