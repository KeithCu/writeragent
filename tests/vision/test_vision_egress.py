# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for vision HTML egress."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from plugin.framework.errors import ToolExecutionError
from plugin.vision.vision_egress import (
    insert_vision_result,
    is_vision_result,
    vision_html_from_result,
)


def test_is_vision_result_ok():
    assert is_vision_result({"status": "ok", "helper": "extract_text", "html": "<p>x</p>"})


def test_is_vision_result_error():
    assert is_vision_result({"status": "error", "code": "VISION_ERROR", "message": "fail", "helper": "extract_text"})


def test_is_vision_result_rejects_analysis_helper():
    assert not is_vision_result({"status": "ok", "helper": "quick_stats", "metrics": {"rows": 3}})


def test_is_vision_result_rejects_missing_status():
    assert not is_vision_result({"helper": "extract_text"})


def test_vision_html_from_result_success():
    html = vision_html_from_result({"status": "ok", "helper": "extract_text", "html": "<p>line</p>"})
    assert html == "<p>line</p>"


def test_vision_html_from_result_error_raises():
    with pytest.raises(ToolExecutionError) as exc:
        vision_html_from_result(
            {
                "status": "error",
                "code": "PADDLEOCR_UNAVAILABLE",
                "message": "Install paddleocr",
                "helper": "extract_text",
            }
        )
    assert exc.value.code == "PADDLEOCR_UNAVAILABLE"


def test_vision_html_from_result_missing_html_raises():
    with pytest.raises(ToolExecutionError) as exc:
        vision_html_from_result({"status": "ok", "helper": "extract_text"})
    assert exc.value.code == "VISION_ERROR"


@patch("plugin.vision.vision_egress.insert_vision_result_into_writer")
def test_insert_vision_result_writer(mock_writer):
    ctx = MagicMock()
    doc = MagicMock()
    result = {"status": "ok", "helper": "extract_text", "html": "<p>hi</p>"}

    with patch("plugin.doc.document_helpers.is_writer", return_value=True), patch(
        "plugin.doc.document_helpers.is_calc", return_value=False
    ), patch("plugin.vision.vision_egress.resolve_vision_insert_mode", return_value="html"):
        insert_vision_result(ctx, doc, result)

    mock_writer.assert_called_once_with(ctx, doc, result)


@patch("plugin.calc.vision_egress.insert_vision_html_into_calc")
def test_insert_vision_result_calc(mock_calc):
    ctx = MagicMock()
    doc = MagicMock()
    result = {"status": "ok", "helper": "extract_text", "html": "<p>hi</p>"}

    with patch("plugin.doc.document_helpers.is_writer", return_value=False), patch(
        "plugin.doc.document_helpers.is_calc", return_value=True
    ), patch("plugin.vision.vision_egress.resolve_vision_insert_mode", return_value="html"):
        insert_vision_result(ctx, doc, result)

    mock_calc.assert_called_once_with(doc, ctx, "<p>hi</p>")


@patch("plugin.calc.vision_egress.insert_vision_html_into_calc")
@patch("plugin.calc.vision_egress.insert_vision_structure_into_calc", return_value=5)
def test_insert_vision_result_calc_structured(mock_structured, mock_html):
    ctx = MagicMock()
    doc = MagicMock()
    result = {
        "status": "ok",
        "helper": "extract_structure",
        "html": "<table></table>",
        "tables": [{"name": "table_1", "columns": ["A"], "rows": [["1"]]}],
    }

    with patch("plugin.doc.document_helpers.is_writer", return_value=False), patch(
        "plugin.doc.document_helpers.is_calc", return_value=True
    ), patch("plugin.vision.vision_egress.resolve_vision_insert_mode", return_value="structured"):
        insert_vision_result(ctx, doc, result, params={"insert_mode": "structured"})

    mock_structured.assert_called_once_with(doc, ctx, result)
    mock_html.assert_not_called()
