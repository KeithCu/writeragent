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
    parse_quant_script_header,
)
from plugin.scripting.venv.quant import run_quant as venv_run_quant
from plugin.framework.errors import ToolExecutionError
from plugin.tests.testing_utils import setup_uno_mocks

setup_uno_mocks()


def test_quant_template_header_roundtrip():
    code = get_quant_template("fetch_historical_data")
    assert code is not None
    assert QUANT_HEADER_PREFIX in code
    meta = parse_quant_script_header(code)
    assert meta is not None
    assert meta.helper == "fetch_historical_data"
    assert "tickers" in meta.params


def test_run_quant_missing_package(monkeypatch):
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "yfinance":
            raise ImportError("no yfinance")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    result = venv_run_quant("fetch_historical_data", {"tickers": ["AAPL"]}, None, {})
    assert result["status"] == "error"
    assert result["code"] == "MISSING_PACKAGE"


def test_run_quant_invalid_params(monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, "yfinance", object())
    result = venv_run_quant("fetch_historical_data", {}, None, {})
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
        result = run_quant(ctx, "fetch_historical_data", {"tickers": ["AAPL"]})

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
            run_quant(ctx, "fetch_historical_data", {"tickers": ["AAPL"]})


def test_helper_names_cover_templates():
    for helper in HELPER_NAMES:
        assert get_quant_template(helper) is not None
