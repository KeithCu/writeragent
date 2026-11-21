# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for Vision / OCR dialog live preview job."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from plugin.chatbot.module_config_dialog import PreviewImage
from plugin.framework.errors import ToolExecutionError
from plugin.framework.send_cancellation import SendCancellation, SendCancelled
from plugin.vision.vision_preview import render_preview_page, run_vision_insert, run_vision_preview


def test_preview_uses_first_selected_image_and_dialog_values():
    ctx = MagicMock()
    doc = MagicMock()
    cancel = SendCancellation()
    reports: list[str] = []
    expected = PreviewImage(MagicMock(), 1600, 2000, "Done: 5 characters.")

    with (
        patch("plugin.vision.vision_preview.main.call", side_effect=lambda fn, *a, **kw: fn(*a, **kw)),
        patch("plugin.vision.vision_preview.get_active_document", return_value=doc),
        patch("plugin.vision.vision_preview.supports_vision_manual", return_value=True),
        patch("plugin.vision.vision_preview.graphic_objects_in_selection", return_value=[("Image2", MagicMock()), ("Image3", MagicMock())]),
        patch(
            "plugin.vision.vision_preview.run_trusted_vision",
            return_value={"status": "ok", "helper": "extract_structure", "html": "<p>hello</p>", "full_text": "hello"},
        ) as mock_run,
        patch("plugin.vision.vision_preview.render_preview_page", return_value=expected) as mock_render,
    ):
        out = run_vision_preview(ctx, {"insert_mode": "structured", "layout_model": "heron"}, cancel, reports.append)

    assert out is expected
    assert mock_run.call_args.kwargs["helper"] == "extract_structure"
    assert mock_run.call_args.kwargs["params"] == {"insert_mode": "structured", "layout_model": "heron", "image_name": "Image2"}
    mock_render.assert_called_once_with(ctx, "<p>hello</p>", "Done: 5 characters.")


def test_preview_error_result_raises_tool_error():
    ctx = MagicMock()
    doc = MagicMock()
    cancel = SendCancellation()

    with (
        patch("plugin.vision.vision_preview.main.call", side_effect=lambda fn, *a, **kw: fn(*a, **kw)),
        patch("plugin.vision.vision_preview.get_active_document", return_value=doc),
        patch("plugin.vision.vision_preview.supports_vision_manual", return_value=True),
        patch("plugin.vision.vision_preview.graphic_objects_in_selection", return_value=[("Image1", MagicMock())]),
        patch(
            "plugin.vision.vision_preview.run_trusted_vision",
            return_value={"status": "error", "code": "DOCLING_UNAVAILABLE", "message": "Install docling"},
        ),
    ):
        with pytest.raises(ToolExecutionError, match="Install docling"):
            run_vision_preview(ctx, {}, cancel, lambda _t: None)


def test_load_models_hook_sends_exported_params_with_format():
    ctx = MagicMock()
    doc = MagicMock()
    cancel = SendCancellation()
    reports: list[str] = []
    expected = PreviewImage(MagicMock(), 1600, 2000, "Done: 2 characters.")

    def fake_run_trusted(_ctx, _doc, *, helper, params=None, cancel=None, on_exported=None):
        on_exported({"lang": "en", "layout_model": "heron"}, b"\x89PNG\r\n\x1a\n")
        return {"status": "ok", "helper": helper, "html": "<p>hi</p>", "full_text": "hi"}

    with (
        patch("plugin.vision.vision_preview.main.call", side_effect=lambda fn, *a, **kw: fn(*a, **kw)),
        patch("plugin.vision.vision_preview.get_active_document", return_value=doc),
        patch("plugin.vision.vision_preview.supports_vision_manual", return_value=True),
        patch("plugin.vision.vision_preview.graphic_objects_in_selection", return_value=[("Image1", MagicMock())]),
        patch("plugin.vision.vision_preview.run_trusted_vision", side_effect=fake_run_trusted),
        patch("plugin.vision.vision_preview.run_vision", return_value={"status": "ok"}) as mock_rpc,
        patch("plugin.vision.vision_preview.render_preview_page", return_value=expected),
    ):
        run_vision_preview(ctx, {}, cancel, reports.append)

    mock_rpc.assert_called_once()
    spec = mock_rpc.call_args.args[1]
    assert spec["helper"] == "load_models"
    assert spec["params"] == {"lang": "en", "layout_model": "heron", "format": "image"}
    assert reports == ["Loading models…", "Recognizing text…", "Rendering preview…"]


def test_render_preview_page_closes_scratch_doc_on_failure():
    ctx = MagicMock()
    doc = MagicMock()
    style = MagicMock()
    style.getPropertyValue.side_effect = lambda name: 29700 if name == "Height" else 21000
    doc.getStyleFamilies.return_value.getByName.return_value.getByName.return_value = style

    with (
        patch("plugin.vision.vision_preview.new_blank_writer", return_value=doc),
        patch("plugin.vision.vision_preview.insert_html_at_cursor", side_effect=RuntimeError("bad html")),
    ):
        with pytest.raises(RuntimeError, match="bad html"):
            render_preview_page(ctx, "<p>x</p>", "Done: 1 characters.")

    doc.close.assert_called_once_with(True)


def test_preview_with_delete_image_after_insert_never_inserts_or_deletes():
    ctx = MagicMock()
    doc = MagicMock()
    cancel = SendCancellation()
    expected = PreviewImage(MagicMock(), 1600, 2000, "Done: 5 characters.")

    with (
        patch("plugin.vision.vision_preview.main.call", side_effect=lambda fn, *a, **kw: fn(*a, **kw)),
        patch("plugin.vision.vision_preview.get_active_document", return_value=doc),
        patch("plugin.vision.vision_preview.supports_vision_manual", return_value=True),
        patch("plugin.vision.vision_preview.graphic_objects_in_selection", return_value=[("Image1", MagicMock())]),
        patch(
            "plugin.vision.vision_preview.run_trusted_vision",
            return_value={"status": "ok", "helper": "extract_structure", "html": "<p>hello</p>", "full_text": "hello"},
        ),
        patch("plugin.vision.vision_preview.render_preview_page", return_value=expected),
        patch("plugin.vision.vision_preview.insert_vision_result") as mock_insert,
        patch("plugin.doc.visual_helpers.delete_graphic") as mock_delete,
    ):
        out = run_vision_preview(ctx, {"delete_image_after_insert": True}, cancel, lambda _t: None)

    assert out is expected
    mock_insert.assert_not_called()
    mock_delete.assert_not_called()


def test_run_vision_insert_inserts_all_images_and_warms_models_once():
    ctx = MagicMock()
    doc = MagicMock()
    cancel = SendCancellation()
    reports: list[str] = []

    res1 = {"status": "ok", "helper": "extract_structure", "html": "<p>one</p>", "full_text": "one", "image_name": "Img1"}
    res2 = {"status": "ok", "helper": "extract_structure", "html": "<p>two</p>", "full_text": "two", "image_name": "Img2"}

    def fake_selection(c, d, *, helper, params=None, insert_into_document=True, cancel=None, on_exported=None):
        assert insert_into_document is False
        on_exported({**(params or {}), "image_name": "Img1"}, b"\x89PNG\r\n\x1a\n")
        on_exported({**(params or {}), "image_name": "Img2"}, b"\x89PNG\r\n\x1a\n")
        return {
            "status": "ok",
            "images_processed": 2,
            "full_text": "one\n\ntwo",
            "individual_results": [res1, res2],
        }

    with (
        patch("plugin.vision.vision_preview.main.call", side_effect=lambda fn, *a, **kw: fn(*a, **kw)),
        patch("plugin.vision.vision_preview.get_active_document", return_value=doc),
        patch("plugin.vision.vision_preview.supports_vision_manual", return_value=True),
        patch("plugin.vision.vision_preview.is_draw", return_value=False),
        patch("plugin.vision.vision_preview.run_and_insert_vision_for_selection", side_effect=fake_selection),
        patch("plugin.vision.vision_preview.run_vision", return_value={"status": "ok"}) as mock_rpc,
        patch("plugin.vision.vision_preview.insert_vision_result") as mock_insert,
    ):
        summary = run_vision_insert(ctx, {"delete_image_after_insert": True, "lang": "en"}, cancel, reports.append)

    assert summary == "Inserted 2 images."
    assert mock_rpc.call_count == 1
    assert reports == ["Loading models…", "Recognizing text…", "Recognizing text…", "Inserting…"]
    assert mock_insert.call_count == 2
    assert mock_insert.call_args_list[0].kwargs["params"] == {"delete_image_after_insert": True, "lang": "en", "image_name": "Img1"}
    assert mock_insert.call_args_list[1].kwargs["params"] == {"delete_image_after_insert": True, "lang": "en", "image_name": "Img2"}


def test_run_vision_insert_fails_fast_on_draw():
    ctx = MagicMock()
    doc = MagicMock()
    cancel = SendCancellation()

    with (
        patch("plugin.vision.vision_preview.main.call", side_effect=lambda fn, *a, **kw: fn(*a, **kw)),
        patch("plugin.vision.vision_preview.get_active_document", return_value=doc),
        patch("plugin.vision.vision_preview.supports_vision_manual", return_value=True),
        patch("plugin.vision.vision_preview.is_draw", return_value=True),
        patch("plugin.vision.vision_preview.run_and_insert_vision_for_selection") as mock_sel,
    ):
        with pytest.raises(ToolExecutionError, match="use Preview for Draw/Impress"):
            run_vision_insert(ctx, {}, cancel, lambda _t: None)

    mock_sel.assert_not_called()


def test_run_vision_insert_cancelled_before_insert_skips_insert():
    ctx = MagicMock()
    doc = MagicMock()
    cancel = SendCancellation()

    def fake_selection(c, d, *, helper, params=None, insert_into_document=True, cancel=None, on_exported=None):
        cancel.cancel()
        return {
            "status": "ok",
            "images_processed": 1,
            "full_text": "hello",
            "individual_results": [{"status": "ok", "html": "<p>hello</p>", "full_text": "hello", "image_name": "Img1"}],
        }

    with (
        patch("plugin.vision.vision_preview.main.call", side_effect=lambda fn, *a, **kw: fn(*a, **kw)),
        patch("plugin.vision.vision_preview.get_active_document", return_value=doc),
        patch("plugin.vision.vision_preview.supports_vision_manual", return_value=True),
        patch("plugin.vision.vision_preview.is_draw", return_value=False),
        patch("plugin.vision.vision_preview.run_and_insert_vision_for_selection", side_effect=fake_selection),
        patch("plugin.vision.vision_preview.insert_vision_result") as mock_insert,
    ):
        with pytest.raises(SendCancelled):
            run_vision_insert(ctx, {}, cancel, lambda _t: None)

    mock_insert.assert_not_called()


def test_run_vision_insert_partial_failure_inserts_succeeded_and_raises():
    ctx = MagicMock()
    doc = MagicMock()
    cancel = SendCancellation()
    res1 = {"status": "ok", "helper": "extract_structure", "html": "<p>1</p>", "full_text": "1", "image_name": "Img1"}

    with (
        patch("plugin.vision.vision_preview.main.call", side_effect=lambda fn, *a, **kw: fn(*a, **kw)),
        patch("plugin.vision.vision_preview.get_active_document", return_value=doc),
        patch("plugin.vision.vision_preview.supports_vision_manual", return_value=True),
        patch("plugin.vision.vision_preview.is_draw", return_value=False),
        patch(
            "plugin.vision.vision_preview.run_and_insert_vision_for_selection",
            return_value={
                "status": "error",
                "code": "VISION_ERROR",
                "message": "OCR failed for Img2 (boom); 1 of 2 images completed.",
                "individual_results": [res1],
            },
        ),
        patch("plugin.vision.vision_preview.insert_vision_result") as mock_insert,
    ):
        with pytest.raises(ToolExecutionError, match="OCR failed for Img2"):
            run_vision_insert(ctx, {"delete_image_after_insert": False}, cancel, lambda _t: None)

    mock_insert.assert_called_once()
    assert mock_insert.call_args.kwargs["params"] == {"delete_image_after_insert": False, "image_name": "Img1"}


def test_run_vision_insert_total_error_never_inserts():
    ctx = MagicMock()
    doc = MagicMock()
    cancel = SendCancellation()

    with (
        patch("plugin.vision.vision_preview.main.call", side_effect=lambda fn, *a, **kw: fn(*a, **kw)),
        patch("plugin.vision.vision_preview.get_active_document", return_value=doc),
        patch("plugin.vision.vision_preview.supports_vision_manual", return_value=True),
        patch("plugin.vision.vision_preview.is_draw", return_value=False),
        patch(
            "plugin.vision.vision_preview.run_and_insert_vision_for_selection",
            return_value={"status": "error", "code": "VISION_ERROR", "message": "No text found.", "individual_results": []},
        ),
        patch("plugin.vision.vision_preview.insert_vision_result") as mock_insert,
    ):
        with pytest.raises(ToolExecutionError, match="No text found."):
            run_vision_insert(ctx, {"delete_image_after_insert": True}, cancel, lambda _t: None)

    mock_insert.assert_not_called()
