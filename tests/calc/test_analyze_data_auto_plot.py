# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for analyze_data auto_plot chaining."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from plugin.calc.analysis import AnalyzeDataTool
from plugin.calc.viz_auto_plot import build_viz_request, should_auto_plot, task_hint_implies_plot
from plugin.tests.testing_utils import setup_uno_mocks

setup_uno_mocks()


@pytest.fixture
def calc_ctx():
    ctx = MagicMock()
    ctx.doc = MagicMock()
    ctx.ctx = MagicMock()
    ctx.doc_type = "calc"
    return ctx


def test_task_hint_implies_plot():
    assert task_hint_implies_plot("show a histogram of sales") is True
    assert task_hint_implies_plot("describe the table") is False


def test_should_auto_plot_requires_supported_helper():
    assert should_auto_plot(helper="run_regression", auto_plot=True, task_hint=None) is True
    assert should_auto_plot(helper="describe_data", auto_plot=True, task_hint="plot chart") is False


def test_build_viz_request_regression():
    req = build_viz_request(
        "run_regression",
        analysis_result={"status": "ok"},
        analysis_params={"target": "y", "features": ["x"]},
    )
    assert req is not None
    helper, params = req
    assert helper == "plot_data"
    assert params["spec"]["chart_type"] == "scatter"


@patch("plugin.scripting.viz_egress.insert_viz_result_into_doc")
@patch("plugin.calc.viz_auto_plot.run_auto_plot_after_analysis")
@patch("plugin.framework.queue_executor.execute_on_main_thread")
@patch("plugin.calc.analysis_runner.run_trusted_analysis")
def test_analyze_data_auto_plot_inserts_chart(
    mock_run_analysis,
    mock_main_thread,
    mock_auto_plot,
    mock_insert,
    calc_ctx,
):
    mock_main_thread.side_effect = lambda fn, *args, **kwargs: fn(*args, **kwargs)
    mock_run_analysis.return_value = {"status": "ok", "helper": "run_regression", "metrics": {}}
    mock_auto_plot.return_value = {
        "status": "ok",
        "helper": "plot_data",
        "title": "Regression plot",
        "image": {"__wa_payload__": "image", "format": "png", "data": b"x"},
    }

    tool = AnalyzeDataTool()
    result = tool.execute(
        calc_ctx,
        helper="run_regression",
        data_range="Sheet1.A1:C20",
        params={"target": "y", "features": ["x"]},
        auto_plot=True,
    )

    assert result["status"] == "ok"
    assert result.get("image_inserted") is True
    assert result.get("plot", {}).get("helper") == "plot_data"
    mock_auto_plot.assert_called_once()
    mock_insert.assert_called_once()
