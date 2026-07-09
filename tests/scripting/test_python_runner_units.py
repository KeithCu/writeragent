# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for Run Python Script units venv path (fast path disabled)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from plugin.scripting.python_runner import execute_and_insert_result
from plugin.scripting.units import get_units_script_templates
from plugin.tests.testing_utils import setup_uno_mocks

setup_uno_mocks()


@patch("plugin.scripting.units.insert_units_result_into_doc")
@patch("plugin.scripting.units.run_trusted_units")
@patch("plugin.scripting.python_runner.run_code_in_user_venv")
def test_execute_and_insert_units_skips_fast_path(mock_venv, mock_run, mock_insert):
    ctx = MagicMock()
    doc = MagicMock()

    with patch("plugin.scripting.python_runner.is_writer", return_value=True):
        mock_venv.return_value = {
            "status": "ok",
            "result": {
                "status": "ok",
                "helper": "convert_quantity",
                "formatted": "36 kilometer / hour",
                "text": "36 kilometer / hour",
                "magnitude": 36.0,
            },
        }
        code = get_units_script_templates()["convert_quantity"]
        outcome = execute_and_insert_result(ctx, doc, code)

    assert outcome["ok"] is True
    assert "convert_quantity" in outcome["status_ok_text"]
    mock_run.assert_not_called()
    mock_venv.assert_called_once()
    mock_insert.assert_called_once()


@patch("plugin.scripting.units.insert_units_result_into_doc")
@patch("plugin.scripting.python_runner.run_code_in_user_venv")
def test_execute_and_insert_units_passes_output_style_from_body(mock_venv, mock_insert):
    ctx = MagicMock()
    doc = MagicMock()

    code = (
        "from writeragent.scripting.units import run_units\n"
        'result = run_units({"helper": "convert_quantity", "params": {"value":"10","from_unit":"m/s","to_unit":"km/h","output_style":"detailed"}}, None, {})\n'
    )
    with patch("plugin.scripting.python_runner.is_writer", return_value=True):
        mock_venv.return_value = {
            "status": "ok",
            "result": {
                "status": "ok",
                "helper": "convert_quantity",
                "formatted": "36 km/h",
                "text": "36 km/h",
            },
        }
        outcome = execute_and_insert_result(ctx, doc, code)

    assert outcome["ok"] is True
    mock_insert.assert_called_once()
    assert mock_insert.call_args.kwargs.get("output_style") == "detailed"


@patch("plugin.scripting.units.insert_units_result_into_doc")
@patch("plugin.scripting.python_runner.run_code_in_user_venv")
def test_execute_and_insert_units_uses_body_params_not_header(mock_venv, mock_insert):
    ctx = MagicMock()
    doc = MagicMock()

    code = (
        "from writeragent.scripting.units import run_units\n"
        'result = run_units({"helper": "convert_quantity", "params": {"value":"20","from_unit":"m/s","to_unit":"mm/h"}}, None, {})\n'
    )
    with patch("plugin.scripting.python_runner.is_writer", return_value=True):
        mock_venv.return_value = {
            "status": "ok",
            "result": {
                "status": "ok",
                "helper": "convert_quantity",
                "formatted": "72000000 millimeter / hour",
                "text": "72000000 millimeter / hour",
            },
        }
        outcome = execute_and_insert_result(ctx, doc, code)

    assert outcome["ok"] is True
    mock_venv.assert_called_once_with(ctx, code, data=None)
    assert "millimeter" in mock_insert.call_args.args[2].get("formatted", "")


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
