# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for vision HTML egress."""

from __future__ import annotations

from unittest.mock import MagicMock, call, patch

import pytest

from plugin.framework.errors import DocumentDisposedError, ToolExecutionError
from plugin.vision.vision_egress import (
    insert_vision_result,
    is_vision_result,
    source_image_note,
    vision_html_from_result,
)


@pytest.mark.parametrize(
    ("outcomes", "expected"),
    [
        ([], ""),
        (["inserted", "empty"], ""),
        (["image_deleted"], "Source images deleted: 1."),
        (["image_deleted", "image_deleted", "empty"], "Source images deleted: 2."),
        (["image_kept"], "Image kept because LibreOffice cannot record deleting it as a tracked change."),
        (
            ["image_deleted", "image_kept"],
            "Source images deleted: 1. Image kept because LibreOffice cannot record deleting it as a tracked change.",
        ),
    ],
)
def test_source_image_note(outcomes, expected):
    assert source_image_note(outcomes) == expected


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

    with patch("plugin.framework.doc_type.is_writer", return_value=True), patch(
        "plugin.framework.doc_type.is_calc", return_value=False
    ), patch("plugin.vision.vision_egress.resolve_vision_insert_mode", return_value="html"):
        insert_vision_result(ctx, doc, result)

    mock_writer.assert_called_once_with(ctx, doc, result, params=None)


@patch("plugin.calc.vision_egress.insert_vision_html_into_calc")
def test_insert_vision_result_calc(mock_calc):
    ctx = MagicMock()
    doc = MagicMock()
    result = {"status": "ok", "helper": "extract_text", "html": "<p>hi</p>"}

    with patch("plugin.framework.doc_type.is_writer", return_value=False), patch(
        "plugin.framework.doc_type.is_calc", return_value=True
    ), patch("plugin.vision.vision_egress.resolve_vision_insert_mode", return_value="html"):
        insert_vision_result(ctx, doc, result)

    mock_calc.assert_called_once_with(doc, ctx, "<p>hi</p>", image_name=None, graphic=None)


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

    with patch("plugin.framework.doc_type.is_writer", return_value=False), patch(
        "plugin.framework.doc_type.is_calc", return_value=True
    ), patch("plugin.vision.vision_egress.resolve_vision_insert_mode", return_value="structured"):
        insert_vision_result(ctx, doc, result, params={"insert_mode": "structured"})

    mock_structured.assert_called_once_with(doc, result, image_name=None, graphic=None)
    mock_html.assert_not_called()


@patch("plugin.writer.edit_review.review_recording_enabled", return_value=False)
@patch("plugin.writer.html_import.insert_html_at_cursor")
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


@patch("plugin.writer.html_import.insert_html_at_cursor")
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

    anchor = MagicMock()
    text = MagicMock()
    anchor_cursor = MagicMock()
    text.createTextCursorByRange.return_value = anchor_cursor
    anchor.getText.return_value = text

    graphic = MagicMock()
    graphic.getAnchor.return_value = anchor
    graphic.getName.return_value = "Image1"

    view_cursor = MagicMock()
    window = MagicMock()
    frame = MagicMock()
    frame.getContainerWindow.return_value = window
    dispatcher = MagicMock()
    smgr = MagicMock()
    smgr.createInstanceWithContext.return_value = dispatcher
    controller = MagicMock()
    controller.getFrame.return_value = frame
    controller.getViewCursor.return_value = view_cursor
    anchor_cursor.getStart.return_value = "insert-start"

    doc = MagicMock()
    doc.getCurrentController.return_value = controller
    ctx = MagicMock()
    ctx.ServiceManager = smgr

    with patch("plugin.doc.visual_helpers.selected_graphic_object", side_effect=[graphic, None]), patch(
        "plugin.doc.visual_helpers.list_graphic_objects", return_value=[("Image1", graphic)]
    ), patch("plugin.doc.visual_helpers.get_graphic_object_by_name", return_value=graphic):
        out = prepare_vision_writer_insert(doc, ctx)

    assert out is anchor_cursor
    dispatcher.executeDispatch.assert_called()
    text.createTextCursorByRange.assert_called_with(anchor)
    anchor_cursor.collapseToEnd.assert_called_once()
    text.insertControlCharacter.assert_called_once_with(anchor_cursor, 0, False)
    window.setFocus.assert_called_once()
    view_cursor.gotoRange.assert_called_with("insert-start", False)
    controller.select.assert_called_with(view_cursor)


def test_prepare_vision_writer_insert_prefers_image_name_over_selection():
    from plugin.vision.vision_egress import prepare_vision_writer_insert

    named = MagicMock()
    named.getName.return_value = "NamedImg"
    anchor = MagicMock()
    text = MagicMock()
    cursor = MagicMock()
    cursor.getStart.return_value = "pos"
    text.createTextCursorByRange.return_value = cursor
    anchor.getText.return_value = text
    named.getAnchor.return_value = anchor

    selected = MagicMock()
    selected.getName.return_value = "SelectedImg"

    view_cursor = MagicMock()
    frame = MagicMock()
    frame.getContainerWindow.return_value = MagicMock()
    dispatcher = MagicMock()
    smgr = MagicMock()
    smgr.createInstanceWithContext.return_value = dispatcher
    controller = MagicMock()
    controller.getFrame.return_value = frame
    controller.getViewCursor.return_value = view_cursor
    doc = MagicMock()
    doc.getCurrentController.return_value = controller
    ctx = MagicMock()
    ctx.ServiceManager = smgr

    with patch("plugin.doc.visual_helpers.selected_graphic_object", return_value=None), patch(
        "plugin.doc.visual_helpers.list_graphic_objects", return_value=[("NamedImg", named), ("SelectedImg", selected)]
    ), patch("plugin.doc.visual_helpers.get_graphic_object_by_name", return_value=named) as mock_get:
        out = prepare_vision_writer_insert(doc, ctx, image_name="NamedImg")

    assert out is cursor
    mock_get.assert_called_with(doc, "NamedImg")
    named.getAnchor.assert_called()
    selected.getAnchor.assert_not_called()
    cursor.collapseToEnd.assert_called_once()
    dispatcher.executeDispatch.assert_called()


@patch("plugin.calc.vision_egress.insert_vision_html_into_calc")
def test_insert_vision_result_calc_passes_image_name(mock_calc):
    ctx = MagicMock()
    doc = MagicMock()
    result = {"status": "ok", "helper": "extract_text", "html": "<p>hi</p>"}
    with patch("plugin.framework.doc_type.is_writer", return_value=False), patch(
        "plugin.framework.doc_type.is_calc", return_value=True
    ), patch("plugin.vision.vision_egress.resolve_vision_insert_mode", return_value="html"):
        insert_vision_result(ctx, doc, result, params={"image_name": "Photo1"})
    mock_calc.assert_called_once_with(doc, ctx, "<p>hi</p>", image_name="Photo1", graphic=None)


@patch("plugin.calc.vision_egress.insert_vision_html_into_calc")
@patch(
    "plugin.calc.vision_egress.insert_vision_structure_into_calc",
    side_effect=ToolExecutionError("No structured tables or text blocks to insert.", code="VISION_ERROR"),
)
def test_calc_structured_fallback_uses_html_docling(mock_structured, mock_html):
    ctx = MagicMock()
    doc = MagicMock()
    result = {
        "status": "ok",
        "helper": "extract_structure",
        "html": "<table><tr><td>layout</td></tr></table>",
        "html_docling": "<p>original docling</p>",
        "tables": [],
    }
    with patch("plugin.framework.doc_type.is_writer", return_value=False), patch(
        "plugin.framework.doc_type.is_calc", return_value=True
    ), patch("plugin.vision.vision_egress.resolve_vision_insert_mode", return_value="structured"):
        insert_vision_result(ctx, doc, result, params={"insert_mode": "structured", "image_name": "Photo1"})
    mock_structured.assert_called_once()
    mock_html.assert_called_once_with(doc, ctx, "<p>original docling</p>", image_name="Photo1", graphic=None)



def test_prepare_vision_writer_insert_disposed_anchor_raises_document_disposed():
    from plugin.vision.vision_egress import prepare_vision_writer_insert

    class DisposedException(Exception):
        pass

    graphic = MagicMock()
    graphic.getName.return_value = "Image1"
    graphic.getAnchor.side_effect = DisposedException("gone")
    controller = MagicMock()
    controller.getFrame.return_value = None
    doc = MagicMock()
    doc.getCurrentController.return_value = controller
    with patch("plugin.doc.visual_helpers.selected_graphic_object", return_value=graphic), patch(
        "plugin.doc.visual_helpers.list_graphic_objects", return_value=[("Image1", graphic)]
    ):
        with pytest.raises(DocumentDisposedError):
            prepare_vision_writer_insert(doc, MagicMock())


class _TestRuntimeException(Exception):
    # Name must contain "RuntimeException" because _looks_like_teardown matches on type name.
    pass


@pytest.mark.parametrize("exc_factory", [ValueError, _TestRuntimeException])
def test_prepare_vision_writer_insert_non_disposal_error_stays_vision_error(exc_factory):
    from plugin.vision.vision_egress import prepare_vision_writer_insert

    graphic = MagicMock()
    graphic.getName.return_value = "Image1"
    graphic.getAnchor.side_effect = exc_factory("no anchor")
    controller = MagicMock()
    controller.getFrame.return_value = None
    doc = MagicMock()
    doc.getCurrentController.return_value = controller
    with patch("plugin.doc.visual_helpers.selected_graphic_object", return_value=graphic), patch(
        "plugin.doc.visual_helpers.list_graphic_objects", return_value=[("Image1", graphic)]
    ):
        with pytest.raises(ToolExecutionError) as exc:
            prepare_vision_writer_insert(doc, MagicMock())
    assert exc.value.code == "VISION_ERROR"


def _prepare_ctx():
    """Minimal Writer doc/controller/ctx for prepare_vision_writer_insert tests."""
    anchor = MagicMock()
    text = MagicMock()
    cursor = MagicMock()
    cursor.getStart.return_value = "insert-start"
    cursor.isCollapsed.return_value = False
    text.createTextCursorByRange.return_value = cursor
    anchor.getText.return_value = text

    graphic = MagicMock()
    graphic.getAnchor.return_value = anchor
    graphic.getName.return_value = "Image1"

    view_cursor = MagicMock()
    frame = MagicMock()
    frame.getContainerWindow.return_value = MagicMock()
    dispatcher = MagicMock()
    smgr = MagicMock()
    smgr.createInstanceWithContext.return_value = dispatcher
    controller = MagicMock()
    controller.getFrame.return_value = frame
    controller.getViewCursor.return_value = view_cursor
    doc = MagicMock()
    doc.getCurrentController.return_value = controller
    ctx = MagicMock()
    ctx.ServiceManager = smgr
    return doc, ctx, graphic, cursor, text


def test_prepare_vision_writer_insert_reports_removed_named_graphic_without_scanning():
    from plugin.vision.vision_egress import prepare_vision_writer_insert

    doc, ctx, graphic, _cursor, _text = _prepare_ctx()
    doc.getGraphicObjects.return_value.hasByName.return_value = False

    # Resolve succeeds (first lookup), then the graphic is gone (presence check).
    with patch("plugin.doc.visual_helpers.selected_graphic_object", return_value=None), patch(
        "plugin.doc.visual_helpers.get_graphic_object_by_name", side_effect=[graphic, None]
    ) as mock_get:
        with pytest.raises(ToolExecutionError) as exc:
            prepare_vision_writer_insert(doc, ctx, image_name="Image1")

    assert exc.value.code == "VISION_ERROR"
    assert "removed while preparing" in str(exc.value)
    assert mock_get.call_count == 2


def test_prepare_vision_writer_insert_accepts_draw_page_only_graphic():
    """As-char / SdrGrafObj images are not in getGraphicObjects(); hasByName is a false negative."""
    from plugin.vision.vision_egress import prepare_vision_writer_insert

    doc, ctx, graphic, cursor, text = _prepare_ctx()
    doc.getGraphicObjects.return_value.hasByName.return_value = False

    with patch("plugin.doc.visual_helpers.selected_graphic_object", return_value=None), patch(
        "plugin.doc.visual_helpers.get_graphic_object_by_name", return_value=graphic
    ):
        out = prepare_vision_writer_insert(doc, ctx, image_name="Image1")

    assert out is cursor
    text.insertControlCharacter.assert_called_once_with(cursor, 0, False)


def _as_char_graphic(graphic):
    """AnchorType AS_CHARACTER (IDL index 1) for the step-over probe."""
    graphic.getPropertyValue.side_effect = lambda name: 1 if name == "AnchorType" else MagicMock()
    return graphic


def _probe_range(*, portion_type: str, content: object):
    probe = MagicMock()
    probe.goRight.return_value = True
    # Frame / graphic characters have no text; ordinary text must be non-empty
    # so the production gate does not treat a MagicMock getString as a hit.
    probe.getString.return_value = "" if portion_type == "Frame" else "T"
    probe.getPropertyValue.side_effect = lambda name: {
        "TextPortionType": portion_type,
        "TextContent": content,
    }.get(name, MagicMock())
    probe.createContentEnumeration.side_effect = Exception("no enum")
    return probe


def test_prepare_vision_writer_insert_steps_over_collapsed_as_char_anchor():
    """A collapsed as-char anchor that sits on the graphic must step over it."""
    from plugin.vision.vision_egress import prepare_vision_writer_insert

    doc, ctx, graphic, cursor, text = _prepare_ctx()
    cursor.isCollapsed.return_value = True
    cursor.getText.return_value = text
    doc.getGraphicObjects.return_value.hasByName.return_value = True
    _as_char_graphic(graphic)
    probe = _probe_range(portion_type="Frame", content=graphic)
    text.createTextCursorByRange.side_effect = [cursor, probe]

    with patch("plugin.doc.visual_helpers.selected_graphic_object", return_value=None), patch(
        "plugin.doc.visual_helpers.get_graphic_object_by_name", return_value=graphic
    ):
        prepare_vision_writer_insert(doc, ctx, image_name="Image1")

    # First goRight steps over the graphic; the second is the post-break move.
    assert cursor.goRight.call_args_list[0] == call(1, False)
    assert cursor.goRight.call_count == 2
    cursor.collapseToEnd.assert_not_called()
    text.insertControlCharacter.assert_called_once_with(cursor, 0, False)


def test_prepare_vision_writer_insert_does_not_step_over_following_text():
    """A collapsed as-char anchor already after the graphic must not skip the next character."""
    from plugin.vision.vision_egress import prepare_vision_writer_insert

    doc, ctx, graphic, cursor, text = _prepare_ctx()
    cursor.isCollapsed.return_value = True
    cursor.getText.return_value = text
    doc.getGraphicObjects.return_value.hasByName.return_value = True
    _as_char_graphic(graphic)
    # goRight(1, True) would select ordinary text, not the graphic.
    probe = _probe_range(portion_type="Text", content=None)
    text.createTextCursorByRange.side_effect = [cursor, probe]

    with patch("plugin.doc.visual_helpers.selected_graphic_object", return_value=None), patch(
        "plugin.doc.visual_helpers.get_graphic_object_by_name", return_value=graphic
    ):
        prepare_vision_writer_insert(doc, ctx, image_name="Image1")

    cursor.collapseToEnd.assert_not_called()
    # Only the post-paragraph-break goRight.
    assert cursor.goRight.call_args_list == [call(1, False)]
    text.insertControlCharacter.assert_called_once_with(cursor, 0, False)


@patch("plugin.writer.edit_review.review_recording_enabled", return_value=False)
@patch("plugin.writer.html_import.insert_html_at_cursor")
@patch("plugin.vision.vision_egress.prepare_vision_writer_insert")
def test_insert_vision_result_into_writer_detects_removed_image_without_scanning(mock_prepare, _insert, _review):
    from plugin.vision.vision_egress import insert_vision_result_into_writer

    cursor = MagicMock()
    mock_prepare.return_value = cursor
    ctx = MagicMock()
    doc = MagicMock()
    doc.getGraphicObjects.return_value.hasByName.return_value = False
    result = {"status": "ok", "helper": "extract_text", "html": "<p>line</p>"}

    with patch("plugin.doc.visual_helpers.list_graphic_objects") as mock_list, patch(
        "plugin.doc.visual_helpers.get_graphic_object_by_name", return_value=None
    ):
        with pytest.raises(ToolExecutionError) as exc:
            insert_vision_result_into_writer(ctx, doc, result, params={"image_name": "Photo1"})

    assert exc.value.code == "VISION_ERROR"
    assert "removed during OCR insert" in str(exc.value)
    mock_list.assert_not_called()


def test_prepare_vision_writer_insert_named_missing_graphic_is_image_not_found():
    from plugin.vision.vision_egress import prepare_vision_writer_insert

    doc = MagicMock()
    ctx = MagicMock()

    with patch("plugin.doc.visual_helpers.get_graphic_object_by_name", return_value=None):
        with pytest.raises(ToolExecutionError) as exc:
            prepare_vision_writer_insert(doc, ctx, image_name="Gone")

    assert exc.value.code == "IMAGE_NOT_FOUND"
    assert exc.value.details == {"image_name": "Gone"}


@patch("plugin.writer.edit_review.review_recording_enabled", return_value=False)
@patch("plugin.writer.html_import.insert_html_at_cursor")
@patch("plugin.vision.vision_egress.prepare_vision_writer_insert")
def test_writer_delete_image_after_insert_on_removes_graphic(mock_prepare, mock_insert, _review):
    from plugin.vision.vision_egress import insert_vision_result_into_writer

    mock_prepare.return_value = MagicMock()
    graphic = MagicMock()
    doc = MagicMock()
    doc.getGraphicObjects.return_value.hasByName.return_value = True
    result = {"status": "ok", "helper": "extract_structure", "html": "<p>ocr</p>"}

    ctx = MagicMock()
    with (
        patch("plugin.doc.visual_helpers.get_graphic_object_by_name", return_value=graphic) as mock_get,
        patch("plugin.doc.visual_helpers.delete_graphic") as mock_delete,
    ):
        outcome = insert_vision_result_into_writer(
            ctx, doc, result, params={"image_name": "Img1", "delete_image_after_insert": True}
        )

    assert outcome == "image_deleted"

    mock_prepare.assert_called_once_with(doc, mock_prepare.call_args.args[1], image_name="Img1")
    mock_insert.assert_called_once()
    mock_get.assert_called_once_with(doc, "Img1")
    mock_delete.assert_called_once_with(ctx, doc, graphic)


@patch("plugin.writer.edit_review.review_recording_enabled", return_value=True)
@patch("plugin.writer.edit_review.EditReviewSession")
@patch("plugin.writer.html_import.insert_html_at_cursor")
@patch("plugin.vision.vision_egress.prepare_vision_writer_insert")
def test_writer_delete_image_after_insert_deletes_in_review_mode(mock_prepare, mock_insert, mock_session, _review):
    from plugin.vision.vision_egress import insert_vision_result_into_writer

    mock_prepare.return_value = MagicMock()
    mock_session.return_value.__enter__.return_value = mock_session.return_value
    mock_session.return_value.record_mutation.side_effect = lambda fn: fn()
    graphic = MagicMock()
    ctx = MagicMock()
    doc = MagicMock()
    doc.getGraphicObjects.return_value.hasByName.return_value = True
    result = {"status": "ok", "helper": "extract_structure", "html": "<p>ocr</p>"}

    with (
        patch("plugin.doc.visual_helpers.get_graphic_object_by_name", return_value=graphic),
        patch("plugin.doc.visual_helpers.delete_graphic", return_value=True) as mock_delete,
    ):
        outcome = insert_vision_result_into_writer(
            ctx, doc, result, params={"image_name": "Img1", "delete_image_after_insert": True}
        )

    assert outcome == "image_deleted"
    mock_insert.assert_called_once()
    mock_delete.assert_called_once_with(ctx, doc, graphic)


@patch("plugin.writer.edit_review.review_recording_enabled", return_value=False)
@patch("plugin.writer.html_import.insert_html_at_cursor")
@patch("plugin.vision.vision_egress.prepare_vision_writer_insert")
def test_writer_delete_image_not_trackable_returns_image_kept_without_rollback(mock_prepare, mock_insert, _review):
    from plugin.vision.vision_egress import insert_vision_result_into_writer

    mock_prepare.return_value = MagicMock()
    graphic = MagicMock()
    doc = MagicMock()
    doc.getGraphicObjects.return_value.hasByName.return_value = True
    um = MagicMock()
    um.isLocked.return_value = False
    um.getAllUndoActionTitles.side_effect = [(), ("WriterAgent: Insert OCR",)]
    doc.getUndoManager.return_value = um
    result = {"status": "ok", "helper": "extract_structure", "html": "<p>ocr</p>"}

    with (
        patch("plugin.doc.visual_helpers.get_graphic_object_by_name", return_value=graphic),
        patch(
            "plugin.doc.visual_helpers.delete_graphic",
            side_effect=ToolExecutionError("cannot record", code="IMAGE_DELETE_NOT_TRACKABLE"),
        ),
    ):
        outcome = insert_vision_result_into_writer(
            MagicMock(), doc, result, params={"image_name": "Img1", "delete_image_after_insert": True}
        )

    assert outcome == "image_kept"
    mock_insert.assert_called_once()
    um.undo.assert_not_called()


@patch("plugin.writer.edit_review.review_recording_enabled", return_value=True)
@patch("plugin.writer.edit_review.EditReviewSession")
@patch("plugin.writer.html_import.insert_html_at_cursor")
@patch("plugin.vision.vision_egress.prepare_vision_writer_insert")
def test_writer_review_mode_without_delete_setting_returns_inserted(mock_prepare, mock_insert, mock_session, _review):
    from plugin.vision.vision_egress import insert_vision_result_into_writer

    mock_prepare.return_value = MagicMock()
    mock_session.return_value.__enter__.return_value = mock_session.return_value
    mock_session.return_value.record_mutation.side_effect = lambda fn: fn()
    doc = MagicMock()
    doc.getGraphicObjects.return_value.hasByName.return_value = True
    result = {"status": "ok", "helper": "extract_structure", "html": "<p>ocr</p>"}

    with patch("plugin.doc.visual_helpers.delete_graphic") as mock_delete:
        outcome = insert_vision_result_into_writer(
            MagicMock(), doc, result, params={"image_name": "Img1", "delete_image_after_insert": False}
        )

    assert outcome == "inserted"
    mock_insert.assert_called_once()
    mock_delete.assert_not_called()


@patch("plugin.writer.edit_review.review_recording_enabled", return_value=False)
@patch("plugin.writer.html_import.insert_html_at_cursor")
@patch("plugin.vision.vision_egress.prepare_vision_writer_insert")
def test_writer_delete_image_failure_rolls_back_insert(mock_prepare, _insert, _review):
    from plugin.vision.vision_egress import insert_vision_result_into_writer

    mock_prepare.return_value = MagicMock()
    graphic = MagicMock()
    doc = MagicMock()
    doc.getGraphicObjects.return_value.hasByName.return_value = True
    um = MagicMock()
    um.isLocked.return_value = False
    um.getAllUndoActionTitles.side_effect = [(), ("WriterAgent: Insert OCR",)]
    doc.getUndoManager.return_value = um
    result = {"status": "ok", "helper": "extract_structure", "html": "<p>ocr</p>"}

    with (
        patch("plugin.doc.visual_helpers.get_graphic_object_by_name", return_value=graphic),
        patch("plugin.doc.visual_helpers.delete_graphic", side_effect=ToolExecutionError("delete failed", code="VISION_ERROR")),
    ):
        with pytest.raises(ToolExecutionError, match="delete failed"):
            insert_vision_result_into_writer(
                MagicMock(), doc, result, params={"image_name": "Img1", "delete_image_after_insert": True}
            )

    um.undo.assert_called_once()


@patch("plugin.writer.edit_review.review_recording_enabled", return_value=False)
@patch("plugin.writer.html_import.insert_html_at_cursor")
@patch("plugin.vision.vision_egress.prepare_vision_writer_insert")
def test_writer_delete_image_after_insert_off_keeps_graphic(mock_prepare, mock_insert, _review):
    from plugin.vision.vision_egress import insert_vision_result_into_writer

    mock_prepare.return_value = MagicMock()
    doc = MagicMock()
    doc.getGraphicObjects.return_value.hasByName.return_value = True
    result = {"status": "ok", "helper": "extract_structure", "html": "<p>ocr</p>"}

    with patch("plugin.doc.visual_helpers.delete_graphic") as mock_delete:
        outcome = insert_vision_result_into_writer(
            MagicMock(), doc, result, params={"image_name": "Img1", "delete_image_after_insert": False}
        )

    assert outcome == "inserted"

    mock_insert.assert_called_once()
    mock_delete.assert_not_called()


@pytest.mark.parametrize(
    "result",
    [
        {"status": "error", "code": "VISION_ERROR", "message": "boom"},
        {"status": "ok", "helper": "extract_structure", "html": "   "},
    ],
)
def test_writer_delete_image_after_insert_error_or_empty_keeps_graphic(result):
    from plugin.vision.vision_egress import insert_vision_result_into_writer

    doc = MagicMock()
    with patch("plugin.doc.visual_helpers.delete_graphic") as mock_delete:
        with pytest.raises(ToolExecutionError):
            insert_vision_result_into_writer(
                MagicMock(), doc, result, params={"image_name": "Img1", "delete_image_after_insert": True}
            )

    mock_delete.assert_not_called()


def _calc_egress_patches(doc: MagicMock, graphic: MagicMock, *, mode: str):
    from contextlib import ExitStack

    stack = ExitStack()
    stack.enter_context(patch("plugin.framework.doc_type.is_writer", return_value=False))
    stack.enter_context(patch("plugin.framework.doc_type.is_calc", return_value=True))
    stack.enter_context(patch("plugin.vision.vision_egress.resolve_vision_insert_mode", return_value=mode))
    stack.enter_context(patch("plugin.doc.visual_helpers.get_graphic_object_by_name", return_value=graphic))
    mock_delete = stack.enter_context(patch("plugin.doc.visual_helpers.delete_graphic"))
    return stack, mock_delete


@patch("plugin.calc.vision_egress.insert_vision_html_into_calc")
def test_calc_delete_image_after_insert_on_and_off(mock_calc_html):
    from plugin.vision.vision_egress import insert_vision_result

    graphic = MagicMock()
    doc = MagicMock()
    result = {"status": "ok", "helper": "extract_structure", "html": "<p>ocr</p>"}

    ctx = MagicMock()
    stack, mock_delete = _calc_egress_patches(doc, graphic, mode="html")
    with stack:
        assert (
            insert_vision_result(ctx, doc, result, params={"image_name": "Img1", "delete_image_after_insert": True})
            == "image_deleted"
        )
        mock_delete.assert_called_once_with(ctx, doc, graphic)

        mock_delete.reset_mock()
        assert (
            insert_vision_result(ctx, doc, result, params={"image_name": "Img1", "delete_image_after_insert": False})
            == "inserted"
        )
        mock_delete.assert_not_called()


@patch("plugin.calc.vision_egress.insert_vision_structure_into_calc", return_value=2)
def test_calc_structured_delete_image_after_insert_on_and_off(mock_calc_struct):
    from plugin.vision.vision_egress import insert_vision_result

    graphic = MagicMock()
    doc = MagicMock()
    result = {"status": "ok", "helper": "extract_structure", "html": "<table></table>", "tables": [{"name": "t1"}]}

    ctx = MagicMock()
    stack, mock_delete = _calc_egress_patches(doc, graphic, mode="structured")
    with stack:
        assert (
            insert_vision_result(ctx, doc, result, params={"image_name": "Img1", "delete_image_after_insert": True})
            == "image_deleted"
        )
        mock_calc_struct.assert_called_once_with(doc, result, image_name="Img1", graphic=None)
        mock_delete.assert_called_once_with(ctx, doc, graphic)

        mock_delete.reset_mock()
        assert (
            insert_vision_result(ctx, doc, result, params={"image_name": "Img1", "delete_image_after_insert": False})
            == "inserted"
        )
        mock_delete.assert_not_called()


@patch("plugin.calc.vision_egress.insert_vision_html_into_calc")
def test_calc_delete_image_after_insert_error_or_empty_keeps_graphic(mock_calc_html):
    from plugin.vision.vision_egress import insert_vision_result

    graphic = MagicMock()
    doc = MagicMock()
    for bad in (
        {"status": "error", "code": "VISION_ERROR", "message": "boom"},
        {"status": "ok", "helper": "extract_structure", "html": ""},
    ):
        stack, mock_delete = _calc_egress_patches(doc, graphic, mode="html")
        with stack:
            with pytest.raises(ToolExecutionError):
                insert_vision_result(MagicMock(), doc, bad, params={"image_name": "Img1", "delete_image_after_insert": True})
            mock_delete.assert_not_called()
            mock_calc_html.assert_not_called()




# Docling returns skeleton HTML (title, empty body) when OCR finds nothing; only full_text/tables show it.
_NO_TEXT_RESULT = {"status": "ok", "helper": "extract_structure", "html": "<h1>image</h1><p></p>", "full_text": "", "tables": []}


@patch("plugin.framework.doc_type.is_writer", return_value=True)
@patch("plugin.writer.html_import.insert_html_at_cursor")
@patch("plugin.vision.vision_egress.prepare_vision_writer_insert")
def test_writer_empty_ocr_inserts_nothing_and_keeps_image(mock_prepare, mock_insert, _is_writer):
    from plugin.vision.vision_egress import insert_vision_result

    doc = MagicMock()
    with patch("plugin.doc.visual_helpers.delete_graphic") as mock_delete:
        outcome = insert_vision_result(
            MagicMock(), doc, dict(_NO_TEXT_RESULT), params={"image_name": "Img1", "delete_image_after_insert": True}
        )

    assert outcome == "empty"
    mock_prepare.assert_not_called()
    mock_insert.assert_not_called()
    mock_delete.assert_not_called()


@pytest.mark.parametrize("mode", ["html", "structured"])
@patch("plugin.calc.vision_egress.insert_vision_structure_into_calc")
@patch("plugin.calc.vision_egress.insert_vision_html_into_calc")
def test_calc_empty_ocr_inserts_nothing_and_keeps_image(mock_calc_html, mock_calc_struct, mode):
    from plugin.vision.vision_egress import insert_vision_result

    doc = MagicMock()
    stack, mock_delete = _calc_egress_patches(doc, MagicMock(), mode=mode)
    with stack:
        outcome = insert_vision_result(
            MagicMock(), doc, dict(_NO_TEXT_RESULT), params={"image_name": "Img1", "delete_image_after_insert": True}
        )

    assert outcome == "empty"
    mock_calc_html.assert_not_called()
    mock_calc_struct.assert_not_called()
    mock_delete.assert_not_called()


def test_empty_html_without_text_is_empty_not_error():
    from plugin.vision.vision_egress import insert_vision_result

    paddle_empty = {"status": "ok", "helper": "extract_structure", "html": "", "full_text": ""}
    assert insert_vision_result(MagicMock(), MagicMock(), paddle_empty) == "empty"


@pytest.mark.parametrize("mode", ["html", "structured"])
@pytest.mark.parametrize("img_name", ["", "Img1"])
@patch("plugin.calc.vision_egress.insert_vision_structure_into_calc", return_value=2)
@patch("plugin.calc.vision_egress.insert_vision_html_into_calc")
def test_calc_delete_kept_graphic_ignores_name_lookup(_mock_html, _mock_struct, mode, img_name):
    from plugin.doc.visual_helpers import AmbiguousGraphicNameError
    from plugin.vision.vision_egress import insert_vision_result

    ctx = MagicMock()
    doc = MagicMock()
    kept_shape = MagicMock()
    result = {"status": "ok", "helper": "extract_structure", "html": "<p>ocr</p>", "tables": [{"name": "t1"}]}

    stack, mock_delete = _calc_egress_patches(doc, kept_shape, mode=mode)
    with stack, patch(
        "plugin.doc.visual_helpers.get_graphic_object_by_name",
        side_effect=AmbiguousGraphicNameError("Several shapes named 'Img1'"),
    ):
        outcome = insert_vision_result(
            ctx,
            doc,
            result,
            params={"image_name": img_name, "delete_image_after_insert": True},
            graphic=kept_shape,
        )

    assert outcome == "image_deleted"
    mock_delete.assert_called_once_with(ctx, doc, kept_shape)


@pytest.mark.parametrize("mode", ["html", "structured"])
@patch("plugin.calc.vision_egress.insert_vision_structure_into_calc", return_value=2)
@patch("plugin.calc.vision_egress.insert_vision_html_into_calc")
def test_calc_delete_unnamed_without_kept_graphic_still_refuses(_mock_html, _mock_struct, mode):
    from plugin.vision.vision_egress import insert_vision_result

    doc = MagicMock()
    result = {"status": "ok", "helper": "extract_structure", "html": "<p>ocr</p>", "tables": [{"name": "t1"}]}

    stack, mock_delete = _calc_egress_patches(doc, MagicMock(), mode=mode)
    with stack:
        with pytest.raises(ToolExecutionError) as exc:
            insert_vision_result(MagicMock(), doc, result, params={"delete_image_after_insert": True})

    assert exc.value.code == "VISION_ERROR"
    mock_delete.assert_not_called()
