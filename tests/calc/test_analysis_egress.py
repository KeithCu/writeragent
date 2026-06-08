# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for analysis result sheet egress formatting."""

from __future__ import annotations

from plugin.calc.analysis_egress import format_analysis_for_calc, is_analysis_result


def test_is_analysis_result():
    assert is_analysis_result({"status": "ok", "helper": "describe_data", "metrics": {}})
    assert is_analysis_result({"status": "ok", "helper": "quick_stats", "metrics": {"rows": 3}})
    assert is_analysis_result({"status": "error", "code": "ANALYSIS_ERROR", "message": "fail"})
    assert not is_analysis_result({"status": "ok", "helper": "extract_text", "html": "<p>x</p>"})
    assert not is_analysis_result({"title": "x"})
    assert not is_analysis_result(None)


def test_format_error_result():
    grid = format_analysis_for_calc({"status": "error", "code": "MISSING_PARAM", "message": "need metrics"})
    assert grid[0][0].startswith("Analysis error")
    assert "need metrics" in grid[1][0]


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
