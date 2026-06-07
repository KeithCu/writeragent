# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for Run Python Script symbolic math fast path."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from plugin.scripting.python_runner import execute_and_insert_result
from plugin.scripting.symbolic_templates import get_math_script_templates
from plugin.tests.testing_utils import setup_uno_mocks

setup_uno_mocks()


@patch("plugin.scripting.symbolic_egress.insert_symbolic_result_into_doc")
@patch("plugin.scripting.symbolic_runner.run_trusted_symbolic")
def test_execute_and_insert_math_fast_path(mock_run, mock_insert):
    ctx = MagicMock()
    doc = MagicMock()

    with patch("plugin.scripting.python_runner.is_writer", return_value=True):
        mock_run.return_value = {
            "status": "ok",
            "helper": "solve_equation",
            "latex": "2,-2",
            "text": "-2, 2",
        }
        code = get_math_script_templates()["solve_equation"]
        outcome = execute_and_insert_result(ctx, doc, code)

    assert outcome["ok"] is True
    assert "solve_equation" in outcome["status_ok_text"]
    mock_run.assert_called_once()
    mock_insert.assert_called_once()


@patch("plugin.scripting.symbolic_egress.insert_symbolic_result_into_doc")
@patch("plugin.scripting.python_runner.run_code_in_user_venv")
def test_execute_and_insert_detects_symbolic_result_from_venv(mock_venv, mock_insert):
    ctx = MagicMock()
    doc = MagicMock()

    with patch("plugin.scripting.python_runner.is_writer", return_value=True):
        mock_venv.return_value = {
            "status": "ok",
            "result": {
                "status": "ok",
                "helper": "symbolic_simplify",
                "latex": "2",
                "text": "2",
            },
        }
        outcome = execute_and_insert_result(ctx, doc, "from plugin.scripting.symbolic import run_symbolic\nresult = ...")

    assert outcome["ok"] is True
    assert "symbolic_simplify" in outcome["status_ok_text"]
    mock_insert.assert_called_once()
