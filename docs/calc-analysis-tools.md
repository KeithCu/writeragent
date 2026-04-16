# Calc Analysis Tools (Goal Seek & Solver)

This document describes the specialized tools for performing analysis in LibreOffice Calc, specifically **Goal Seek** and **Solver**. These tools are part of the `analysis` specialized domain.

## 1. Goal Seek

Goal Seek is used to find the value of a single variable that results in a specific target value for a formula.

### Tool: `calc_goal_seek`
Finds the value for a variable cell that makes a formula cell reach a target value.

**Arguments:**
* `formula_cell`: The address of the cell containing the formula (e.g., "Sheet1.B1").
* `variable_cell`: The address of the cell containing the variable to adjust (e.g., "Sheet1.A1").
* `target_value`: The desired result of the formula (float).
* `apply_result`: (Optional, default: `true`) Whether to automatically apply the found result to the variable cell.

**Returns:**
* `result`: The value found for the variable cell.
* `divergence`: The difference between the target and the actual result achieved.

---

## 2. Solver

The Solver is used for more complex optimization problems involving multiple variables and constraints.

### Tool: `calc_solver`
Solves an optimization problem to maximize, minimize, or reach a value for an objective cell by changing multiple variable cells subject to constraints.

**Arguments:**
* `objective_cell`: The cell address of the objective function.
* `variables`: A list of cell addresses that the solver can change.
* `maximize`: (Optional, default: `true`) Whether to maximize (`true`) or minimize (`false`) the objective.
* `constraints`: A list of constraint objects:
    * `left`: Cell address for the left side of the constraint.
    * `operator`: One of `"EQUAL"`, `"GREATER_EQUAL"`, `"LESS_EQUAL"`.
    * `right`: A constant value or a cell address (as a string or float).
* `engine`: (Optional) The specific solver engine to use (e.g., `"com.sun.star.sheet.SolverLinear"`).

**Returns:**
* `success`: Whether a solution was found.
* `result_value`: The final value of the objective cell.
* `solution`: A list of values for the variables in the same order as provided.

---

## 3. Implementation Details

- **Goal Seek**: Uses the `com.sun.star.sheet.XGoalSeek` interface, which is implemented directly by the Spreadsheet Document model. This allows for efficient root-finding without external service overhead.
- **Solver**:
    - **Engine Enumeration**: The tool automatically discovers all registered solver implementations using `XContentEnumerationAccess` for the `com.sun.star.sheet.Solver` service.
    - **Prioritization**: In headless or restricted environments, the tool prioritizes non-Java engines (like `CoinMP` or `Lpsolve`) over Java-based ones (like `NLPSolver`) to avoid `NullPointerException` errors related to frame/controller access.
    - **Auto-Discovery**: If no `engine` is specified, it iterates through available implementations until a compatible one is found.

## 4. Environment Notes

- **Headless Mode**: The Evolutionary/NLP solvers in LibreOffice often require an active controller/frame to display status dialogs, which can lead to Java `NullPointerException` errors when run in headless tests or background tasks. WriterAgent's prioritization of native linear solvers mitigates this.
- **Goal Seek Accuracy**: Goal Seek returns both the found `result` and the `divergence`. A non-zero divergence indicates that the target value was approached but not exactly met within the defined precision.

## 5. Example Usage

### Goal Seek
"Find what value in A1 makes B1 (which is A1*A1) equal to 100."
- `formula_cell`: "Sheet1.B1"
- `variable_cell`: "Sheet1.A1"
- `target_value`: 100.0

### Solver
"Maximize C1 (Profit) by changing A1 and B1, subject to A1+B1 <= 10."
- `objective_cell`: "Sheet1.C1"
- `variables`: ["Sheet1.A1", "Sheet1.B1"]
- `constraints`: [{"left": "Sheet1.D1", "operator": "LESS_EQUAL", "right": 10.0}] (where D1 is `=A1+B1`)
