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
    _is_date_like_column_name,
    coerce_bool,
    coerce_optional_float,
    coerce_str_list,
    dispatch_common_kwargs,
    error_result as _error_result,
    numeric_columns as _numeric_columns,
    ok_result as _ok_result,
    resolve_df as _resolve_df,
    run_trusted_spec,
    table_from_df as _table_from_df,
)

log = logging.getLogger(__name__)


def _id_like(series: Any) -> bool:
    import numpy as np
    import pandas as pd

    if len(series) < 2:
        return False
    if pd.api.types.is_bool_dtype(series) or not pd.api.types.is_numeric_dtype(series):
        return False
    try:
        arr = series.to_numpy(dtype=float)
        if not np.isfinite(arr).all():
            return False
        if not (arr % 1 == 0).all():
            return False
        if len(np.unique(arr)) != len(arr):
            return False
        diffs = np.diff(arr)
        if (diffs > 0).all() or (diffs < 0).all():
            return True
    except Exception:
        pass
    return False


def _auto_numeric(
    df: Any,
    requested: list[str] | None,
    flags: list[str] | None = None,
) -> list[Any]:
    if requested is not None:
        return _numeric_columns(df, requested)
    # What was wrong: numeric_columns stringifies labels, so column 0 became "0"
    # and df["0"] raised KeyError on a DataFrame passed into the helper. Skipped
    # date and id columns were also silent, so a scheduling solve could drop a
    # real cost column and still return status: ok.
    # How it happened: resolve_df keeps a DataFrame's columns, and the auto path
    # indexed with the stringified name. dropped_rows already appends to flags;
    # this path did not.
    # Why this change: select select_dtypes(include="number") labels as they are,
    # and append one flag per skipped column when the caller passes a list.
    cols = list(df.select_dtypes(include="number").columns)
    kept: list[Any] = []
    for col in cols:
        name = str(col)
        if _is_date_like_column_name(name):
            if flags is not None:
                flags.append(f"Skipped column {name} (looks like a date); pass the column list explicitly to include it.")
        elif _id_like(df[col]):
            if flags is not None:
                flags.append(f"Skipped column {name} (looks like an id); pass the column list explicitly to include it.")
        else:
            kept.append(col)
    return kept


def _is_bound_scalar(x: Any) -> bool:
    import math
    import numpy as np

    if x is None:
        return True
    if isinstance(x, bool):
        return False
    if isinstance(x, (int, float, np.integer, np.floating)):
        return math.isfinite(float(x))
    return False


def _linprog_bounds(bounds: Any, n_vars: int) -> Any:
    if bounds is None:
        return None
    if isinstance(bounds, (list, tuple)):
        if len(bounds) == 2 and _is_bound_scalar(bounds[0]) and _is_bound_scalar(bounds[1]):
            return (bounds[0], bounds[1])
        if len(bounds) == n_vars:
            res: list[tuple[Any, Any]] = []
            for item in bounds:
                if not isinstance(item, (list, tuple)) or len(item) != 2:
                    raise ValueError("bounds must be one [low, high] pair or a list of one pair per variable")
                if not _is_bound_scalar(item[0]) or not _is_bound_scalar(item[1]):
                    raise ValueError("bounds must be one [low, high] pair or a list of one pair per variable")
                res.append((item[0], item[1]))
            return res
    raise ValueError("bounds must be one [low, high] pair or a list of one pair per variable")


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
    c_col: Any,
    a_cols: Any,
    b_col: Any,
    bounds: Any = (0, None),
    maximize: Any = False,
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

    helper = "linear_programming"
    if not isinstance(c_col, str) or not c_col.strip():
        return _error_result("INVALID_PARAM", f"c_col must be a string, got {c_col!r}", helper=helper)
    if not isinstance(b_col, str) or not b_col.strip():
        return _error_result("INVALID_PARAM", f"b_col must be a string, got {b_col!r}", helper=helper)

    try:
        a_cols_list = coerce_str_list(a_cols, name="a_cols")
    except ValueError as exc:
        return _error_result("INVALID_PARAM", str(exc), helper=helper)

    if not a_cols_list:
        return _error_result("INVALID_PARAM", "a_cols is required", helper=helper)

    all_lp_cols = [c_col, b_col, *a_cols_list]
    counts: dict[str, int] = {}
    for col_name in all_lp_cols:
        counts[col_name] = counts.get(col_name, 0) + 1
    duplicates = [col_name for col_name, count in counts.items() if count > 1]
    if duplicates:
        names = ", ".join(duplicates)
        return _error_result(
            "INVALID_PARAM",
            f"c_col, b_col, and a_cols must name distinct columns; repeated: {names}",
            helper=helper,
        )

    try:
        maximize_val = coerce_bool(maximize, name="maximize", default=False)
    except ValueError as exc:
        return _error_result("INVALID_PARAM", str(exc), helper=helper)

    coerced = _resolve_df(data, headers=headers, header_row=header_row, sheet_hint=sheet_hint)
    df = coerced.df

    available = [str(c) for c in df.columns]
    needed_cols = [c_col, b_col] + a_cols_list
    missing = [c for c in needed_cols if c not in df.columns]
    if missing:
        return _error_result(
            "UNKNOWN_COLUMN",
            f"Columns not found: {', '.join(missing)}. Available: {', '.join(available)}",
            helper=helper,
        )

    # What was wrong: non-numeric text cells survived dropna and ndarray.astype(float)
    # raised ValueError, causing OPTIMIZATION_FAILED in run_optimize or raising directly.
    # How it happened: dropna only filtered nulls/NaNs, not text strings in numeric columns.
    # Why this change fixes it: apply(pd.to_numeric, errors="coerce") turns text into NaN,
    # and dropna() drops rows with invalid cells the same way blank rows are dropped.
    clean_df = df[needed_cols].apply(pd.to_numeric, errors="coerce").dropna()
    if clean_df.empty:
        return _error_result("INSUFFICIENT_DATA", "No data for linear programming", helper=helper)

    # Objective coefficients, one per variable row.
    c = clean_df[c_col].to_numpy(dtype=float)
    if maximize_val:
        c = -c

    # a-columns are constraints, so transpose to linprog's (constraints, variables).
    coeff = clean_df[a_cols_list].to_numpy(dtype=float)
    n_constraints = int(coeff.shape[1])
    A_ub = coeff.T
    b_ub = _constraint_bounds(clean_df[b_col].to_numpy(dtype=float), n_constraints)
    if b_ub is None:
        return _error_result(
            "SHAPE_MISMATCH",
            (
                "linear_programming constraint shapes do not match: "
                f"{len(c)} variable rows and {n_constraints} constraint column(s) "
                f"({', '.join(str(name) for name in a_cols_list)}). "
                "Each row is a variable and each a-column is a <= constraint, so b "
                "needs one bound per constraint column. A single constraint column "
                "may repeat that same bound on every row. Mismatched b is rejected "
                "instead of being replaced with zeros."
            ),
            helper=helper,
            details={
                "n_variables": int(len(c)),
                "n_constraints": n_constraints,
                "n_rows": int(len(clean_df)),
            },
        )

    try:
        lp_bounds = _linprog_bounds(bounds, len(c))
    except ValueError as exc:
        return _error_result("INVALID_PARAM", str(exc), helper=helper)

    try:
        res = scipy_optimize.linprog(c, A_ub=A_ub, b_ub=b_ub, bounds=lp_bounds)
    except Exception as e:
        return _error_result("OPTIMIZATION_FAILED", str(e), helper=helper)

    if not res.success:
        return _error_result("OPTIMIZATION_FAILED", res.message, helper=helper)

    solution_df = pd.DataFrame({
        "variable_index": range(len(res.x)),
        "optimal_value": np.round(res.x, 4)
    })
    table = _table_from_df(solution_df, name="lp_solution")

    metrics = {
        "objective_value": float(-res.fun) if maximize_val else float(res.fun),
        "status": res.message,
        "iterations": int(res.nit)
    }

    return _ok_result(helper, metrics=metrics, tables=[table], metadata=coerced.metadata)


def optimize_portfolio(
    data: Any,
    *,
    returns_col: Any = None,
    target_return: Any = None,
    risk_free_rate: Any = 0.0,
    headers: bool = True,
    header_row: int = 0,
    sheet_hint: str | None = None,
) -> dict[str, Any]:
    """Long-only minimum-variance portfolio optimization.

    Weights are in [0, 1] and sum to 1. target_return is an optional
    per-period equality constraint. risk_free_rate does not affect weights;
    not annualized; no short sales. When returns_col is omitted, date-named
    columns and monotonic unique integer columns are skipped, and each skip
    is a flags entry.
    """
    import math
    import numpy as np
    import pandas as pd
    from scipy import optimize as scipy_optimize

    helper = "optimize_portfolio"
    try:
        returns_col_list = coerce_str_list(returns_col, name="returns_col")
    except ValueError as exc:
        return _error_result("INVALID_PARAM", str(exc), helper=helper)

    try:
        target_return_val = coerce_optional_float(target_return, name="target_return")
    except ValueError as exc:
        return _error_result("INVALID_PARAM", str(exc), helper=helper)

    try:
        rf_val = coerce_optional_float(risk_free_rate, name="risk_free_rate")
        if rf_val is None:
            rf_val = 0.0
    except ValueError as exc:
        return _error_result("INVALID_PARAM", str(exc), helper=helper)

    coerced = _resolve_df(data, headers=headers, header_row=header_row, sheet_hint=sheet_hint)
    df = coerced.df

    flags: list[str] = []
    try:
        numeric_cols = _auto_numeric(df, returns_col_list, flags)
    except ValueError as exc:
        return _error_result("UNKNOWN_COLUMN", str(exc), helper=helper)

    if not numeric_cols or len(numeric_cols) < 2:
        err = _error_result("INSUFFICIENT_DATA", "Need at least two numeric columns (assets).", helper=helper)
        if flags:
            err["flags"] = flags
        return err

    returns_raw = df[numeric_cols].apply(pd.to_numeric, errors="coerce")
    n_before_drop = len(returns_raw)
    returns = returns_raw.dropna()
    dropped_rows = n_before_drop - len(returns)
    if dropped_rows > 0:
        flags.append(f"Dropped {dropped_rows} return rows with a missing value")

    if len(returns) < 2:
        return _error_result("INSUFFICIENT_DATA", "Need at least two complete return rows.", helper=helper)

    cov_matrix = returns.cov().to_numpy()
    mean_returns = returns.mean().to_numpy()

    if not np.isfinite(cov_matrix).all():
        return _error_result("INSUFFICIENT_DATA", "Return covariance is not positive.", helper=helper)

    scale = float(np.mean(np.diag(cov_matrix)))
    if not math.isfinite(scale) or scale <= 0:
        return _error_result("INSUFFICIENT_DATA", "Return covariance is not positive.", helper=helper)

    scaled_cov = cov_matrix / scale
    num_assets = len(numeric_cols)

    # Objective: Minimize scaled portfolio variance
    def scaled_portfolio_variance(weights: Any) -> Any:
        return weights.T @ scaled_cov @ weights

    constraints: list[dict[str, Any]] = [
        {"type": "eq", "fun": lambda x: np.sum(x) - 1.0}
    ]

    if target_return_val is not None:
        constraints.append({
            "type": "eq",
            "fun": lambda x: np.sum(mean_returns * x) - target_return_val
        })

    bounds = tuple((0.0, 1.0) for _unused in range(num_assets))
    init_guess = np.array(num_assets * [1.0 / num_assets])

    # Portfolio objective: daily variances sit under SLSQP's default ftol,
    # so the equal-weight start is reported as optimal (nit == 1, about 34%
    # more volatility on a four-asset daily book). Scaling makes the objective
    # O(1); ftol=1e-12 covers a still-flat direction. Volatility uses the
    # original covariance because result.fun is scaled.
    try:
        result = scipy_optimize.minimize(
            scaled_portfolio_variance,
            init_guess,
            method="SLSQP",
            bounds=bounds,
            constraints=constraints,
            options={"ftol": 1e-12},
        )
    except Exception as e:
        return _error_result("OPTIMIZATION_FAILED", str(e), helper=helper)

    if not result.success:
        return _error_result("OPTIMIZATION_FAILED", result.message, helper=helper)

    raw_weights = result.x
    expected_return = float(np.sum(mean_returns * raw_weights))
    variance = float(raw_weights.T @ cov_matrix @ raw_weights)
    expected_volatility = float(math.sqrt(max(variance, 0.0)))
    sharpe_ratio = float((expected_return - rf_val) / expected_volatility) if expected_volatility > 0 else 0.0

    weights_df = pd.DataFrame({
        "asset": numeric_cols,
        "weight": np.round(raw_weights, 4)
    })
    table = _table_from_df(weights_df, name="portfolio_weights")

    metrics = {
        "expected_return": float(expected_return),
        "expected_volatility": float(expected_volatility),
        "sharpe_ratio": float(sharpe_ratio)
    }

    res_payload: dict[str, Any] = {
        "metrics": metrics,
        "tables": [table],
        "metadata": coerced.metadata,
    }
    if flags:
        res_payload["flags"] = flags

    return _ok_result(helper, **res_payload)


def solve_scheduling_problem(
    data: Any,
    *,
    cost_cols: Any = None,
    headers: bool = True,
    header_row: int = 0,
    sheet_hint: str | None = None,
) -> dict[str, Any]:
    """Solve an assignment problem (e.g. workers to tasks) using linear_sum_assignment.

    When cost_cols is omitted, date-named columns and monotonic unique integer
    columns are skipped, and each skip is a flags entry.
    """
    import pandas as pd
    from scipy import optimize as scipy_optimize

    helper = "solve_scheduling_problem"
    try:
        cost_cols_list = coerce_str_list(cost_cols, name="cost_cols")
    except ValueError as exc:
        return _error_result("INVALID_PARAM", str(exc), helper=helper)

    coerced = _resolve_df(data, headers=headers, header_row=header_row, sheet_hint=sheet_hint)
    df = coerced.df

    flags: list[str] = []
    try:
        numeric_cols = _auto_numeric(df, cost_cols_list, flags)
    except ValueError as exc:
        return _error_result("UNKNOWN_COLUMN", str(exc), helper=helper)

    if not numeric_cols:
        err = _error_result("INSUFFICIENT_DATA", "Need numeric columns for cost matrix.", helper=helper)
        if flags:
            err["flags"] = flags
        return err

    df_cost = df[numeric_cols].apply(pd.to_numeric, errors="coerce")
    df_clean = df_cost.dropna()
    if df_clean.empty:
        return _error_result("INSUFFICIENT_DATA", "Need numeric columns for cost matrix.", helper=helper)

    n_rows = len(df_clean)
    n_cols = len(numeric_cols)
    unassigned_rows = max(0, n_rows - n_cols)
    unassigned_columns = max(0, n_cols - n_rows)
    if unassigned_rows > 0:
        flags.append(f"{unassigned_rows} rows were not assigned because there are more rows than columns")
    if unassigned_columns > 0:
        if unassigned_columns == 1:
            flags.append("1 column was not assigned because there are more columns than rows")
        else:
            flags.append(f"{unassigned_columns} columns were not assigned because there are more columns than rows")

    cost_matrix = df_clean.to_numpy(dtype=float)

    try:
        row_ind, col_ind = scipy_optimize.linear_sum_assignment(cost_matrix)
    except Exception as e:
        return _error_result("OPTIMIZATION_FAILED", str(e), helper=helper)

    total_cost = float(cost_matrix[row_ind, col_ind].sum())

    kept_index = list(df_clean.index)
    assigned_row_labels = [kept_index[i] for i in row_ind]

    assignment_df = pd.DataFrame({
        "row_index": assigned_row_labels,
        "assigned_column": [numeric_cols[i] for i in col_ind],
        "cost": cost_matrix[row_ind, col_ind]
    })

    table = _table_from_df(assignment_df, name="optimal_assignment")

    metrics = {
        "total_cost": float(total_cost),
        "assignments": len(row_ind),
        "unassigned_rows": unassigned_rows,
        "unassigned_columns": unassigned_columns,
    }

    res_payload: dict[str, Any] = {
        "metrics": metrics,
        "tables": [table],
        "metadata": coerced.metadata,
    }
    if flags:
        res_payload["flags"] = flags

    return _ok_result(helper, **res_payload)


def _dispatch_helper(name: str, data: Any, params: dict[str, Any], *, headers: bool, header_row: int, context: dict[str, Any]) -> dict[str, Any]:
    common = dispatch_common_kwargs(context, headers=headers, header_row=header_row)

    if name == "optimize_portfolio":
        return optimize_portfolio(
            data,
            returns_col=params.get("returns_col"),
            target_return=params.get("target_return"),
            risk_free_rate=params.get("risk_free_rate", 0.0),
            **common,
        )
    if name == "linear_programming":
        if "c_col" not in params or "a_cols" not in params or "b_col" not in params:
            return _error_result("MISSING_PARAM", "linear_programming requires c_col, a_cols, and b_col", helper=name)
        return linear_programming(
            data,
            c_col=params.get("c_col"),
            a_cols=params.get("a_cols"),
            b_col=params.get("b_col"),
            bounds=params.get("bounds", (0, None)),
            maximize=params.get("maximize", False),
            **common,
        )
    if name == "solve_scheduling_problem":
        return solve_scheduling_problem(data, cost_cols=params.get("cost_cols"), **common)

    return _error_result("UNKNOWN_HELPER", f"Optimization helper {name!r} not found", helper=name)


def run_optimize(
    spec: dict[str, Any] | str,
    data: Any,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Spec-driven dispatcher — single trusted entry for host RPC."""
    return run_trusted_spec(
        spec,
        data,
        context,
        helper_names=HELPER_NAMES,
        dispatch_fn=_dispatch_helper,
        domain="Optimization",
        error_code="OPTIMIZATION_FAILED",
        logger=log,
    )
