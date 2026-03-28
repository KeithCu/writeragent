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
"""Sheet analyzer — analyses the structure and statistics of Calc sheets.

Ported from core/calc_sheet_analyzer.py for the plugin framework.
"""

import logging

from plugin.framework.errors import ToolExecutionError

try:
    from com.sun.star.table.CellContentType import EMPTY, VALUE, TEXT, FORMULA
    UNO_AVAILABLE = True
except ImportError:
    from typing import Any, cast
    EMPTY, VALUE, TEXT, FORMULA = cast(Any, 0), cast(Any, 1), cast(Any, 2), cast(Any, 3)
    UNO_AVAILABLE = False

logger = logging.getLogger("writeragent.calc")


class SheetAnalyzer:
    """Analyses the structure and data of a worksheet."""

    def __init__(self, bridge):
        """
        Args:
            bridge: CalcBridge instance.
        """
        self.bridge = bridge

    def get_sheet_summary(self, sheet_name=None) -> dict:
        """Return a general summary of the active or specified sheet.

        Args:
            sheet_name: Optional name of the sheet to analyse.

        Returns:
            dict with keys: sheet_name, used_range, row_count, col_count,
            headers.
        """
        try:
            if sheet_name:
                doc = self.bridge.get_active_document()
                sheet = doc.getSheets().getByName(sheet_name)
            else:
                sheet = self.bridge.get_active_sheet()

            cursor = sheet.createCursor()
            cursor.gotoStartOfUsedArea(False)
            cursor.gotoEndOfUsedArea(True)

            range_addr = cursor.getRangeAddress()
            start_col = range_addr.StartColumn
            start_row = range_addr.StartRow
            end_col = range_addr.EndColumn
            end_row = range_addr.EndRow

            row_count = end_row - start_row + 1
            col_count = end_col - start_col + 1

            start_col_str = self.bridge._index_to_column(start_col)
            end_col_str = self.bridge._index_to_column(end_col)
            used_range = f"{start_col_str}{start_row + 1}:{end_col_str}{end_row + 1}"

            header_range = sheet.getCellRangeByPosition(start_col, start_row, end_col, start_row)
            header_data = header_range.getDataArray()

            headers = []
            if header_data and len(header_data) > 0:
                for val in header_data[0]:
                    # getDataArray returns floats for numbers and strings for text.
                    # Convert float to string if needed, empty strings to None.
                    str_val = str(val) if val != "" else None
                    headers.append(str_val)

            result = {
                "sheet_name": sheet.getName(),
                "used_range": used_range,
                "row_count": row_count,
                "col_count": col_count,
                "headers": headers,
            }

            # Charts
            try:
                charts = sheet.getCharts()
                result["chart_count"] = charts.getCount()
                result["charts"] = list(charts.getElementNames())
            except Exception as e:
                logger.debug("get_sheet_summary charts error: %s", e)
                result["chart_count"] = 0
                result["charts"] = []

            # Annotations
            try:
                result["annotation_count"] = sheet.getAnnotations().getCount()
            except Exception as e:
                logger.debug("get_sheet_summary annotations error: %s", e)
                result["annotation_count"] = 0

            # Merged cells - count via querying
            try:
                result["has_merges"] = sheet.getPropertyValue("HasMergedCells") if hasattr(sheet, "getPropertyValue") else None
            except Exception as e:
                logger.debug("get_sheet_summary merged cells error: %s", e)
                result["has_merges"] = None

            # Draw page (shapes on sheet)
            try:
                dp = sheet.DrawPage
                result["shape_count"] = dp.getCount()
            except Exception as e:
                logger.debug("get_sheet_summary shape_count error: %s", e)
                result["shape_count"] = 0

            return result
        except Exception as e:
            logger.error("Error creating sheet summary: %s", str(e))
            raise ToolExecutionError(str(e)) from e
