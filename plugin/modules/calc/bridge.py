"""In-process UNO bridge for Calc.

Wraps a Calc document and provides convenience methods for accessing
sheets, cells, and ranges. Ported from core/calc_bridge.py for the
plugin framework.
"""

import logging

from plugin.modules.calc.address_utils import (
    index_to_column,
    column_to_index,
    parse_range_string,
)

logger = logging.getLogger("localwriter.calc")


class CalcBridge:
    """Bridge between the plugin layer and the UNO Calc document."""

    def __init__(self, doc):
        self.doc = doc

    def get_active_document(self):
        """Return the wrapped document."""
        return self.doc

    def get_active_sheet(self):
        """Return the currently active sheet.

        Falls back to the first sheet when the controller does not expose
        *getActiveSheet* (e.g. headless mode).

        Raises:
            RuntimeError: Document is not a spreadsheet or no sheet found.
        """
        if not hasattr(self.doc, "getSheets"):
            raise RuntimeError("Active document is not a spreadsheet.")

        controller = self.doc.getCurrentController()
        if hasattr(controller, "getActiveSheet"):
            sheet = controller.getActiveSheet()
        else:
            sheets = self.doc.getSheets()
            sheet = sheets.getByIndex(0)

        if sheet is None:
            raise RuntimeError("No active sheet found.")
        return sheet

    def get_cell(self, sheet, col: int, row: int):
        """Return the cell object at *col*, *row* on *sheet*."""
        return sheet.getCellByPosition(col, row)

    def get_cell_range(self, sheet, range_str: str):
        """Return a cell range object from a range string like ``A1:D10``."""
        start, end = parse_range_string(range_str)
        return sheet.getCellRangeByPosition(start[0], start[1], end[0], end[1])

    @staticmethod
    def _index_to_column(index: int) -> str:
        return index_to_column(index)

    @staticmethod
    def _column_to_index(col_str: str) -> int:
        return column_to_index(col_str)

    @staticmethod
    def parse_range_string(range_str: str):
        return parse_range_string(range_str)
