# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for Run Python Script forecast fast path."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from plugin.scripting.forecast import get_forecast_template
from plugin.scripting.python_runner import execute_and_insert_result
from plugin.tests.testing_utils import setup_uno_mocks

setup_uno_mocks()


@patch("plugin.scripting.forecast.insert_forecast_result_into_calc")
@patch("plugin.scripting.forecast.run_trusted_forecast")
def test_execute_and_insert_forecast_fast_path(mock_run, mock_insert):
    ctx = MagicMock()
    doc = MagicMock()

    with patch("plugin.scripting.python_runner.is_calc", return_value=True):
        mock_run.return_value = {
            "status": "ok",
            "helper": "forecast_time_series",
            "metrics": {"periods": 6},
            "tables": [{"name": "forecast", "columns": ["date", "forecast"], "rows": [[1, 2]]}],
        }
        mock_insert.return_value = 5
        code = get_forecast_template("forecast_time_series")
        assert code is not None
        outcome = execute_and_insert_result(ctx, doc, code, data_range="Sheet1.A1:B37")

    assert outcome["ok"] is True
    assert "forecast_time_series" in outcome["status_ok_text"]
    mock_run.assert_called_once()
    mock_insert.assert_called_once()


@patch("plugin.scripting.forecast.insert_forecast_result_into_calc")
@patch("plugin.scripting.python_runner.run_code_in_user_venv")
def test_execute_and_insert_detects_forecast_result_from_venv(mock_venv, mock_insert):
    ctx = MagicMock()
    doc = MagicMock()

    with patch("plugin.scripting.python_runner.is_calc", return_value=True):
        mock_venv.return_value = {
            "status": "ok",
            "result": {
                "status": "ok",
                "helper": "decompose_time_series",
                "metrics": {"period": 12},
                "tables": [{"name": "decomposition", "columns": ["date"], "rows": [[1]]}],
            },
        }
        mock_insert.return_value = 3
        outcome = execute_and_insert_result(ctx, doc, "print('x')")

    assert outcome["ok"] is True
    mock_insert.assert_called_once()
