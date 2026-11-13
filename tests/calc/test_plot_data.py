# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for Calc plot_data tool."""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

from plugin.calc.viz import PlotDataTool
from plugin.framework.errors import ToolExecutionError


@pytest.fixture
def calc_ctx():
    ctx = MagicMock()
    ctx.doc = MagicMock()
    ctx.ctx = MagicMock()
    ctx.doc_type = "calc"
    return ctx


@patch("plugin.framework.queue_executor.main.call")
@patch("plugin.calc.analysis.run_domain")
def test_plot_data_happy_path(mock_run, mock_main_thread, calc_ctx):
    mock_run.return_value = {
        "status": "ok",
        "helper": "quick_plot",
        "title": "Quick plot",
        "image": {"__wa_payload__": "image", "format": "png", "data": b"x"},
    }
    mock_main_thread.side_effect = lambda fn, *args, **kwargs: fn(*args, **kwargs)

    tool = PlotDataTool()
    result = tool.execute(calc_ctx, helper="quick_plot", data_range="Sheet1.A1:C10")

    assert result["status"] == "ok"
    assert result.get("image_inserted") is True
    mock_run.assert_called_once()


@patch("plugin.framework.queue_executor.main.call")
@patch("plugin.calc.analysis.run_domain")
def test_plot_data_insert_failure_is_not_success(mock_run, mock_main_thread, calc_ctx):
    mock_run.return_value = {
        "status": "ok",
        "helper": "quick_plot",
        "title": "Quick plot",
        "image": {"__wa_payload__": "image", "format": "png", "data": b"x"},
    }
    mock_main_thread.side_effect = lambda fn, *args, **kwargs: fn(*args, **kwargs)

    with patch(
        "plugin.calc.python.venv.insert_image_result_on_sheet",
        side_effect=RuntimeError("target sheet has no DrawPage"),
    ):
        tool = PlotDataTool()
        result = tool.execute(calc_ctx, helper="quick_plot", data_range="Sheet1.A1:C10")

    assert result["status"] == "error"
    assert result.get("image_inserted") is False
    assert result["plot_error"] == "target sheet has no DrawPage"
    assert "not inserted" in result["message"]
    assert result["message"] != "Plot inserted on active sheet"
    image_path = result["image_path"]
    try:
        assert os.path.exists(image_path)
    finally:
        if os.path.exists(image_path):
            os.unlink(image_path)


def test_plot_data_requires_helper(calc_ctx):
    tool = PlotDataTool()
    with pytest.raises(ToolExecutionError, match="helper is required") as exc_info:
        tool.execute(calc_ctx, data_range="A1:B2")
    assert exc_info.value.code == "VIZ_ERROR"


def test_plot_data_not_registered():
    from plugin.app import get_tools

    from plugin.tests.testing_utils import CalcDocStub

    registry = get_tools()
    doc = CalcDocStub()
    names = {t.name for t in registry.get_tools(doc=doc, active_domain="analysis", exclude_tiers=())}
    assert "plot_data" not in names


@patch("plugin.calc.python.venv.insert_image_result_on_sheet")
@patch("plugin.framework.queue_executor.main.call")
@patch("plugin.calc.analysis.run_domain")
def test_plot_data_reports_like_run_venv_python_script(mock_run, mock_main_thread, mock_insert, calc_ctx):
    mock_run.return_value = {
        "status": "ok",
        "helper": "quick_plot",
        "title": "Quick plot",
        "image": {"__wa_payload__": "image", "format": "png", "data": b"x"},
    }
    mock_main_thread.side_effect = lambda fn, *args, **kwargs: fn(*args, **kwargs)

    result = PlotDataTool().execute(calc_ctx, helper="quick_plot", data_range="Sheet1.A1:C10")

    assert result["status"] == "ok"
    assert result["image_inserted"] is True
    assert result["message"] == "1 plot(s) inserted on active sheet"
    assert result["result"] == {"status": "ok", "helper": "quick_plot", "title": "Quick plot", "image": "<image>"}
    assert "image_path" not in result
    assert mock_insert.call_args.kwargs["doc"] is calc_ctx.doc
    assert mock_insert.call_args.kwargs["image_count"] == 1


@patch("plugin.calc.analysis.run_domain")
def test_plot_data_without_image_is_viz_error(mock_run, calc_ctx):
    mock_run.return_value = {"status": "ok", "helper": "quick_plot"}

    result = PlotDataTool().execute(calc_ctx, helper="quick_plot", data_range="Sheet1.A1:C10")

    assert result["status"] == "error"
    assert result["code"] == "VIZ_ERROR"


@patch("plugin.calc.analysis.run_domain")
def test_plot_data_outside_calc_returns_the_image_file(mock_run):
    mock_run.return_value = {
        "status": "ok",
        "helper": "quick_plot",
        "title": "Quick plot",
        "image": {"__wa_payload__": "image", "format": "png", "data": b"x"},
    }
    writer_ctx = MagicMock()
    writer_ctx.doc_type = "writer"

    result = PlotDataTool().execute(writer_ctx, helper="quick_plot")
    image_path = result["image_path"]
    try:
        assert result["status"] == "ok"
        assert "image_inserted" not in result
        with open(image_path, "rb") as handle:
            assert handle.read() == b"x"
    finally:
        if os.path.exists(image_path):
            os.unlink(image_path)
