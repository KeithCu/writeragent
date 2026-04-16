# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
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
"""Calc analysis tools: Goal Seek and Solver."""

import logging
from typing import Any

from plugin.framework.errors import ToolExecutionError, UnoObjectError
from plugin.modules.calc.base import ToolCalcAnalysisBase
from plugin.modules.calc.bridge import CalcBridge
from plugin.modules.calc.address_utils import parse_address

try:
    from com.sun.star.table import CellAddress

    UNO_AVAILABLE = True
except ImportError:
    UNO_AVAILABLE = False

logger = logging.getLogger("writeragent.calc")


def _get_cell_address(doc, address_str: str) -> "CellAddress":
    """Convert a cell address string (e.g. 'A1' or 'Sheet1.A1') to a CellAddress struct.

    Args:
        doc: The Calc document model.
        address_str: The address string.

    Returns:
        com.sun.star.table.CellAddress struct.
    """
    if not UNO_AVAILABLE:
        raise RuntimeError("UNO not available")

    # Split sheet and cell part
    if "." in address_str:
        sheet_part, cell_part = address_str.rsplit(".", 1)
        # Remove potential surrounding quotes from sheet name
        sheet_part = sheet_part.strip("'")
    else:
        sheet_part = None
        cell_part = address_str

    col, row = parse_address(cell_part)
    
    sheets = doc.getSheets()
    if sheet_part:
        if not sheets.hasByName(sheet_part):
            raise UnoObjectError(f"No sheet found named '{sheet_part}'.")
        sheet = sheets.getByName(sheet_part)
    else:
        # Fallback to active sheet
        controller = doc.getCurrentController()
        if hasattr(controller, "getActiveSheet"):
            sheet = controller.getActiveSheet()
        else:
            sheet = sheets.getByIndex(0)
    
    # Get sheet index. Sheet objects have a RangeAddress property which includes the sheet index.
    # Or we can get it via the index in the sheets collection if we search for it.
    # Actually, XCell has getCellAddress() if we fetch the cell first.
    cell = sheet.getCellByPosition(col, row)
    return cell.getCellAddress()


class GoalSeekTool(ToolCalcAnalysisBase):
    """Find the value of a variable cell that results in a target formula value."""

    name = "calc_goal_seek"
    description = (
        "Finds the value for a variable cell that makes a formula cell reach a target value."
    )
    parameters = {
        "type": "object",
        "properties": {
            "formula_cell": {
                "type": "string",
                "description": "Address of the formula cell (e.g. 'Sheet1.B1').",
            },
            "variable_cell": {
                "type": "string",
                "description": "Address of the variable cell to adjust (e.g. 'Sheet1.A1').",
            },
            "target_value": {
                "type": "number",
                "description": "The desired result of the formula.",
            },
            "apply_result": {
                "type": "boolean",
                "description": "Whether to automatically apply the found result to the variable cell (default: true).",
            },
        },
        "required": ["formula_cell", "variable_cell", "target_value"],
    }
    is_mutation = True

    def execute(self, ctx, **kwargs):
        if not UNO_AVAILABLE:
            return self._tool_error("UNO not available")

        formula_str = kwargs["formula_cell"]
        variable_str = kwargs["variable_cell"]
        target_value = float(kwargs["target_value"])
        apply_result = kwargs.get("apply_result", True)

        try:
            bridge = CalcBridge(ctx.doc)
            doc = bridge.get_active_document()
            
            formula_addr = _get_cell_address(doc, formula_str)
            variable_addr = _get_cell_address(doc, variable_str)
            
            # SpreadsheetDocument implements XGoalSeek directly
            if not hasattr(doc, "seekGoal"):
                 return self._tool_error("Document does not support Goal Seek")
            
            # seekGoal returns a GoalResult struct: {Result: float, Divergence: float}
            gs_result = doc.seekGoal(formula_addr, variable_addr, target_value)
            
            result_val = gs_result.Result
            divergence = gs_result.Divergence
            
            if apply_result:
                sheets = doc.getSheets()
                sheet = sheets.getByIndex(variable_addr.Sheet)
                cell = sheet.getCellByPosition(variable_addr.Column, variable_addr.Row)
                cell.setValue(result_val)
                message = f"Goal Seek success. Found result {result_val} and applied it to {variable_str}."
            else:
                message = f"Goal Seek success. Found result {result_val} for {variable_str}."

            return {
                "status": "ok",
                "message": message,
                "result": {
                    "value": result_val,
                    "divergence": divergence
                }
            }
        except Exception as e:
            logger.error("Goal Seek error: %s", str(e))
            raise ToolExecutionError(str(e)) from e


class SolverTool(ToolCalcAnalysisBase):
    """Solve an optimization problem with multiple variables and constraints."""

    name = "calc_solver"
    description = (
        "Solves an optimization problem to maximize, minimize, or reach a value "
        "for an objective cell by changing multiple variable cells subject to constraints."
    )
    parameters = {
        "type": "object",
        "properties": {
            "objective_cell": {
                "type": "string",
                "description": "Cell address of the objective function (e.g. 'Sheet1.C1').",
            },
            "variables": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of cell addresses that the solver can change.",
            },
            "maximize": {
                "type": "boolean",
                "description": "Whether to maximize (true) or minimize (false) the objective (default: true).",
            },
            "constraints": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "left": {"type": "string", "description": "Cell address for the left side of the constraint."},
                        "operator": {
                            "type": "string",
                            "enum": ["EQUAL", "GREATER_EQUAL", "LESS_EQUAL"],
                            "description": "Comparison operator.",
                        },
                        "right": {
                            "type": "string",
                            "description": "A constant value or a cell address for the right side.",
                        },
                    },
                    "required": ["left", "operator", "right"],
                },
                "description": "List of constraints for the optimization.",
            },
            "engine": {
                "type": "string",
                "description": "Specific solver engine service name (e.g. 'com.sun.star.sheet.SolverLinear').",
            },
        },
        "required": ["objective_cell", "variables"],
    }
    is_mutation = True

    def execute(self, ctx, **kwargs):
        if not UNO_AVAILABLE:
            return self._tool_error("UNO not available")

        from com.sun.star.sheet import SolverConstraint
        from com.sun.star.sheet.SolverConstraintOperator import EQUAL, GREATER_EQUAL, LESS_EQUAL

        objective_str = kwargs["objective_cell"]
        variable_strs = kwargs["variables"]
        maximize = kwargs.get("maximize", True)
        constraints_raw = kwargs.get("constraints", [])
        engine_name = kwargs.get("engine", "com.sun.star.sheet.Solver")

        try:
            bridge = CalcBridge(ctx.doc)
            doc = bridge.get_active_document()
            
            objective_addr = _get_cell_address(doc, objective_str)
            variable_addrs = tuple(_get_cell_address(doc, v) for v in variable_strs)
            
            smgr = ctx.ctx.ServiceManager
            solver = None

            # 1. Try specified engine first
            if engine_name and engine_name != "com.sun.star.sheet.Solver":
                solver = smgr.createInstanceWithContext(engine_name, ctx.ctx)

            # 2. If no specific engine or it failed, enumerate implementations
            if not solver:
                # Use XContentEnumerationAccess to find all services that implement Solver
                enum_access = smgr.createInstanceWithContext("com.sun.star.container.XContentEnumerationAccess", ctx.ctx)
                if enum_access:
                    enum = enum_access.createContentEnumeration("com.sun.star.sheet.Solver")
                    if enum and enum.hasMoreElements():
                        impls = []
                        while enum.hasMoreElements():
                            el = enum.nextElement()
                            if hasattr(el, "createInstanceWithContext"):
                                impls.append(el)
                        
                        # Sort to prioritize known non-Java solvers
                        # (Java solvers often start with com.sun.star.comp.Calc.NLPSolver)
                        def _priority(factory):
                            name = ""
                            if hasattr(factory, "getImplementationName"):
                                name = factory.getImplementationName()
                            # Prioritize CoinMP and Lpsolve
                            if "CoinMP" in name or "Lpsolve" in name:
                                return 0
                            if "NLPSolver" in name:
                                return 2
                            return 1
                        
                        impls.sort(key=_priority)

                        for el in impls:
                            try:
                                s = el.createInstanceWithContext(ctx.ctx)
                                if s:
                                    solver = s
                                    impl_name = "unknown"
                                    if hasattr(el, "getImplementationName"):
                                        impl_name = el.getImplementationName()
                                    logger.info("Found solver implementation: %s", impl_name)
                                    break
                            except Exception:
                                continue

            # 3. Last ditch fallback to generic name
            if not solver:
                solver = smgr.createInstanceWithContext("com.sun.star.sheet.Solver", ctx.ctx)

            if not solver:
                return self._tool_error("No Solver engine available in this LibreOffice installation")

            solver.Document = doc
            solver.Maximize = maximize
            solver.Objective = objective_addr
            solver.Variables = variable_addrs

            # Process constraints
            op_map = {
                "EQUAL": EQUAL,
                "GREATER_EQUAL": GREATER_EQUAL,
                "LESS_EQUAL": LESS_EQUAL
            }
            
            solver_constraints = []
            for c in constraints_raw:
                constraint = SolverConstraint()
                constraint.Left = _get_cell_address(doc, c["left"])
                constraint.Operator = op_map[c["operator"]]
                
                right_val = c["right"]
                # Try to parse as float (constant), otherwise assume it's a cell address
                try:
                    constraint.Right = float(right_val)
                except ValueError:
                    constraint.Right = _get_cell_address(doc, right_val)
                
                solver_constraints.append(constraint)
            
            solver.Constraints = tuple(solver_constraints)
            
            # Execute Solver
            solver.solve()
            
            if solver.Success:
                # The solution is already applied to the document by solver.solve()
                return {
                    "status": "ok",
                    "message": f"Solver success. Objective value: {solver.ResultValue}",
                    "result": {
                        "success": True,
                        "result_value": solver.ResultValue,
                        "solution": list(solver.Solution)
                    }
                }
            else:
                return {
                    "status": "error",
                    "message": "Solver failed to find a solution.",
                    "result": {"success": False}
                }
                
        except Exception as e:
            logger.error("Solver error: %s", str(e))
            raise ToolExecutionError(str(e)) from e
