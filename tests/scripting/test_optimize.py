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


def test_linear_programming_text_row_is_dropped():
    df = pd.DataFrame({
        "c": [3, "x"],
        "a1": [1, 1],
        "b": [10, 10],
    })
    result = linear_programming(df, c_col="c", a_cols=["a1"], b_col="b", maximize=True)
    assert result["status"] == "ok", result
    assert abs(result["metrics"]["objective_value"] - 30.0) < 1e-6

    spec = {
        "helper": "linear_programming",
        "params": {"c_col": "c", "a_cols": ["a1"], "b_col": "b", "maximize": True},
    }
    opt_result = run_optimize(spec, df)
    assert opt_result["status"] == "ok", opt_result
    assert abs(opt_result["metrics"]["objective_value"] - 30.0) < 1e-6


def test_linear_programming_all_text_is_insufficient():
    df = pd.DataFrame({
        "c": ["bad", "text"],
        "a1": ["foo", "bar"],
        "b": ["baz", "qux"],
    })
    result = linear_programming(df, c_col="c", a_cols=["a1"], b_col="b", maximize=True)
    assert result["status"] == "error"
    assert result["code"] == "INSUFFICIENT_DATA"


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
    assert result["metrics"]["unassigned_columns"] == 0
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


def test_insert_optimize_result_into_calc() -> None:
    from unittest.mock import MagicMock, patch
    from plugin.scripting.optimize import insert_optimize_result_into_calc

    doc = MagicMock()
    ctx = MagicMock()
    result = {"status": "ok", "tables": [{"columns": ["Var", "Value"], "rows": [["x", 1.0]]}]}

    with patch("plugin.calc.tabular_egress.insert_tabular_result_into_calc", return_value=1) as mock_insert:
        res = insert_optimize_result_into_calc(doc, ctx, result)
        assert res == 1
        mock_insert.assert_called_once()


def test_portfolio_daily_returns_are_not_equal_weight():
    from scipy.optimize import minimize
    rng = np.random.default_rng(0)
    n = 500
    z = rng.normal(size=(n, 4))
    scales = [0.01, 0.015, 0.008, 0.02]
    rets = z * scales
    f = rng.normal(size=(n, 1)) * 0.01
    rets = rets + f * [1.0, 0.5, 1.2, 0.3]
    df = pd.DataFrame(rets, columns=["A", "B", "C", "D"])
    cov = np.cov(rets, rowvar=False)
    inv_cov = np.linalg.inv(cov)
    ones = np.ones(4)
    w_cf = inv_cov @ ones / (ones @ inv_cov @ ones)

    result = optimize_portfolio(df)
    assert result["status"] == "ok", result
    table = result["tables"][0]
    assert len(table["rows"]) == 4
    weights = [row[1] for row in table["rows"]]
    for w, cf in zip(weights, w_cf):
        assert abs(w - cf) < 0.01
    assert not all(abs(w - 0.25) < 0.01 for w in weights)

    # Equal-weight volatility vs solver volatility
    w_eq = np.array([0.25, 0.25, 0.25, 0.25])
    vol_eq = float(np.sqrt(w_eq @ cov @ w_eq))
    vol_reported = result["metrics"]["expected_volatility"]

    scale = float(np.mean(np.diag(cov)))
    res_solver = minimize(
        lambda w: (w @ cov @ w) / scale,
        np.ones(4) / 4.0,
        method="SLSQP",
        bounds=[(0, 1)] * 4,
        constraints=[{"type": "eq", "fun": lambda w: np.sum(w) - 1.0}],
        options={"ftol": 1e-12},
    )
    w_unrounded = res_solver.x
    expected_vol = float(np.sqrt(max(float(w_unrounded @ cov @ w_unrounded), 0.0)))
    assert abs(vol_reported - expected_vol) < 1e-9
    assert vol_reported < vol_eq


def test_portfolio_keeps_tiny_weights(monkeypatch):
    from unittest.mock import MagicMock
    mock_res = MagicMock()
    mock_res.success = True
    mock_res.x = np.array([0.5, 0.4999, 1e-8, 0.0])
    mock_res.fun = 1.0
    mock_res.message = "ok"

    monkeypatch.setattr("scipy.optimize.minimize", lambda *args, **kwargs: mock_res)
    df = pd.DataFrame(np.random.normal(size=(10, 4)), columns=["A", "B", "C", "D"])
    result = optimize_portfolio(df)
    assert result["status"] == "ok", result
    table = result["tables"][0]
    assert len(table["rows"]) == 4
    vol = result["metrics"]["expected_volatility"]
    assert np.isfinite(vol)
    assert vol != 1.0


def test_portfolio_one_row_is_insufficient():
    df = pd.DataFrame({"A": [0.01], "B": [0.02]})
    result = optimize_portfolio(df)
    assert result["status"] == "error"
    assert result["code"] == "INSUFFICIENT_DATA"
    msg = result.get("error") or result.get("message", "")
    assert "Need at least two complete return rows." in msg


def test_portfolio_nan_rows():
    df = pd.DataFrame({
        "A": [0.01, np.nan, 0.03],
        "B": [0.02, 0.01, 0.04],
    })
    result = optimize_portfolio(df)
    assert result["status"] == "ok", result
    assert "Dropped 1 return rows with a missing value" in result["flags"]


def test_risk_free_rate_null_is_zero():
    np.random.seed(42)
    returns = pd.DataFrame({
        "AAPL": np.random.normal(0.01, 0.02, 100),
        "MSFT": np.random.normal(0.008, 0.015, 100),
        "GOOG": np.random.normal(0.012, 0.025, 100),
    })
    res_none = run_optimize({
        "helper": "optimize_portfolio",
        "params": {"returns_col": ["AAPL", "MSFT", "GOOG"], "risk_free_rate": None},
    }, returns)
    res_zero = run_optimize({
        "helper": "optimize_portfolio",
        "params": {"returns_col": ["AAPL", "MSFT", "GOOG"], "risk_free_rate": 0},
    }, returns)
    assert res_none["status"] == "ok"
    assert res_zero["status"] == "ok"
    assert res_none["tables"][0]["rows"] == res_zero["tables"][0]["rows"]


def test_lp_unknown_column():
    data = pd.DataFrame({"c": [1, 2], "a1": [3, 4], "b": [5, 6]})
    result = linear_programming(data, c_col="cost", a_cols=["a1"], b_col="b")
    assert result["status"] == "error"
    assert result["code"] == "UNKNOWN_COLUMN"
    msg = result.get("error") or result.get("message", "")
    assert "cost" in msg
    assert "a1" in msg


def test_lp_string_a_cols():
    data = pd.DataFrame({
        "c": [3, 2, 1],
        "a1": [1, 1, 1],
        "b": [10, 10, 10],
    })
    result = linear_programming(data, c_col="c", a_cols="a1", b_col="b", maximize=True)
    assert result["status"] == "ok", result
    assert abs(result["metrics"]["objective_value"] - 30.0) < 1e-6


def test_lp_maximize_false_string():
    data = pd.DataFrame({
        "c": [3, 2, 1],
        "a1": [1, 1, 1],
        "b": [10, 10, 10],
    })
    result = linear_programming(data, c_col="c", a_cols=["a1"], b_col="b", maximize="false")
    assert result["status"] == "ok", result
    assert abs(result["metrics"]["objective_value"] - 0.0) < 1e-6


def test_lp_per_variable_bounds():
    data = pd.DataFrame({
        "c": [3, 2],
        "a1": [1, 2],
        "b": [4, 4],
    })
    result = linear_programming(data, c_col="c", a_cols=["a1"], b_col="b", bounds=[[0, 1], [0, 1]], maximize=True)
    assert result["status"] == "ok", result


def test_scheduling_skips_worker_id():
    df = pd.DataFrame({
        "worker_id": [1, 2, 3],
        "t1": [10, 25, 15],
        "t2": [20, 12, 18],
    })
    result = solve_scheduling_problem(df)
    assert result["status"] == "ok", result
    table = result["tables"][0]
    cols = table["columns"]
    col_idx = cols.index("assigned_column")
    for row in table["rows"]:
        assert row[col_idx] in ["t1", "t2"]


def test_scheduling_unassigned_rows():
    df = pd.DataFrame({
        "t1": [10, 20, 30],
        "t2": [15, 25, 35],
    })
    result = solve_scheduling_problem(df, cost_cols=["t1", "t2"])
    assert result["status"] == "ok", result
    assert result["metrics"]["unassigned_rows"] == 1
    assert result["metrics"]["unassigned_columns"] == 0
    assert any("1 rows were not assigned" in f for f in result["flags"])


def test_scheduling_unassigned_columns():
    df = pd.DataFrame({
        "t1": [1, 10],
        "t2": [10, 1],
        "t3": [100, 100],
    })
    result = solve_scheduling_problem(df, cost_cols=["t1", "t2", "t3"])
    assert result["status"] == "ok", result
    assert result["metrics"]["total_cost"] == 2
    assert result["metrics"]["assignments"] == 2
    assert result["metrics"]["unassigned_rows"] == 0
    assert result["metrics"]["unassigned_columns"] == 1
    assert result["flags"] == ["1 column was not assigned because there are more columns than rows"]



def test_string_returns_col_is_not_characters():
    df = pd.DataFrame({
        "AAPL": [0.01, 0.02, 0.03],
        "MSFT": [0.02, 0.03, 0.04],
    })
    result = optimize_portfolio(df, returns_col="AAPL")
    assert result["status"] == "error"
    assert result["code"] == "INSUFFICIENT_DATA"


def test_auto_numeric_keeps_runtime_update_consolidated():
    from plugin.scripting.venv.optimize import _auto_numeric

    df = pd.DataFrame({
        "Runtime": [3.0, 1.0, 2.0],
        "Update": [5.0, 2.0, 4.0],
        "Consolidated": [10.0, 8.0, 9.0],
        "Amount": [100.0, 50.0, 75.0],
        "Date": [45292.0, 45293.0, 45294.0],
    })
    res = _auto_numeric(df, None)
    assert "Runtime" in res
    assert "Update" in res
    assert "Consolidated" in res
    assert "Amount" in res
    assert "Date" not in res



