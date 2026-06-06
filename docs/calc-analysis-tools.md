# Calc Analysis Tools (Trusted Helpers, Goal Seek, and Solver)

This document describes the specialized tools for performing analysis in LibreOffice Calc.

All tools below live under the **`analysis`** specialized domain. The main chat agent delegates with `delegate_to_specialized_calc_toolset(domain="analysis", task=…)`; the analysis sub-agent chooses the right tool:

| Task type | Tool |
|-----------|------|
| Stats, cleaning, regression, clustering, Monte Carlo on tabular data | `analyze_data` |
| Single-variable what-if on live formulas | `calc_goal_seek` |
| Constrained optimization on formula cells | `calc_solver` |

See [calc-specialized-toolsets.md](calc-specialized-toolsets.md) for delegation mechanics and [Analysis Sub-Agent](analysis-sub-agent.md) for the broader plan.

---

## 1. Trusted data analysis (`analyze_data`)

Runs curated numpy/pandas/scipy helpers in the user venv via a fixed RPC stub (not LLM-submitted code). Prefer this over inventing pandas code.

### Tool: `analyze_data`

**Arguments:**

* `helper` (required): Helper name — `describe_data`, `kpi_summary`, `detect_outliers`, `quick_stats`, `format_currency`, `format_percent`, `clean_and_prepare`, `pivot_aggregate`, `group_summary`, `compare_periods`, `correlation_matrix`, `run_regression`, `cluster_numeric`, `monte_carlo`
* `params`: Helper-specific parameters (object)
* `data_range`: A1 range to read from the sheet (e.g. `Sheet1.A1:D20`)
* `data`: 2D array alternative (e.g. from `read_cell_range`)
* `headers`: First row is column names (default `true`)
* `task_hint`: Optional string echoed in result context

**Returns:** Compact JSON with `status`, `helper`, `metrics`, `tables`, `flags`, etc. See [analysis-sub-agent.md](analysis-sub-agent.md) for the full result contract.

**Example:** "Describe the sales table in A1:C50."

```
helper: describe_data
data_range: Sheet1.A1:C50
```

---

## 2. Goal Seek

Goal Seek finds the value of a single variable that results in a specific target value for a formula.

### Tool: `calc_goal_seek`

**Arguments:**

* `formula_cell`: The address of the cell containing the formula (e.g., "Sheet1.B1").
* `variable_cell`: The address of the cell containing the variable to adjust (e.g., "Sheet1.A1").
* `target_value`: The desired result of the formula (float).
* `apply_result`: (Optional, default: `true`) Whether to automatically apply the found result to the variable cell.

**Returns:**

* `result`: The value found for the variable cell.
* `divergence`: The difference between the target and the actual result achieved.

---

## 3. Solver

The Solver is used for more complex optimization problems involving multiple variables and constraints.

### Tool: `calc_solver`

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

## 4. Implementation Details

- **`analyze_data`**: Host reads range via `CellInspector` → `analysis_client.run_analysis` → warm venv worker executing `plugin.scripting.analysis.run_analysis`.
- **Goal Seek**: Uses the `com.sun.star.sheet.XGoalSeek` interface on the Spreadsheet Document model.
- **Solver**:
    - **Engine Enumeration**: Discovers registered solver implementations via `XContentEnumerationAccess`.
    - **Prioritization**: In headless environments, prioritizes non-Java engines (CoinMP, Lpsolve) over Java NLPSolver engines that require a UI frame.
    - **Auto-Discovery**: If no `engine` is specified, iterates until a compatible engine is found.

## 5. Environment Notes

- **Headless Mode**: Evolutionary/NLP solvers often require an active controller/frame. WriterAgent prioritizes native linear solvers in headless tests.
- **Goal Seek Accuracy**: Returns both `result` and `divergence`; non-zero divergence means the target was approached but not met exactly.
- **Venv**: `analyze_data` requires a configured user Python venv with the scientific stack (see [enabling_numpy_in_libreoffice.md](enabling_numpy_in_libreoffice.md)).

## 6. Example Usage

### analyze_data
"Summarize outliers in the sales data range."

```
helper: detect_outliers
data_range: Sheet1.A1:C50
params: {"method": "iqr"}
```

### Goal Seek
"Find what value in A1 makes B1 (which is A1*A1) equal to 100."

```
formula_cell: Sheet1.B1
variable_cell: Sheet1.A1
target_value: 100.0
```

### Solver
"Maximize C1 (Profit) by changing A1 and B1, subject to A1+B1 <= 10."

```
objective_cell: Sheet1.C1
variables: ["Sheet1.A1", "Sheet1.B1"]
constraints: [{"left": "Sheet1.D1", "operator": "LESS_EQUAL", "right": 10.0}]
```
