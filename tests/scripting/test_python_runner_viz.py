# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for Run Python Script viz fast path and image egress."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from plugin.scripting.python_runner import execute_and_insert_result
from plugin.scripting.viz_templates import get_viz_script_templates
from plugin.tests.testing_utils import setup_uno_mocks

setup_uno_mocks()

_SAMPLE_IMAGE = {"__wa_payload__": "image", "format": "png", "data": b"abc"}


@patch("plugin.scripting.viz_egress.insert_viz_result_into_doc")
@patch("plugin.scripting.viz_runner.run_trusted_viz")
def test_execute_and_insert_viz_fast_path(mock_run, mock_insert):
    ctx = MagicMock()
    doc = MagicMock()
    doc.getCurrentController.return_value = MagicMock()
    doc.getCurrentController.return_value.getSelection.return_value = None

    with patch("plugin.scripting.python_runner.is_calc", return_value=True):
        mock_run.return_value = {
            "status": "ok",
            "helper": "quick_plot",
            "title": "Quick plot: Sales",
            "image": _SAMPLE_IMAGE,
        }
        code = get_viz_script_templates()["quick_plot"]
        outcome = execute_and_insert_result(ctx, doc, code, data_range="Sheet1.A1:B5")

    assert outcome["ok"] is True
    assert "quick_plot" in outcome["status_ok_text"]
    assert "Plot inserted" in outcome["status_ok_text"]
    mock_run.assert_called_once()
    mock_insert.assert_called_once()


@patch("plugin.scripting.viz_egress.try_insert_plot_result")
@patch("plugin.scripting.python_runner.run_code_in_user_venv")
def test_execute_and_insert_detects_raw_image_from_venv(mock_venv, mock_try_insert):
    ctx = MagicMock()
    doc = MagicMock()

    with patch("plugin.scripting.python_runner.is_calc", return_value=True):
        mock_venv.return_value = {"status": "ok", "result": _SAMPLE_IMAGE}
        mock_try_insert.return_value = True
        outcome = execute_and_insert_result(ctx, doc, "result = plt.plot([1,2,3])")

    assert outcome["ok"] is True
    assert "Plot inserted" in outcome["status_ok_text"]
    mock_try_insert.assert_called_once()


@patch("plugin.scripting.viz_egress.insert_viz_result_into_doc")
@patch("plugin.scripting.python_runner.run_code_in_user_venv")
def test_execute_and_insert_detects_viz_result_from_venv(mock_venv, mock_insert):
    ctx = MagicMock()
    doc = MagicMock()

    with patch("plugin.scripting.python_runner.is_calc", return_value=True):
        mock_venv.return_value = {
            "status": "ok",
            "result": {
                "status": "ok",
                "helper": "plot_data",
                "title": "Scatter",
                "image": _SAMPLE_IMAGE,
            },
        }
        outcome = execute_and_insert_result(ctx, doc, "from plugin.scripting.viz import run_viz\nresult = run_viz(...)")

    assert outcome["ok"] is True
    assert "plot_data" in outcome["status_ok_text"] or "Plot inserted" in outcome["status_ok_text"]
    mock_insert.assert_called_once()
