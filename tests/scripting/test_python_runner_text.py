# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for Run Python Script text analytics venv path (fast path disabled)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from plugin.scripting.python_runner import execute_and_insert_result
from plugin.scripting.text_analytics import get_text_analytics_script_templates
from plugin.tests.testing_utils import setup_uno_mocks

setup_uno_mocks()


@patch("plugin.scripting.text_analytics.insert_text_analytics_result_into_doc")
@patch("plugin.scripting.text_analytics.run_trusted_text_analytics")
@patch("plugin.scripting.python_runner.run_code_in_user_venv")
def test_execute_and_insert_text_skips_fast_path(mock_venv, mock_run, mock_insert):
    ctx = MagicMock()
    doc = MagicMock()

    with (
        patch("plugin.scripting.python_runner.is_writer", return_value=True),
        patch(
            "plugin.scripting.text_analytics.resolve_text_analytics_document_inputs",
            return_value=("Sample document text.", {}),
        ),
    ):
        mock_venv.return_value = {
            "status": "ok",
            "result": {
                "status": "ok",
                "result": {"readability": {"flesch_reading_ease": 60}, "entities": []},
            },
        }
        code = get_text_analytics_script_templates()["full"]
        outcome = execute_and_insert_result(ctx, doc, code)

    assert outcome["ok"] is True
    assert "Text analytics" in outcome["status_ok_text"]
    mock_run.assert_not_called()
    mock_venv.assert_called_once()
    injected = mock_venv.call_args.args[1]
    assert "text = " in injected
    assert "document_context" in injected
    mock_insert.assert_called_once()


@patch("plugin.scripting.text_analytics.insert_text_analytics_result_into_doc")
@patch("plugin.scripting.python_runner.run_code_in_user_venv")
def test_execute_and_insert_text_uses_body_helper(mock_venv, mock_insert):
    ctx = MagicMock()
    doc = MagicMock()

    code = (
        "from writeragent.scripting.text_analytics import run_text_analytics\n"
        'result = run_text_analytics({"helper": "entities", "params": {}}, text, document_context)\n'
    )
    with (
        patch("plugin.scripting.python_runner.is_writer", return_value=True),
        patch(
            "plugin.scripting.text_analytics.resolve_text_analytics_document_inputs",
            return_value=("section text", {"lang": "en"}),
        ),
    ):
        mock_venv.return_value = {
            "status": "ok",
            "result": {
                "status": "ok",
                "result": {"entities": [{"text": "ACME", "label": "ORG"}]},
            },
        }
        outcome = execute_and_insert_result(ctx, doc, code)

    assert outcome["ok"] is True
    injected = mock_venv.call_args.args[1]
    assert '"section text"' in injected or "section text" in injected
    assert mock_venv.call_args.args[1].startswith("# WriterAgent:")
    mock_insert.assert_called_once()


@patch("plugin.scripting.text_analytics.insert_text_analytics_result_into_doc")
@patch("plugin.scripting.python_runner.run_code_in_user_venv")
def test_execute_and_insert_detects_text_result_from_venv(mock_venv, mock_insert):
    ctx = MagicMock()
    doc = MagicMock()

    with patch("plugin.scripting.python_runner.is_writer", return_value=True):
        mock_venv.return_value = {
            "status": "ok",
            "result": {
                "status": "ok",
                "result": {"key_phrases": [{"text": "budget review", "lemma": "budget review"}]},
            },
        }
        outcome = execute_and_insert_result(
            ctx,
            doc,
            "from writeragent.scripting.text_analytics import run_text_analytics\nresult = ...",
        )

    assert outcome["ok"] is True
    assert "Text analytics" in outcome["status_ok_text"]
    mock_insert.assert_called_once()
