# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for Calc analyze_data tool and analysis domain wiring."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from plugin.calc.analysis import AnalyzeDataTool


@pytest.fixture
def calc_ctx():
    ctx = MagicMock()
    ctx.doc = MagicMock()
    ctx.ctx = MagicMock()
    ctx.doc_type = "calc"
    return ctx


def test_output_anchor_quoted_dotted_sheet_and_range():
    """rsplit('.') dropped quoted/dotted sheets and used the range's end cell."""
    from plugin.calc.analysis import _output_anchor

    assert _output_anchor("'Q1.Sales'!B2") == ("Q1.Sales", 1, 1)
    assert _output_anchor("'Data Sheet'.C3") == ("Data Sheet", 2, 2)
    assert _output_anchor("Sheet1.A1:Sheet1.C10") == ("Sheet1", 0, 0)
    assert _output_anchor("$A$1:$C$5") == (None, 0, 0)
    assert _output_anchor("Sheet1.$B$2") == ("Sheet1", 1, 1)
    assert _output_anchor("A1") == (None, 0, 0)


@patch("plugin.calc.analysis_egress.insert_analysis_result_into_calc")
@patch("plugin.framework.queue_executor.execute_on_main_thread")
@patch("plugin.calc.analysis_runner.run_trusted_analysis")
def test_analyze_data_output_range_quoted_sheet(mock_run_trusted, mock_main_thread, mock_insert, calc_ctx):
    mock_run_trusted.return_value = {"status": "ok", "helper": "describe_data"}
    mock_main_thread.side_effect = lambda fn, *args, **kwargs: fn(*args, **kwargs)

    tool = AnalyzeDataTool()
    result = tool.execute(
        calc_ctx,
        helper="describe_data",
        data_range="A1:B2",
        output_range="'Q1.Sales'!B2",
    )

    assert result["status"] == "ok"
    mock_insert.assert_called_once()
    assert mock_insert.call_args.kwargs["sheet_name"] == "Q1.Sales"
    assert mock_insert.call_args.kwargs["start_col"] == 1
    assert mock_insert.call_args.kwargs["start_row"] == 1


def _disposed():
    class DisposedException(Exception):
        pass

    return DisposedException("Binary URP bridge disposed during call")


def test_goal_seek_reraises_disposed(calc_ctx):
    from plugin.calc.analysis import GoalSeekTool

    boom = _disposed()
    with (
        patch("plugin.calc.analysis.UNO_AVAILABLE", True),
        patch("plugin.calc.analysis.CalcBridge", side_effect=boom),
        pytest.raises(type(boom), match="URP"),
    ):
        GoalSeekTool().execute(calc_ctx, formula_cell="A1", variable_cell="B1", target_value=1)


def test_goal_seek_wraps_other_errors(calc_ctx):
    from plugin.calc.analysis import GoalSeekTool
    from plugin.framework.errors import ToolExecutionError

    with (
        patch("plugin.calc.analysis.UNO_AVAILABLE", True),
        patch("plugin.calc.analysis.CalcBridge", side_effect=ValueError("seek failed")),
        pytest.raises(ToolExecutionError, match="seek failed"),
    ):
        GoalSeekTool().execute(calc_ctx, formula_cell="A1", variable_cell="B1", target_value=1)


def test_solver_reraises_disposed(calc_ctx):
    import sys
    import types

    from plugin.calc.analysis import SolverTool

    # Solver imports sheet UNO types before its try. Unit tests have no live
    # office, so install stubs and fail inside the try on CalcBridge.
    star = types.ModuleType("com.sun.star")
    sheet = types.ModuleType("com.sun.star.sheet")
    ops = types.ModuleType("com.sun.star.sheet.SolverConstraintOperator")
    sheet.SolverConstraint = type("SolverConstraint", (), {})
    ops.EQUAL = ops.GREATER_EQUAL = ops.LESS_EQUAL = 0
    modules = {
        "com": types.ModuleType("com"),
        "com.sun": types.ModuleType("com.sun"),
        "com.sun.star": star,
        "com.sun.star.sheet": sheet,
        "com.sun.star.sheet.SolverConstraintOperator": ops,
    }
    boom = _disposed()
    with (
        patch.dict(sys.modules, modules),
        patch("plugin.calc.analysis.UNO_AVAILABLE", True),
        patch("plugin.calc.analysis.CalcBridge", side_effect=boom),
        pytest.raises(type(boom), match="URP"),
    ):
        SolverTool().execute(calc_ctx, objective_cell="C1", variables=["A1"])


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
    # execute_on_main_thread is now inside run_trusted_analysis for reading data only,
    # but since run_trusted_analysis is mocked entirely here, main_thread won't be called.
    mock_run_trusted.assert_called_once()
    _, kwargs = mock_run_trusted.call_args
    assert kwargs["helper"] == "describe_data"
    assert kwargs["data_range"] == "Sheet1.A1:B2"
    assert kwargs["task_hint"] == "sales summary"


@patch("plugin.framework.queue_executor.execute_on_main_thread")
@patch("plugin.calc.analysis_runner.run_analysis")
@patch("plugin.calc.calc_addin_data._resolve_python_data")
def test_analyze_data_resolves_data_on_main_thread_before_venv(mock_resolve, mock_run_analysis, mock_main_thread, calc_ctx):
    call_order: list[str] = []

    def main_thread(fn, *args, **kwargs):
        call_order.append("main")
        return fn(*args, **kwargs)

    def run_side(*args, **kwargs):
        call_order.append("venv")
        return {"status": "ok", "helper": "describe_data"}

    mock_main_thread.side_effect = main_thread
    mock_run_analysis.side_effect = run_side
    mock_resolve.return_value = ({"col1": [1, 2]}, None)

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


def test_analysis_domain_tools_not_registered():
    from plugin.main import get_tools

    from plugin.tests.testing_utils import CalcDocStub

    registry = get_tools()
    doc = CalcDocStub()
    names = {t.name for t in registry.get_tools(doc=doc, active_domain="analysis", exclude_tiers=())}
    assert "analyze_data" not in names
    assert "plot_data" not in names
    assert "calc_goal_seek" not in names
    assert "calc_solver" not in names


def test_analyze_data_not_in_default_core_list():
    from plugin.main import get_tools

    from plugin.tests.testing_utils import CalcDocStub

    registry = get_tools()
    doc = CalcDocStub()
    names = {t.name for t in registry.get_tools(doc=doc)}
    assert "analyze_data" not in names
    assert "calc_goal_seek" not in names


def test_delegate_calc_gateway_omits_python_and_analysis():
    from plugin.calc.specialized import DelegateToSpecializedCalc

    gateway = DelegateToSpecializedCalc()
    domains = gateway.parameters["properties"]["domain"]["enum"]
    assert "python" not in domains
    assert "analysis" not in domains
    assert "solvers" not in domains


def test_analyze_data_forces_data_range_in_analysis_domain(calc_ctx):
    """In the analysis specialized domain the sub-agent must only ever see/pass ranges.

    Raw `data` values are stripped from the schema (get_parameters) and rejected at
    runtime. This keeps bulk data out of the sub-agent LLM context (see
    docs/calc/analysis-sub-agent.md § Data Handoff).
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

    adapter = SmolToolAdapter(tool, calc_ctx, safe=True, inputs_style="specialized")
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
