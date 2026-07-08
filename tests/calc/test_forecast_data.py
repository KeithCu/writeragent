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
from plugin.tests.testing_utils import setup_uno_mocks

setup_uno_mocks()


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
