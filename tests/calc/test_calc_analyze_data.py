# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for Calc analyze_data tool and analysis domain wiring."""

from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock, patch

import pytest

from plugin.calc.analysis import (
    AnalyzeDataTool,
    GoalSeekTool,
    SolverTool,
    parse_output_anchor as _output_anchor,
    resolve_cell_address,
    write_anchor_result,
)
from plugin.calc.forecast import ForecastDataTool
from plugin.calc.optimize import OptimizeDataTool
from plugin.calc.viz import PlotDataTool
from plugin.framework.errors import DocumentDisposedError, ToolExecutionError


@pytest.fixture
def calc_ctx():
    ctx = MagicMock()
    ctx.doc = MagicMock()
    ctx.ctx = MagicMock()
    ctx.doc_type = "calc"
    return ctx


def test_output_anchor_quoted_dotted_sheet_and_range():
    """rsplit('.') dropped quoted/dotted sheets and used the range's end cell."""

    assert _output_anchor("'Q1.Sales'!B2") == ("Q1.Sales", 1, 1)
    assert _output_anchor("'Data Sheet'.C3") == ("Data Sheet", 2, 2)
    assert _output_anchor("Sheet1.A1:Sheet1.C10") == ("Sheet1", 0, 0)
    assert _output_anchor("$A$1:$C$5") == (None, 0, 0)
    assert _output_anchor("Sheet1.$B$2") == ("Sheet1", 1, 1)
    assert _output_anchor("A1") == (None, 0, 0)


@patch("plugin.calc.analysis.insert_analysis_result_into_calc")
@patch("plugin.framework.queue_executor.main.call")
@patch("plugin.calc.analysis.run_domain")
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


@patch("plugin.calc.analysis.insert_analysis_result_into_calc")
@patch("plugin.framework.queue_executor.main.call")
@patch("plugin.calc.analysis.run_domain")
def test_analyze_data_sheet_write_failure_keeps_the_result(
    mock_run_trusted,
    mock_main_thread,
    mock_insert,
    calc_ctx,
):
    mock_run_trusted.return_value = {
        "status": "ok",
        "helper": "describe_data",
        "metrics": {"row_count": 1},
    }
    mock_main_thread.side_effect = lambda fn, *a, **k: fn(*a, **k)
    mock_insert.side_effect = RuntimeError("disk full")

    result = AnalyzeDataTool().execute(
        calc_ctx,
        helper="describe_data",
        data_range="A1:B2",
        output_range="Sheet1.D1",
    )

    assert result["status"] == "ok"
    assert result["sheet_write_error"] == "disk full"
    assert result["metrics"] == {"row_count": 1}


@patch("plugin.calc.analysis.insert_analysis_result_into_calc")
@patch("plugin.framework.queue_executor.main.call")
@patch("plugin.calc.analysis.run_domain")
def test_analyze_data_sheet_write_reraises_disposed(
    mock_run_trusted,
    mock_main_thread,
    mock_insert,
    calc_ctx,
):
    mock_run_trusted.return_value = {
        "status": "ok",
        "helper": "describe_data",
        "metrics": {"row_count": 1},
    }
    mock_main_thread.side_effect = lambda fn, *a, **k: fn(*a, **k)
    mock_insert.side_effect = _disposed()

    with pytest.raises(DocumentDisposedError):
        AnalyzeDataTool().execute(
            calc_ctx,
            helper="describe_data",
            data_range="A1:B2",
            output_range="Sheet1.D1",
        )


def test_write_anchor_result_records_ordinary_write_error(calc_ctx):
    with patch(
        "plugin.framework.queue_executor.main.call",
        side_effect=RuntimeError("disk full"),
    ):
        res = write_anchor_result(
            calc_ctx,
            {"status": "ok"},
            "Sheet1.D1",
            lambda *a, **k: None,
        )
    assert res["sheet_write_error"] == "disk full"
    assert res["status"] == "ok"


def test_write_anchor_result_reraises_disposed(calc_ctx):
    with (
        patch(
            "plugin.framework.queue_executor.main.call",
            side_effect=_disposed(),
        ),
        pytest.raises(DocumentDisposedError),
    ):
        write_anchor_result(
            calc_ctx,
            {"status": "ok"},
            "Sheet1.D1",
            lambda *a, **k: None,
        )


def test_write_anchor_result_skips_without_anchor_or_on_error(calc_ctx):
    with patch("plugin.framework.queue_executor.main.call") as mock_call:
        ok_res = {"status": "ok", "helper": "describe_data"}
        assert write_anchor_result(calc_ctx, ok_res, None, lambda *a, **k: None) is ok_res
        err_res = {"status": "error", "message": "failed"}
        assert write_anchor_result(calc_ctx, err_res, "Sheet1.D1", lambda *a, **k: None) is err_res
        mock_call.assert_not_called()


def test_goal_seek_reraises_disposed(calc_ctx):
    boom = _disposed()
    with (
        patch("plugin.calc.analysis.resolve_cell_address", side_effect=boom),
        pytest.raises(type(boom), match="URP"),
    ):
        GoalSeekTool().execute(calc_ctx, formula_cell="A1", variable_cell="B1", target_value=1)


def test_goal_seek_lets_unexpected_errors_through(calc_ctx):
    with (
        patch("plugin.calc.analysis.resolve_cell_address", side_effect=ValueError("seek failed")),
        pytest.raises(ValueError, match="seek failed"),
    ):
        GoalSeekTool().execute(calc_ctx, formula_cell="A1", variable_cell="B1", target_value=1)


def test_solver_reraises_disposed(calc_ctx):
    # Solver imports sheet UNO types before its execution. Unit tests have no live
    # office, so install stubs and fail inside CalcBridge.
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
        patch("plugin.calc.analysis.resolve_cell_address", side_effect=boom),
        pytest.raises(type(boom), match="URP"),
    ):
        SolverTool().execute(calc_ctx, objective_cell="C1", variables=["A1"])


class TestResolveCellAddress:
    def test_resolve_cell_address_valid(self):
        mock_cell = MagicMock()
        mock_cell_addr = MagicMock()
        mock_cell.getCellAddress.return_value = mock_cell_addr

        mock_sheet = MagicMock()
        mock_sheet.getCellByPosition.return_value = mock_cell

        doc = MagicMock()
        controller = MagicMock()
        controller.getActiveSheet.return_value = mock_sheet
        doc.getCurrentController.return_value = controller

        assert resolve_cell_address(doc, "A1") is mock_cell_addr
        mock_sheet.getCellByPosition.assert_called_once_with(0, 0)

    def test_resolve_cell_address_invalid_raises(self):
        doc = MagicMock()
        with pytest.raises(ToolExecutionError, match="Cannot resolve cell address") as exc_info:
            resolve_cell_address(doc, "BadRef")
        assert exc_info.value.code == "INVALID_PARAM"


def test_analyze_data_requires_helper(calc_ctx):
    tool = AnalyzeDataTool()
    with pytest.raises(ToolExecutionError) as exc_info:
        tool.execute(calc_ctx, data=[["A"], [1]])
    assert exc_info.value.code == "ANALYSIS_ERROR"
    assert "helper" in str(exc_info.value).lower()


def test_analyze_data_requires_data_source(calc_ctx):
    tool = AnalyzeDataTool()
    with pytest.raises(ToolExecutionError) as exc_info:
        tool.execute(calc_ctx, helper="describe_data")
    assert exc_info.value.code == "ANALYSIS_ERROR"
    assert "data_range" in str(exc_info.value).lower() or "data" in str(exc_info.value).lower()


@patch("plugin.framework.queue_executor.main.call")
@patch("plugin.calc.analysis.run_domain")
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
    # main.call is now inside run_domain for reading data only,
    # but since run_domain is mocked entirely here, main_thread won't be called.
    mock_run_trusted.assert_called_once()
    _, kwargs = mock_run_trusted.call_args
    assert kwargs["helper"] == "describe_data"
    assert kwargs["data_range"] == "Sheet1.A1:B2"
    assert kwargs["task_hint"] == "sales summary"


@patch("plugin.framework.queue_executor.main.is_current", return_value=False)
@patch("plugin.framework.queue_executor.main.call")
@patch("plugin.scripting.client.run_analysis")
@patch("plugin.calc.calc_addin_data.resolve_python_data")
def test_analyze_data_resolves_data_on_main_thread_before_venv(mock_resolve, mock_run_analysis, mock_main_thread, mock_on_main, calc_ctx):
    del mock_on_main
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


@patch("plugin.framework.queue_executor.main.call")
@patch("plugin.calc.analysis.run_domain")
def test_analyze_data_worker_error(mock_run_trusted, mock_main_thread, calc_ctx):
    calc_ctx.active_domain = None
    mock_main_thread.side_effect = lambda fn, *args, **kwargs: fn(*args, **kwargs)
    mock_run_trusted.side_effect = ToolExecutionError("worker failed", code="ANALYSIS_ERROR")

    tool = AnalyzeDataTool()
    with pytest.raises(ToolExecutionError, match="worker failed") as exc_info:
        tool.execute(calc_ctx, helper="describe_data", data=[["x"], [1]])

    assert exc_info.value.code == "ANALYSIS_ERROR"


def test_analyze_data_forces_data_range_in_analysis_domain(calc_ctx):
    """In the analysis specialized domain the sub-agent must only ever see/pass ranges.

    Raw `data` values are stripped from the schema (get_parameters) and rejected at
    runtime. This keeps bulk data out of the sub-agent LLM context (see
    docs/calc/analysis-sub-agent.md § Data Handoff).
    """
    calc_ctx.active_domain = "analysis"
    tool = AnalyzeDataTool()

    # The actual path used by specialized sub-agents (see specialized_base.py) goes through
    # SmolToolAdapter, which builds the inputs the LLM sees. Verify no data here too.
    from plugin.chatbot.smol_agent import SmolToolAdapter

    adapter = SmolToolAdapter(tool, calc_ctx, safe=True, inputs_style="specialized")
    assert "data" not in adapter.inputs
    assert "data_range" in adapter.inputs

    # Runtime guard: even if someone bypasses the schema and passes data, reject for analysis.
    with pytest.raises(ToolExecutionError) as exc_info:
        tool.execute(calc_ctx, helper="describe_data", data=[["Region"], ["North"]])
    assert exc_info.value.code == "INVALID_PARAM"
    assert "data_range" in str(exc_info.value).lower()
    assert "out-of-band" in str(exc_info.value).lower()

    # data_range path remains valid even under analysis domain (the resolver will be mocked in other tests).
    # Here we just check it doesn't hit the "data not allowed" error before the data source check.
    with pytest.raises(ToolExecutionError) as exc_info2:
        tool.execute(calc_ctx, helper="describe_data")  # no data_range and no data
    assert exc_info2.value.code == "ANALYSIS_ERROR"
    assert "data_range" in str(exc_info2.value).lower() or "data" in str(exc_info2.value).lower()


@pytest.mark.parametrize(
    ("tool_cls", "helper_name"),
    [
        pytest.param(AnalyzeDataTool, "describe_data", id="analyze_data"),
        pytest.param(ForecastDataTool, "forecast_time_series", id="forecast_data"),
        pytest.param(OptimizeDataTool, "linear_programming", id="optimize_data"),
        pytest.param(PlotDataTool, "quick_plot", id="plot_data"),
    ],
)
def test_data_tools_let_disposal_through(calc_ctx, tool_cls, helper_name):
    tool = tool_cls()
    calc_ctx.active_domain = None
    boom = _disposed()
    with (
        patch("plugin.calc.analysis.run_domain", side_effect=boom),
        pytest.raises(type(boom)),
    ):
        tool.execute(calc_ctx, helper=helper_name, data_range="Sheet1.A1:B2")


@pytest.mark.parametrize(
    ("tool_cls", "helper_name", "active_domain"),
    [
        pytest.param(AnalyzeDataTool, "describe_data", "analysis", id="analyze_in_analysis"),
        pytest.param(ForecastDataTool, "forecast_time_series", "forecast", id="forecast_in_forecast"),
        pytest.param(ForecastDataTool, "forecast_time_series", "analysis", id="forecast_in_analysis"),
        pytest.param(OptimizeDataTool, "linear_programming", "optimize", id="optimize_in_optimize"),
        pytest.param(PlotDataTool, "quick_plot", "analysis", id="plot_in_analysis"),
        pytest.param(PlotDataTool, "quick_plot", "viz", id="plot_in_viz"),
    ],
)
def test_raw_data_refused_in_range_only_domains(calc_ctx, tool_cls, helper_name, active_domain):
    tool = tool_cls()
    calc_ctx.active_domain = active_domain
    with (
        patch("plugin.calc.analysis.run_domain") as mock_run,
        pytest.raises(ToolExecutionError) as exc_info,
    ):
        tool.execute(calc_ctx, helper=helper_name, data=[["x"], [1]])
    assert exc_info.value.code == "INVALID_PARAM"
    mock_run.assert_not_called()


def test_raw_data_allowed_outside_owning_and_analysis_domains(calc_ctx):
    calc_ctx.active_domain = "forecast"
    tool = AnalyzeDataTool()
    with patch("plugin.calc.analysis.run_domain", return_value={"status": "ok"}) as mock_run:
        res = tool.execute(calc_ctx, helper="describe_data", data=[["x"], [1]])
    assert res["status"] == "ok"
    assert mock_run.call_args.kwargs["data"] == [["x"], [1]]
