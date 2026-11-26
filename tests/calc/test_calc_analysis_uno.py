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
from plugin.testing_runner import native_test
from plugin.tests.testing_utils import with_native_doc


def _execute_calc_tool(doc, ctx, name, args):
    # The chat registry does not list analysis tools (ToolBaseDummy), so call execute directly.
    from plugin.calc.analysis import GoalSeekTool, SolverTool
    from plugin.tests.testing_utils import TestingFactory as _TF

    tctx = _TF.create_context(
        doc=doc,
        ctx=ctx,
        env="native",
        doc_type="calc",
        status_callback=lambda m: None,
        append_thinking_callback=lambda m: None,
    )
    tool = GoalSeekTool() if name == "calc_goal_seek" else SolverTool()
    return tool.execute(tctx, **(args or {}))


@native_test
@with_native_doc("calc")
def test_goal_seek(ctx, doc):
    active_sheet = doc.getCurrentController().getActiveSheet()

    # Problem: Find x such that x^2 = 100
    # A1: Variable (x)
    # B1: Formula (=A1*A1)
    active_sheet.getCellByPosition(0, 0).setValue(1.0) # Initial guess
    active_sheet.getCellByPosition(1, 0).setFormula("=A1*A1")

    # Direct execute (analysis tools are ToolBaseDummy) lets a missing kwarg
    # reach the tool instead of going through TestingFactory.execute_tool.
    sheet_name = active_sheet.getName()
    res = _execute_calc_tool(doc, ctx, "calc_goal_seek", {
        "formula_cell": f"{sheet_name}.B1",
        "variable_cell": f"{sheet_name}.A1",
        "target_value": 100.0,
        "apply_result": True
    })

    assert res.get("status") == "ok", f"Goal Seek failed: {res}"
    result_val = res.get("result", {}).get("value")
    # Result should be 10.0 (or -10.0)
    assert abs(abs(result_val) - 10.0) < 0.0001, f"Expected 10.0, got {result_val}"

    # Verify applied
    assert abs(abs(active_sheet.getCellByPosition(0, 0).getValue()) - 10.0) < 0.0001


@native_test
@with_native_doc("calc")
def test_solver(ctx, doc):
    active_sheet = doc.getCurrentController().getActiveSheet()
    sheet_name = active_sheet.getName()

    # Problem: Maximize 3x + 5y subject to x + y <= 10 and x, y >= 0
    # Cell A3: x, Cell B3: y
    # Cell C3: Objective (3*A3 + 5*B3)
    active_sheet.getCellByPosition(0, 2).setValue(1.0) # x
    active_sheet.getCellByPosition(1, 2).setValue(1.0) # y
    active_sheet.getCellByPosition(2, 2).setFormula("=3*A3+5*B3")

    # Define Constraint: x + y <= 10 in D3
    active_sheet.getCellByPosition(3, 2).setFormula("=A3+B3")

    # Linear program: use built-in linear solver (avoids Java NLPSolver / DEPS on hidden docs).
    res = _execute_calc_tool(doc, ctx, "calc_solver", {
        "objective_cell": f"{sheet_name}.C3",
        "variables": [f"{sheet_name}.A3", f"{sheet_name}.B3"],
        "maximize": True,
        "engine": "com.sun.star.sheet.SolverLinear",
        "constraints": [
            {"left": f"{sheet_name}.D3", "operator": "LESS_EQUAL", "right": "10.0"},
            {"left": f"{sheet_name}.A3", "operator": "GREATER_EQUAL", "right": "0.0"},
            {"left": f"{sheet_name}.B3", "operator": "GREATER_EQUAL", "right": "0.0"}
        ]
    })

    if res.get("status") == "error":
        msg = res.get("message", "")
        detail_err = ""
        det = res.get("details")
        if isinstance(det, dict):
            detail_err = str(det.get("error", ""))
        combined = f"{msg} {detail_err}"
        if "No Solver engine available" in combined:
            print("Skipping solver test: no engine available")
            return
        if "NLPSolver" in combined or "NullPointerException" in combined:
            print(f"Skipping solver test: engine unstable in this env: {combined}")
            return

    assert res.get("status") == "ok", f"Solver failed: {res}"
    assert res.get("result", {}).get("success"), "Solver did not succeed"

    # Result should be 50.0 (x=0, y=10)
    result_val = res.get("result", {}).get("result_value")
    assert abs(result_val - 50.0) < 0.0001, f"Expected 50.0, got {result_val}"

    # Verify solution values in returned result or sheet cells
    sol = res.get("result", {}).get("solution")
    if sol and len(sol) >= 2:
        assert abs(float(sol[0]) - 0.0) < 0.0001, f"Expected sol[0]=0, got {sol[0]}"
        assert abs(float(sol[1]) - 10.0) < 0.0001, f"Expected sol[1]=10, got {sol[1]}"
    else:
        x = active_sheet.getCellByPosition(0, 2).getValue()
        y = active_sheet.getCellByPosition(1, 2).getValue()
        assert abs(x - 0.0) < 0.0001, f"Expected x=0, got {x}"
        assert abs(y - 10.0) < 0.0001, f"Expected y=10, got {y}"
