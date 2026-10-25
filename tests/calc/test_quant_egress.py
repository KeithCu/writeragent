# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for quant result sheet egress formatting."""

from __future__ import annotations

from plugin.calc.quant_egress import (
    format_quant_for_calc,
    insert_quant_result_into_calc,
    is_quant_result,
)


def test_is_quant_result_contract():
    assert is_quant_result({"status": "ok", "helper": "portfolio_tearsheet"}) is True
    assert is_quant_result({"status": "ok", "helper": "unknown_helper"}) is False
    assert is_quant_result({"status": "error", "code": "QUANT_ERROR"}) is False
    assert is_quant_result({"status": "error", "helper": "efficient_frontier", "code": "EXECUTION_ERROR"}) is True
    assert is_quant_result({"status": "ok"}) is False
    assert is_quant_result("not a dict") is False
    assert is_quant_result(None) is False
    assert callable(insert_quant_result_into_calc)


def test_warning_renders_in_flags_block():
    result = {
        "status": "ok",
        "helper": "portfolio_tearsheet",
        "metrics": {"Sharpe": 1.2},
        "warning": "Averaged duplicate dates.",
    }
    grid = format_quant_for_calc(result)
    assert ["Flags"] in grid
    assert ["Averaged duplicate dates."] in grid
    flags_idx = grid.index(["Flags"])
    assert ["Portfolio Metrics"] in grid[:flags_idx]
    assert ["Sharpe", 1.2] in grid[:flags_idx]


def test_empty_table_gets_placeholder():
    assert format_quant_for_calc(
        {"status": "ok", "helper": "technical_analysis", "table": {"columns": [], "rows": []}}
    ) == [["Quant Result: technical_analysis"], ["(no tabular output)"]]

    non_empty = format_quant_for_calc(
        {
            "status": "ok",
            "helper": "technical_analysis",
            "table": {"columns": ["Date", "SMA"], "rows": [["2024-01-01", 10.5]]},
        }
    )
    assert non_empty[0] == ["Quant Result: technical_analysis"]
    assert non_empty[1] == []
    assert non_empty[2] == ["Date", "SMA"]
    assert non_empty[3] == ["2024-01-01", 10.5]
