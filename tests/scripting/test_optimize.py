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
    # Maximize 3x + 2y => minimize -3x - 2y
    # subject to:
    # x + 2y <= 4
    # x + y <= 3
    data = pd.DataFrame({
        "c": [3, 2],
        "a1": [1, 2],
        "a2": [1, 1],
        "b": [4, 3] # This isn't exactly the standard format but it's what we mapped
    })
    
    # Actually wait, the linear_programming takes b as a column and a as columns, assuming A_ub is Transpose of A if dimensions mismatch.
    # We should pass b = [4, 3]. A = [[1, 1], [2, 1]]. Let's see...
    data = pd.DataFrame({
        "c": [3, 2],       # length 2
        "a1": [1, 2],      # length 2
        "a2": [1, 1],      # length 2
        "b": [4, 3],       # length 2
    })
    result = linear_programming(data, c_col="c", a_cols=["a1", "a2"], b_col="b", maximize=True)
    assert result["status"] == "ok"
    assert "metrics" in result
    assert "tables" in result


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
