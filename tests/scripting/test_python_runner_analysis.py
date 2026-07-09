# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for Run Python Script analysis fast path."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from plugin.scripting.analysis import get_analysis_script_templates
from plugin.scripting.python_runner import execute_and_insert_result
from plugin.tests.testing_utils import setup_uno_mocks

setup_uno_mocks()


@patch("plugin.calc.analysis_egress.insert_analysis_result_into_calc")
@patch("plugin.calc.analysis_runner.run_trusted_analysis")
def test_execute_and_insert_analysis_fast_path(mock_run, mock_insert):
    ctx = MagicMock()
    doc = MagicMock()
    doc.getCurrentController.return_value = MagicMock()
    doc.getCurrentController.return_value.getSelection.return_value = None

    with (
        patch("plugin.scripting.domain_registry.is_calc", return_value=True),
        patch("plugin.scripting.python_runner.is_calc", return_value=True),
    ):
        mock_run.return_value = {"status": "ok", "helper": "describe_data", "metrics": {"row_count": 1}}
        mock_insert.return_value = 5
        code = get_analysis_script_templates()["describe_data"]
        outcome = execute_and_insert_result(ctx, doc, code, data_range="Sheet1.A1:B2")

    assert outcome["ok"] is True
    assert "describe_data" in outcome["status_ok_text"]
    mock_run.assert_called_once()
    mock_insert.assert_called_once()


@patch("plugin.scripting.python_runner.run_code_in_user_venv")
@patch("plugin.calc.analysis_egress.insert_analysis_result_into_calc")
def test_execute_and_insert_detects_analysis_result_from_venv(mock_insert, mock_venv):
    ctx = MagicMock()
    doc = MagicMock()

    with (
        patch("plugin.scripting.domain_registry.is_calc", return_value=True),
        patch("plugin.scripting.python_runner.is_calc", return_value=True),
    ):
        mock_venv.return_value = {
            "status": "ok",
            "result": {"status": "ok", "helper": "quick_stats", "metrics": {"rows": 3}},
        }
        mock_insert.return_value = 4
        outcome = execute_and_insert_result(ctx, doc, "result = {'status': 'ok', 'helper': 'quick_stats', 'metrics': {}}")

    assert outcome["ok"] is True
    assert "quick_stats" in outcome["status_ok_text"]
    mock_insert.assert_called_once()
