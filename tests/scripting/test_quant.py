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
from plugin.tests.testing_utils import setup_uno_mocks

setup_uno_mocks()


def test_quant_template_is_executable():
    code = get_quant_template("fetch_historical_data")
    assert code is not None
    assert "from writeragent.scripting.quant import fetch_historical_data" in code
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
    assert kwargs["session_id"] == "writeragent:quant"
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
    grid = [
        ["HeaderA", "HeaderB"],
        ["text1", "text2"],
        ["text3", "text4"],
    ]
    result = venv_run_quant({"helper": "portfolio_tearsheet", "params": {}}, data=grid)
    assert result["status"] == "error"
    assert result["code"] == "INVALID_DATA"
    assert result["helper"] == "portfolio_tearsheet"
