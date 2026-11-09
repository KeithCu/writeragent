# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for analysis result sheet egress formatting."""

from __future__ import annotations

from plugin.calc.analysis_egress import format_analysis_for_calc
from plugin.scripting.domain_registry import get_domain_wiring


def test_is_analysis_result():
    is_res = get_domain_wiring("analysis").is_result
    assert is_res({"status": "ok", "helper": "describe_data", "metrics": {}})
    assert is_res({"status": "ok", "helper": "quick_stats", "metrics": {"rows": 3}})
    assert is_res({"status": "error", "code": "ANALYSIS_ERROR", "message": "fail"})
    assert is_res({"status": "error", "helper": "kpi_summary", "code": "MISSING_PARAM", "message": "kpi_summary requires metrics"})
    assert not is_res({"status": "error", "helper": "linear_programming", "code": "MISSING_PARAM", "message": "linear_programming requires c_col, a_cols, and b_col"})
    assert not is_res({"status": "error", "code": "MISSING_PARAM", "message": "need metrics"})
    assert not is_res({"status": "ok", "helper": "extract_text", "html": "<p>x</p>"})
    assert not is_res({"title": "x"})
    assert not is_res(None)


def test_optimize_missing_param_is_not_analysis():
    is_analysis_res = get_domain_wiring("analysis").is_result
    is_optimize_res = get_domain_wiring("optimize").is_result

    result = {"status": "error", "helper": "linear_programming", "code": "MISSING_PARAM", "message": "linear_programming requires c_col, a_cols, and b_col"}
    assert not is_analysis_res(result)
    assert is_optimize_res(result)


def test_format_error_result():
    grid = format_analysis_for_calc({"status": "error", "code": "MISSING_PARAM", "message": "need metrics"})
    assert grid[0][0].startswith("Analysis error")
    assert "need metrics" in grid[1][0]


def test_tabular_egress_shared_forecast_label():
    from plugin.calc.tabular_egress import format_tabular_helper_for_calc

    grid = format_tabular_helper_for_calc(
        {"status": "error", "code": "X", "message": "bad"},
        domain_label="Forecast",
        default_helper="forecast",
        failed_message="Forecast failed.",
    )
    assert grid[0][0].startswith("Forecast error")


def test_format_describe_data_shape():
    result = {
        "status": "ok",
        "helper": "describe_data",
        "context": {"range_a1": "Sheet1.A1:C10"},
        "metrics": {"row_count": 10, "column_count": 3},
        "flags": ["missing_values_in_A"],
        "tables": [
            {
                "name": "columns",
                "columns": ["col", "dtype"],
                "rows": [["Sales", "float64"], ["Region", "object"]],
                "truncated": False,
                "total_rows": 2,
            }
        ],
        "metadata": {"n_rows": 10, "numeric_cols": ["Sales"]},
    }
    grid = format_analysis_for_calc(result)
    flat = [cell for row in grid for cell in row if cell]
    assert any("describe_data" in str(cell) for cell in flat)
    assert any("Sheet1.A1:C10" in str(cell) for cell in flat)
    assert any("row_count" in str(cell) for cell in flat)
    assert any("columns" in str(cell) for cell in flat)
    assert any("Sales" in str(cell) for cell in flat)
