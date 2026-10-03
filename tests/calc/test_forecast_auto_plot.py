# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for forecast_data auto_plot chaining."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from plugin.calc.forecast import ForecastDataTool
from plugin.calc.forecast_auto_plot import build_viz_request, merge_forecast_plot_data, run_auto_plot_after_forecast, should_auto_plot
from plugin.calc.viz_auto_plot import task_hint_implies_plot
from plugin.scripting.payload_codec import PAYLOAD_CALC_RANGE


@pytest.fixture
def calc_ctx():
    ctx = MagicMock()
    ctx.doc = MagicMock()
    ctx.ctx = MagicMock()
    ctx.doc_type = "calc"
    return ctx


def test_forecast_task_hint_implies_plot_reused():
    assert task_hint_implies_plot("plot forecast with bands") is True
    assert task_hint_implies_plot("monthly forecast") is False


def test_should_auto_plot_forecast_helper():
    assert should_auto_plot(helper="forecast_time_series", auto_plot=True, task_hint=None) is True
    assert should_auto_plot(helper="decompose_time_series", auto_plot=True, task_hint="plot") is False


def test_build_viz_request_forecast():
    req = build_viz_request(
        "forecast_time_series",
        forecast_result={
            "status": "ok",
            "tables": [
                {
                    "name": "forecast",
                    "columns": ["date", "forecast", "lower", "upper"],
                    "rows": [["2025-01-01", 120.0, 115.0, 125.0]],
                }
            ],
        },
        forecast_params={"date_col": "Date", "value_col": "Value"},
    )
    assert req is not None
    helper, params = req
    assert helper == "time_series_plot"
    assert params["date_col"] == "date"
    assert params["value_col"] == "Value"
    assert params["forecast_col"] == "forecast"
    assert params["lower_col"] == "lower"
    assert params["upper_col"] == "upper"


def test_merge_forecast_plot_data():
    history = pd.DataFrame({"Date": ["2024-01-01", "2024-02-01"], "Value": [100.0, 110.0]})
    forecast_result = {
        "status": "ok",
        "tables": [
            {
                "name": "forecast",
                "columns": ["date", "forecast", "lower", "upper"],
                "rows": [["2024-03-01", 120.0, 115.0, 125.0]],
            }
        ],
    }
    merged = merge_forecast_plot_data(history, forecast_result, {"date_col": "Date", "value_col": "Value"})
    assert merged is not None
    assert merged[0] == ["date", "Value", "forecast", "lower", "upper"]
    assert len(merged) == 4


def _forecast_band_result() -> dict:
    return {
        "status": "ok",
        "tables": [
            {
                "name": "forecast",
                "columns": ["date", "forecast", "lower", "upper"],
                "rows": [["2024-03-01", 120.0, 115.0, 125.0]],
            }
        ],
    }


def test_merge_forecast_plot_data_unpacks_calc_range_envelope():
    """A packed calc_range must not become one cell. That dropped Date/Value."""
    history = {
        "__wa_payload__": PAYLOAD_CALC_RANGE,
        "shape": [3, 2],
        "address": "Sheet1.A1:B3",
        "data": [
            ["Date", "Value"],
            ["2024-01-01", 100.0],
            ["2024-02-01", 110.0],
        ],
    }
    merged = merge_forecast_plot_data(history, _forecast_band_result(), {"date_col": "Date", "value_col": "Value"})
    assert merged is not None
    assert merged[0] == ["date", "Value", "forecast", "lower", "upper"]
    assert str(merged[1][0]).startswith("2024-01-01")
    assert merged[1][1] == 100.0
    assert merged[1][2] is None
    assert str(merged[2][0]).startswith("2024-02-01")
    assert merged[3][2] == 120.0
    assert all(not isinstance(cell, dict) for row in merged for cell in row)


@patch("plugin.scripting.viz.insert_viz_result_into_doc")
@patch("plugin.calc.forecast_auto_plot.run_auto_plot_after_forecast")
@patch("plugin.framework.queue_executor.execute_on_main_thread")
@patch("plugin.scripting.forecast.run_trusted_forecast")
def test_forecast_data_auto_plot_inserts_chart(
    mock_run_trusted,
    mock_main_thread,
    mock_auto_plot,
    mock_insert,
    calc_ctx,
):
    mock_main_thread.side_effect = lambda fn, *args, **kwargs: fn(*args, **kwargs)
    mock_run_trusted.return_value = {
        "status": "ok",
        "helper": "forecast_time_series",
        "metrics": {"periods": 6},
        "tables": [{"name": "forecast", "columns": ["date", "forecast"], "rows": [["2025-01-01", 120.0]]}],
    }
    mock_auto_plot.return_value = {
        "status": "ok",
        "helper": "time_series_plot",
        "title": "Forecast plot",
        "image": {"__wa_payload__": "image", "format": "png", "data": b"x"},
    }

    tool = ForecastDataTool()
    result = tool.execute(
        calc_ctx,
        helper="forecast_time_series",
        data_range="Sheet1.A1:B37",
        auto_plot=True,
    )

    assert result["status"] == "ok"
    assert result.get("image_inserted") is True
    assert result.get("plot", {}).get("helper") == "time_series_plot"
    mock_auto_plot.assert_called_once()
    mock_insert.assert_called_once()


@patch("plugin.scripting.viz.insert_viz_result_into_doc")
@patch("plugin.calc.forecast_auto_plot.run_auto_plot_after_forecast")
@patch("plugin.framework.queue_executor.execute_on_main_thread")
@patch("plugin.scripting.forecast.run_trusted_forecast")
def test_forecast_data_auto_plot_marshals_viz_read(
    mock_run_trusted,
    mock_main_thread,
    mock_auto_plot,
    mock_insert,
    calc_ctx,
):
    inside_marshal = {"flag": False}

    def mock_execute_on_main(fn, *args, **kwargs):
        inside_marshal["flag"] = True
        try:
            return fn(*args, **kwargs)
        finally:
            inside_marshal["flag"] = False

    mock_main_thread.side_effect = mock_execute_on_main
    mock_run_trusted.return_value = {
        "status": "ok",
        "helper": "forecast_time_series",
        "metrics": {},
        "tables": [{"name": "forecast", "columns": ["date", "forecast"], "rows": []}],
    }

    def _auto_plot_side_effect(*_args, **_kwargs):
        assert not inside_marshal["flag"], "viz IPC must stay off execute_on_main_thread"
        return {
            "status": "ok",
            "helper": "time_series_plot",
            "title": "Forecast plot",
            "image": {"__wa_payload__": "image", "format": "png", "data": b"x"},
        }

    def _insert_side_effect(*_args, **_kwargs):
        assert inside_marshal["flag"], "chart insert must run on the main thread"

    mock_auto_plot.side_effect = _auto_plot_side_effect
    mock_insert.side_effect = _insert_side_effect

    tool = ForecastDataTool()
    result = tool.execute(
        calc_ctx,
        helper="forecast_time_series",
        data_range="Sheet1.A1:B37",
        auto_plot=True,
    )

    assert result["status"] == "ok"
    assert result.get("image_inserted") is True
    mock_auto_plot.assert_called_once()
    mock_insert.assert_called_once()
    assert mock_main_thread.call_count == 1


def test_auto_plot_reads_sheet_on_main_and_uses_analysis_runner_context():
    """Wrong calc_tool_context import raises AttributeError; envelope must merge."""
    envelope = {
        "__wa_payload__": PAYLOAD_CALC_RANGE,
        "shape": [3, 2],
        "address": "Sheet1.A1:B3",
        "data": [
            ["Date", "Value"],
            ["2024-01-01", 100.0],
            ["2024-02-01", 110.0],
        ],
    }
    inside = {"flag": False}

    def exec_main(fn, *args, **kwargs):
        inside["flag"] = True
        try:
            return fn(*args, **kwargs)
        finally:
            inside["flag"] = False

    def viz(*_args, **kwargs):
        assert not inside["flag"], "run_trusted_viz must stay off the sheet-read hop"
        assert kwargs["data"][0] == ["date", "Value", "forecast", "lower", "upper"]
        assert str(kwargs["data"][1][0]).startswith("2024-01-01")
        return {"status": "ok", "helper": "time_series_plot"}

    with (
        patch("plugin.framework.thread_guard.on_main_thread", return_value=False),
        patch("plugin.framework.queue_executor.execute_on_main_thread", side_effect=exec_main),
        patch("plugin.calc.analysis_runner.calc_tool_context", return_value=MagicMock()) as mock_ctx,
        patch("plugin.calc.calc_addin_data._resolve_python_data", return_value=(envelope, None)),
        patch("plugin.scripting.viz.run_trusted_viz", side_effect=viz) as mock_viz,
    ):
        result = run_auto_plot_after_forecast(
            MagicMock(),
            MagicMock(),
            forecast_helper="forecast_time_series",
            forecast_result=_forecast_band_result(),
            forecast_params={"date_col": "Date", "value_col": "Value"},
            data_range="Sheet1.A1:B3",
            auto_plot=True,
            task_hint=None,
        )

    assert result is not None
    assert result["status"] == "ok"
    mock_ctx.assert_called_once()
    mock_viz.assert_called_once()
