import pytest
import pandas as pd
import numpy as np

from plugin.scripting.optimize import (
    linear_programming,
    optimize_portfolio,
    run_optimize,
    solve_scheduling_problem,
)

def test_linear_programming():
    # Each row is a variable. a1/a2 are constraints, so A is transposed:
    # max 3x + 2y s.t. x + 2y <= 4 and x + y <= 3.
    data = pd.DataFrame({
        "c": [3, 2],
        "a1": [1, 2],
        "a2": [1, 1],
        "b": [4, 3],
    })
    result = linear_programming(data, c_col="c", a_cols=["a1", "a2"], b_col="b", maximize=True)
    assert result["status"] == "ok"
    assert abs(result["metrics"]["objective_value"] - 9.0) < 1e-6
    assert "tables" in result


def test_linear_programming_single_constraint_column():
    # Default template a_cols=["a1"] on a multi-row grid: one constraint,
    # the same bound repeated on every variable row.
    data = pd.DataFrame({
        "c": [3, 2, 1],
        "a1": [1, 1, 1],
        "b": [10, 10, 10],
    })
    result = linear_programming(data, c_col="c", a_cols=["a1"], b_col="b", maximize=True)
    assert result["status"] == "ok", result
    assert abs(result["metrics"]["objective_value"] - 30.0) < 1e-6


def test_linear_programming_rejects_mismatched_bounds():
    data = pd.DataFrame({
        "c": [3, 2, 1],
        "a1": [1, 0, 1],
        "a2": [0, 1, 1],
        "b": [4, 5, 6],
    })
    result = linear_programming(data, c_col="c", a_cols=["a1", "a2"], b_col="b", maximize=True)
    assert result["status"] == "error"
    assert result["code"] == "SHAPE_MISMATCH"
    assert "zeros" in result["message"]


def test_linear_programming_rejects_ambiguous_single_bound():
    data = pd.DataFrame({
        "c": [3, 2],
        "a1": [1, 1],
        "b": [10, 4],
    })
    result = linear_programming(data, c_col="c", a_cols=["a1"], b_col="b", maximize=True)
    assert result["status"] == "error"
    assert result["code"] == "SHAPE_MISMATCH"


def test_optimize_portfolio():
    np.random.seed(42)
    returns = pd.DataFrame({
        "AAPL": np.random.normal(0.01, 0.02, 100),
        "MSFT": np.random.normal(0.008, 0.015, 100),
        "GOOG": np.random.normal(0.012, 0.025, 100)
    })
    result = optimize_portfolio(returns, returns_col=["AAPL", "MSFT", "GOOG"])
    assert result["status"] == "ok"
    assert "metrics" in result
    assert "tables" in result


def test_solve_scheduling_problem():
    cost_matrix = pd.DataFrame({
        "Task1": [4, 2, 8],
        "Task2": [2, 3, 4],
        "Task3": [8, 1, 2]
    })
    result = solve_scheduling_problem(cost_matrix, cost_cols=["Task1", "Task2", "Task3"])
    assert result["status"] == "ok"
    assert "metrics" in result
    assert result["metrics"]["total_cost"] == 6.0
    assert "tables" in result

def test_run_optimize_dispatcher():
    cost_matrix = pd.DataFrame({
        "Task1": [4, 2, 8],
        "Task2": [2, 3, 4],
        "Task3": [8, 1, 2]
    })
    spec = {
        "helper": "solve_scheduling_problem",
        "params": {"cost_cols": ["Task1", "Task2", "Task3"]}
    }
    result = run_optimize(spec, cost_matrix)
    assert result["status"] == "ok"


def test_insert_optimize_result_into_calc_returns_early_if_stopped(monkeypatch: pytest.MonkeyPatch) -> None:
    from plugin.scripting.optimize import insert_optimize_result_into_calc
    class MockCtx:
        def stop_checker(self):
            return True

    doc = None
    ctx = MockCtx()
    result = {"status": "ok", "result": "val"}

    res = insert_optimize_result_into_calc(doc, ctx, result)
    assert res == 0
