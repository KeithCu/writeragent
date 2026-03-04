"""Sheet analyzer — analyses the structure and statistics of Calc sheets.

Ported from core/calc_sheet_analyzer.py for the plugin framework.
"""

import logging

try:
    from com.sun.star.table.CellContentType import EMPTY, VALUE, TEXT, FORMULA
    UNO_AVAILABLE = True
except ImportError:
    EMPTY, VALUE, TEXT, FORMULA = 0, 1, 2, 3
    UNO_AVAILABLE = False

logger = logging.getLogger("localwriter.calc")


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

            headers = []
            for col in range(start_col, end_col + 1):
                cell = sheet.getCellByPosition(col, start_row)
                cell_value = cell.getString()
                headers.append(cell_value if cell_value else None)

            return {
                "sheet_name": sheet.getName(),
                "used_range": used_range,
                "row_count": row_count,
                "col_count": col_count,
                "headers": headers,
            }
        except Exception as e:
            logger.error("Error creating sheet summary: %s", str(e))
            raise
