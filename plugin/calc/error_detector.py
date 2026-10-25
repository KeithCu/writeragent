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

from __future__ import annotations

import logging
from typing import Any

from plugin.calc.address_utils import A1_CELL_RE, format_address, parse_address, parse_range_string, qualify_a1
from plugin.framework.errors import reraise_if_disposed, uno_probe

try:
    from com.sun.star.sheet.FormulaResult import ERROR as RESULT_ERROR  # type: ignore
except ImportError:
    from typing import cast

    RESULT_ERROR = cast("Any", 4)

log = logging.getLogger("writeragent.calc")

# Cap per call, matching the other large-result caps.
_MAX_EXPLAINED_ERRORS = 50

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
    32767: {"code": "#N/A", "name": "#N/A", "description": "The value is not available. A lookup or reference did not find a match."},
}

# Bugfix: unifies the two disparate Calc error token lists (ERROR_PATTERNS here
# and _CALC_ERROR_STRINGS in graph.py). Previously graph.py missed Err:NNN codes
# and ERROR_PATTERNS missed #ERROR!. Case-insensitive lookup maps to canonical spelling.
ERROR_PATTERNS = frozenset({
    "#REF!", "#NAME?", "#VALUE!", "#DIV/0!", "#NULL!", "#N/A", "#NUM!", "#ERROR!",
    "Err:502", "Err:504", "Err:519", "Err:522", "Err:524", "Err:525", "Err:532",
})
# "Err:502".upper() is "ERR:502", so membership on the uppercased text cannot
# use the canonical spelling as the key.
_ERROR_PATTERN_BY_UPPER = {token.upper(): token for token in ERROR_PATTERNS}


def is_calc_error_display(value: object) -> str | None:
    """Canonical Calc error token when *value* is an error display string."""
    if isinstance(value, str):
        return _ERROR_PATTERN_BY_UPPER.get(value.strip().upper())
    return None


def get_calc_error_name(error_code: int) -> str:
    """Return a human-readable name for a Calc error code."""
    if error_code in ERROR_TYPES:
        return ERROR_TYPES[error_code]["name"]
    return f"Unknown error ({error_code})"


class ErrorDetector:
    """Detects and explains formula errors in the worksheet."""

    bridge: Any
    inspector: Any
    ctx: Any

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
        error_code = cell.getError()
        if error_code == 0:
            return {}
        if error_code in ERROR_TYPES:
            info = ERROR_TYPES[error_code].copy()
            info["error_code"] = error_code
            return info
        return {"error_code": error_code, "code": f"Err:{error_code}", "name": "Unknown error", "description": f"Unknown error code: {error_code}"}

    def detect_errors(self, range_str: str | None = None) -> list[dict[str, Any]]:
        """Detect errors in the specified range or the entire sheet.

        Args:
            range_str: Cell range (e.g. "A1:D10"). Scans the whole sheet
                when *None*.

        Returns:
            List of dicts with keys: address, formula, error.
        """
        if range_str:
            # Honour sheet-qualified ranges (Sheet1.A1:D10) without switching
            # the active sheet; parse_range_string alone rejects prefixes.
            sheet, bare = self.bridge.resolve(range_str)
            start, end = parse_range_string(bare)
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
                        address = qualify_a1(str(sheet.getName()), format_address(addr.Column, addr.Row))
                        errors.append({"address": address, "formula": cell.getFormula(), "error": error_info})

        log.debug("%d errors detected (range: %s).", len(errors), range_str or "full sheet")
        return errors

    def explain_error(self, address: str) -> dict[str, Any]:
        """Explain the error in the specified cell in detail.

        Args:
            address: Cell address (e.g. "A1").

        Returns:
            dict with keys: address, formula, error, precedents, suggestion.
        """
        sheet, bare = self.bridge.resolve(address)
        col, row = parse_address(bare)
        cell = sheet.getCellByPosition(col, row)
        formula = cell.getFormula() or ""
        display_address = qualify_a1(str(sheet.getName()), format_address(col, row))

        # Get precedent cells via formula parsing
        refs = A1_CELL_RE.findall(formula.upper())
        precedent_addrs = list({f"{c}{r}" for c, r in refs})

        error_info = self.get_error_type(cell)

        if not error_info:
            return {"address": display_address, "formula": formula, "error": None, "precedents": [], "suggestion": "No error detected in this cell."}

        precedent_details = []
        for prec_addr in precedent_addrs:
            def _read_prec(addr: str = prec_addr) -> dict[str, Any]:
                return self.inspector.read_cell(addr)

            prec_info = uno_probe(
                _read_prec,
                {"address": prec_addr, "value": "UNREADABLE", "type": "unknown"},
            )
            precedent_details.append(prec_info)

        suggestion = self._generate_suggestion(error_info, precedent_details)

        dependency_chain = None
        try:
            from plugin.calc.formula_dep_chain import fetch_formula_dep_chain

            dependency_chain = fetch_formula_dep_chain(self.bridge.doc, self.ctx, address)
        except Exception as e:
            log.debug("FormulaDepChain unavailable for %s: %s", address, e)

        result = {"address": display_address, "formula": formula, "error": error_info, "precedents": precedent_details, "suggestion": suggestion}
        if dependency_chain:
            result["dependency_chain"] = dependency_chain
        return result

    def detect_and_explain(self, range_str: str | None = None) -> dict[str, Any]:
        """Detect formula errors in a range and return them with explanations.

        Args:
            range_str: Cell range to check (whole sheet if *None*).

        Returns:
            dict with keys: range, total_error_count, error_count, truncated, errors.
        """
        errors = self.detect_errors(range_str)
        detailed = []
        truncated = len(errors) > _MAX_EXPLAINED_ERRORS

        for item in errors[:_MAX_EXPLAINED_ERRORS]:
            address = item.get("address")
            if not address:
                continue
            try:
                detailed.append(self.explain_error(address))
            except Exception as e:
                reraise_if_disposed(e)
                log.warning("Explain errors failed for %s: %s", address, e)
                detailed.append({"address": address, "formula": item.get("formula", ""), "error": item.get("error"), "precedents": [], "suggestion": "Could not explain error; basic info shown."})

        return {
            "range": range_str or "used_area",
            "total_error_count": len(errors),
            "error_count": len(detailed),
            "truncated": truncated,
            "errors": detailed,
        }

    @staticmethod
    def _generate_suggestion(error_info: dict[str, Any], precedents: list[dict[str, Any]]) -> str:
        """Generate a fix suggestion based on error type and precedent cells."""
        error_code = error_info.get("error_code")

        if error_code == 532:
            zero_cells = [p["address"] for p in precedents if p.get("value") == 0 or p.get("value") is None]
            if zero_cells:
                return f"Division by zero error. The following cells are zero or empty: {', '.join(zero_cells)}. Try adding a zero check with the IF function: =IF(divisor<>0; dividend/divisor; 0)"
            return "Division by zero error. Make sure the divisor value is not zero or add a check with the IF function."

        if error_code == 524:
            return "#REF! error: Invalid cell reference. The reference may be broken due to a deleted cell, row, or column. Check the formula and update the references."

        if error_code == 522:
            return "Circular reference: the formula refers to itself directly or indirectly. Break the circular chain or enable iterative calculation."

        if error_code == 525:
            return "Unrecognised name error. Make sure the function name in the formula is spelled correctly and that any defined names exist."

        if error_code == 519:
            text_cells = [p["address"] for p in precedents if p.get("type") == "text"]
            if text_cells:
                return f"Value type error. The following cells contain text instead of numbers: {', '.join(text_cells)}. You can use the VALUE() function for text-to-number conversion."
            return "Value type error. A value of an unexpected type was used in the formula. Check the types of cell values."

        if error_code == 32767:
            return "#N/A: a lookup or reference did not find a match. Check the search value or add a default with IFERROR."

        return error_info.get("description", "Unknown error. Check the formula.")
