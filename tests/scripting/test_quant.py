# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for trusted quant helpers."""

from __future__ import annotations

import builtins
from unittest.mock import MagicMock, patch

import pytest

from plugin.scripting.client import run_quant
from plugin.scripting.quant import (
    HELPER_NAMES,
    QUANT_HEADER_PREFIX,
    get_quant_template,
)
from plugin.scripting.venv.quant import run_quant as venv_run_quant
from plugin.framework.errors import ToolExecutionError


def test_quant_template_is_executable():
    code = get_quant_template("fetch_historical_data")
    assert code is not None
    assert "from writeragent.scripting.quant import run_quant" in code
    assert "fetch_historical_data" in code
    assert QUANT_HEADER_PREFIX not in code.splitlines()[0]


def test_run_quant_missing_package(monkeypatch):
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "yfinance":
            raise ImportError("no yfinance")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    result = venv_run_quant({"helper": "fetch_historical_data", "params": {"tickers": ["AAPL"]}})
    assert result["status"] == "error"
    assert result["code"] == "MISSING_PACKAGE"


def test_run_quant_invalid_params(monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, "yfinance", object())
    result = venv_run_quant({"helper": "fetch_historical_data", "params": {}})
    assert result["status"] == "error"
    assert result["code"] == "INVALID_PARAMS"
    assert result["helper"] == "fetch_historical_data"
    from plugin.calc.quant_egress import is_quant_result

    assert is_quant_result(result) is True


def test_fetch_historical_data_calc_range(monkeypatch):
    import sys
    from plugin.scripting.calc_range import CalcRange
    import pandas as pd

    fake_yf = MagicMock()
    fake_df = pd.DataFrame({"Date": ["2023-01-01"], "Close": [150.0]})
    fake_yf.download.return_value = fake_df
    monkeypatch.setitem(sys.modules, "yfinance", fake_yf)

    cr = CalcRange([["AAPL"], ["MSFT"], [None]])
    result = venv_run_quant({"helper": "fetch_historical_data", "params": {"tickers": cr}})
    assert result["status"] == "ok"
    fake_yf.download.assert_called_once()
    assert fake_yf.download.call_args[0][0] == ["AAPL", "MSFT"]


@pytest.fixture
def ctx():
    return MagicMock()


def test_client_run_quant_happy_path(ctx):
    worker_result = {"status": "ok", "helper": "fetch_historical_data", "table": {"columns": ["Date"], "rows": []}}
    with (
        patch("plugin.scripting.client.configured_python_exec_timeout", return_value=30),
        patch("plugin.scripting.client.run_trusted_worker_action", return_value=worker_result) as mock_run,
    ):
        result = run_quant(ctx, {"helper": "fetch_historical_data", "params": {"tickers": ["AAPL"]}})

    assert result["helper"] == "fetch_historical_data"
    mock_run.assert_called_once()
    kwargs = mock_run.call_args.kwargs
    # writeragent:* ids are not sandbox sessions; the client no longer forwards one.
    assert "session_id" not in kwargs
    assert kwargs["domain"] == "quant"
    assert kwargs["helper"] == "fetch_historical_data"


def test_client_run_quant_worker_error(ctx):
    with (
        patch("plugin.scripting.client.configured_python_exec_timeout", return_value=10),
        patch(
            "plugin.scripting.client.run_trusted_worker_action",
            side_effect=ToolExecutionError("boom", code="QUANT_ERROR"),
        ),
    ):
        with pytest.raises(ToolExecutionError, match="boom"):
            run_quant(ctx, {"helper": "fetch_historical_data", "params": {"tickers": ["AAPL"]}})


def test_helper_names_cover_templates():
    for helper in HELPER_NAMES:
        assert get_quant_template(helper) is not None


def test_portfolio_tearsheet_portfolio_returns_grid():
    pytest.importorskip("quantstats")
    grid = [
        ["AAPL", "MSFT", "GOOG"],
        [0.01, 0.008, 0.012],
        [0.02, -0.01, 0.025],
        [-0.005, 0.015, 0.01],
        [0.012, 0.005, -0.008],
        [0.008, 0.02, 0.015],
        [0.015, -0.005, 0.02],
        [-0.01, 0.012, 0.005],
        [0.02, 0.008, 0.018],
        [0.005, 0.015, -0.002],
        [0.01, 0.01, 0.01],
    ]
    result = venv_run_quant({"helper": "portfolio_tearsheet", "params": {}}, data=grid)
    assert result["status"] == "ok"
    assert result["helper"] == "portfolio_tearsheet"
    assert "metrics" in result
    assert isinstance(result["metrics"], dict)
    assert "Cumulative Return" in result["metrics"] or len(result["metrics"]) > 0


def test_portfolio_tearsheet_with_date_column_and_single_column_param():
    pytest.importorskip("quantstats")
    grid = [
        ["Date", "AAPL", "MSFT"],
        ["2024-01-01", 0.01, 0.02],
        ["2024-01-02", 0.02, -0.01],
        ["2024-01-03", -0.005, 0.015],
    ]
    result = venv_run_quant({"helper": "portfolio_tearsheet", "params": {"column": "MSFT"}}, data=grid)
    assert result["status"] == "ok"
    assert "metrics" in result


def test_portfolio_tearsheet_invalid_data():
    pytest.importorskip("quantstats")
    grid = [
        ["HeaderA", "HeaderB"],
        ["text1", "text2"],
        ["text3", "text4"],
    ]
    result = venv_run_quant({"helper": "portfolio_tearsheet", "params": {}}, data=grid)
    assert result["status"] == "error"
    assert result["code"] == "INVALID_DATA"
    assert result["helper"] == "portfolio_tearsheet"


def test_technical_analysis_missing_column_sets_helper(monkeypatch):
    pytest.importorskip("pandas")
    import importlib

    real_import = importlib.import_module

    def _fake_import(name, package=None):
        if name == "pandas_ta":
            return object()
        return real_import(name, package)

    monkeypatch.setattr("plugin.scripting.venv.quant.importlib.import_module", _fake_import)
    result = venv_run_quant(
        {"helper": "technical_analysis", "params": {}},
        data=[["Open"], [1.0]],
    )
    assert result["status"] == "error"
    assert result["code"] == "MISSING_COLUMN"
    assert result["helper"] == "technical_analysis"
    from plugin.calc.quant_egress import is_quant_result

    assert is_quant_result(result) is True


def test_efficient_frontier_treats_grid_as_returns():
    pytest.importorskip("pypfopt")
    grid = [
        ["AAPL", "MSFT", "GOOG"],
        [0.01, 0.008, 0.012],
        [0.02, -0.01, 0.025],
        [-0.005, 0.015, 0.01],
        [0.012, 0.005, -0.008],
        [0.008, 0.02, 0.015],
        [0.015, -0.005, 0.02],
        [-0.01, 0.012, 0.005],
        [0.02, 0.008, 0.018],
        [0.005, 0.015, -0.002],
        [0.01, 0.01, 0.01],
    ]
    result = venv_run_quant({"helper": "efficient_frontier", "params": {}}, data=grid)
    assert result["status"] == "ok", result
    weights = result["weights"]
    assert abs(sum(weights.values()) - 1.0) < 1e-3
    # Reading the same returns as prices collapses onto MSFT with weight 1.
    assert all(0.05 < float(weight) < 0.8 for weight in weights.values())


def test_is_quant_result_uses_helper_names_not_fetch_prefix():
    from plugin.calc.quant_egress import is_quant_result

    assert is_quant_result({"status": "ok", "helper": "fetch_historical_data"}) is True
    assert is_quant_result({"status": "ok", "helper": "fetch_not_a_helper"}) is False
    assert is_quant_result({"status": "error", "code": "QUANT_ERROR"}) is True
    assert is_quant_result({"status": "error", "code": "OTHER"}) is False
