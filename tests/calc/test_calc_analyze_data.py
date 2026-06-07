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


@patch("plugin.framework.queue_executor.execute_on_main_thread")
@patch("plugin.calc.analysis_runner.run_trusted_analysis")
def test_analyze_data_happy_path(mock_run_trusted, mock_main_thread, calc_ctx):
    mock_run_trusted.return_value = {"status": "ok", "helper": "describe_data", "metrics": {"row_count": 1}}
    mock_main_thread.side_effect = lambda fn, *args, **kwargs: fn(*args, **kwargs)

    tool = AnalyzeDataTool()
    result = tool.execute(
        calc_ctx,
        helper="describe_data",
        data_range="Sheet1.A1:B2",
        task_hint="sales summary",
    )

    assert result["status"] == "ok"
    assert result["helper"] == "describe_data"
    mock_main_thread.assert_called_once()
    mock_run_trusted.assert_called_once()
    _, kwargs = mock_run_trusted.call_args
    assert kwargs["helper"] == "describe_data"
    assert kwargs["data_range"] == "Sheet1.A1:B2"
    assert kwargs["task_hint"] == "sales summary"


@patch("plugin.framework.queue_executor.execute_on_main_thread")
@patch("plugin.calc.analysis_runner.run_trusted_analysis")
def test_analyze_data_resolves_data_on_main_thread_before_venv(mock_run_trusted, mock_main_thread, calc_ctx):
    call_order: list[str] = []

    def main_thread(fn, *args, **kwargs):
        call_order.append("main")
        return fn(*args, **kwargs)

    def run_side(*args, **kwargs):
        call_order.append("venv")
        return {"status": "ok", "helper": "describe_data"}

    mock_main_thread.side_effect = main_thread
    mock_run_trusted.side_effect = run_side

    tool = AnalyzeDataTool()
    result = tool.execute(calc_ctx, helper="describe_data", data_range="A1:B2")

    assert result["status"] == "ok"
    assert call_order == ["main", "venv"]


@patch("plugin.framework.queue_executor.execute_on_main_thread")
@patch("plugin.calc.analysis_runner.run_trusted_analysis")
def test_analyze_data_worker_error(mock_run_trusted, mock_main_thread, calc_ctx):
    from plugin.framework.errors import ToolExecutionError

    calc_ctx.active_domain = None
    mock_main_thread.side_effect = lambda fn, *args, **kwargs: fn(*args, **kwargs)
    mock_run_trusted.side_effect = ToolExecutionError("worker failed", code="ANALYSIS_ERROR")

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
    assert "plot_data" in names
    assert "calc_goal_seek" in names
    assert "calc_solver" in names
    # Data handoff discipline: analysis sub-agents must not receive read_cell_range.
    # They discover structure via get_sheet_summary and pass addresses (data_range) for bulk data.
    # read_cell_range would cause full cell values to be stringified into sub-agent observations.
    assert "read_cell_range" not in names
    assert "get_sheet_summary" in names


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


def test_analyze_data_forces_data_range_in_analysis_domain(calc_ctx):
    """In the analysis specialized domain the sub-agent must only ever see/pass ranges.

    Raw `data` values are stripped from the schema (get_parameters) and rejected at
    runtime. This keeps bulk data out of the sub-agent LLM context (see
    docs/analysis-sub-agent.md § Data Handoff).
    """
    calc_ctx.active_domain = "analysis"
    tool = AnalyzeDataTool()

    # Schema presented to the analysis sub-agent should not contain the data property.
    schema = tool.get_parameters("calc")
    assert "data" not in (schema or {}).get("properties", {})
    assert "data_range" in (schema or {}).get("properties", {})

    # The actual path used by specialized sub-agents (see specialized_base.py) goes through
    # SmolToolAdapter, which builds the inputs the LLM sees. Verify no data here too.
    from plugin.chatbot.smol_agent import SmolToolAdapter

    adapter = SmolToolAdapter(tool, calc_ctx, safe=True, main_thread_sync=True, inputs_style="specialized")
    assert "data" not in adapter.inputs
    assert "data_range" in adapter.inputs

    # Runtime guard: even if someone bypasses the schema and passes data, reject for analysis.
    result = tool.execute(calc_ctx, helper="describe_data", data=[["Region"], ["North"]])
    assert result["status"] == "error"
    assert "data_range" in result.get("message", "").lower()
    assert "address" in result.get("message", "").lower() or "out-of-band" in result.get("message", "").lower() or "host" in result.get("message", "").lower()

    # data_range path remains valid even under analysis domain (the resolver will be mocked in other tests).
    # Here we just check it doesn't hit the "data not allowed" error before the data source check.
    result2 = tool.execute(calc_ctx, helper="describe_data")  # no data_range and no data
    assert result2["status"] == "error"
    assert "data_range" in result2.get("message", "").lower() or "data" in result2.get("message", "").lower()
