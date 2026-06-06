# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for Calc analyze_data tool and analysis domain wiring."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from plugin.calc.analysis import AnalyzeDataTool
from plugin.tests.testing_utils import setup_uno_mocks

setup_uno_mocks()


@pytest.fixture
def calc_ctx():
    ctx = MagicMock()
    ctx.doc = MagicMock()
    ctx.ctx = MagicMock()
    ctx.doc_type = "calc"
    return ctx


def test_analyze_data_requires_helper(calc_ctx):
    tool = AnalyzeDataTool()
    result = tool.execute(calc_ctx, data=[["A"], [1]])
    assert result["status"] == "error"
    assert "helper" in result["message"].lower()


def test_analyze_data_requires_data_source(calc_ctx):
    tool = AnalyzeDataTool()
    result = tool.execute(calc_ctx, helper="describe_data")
    assert result["status"] == "error"
    assert "data_range" in result["message"].lower() or "data" in result["message"].lower()


@patch("plugin.framework.client.analysis_client.run_analysis")
@patch("plugin.calc.analysis._resolve_python_data")
def test_analyze_data_happy_path(mock_resolve, mock_run_analysis, calc_ctx):
    grid = [["Region", "Sales"], ["North", 100]]
    mock_resolve.return_value = (grid, None)
    mock_run_analysis.return_value = {"status": "ok", "helper": "describe_data", "metrics": {"row_count": 1}}

    tool = AnalyzeDataTool()
    result = tool.execute(
        calc_ctx,
        helper="describe_data",
        data_range="Sheet1.A1:B2",
        task_hint="sales summary",
    )

    assert result["status"] == "ok"
    assert result["helper"] == "describe_data"
    mock_resolve.assert_called_once()
    mock_run_analysis.assert_called_once()
    args, kwargs = mock_run_analysis.call_args
    assert args[1]["helper"] == "describe_data"
    assert kwargs["context"]["range_a1"] == "Sheet1.A1:B2"
    assert kwargs["context"]["task_hint"] == "sales summary"


@patch("plugin.framework.client.analysis_client.run_analysis")
@patch("plugin.calc.analysis._resolve_python_data")
def test_analyze_data_worker_error(mock_resolve, mock_run_analysis, calc_ctx):
    from plugin.framework.errors import ToolExecutionError

    mock_resolve.return_value = ([["x"], [1]], None)
    mock_run_analysis.side_effect = ToolExecutionError("worker failed", code="ANALYSIS_ERROR")

    tool = AnalyzeDataTool()
    result = tool.execute(calc_ctx, helper="describe_data", data=[["x"], [1]])

    assert result["status"] == "error"
    assert "worker failed" in result["message"]


def test_analysis_domain_includes_all_tools():
    from plugin.main import get_tools

    registry = get_tools()
    doc = MagicMock()
    doc.supportsService.return_value = True
    names = {t.name for t in registry.get_tools(doc=doc, active_domain="analysis", exclude_tiers=())}
    assert "analyze_data" in names
    assert "calc_goal_seek" in names
    assert "calc_solver" in names


def test_analyze_data_not_in_default_core_list():
    from plugin.main import get_tools

    registry = get_tools()
    doc = MagicMock()
    doc.supportsService.return_value = True
    names = {t.name for t in registry.get_tools(doc=doc)}
    assert "analyze_data" not in names
    assert "calc_goal_seek" not in names


def test_delegate_calc_gateway_includes_analysis_not_solvers():
    from plugin.calc.specialized import DelegateToSpecializedCalc

    gateway = DelegateToSpecializedCalc()
    domains = gateway.parameters["properties"]["domain"]["enum"]
    assert "analysis" in domains
    assert "solvers" not in domains
