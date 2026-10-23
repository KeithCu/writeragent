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
    from plugin.scripting.helper_domain import insert_helper_result_into_calc

    doc = MagicMock()
    ctx = MagicMock()
    result = {"status": "ok", "tables": [{"columns": ["Var", "Value"], "rows": [["x", 1.0]]}]}

    with patch("plugin.calc.tabular_egress.insert_tabular_result_into_calc", return_value=1) as mock_insert:
        res = insert_helper_result_into_calc(doc, ctx, result)
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
    assert result["flags"] == [
        "Skipped column worker_id (looks like an id); pass the column list explicitly to include it.",
        "1 rows were not assigned because there are more rows than columns",
    ]


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


def test_portfolio_flags_skipped_date_and_id_columns():
    df = pd.DataFrame({
        "Date": [45292.0, 45293.0, 45294.0],
        "worker_id": [1, 2, 3],
        "A": [0.01, 0.02, 0.03],
        "B": [0.02, 0.01, 0.04],
    })
    result = optimize_portfolio(df)
    assert result["status"] == "ok", result
    assert result["flags"] == [
        "Skipped column Date (looks like a date); pass the column list explicitly to include it.",
        "Skipped column worker_id (looks like an id); pass the column list explicitly to include it.",
    ]
    table = result["tables"][0]
    assert [row[0] for row in table["rows"]] == ["A", "B"]


def test_explicit_column_list_includes_date_and_emits_no_skip_flag():
    df = pd.DataFrame({
        "Date": [45292.0, 45293.0, 45294.0],
        "A": [0.01, 0.02, 0.03],
        "B": [0.02, 0.01, 0.04],
    })
    result = optimize_portfolio(df, returns_col=["Date", "A", "B"])
    assert result["status"] == "ok", result
    table = result["tables"][0]
    assert [row[0] for row in table["rows"]] == ["Date", "A", "B"]
    assert not any(f.startswith("Skipped column") for f in result.get("flags", []))


def test_skipped_columns_are_on_the_insufficient_data_error():
    df = pd.DataFrame({
        "Date": [45292.0, 45293.0, 45294.0],
        "worker_id": [1, 2, 3],
    })
    res_port = optimize_portfolio(df)
    assert res_port["status"] == "error"
    assert res_port["code"] == "INSUFFICIENT_DATA"
    assert res_port["message"] == "Need at least two numeric columns (assets)."
    assert res_port["flags"] == [
        "Skipped column Date (looks like a date); pass the column list explicitly to include it.",
        "Skipped column worker_id (looks like an id); pass the column list explicitly to include it.",
    ]

    df_sched = pd.DataFrame({"worker_id": [1, 2, 3]})
    res_sched = solve_scheduling_problem(df_sched)
    assert res_sched["status"] == "error"
    assert res_sched["code"] == "INSUFFICIENT_DATA"
    assert res_sched["message"] == "Need numeric columns for cost matrix."
    assert res_sched["flags"] == [
        "Skipped column worker_id (looks like an id); pass the column list explicitly to include it.",
    ]


def test_auto_numeric_integer_labels_do_not_key_error():
    from plugin.scripting.venv.optimize import _auto_numeric

    df = pd.DataFrame({0: [0.01, 0.02, -0.01], 1: [0.03, -0.02, 0.01]})
    assert _auto_numeric(df, None) == [0, 1]
    result = optimize_portfolio(df)
    assert result["status"] == "ok", result
    assert [row[0] for row in result["tables"][0]["rows"]] == [0, 1]
    assert "flags" not in result


def test_linear_programming_rejects_duplicate_columns():
    data = pd.DataFrame({
        "c": [3, 2, 1],
        "a1": [1, 1, 1],
        "b": [10, 10, 10],
    })
    cases = [
        ({"c_col": "c", "b_col": "b", "a_cols": ["c"]}, "c"),
        ({"c_col": "c", "b_col": "c", "a_cols": ["a1"]}, "c"),
        ({"c_col": "c", "b_col": "b", "a_cols": ["a1", "a1"]}, "a1"),
        ({"c_col": "c", "b_col": "b", "a_cols": ["a1", "b"]}, "b"),
    ]
    for params, expected_repeat in cases:
        result = linear_programming(data, **params)
        assert result["status"] == "error"
        assert result["code"] == "INVALID_PARAM"
        assert result["message"] == f"c_col, b_col, and a_cols must name distinct columns; repeated: {expected_repeat}"


def test_optimize_facade_does_not_import_client_at_module_scope():
    import ast
    from pathlib import Path

    facade_path = Path(__file__).resolve().parents[2] / "plugin" / "scripting" / "optimize.py"
    tree = ast.parse(facade_path.read_text(encoding="utf-8"))

    forbidden = {
        "plugin.scripting.client",
        "plugin.scripting.trusted_rpc",
        "plugin.scripting.venv_worker",
    }

    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                for f in forbidden:
                    assert alias.name != f and not alias.name.startswith(f + "."), (
                        f"Module-level import of {alias.name} forbidden in optimize facade"
                    )
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            for f in forbidden:
                assert mod != f and not mod.startswith(f + "."), (
                    f"Module-level import from {mod} forbidden in optimize facade"
                )


def test_optimize_facade_import_does_not_load_host_worker():
    import os
    import subprocess
    import sys
    from pathlib import Path

    repo_root = str(Path(__file__).resolve().parents[2])
    env = dict(os.environ)
    existing_pp = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{repo_root}:{existing_pp}" if existing_pp else repo_root

    child_code = (
        "import sys\n"
        "import plugin.scripting.optimize as host\n"
        "forbidden = [\n"
        "    'plugin.scripting.client',\n"
        "    'plugin.scripting.trusted_rpc',\n"
        "    'plugin.scripting.venv_worker',\n"
        "    'plugin.framework.config',\n"
        "    'plugin.framework.worker_pool',\n"
        "    'plugin.framework.thread_guard',\n"
        "    'plugin.scripting.sandbox',\n"
        "]\n"
        "loaded = [m for m in forbidden if m in sys.modules]\n"
        "if loaded:\n"
        "    sys.stderr.write(f'Forbidden modules loaded after import: {loaded}\\n')\n"
        "    sys.exit(1)\n"
        "_ = host.linear_programming\n"
        "loaded_after_access = [m for m in forbidden if m in sys.modules]\n"
        "if loaded_after_access:\n"
        "    sys.stderr.write(f'Forbidden modules loaded after attribute access: {loaded_after_access}\\n')\n"
        "    sys.exit(1)\n"
        "from plugin.framework.errors import ToolExecutionError\n"
        "try:\n"
        "    host.run_trusted_optimize(None, None, helper='not-a-helper')\n"
        "    sys.stderr.write('Expected ToolExecutionError not raised\\n')\n"
        "    sys.exit(1)\n"
        "except ToolExecutionError as exc:\n"
        "    if 'not-a-helper' not in str(exc):\n"
        "        sys.stderr.write(f'Unexpected error message: {exc}\\n')\n"
        "        sys.exit(1)\n"
        "if 'plugin.scripting.client' not in sys.modules:\n"
        "    sys.stderr.write('Expected plugin.scripting.client in sys.modules after run_trusted_optimize\\n')\n"
        "    sys.exit(1)\n"
        "sys.exit(0)\n"
    )

    proc = subprocess.run(
        [sys.executable, "-c", child_code],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, f"Child failed (stdout={proc.stdout}, stderr={proc.stderr})"




