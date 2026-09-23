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
"""Error detector — finds and explains formula errors in Calc cells.

Ported from core/calc_error_detector.py for the plugin framework.
"""

import logging
import re
from typing import Any

from plugin.calc.address_utils import parse_address
from plugin.framework.errors import ToolExecutionError

# Regex for matching cell references (e.g. A1, $B$2)
CELL_REF_PATTERN = re.compile(r"\$?([A-Z]+)\$?(\d+)")

try:
    from com.sun.star.sheet.FormulaResult import ERROR as RESULT_ERROR  # type: ignore

    UNO_AVAILABLE = True
except ImportError:
    from typing import Any, cast

    RESULT_ERROR = cast("Any", 4)
    UNO_AVAILABLE = False

log = logging.getLogger("writeragent.calc")

# LibreOffice Calc error types and descriptions
ERROR_TYPES = {
    501: {"code": "#NULL!", "name": "Invalid character", "description": "An invalid character was found in the formula."},
    502: {"code": "#NULL!", "name": "Invalid argument", "description": "The function argument is invalid."},
    503: {"code": "#NUM!", "name": "#NUM!", "description": "A calculation resulted in a number overflow or invalid float."},
    504: {"code": "#NAME?", "name": "Error in parameter list", "description": "An unrecognised function or area name was used. Make sure the function name is spelled correctly."},
    507: {"code": "#NULL!", "name": "Missing parenthesis", "description": "There is an unclosed parenthesis in the formula."},
    508: {"code": "#NULL!", "name": "Error: Pair missing", "description": "An extra or missing parenthesis was found in the formula."},
    509: {"code": "#NULL!", "name": "Missing operator", "description": "A required operator is missing in the formula."},
    510: {"code": "#NULL!", "name": "Missing variable", "description": "A required variable is missing in the formula."},
    511: {"code": "#NULL!", "name": "Missing variable", "description": "A required variable is missing in the formula."},
    512: {"code": "#NULL!", "name": "Formula overflow", "description": "Formula or internal token count exceeds the maximum limit."},
    513: {"code": "#NULL!", "name": "String overflow", "description": "A string identifier or token in the formula exceeds size limit."},
    514: {"code": "#NULL!", "name": "Internal overflow", "description": "An internal interpreter overflow occurred."},
    516: {"code": "#NULL!", "name": "Internal syntax error", "description": "A syntax error was detected in the calculation engine."},
    517: {"code": "#NULL!", "name": "Internal syntax error", "description": "A syntax error was detected in the calculation engine."},
    518: {"code": "#NULL!", "name": "Internal syntax error", "description": "A syntax error was detected in the calculation engine."},
    519: {"code": "#VALUE!", "name": "#VALUE!", "description": "A value in the formula is not of the expected type. Text may have been used instead of a number or vice versa."},
    520: {"code": "#NULL!", "name": "Internal syntax error", "description": "A syntax error was detected in the calculation engine."},
    521: {"code": "#NULL!", "name": "Internal error", "description": "An internal calculation error occurred."},
    522: {"code": "#REF!", "name": "Circular reference", "description": "The formula refers to itself directly or indirectly."},
    523: {"code": "#NUM!", "name": "The calculation process does not converge", "description": "The calculation process does not converge."},
    524: {"code": "#REF!", "name": "#REF!", "description": "A cell reference in the formula is invalid. It may be a deleted cell or sheet reference."},
    525: {"code": "#NAME?", "name": "#NAME?", "description": "An invalid name or undefined identifier was used."},
    526: {"code": "#NULL!", "name": "Internal syntax error", "description": "A syntax error was detected in the calculation engine."},
    527: {"code": "#NULL!", "name": "Internal overflow", "description": "An internal interpreter overflow occurred."},
    532: {"code": "#DIV/0!", "name": "#DIV/0!", "description": "An attempt was made to divide a number by zero. Check the value of the divisor cell."},
    533: {"code": "#NULL!", "name": "Nested arrays are not supported", "description": "The intersection of two ranges is empty or nested arrays are unsupported."},
    538: {"code": "#VALUE!", "name": "Error: Array or matrix size", "description": "Array or matrix dimensions do not match."},
    539: {"code": "#VALUE!", "name": "Unsupported inline array content", "description": "Unsupported inline array content in formula."},
}

# Cell error text patterns
ERROR_PATTERNS = ["#REF!", "#NAME?", "#VALUE!", "#DIV/0!", "#NULL!", "#N/A", "#NUM!", "Err:502", "Err:504", "Err:519", "Err:522", "Err:524", "Err:525", "Err:532"]


def get_calc_error_name(error_code: int) -> str:
    """Return a human-readable name for a Calc error code."""
    if error_code in ERROR_TYPES:
        return ERROR_TYPES[error_code]["name"]
    return f"Unknown error ({error_code})"



class ErrorDetector:
    """Detects and explains formula errors in the worksheet."""

    def __init__(self, bridge: Any, inspector: Any, ctx: Any = None) -> None:
        """
        Args:
            bridge: CalcBridge instance.
            inspector: CellInspector instance.
            ctx: Optional UNO component context (for ``FormulaDepChain`` dispatch).
        """
        self.bridge = bridge
        self.inspector = inspector
        self.ctx = ctx

    @staticmethod
    def get_error_type(cell: Any) -> dict[str, Any]:
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
            return {"code": f"Err:{error_code}", "name": "Unknown error", "description": f"Unknown error code: {error_code}"}
        except Exception as e:
            log.debug("Explain error getError exception: %s", e)
            try:
                text = cell.getString()
                for pattern in ERROR_PATTERNS:
                    if pattern in text:
                        return {"code": pattern, "name": "Formula error", "description": f"'{pattern}' error detected in the cell."}
            except Exception as e2:
                log.debug("Explain error getString exception: %s", e2)
            return {}

    def detect_errors(self, range_str: str | None = None) -> list[dict[str, Any]]:
        """Detect errors in the specified range or the entire sheet.

        Args:
            range_str: Cell range (e.g. "A1:D10"). Scans the whole sheet
                when *None*.

        Returns:
            List of dicts with keys: address, formula, error.
        """
        try:
            if range_str:
                # Honour sheet-qualified ranges (Sheet1.A1:D10) without switching
                # the active sheet; parse_range_string alone rejects prefixes.
                sheet, bare = self.bridge.resolve(range_str)
                start, end = self.bridge.parse_range_string(bare)
                start_col, start_row = start
                end_col, end_row = end
            else:
                sheet = self.bridge.get_active_sheet()
                cursor = sheet.createCursor()
                cursor.gotoStartOfUsedArea(False)
                cursor.gotoEndOfUsedArea(True)
                addr = cursor.getRangeAddress()
                start_col = addr.StartColumn
                start_row = addr.StartRow
                end_col = addr.EndColumn
                end_row = addr.EndRow

            errors = []
            cell_range = sheet.getCellRangeByPosition(start_col, start_row, end_col, end_row)
            formula_cells = cell_range.queryFormulaCells(RESULT_ERROR)

            if formula_cells:
                # getCells() returns a collection of cells. We can iterate over them.
                cells_collection = formula_cells.getCells()
                if cells_collection:
                    enum = cells_collection.createEnumeration()
                    while enum.hasMoreElements():
                        cell = enum.nextElement()
                        error_info = self.get_error_type(cell)
                        if error_info:
                            addr = cell.getCellAddress()
                            col_str = self.bridge._index_to_column(addr.Column)
                            address = f"{col_str}{addr.Row + 1}"
                            errors.append({"address": address, "formula": cell.getFormula(), "error": error_info})

            log.info("%d errors detected (range: %s).", len(errors), range_str or "full sheet")
            return errors
        except Exception as e:
            log.exception("Error detection failed")
            raise ToolExecutionError(str(e)) from e

    def explain_error(self, address: str) -> dict[str, Any]:
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
            refs = CELL_REF_PATTERN.findall(formula.upper())
            precedent_addrs = list({f"{c}{r}" for c, r in refs})

            error_info = self.get_error_type(cell)

            if not error_info:
                return {"address": address.upper(), "formula": cell_details.get("formula", ""), "error": None, "precedents": [], "suggestion": "No error detected in this cell."}

            precedent_details = []
            for prec_addr in precedent_addrs:
                try:
                    prec_info = self.inspector.read_cell(prec_addr)
                    precedent_details.append(prec_info)
                except Exception:
                    precedent_details.append({"address": prec_addr, "value": "UNREADABLE", "type": "unknown"})

            suggestion = self._generate_suggestion(error_info, precedent_details)

            dependency_chain = None
            try:
                from plugin.calc.formula_dep_chain import fetch_formula_dep_chain

                dependency_chain = fetch_formula_dep_chain(self.bridge.doc, self.ctx, address)
            except Exception as e:
                log.debug("FormulaDepChain unavailable for %s: %s", address, e)

            result = {
                "address": address.upper(),
                "formula": cell_details.get("formula", ""),
                "error": error_info,
                "precedents": precedent_details,
                "suggestion": suggestion,
            }
            if dependency_chain:
                result["dependency_chain"] = dependency_chain
            return result
        except Exception as e:
            log.exception("Error explanation failed for %s", address)
            raise ToolExecutionError(str(e)) from e

    def detect_and_explain(self, range_str: str | None = None) -> dict[str, Any]:
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
            except Exception as e:
                log.warning("Explain errors failed for %s: %s", address, e)
                detailed.append({"address": address, "formula": item.get("formula", ""), "error": item.get("error"), "precedents": [], "suggestion": "Could not explain error; basic info shown."})

        return {"range": range_str or "used_area", "error_count": len(detailed), "errors": detailed}

    @staticmethod
    def _generate_suggestion(error_info: dict[str, Any], precedents: list[dict[str, Any]]) -> str:
        """Generate a fix suggestion based on error type and precedent cells."""
        code = error_info.get("code", "")

        if code == "#DIV/0!":
            zero_cells = [p["address"] for p in precedents if p.get("value") == 0 or p.get("value") is None]
            if zero_cells:
                return f"Division by zero error. The following cells are zero or empty: {', '.join(zero_cells)}. Try adding a zero check with the IF function: =IF(divisor<>0; dividend/divisor; 0)"
            return "Division by zero error. Make sure the divisor value is not zero or add a check with the IF function."

        if code == "#REF!":
            return "#REF! error: Invalid cell reference. The reference may be broken due to a deleted cell, row, or column. Check the formula and update the references."

        if code == "#NAME?":
            return "Unrecognised name error. Make sure the function name in the formula is spelled correctly and that any defined names exist."

        if code == "#VALUE!":
            text_cells = [p["address"] for p in precedents if p.get("type") == "text"]
            if text_cells:
                return f"Value type error. The following cells contain text instead of numbers: {', '.join(text_cells)}. You can use the VALUE() function for text-to-number conversion."
            return "Value type error. A value of an unexpected type was used in the formula. Check the types of cell values."

        if code == "#N/A":
            return "Value not found error. The value being searched for in VLOOKUP or a similar search function was not found. You can set a default value with IFERROR."

        return error_info.get("description", "Unknown error. Check the formula.")
