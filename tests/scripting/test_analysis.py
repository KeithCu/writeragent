# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for plugin.scripting.analysis and plugin.scripting.venv.coerce."""

from __future__ import annotations

from unittest.mock import patch

import pytest

pytest.importorskip("pandas")
pytest.importorskip("sklearn")
pytest.importorskip("scipy")

import plugin.scripting.analysis as analysis
from plugin.scripting.analysis import coerce_to_dataframe
from plugin.scripting.calc_functions_common import ANALYSIS_MAX_TABLE_ROWS as MAX_TABLE_ROWS


SALES_GRID = [
    ["Region", "Sales", "Units"],
    ["North", "$1,200.50", 10],
    ["South", "800", 8],
    ["North", "$1,500.00", 12],
    ["East", "", 5],
]

DATE_GRID = [
    ["Date", "Revenue"],
    ["2023-01-15", 100],
    ["2023-06-15", 150],
    ["2024-01-15", 200],
    ["2024-06-15", 250],
]

PIVOT_GRID = [
    ["Region", "Quarter", "Sales"],
    ["North", "Q1", 100],
    ["North", "Q2", 120],
    ["South", "Q1", 80],
    ["South", "Q2", 90],
]

MONTE_CARLO_GRID = [
    ["Return"],
    [0.05],
    [-0.02],
    [0.03],
    [0.01],
    [-0.04],
]

REGRESSION_GRID = [["x", "y"], [1, 2], [2, 4], [3, 6], [4, 8]]


def test_coerce_headers_and_currency():
    result = coerce_to_dataframe(SALES_GRID, headers=True)
    df = result.df
    assert list(df.columns) == ["Region", "Sales", "Units"]
    assert df.loc[0, "Sales"] == pytest.approx(1200.50)
    assert df.loc[2, "Sales"] == pytest.approx(1500.0)
    assert result.metadata["numeric_cols"] == ["Sales", "Units"]
    assert result.metadata["n_rows"] == 4


def test_coerce_percent_and_empty_to_nan():
    import pandas as pd

    grid = [["Rate", "Label"], ["12%", "a"], ["", "b"], ["0.5", "c"]]
    result = coerce_to_dataframe(grid, headers=True)
    series = result.df["Rate"]
    assert len(series) == 3
    assert series.iloc[0] == pytest.approx(0.12)
    assert pd.isna(series.iloc[1])
    assert series.iloc[2] == pytest.approx(0.5)


def test_coerce_records_dict_shape():
    records = [{"a": 1, "b": 2}, {"a": 3, "b": 4}]
    result = coerce_to_dataframe(records, headers=True)
    assert list(result.df.columns) == ["a", "b"]
    assert len(result.df) == 2


def test_coerce_columns_rows_dict():
    payload = {"columns": ["x", "y"], "rows": [[1, 2], [3, 4]]}
    result = coerce_to_dataframe(payload, headers=True)
    assert list(result.df.columns) == ["x", "y"]
    assert len(result.df) == 2


def test_coerce_dedupes_header_names():
    grid = [["A", "A", "B"], [1, 2, 3]]
    result = coerce_to_dataframe(grid, headers=True)
    assert list(result.df.columns) == ["A", "A_1", "B"]


def _has_working_data_profiling() -> bool:
    try:
        from data_profiling import ProfileReport  # noqa: F401
        return True
    except Exception:
        return False


def test_describe_data_basic():
    if not _has_working_data_profiling():
        pytest.skip("fg-data-profiling not usable on this platform")
    result = analysis.describe_data(SALES_GRID)
    assert result["status"] == "ok"
    assert result["helper"] == "describe_data"
    assert result["metrics"]["row_count"] == 4
    assert result["metrics"]["col_count"] == 3
    assert result["metrics"]["cols_analyzed"] == 3
    assert not any(f.startswith("Analyzed the first") for f in result.get("flags", []))
    assert any(col["name"] == "Sales" for col in result["columns"])
    assert result["tables"][0]["name"] == "describe"


def test_describe_data_empty_and_bad_max_cols():
    res_empty = analysis.describe_data([])
    assert res_empty["status"] == "ok"
    assert res_empty["metrics"]["col_count"] == 0
    assert res_empty["metrics"]["cols_analyzed"] == 0
    assert "Empty dataset" in res_empty["flags"]

    res_bad1 = analysis.describe_data([], max_cols="no")
    assert res_bad1["status"] == "error"
    assert res_bad1["code"] == "INVALID_PARAM"

    res_bad2 = analysis.describe_data(SALES_GRID, max_cols="no")
    assert res_bad2["status"] == "error"
    assert res_bad2["code"] == "INVALID_PARAM"

    res_bad3 = analysis.describe_data(SALES_GRID, max_cols=0)
    assert res_bad3["status"] == "error"
    assert res_bad3["code"] == "INVALID_PARAM"

    res_bad4 = analysis.describe_data(SALES_GRID, include_outliers="nope")
    assert res_bad4["status"] == "error"
    assert res_bad4["code"] == "INVALID_PARAM"


def test_describe_data_flags_column_cap(monkeypatch):
    if not _has_working_data_profiling():
        pytest.skip("fg-data-profiling not usable on this platform")

    # Tip: stub ProfileReport to verify column capping without heavy profiling crashing xdist workers.
    class _StubReport:
        def __init__(self, df, *args, **kwargs):
            self._cols = list(df.columns)

        def to_json(self):
            import json

            vars_dict = {col: {"type": "Categorical", "p_missing": 0.0, "n_unique": 1} for col in self._cols}
            return json.dumps({"variables": vars_dict})

    monkeypatch.setattr("data_profiling.ProfileReport", _StubReport)
    res = analysis.describe_data(SALES_GRID, max_cols=1)
    assert res["status"] == "ok"
    assert res["metrics"]["cols_analyzed"] == 1
    assert res["metrics"]["col_count"] == 3
    assert len(res["columns"]) == 1
    assert "Analyzed the first 1 of 3 columns" in res["flags"]


def test_describe_data_without_profiling():
    import builtins

    real_import = builtins.__import__

    def blocked_import(name, *args, **kwargs):
        if name == "data_profiling":
            raise ImportError("no profiling")
        return real_import(name, *args, **kwargs)

    with patch("builtins.__import__", side_effect=blocked_import):
        result = analysis.describe_data(SALES_GRID)

    # Now returns error since fallback removed
    assert result["status"] == "error"
    assert result["code"] == "MISSING_PACKAGE"
    assert "fg-data-profiling" in result["message"]


def test_describe_data_with_profiling():
    if not _has_working_data_profiling():
        pytest.skip("fg-data-profiling not usable on this platform")
    result = analysis.describe_data(SALES_GRID)
    assert result["status"] == "ok"
    sales_col = next(col for col in result["columns"] if col["name"] == "Sales")
    assert "missing_pct" in sales_col
    assert sales_col.get("mean") is not None


def test_describe_data_pins_profile_pool_size():
    if not _has_working_data_profiling():
        pytest.skip("fg-data-profiling not usable on this platform")
    import data_profiling

    seen: dict = {}
    real = data_profiling.ProfileReport

    def recording(*args, **kwargs):
        seen["kwargs"] = kwargs
        return real(*args, **kwargs)

    with patch("data_profiling.ProfileReport", recording):
        result = analysis.describe_data(SALES_GRID)

    assert result["status"] == "ok"
    assert seen["kwargs"]["pool_size"] == 1


def test_kpi_summary():
    result = analysis.kpi_summary(SALES_GRID, ["Sales", "Units"])
    assert result["status"] == "ok"
    table = result["tables"][0]
    assert "metric" in table["columns"]
    assert result["writer_cleanup_hints"]["markdown_table"].startswith("|")


def test_kpi_summary_empty_metrics():
    result = analysis.kpi_summary(SALES_GRID, [])
    assert result["status"] == "error"
    assert result["code"] == "MISSING_PARAM"


def test_kpi_summary_string_metric_and_agg():
    res1 = analysis.kpi_summary(SALES_GRID, "Sales")
    assert res1["status"] == "ok"
    assert res1["metrics"]["metrics"] == ["Sales"]

    res2 = analysis.run_analysis(
        {"helper": "kpi_summary", "params": {"metrics": "Sales", "agg": "mean"}},
        SALES_GRID,
    )
    assert res2["status"] == "ok"
    assert res2["metrics"]["aggregations"] == ["mean"]
    table_cols = res2["tables"][0]["columns"]
    assert "Sales" in table_cols
    assert "S" not in table_cols

    res3 = analysis.run_analysis({"helper": "kpi_summary"}, SALES_GRID)
    assert res3["status"] == "error"
    assert res3["code"] == "MISSING_PARAM"

    res4 = analysis.kpi_summary(SALES_GRID, ["Region"])
    assert res4["status"] == "error"
    assert res4["code"] == "NON_NUMERIC_COLUMN"


def test_detect_outliers_iqr():
    grid = [["Value"], [1], [2], [3], [4], [100]]
    result = analysis.detect_outliers(grid, method="iqr")
    assert result["status"] == "ok"
    assert result["metrics"]["outlier_count"] >= 1


def test_detect_outliers_iqr_per_column_counts():
    grid = [
        ["a", "b"],
        [1, 1],
        [2, 2],
        [3, 3],
        [4, 4],
        [100, 4],
    ]
    result = analysis.detect_outliers(grid, method="iqr")
    assert result["status"] == "ok"
    per_column = result["metrics"]["per_column"]
    assert per_column["a"] >= 1
    assert per_column["b"] == 0


def test_detect_outliers_zscore():
    grid = [["Value"]] + [[i] for i in range(1, 21)] + [[500]]
    result = analysis.detect_outliers(grid, method="zscore", threshold=2.0)
    assert result["status"] == "ok"
    assert result["metrics"]["outlier_count"] >= 1
    assert result["metrics"]["method"] == "zscore"


def test_detect_outliers_rejects_unknown_method():
    res1 = analysis.detect_outliers(SALES_GRID, method="bogus")
    assert res1["status"] == "error"
    assert res1["code"] == "INVALID_PARAM"

    res2 = analysis.detect_outliers(SALES_GRID, method="IQR", columns=["Sales"])
    assert res2["status"] == "ok"
    assert res2["metrics"]["method"] == "iqr"

    res3 = analysis.run_analysis(
        {"helper": "detect_outliers", "params": {"method": "bogus", "columns": ["Note"]}},
        [["Note", "Sales"], ["a", 10], ["b", 20]],
    )
    assert res3["status"] == "error"
    assert res3["code"] == "INVALID_PARAM"

    res4 = analysis.detect_outliers(SALES_GRID, threshold="no")
    assert res4["status"] == "error"
    assert res4["code"] == "INVALID_PARAM"

    z_grid = [["Value"]] + [[i] for i in range(1, 21)] + [[500]]
    res5 = analysis.detect_outliers(z_grid, method="zscore", threshold="2")
    assert res5["status"] == "ok"


def test_detect_outliers_rejects_non_numeric_column():
    grid = [["Note", "Sales"], ["a", 10], ["b", 20]]
    res1 = analysis.detect_outliers(grid, columns=["Note"])
    assert res1["status"] == "error"
    assert res1["code"] == "NON_NUMERIC_COLUMN"

    res2 = analysis.detect_outliers(grid, columns=["Missing"])
    assert res2["status"] == "error"
    assert res2["code"] == "UNKNOWN_COLUMN"

    res3 = analysis.run_analysis(
        {"helper": "detect_outliers", "params": {"columns": ["Note"]}},
        grid,
    )
    assert res3["status"] == "error"
    assert res3["code"] == "NON_NUMERIC_COLUMN"

    res4 = analysis.detect_outliers(grid, columns=["Sales"])
    assert res4["status"] == "ok"


def test_quick_stats_tooltip():
    qs = analysis.QuickStats(SALES_GRID)
    result = qs.tooltip()
    assert result["status"] == "ok"
    assert result["metrics"]["record_count"] == 4
    assert result["tables"][0]["columns"] == ["Metric", "Value"]


def test_quick_stats_string_column():
    res1 = analysis.QuickStats(SALES_GRID, numeric_columns="Sales").tooltip()
    assert res1["status"] == "ok"
    assert any("Avg Sales" in str(row[0]) for row in res1["tables"][0]["rows"])

    res2 = analysis.QuickStats(SALES_GRID, numeric_columns="Region").tooltip()
    assert res2["status"] == "error"
    assert res2["code"] == "NON_NUMERIC_COLUMN"


def test_format_currency_and_percent():
    # 1D list
    assert analysis.format_currency([1234.5, 500]) == ["$1,234.50", "$500.00"]
    assert analysis.format_percent([0.125, 0.5]) == ["12.5%", "50.0%"]

    # Scalar
    assert analysis.format_currency(1234.5) == "$1,234.50"
    assert analysis.format_percent(0.125) == "12.5%"

    # 1x1 CalcRange -> scalar string
    from plugin.scripting.calc_range import CalcRange
    assert analysis.format_currency(CalcRange([[1234.5]])) == "$1,234.50"
    assert analysis.format_percent(CalcRange([[0.125]])) == "12.5%"

    # Nx1 CalcRange column -> Nx1 2D list
    assert analysis.format_currency(CalcRange([[1234.5], [None], [500]])) == [["$1,234.50"], [""], ["$500.00"]]
    assert analysis.format_percent(CalcRange([[0.125], [None], [0.5]])) == [["12.5%"], [""], ["50.0%"]]


def test_clean_and_prepare_fills_missing():
    result = analysis.clean_and_prepare(SALES_GRID, fill_numeric="median")
    assert result["status"] == "ok"
    assert result["metrics"]["row_count"] == 4


def test_pivot_aggregate():
    result = analysis.pivot_aggregate(PIVOT_GRID, index="Region", columns="Quarter", values="Sales", aggfunc="sum")
    assert result["status"] == "ok"
    assert result["tables"][0]["total_rows"] >= 2


def test_pivot_aggregate_missing_column_propagates():
    with pytest.raises(KeyError):
        analysis.pivot_aggregate(SALES_GRID, index="Missing", values="Sales")
    res = analysis.run_analysis(
        {"helper": "pivot_aggregate", "params": {"index": "Missing", "values": "Sales"}},
        SALES_GRID,
    )
    assert res["status"] == "error"
    assert res["code"] == "ANALYSIS_FAILED"
    assert "Missing" in res["message"]


def test_group_summary():
    result = analysis.group_summary(SALES_GRID, by="Region", metrics=["Sales"], aggfunc="sum")
    assert result["status"] == "ok"
    assert result["metrics"]["group_count"] >= 2


def test_group_summary_string_metrics():
    res1 = analysis.group_summary(SALES_GRID, by="Region", metrics="Sales")
    assert res1["status"] == "ok"

    res2 = analysis.group_summary(SALES_GRID, by="Region", metrics=["Region"])
    assert res2["status"] == "error"
    assert res2["code"] == "NON_NUMERIC_COLUMN"

    res3 = analysis.run_analysis(
        {"helper": "group_summary", "params": {"by": "Region", "metrics": "Sales"}},
        SALES_GRID,
    )
    assert res3["status"] == "ok"


def test_compare_periods_yoy():
    result = analysis.compare_periods(DATE_GRID, date_col="Date", value_col="Revenue", period="Y")
    assert result["status"] == "ok"
    assert "change" in result["tables"][0]["columns"]
    assert result["metrics"]["agg"] == "sum"
    revenue = [row[1] for row in result["tables"][0]["rows"]]
    assert 250 in revenue or 250.0 in revenue


def test_compare_periods_mean_does_not_sum_prices():
    result = analysis.compare_periods(DATE_GRID, date_col="Date", value_col="Revenue", period="Y", agg="mean")
    assert result["status"] == "ok"
    assert result["metrics"]["agg"] == "mean"
    revenue = [row[1] for row in result["tables"][0]["rows"]]
    assert 125 in revenue or 125.0 in revenue


def test_compare_periods_rejects_unknown_agg():
    result = analysis.compare_periods(DATE_GRID, date_col="Date", value_col="Revenue", agg="median")
    assert result["status"] == "error"
    assert result["code"] == "INVALID_PARAM"


def test_compare_periods_rejects_bad_period_and_calc():
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("error", FutureWarning)
        res1 = analysis.compare_periods(DATE_GRID, date_col="Date", value_col="Revenue", period="bogus")
        assert res1["status"] == "error"
        assert res1["code"] == "INVALID_PARAM"

    res2 = analysis.compare_periods(DATE_GRID, date_col="Date", value_col="Revenue", period="W")
    assert res2["status"] == "error"
    assert res2["code"] == "INVALID_PARAM"

    res3 = analysis.compare_periods(DATE_GRID, date_col="Date", value_col="Revenue", calc="nope")
    assert res3["status"] == "error"
    assert res3["code"] == "INVALID_PARAM"

    res4 = analysis.compare_periods(DATE_GRID, date_col="Date", value_col="Revenue", period="m")
    assert res4["status"] == "ok"
    assert res4["metrics"]["period"] == "M"

    res5 = analysis.compare_periods(DATE_GRID, date_col="Date", value_col="Revenue", period="YE")
    assert res5["status"] == "ok"
    assert res5["metrics"]["period"] == "YE"

    res6 = analysis.compare_periods(DATE_GRID, date_col="Date", value_col="Revenue", calc="DIFF")
    assert res6["status"] == "ok"
    assert res6["metrics"]["calc"] == "diff"

    text_grid = [["Date", "Label"], ["2023-01-15", "a"], ["2024-01-15", "b"]]
    res7 = analysis.compare_periods(text_grid, date_col="Date", value_col="Label")
    assert res7["status"] == "error"
    assert res7["code"] == "NON_NUMERIC_COLUMN"


def test_correlation_matrix():
    grid = [["a", "b", "c"], [1, 2, 3], [2, 4, 6], [3, 6, 9]]
    result = analysis.correlation_matrix(grid)
    assert result["status"] == "ok"
    assert result["metrics"]["pair_count"] >= 1


def test_correlation_pair_count_survives_the_cap():
    import numpy as np

    rng = np.random.default_rng(42)
    cols12 = [f"c{i}" for i in range(12)]
    data12 = [cols12] + rng.normal(size=(8, 12)).tolist()
    res12 = analysis.correlation_matrix(data12)
    assert res12["status"] == "ok"
    assert res12["metrics"]["pair_count"] == 66
    assert res12["tables"][0]["total_rows"] == 66
    assert len(res12["tables"][0]["rows"]) == MAX_TABLE_ROWS
    assert res12["tables"][0]["truncated"] is True

    cols10 = [f"c{i}" for i in range(10)]
    data10 = [cols10] + rng.normal(size=(8, 10)).tolist()
    res10 = analysis.correlation_matrix(data10)
    assert res10["status"] == "ok"
    assert res10["metrics"]["pair_count"] == 45
    assert res10["tables"][0]["total_rows"] == 45
    assert len(res10["tables"][0]["rows"]) == 45
    assert res10["tables"][0]["truncated"] is False

    res_pearson = analysis.correlation_matrix(data10, method="Pearson")
    assert res_pearson["status"] == "ok"
    assert res_pearson["metrics"]["method"] == "pearson"

    res_bogus = analysis.correlation_matrix(data10, method="bogus")
    assert res_bogus["status"] == "error"
    assert res_bogus["code"] == "INVALID_PARAM"

    res_bad_min = analysis.correlation_matrix(data10, min_abs="no")
    assert res_bad_min["status"] == "error"
    assert res_bad_min["code"] == "INVALID_PARAM"


def test_run_regression_linear():
    import importlib.util

    if importlib.util.find_spec("statsmodels") is None:
        pytest.skip("statsmodels not installed")
    grid = [["x", "y"], [1, 2], [2, 4], [3, 6], [4, 8]]
    result = analysis.run_regression(grid, target="y", features=["x"])
    assert result["status"] == "ok"
    assert result["metrics"]["r_squared"] == pytest.approx(1.0, abs=1e-4)
    assert result["tables"][0]["columns"] == ["term", "coefficient", "std_err", "p_value", "ci_low", "ci_high"]


def test_run_regression_missing_statsmodels(monkeypatch):
    import sys

    # Remove statsmodels from sys.modules to trigger ImportError
    monkeypatch.setitem(sys.modules, "statsmodels", None)
    monkeypatch.setitem(sys.modules, "statsmodels.api", None)

    from plugin.scripting import analysis

    grid = [["x", "y"], [1, 2], [2, 4], [3, 6], [4, 8]]
    result = analysis.run_regression(grid, target="y", features=["x"])

    assert result["status"] == "error"
    assert result["code"] == "MISSING_PACKAGE"
    assert "statsmodels" in result["message"]


def test_run_regression_string_feature_and_types():
    import importlib.util

    if importlib.util.find_spec("statsmodels") is None:
        pytest.skip("statsmodels not installed")

    res1 = analysis.run_regression(REGRESSION_GRID, target="y", features="x")
    assert res1["status"] == "ok"
    assert res1["metrics"]["r_squared"] == pytest.approx(1.0, abs=1e-4)

    res2 = analysis.run_regression(REGRESSION_GRID, target="y")
    assert res2["status"] == "ok"
    assert res2["metrics"]["r_squared"] == pytest.approx(1.0, abs=1e-4)

    res3 = analysis.run_regression(REGRESSION_GRID, target="y", features=[])
    assert res3["status"] == "error"
    assert res3["code"] == "NO_FEATURES"

    res4 = analysis.run_regression(REGRESSION_GRID, target="y", features="x", add_constant="false")
    assert res4["status"] == "ok"

    res5 = analysis.run_regression(REGRESSION_GRID, target="y", features="x", add_constant="nope")
    assert res5["status"] == "error"
    assert res5["code"] == "INVALID_PARAM"

    text_grid = [["x", "y", "text"], [1, 2, "a"], [2, 4, "b"], [3, 6, "c"], [4, 8, "d"]]
    res6 = analysis.run_regression(text_grid, target="y", features=["text"])
    assert res6["status"] == "error"
    assert res6["code"] == "NON_NUMERIC_COLUMN"

    res7 = analysis.run_regression(REGRESSION_GRID, target=1)  # type: ignore[arg-type]
    assert res7["status"] == "error"
    assert res7["code"] == "INVALID_PARAM"


def test_cluster_numeric():
    grid = [["a", "b"], [1, 1], [1.1, 1.2], [5, 5], [5.2, 4.8]]
    result = analysis.cluster_numeric(grid, n_clusters=2)
    assert result["status"] == "ok"
    assert result["metrics"]["n_clusters"] == 2
    assert result["metrics"]["scale"] is True
    names = [table["name"] for table in result["tables"]]
    assert names == ["centroids", "labels"]


def test_cluster_numeric_params():
    grid = [["a", "b", "Note"], [1, 1, "x"], [1.1, 1.2, "y"], [5, 5, "z"], [5.2, 4.8, "w"]]
    res1 = analysis.cluster_numeric(grid, columns=["Note"])
    assert res1["status"] == "error"
    assert res1["code"] == "NON_NUMERIC_COLUMN"

    four_pt = [["a", "b"], [1, 1], [1.1, 1.2], [5, 5], [5.2, 4.8]]
    res2 = analysis.cluster_numeric(four_pt, n_clusters="no")
    assert res2["status"] == "error"
    assert res2["code"] == "INVALID_PARAM"

    res3 = analysis.cluster_numeric(four_pt, n_clusters=2.5)  # type: ignore[arg-type]
    assert res3["status"] == "error"
    assert res3["code"] == "INVALID_PARAM"

    res4 = analysis.cluster_numeric(four_pt, n_clusters="2")
    assert res4["status"] == "ok"
    assert res4["metrics"]["n_clusters"] == 2

    res5 = analysis.cluster_numeric(four_pt, scale="false")
    assert res5["status"] == "ok"
    assert res5["metrics"]["scale"] is False

    res6 = analysis.cluster_numeric(four_pt, method="KMeans")
    assert res6["status"] == "error"
    assert res6["code"] == "UNSUPPORTED_METHOD"


def test_cluster_numeric_scaling_follows_the_small_column():
    # The noise column spans 0..1000. Unscaled KMeans follows that column and
    # mixes the two groups. Standardization keeps each group together.
    import numpy as np

    rng = np.random.default_rng(0)
    n = 16
    signal = [0.0] * n + [10.0] * n
    noise = rng.uniform(0, 1000, size=2 * n)
    grid = [["signal", "noise"], *[[s, float(v)] for s, v in zip(signal, noise)]]
    result = analysis.cluster_numeric(grid, n_clusters=2)
    assert result["status"] == "ok"
    labels = next(table for table in result["tables"] if table["name"] == "labels")
    clusters = [row[1] for row in labels["rows"]]
    assert len(set(clusters[:n])) == 1
    assert len(set(clusters[n:])) == 1
    assert clusters[0] != clusters[n]
    centroids = next(table for table in result["tables"] if table["name"] == "centroids")
    signal_centers = [row[1] for row in centroids["rows"]]
    assert min(signal_centers) < 2
    assert max(signal_centers) > 8
    assert "silhouette" in result["metrics"]


def test_clean_and_prepare_keeps_rows_past_the_old_50_cap():
    from plugin.scripting.calc_functions_common import ANALYSIS_DATA_TABLE_ROWS

    header = ["x"]
    result = analysis.clean_and_prepare([header, *[[i] for i in range(120)]])
    table = result["tables"][0]
    assert table["total_rows"] == 120
    assert len(table["rows"]) == 120
    assert table["truncated"] is False

    extra = ANALYSIS_DATA_TABLE_ROWS + 10
    result = analysis.clean_and_prepare([header, *[[i] for i in range(extra)]])
    table = result["tables"][0]
    assert table["truncated"] is True
    assert len(table["rows"]) == ANALYSIS_DATA_TABLE_ROWS
    assert table["total_rows"] == extra


def test_format_dispatch_flattens_cells():
    res1 = analysis.run_analysis({"helper": "format_currency"}, [[1234.5], [500.0]])
    assert res1["status"] == "ok"
    assert res1["metrics"]["count"] == 2
    assert res1["tables"][0]["rows"] == [["$1,234.50"], ["$500.00"]]

    res2 = analysis.run_analysis({"helper": "format_currency"}, 1234.5)
    assert res2["status"] == "ok"
    assert res2["metrics"]["count"] == 1
    assert res2["tables"][0]["rows"] == [["$1,234.50"]]

    res3 = analysis.run_analysis({"helper": "format_currency"}, [[1234.5, 500]])
    assert res3["status"] == "ok"
    assert res3["metrics"]["count"] == 2
    assert res3["tables"][0]["rows"] == [["$1,234.50"], ["$500.00"]]

    res4 = analysis.run_analysis({"helper": "format_percent"}, 0.125)
    assert res4["status"] == "ok"
    assert res4["metrics"]["count"] == 1
    assert res4["tables"][0]["rows"] == [["12.5%"]]

    res5 = analysis.run_analysis(
        {"helper": "format_percent", "params": {"values": [0.125]}},
        [[999.0]],
    )
    assert res5["status"] == "ok"
    assert res5["metrics"]["count"] == 1
    assert res5["tables"][0]["rows"] == [["12.5%"]]

    res6 = analysis.run_analysis({"helper": "format_currency", "params": {"decimals": "no"}}, [[1234.5]])
    assert res6["status"] == "error"
    assert res6["code"] == "INVALID_PARAM"

    res7 = analysis.run_analysis({"helper": "format_currency", "params": {"decimals": -1}}, [[1234.5]])
    assert res7["status"] == "error"
    assert res7["code"] == "INVALID_PARAM"

    res8 = analysis.run_analysis({"helper": "format_currency", "params": {"symbol": 1}}, [[1234.5]])
    assert res8["status"] == "error"
    assert res8["code"] == "INVALID_PARAM"


def test_run_analysis_ignores_return_data():
    grid = [["Region", "Sales"], ["N", None], ["S", 10], ["E", 30]]
    result = analysis.run_analysis(
        {"helper": "clean_and_prepare", "params": {"fill_numeric": "median"}, "return_data": True},
        grid,
    )
    assert result["status"] == "ok"
    assert "data_records" not in result
    sales_val = result["tables"][0]["rows"][0][1]
    assert sales_val in (20, 20.0)


def test_monte_carlo_resample_metrics():
    import importlib.util
    import numpy as np

    if importlib.util.find_spec("pandas_montecarlo") is None:
        pytest.skip("pandas-montecarlo not installed")
    # Seed numpy's RNG used by pandas_montecarlo (Series.sample) to ensure
    # deterministic permutation sampling. With only 50 simulations, permutations
    # reaching the global trough (-0.06) can occasionally be missed (~0.5% of runs,
    # yielding -0.05). Tolerance abs=0.02 allows close random-derived troughs.
    np.random.seed(0)
    result = analysis.monte_carlo(MONTE_CARLO_GRID, sims=50, bust=-0.05, goal=0.05)
    assert result["status"] == "ok"
    metrics = result["metrics"]
    assert metrics["simulations"] == 50
    assert metrics["final_value"] == pytest.approx(0.03)
    assert 0 <= metrics["bust_prob"] <= 1
    assert 0 <= metrics["goal_prob"] <= 1
    assert metrics["worst_trough"] == pytest.approx(-0.06, abs=0.02)
    for bad_key in ("min", "max", "mean", "median", "std"):
        assert bad_key not in metrics
    table = result["tables"][0]
    assert table["name"] == "monte_carlo_totals"
    assert table["columns"] == ["simulation", "final_value", "trough"]
    assert table["rows"][0][0] == "original"
    for row in table["rows"]:
        assert row[1] == pytest.approx(0.03)
    assert table["total_rows"] == 50
    assert table["truncated"] is False


def test_monte_carlo_small_series():
    import importlib.util

    if importlib.util.find_spec("pandas_montecarlo") is None:
        pytest.skip("pandas-montecarlo not installed")
    grid = [["x"], [1], [2], [3], [4], [5]]
    result = analysis.monte_carlo(grid, sims=10)
    assert result["status"] == "ok"
    assert result["metrics"]["simulations"] == 10
    assert result["metrics"]["final_value"] == pytest.approx(15)
    assert result["metrics"]["worst_trough"] > 0
    table = result["tables"][0]
    assert table["columns"] == ["simulation", "final_value", "trough"]


def test_monte_carlo_missing_pandas_montecarlo(monkeypatch):
    import sys

    # Remove pandas_montecarlo from sys.modules to trigger ImportError
    monkeypatch.setitem(sys.modules, "pandas_montecarlo", None)

    from plugin.scripting import analysis

    grid = [["x"], [1], [2], [3], [4], [5]]
    result = analysis.monte_carlo(grid, sims=10)

    assert result["status"] == "error"
    assert result["code"] == "MISSING_PACKAGE"
    assert "pandas-montecarlo" in result["message"]


def test_run_analysis_monte_carlo_dispatch():
    import importlib.util

    if importlib.util.find_spec("pandas_montecarlo") is None:
        pytest.skip("pandas-montecarlo not installed")
    result = analysis.run_analysis(
        {"helper": "monte_carlo", "params": {"sims": 20, "bust": -0.1, "goal": 0.0}},
        MONTE_CARLO_GRID,
    )
    assert result["status"] == "ok"
    assert result["helper"] == "monte_carlo"
    assert result["metrics"]["simulations"] == 20

    res_str = analysis.run_analysis(
        {"helper": "monte_carlo", "params": {"sims": "20"}},
        MONTE_CARLO_GRID,
    )
    assert res_str["status"] == "ok"
    assert res_str["metrics"]["simulations"] == 20

    res_bad_sims = analysis.run_analysis(
        {"helper": "monte_carlo", "params": {"sims": "no"}},
        MONTE_CARLO_GRID,
    )
    assert res_bad_sims["status"] == "error"
    assert res_bad_sims["code"] == "INVALID_PARAM"

    res_bad_bust = analysis.run_analysis(
        {"helper": "monte_carlo", "params": {"bust": "no"}},
        MONTE_CARLO_GRID,
    )
    assert res_bad_bust["status"] == "error"
    assert res_bad_bust["code"] == "INVALID_PARAM"


def test_run_analysis_dispatches_helper(monkeypatch):
    # Tip: the real profile can crash an xdist worker (native stack); stub it so this checks dispatch only.
    # Stub describe_data to verify run_analysis dispatch and context echo without invoking heavy profiling.
    recorded = {}

    def stub_describe(data, **kwargs):
        recorded["data"] = data
        recorded["kwargs"] = kwargs
        return {"status": "ok", "helper": "describe_data"}

    monkeypatch.setattr("plugin.scripting.venv.analysis.describe_data", stub_describe)
    result = analysis.run_analysis("describe_data", SALES_GRID)
    assert result["status"] == "ok"
    assert result["helper"] == "describe_data"
    assert recorded["data"] == SALES_GRID

    result_ctx = analysis.run_analysis("describe_data", SALES_GRID, {"sheet_name": "Sheet1"})
    assert result_ctx["status"] == "ok"
    assert result_ctx["helper"] == "describe_data"
    assert result_ctx["context"]["sheet_name"] == "Sheet1"


def test_run_analysis_unknown_helper():
    result = analysis.run_analysis({"helper": "not_real"}, SALES_GRID)
    assert result["status"] == "error"
    assert result["code"] == "UNKNOWN_HELPER"


def test_run_analysis_echoes_context():
    result = analysis.run_analysis(
        {"helper": "kpi_summary", "params": {"metrics": ["Sales", "Units"]}},
        SALES_GRID,
        {"sheet_name": "Sheet1", "range_a1": "A1:C5"},
    )
    assert result["status"] == "ok"
    assert result["context"]["sheet_name"] == "Sheet1"


def test_table_row_cap():
    grid = [["Region", "Sales"]] + [[f"R{i % 5}", i * 10] for i in range(100)]
    result = analysis.group_summary(grid, by="Region", metrics=["Sales"], aggfunc="sum")
    assert result["tables"][0]["truncated"] is False
    assert result["tables"][0]["total_rows"] <= MAX_TABLE_ROWS


@pytest.mark.parametrize(
    ("helper", "call", "metric_keys", "requires"),
    [
        (
            "describe_data",
            lambda: analysis.describe_data(SALES_GRID),
            ("row_count", "col_count"),
            "data_profiling",
        ),
        (
            "detect_outliers",
            lambda: analysis.detect_outliers(SALES_GRID, method="iqr", columns=["Sales"]),
            ("outlier_count", "method"),
            None,
        ),
        (
            "monte_carlo",
            lambda: analysis.monte_carlo(MONTE_CARLO_GRID, sims=25),
            ("simulations", "final_value", "bust_prob", "goal_prob", "worst_trough"),
            "pandas_montecarlo",
        ),
        (
            "run_regression",
            lambda: analysis.run_regression(REGRESSION_GRID, target="y", features=["x"]),
            ("r_squared", "n_obs"),
            "statsmodels",
        ),
    ],
)
def test_helper_golden_metrics(helper, call, metric_keys, requires):
    import importlib.util

    if requires == "data_profiling":
        if not _has_working_data_profiling():
            pytest.skip("data_profiling not usable on this platform")
    elif requires and importlib.util.find_spec(requires) is None:
        pytest.skip(f"{requires} not installed")
    result = call()
    assert result["status"] == "ok"
    assert result["helper"] == helper
    for key in metric_keys:
        assert key in result["metrics"], f"missing metric {key!r} for {helper}"


def test_insert_analysis_result_into_calc() -> None:
    from unittest.mock import MagicMock
    from plugin.calc.analysis_egress import insert_analysis_result_into_calc

    doc = object()
    ctx = MagicMock()
    result = {"status": "ok", "result": "val"}

    def _write(_doc, _uno_ctx, grid, **_kwargs):
        return len(grid)

    with patch("plugin.calc.analysis_egress.insert_tabular_result_into_calc", side_effect=_write) as mock_insert:
        res = insert_analysis_result_into_calc(doc, ctx, result)

    mock_insert.assert_called_once()
    assert mock_insert.call_args.args[0] is doc
    assert mock_insert.call_args.args[1] is ctx
    grid = mock_insert.call_args.args[2]
    assert grid
    assert res == len(grid)


def test_compare_periods_parses_calc_serials_and_years():
    grid_serials = [
        ["Date", "Revenue"],
        [44927, 100],
        [45292, 150],
        [45658, 200],
    ]
    res_serials = analysis.compare_periods(grid_serials, date_col="Date", value_col="Revenue", period="Y")
    assert res_serials["status"] == "ok"
    assert res_serials["metrics"]["periods"] == 3
    rows = res_serials["tables"][0]["rows"]
    assert [row[0].year for row in rows] == [2023, 2024, 2025]
    assert [row[1] for row in rows] == [100, 150, 200]

    grid_years = [
        ["Date", "Revenue"],
        [2023, 100],
        [2024, 150],
        [2025, 200],
    ]
    res_years = analysis.compare_periods(grid_years, date_col="Date", value_col="Revenue", period="Y")
    assert res_years["status"] == "ok"
    assert res_years["metrics"]["periods"] == 3
    rows_y = res_years["tables"][0]["rows"]
    assert [row[0].year for row in rows_y] == [2023, 2024, 2025]
    assert [row[1] for row in rows_y] == [100, 150, 200]


def test_clean_and_prepare_extension_dtypes():
    import numpy as np
    import pandas as pd

    # Float64 series [1.0, NA, 3.0]
    df_float = pd.DataFrame({"x": pd.Series([1.0, pd.NA, 3.0], dtype="Float64")})
    res_float = analysis.clean_and_prepare(df_float)
    assert any("Filled numeric column x with median" in f for f in res_float["flags"])
    assert not any("categorical" in f and "x" in f for f in res_float["flags"])
    assert res_float["tables"][0]["rows"][1][0] == 2.0

    # UInt64 series [1, NA, 3]
    df_uint = pd.DataFrame({"x": pd.Series([1, pd.NA, 3], dtype="UInt64")})
    res_uint = analysis.clean_and_prepare(df_uint)
    assert any("Filled numeric column x with median" in f for f in res_uint["flags"])
    assert res_uint["tables"][0]["rows"][1][0] == 2

    # pandas boolean series [True, NA, True]
    df_bool = pd.DataFrame({"x": pd.Series([True, pd.NA, True], dtype="boolean")})
    res_bool = analysis.clean_and_prepare(df_bool)
    assert any("Filled categorical column x with mode" in f for f in res_bool["flags"])
    assert not any("numeric" in f and "x" in f for f in res_bool["flags"])

    # complex128 series [1+1j, nan, 1+1j]
    df_c = pd.DataFrame({"x": pd.Series([1 + 1j, np.nan, 1 + 1j], dtype="complex128")})
    res_c = analysis.clean_and_prepare(df_c)
    assert any("Filled categorical column x with mode" in f for f in res_c["flags"])
    assert res_c["tables"][0]["rows"][1][0] == 1 + 1j


def test_detect_outliers_isolation_forest_is_row_level():
    grid = [["a", "b"]] + [[1.0, 1.0] for _ in range(20)] + [[1000.0, 1000.0]]
    result = analysis.detect_outliers(grid, method="isolation_forest")
    assert result["status"] == "ok"
    outlier_count = result["metrics"]["outlier_count"]
    assert outlier_count >= 1
    assert result["metrics"]["per_column"] == {}
    assert result["flags"] == [f"{outlier_count} outlier rows (isolation_forest)"]
    assert not any("outliers in" in f for f in result["flags"])
    assert result["tables"][0]["total_rows"] == outlier_count


def test_sklearn_helpers_report_missing_package(monkeypatch):
    import sys

    # 1. sklearn.ensemble set to None
    monkeypatch.setitem(sys.modules, "sklearn.ensemble", None)
    grid = [["a", "b"], [1, 1], [2, 2], [3, 3], [100, 100]]
    res_forest = analysis.detect_outliers(grid, method="isolation_forest")
    assert res_forest["status"] == "error"
    assert res_forest["code"] == "MISSING_PACKAGE"
    assert "scikit-learn" in res_forest["message"]

    res_iqr = analysis.detect_outliers(grid, method="iqr")
    assert res_iqr["status"] == "ok"

    # 2. sklearn.cluster set to None
    four_pt = [["a", "b"], [1, 1], [1.1, 1.2], [5, 5], [5.2, 4.8]]
    monkeypatch.undo()
    monkeypatch.setitem(sys.modules, "sklearn.cluster", None)
    res_kmeans = analysis.cluster_numeric(four_pt)
    assert res_kmeans["status"] == "error"
    assert res_kmeans["code"] == "MISSING_PACKAGE"
    assert "scikit-learn" in res_kmeans["message"]

    # 3. sklearn.preprocessing set to None and scale left at default True
    monkeypatch.undo()
    monkeypatch.setitem(sys.modules, "sklearn.preprocessing", None)
    res_scaler = analysis.cluster_numeric(four_pt, scale=True)
    assert res_scaler["status"] == "error"
    assert res_scaler["code"] == "MISSING_PACKAGE"
    assert "scikit-learn" in res_scaler["message"]


def test_run_regression_drops_non_finite_rows():
    import importlib.util

    if importlib.util.find_spec("statsmodels") is None:
        pytest.skip("statsmodels not installed")

    grid_inf = [["x", "y"], [1, 2], [2, 4], [3, float("inf")], [4, 8]]
    res = analysis.run_regression(grid_inf, target="y", features=["x"])
    assert res["status"] == "ok"
    assert res["metrics"]["n_obs"] == 3
    assert res["metrics"]["r_squared"] == pytest.approx(1.0, abs=1e-4)

    grid_all_inf = [["x", "y"], [1, float("inf")], [2, float("-inf")]]
    res_insuf = analysis.run_regression(grid_all_inf, target="y", features=["x"])
    assert res_insuf["status"] == "error"
    assert res_insuf["code"] == "INSUFFICIENT_DATA"


def test_correlation_matrix_rejects_negative_min_abs():
    grid = [["a", "b", "c"], [1, 2, 3], [2, 4, 6], [3, 6, 9]]
    res_neg = analysis.correlation_matrix(grid, min_abs=-0.1)
    assert res_neg["status"] == "error"
    assert res_neg["code"] == "INVALID_PARAM"

    res_default = analysis.correlation_matrix(grid)
    res_zero = analysis.correlation_matrix(grid, min_abs=0)
    assert res_zero["status"] == "ok"
    assert res_zero["metrics"]["pair_count"] == res_default["metrics"]["pair_count"]

    res_high = analysis.correlation_matrix(grid, min_abs=1.1)
    assert res_high["status"] == "ok"
    assert res_high["metrics"]["pair_count"] == 0


def test_pivot_and_group_aggfunc_and_headers():
    res1 = analysis.pivot_aggregate(PIVOT_GRID, index="Region", columns="Quarter", values="Sales", aggfunc="not_an_agg")
    assert res1["status"] == "error"
    assert res1["code"] == "INVALID_PARAM"

    res2 = analysis.pivot_aggregate(PIVOT_GRID, index="Region", columns="Quarter", values="Sales", aggfunc="SUM")
    assert res2["status"] == "ok"

    res3 = analysis.group_summary(SALES_GRID, by="Region", metrics=["Sales"], aggfunc="not_an_agg")
    assert res3["status"] == "error"
    assert res3["code"] == "INVALID_PARAM"

    res4 = analysis.run_analysis(
        {"helper": "group_summary", "params": {"by": "Region", "metrics": ["Sales"], "aggfunc": "not_an_agg"}},
        SALES_GRID,
    )
    assert res4["status"] == "error"
    assert res4["code"] == "INVALID_PARAM"

    res5 = analysis.group_summary(SALES_GRID, by="Region", metrics=["Sales"], aggfunc={"Sales": "nope"})
    assert res5["status"] == "error"
    assert res5["code"] == "INVALID_PARAM"

    res6 = analysis.group_summary(SALES_GRID, by="Region", metrics=["Sales"], aggfunc={"Nope": "sum"})
    assert res6["status"] == "error"
    assert res6["code"] == "INVALID_PARAM"

    res7 = analysis.group_summary(SALES_GRID, by="Region", metrics=["Sales"], aggfunc=["sum", "mean"])
    assert res7["status"] == "ok"
    cols7 = res7["tables"][0]["columns"]
    assert "Region" in cols7
    assert "Sales_sum" in cols7
    assert "Sales_mean" in cols7
    for col_name in cols7:
        assert "(" not in str(col_name)

    fixture = [
        ["Region", "Quarter", "Sales", "Units"],
        ["North", "Q1", 10, 1],
        ["North", "Q2", 20, 2],
        ["South", "Q1", 30, 3],
    ]
    res8 = analysis.pivot_aggregate(fixture, index="Region", columns="Quarter", values=["Sales", "Units"], aggfunc="sum")
    assert res8["status"] == "ok"
    assert res8["tables"][0]["columns"] == ["Region", "Sales_Q1", "Sales_Q2", "Units_Q1", "Units_Q2"]
    rows8 = {r[0]: r[1:] for r in res8["tables"][0]["rows"]}
    # South's Q2 sales (idx 1) and units (idx 3) are 0
    assert rows8["South"][1] == 0
    assert rows8["South"][3] == 0

    res9 = analysis.pivot_aggregate(fixture, index="Region", columns="Quarter", values="Sales", aggfunc="mean")
    assert res9["status"] == "ok"
    rows9 = {r[0]: r[1:] for r in res9["tables"][0]["rows"]}
    # South/Q2 is None, not 0
    assert rows9["South"][1] is None

    res10 = analysis.pivot_aggregate(fixture, index="Region", columns="Quarter", values="Sales", aggfunc=["sum", "mean"])
    assert res10["status"] == "ok"
    assert res10["tables"][0]["columns"] == ["Region", "sum_Q1", "sum_Q2", "mean_Q1", "mean_Q2"]
    rows10 = {r[0]: r[1:] for r in res10["tables"][0]["rows"]}
    # South mean_Q2 (idx 3) is None
    assert rows10["South"][3] is None


def test_analysis_lazy_exports_resolve():
    from plugin.scripting.analysis import _ANALYSIS_VENV_EXPORTS, _ANALYSIS_COERCE_EXPORTS
    import plugin.scripting.analysis as host_facade
    import plugin.scripting.venv.analysis as venv_analysis
    import plugin.scripting.venv.coerce as venv_coerce

    assert len(_ANALYSIS_VENV_EXPORTS) > 0
    assert len(_ANALYSIS_COERCE_EXPORTS) > 0

    for name in _ANALYSIS_VENV_EXPORTS:
        venv_obj = getattr(venv_analysis, name)
        host_obj = getattr(host_facade, name)
        assert venv_obj is host_obj, f"Export {name} did not resolve to same object"

    for name in _ANALYSIS_COERCE_EXPORTS:
        venv_obj = getattr(venv_coerce, name)
        host_obj = getattr(host_facade, name)
        assert venv_obj is host_obj, f"Coerce export {name} did not resolve to same object"
