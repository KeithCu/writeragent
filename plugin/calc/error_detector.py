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
"""Error detector — finds and explains formula errors in Calc cells."""

from __future__ import annotations

import logging
from typing import Any

from plugin.calc.address_utils import A1_CELL_RE, format_address, parse_address, parse_range_string, qualify_a1
from plugin.calc.bridge import used_range_address
from plugin.framework.errors import reraise_if_disposed, uno_read
from plugin.framework.uno_context import enum_has_more

try:
    from com.sun.star.sheet.FormulaResult import ERROR as RESULT_ERROR  # type: ignore
except ImportError:
    from typing import cast

    RESULT_ERROR = cast("Any", 4)

log = logging.getLogger(__name__)

# Cap per range (one detect_and_explain call), matching the other large-result caps.
_MAX_EXPLAINED_ERRORS = 50

# LibreOffice Calc error types and descriptions.
# `code` is what the cell shows, matching ScGlobal::GetErrorString (sc/source/core/data/global.cxx):
# `Err:NNN` unless Calc has a `#` form; numeric codes come from include/formula/errorcodes.hxx.
ERROR_TYPES: dict[int, dict[str, str]] = {
    501: {"code": "Err:501", "name": "Invalid character", "description": "An invalid character was found in the formula."},
    502: {"code": "Err:502", "name": "Invalid argument", "description": "The function argument is invalid."},
    503: {"code": "#NUM!", "name": "Invalid numeric value", "description": "A calculation resulted in a number overflow or invalid float."},
    504: {"code": "Err:504", "name": "Error in parameter list", "description": "A function parameter is not valid, for example text where a number is expected, or a range where a single cell is expected."},
    507: {"code": "Err:507", "name": "Missing parenthesis", "description": "There is an unclosed parenthesis in the formula."},
    508: {"code": "Err:508", "name": "Pair missing bracket", "description": "An extra or missing parenthesis was found in the formula."},
    509: {"code": "Err:509", "name": "Operator missing", "description": "A required operator is missing in the formula."},
    510: {"code": "Err:510", "name": "Variable missing", "description": "A required variable is missing in the formula."},
    511: {"code": "Err:511", "name": "Parameter missing", "description": "A function was given fewer parameters than it needs, for example AND() with no arguments."},
    512: {"code": "Err:512", "name": "Formula overflow", "description": "Formula or internal token count exceeds the maximum limit."},
    513: {"code": "Err:513", "name": "String overflow", "description": "A string identifier or token in the formula exceeds size limit."},
    514: {"code": "Err:514", "name": "Internal overflow", "description": "An internal interpreter overflow occurred."},
    516: {"code": "Err:516", "name": "Internal syntax error", "description": "A syntax error was detected in the calculation engine."},
    517: {"code": "Err:517", "name": "Internal syntax error", "description": "A syntax error was detected in the calculation engine."},
    518: {"code": "Err:518", "name": "Internal syntax error", "description": "A syntax error was detected in the calculation engine."},
    519: {"code": "#VALUE!", "name": "Wrong data type", "description": "A value in the formula is not of the expected type. Text may have been used instead of a number or vice versa."},
    520: {"code": "Err:520", "name": "Internal syntax error", "description": "A syntax error was detected in the calculation engine."},
    521: {"code": "#NULL!", "name": "Empty intersection", "description": "The ranges joined by the intersection operator (!) do not overlap."},
    522: {
        "code": "Err:522",
        "name": "Circular reference",
        "description": "The formula refers to itself directly or indirectly.",
        "suggestion": "Circular reference: the formula refers to itself directly or indirectly. Break the circular chain or enable iterative calculation.",
    },
    523: {"code": "Err:523", "name": "The calculation process does not converge", "description": "The calculation process does not converge."},
    524: {
        "code": "#REF!",
        "name": "Invalid reference",
        "description": "A cell reference in the formula is invalid. It may be a deleted cell or sheet reference.",
        "suggestion": "#REF! error: Invalid cell reference. The reference may be broken due to a deleted cell, row, or column. Check the formula and update the references.",
    },
    525: {
        "code": "#NAME?",
        "name": "Invalid name",
        "description": "An invalid name or undefined identifier was used.",
        "suggestion": "Unrecognised name error. Make sure the function name in the formula is spelled correctly and that any defined names exist.",
    },
    526: {"code": "Err:526", "name": "Internal syntax error", "description": "A syntax error was detected in the calculation engine."},
    527: {"code": "Err:527", "name": "Internal overflow", "description": "An internal interpreter overflow occurred."},
    530: {"code": "#ADDIN?", "name": "Add-in not found", "description": "The formula calls an add-in function that is not installed."},
    531: {"code": "#MACRO?", "name": "Macro not found", "description": "The formula calls a macro function that was not found."},
    532: {"code": "#DIV/0!", "name": "Division by zero", "description": "An attempt was made to divide a number by zero. Check the value of the divisor cell."},
    533: {"code": "Err:533", "name": "Nested arrays are not supported", "description": "Nested arrays are not supported."},
    538: {"code": "Err:538", "name": "Array or matrix size", "description": "Array or matrix dimensions do not match."},
    539: {"code": "Err:539", "name": "Unsupported inline array content", "description": "Unsupported inline array content in formula."},
    32767: {
        "code": "#N/A",
        "name": "Value not available",
        "description": "The value is not available. A lookup or reference did not find a match.",
        "suggestion": "#N/A: a lookup or reference did not find a match. Check the search value or add a default with IFERROR.",
    },
}

# One case-insensitive map: keys are uppercased tokens; values keep canonical spelling (#ERROR!, Err:502, …).
_ERROR_PATTERN_BY_UPPER = {token.upper(): token for token in (
    "#REF!", "#NAME?", "#VALUE!", "#DIV/0!", "#NULL!", "#N/A", "#NUM!", "#ERROR!",
    "Err:502", "Err:504", "Err:519", "Err:522", "Err:524", "Err:525", "Err:532",
)}


def truncation_hint(shown: int, total: int) -> str:
    """Hint text when ``error_detect`` caps explained errors."""
    return "First %d of %d. Pass a range that starts after the last address in errors." % (shown, total)


def is_calc_error_display(value: object) -> str | None:
    """Canonical Calc error token when *value* is an error display string."""
    if isinstance(value, str):
        return _ERROR_PATTERN_BY_UPPER.get(value.strip().upper())
    return None


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
            row = ERROR_TYPES[error_code]
            # Static suggestion goes in explain_error's top-level `suggestion`, not in the error dict.
            return {"code": row["code"], "name": row["name"], "description": row["description"], "error_code": error_code}
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
            addr = used_range_address(sheet)
            start_col = addr.StartColumn
            start_row = addr.StartRow
            end_col = addr.EndColumn
            end_row = addr.EndRow

        errors = []
        cell_range = sheet.getCellRangeByPosition(start_col, start_row, end_col, end_row)
        formula_cells = cell_range.queryFormulaCells(RESULT_ERROR)

        enum = formula_cells.getCells().createEnumeration()
        while enum_has_more(enum):
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
        sheet_name = str(sheet.getName())
        display_address = qualify_a1(sheet_name, format_address(col, row))

        # Get precedent cells via formula parsing
        refs = A1_CELL_RE.findall(formula.upper())
        precedent_addrs = list({f"{c}{r}" for c, r in refs})

        error_info = self.get_error_type(cell)

        if not error_info:
            return {"address": display_address, "formula": formula, "error": None, "precedents": [], "suggestion": "No error detected in this cell."}

        # Bare refs live on the error cell's sheet, not the active one.
        precedent_details = [
            uno_read(
                lambda: self.inspector.read_cell(qualify_a1(sheet_name, prec_addr)),
                {"address": prec_addr, "value": "UNREADABLE", "type": "unknown"},
            )
            for prec_addr in precedent_addrs
        ]

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
            assert isinstance(address, str)
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
            **({"hint": truncation_hint(len(detailed), len(errors))} if truncated else {}),
            "errors": detailed,
        }

    @staticmethod
    def _generate_suggestion(error_info: dict[str, Any], precedents: list[dict[str, Any]]) -> str:
        """Generate a fix suggestion based on error type and precedent cells."""
        error_code = error_info.get("error_code")
        static = ERROR_TYPES.get(error_code, {}).get("suggestion") if isinstance(error_code, int) else None
        if static is not None:
            return static

        if error_code == 532:
            # Under the read contract, an error-formula precedent has value None with type "formula";
            # only a zero value or an empty cell is a zero divisor.
            zero_cells = [p["address"] for p in precedents if p.get("value") == 0 or p.get("type") == "empty"]
            if zero_cells:
                return f"Division by zero error. The following cells are zero or empty: {', '.join(zero_cells)}. Try adding a zero check with the IF function: =IF(divisor<>0; dividend/divisor; 0)"
            return "Division by zero error. Make sure the divisor value is not zero or add a check with the IF function."

        if error_code == 519:
            text_cells = [p["address"] for p in precedents if p.get("type") == "text"]
            if text_cells:
                return f"Value type error. The following cells contain text instead of numbers: {', '.join(text_cells)}. You can use the VALUE() function for text-to-number conversion."
            return "Value type error. A value of an unexpected type was used in the formula. Check the types of cell values."

        return error_info.get("description", "Unknown error. Check the formula.")
