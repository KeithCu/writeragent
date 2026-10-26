# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for Run Python Script text analytics venv path (fast path disabled)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from tests.harness.strip_bundle import requires_asserts

from plugin.scripting.python_runner import execute_and_insert_result
from plugin.scripting.text_analytics import get_text_analytics_script_templates


@patch("plugin.scripting.text_analytics.insert_text_analytics_result_into_doc")
@patch("plugin.scripting.python_runner.run_code_in_user_venv")
def test_execute_and_insert_text_skips_fast_path(mock_venv, mock_insert):
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
    mock_venv.assert_called_once()
    assert mock_venv.call_args.args[1] == code
    assert mock_venv.call_args.kwargs["bindings"] == {
        "text": "Sample document text.",
        "document_context": {},
    }
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
    assert mock_venv.call_args.args[1] == code
    assert mock_venv.call_args.kwargs["bindings"] == {
        "text": "section text",
        "document_context": {"lang": "en"},
    }
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


@patch("plugin.scripting.text_analytics.insert_text_analytics_result_into_doc")
@patch("plugin.scripting.python_runner.run_code_in_user_venv")
def test_topics_and_sentiment_bind_section_list(mock_venv, mock_insert):
    """Section lists must be JSON arrays, not the repr of the list."""
    ctx = MagicMock()
    doc = MagicMock()
    sections = ["Section one about revenue.", "Section two about budgets."]
    mock_venv.return_value = {
        "status": "ok",
        "result": {"status": "ok", "result": {"topics": [], "sentiment": {}}},
    }
    for helper in ("topics", "sentiment"):
        mock_venv.reset_mock()
        code = get_text_analytics_script_templates()[helper]
        with (
            patch("plugin.scripting.python_runner.is_writer", return_value=True),
            patch(
                "plugin.scripting.text_analytics.resolve_text_analytics_document_inputs",
                return_value=(sections, {"lang": "en"}),
            ),
        ):
            outcome = execute_and_insert_result(ctx, doc, code)
        assert outcome["ok"] is True
        assert mock_venv.call_args.args[1] == code
        assert mock_venv.call_args.kwargs["bindings"] == {
            "text": sections,
            "document_context": {"lang": "en"},
        }


@requires_asserts
def test_text_analytics_input_type_assertions():
    from plugin.scripting.python_runner import _prepare_rps_execution

    ctx = MagicMock()
    doc = MagicMock()
    code = get_text_analytics_script_templates()["full"]

    with patch("plugin.scripting.python_runner.is_writer", return_value=True):
        with patch("plugin.scripting.text_analytics.resolve_text_analytics_document_inputs", return_value=(123, {})):
            with pytest.raises(AssertionError):
                _prepare_rps_execution(ctx, doc, code)

        with patch("plugin.scripting.text_analytics.resolve_text_analytics_document_inputs", return_value=("valid text", "not a dict")):
            with pytest.raises(AssertionError):
                _prepare_rps_execution(ctx, doc, code)



