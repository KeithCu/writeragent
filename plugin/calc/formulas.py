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
> writes the formula at a bare cell coordinate on that copy (a sheet prefix is not followed, and a
> defined name is rejected), and deletes the copied sheet in a `finally` block before returning.
"""

from __future__ import annotations

from plugin.framework.constants import now_aware
import logging
from typing import TYPE_CHECKING, Any

from plugin.calc.address_utils import parse_address, split_sheet_prefix
from plugin.calc.calc_utils import (
    EMPTY as EMPTY,
    FORMULA as FORMULA,
    TEXT as TEXT,
    VALUE as VALUE,
    cell_value_and_type,
    resolve_sheet,
)
from plugin.calc.error_detector import formula_evaluation_error_message as formula_evaluation_error_message
from plugin.framework.errors import ToolExecutionError, is_disposed_exception
from plugin.framework.tool import ToolBase

if TYPE_CHECKING:
    from collections.abc import Callable
    from plugin.framework.tool import ToolContext

from plugin.calc.base import ToolCalcErrorBase

log = logging.getLogger("writeragent.calc")


def _context_position(cell_address: str) -> tuple[int, int]:
    """Bare column and row for the formula context on the temporary sheet.

    ``split_sheet_prefix`` drops the sheet (the copy is already the active
    sheet), ``parse_address`` rejects a name that is not one cell, and the
    caller writes with ``getCellByPosition``. ``getCellRangeByName`` is not
    limited to the sheet it is called on, so ``Sheet1.C5`` and a defined
    name hit the live document. The ``finally`` block only deletes the
    temporary copy.
    """
    _prefix, local = split_sheet_prefix((cell_address or "").strip())
    return parse_address(local.replace("$", ""))


def _quiet_cleanup(action: Callable[[], None], what: str) -> None:
    """Best-effort cleanup that still surfaces a disposed document."""
    try:
        action()
    except Exception as e:
        if is_disposed_exception(e):
            raise
        log.debug("%s failed during evaluation cleanup", what, exc_info=True)


class _EvaluationUndoGuard:
    """Isolate evaluation sheet copy from document undo history and modified state."""

    def __init__(self, doc: Any) -> None:
        self.doc: Any = doc
        self.was_modified: bool = False
        self.undo_mgr: Any = None
        self.was_possible: bool = False
        self.hidden_context_entered: bool = False
        self.sheet_removed: bool = False

    def __enter__(self) -> _EvaluationUndoGuard:
        doc = self.doc
        if hasattr(doc, "isModified"):
            try:
                self.was_modified = bool(doc.isModified())
            except Exception as e:
                if is_disposed_exception(e):
                    raise
        if hasattr(doc, "getUndoManager"):
            self.undo_mgr = doc.getUndoManager()
        if self.undo_mgr:
            try:
                self.was_possible = bool(self.undo_mgr.isUndoPossible())
            except Exception as e:
                if is_disposed_exception(e):
                    raise
            if self.was_possible:
                try:
                    self.undo_mgr.enterHiddenUndoContext()
                    self.hidden_context_entered = True
                except Exception as e:
                    if is_disposed_exception(e):
                        raise
                    if "EmptyUndoStackException" not in type(e).__name__:
                        raise
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        if self.hidden_context_entered and self.undo_mgr:
            _quiet_cleanup(self.undo_mgr.leaveUndoContext, "leaveUndoContext")
        elif self.undo_mgr and not self.was_possible and self.sheet_removed:
            _quiet_cleanup(self.undo_mgr.clear, "clear")

        if self.sheet_removed and not self.was_modified:
            _quiet_cleanup(lambda: self.doc.setModified(False), "setModified")


class ListCalcFunctions(ToolBase):
    """Retrieve available spreadsheet functions inside LibreOffice Calc."""

    name: str | None = "list_calc_functions"
    description: str = "Lists available Calc spreadsheet functions. Use the 'filter' parameter to perform a case-insensitive search for a partial substring anywhere in function names or descriptions to avoid context window bloat."
    parameters: dict[str, Any] | None = {"type": "object", "properties": {"filter": {"type": "string", "description": "Optional substring to filter functions by (case-insensitive search anywhere in the name or description)."}}, "required": []}
    uno_services: list[str] | None = ["com.sun.star.sheet.SpreadsheetDocument"]
    tier: str = "core"
    is_mutation: bool | None = False

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        filter_str = (kwargs.get("filter") or "").strip().upper()
        uno_ctx = ctx.ctx
        if not uno_ctx:
            raise ToolExecutionError("UNO context not available.")

        smgr = uno_ctx.getServiceManager()
        func_descr_service = smgr.createInstanceWithContext("com.sun.star.sheet.FunctionDescriptions", uno_ctx)
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
            arg_list = [
                {"name": getattr(a, "Name", ""), "description": getattr(a, "Description", ""), "optional": getattr(a, "IsOptional", False)}
                for a in arguments
            ]

            matched_functions.append({"name": name, "description": desc, "category_id": func_data.get("Category", 0), "arguments": arg_list})
        log.info("Found %d matching Calc functions for filter '%s'", len(matched_functions), filter_str)
        return {"status": "ok", "functions": matched_functions}


class EvaluateFormula(ToolCalcErrorBase):
    """Pre-evaluate a spreadsheet formula on a temporary worksheet copy without side effects."""

    name: str | None = "evaluate_formula"
    description: str = "Evaluates a Calc formula on a temporary duplicate sheet and returns the result or error, without modifying the active sheets."
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {"formula": {"type": "string", "description": "The formula to evaluate, e.g. '=SUM(A1:B2)' or '=A1*1.1'."}, "cell": {"type": "string", "description": "Optional bare cell on the copied active sheet whose coordinate is the formula context, e.g. 'C5' (defaults to 'A1'). A sheet prefix is ignored. A defined name is rejected."}},
        "required": ["formula"],
    }
    uno_services: list[str] | None = ["com.sun.star.sheet.SpreadsheetDocument"]
    tier: str = "specialized"
    is_mutation: bool | None = False

    def requires_document_lock(self, arguments: Any = None) -> bool:
        return True

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        formula_string = (kwargs.get("formula") or "").strip()
        cell_address = (kwargs.get("cell") or "A1").strip()
        if not formula_string:
            return self._tool_error("formula is required")
        if not formula_string.startswith("="):
            formula_string = "=" + formula_string

        # Reject a defined name before copying a sheet. A sheet prefix is
        # dropped here; only the bare coordinate is used below.
        try:
            col, row = _context_position(cell_address)
        except ValueError as e:
            return self._tool_error(f"Invalid cell context address '{cell_address}': {e}")

        doc = ctx.doc
        if not doc:
            raise ToolExecutionError("Document model not available.")

        sheets = doc.getSheets()

        # Resolve the active sheet to copy from
        active_sheet = resolve_sheet(doc)
        active_name = active_sheet.getName()

        temp_sheet_name = f"__wa_eval_copy_{int(now_aware().timestamp())}_0__"

        # Ensure name uniqueness
        counter = 0
        while sheets.hasByName(temp_sheet_name):
            counter += 1
            temp_sheet_name = f"__wa_eval_copy_{int(now_aware().timestamp())}_{counter}__"

        eval_result: dict[str, Any] | None = None
        sheet_copied = False
        cleanup_failed = False
        eval_exc: Exception | None = None

        with _EvaluationUndoGuard(doc) as undo_guard:
            try:
                # Copy active sheet with all its data
                sheets.copyByName(active_name, temp_sheet_name, sheets.getCount())
                sheet_copied = True
                sheet = sheets.getByName(temp_sheet_name)

                # getCellByPosition stays on this sheet. getCellRangeByName would
                # follow Sheet1.C5 or a defined name back to the live workbook.
                try:
                    cell = sheet.getCellByPosition(col, row)
                except Exception as e:
                    eval_result = self._tool_error(f"Invalid cell context address '{cell_address}': {str(e)}")
                    return eval_result

                try:
                    cell.setFormula(formula_string)
                except Exception as e:
                    if is_disposed_exception(e):
                        raise
                    eval_result = self._tool_error(f"Failed to set formula '{formula_string}' at cell '{cell_address}': {str(e)}")
                    return eval_result

                error_code = cell.Error
                if error_code != 0:
                    eval_result = {"status": "error", "error_code": error_code, "message": formula_evaluation_error_message(error_code)}
                    return eval_result

                result, result_type_str = cell_value_and_type(
                    cell, formula_error_as_string=False, empty_as_none=False
                )

                eval_result = {"status": "ok", "formula": formula_string, "result": result, "result_type": result_type_str}
                return eval_result
            except Exception as exc:
                # Held until cleanup runs; a cleanup failure is reported instead.
                eval_exc = exc
            finally:
                if sheet_copied:
                    try:
                        if sheets.hasByName(temp_sheet_name):
                            sheets.removeByName(temp_sheet_name)
                        undo_guard.sheet_removed = True
                    except Exception:
                        cleanup_failed = True
                        log.exception("Failed to cleanup evaluation sheet")
                        # No return in finally; the cleanup_failed check below reports it.
                        if eval_result is not None:
                            eval_result["warning"] = f"Failed to clean up temporary evaluation sheet '{temp_sheet_name}'"

        if cleanup_failed and eval_result is None:
            return self._tool_error(f"Failed to clean up temporary evaluation sheet '{temp_sheet_name}'")
        if eval_exc is not None:
            raise eval_exc

        if eval_result is not None:
            return eval_result
        return self._tool_error("Failed to evaluate formula")
