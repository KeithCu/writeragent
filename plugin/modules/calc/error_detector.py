"""Error detector — finds and explains formula errors in Calc cells.

Ported from core/calc_error_detector.py for the plugin framework.
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

# LibreOffice Calc error types and descriptions
ERROR_TYPES = {
    501: {
        "code": "#NULL!",
        "name": "Invalid character",
        "description": "An invalid character was found in the formula.",
    },
    502: {
        "code": "#NULL!",
        "name": "Invalid argument",
        "description": "The function argument is invalid.",
    },
    504: {
        "code": "#NAME?",
        "name": "Name error",
        "description": (
            "An unrecognised function or area name was used. "
            "Make sure the function name is spelled correctly."
        ),
    },
    507: {
        "code": "#NULL!",
        "name": "Missing parenthesis",
        "description": "There is an unclosed parenthesis in the formula.",
    },
    508: {
        "code": "#NULL!",
        "name": "Parenthesis error",
        "description": "An extra or missing parenthesis was found in the formula.",
    },
    510: {
        "code": "#NULL!",
        "name": "Missing operator",
        "description": "A required operator is missing in the formula.",
    },
    511: {
        "code": "#NULL!",
        "name": "Missing variable",
        "description": "A required variable is missing in the formula.",
    },
    519: {
        "code": "#VALUE!",
        "name": "Value error",
        "description": (
            "A value in the formula is not of the expected type. "
            "Text may have been used instead of a number or vice versa."
        ),
    },
    521: {
        "code": "#NULL!",
        "name": "Internal error",
        "description": "An internal calculation error occurred.",
    },
    522: {
        "code": "#REF!",
        "name": "Circular reference",
        "description": "The formula refers to itself directly or indirectly.",
    },
    524: {
        "code": "#REF!",
        "name": "Reference error",
        "description": (
            "A cell reference in the formula is invalid. "
            "It may be a deleted cell or sheet reference."
        ),
    },
    525: {
        "code": "#NAME?",
        "name": "Name error",
        "description": "An invalid name or undefined identifier was used.",
    },
    532: {
        "code": "#DIV/0!",
        "name": "Division by zero",
        "description": (
            "An attempt was made to divide a number by zero. "
            "Check the value of the divisor cell."
        ),
    },
    533: {
        "code": "#NULL!",
        "name": "Intersection error",
        "description": "The intersection of two ranges is empty.",
    },
}

# Cell error text patterns
ERROR_PATTERNS = [
    "#REF!", "#NAME?", "#VALUE!", "#DIV/0!", "#NULL!",
    "#N/A", "#NUM!", "Err:502", "Err:504", "Err:519",
    "Err:522", "Err:524", "Err:525", "Err:532",
]


class ErrorDetector:
    """Detects and explains formula errors in the worksheet."""

    def __init__(self, bridge, inspector):
        """
        Args:
            bridge: CalcBridge instance.
            inspector: CellInspector instance.
        """
        self.bridge = bridge
        self.inspector = inspector

    @staticmethod
    def get_error_type(cell) -> dict:
        """Determine the error type of a cell.

        Args:
            cell: LibreOffice cell object.

        Returns:
            Error info dict, or empty dict when there is no error.
        """
        try:
            error_code = cell.getError()
            if error_code == 0:
                return {}
            if error_code in ERROR_TYPES:
                return ERROR_TYPES[error_code].copy()
            return {
                "code": f"Err:{error_code}",
                "name": "Unknown error",
                "description": f"Unknown error code: {error_code}",
            }
        except Exception:
            try:
                text = cell.getString()
                for pattern in ERROR_PATTERNS:
                    if pattern in text:
                        return {
                            "code": pattern,
                            "name": "Formula error",
                            "description": f"'{pattern}' error detected in the cell.",
                        }
            except Exception:
                pass
            return {}

    def detect_errors(self, range_str: str = None) -> list:
        """Detect errors in the specified range or the entire sheet.

        Args:
            range_str: Cell range (e.g. "A1:D10"). Scans the whole sheet
                when *None*.

        Returns:
            List of dicts with keys: address, formula, error.
        """
        try:
            sheet = self.bridge.get_active_sheet()

            if range_str:
                start, end = self.bridge.parse_range_string(range_str)
                start_col, start_row = start
                end_col, end_row = end
            else:
                cursor = sheet.createCursor()
                cursor.gotoStartOfUsedArea(False)
                cursor.gotoEndOfUsedArea(True)
                addr = cursor.getRangeAddress()
                start_col = addr.StartColumn
                start_row = addr.StartRow
                end_col = addr.EndColumn
                end_row = addr.EndRow

            errors = []
            for row in range(start_row, end_row + 1):
                for col in range(start_col, end_col + 1):
                    cell = sheet.getCellByPosition(col, row)
                    if cell.getType() != FORMULA:
                        continue
                    error_info = self.get_error_type(cell)
                    if error_info:
                        col_str = self.bridge._index_to_column(col)
                        address = f"{col_str}{row + 1}"
                        errors.append({
                            "address": address,
                            "formula": cell.getFormula(),
                            "error": error_info,
                        })

            logger.info(
                "%d errors detected (range: %s).",
                len(errors), range_str or "full sheet",
            )
            return errors
        except Exception as e:
            logger.error("Error detection failure: %s", str(e))
            raise

    def explain_error(self, address: str) -> dict:
        """Explain the error in the specified cell in detail.

        Args:
            address: Cell address (e.g. "A1").

        Returns:
            dict with keys: address, formula, error, precedents, suggestion.
        """
        try:
            cell_details = self.inspector.get_cell_details(address)

            # Get precedent cells via formula parsing
            col, row = parse_address(address)
            sheet = self.bridge.get_active_sheet()
            cell = sheet.getCellByPosition(col, row)
            formula = cell.getFormula() or ""
            refs = re.findall(r'\$?([A-Z]+)\$?(\d+)', formula.upper())
            precedent_addrs = list(set([f"{c}{r}" for c, r in refs]))

            error_info = self.get_error_type(cell)

            if not error_info:
                return {
                    "address": address.upper(),
                    "formula": cell_details.get("formula", ""),
                    "error": None,
                    "precedents": [],
                    "suggestion": "No error detected in this cell.",
                }

            precedent_details = []
            for prec_addr in precedent_addrs:
                try:
                    prec_info = self.inspector.read_cell(prec_addr)
                    precedent_details.append(prec_info)
                except Exception:
                    precedent_details.append({
                        "address": prec_addr,
                        "value": "UNREADABLE",
                        "type": "unknown",
                    })

            suggestion = self._generate_suggestion(error_info, precedent_details)

            return {
                "address": address.upper(),
                "formula": cell_details.get("formula", ""),
                "error": error_info,
                "precedents": precedent_details,
                "suggestion": suggestion,
            }
        except Exception as e:
            logger.error("Error explanation failure (%s): %s", address, str(e))
            raise

    def detect_and_explain(self, range_str: str = None) -> dict:
        """Detect formula errors in a range and return them with explanations.

        Args:
            range_str: Cell range to check (whole sheet if *None*).

        Returns:
            dict with keys: range, error_count, errors.
        """
        errors = self.detect_errors(range_str)
        detailed = []

        for item in errors:
            address = item.get("address")
            if not address:
                continue
            try:
                detailed.append(self.explain_error(address))
            except Exception:
                detailed.append({
                    "address": address,
                    "formula": item.get("formula", ""),
                    "error": item.get("error"),
                    "precedents": [],
                    "suggestion": "Could not explain error; basic info shown.",
                })

        return {
            "range": range_str or "used_area",
            "error_count": len(detailed),
            "errors": detailed,
        }

    @staticmethod
    def _generate_suggestion(error_info: dict, precedents: list) -> str:
        """Generate a fix suggestion based on error type and precedent cells."""
        code = error_info.get("code", "")

        if code == "#DIV/0!":
            zero_cells = [
                p["address"] for p in precedents
                if p.get("value") == 0 or p.get("value") is None
            ]
            if zero_cells:
                return (
                    f"Division by zero error. The following cells are zero or "
                    f"empty: {', '.join(zero_cells)}. Try adding a zero check "
                    f"with the IF function: =IF(divisor<>0; dividend/divisor; 0)"
                )
            return (
                "Division by zero error. Make sure the divisor value is not "
                "zero or add a check with the IF function."
            )

        if code == "#REF!":
            return (
                "Invalid cell reference. The reference may be broken due to a "
                "deleted cell, row, or column. Check the formula and update "
                "the references."
            )

        if code == "#NAME?":
            return (
                "Unrecognised name error. Make sure the function name in the "
                "formula is spelled correctly and that any defined names exist."
            )

        if code == "#VALUE!":
            text_cells = [
                p["address"] for p in precedents
                if p.get("type") == "text"
            ]
            if text_cells:
                return (
                    f"Value type error. The following cells contain text "
                    f"instead of numbers: {', '.join(text_cells)}. You can "
                    f"use the VALUE() function for text-to-number conversion."
                )
            return (
                "Value type error. A value of an unexpected type was used in "
                "the formula. Check the types of cell values."
            )

        if code == "#N/A":
            return (
                "Value not found error. The value being searched for in "
                "VLOOKUP or a similar search function was not found. You can "
                "set a default value with IFERROR."
            )

        return error_info.get("description", "Unknown error. Check the formula.")
