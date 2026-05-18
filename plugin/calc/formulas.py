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
"""Calc formula integration tools.

Provides built-in Calc function discovery and arbitrary formula pre-evaluation tools.

> [!NOTE]
> **EvaluateFormula Sheet-Copy Pattern**:
> Unlike standard empty-worksheet insertion which fails to resolve relative or unqualified cell
> references (e.g. `=A1+B1` evaluates to 0 on an empty sheet), `EvaluateFormula` uses a robust,
> side-effect-free Sheet-Copy Pattern. It duplicates the active sheet to a temporary hidden sheet,
> writes the formula at the specified `cell` coordinate context (resolving relative cell dependencies
> against the duplicated live sheet values perfectly), and cleanly deletes the copied sheet in a
> `finally` block before returning.
"""

import datetime
import logging
from typing import Any, cast

from plugin.framework.errors import ToolExecutionError
from plugin.framework.tool import ToolBase
from plugin.calc.base import ToolCalcErrorBase

try:
    from com.sun.star.table.CellContentType import EMPTY, VALUE, TEXT, FORMULA

    UNO_AVAILABLE = True
except ImportError:
    from typing import cast

    EMPTY, VALUE, TEXT, FORMULA = cast("Any", 0), cast("Any", 1), cast("Any", 2), cast("Any", 3)
    UNO_AVAILABLE = False

logger = logging.getLogger("writeragent.calc")


class ListCalcFunctions(ToolBase):
    """Retrieve available spreadsheet functions inside LibreOffice Calc."""

    name = "list_calc_functions"
    description = (
        "Lists available Calc spreadsheet functions. "
        "Use the 'filter' parameter to perform a case-insensitive search for a partial substring "
        "anywhere in function names or descriptions to avoid context window bloat."
    )
    parameters = {
        "type": "object",
        "properties": {
            "filter": {
                "type": "string",
                "description": "Optional substring to filter functions by (case-insensitive search anywhere in the name or description)."
            }
        },
        "required": []
    }
    uno_services = ["com.sun.star.sheet.SpreadsheetDocument"]
    tier = "core"
    is_mutation = False

    def execute(self, ctx, **kwargs):
        filter_str = kwargs.get("filter", "").strip().upper()
        uno_ctx = ctx.ctx
        if not uno_ctx:
            from plugin.framework.uno_context import get_ctx
            uno_ctx = get_ctx()

        if not uno_ctx:
            raise ToolExecutionError("UNO context not available.")

        try:
            smgr = cast("Any", uno_ctx).getServiceManager()
            func_descr_service = smgr.createInstanceWithContext(
                "com.sun.star.sheet.FunctionDescriptions", uno_ctx
            )
            if not func_descr_service:
                raise ToolExecutionError("FunctionDescriptions service not available.")

            matched_functions = []
            for i in range(func_descr_service.getCount()):
                props = func_descr_service.getByIndex(i)
                func_data = {prop.Name: prop.Value for prop in props}
                name = func_data.get("Name", "")
                desc = func_data.get("Description", "")

                if filter_str and (filter_str not in name.upper() and filter_str not in desc.upper()):
                    continue

                arguments = func_data.get("Arguments") or ()
                arg_list = []
                for a in arguments:
                    arg_list.append({
                        "name": getattr(a, "Name", ""),
                        "description": getattr(a, "Description", ""),
                        "optional": getattr(a, "IsOptional", False)
                    })

                matched_functions.append({
                    "name": name,
                    "description": func_data.get("Description", ""),
                    "category_id": func_data.get("Category", 0),
                    "arguments": arg_list,
                })
            logger.info("Found %d matching Calc functions for filter '%s'", len(matched_functions), filter_str)
            return {"status": "ok", "functions": matched_functions}
        except Exception as e:
            logger.error("Error listing Calc functions: %s", str(e))
            raise ToolExecutionError(f"Error listing Calc functions: {str(e)}") from e


class EvaluateFormula(ToolCalcErrorBase):
    """Pre-evaluate a spreadsheet formula on a temporary worksheet copy without side effects."""

    name = "evaluate_formula"
    description = "Evaluates a Calc formula on a temporary duplicate sheet and returns the result or error, without modifying the active sheets."
    parameters = {
        "type": "object",
        "properties": {
            "formula": {
                "type": "string",
                "description": "The formula to evaluate, e.g. '=SUM(A1:B2)' or '=A1*1.1'."
            },
            "cell": {
                "type": "string",
                "description": "Optional cell coordinate/address context to evaluate relative references from, e.g. 'C5' (defaults to 'A1')."
            }
        },
        "required": ["formula"]
    }
    uno_services = ["com.sun.star.sheet.SpreadsheetDocument"]
    tier = "specialized"
    is_mutation = False

    def execute(self, ctx, **kwargs):
        formula_string = kwargs.get("formula", "").strip()
        cell_address = kwargs.get("cell", "A1").strip()
        if not formula_string:
            return self._tool_error("formula is required")
        if not formula_string.startswith("="):
            formula_string = "=" + formula_string

        doc = ctx.doc
        if not doc:
            raise ToolExecutionError("Document model not available.")

        try:
            sheets = doc.getSheets()
        except Exception as e:
            raise ToolExecutionError(f"Failed to get sheets: {str(e)}") from e

        # Resolve the active sheet to copy from
        try:
            active_sheet = doc.getCurrentController().getActiveSheet()
            active_name = active_sheet.getName()
        except Exception as e:
            raise ToolExecutionError(f"Failed to get active sheet: {str(e)}") from e

        temp_sheet_name = f"__wa_eval_copy_{int(datetime.datetime.now().timestamp())}__"

        # Ensure name uniqueness
        counter = 0
        while sheets.hasByName(temp_sheet_name):
            counter += 1
            temp_sheet_name = f"__wa_eval_copy_{int(datetime.datetime.now().timestamp())}_{counter}__"

        try:
            # Copy active sheet with all its data
            sheets.copyByName(active_name, temp_sheet_name, sheets.getCount())
            sheet = sheets.getByName(temp_sheet_name)
            
            # Retrieve the cell by coordinates context
            try:
                cell_range = sheet.getCellRangeByName(cell_address)
                cell = cell_range.getCellByPosition(0, 0)
            except Exception as e:
                return self._tool_error(f"Invalid cell context address '{cell_address}': {str(e)}")

            cell.setFormula(formula_string)

            error_code = cell.Error
            if error_code != 0:
                error_msg = f"Formula evaluation error: code {error_code}"
                if error_code in (503, 532):
                    error_msg = "Formula evaluation error: #DIV/0! (Division by zero, code 532)"
                elif error_code == 508:
                    error_msg = "Formula evaluation error: Pair missing bracket (code 508)"
                elif error_code == 509:
                    error_msg = "Formula evaluation error: Operator missing (code 509)"
                elif error_code == 510:
                    error_msg = "Formula evaluation error: Variable missing (code 510)"
                elif error_code == 511:
                    error_msg = "Formula evaluation error: Parameter missing (code 511)"
                elif error_code == 524:
                    error_msg = "Formula evaluation error: #REF! (Invalid reference, code 524)"
                elif error_code == 525:
                    error_msg = "Formula evaluation error: #NAME? (Invalid name, code 525)"
                return {"status": "error", "error_code": error_code, "message": error_msg}

            result_type = cell.getType()
            if result_type == VALUE:
                result = cell.getValue()
            elif result_type == TEXT:
                result = cell.getString()
            elif result_type == FORMULA:
                result = cell.getValue() if cell.getValue() != 0 else cell.getString()
            else:
                result = cell.getString()

            if result_type == EMPTY:
                result_type_str = "empty"
            elif result_type == VALUE:
                result_type_str = "value"
            elif result_type == TEXT:
                result_type_str = "text"
            elif result_type == FORMULA:
                result_type_str = "formula"
            else:
                result_type_str = "unknown"

            return {
                "status": "ok",
                "formula": formula_string,
                "result": result,
                "result_type": result_type_str
            }
        except Exception as e:
            logger.error("Formula evaluation failed: %s", str(e))
            raise ToolExecutionError(f"Formula evaluation failed: {str(e)}") from e
        finally:
            try:
                if sheets.hasByName(temp_sheet_name):
                    sheets.removeByName(temp_sheet_name)
            except Exception as cleanup_err:
                logger.error("Failed to cleanup evaluation sheet: %s", cleanup_err)
