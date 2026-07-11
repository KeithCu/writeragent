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

    mock_writer.assert_called_once_with(ctx, doc, result, params=None)


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


@patch("plugin.writer.edit_review.review_recording_enabled", return_value=False)
@patch("plugin.writer.format.insert_html_at_cursor")
@patch("plugin.vision.vision_egress.prepare_vision_writer_insert")
def test_insert_vision_result_into_writer_uses_prepare_and_insert(mock_prepare, mock_insert, _review):
    from plugin.vision.vision_egress import insert_vision_result_into_writer

    cursor = MagicMock()
    mock_prepare.return_value = cursor
    ctx = MagicMock()
    doc = MagicMock()
    result = {"status": "ok", "helper": "extract_text", "html": "<p>line</p>"}

    insert_vision_result_into_writer(ctx, doc, result)

    mock_prepare.assert_called_once_with(doc, ctx, image_name=None)
    mock_insert.assert_called_once_with(doc, ctx, cursor, "<p>line</p>", apply_styles=False)


@patch("plugin.writer.format.insert_html_at_cursor")
@patch("plugin.vision.vision_egress.prepare_vision_writer_insert")
def test_insert_vision_result_into_writer_without_edit_review(mock_prepare, mock_insert):
    """LibrePy omits edit_review; OCR insert must still apply HTML directly."""
    from plugin.vision.vision_egress import insert_vision_result_into_writer

    cursor = MagicMock()
    mock_prepare.return_value = cursor
    ctx = MagicMock()
    doc = MagicMock()
    result = {"status": "ok", "helper": "extract_text", "html": "<p>line</p>"}

    real_import = __import__

    def import_without_edit_review(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "plugin.writer.edit_review":
            raise ImportError("No module named 'plugin.writer.edit_review'")
        return real_import(name, globals, locals, fromlist, level)

    with patch("builtins.__import__", side_effect=import_without_edit_review):
        insert_vision_result_into_writer(ctx, doc, result)

    mock_prepare.assert_called_once_with(doc, ctx, image_name=None)
    mock_insert.assert_called_once_with(doc, ctx, cursor, "<p>line</p>", apply_styles=False)


def test_prepare_vision_writer_insert_inserts_paragraph_break_and_collapses_view_cursor():
    from plugin.vision.vision_egress import prepare_vision_writer_insert

    anchor_start = MagicMock()
    anchor = MagicMock()
    anchor.getStart.return_value = anchor_start
    text = MagicMock()
    anchor_cursor = MagicMock()
    anchor_cursor.goRight.return_value = True
    text.createTextCursorByRange.side_effect = lambda pos: anchor_cursor if pos is anchor_start else MagicMock()
    anchor.getText.return_value = text

    graphic = MagicMock()
    graphic.getAnchor.return_value = anchor
    graphic.getName.return_value = "Image1"

    view_cursor = MagicMock()
    window = MagicMock()
    frame = MagicMock()
    frame.getContainerWindow.return_value = window
    controller = MagicMock()
    controller.getFrame.return_value = frame
    controller.getViewCursor.return_value = view_cursor
    anchor_cursor.getStart.return_value = "insert-start"

    doc = MagicMock()
    doc.getCurrentController.return_value = controller
    ctx = MagicMock()

    with patch("plugin.doc.visual_helpers.selected_graphic_object", side_effect=[graphic, None]), patch(
        "plugin.doc.visual_helpers.list_graphic_objects", return_value=[("Image1", graphic)]
    ), patch("plugin.doc.visual_helpers.get_graphic_object_by_name", return_value=graphic):
        out = prepare_vision_writer_insert(doc, ctx)

    assert out is anchor_cursor
    anchor_cursor.goRight.assert_any_call(1, False)
    text.insertControlCharacter.assert_called_once_with(anchor_cursor, 0, False)
    window.setFocus.assert_called_once()
    view_cursor.gotoRange.assert_called_with("insert-start", False)
    controller.select.assert_called_with(view_cursor)
