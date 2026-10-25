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
"""Calc analysis tools: trusted numpy helpers, Goal Seek, and Solver.

Chat/MCP: these classes are ToolBaseDummy (not registered). Use =PY() or domain=python.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from plugin.framework.errors import (
    ToolExecutionError,
    UnoObjectError,
    is_disposed_exception,
    reraise_if_disposed,
    uno_probe,
)
from plugin.framework.tool import ToolBaseDummy
from plugin.calc.address_utils import parse_address, split_sheet_prefix
from plugin.calc.calc_utils import resolve_sheet_and_cell
from plugin.scripting.analysis import HELPER_NAMES


if TYPE_CHECKING:
    from plugin.framework.tool import ToolContext

log = logging.getLogger("writeragent.calc")


# Prefer non-Java solvers first so hidden Calc documents (no frame/controller) do not hit
# NLPSolver engines that open status dialogs (see docs/calc/analysis-tools.md).
_PREFERRED_SOLVER_SERVICES: tuple[str, ...] = ("com.sun.star.sheet.SolverLinear", "com.sun.star.comp.Calc.CoinMPSolver", "com.sun.star.comp.Calc.LpsolveSolver")


def _solver_impl_name(solver_obj: Any) -> str:
    if solver_obj is not None and hasattr(solver_obj, "getImplementationName"):
        return str(solver_obj.getImplementationName())
    return "unknown"


def _impl_name_is_java_nlp_headless_unsafe(impl_name: str) -> bool:
    """True for nlpsolver DEPS/SCO engines that need a UI frame (DEPSSolverImpl omits 'NLPSolver')."""
    if not impl_name:
        return False
    n = impl_name
    return "NLPSolver" in n or "DEPSSolver" in n or "SCOSolver" in n or "EvolutionarySolver" in n or "BaseEvolutionary" in n


def _user_requested_java_nlp_engine(engine_name: str | None) -> bool:
    if not engine_name or engine_name == "com.sun.star.sheet.Solver":
        return False
    en = engine_name
    return "NLPSolver" in en or "DEPS" in en or "SCO" in en


def _should_reject_solver_for_headless(engine_name: str | None, solver: Any) -> bool:
    """Drop instances that need a visible frame when user did not ask for a Java NLP engine."""
    if _user_requested_java_nlp_engine(engine_name):
        return False
    return _impl_name_is_java_nlp_headless_unsafe(_solver_impl_name(solver))


def resolve_cell_address(doc: Any, address: str) -> Any:
    """Convert a cell address string (e.g. 'A1' or 'Sheet1.A1') to a UNO CellAddress struct.

    Raises:
        UnoObjectError: When sheet cannot be found or address is invalid.
    """
    resolved = resolve_sheet_and_cell(doc, address)
    if resolved is None:
        raise UnoObjectError(f"Cannot resolve cell address '{address}'.")
    sheet, col, row = resolved
    cell = sheet.getCellByPosition(col, row)
    return cell.getCellAddress()


class GoalSeekTool(ToolBaseDummy):
    """Find the value of a variable cell that results in a target formula value."""

    name: str | None = "calc_goal_seek"
    description: str = "Finds the value for a variable cell that makes a formula cell reach a target value."
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "formula_cell": {"type": "string", "description": "Address of the formula cell (e.g. 'Sheet1.B1')."},
            "variable_cell": {"type": "string", "description": "Address of the variable cell to adjust (e.g. 'Sheet1.A1')."},
            "target_value": {"type": "number", "description": "The desired result of the formula."},
            "apply_result": {"type": "boolean", "description": "Whether to automatically apply the found result to the variable cell (default: true)."},
        },
        "required": ["formula_cell", "variable_cell", "target_value"],
    }
    is_mutation: bool | None = True

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        formula_str = kwargs["formula_cell"]
        variable_str = kwargs["variable_cell"]
        target_value = float(kwargs["target_value"])
        apply_result = kwargs.get("apply_result", True)

        try:
            doc = ctx.doc

            formula_addr = resolve_cell_address(doc, formula_str)
            variable_addr = resolve_cell_address(doc, variable_str)

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

            return {"status": "ok", "message": message, "result": {"value": result_val, "divergence": divergence}}
        except Exception as e:
            # Re-raise disposal before wrapping. Catching DisposedException
            # as ToolExecutionError makes execute_safe report
            # TOOL_EXECUTION_ERROR, and the native runner keeps going on a
            # dead document.
            if is_disposed_exception(e):
                raise
            raise ToolExecutionError(str(e)) from e


class SolverTool(ToolBaseDummy):
    """Solve an optimization problem with multiple variables and constraints."""

    name: str | None = "calc_solver"
    description: str = "Solves an optimization problem to maximize, minimize, or reach a value for an objective cell by changing multiple variable cells subject to constraints."
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "objective_cell": {"type": "string", "description": "Cell address of the objective function (e.g. 'Sheet1.C1')."},
            "variables": {"type": "array", "items": {"type": "string"}, "description": "List of cell addresses that the solver can change."},
            "maximize": {"type": "boolean", "description": "Whether to maximize (true) or minimize (false) the objective (default: true)."},
            "constraints": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "left": {"type": "string", "description": "Cell address for the left side of the constraint."},
                        "operator": {"type": "string", "enum": ["EQUAL", "GREATER_EQUAL", "LESS_EQUAL"], "description": "Comparison operator."},
                        "right": {"type": "string", "description": "A constant value or a cell address for the right side."},
                    },
                    "required": ["left", "operator", "right"],
                },
                "description": "List of constraints for the optimization.",
            },
            "engine": {"type": "string", "description": "Specific solver engine service name (e.g. 'com.sun.star.sheet.SolverLinear')."},
        },
        "required": ["objective_cell", "variables"],
    }
    is_mutation: bool | None = True

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        from com.sun.star.sheet import SolverConstraint
        from com.sun.star.sheet.SolverConstraintOperator import EQUAL, GREATER_EQUAL, LESS_EQUAL

        objective_str = kwargs["objective_cell"]
        variable_strs = kwargs["variables"]
        maximize = kwargs.get("maximize", True)
        constraints_raw = kwargs.get("constraints", [])
        engine_name = kwargs.get("engine", "com.sun.star.sheet.Solver")

        try:
            doc = ctx.doc

            objective_addr = resolve_cell_address(doc, objective_str)
            variable_addrs = tuple(resolve_cell_address(doc, v) for v in variable_strs)

            smgr = ctx.ctx.ServiceManager
            solver = None
            selected_engine_label: str | None = None

            # 1. User-specified concrete engine (not the generic Solver service name)
            if engine_name and engine_name != "com.sun.star.sheet.Solver":
                solver = uno_probe(lambda: smgr.createInstanceWithContext(engine_name, ctx.ctx), None)
                if solver and _should_reject_solver_for_headless(engine_name, solver):
                    solver = None
                if solver:
                    selected_engine_label = engine_name

            # 2. Prefer native / non-dialog solvers when using the default service name
            if not solver:
                for svc in _PREFERRED_SOLVER_SERVICES:
                    s = uno_probe(lambda: smgr.createInstanceWithContext(svc, ctx.ctx), None)
                    if not s:
                        continue
                    if _should_reject_solver_for_headless(engine_name, s):
                        continue
                    solver = s
                    selected_engine_label = svc
                    break

            # 3. Enumerate implementations (deprioritize DEPS/NLPSolver — see _priority)
            if not solver:
                enum_access = smgr.createInstanceWithContext("com.sun.star.container.XContentEnumerationAccess", ctx.ctx)
                if enum_access:
                    enum = enum_access.createContentEnumeration("com.sun.star.sheet.Solver")
                    if enum and enum.hasMoreElements():
                        impls = []
                        while enum.hasMoreElements():
                            el = enum.nextElement()
                            if hasattr(el, "createInstanceWithContext"):
                                impls.append(el)

                        def _priority(factory: Any) -> int:
                            name = ""
                            if hasattr(factory, "getImplementationName"):
                                name = factory.getImplementationName()
                            if _impl_name_is_java_nlp_headless_unsafe(name):
                                return 99
                            if "CoinMP" in name or "Lpsolve" in name:
                                return 0
                            return 1

                        impls.sort(key=_priority)

                        for el in impls:
                            impl_name = el.getImplementationName() if hasattr(el, "getImplementationName") else "unknown"
                            if _impl_name_is_java_nlp_headless_unsafe(impl_name):
                                continue
                            s = uno_probe(lambda: el.createInstanceWithContext(ctx.ctx), None)
                            if not s or _should_reject_solver_for_headless(engine_name, s):
                                continue
                            solver = s
                            selected_engine_label = f"enumeration:{impl_name}"
                            break

            # 4. Last ditch fallback to generic name
            if not solver:
                g = uno_probe(lambda: smgr.createInstanceWithContext("com.sun.star.sheet.Solver", ctx.ctx), None)
                if g and not _should_reject_solver_for_headless(engine_name, g):
                    solver = g
                    selected_engine_label = "com.sun.star.sheet.Solver"

            if not solver:
                return self._tool_error("No Solver engine available in this LibreOffice installation")

            log.info("calc_solver: engine=%s implementation=%s", selected_engine_label or "unknown", _solver_impl_name(solver))

            solver.Document = doc
            solver.Maximize = maximize
            solver.Objective = objective_addr
            solver.Variables = variable_addrs

            # Process constraints
            op_map = {"EQUAL": EQUAL, "GREATER_EQUAL": GREATER_EQUAL, "LESS_EQUAL": LESS_EQUAL}

            solver_constraints = []
            for c in constraints_raw:
                constraint = SolverConstraint()
                constraint.Left = resolve_cell_address(doc, c["left"])
                constraint.Operator = op_map[c["operator"]]

                right_val = c["right"]
                # Try to parse as float (constant), otherwise assume it's a cell address
                try:
                    constraint.Right = float(right_val)
                except ValueError:
                    constraint.Right = resolve_cell_address(doc, right_val)

                solver_constraints.append(constraint)

            solver.Constraints = tuple(solver_constraints)

            # Execute Solver
            solver.solve()

            if solver.Success:
                # The solution is already applied to the document by solver.solve()
                return {"status": "ok", "message": f"Solver success. Objective value: {solver.ResultValue}", "result": {"success": True, "result_value": solver.ResultValue, "solution": list(solver.Solution)}}
            else:
                return {"status": "error", "message": "Solver failed to find a solution.", "result": {"success": False}}

        except Exception as e:
            # Same dispose guard as Goal Seek: do not wrap a dead document.
            if is_disposed_exception(e):
                raise
            raise ToolExecutionError(str(e)) from e


_ANALYZE_DATA_HELPERS = ", ".join(sorted(HELPER_NAMES))


def extract_calc_data_tool_args(
    kwargs: dict[str, Any],
) -> tuple[str, str | None, Any, dict[str, Any] | None, bool, str | None, str | None]:
    """Extract and normalize common calc analysis tool arguments."""
    helper = str(kwargs.get("helper") or "").strip()
    data_range = kwargs.get("data_range")
    data = kwargs.get("data")
    dr = str(data_range).strip() if data_range else None
    params = kwargs.get("params") if isinstance(kwargs.get("params"), dict) else None
    headers = bool(kwargs.get("headers", True))
    task_hint = str(kwargs["task_hint"]) if kwargs.get("task_hint") else None
    output_range = str(kwargs["output_range"]).strip() if kwargs.get("output_range") else None
    return helper, dr, data, params, headers, task_hint, output_range


def parse_output_anchor(output_range: str) -> tuple[str | None, int, int]:
    """Sheet name (if any), column, and row where a generated report should start.

    ``split_sheet_prefix`` keeps quoted names, dots inside quotes, and both
    ``.`` and ``!``. A last-dot split would hand ``'Q1.Sales'!B2`` to
    ``parse_address`` still prefixed, and a range (``Sheet1.A1:Sheet1.C10``
    or ``$A$1:$C$5``) would resolve to the end cell or a token
    ``parse_address`` rejects. The write starts at the first cell, with
    ``$`` locks removed.
    """
    sheet_name, cell_part = split_sheet_prefix(output_range)
    anchor = cell_part.replace("$", "").split(":", 1)[0].strip()
    col, row = parse_address(anchor)
    return sheet_name, col, row


def write_anchor_result(
    ctx: ToolContext,
    result: dict[str, Any],
    output_range: str,
    insert_fn: Any,
) -> dict[str, Any]:
    """Write result to output anchor; record non-disposal write errors on the result."""
    from plugin.framework.queue_executor import execute_on_main_thread

    def _write() -> None:
        sheet, col, row = parse_output_anchor(output_range)
        insert_fn(ctx.doc, ctx.ctx, result, sheet_name=sheet, start_col=col, start_row=row)

    try:
        execute_on_main_thread(_write)
        return result
    except Exception as exc:
        # Propagate document disposal; ordinary write errors are recorded on the result.
        reraise_if_disposed(exc, object_type="calc")
        res = dict(result)
        res["sheet_write_error"] = str(exc)
        return res


class AnalyzeDataTool(ToolBaseDummy):
    """Run trusted numpy/pandas analysis helpers on sheet data via the venv worker."""

    name: str | None = "analyze_data"
    description: str = (
        "Run a trusted numpy/pandas analysis helper on spreadsheet data. "
        f"Helpers: {_ANALYZE_DATA_HELPERS}. "
        "Use data_range (A1 address string, e.g. 'Sheet1.A1:D1000') for bulk data. "
        "The host extracts and shapes the data (via split_grid) before it reaches the analysis code. "
        "This tool is intended for the analysis specialized domain; pass range addresses only."
    )
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "helper": {"type": "string", "description": "Analysis helper name (e.g. describe_data, run_regression)."},
            "params": {"type": "object", "description": "Helper-specific parameters."},
            "data_range": {"type": "string", "description": "A1 range address (e.g. 'Sheet1.A1:D1000'). This is the only way to supply data when using the analysis domain. The host extracts the values out-of-band."},
            "output_range": {"type": "string", "description": "Optional A1 anchor cell to write formatted results (Calc only)."},
            "headers": {"type": "boolean", "description": "First row contains column names (default true)."},
            "task_hint": {"type": "string", "description": "Optional hint echoed in result context."},
            "auto_plot": {"type": "boolean", "description": "When true (or when task_hint mentions charts/plots), run a matching viz helper after successful analysis and insert the chart on Calc."},
        },
        "required": ["helper"],
    }
    long_running: bool = True

    def is_async(self) -> bool:
        return True

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        helper, dr, data, params, headers, task_hint, output_range = extract_calc_data_tool_args(kwargs)

        # Strict enforcement for the analysis domain (see docs/calc/analysis-sub-agent.md "Data Handoff").
        if getattr(ctx, "active_domain", None) == "analysis" and data is not None:
            return self._tool_error("analysis domain requires data_range (A1 address string) only. Do not pass raw data values — the host must resolve the range out-of-band.")

        from plugin.calc.analysis_runner import run_trusted_analysis
        from plugin.calc.analysis_egress import insert_analysis_result_into_calc

        try:
            result = run_trusted_analysis(ctx.ctx, ctx.doc, helper=helper, params=params, data_range=dr, data=data, headers=headers, task_hint=task_hint)
        except ToolExecutionError as exc:
            return self._tool_error(str(exc), code=getattr(exc, "code", "ANALYSIS_ERROR"))
        except Exception as exc:
            return self._tool_error(f"Failed to run analysis: {exc}")

        if output_range and result.get("status") == "ok":
            result = write_anchor_result(ctx, result, output_range, insert_analysis_result_into_calc)

        if result.get("status") == "ok" and ctx.doc_type == "calc":
            auto_plot = bool(kwargs.get("auto_plot", False))
            from plugin.calc.viz_auto_plot import apply_auto_plot_result, run_auto_plot_after_analysis, should_auto_plot
            from plugin.scripting.viz import insert_viz_result_into_doc

            plot_result = None
            if should_auto_plot(helper=helper, auto_plot=auto_plot, task_hint=task_hint):
                # Sub-agent worker thread: viz data reads use CalcBridge — marshal like plot_data.
                plot_result = run_auto_plot_after_analysis(ctx.ctx, ctx.doc, analysis_helper=helper, analysis_result=result, analysis_params=params, data_range=dr, auto_plot=auto_plot, task_hint=task_hint)
            result = apply_auto_plot_result(result, plot_result, lambda pr: insert_viz_result_into_doc(ctx.ctx, ctx.doc, pr))

        return result
