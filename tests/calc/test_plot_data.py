# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for Calc plot_data tool."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from plugin.calc.viz import PlotDataTool


@pytest.fixture
def calc_ctx():
    ctx = MagicMock()
    ctx.doc = MagicMock()
    ctx.ctx = MagicMock()
    ctx.doc_type = "calc"
    return ctx


@patch("plugin.framework.queue_executor.execute_on_main_thread")
@patch("plugin.scripting.viz.run_trusted_viz")
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


def test_plot_data_requires_helper(calc_ctx):
    tool = PlotDataTool()
    result = tool.execute(calc_ctx, data_range="A1:B2")
    assert result["status"] == "error"


def test_plot_data_not_registered():
    from plugin.main import get_tools

    from plugin.tests.testing_utils import CalcDocStub

    registry = get_tools()
    doc = CalcDocStub()
    names = {t.name for t in registry.get_tools(doc=doc, active_domain="analysis", exclude_tiers=())}
    assert "plot_data" not in names
