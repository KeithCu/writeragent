# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Trusted venv optimize compute — runs in user venv worker."""

from __future__ import annotations

import logging
from typing import Any

from plugin.scripting.calc_functions_common import (
    OPTIMIZE_HELPER_NAMES as HELPER_NAMES,
)
from plugin.scripting.venv.coerce import (
    ok_result as _ok_result,
    error_result as _error_result,
    parse_trusted_spec as _parse_trusted_spec,
    numeric_columns as _numeric_columns,
    resolve_df as _resolve_df,
    table_from_df as _table_from_df,
)

log = logging.getLogger(__name__)


def _constraint_bounds(b: Any, n_constraints: int) -> Any:
    """One RHS per constraint column, or None when ``b`` cannot be aligned.

    ``b`` is a column on the same grid as the variables, so its length is the
    row count. That equals the constraint count only for a square tableau.
    The template default ``a_cols=["a1"]`` is one constraint on many variable
    rows: those rows repeat the same bound, and that number is the RHS.

    When the lengths differed, the old fallback transposed the coefficient
    matrix and set ``b_ub`` to zeros, discarding the user's bounds. ``linprog``
    then failed with ``OPTIMIZATION_FAILED``.
    """
    import numpy as np

    values = np.asarray(b, dtype=float).reshape(-1)
    if values.shape[0] == n_constraints:
        return values
    if n_constraints == 1 and values.size > 0 and bool(np.allclose(values, values[0])):
        return values[:1].copy()
    return None


def linear_programming(
    data: Any,
    *,
    c_col: str,
    a_cols: list[str],
    b_col: str,
    bounds: tuple[float | None, float | None] | None = (0, None),
    maximize: bool = False,
    headers: bool = True,
    header_row: int = 0,
    sheet_hint: str | None = None,
) -> dict[str, Any]:
    """Solve a linear program with ``scipy.optimize.linprog``.

    Each row is a variable (``c`` is its objective coefficient). Each name in
    ``a_cols`` is one ``A_ub x <= b`` constraint across those variables.
    ``b`` must supply one bound per constraint column. A single constraint
    column may repeat that same bound on every row.

    Future: Consider pulp for more complex formulations.
    """
    import numpy as np
    import pandas as pd
    from scipy import optimize as scipy_optimize

    coerced = _resolve_df(data, headers=headers, header_row=header_row, sheet_hint=sheet_hint)
    df = coerced.df.dropna(subset=[c_col, b_col] + a_cols)
    
    if df.empty:
        return _error_result("INSUFFICIENT_DATA", "No data for linear programming", helper="linear_programming")

    # Objective coefficients, one per variable row.
    c = df[c_col].values.astype(float)
    if maximize:
        c = -c

    # a-columns are constraints, so transpose to linprog's (constraints, variables).
    coeff = df[a_cols].values.astype(float)
    if getattr(coeff, "ndim", 0) != 2 or coeff.shape[1] == 0:
        return _error_result(
            "SHAPE_MISMATCH",
            "linear_programming needs at least one constraint column in a_cols.",
            helper="linear_programming",
        )
    n_constraints = int(coeff.shape[1])
    A_ub = coeff.T
    b_ub = _constraint_bounds(df[b_col].values.astype(float), n_constraints)
    if b_ub is None or A_ub.shape[1] != len(c) or A_ub.shape[0] != len(b_ub):
        return _error_result(
            "SHAPE_MISMATCH",
            (
                "linear_programming constraint shapes do not match: "
                f"{len(c)} variable rows and {n_constraints} constraint column(s) "
                f"({', '.join(str(name) for name in a_cols)}). "
                "Each row is a variable and each a-column is a <= constraint, so b "
                "needs one bound per constraint column. A single constraint column "
                "may repeat that same bound on every row. Mismatched b is rejected "
                "instead of being replaced with zeros."
            ),
            helper="linear_programming",
            details={
                "n_variables": int(len(c)),
                "n_constraints": n_constraints,
                "b_len": int(len(df)),
            },
        )

    try:
        res = scipy_optimize.linprog(c, A_ub=A_ub, b_ub=b_ub, bounds=(bounds,) * len(c) if bounds else None)
    except Exception as e:
        return _error_result("OPTIMIZATION_FAILED", str(e), helper="linear_programming")

    if not res.success:
        return _error_result("OPTIMIZATION_FAILED", res.message, helper="linear_programming")

    solution_df = pd.DataFrame({
        "variable_index": range(len(res.x)),
        "optimal_value": np.round(res.x, 4)
    })
    table = _table_from_df(solution_df, name="lp_solution")

    metrics = {
        "objective_value": float(-res.fun) if maximize else float(res.fun),
        "status": res.message,
        "iterations": int(res.nit)
    }

    return _ok_result("linear_programming", metrics=metrics, tables=[table], metadata=coerced.metadata)


def optimize_portfolio(
    data: Any,
    *,
    returns_col: list[str] | None = None,
    target_return: float | None = None,
    risk_free_rate: float = 0.0,
    headers: bool = True,
    header_row: int = 0,
    sheet_hint: str | None = None,
) -> dict[str, Any]:
    """Mean-variance portfolio optimization."""
    import numpy as np
    import pandas as pd
    from scipy import optimize as scipy_optimize

    coerced = _resolve_df(data, headers=headers, header_row=header_row, sheet_hint=sheet_hint)
    df = coerced.df
    
    try:
        numeric_cols = _numeric_columns(df, returns_col)
    except ValueError as exc:
        return _error_result("UNKNOWN_COLUMN", str(exc), helper="optimize_portfolio")

    if not numeric_cols or len(numeric_cols) < 2:
        return _error_result("INSUFFICIENT_DATA", "Need at least two numeric columns (assets).", helper="optimize_portfolio")

    returns = df[numeric_cols].astype(float)
    mean_returns = returns.mean().values
    cov_matrix = returns.cov().values
    num_assets = len(mean_returns)

    # Objective: Minimize portfolio variance
    def portfolio_variance(weights: Any) -> Any:
        return weights.T @ cov_matrix @ weights

    # Constraints: sum of weights = 1
    constraints: list[dict[str, Any]] = [
        {"type": "eq", "fun": lambda x: np.sum(x) - 1}
    ]

    # If target_return is specified, add it to constraints
    if target_return is not None:
        constraints.append({
            "type": "eq",
            "fun": lambda x: np.sum(mean_returns * x) - target_return
        })

    # Bounds: weights between 0 and 1 (no short selling)
    bounds = tuple((0.0, 1.0) for _unused in range(num_assets))
    
    # Initial guess: equal weighting
    init_guess = np.array(num_assets * [1.0 / num_assets])

    try:
        result = scipy_optimize.minimize(
            portfolio_variance,
            init_guess,
            method="SLSQP",
            bounds=bounds,
            constraints=constraints
        )
    except Exception as e:
        return _error_result("OPTIMIZATION_FAILED", str(e), helper="optimize_portfolio")

    if not result.success:
        return _error_result("OPTIMIZATION_FAILED", result.message, helper="optimize_portfolio")

    weights = np.round(result.x, 4)
    expected_return = np.sum(mean_returns * weights)
    expected_volatility = np.sqrt(result.fun)
    sharpe_ratio = (expected_return - risk_free_rate) / expected_volatility if expected_volatility > 0 else 0

    weights_df = pd.DataFrame({
        "asset": numeric_cols,
        "weight": weights
    })
    # Filter out near-zero weights
    weights_df = weights_df[weights_df["weight"] > 1e-4]
    
    table = _table_from_df(weights_df, name="portfolio_weights")

    metrics = {
        "expected_return": float(expected_return),
        "expected_volatility": float(expected_volatility),
        "sharpe_ratio": float(sharpe_ratio)
    }

    return _ok_result("optimize_portfolio", metrics=metrics, tables=[table], metadata=coerced.metadata)


def solve_scheduling_problem(
    data: Any,
    *,
    cost_cols: list[str] | None = None,
    headers: bool = True,
    header_row: int = 0,
    sheet_hint: str | None = None,
) -> dict[str, Any]:
    """Solve an assignment problem (e.g. workers to tasks) using linear_sum_assignment."""
    import pandas as pd
    from scipy import optimize as scipy_optimize

    coerced = _resolve_df(data, headers=headers, header_row=header_row, sheet_hint=sheet_hint)
    df = coerced.df
    
    try:
        numeric_cols = _numeric_columns(df, cost_cols)
    except ValueError as exc:
        return _error_result("UNKNOWN_COLUMN", str(exc), helper="solve_scheduling_problem")

    if not numeric_cols:
        return _error_result("INSUFFICIENT_DATA", "Need numeric columns for cost matrix.", helper="solve_scheduling_problem")

    cost_matrix = df[numeric_cols].values.astype(float)
    
    try:
        row_ind, col_ind = scipy_optimize.linear_sum_assignment(cost_matrix)
    except Exception as e:
        return _error_result("OPTIMIZATION_FAILED", str(e), helper="solve_scheduling_problem")

    total_cost = cost_matrix[row_ind, col_ind].sum()
    
    assignment_df = pd.DataFrame({
        "row_index": row_ind,
        "assigned_column": [numeric_cols[i] for i in col_ind],
        "cost": cost_matrix[row_ind, col_ind]
    })
    
    table = _table_from_df(assignment_df, name="optimal_assignment")

    metrics = {
        "total_cost": float(total_cost),
        "assignments": len(row_ind)
    }

    return _ok_result("solve_scheduling_problem", metrics=metrics, tables=[table], metadata=coerced.metadata)


def _dispatch_helper(name: str, data: Any, params: dict[str, Any], *, headers: bool, header_row: int, context: dict[str, Any]) -> dict[str, Any]:
    sheet_hint = context.get("sheet_name") if isinstance(context.get("sheet_name"), str) else None
    common: dict[str, Any] = {"headers": headers, "header_row": header_row, "sheet_hint": sheet_hint}

    if name == "optimize_portfolio":
        return optimize_portfolio(data, returns_col=params.get("returns_col"), target_return=params.get("target_return"), risk_free_rate=params.get("risk_free_rate", 0.0), **common)
    if name == "linear_programming":
        if "c_col" not in params or "a_cols" not in params or "b_col" not in params:
            return _error_result("MISSING_PARAM", "linear_programming requires c_col, a_cols, and b_col", helper=name)
        return linear_programming(data, c_col=params["c_col"], a_cols=params["a_cols"], b_col=params["b_col"], bounds=params.get("bounds", (0, None)), maximize=params.get("maximize", False), **common)
    if name == "solve_scheduling_problem":
        return solve_scheduling_problem(data, cost_cols=params.get("cost_cols"), **common)

    return _error_result("UNKNOWN_HELPER", f"Optimization helper {name!r} not found", helper=name)


def run_optimize(
    spec: dict[str, Any] | str,
    data: Any,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Spec-driven dispatcher — single trusted entry for host RPC."""
    parsed = _parse_trusted_spec(spec, helper_names=HELPER_NAMES, context=context)
    if isinstance(parsed, dict):
        return parsed
    helper, params, headers, header_row, ctx, _spec = parsed

    try:
        result = _dispatch_helper(helper, data, params, headers=headers, header_row=header_row, context=ctx)
    except Exception as exc:
        log.exception("Optimization helper %s failed", helper)
        return _error_result("OPTIMIZATION_FAILED", str(exc), helper=helper)

    if isinstance(result, dict) and result.get("status") == "ok" and ctx:
        result["context"] = {k: v for k, v in ctx.items() if k in ("sheet_name", "range_a1", "task_hint")}

    return result
