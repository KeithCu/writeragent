# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for Calc optimize_data threading."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from plugin.calc.optimize import OptimizeDataTool


@pytest.fixture
def calc_ctx():
    ctx = MagicMock()
    ctx.doc = MagicMock()
    ctx.ctx = MagicMock()
    ctx.doc_type = "calc"
    return ctx


@patch("plugin.scripting.optimize.insert_optimize_result_into_calc")
@patch("plugin.framework.queue_executor.execute_on_main_thread")
@patch("plugin.scripting.optimize.run_trusted_optimize")
def test_optimize_data_keeps_ipc_off_main_thread(mock_run_trusted, mock_main_thread, mock_insert, calc_ctx):
    inside = {"flag": False}

    def exec_main(fn, *args, **kwargs):
        inside["flag"] = True
        try:
            return fn(*args, **kwargs)
        finally:
            inside["flag"] = False

    def run(*_args, **_kwargs):
        assert not inside["flag"], "optimize IPC must not run inside execute_on_main_thread"
        return {"status": "ok", "helper": "linear_programming"}

    def write(*_args, **_kwargs):
        assert inside["flag"], "sheet write must run on the main thread"

    mock_main_thread.side_effect = exec_main
    mock_run_trusted.side_effect = run
    mock_insert.side_effect = write

    tool = OptimizeDataTool()
    result = tool.execute(
        calc_ctx,
        helper="linear_programming",
        data_range="Sheet1.A1:D20",
        output_range="Sheet1.F1",
    )

    assert result["status"] == "ok"
    mock_run_trusted.assert_called_once()
    mock_insert.assert_called_once()
    assert mock_insert.call_args.kwargs["sheet_name"] == "Sheet1"
    assert mock_insert.call_args.kwargs["start_col"] == 5
    assert mock_insert.call_args.kwargs["start_row"] == 0
    mock_main_thread.assert_called_once()

@patch("plugin.scripting.optimize.insert_optimize_result_into_calc")
@patch("plugin.framework.queue_executor.execute_on_main_thread")
@patch("plugin.scripting.optimize.run_trusted_optimize")
def test_optimize_data_handles_complex_output_range(mock_run_trusted, mock_main_thread, mock_insert, calc_ctx):
    mock_run_trusted.return_value = {"status": "ok", "helper": "linear_programming"}
    mock_main_thread.side_effect = lambda fn, *args, **kwargs: fn(*args, **kwargs)

    tool = OptimizeDataTool()

    # Test quoted dotted sheet
    result = tool.execute(
        calc_ctx,
        helper="linear_programming",
        data_range="Sheet1.A1:D20",
        output_range="'Q1.Sales'!B2",
    )
    assert result["status"] == "ok"
    assert mock_insert.call_args.kwargs["sheet_name"] == "Q1.Sales"
    assert mock_insert.call_args.kwargs["start_col"] == 1
    assert mock_insert.call_args.kwargs["start_row"] == 1

    # Test full range string
    result = tool.execute(
        calc_ctx,
        helper="linear_programming",
        data_range="Sheet1.A1:D20",
        output_range="Sheet1.A1:Sheet1.C10",
    )
    assert result["status"] == "ok"
    assert mock_insert.call_args.kwargs["sheet_name"] == "Sheet1"
    assert mock_insert.call_args.kwargs["start_col"] == 0
    assert mock_insert.call_args.kwargs["start_row"] == 0

    # Test absolute address (no sheet: active sheet)
    result = tool.execute(
        calc_ctx,
        helper="linear_programming",
        data_range="Sheet1.A1:D20",
        output_range="$A$1",
    )
    assert result["status"] == "ok"
    assert mock_insert.call_args.kwargs["sheet_name"] is None
    assert mock_insert.call_args.kwargs["start_col"] == 0
    assert mock_insert.call_args.kwargs["start_row"] == 0


@patch("plugin.calc.tabular_egress.CellManipulator")
@patch("plugin.calc.tabular_egress.CalcBridge")
def test_insert_optimize_result_qualifies_sheet_on_the_anchor(mock_bridge, mock_manip_cls):
    """A sheet-qualified anchor must reach write_formula_range, which uses CalcBridge.resolve."""
    del mock_bridge
    from plugin.scripting.optimize import insert_optimize_result_into_calc

    manipulator = MagicMock()
    mock_manip_cls.return_value = manipulator
    result = {"status": "ok", "helper": "linear_programming", "metrics": {"objective": 1}}

    cases = (
        ("Q1.Sales", 1, 1, "'Q1.Sales'.B2"),
        ("Report", 5, 0, "Report.F1"),
        ("Data Sheet", 2, 2, "'Data Sheet'.C3"),
        ("O'Brien", 0, 0, "'O''Brien'.A1"),
        (None, 0, 0, "A1"),
    )
    for sheet_name, col, row, expected in cases:
        manipulator.reset_mock()
        insert_optimize_result_into_calc(
            MagicMock(),
            MagicMock(),
            result,
            sheet_name=sheet_name,
            start_col=col,
            start_row=row,
        )
        assert manipulator.write_formula_range.call_args.args[0] == expected
