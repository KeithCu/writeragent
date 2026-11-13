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

This module also owns the shared data-tool ladder (``CalcDataToolArgs``,
``run_calc_data_tool``, ``write_anchor_result``) used by ``analyze_data``,
``forecast_data``, ``optimize_data``, and ``plot_data``.

Chat/MCP: these classes are ToolBaseDummy (not registered). Use =PY() or domain=python.
"""

from __future__ import annotations

import logging
from functools import partial
from typing import TYPE_CHECKING, Any, NamedTuple

from plugin.framework.errors import (
    ToolExecutionError,
    reraise_if_disposed,
    uno_read,
)
from plugin.framework.tool import ToolBaseDummy
from plugin.framework.uno_context import enum_has_more
from plugin.calc.address_utils import parse_address, split_sheet_prefix
from plugin.calc.calc_utils import resolve_sheet_and_cell
from plugin.scripting.helper_domain import run_domain
from plugin.scripting.helper_registry import names


if TYPE_CHECKING:
    from plugin.framework.tool import ToolContext

log = logging.getLogger(__name__)


_GENERIC_SOLVER_SERVICE = "com.sun.star.sheet.Solver"

# Prefer non-Java solvers first so hidden Calc documents (no frame/controller) do not hit
# NLPSolver engines that open status dialogs (see docs/calc/analysis-tools.md).
_PREFERRED_SOLVER_SERVICES: tuple[str, ...] = ("com.sun.star.sheet.SolverLinear", "com.sun.star.comp.Calc.CoinMPSolver", "com.sun.star.comp.Calc.LpsolveSolver")


def _solver_impl_name(solver_obj: Any) -> str:
    if solver_obj is not None and hasattr(solver_obj, "getImplementationName"):
        return str(solver_obj.getImplementationName())
    return "unknown"


def _impl_name_is_java_nlp_headless_unsafe(impl_name: str) -> bool:
    """True for nlpsolver DEPS/SCO engines that need a UI frame (DEPSSolverImpl omits 'NLPSolver')."""
    return any(tok in impl_name for tok in ("NLPSolver", "DEPSSolver", "SCOSolver", "EvolutionarySolver", "BaseEvolutionary"))


def _user_requested_java_nlp_engine(engine_name: str | None) -> bool:
    if not engine_name or engine_name == _GENERIC_SOLVER_SERVICE:
        return False
    return any(tok in engine_name for tok in ("NLPSolver", "DEPS", "SCO"))


def _solver_is_usable(engine_name: str | None, solver: Any) -> bool:
    """Return True if solver is non-null and safe to run headless.

    Java NLP engines (DEPS, SCO) need a visible frame and open status dialogs,
    so they are used only when the caller named one.
    """
    return bool(solver) and (_user_requested_java_nlp_engine(engine_name) or not _impl_name_is_java_nlp_headless_unsafe(_solver_impl_name(solver)))


def pick_solver(smgr: Any, uno_ctx: Any, engine_name: str | None) -> tuple[Any, str] | None:
    """Select a usable Solver service and return ``(solver, engine_label)`` or ``None``.

    Tries the caller-named engine (unless generic), then native linear engines,
    then every other registered implementation via ``createContentEnumeration``,
    and finally the generic ``com.sun.star.sheet.Solver`` service. The generic
    service comes last because it resolves to the first registered
    implementation, which is a Java NLP engine on standard builds.
    """
    named = (engine_name,) if engine_name and engine_name != _GENERIC_SOLVER_SERVICE else ()
    for svc in (*named, *_PREFERRED_SOLVER_SERVICES):
        solver = uno_read(partial(smgr.createInstanceWithContext, svc, uno_ctx), None)
        if _solver_is_usable(engine_name, solver):
            return solver, svc
    # cppuhelper/source/servicemanager.cxx: ServiceManager::createContentEnumeration.
    # Java factories are skipped by name before instantiation so no JVM starts for them.
    factories = smgr.createContentEnumeration(_GENERIC_SOLVER_SERVICE)
    while factories is not None and enum_has_more(factories):
        factory = factories.nextElement()
        impl_name = _solver_impl_name(factory)
        if _impl_name_is_java_nlp_headless_unsafe(impl_name):
            continue
        solver = uno_read(partial(factory.createInstanceWithContext, uno_ctx), None)
        if _solver_is_usable(engine_name, solver):
            return solver, f"enumeration:{impl_name}"
    solver = uno_read(partial(smgr.createInstanceWithContext, _GENERIC_SOLVER_SERVICE, uno_ctx), None)
    if _solver_is_usable(engine_name, solver):
        return solver, _GENERIC_SOLVER_SERVICE
    return None


def resolve_cell_address(doc: Any, address: str) -> Any:
    """Convert a cell address string (e.g. 'A1' or 'Sheet1.A1') to a UNO CellAddress struct.

    Refuses a caller-passed address that cannot be resolved with ``INVALID_PARAM``.
    """
    resolved = resolve_sheet_and_cell(doc, address)
    if resolved is None:
        raise ToolExecutionError(f"Cannot resolve cell address '{address}'.", code="INVALID_PARAM")
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
    # ScModelObj implements XGoalSeek (sc/source/ui/unoobj/docuno.cxx ScModelObj::seekGoal),
    # and the registry matches this list when the class is wired.
    uno_services: list[str] | None = ["com.sun.star.sheet.SpreadsheetDocument"]

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        formula_str = kwargs["formula_cell"]
        variable_str = kwargs["variable_cell"]
        target_value = float(kwargs["target_value"])
        apply_result = kwargs.get("apply_result", True)

        doc = ctx.doc

        formula_addr = resolve_cell_address(doc, formula_str)
        variable_addr = resolve_cell_address(doc, variable_str)

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
    uno_services: list[str] | None = ["com.sun.star.sheet.SpreadsheetDocument"]

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        from com.sun.star.sheet import SolverConstraint
        from com.sun.star.sheet.SolverConstraintOperator import EQUAL, GREATER_EQUAL, LESS_EQUAL

        objective_str = kwargs["objective_cell"]
        variable_strs = kwargs["variables"]
        maximize = kwargs.get("maximize", True)
        constraints_raw = kwargs.get("constraints", [])
        engine_name = kwargs.get("engine", _GENERIC_SOLVER_SERVICE)

        doc = ctx.doc

        objective_addr = resolve_cell_address(doc, objective_str)
        variable_addrs = tuple(resolve_cell_address(doc, v) for v in variable_strs)

        picked = pick_solver(ctx.ctx.ServiceManager, ctx.ctx, engine_name)
        if picked is None:
            return self._tool_error("No Solver engine available in this LibreOffice installation")

        solver, engine_label = picked
        log.info("calc_solver: engine=%s implementation=%s", engine_label, _solver_impl_name(solver))

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
        # Hand-built on purpose: the solver ran and found no solution, and result.success
        # sits beside status as on the success branch, so a caller reads one shape.
        # A top-level field is allowed for this case.
        return {"status": "error", "message": "Solver failed to find a solution.", "result": {"success": False}}


_ANALYZE_DATA_HELPERS = ", ".join(sorted(names("analysis")))


class CalcDataToolArgs(NamedTuple):
    """Arguments shared by the four Calc data tools, normalized once."""

    helper: str
    data_range: str | None
    data: Any
    params: dict[str, Any] | None
    headers: bool
    task_hint: str | None
    output_range: str | None


def run_calc_data_tool(
    ctx: ToolContext,
    wiring_id: str,
    kwargs: dict[str, Any],
) -> tuple[CalcDataToolArgs, dict[str, Any]]:
    """Normalize shared data-tool arguments, enforce range-only domains, and run the helper.

    Runs on the calling worker. ``run_domain`` marshals only the sheet read to
    the main thread, and the venv IPC must stay off the UI thread or Calc
    freezes. ``run_domain`` refuses a blank or unknown helper, or missing data,
    with the domain's error code. Nothing here catches exceptions: disposal and
    other failures reach ``execute_safe`` once the tool is wired.
    """
    data_range = kwargs.get("data_range")
    args = CalcDataToolArgs(
        helper=str(kwargs.get("helper") or "").strip(),
        data_range=str(data_range).strip() if data_range else None,
        data=kwargs.get("data"),
        params=kwargs.get("params") if isinstance(kwargs.get("params"), dict) else None,
        headers=bool(kwargs.get("headers", True)),
        task_hint=str(kwargs["task_hint"]) if kwargs.get("task_hint") else None,
        output_range=str(kwargs["output_range"]).strip() if kwargs.get("output_range") else None,
    )
    if args.data is not None and ctx.active_domain in ("analysis", wiring_id):
        raise ToolExecutionError(
            f"The {ctx.active_domain} domain takes data_range (an A1 address string) only. "
            "Do not pass raw data values; the host reads the range out-of-band.",
            code="INVALID_PARAM",
        )
    result = run_domain(
        wiring_id,
        ctx.ctx,
        ctx.doc,
        helper=args.helper,
        params=args.params,
        data_range=args.data_range,
        data=args.data,
        headers=args.headers,
        task_hint=args.task_hint,
    )
    return args, result


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
    output_range: str | None,
    insert_fn: Any,
) -> dict[str, Any]:
    """Write result to output anchor; no anchor, or a failed run, writes nothing.

    Records non-disposal write errors on the result and propagates disposal.
    """
    if not output_range or result.get("status") != "ok":
        return result
    anchor = output_range
    from plugin.framework.queue_executor import main

    def _write() -> None:
        sheet, col, row = parse_output_anchor(anchor)
        insert_fn(ctx.doc, ctx.ctx, result, sheet_name=sheet, start_col=col, start_row=row)

    try:
        main.call(_write)
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
        from plugin.calc.analysis_egress import insert_analysis_result_into_calc

        args, result = run_calc_data_tool(ctx, "analysis", kwargs)
        result = write_anchor_result(ctx, result, args.output_range, insert_analysis_result_into_calc)

        if result.get("status") == "ok" and ctx.doc_type == "calc":
            auto_plot = bool(kwargs.get("auto_plot", False))
            from plugin.calc.viz_auto_plot import apply_auto_plot_result, run_auto_plot_after_analysis, should_auto_plot

            plot_result = None
            if should_auto_plot(helper=args.helper, auto_plot=auto_plot, task_hint=args.task_hint):
                # Sub-agent worker thread: viz data reads use CalcBridge — marshal like plot_data.
                plot_result = run_auto_plot_after_analysis(
                    ctx.ctx,
                    ctx.doc,
                    analysis_helper=args.helper,
                    analysis_result=result,
                    analysis_params=args.params,
                    data_range=args.data_range,
                    task_hint=args.task_hint,
                )
            result = apply_auto_plot_result(result, plot_result, ctx.ctx, ctx.doc)

        return result
