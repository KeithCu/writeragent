# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for Calc forecast_data tool and analysis domain wiring."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from plugin.calc.forecast import ForecastDataTool
from plugin.scripting.forecast import HELPER_NAMES


@pytest.fixture
def calc_ctx():
    ctx = MagicMock()
    ctx.doc = MagicMock()
    ctx.ctx = MagicMock()
    ctx.doc_type = "calc"
    return ctx


def test_forecast_data_lists_helpers():
    tool = ForecastDataTool()
    for name in HELPER_NAMES:
        assert name in tool.description


def test_forecast_data_requires_helper(calc_ctx):
    tool = ForecastDataTool()
    result = tool.execute(calc_ctx, data=[["Date"], [1]])
    assert result["status"] == "error"
    assert "helper" in result["message"].lower()


def test_forecast_data_requires_data_source(calc_ctx):
    tool = ForecastDataTool()
    result = tool.execute(calc_ctx, helper="forecast_time_series")
    assert result["status"] == "error"
    assert "data_range" in result["message"].lower() or "data" in result["message"].lower()


@patch("plugin.framework.queue_executor.execute_on_main_thread")
@patch("plugin.scripting.forecast.run_trusted_forecast")
def test_forecast_data_happy_path(mock_run_trusted, mock_main_thread, calc_ctx):
    mock_run_trusted.return_value = {"status": "ok", "helper": "forecast_time_series", "metrics": {"periods": 6}}
    mock_main_thread.side_effect = lambda fn, *args, **kwargs: fn(*args, **kwargs)

    tool = ForecastDataTool()
    result = tool.execute(
        calc_ctx,
        helper="forecast_time_series",
        data_range="Sheet1.A1:B37",
        task_hint="monthly sales",
    )

    assert result["status"] == "ok"
    assert result["helper"] == "forecast_time_series"
    mock_run_trusted.assert_called_once()
    _, kwargs = mock_run_trusted.call_args
    assert kwargs["helper"] == "forecast_time_series"
    assert kwargs["data_range"] == "Sheet1.A1:B37"


@patch("plugin.scripting.forecast.insert_forecast_result_into_calc")
@patch("plugin.framework.queue_executor.execute_on_main_thread")
@patch("plugin.scripting.forecast.run_trusted_forecast")
def test_forecast_data_keeps_ipc_off_main_thread(mock_run_trusted, mock_main_thread, mock_insert, calc_ctx):
    inside = {"flag": False}

    def exec_main(fn, *args, **kwargs):
        inside["flag"] = True
        try:
            return fn(*args, **kwargs)
        finally:
            inside["flag"] = False

    def run(*_args, **_kwargs):
        assert not inside["flag"], "forecast IPC must not run inside execute_on_main_thread"
        return {"status": "ok", "helper": "forecast_time_series"}

    def write(*_args, **_kwargs):
        assert inside["flag"], "sheet write must run on the main thread"

    mock_main_thread.side_effect = exec_main
    mock_run_trusted.side_effect = run
    mock_insert.side_effect = write

    tool = ForecastDataTool()
    result = tool.execute(
        calc_ctx,
        helper="forecast_time_series",
        data_range="Sheet1.A1:B37",
        output_range="Sheet1.D1",
    )

    assert result["status"] == "ok"
    mock_run_trusted.assert_called_once()
    mock_insert.assert_called_once()
    assert mock_insert.call_args.kwargs["sheet_name"] == "Sheet1"
    assert mock_insert.call_args.kwargs["start_col"] == 3
    assert mock_insert.call_args.kwargs["start_row"] == 0
    mock_main_thread.assert_called_once()


@patch("plugin.scripting.forecast.insert_forecast_result_into_calc")
@patch("plugin.framework.queue_executor.execute_on_main_thread")
@patch("plugin.scripting.forecast.run_trusted_forecast")
def test_forecast_data_output_range_keeps_sheet_prefix(mock_run_trusted, mock_main_thread, mock_insert, calc_ctx):
    mock_run_trusted.return_value = {"status": "ok", "helper": "forecast_time_series"}
    mock_main_thread.side_effect = lambda fn, *args, **kwargs: fn(*args, **kwargs)

    tool = ForecastDataTool()
    result = tool.execute(
        calc_ctx,
        helper="forecast_time_series",
        data_range="Sheet1.A1:B37",
        output_range="'Q1.Sales'!B2",
    )
    assert result["status"] == "ok"
    assert mock_insert.call_args.kwargs["sheet_name"] == "Q1.Sales"
    assert mock_insert.call_args.kwargs["start_col"] == 1
    assert mock_insert.call_args.kwargs["start_row"] == 1

    result = tool.execute(
        calc_ctx,
        helper="forecast_time_series",
        data_range="Sheet1.A1:B37",
        output_range="Sheet1.A1:Sheet1.C10",
    )
    assert result["status"] == "ok"
    assert mock_insert.call_args.kwargs["sheet_name"] == "Sheet1"
    assert mock_insert.call_args.kwargs["start_col"] == 0
    assert mock_insert.call_args.kwargs["start_row"] == 0

    result = tool.execute(
        calc_ctx,
        helper="forecast_time_series",
        data_range="Sheet1.A1:B37",
        output_range="$A$1",
    )
    assert result["status"] == "ok"
    assert mock_insert.call_args.kwargs["sheet_name"] is None
    assert mock_insert.call_args.kwargs["start_col"] == 0
    assert mock_insert.call_args.kwargs["start_row"] == 0


@patch("plugin.calc.tabular_egress.CellManipulator")
@patch("plugin.calc.tabular_egress.CalcBridge")
def test_insert_forecast_result_qualifies_sheet_on_the_anchor(mock_bridge, mock_manip_cls):
    """A sheet-qualified anchor must reach write_formula_range, which uses CalcBridge.resolve."""
    del mock_bridge
    from plugin.scripting.forecast import insert_forecast_result_into_calc

    manipulator = MagicMock()
    mock_manip_cls.return_value = manipulator
    result = {"status": "ok", "helper": "forecast_time_series", "metrics": {"periods": 6}}

    cases = (
        ("Q1.Sales", 1, 1, "'Q1.Sales'.B2"),
        ("Report", 3, 0, "Report.D1"),
        ("Data Sheet", 2, 2, "'Data Sheet'.C3"),
        ("O'Brien", 0, 0, "'O''Brien'.A1"),
        (None, 0, 0, "A1"),
    )
    for sheet_name, col, row, expected in cases:
        manipulator.reset_mock()
        insert_forecast_result_into_calc(
            MagicMock(),
            MagicMock(),
            result,
            sheet_name=sheet_name,
            start_col=col,
            start_row=row,
        )
        assert manipulator.write_formula_range.call_args.args[0] == expected


@patch("plugin.scripting.forecast.client_run_forecast")
@patch("plugin.calc.analysis_runner.calc_tool_context", return_value=MagicMock())
@patch("plugin.calc.calc_addin_data._resolve_python_data", return_value=([["Date", "Value"], ["2024-01-01", 1.0]], None))
@patch("plugin.framework.queue_executor.execute_on_main_thread")
@patch("plugin.framework.thread_guard.on_main_thread", return_value=False)
def test_run_trusted_forecast_resolves_on_main_and_runs_client_off_main(mock_on_main, mock_main_thread, _mock_resolve, _mock_ctx, mock_client):
    del mock_on_main
    inside = {"flag": False}

    def exec_main(fn, *args, **kwargs):
        inside["flag"] = True
        try:
            return fn(*args, **kwargs)
        finally:
            inside["flag"] = False

    def client(*_args, **_kwargs):
        assert not inside["flag"], "client_run_forecast must stay off the UNO hop"
        return {"status": "ok", "helper": "forecast_time_series"}

    mock_main_thread.side_effect = exec_main
    mock_client.side_effect = client

    from plugin.scripting.forecast import run_trusted_forecast

    doc = MagicMock()
    doc.getCurrentController.return_value.getActiveSheet.return_value.getName.return_value = "Sheet1"
    result = run_trusted_forecast(MagicMock(), doc, helper="forecast_time_series", data_range="Sheet1.A1:B2")

    assert result["status"] == "ok"
    mock_main_thread.assert_called_once()
    mock_client.assert_called_once()


def test_forecast_data_rejects_raw_data_in_analysis_domain(calc_ctx):
    calc_ctx.active_domain = "analysis"
    tool = ForecastDataTool()
    result = tool.execute(
        calc_ctx,
        helper="forecast_time_series",
        data=[["Date", "Value"], ["2024-01-01", 1]],
    )
    assert result["status"] == "error"
    assert "data_range" in result["message"].lower()
