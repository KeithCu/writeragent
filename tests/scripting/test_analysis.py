# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for plugin.scripting.analysis and analysis_coerce."""

from __future__ import annotations

import pytest

pytest.importorskip("pandas")
pytest.importorskip("sklearn")

from plugin.scripting import analysis
from plugin.scripting.analysis_coerce import coerce_to_dataframe


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


def test_describe_data_basic():
    result = analysis.describe_data(SALES_GRID)
    assert result["status"] == "ok"
    assert result["helper"] == "describe_data"
    assert result["metrics"]["row_count"] == 4
    assert any(col["name"] == "Sales" for col in result["columns"])
    assert result["tables"][0]["name"] == "describe"


def test_kpi_summary():
    result = analysis.kpi_summary(SALES_GRID, ["Sales", "Units"])
    assert result["status"] == "ok"
    table = result["tables"][0]
    assert "metric" in table["columns"]
    assert result["writer_cleanup_hints"]["markdown_table"].startswith("|")


def test_detect_outliers_iqr():
    grid = [["Value"], [1], [2], [3], [4], [100]]
    result = analysis.detect_outliers(grid, method="iqr")
    assert result["status"] == "ok"
    assert result["metrics"]["outlier_count"] >= 1


def test_quick_stats_tooltip():
    qs = analysis.QuickStats(SALES_GRID)
    result = qs.tooltip()
    assert result["status"] == "ok"
    assert result["metrics"]["record_count"] == 4
    assert result["tables"][0]["columns"] == ["Metric", "Value"]


def test_format_currency_and_percent():
    assert analysis.format_currency([1234.5]) == ["$1,234.50"]
    assert analysis.format_percent([0.125]) == ["12.5%"]


def test_clean_and_prepare_fills_missing():
    result = analysis.clean_and_prepare(SALES_GRID, fill_numeric="median")
    assert result["status"] == "ok"
    assert result["metrics"]["row_count"] == 4


def test_pivot_aggregate():
    result = analysis.pivot_aggregate(PIVOT_GRID, index="Region", columns="Quarter", values="Sales", aggfunc="sum")
    assert result["status"] == "ok"
    assert result["tables"][0]["total_rows"] >= 2


def test_group_summary():
    result = analysis.group_summary(SALES_GRID, by="Region", metrics=["Sales"], aggfunc="sum")
    assert result["status"] == "ok"
    assert result["metrics"]["group_count"] >= 2


def test_compare_periods_yoy():
    result = analysis.compare_periods(DATE_GRID, date_col="Date", value_col="Revenue", period="Y")
    assert result["status"] == "ok"
    assert "change" in result["tables"][0]["columns"]


def test_correlation_matrix():
    grid = [["a", "b", "c"], [1, 2, 3], [2, 4, 6], [3, 6, 9]]
    result = analysis.correlation_matrix(grid)
    assert result["status"] == "ok"
    assert result["metrics"]["pair_count"] >= 1


def test_run_regression_linear():
    grid = [["x", "y"], [1, 2], [2, 4], [3, 6], [4, 8]]
    result = analysis.run_regression(grid, target="y", features=["x"])
    assert result["status"] == "ok"
    assert result["metrics"]["r_squared"] == pytest.approx(1.0, abs=1e-4)


def test_cluster_numeric():
    grid = [["a", "b"], [1, 1], [1.1, 1.2], [5, 5], [5.2, 4.8]]
    result = analysis.cluster_numeric(grid, n_clusters=2)
    assert result["status"] == "ok"
    assert result["metrics"]["n_clusters"] == 2


def test_monte_carlo_percentiles():
    result = analysis.monte_carlo(100, 0.1, n=5000, seed=42)
    assert result["status"] == "ok"
    assert result["metrics"]["p50"] == pytest.approx(100, rel=0.05)
    assert result["metrics"]["p5"] < result["metrics"]["p95"]


def test_run_analysis_dispatches_helper():
    result = analysis.run_analysis("describe_data", SALES_GRID)
    assert result["status"] == "ok"
    assert result["helper"] == "describe_data"


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
    assert result["tables"][0]["total_rows"] <= analysis.MAX_TABLE_ROWS
