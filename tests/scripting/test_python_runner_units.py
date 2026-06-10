# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for Run Python Script units fast path."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from plugin.scripting.python_runner import execute_and_insert_result
from plugin.scripting.units import get_units_script_templates
from plugin.tests.testing_utils import setup_uno_mocks

setup_uno_mocks()


@patch("plugin.scripting.units.insert_units_result_into_doc")
@patch("plugin.scripting.units.run_trusted_units")
def test_execute_and_insert_units_fast_path(mock_run, mock_insert):
    ctx = MagicMock()
    doc = MagicMock()

    with patch("plugin.scripting.python_runner.is_writer", return_value=True):
        mock_run.return_value = {
            "status": "ok",
            "helper": "convert_quantity",
            "formatted": "36 kilometer / hour",
            "text": "36 kilometer / hour",
            "magnitude": 36.0,
        }
        code = get_units_script_templates()["convert_quantity"]
        outcome = execute_and_insert_result(ctx, doc, code)

    assert outcome["ok"] is True
    assert "convert_quantity" in outcome["status_ok_text"]
    mock_run.assert_called_once()
    mock_insert.assert_called_once()


@patch("plugin.scripting.units.insert_units_result_into_doc")
@patch("plugin.scripting.python_runner.run_code_in_user_venv")
def test_execute_and_insert_detects_units_result_from_venv(mock_venv, mock_insert):
    ctx = MagicMock()
    doc = MagicMock()

    with patch("plugin.scripting.python_runner.is_writer", return_value=True):
        mock_venv.return_value = {
            "status": "ok",
            "result": {
                "status": "ok",
                "helper": "parse_quantity",
                "formatted": "10 meter / second",
                "text": "10 meter / second",
                "magnitude": 10.0,
            },
        }
        outcome = execute_and_insert_result(ctx, doc, "from plugin.scripting.units import run_units\nresult = ...")

    assert outcome["ok"] is True
    assert "parse_quantity" in outcome["status_ok_text"]
    mock_insert.assert_called_once()
