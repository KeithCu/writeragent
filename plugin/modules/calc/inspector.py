"""Cell inspector — reads detailed information from LibreOffice Calc cells.

Ported from core/calc_inspector.py for the plugin framework.
"""

import logging
import re

from plugin.modules.calc.address_utils import parse_address

try:
    from com.sun.star.table.CellContentType import EMPTY, VALUE, TEXT, FORMULA
    UNO_AVAILABLE = True
except ImportError:
    EMPTY, VALUE, TEXT, FORMULA = 0, 1, 2, 3
    UNO_AVAILABLE = False

logger = logging.getLogger("localwriter.calc")


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
        except Exception:
            return default

    def _get_cell(self, address: str):
        """Return the cell object for *address*."""
        col, row = parse_address(address)
        sheet = self.bridge.get_active_sheet()
        return self.bridge.get_cell(sheet, col, row)

    # ── Public API ─────────────────────────────────────────────────────

    def read_cell(self, address: str) -> dict:
        """Read basic cell information.

        Args:
            address: Cell address (e.g. "A1").

        Returns:
            dict with keys: address, value, formula, type.
        """
        try:
            cell = self._get_cell(address)
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

            return {
                "address": address.upper(),
                "value": value,
                "formula": formula,
                "type": self._cell_type_name(cell_type),
            }
        except Exception as e:
            logger.error("Cell reading error (%s): %s", address, str(e))
            raise

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
            cell = self._get_cell(address)
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
            raise

    def read_range(self, range_name: str) -> list[list[dict]]:
        """Read values and formulas in a cell range.

        Args:
            range_name: Cell range (e.g. "A1:D10", "B2").

        Returns:
            2D list of dicts, each with keys: address, value, formula, type.
        """
        try:
            sheet = self.bridge.get_active_sheet()

            # Single cell shortcut
            if ":" not in range_name:
                cell_info = self.read_cell(range_name)
                return [[cell_info]]

            cell_range = self.bridge.get_cell_range(sheet, range_name)
            addr = cell_range.getRangeAddress()

            result = []
            for row in range(addr.StartRow, addr.EndRow + 1):
                row_data = []
                for col in range(addr.StartColumn, addr.EndColumn + 1):
                    cell = sheet.getCellByPosition(col, row)
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

                    col_letter = self.bridge._index_to_column(col)
                    cell_address = f"{col_letter}{row + 1}"
                    formula = cell.getFormula() if cell_type == FORMULA else None

                    row_data.append({
                        "address": cell_address,
                        "value": value,
                        "formula": formula,
                        "type": self._cell_type_name(cell_type),
                    })
                result.append(row_data)

            return result
        except Exception as e:
            logger.error("Range reading error (%s): %s", range_name, str(e))
            raise

    def get_all_formulas(self, sheet_name: str = None) -> list[dict]:
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

            for row in range(addr.StartRow, addr.EndRow + 1):
                for col in range(addr.StartColumn, addr.EndColumn + 1):
                    cell = sheet.getCellByPosition(col, row)
                    if cell.getType() == FORMULA:
                        col_letter = self.bridge._index_to_column(col)
                        cell_address = f"{col_letter}{row + 1}"
                        formula = cell.getFormula()
                        value = cell.getValue() if cell.getValue() != 0 else cell.getString()

                        refs = re.findall(r'\$?([A-Z]+)\$?(\d+)', formula.upper())
                        precedents = list(set([f"{c}{r}" for c, r in refs]))

                        formulas.append({
                            "address": cell_address,
                            "formula": formula,
                            "value": value,
                            "precedents": precedents,
                        })

            return formulas
        except Exception as e:
            logger.error("Formula listing error: %s", str(e))
            raise
