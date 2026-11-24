# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Live UNO tests for tracked image deletion during vision OCR egress."""

from __future__ import annotations

from typing import Any

from plugin.framework.config import get_config, save
from plugin.testing_runner import native_test
from plugin.tests.testing_utils import logo_32_path, with_native_doc
from plugin.vision.vision_egress import insert_vision_result_into_writer
from plugin.writer.images.image_tools import insert_image_at_locator
from plugin.writer.inline_review import _resolve_all_agent_changes

_FLAG = "doc.agent_edit_review_mode"
_OCR_RESULT = {"status": "ok", "helper": "extract_structure", "html": "<p>OCR text</p>"}


def _dispatch(ctx: Any, doc: Any, cmd: str) -> None:
    helper = ctx.ServiceManager.createInstanceWithContext("com.sun.star.frame.DispatchHelper", ctx)
    helper.executeDispatch(doc.getCurrentController().getFrame(), cmd, "", 0, ())


def _reset_doc(doc: Any, ctx: Any) -> None:
    doc.setPropertyValue("RecordChanges", False)
    if len(doc.getRedlines()):
        _dispatch(ctx, doc, ".uno:AcceptAllTrackedChanges")
    text = doc.getText()
    cur = text.createTextCursor()
    cur.gotoStart(False)
    cur.gotoEnd(True)
    cur.setString("")
    cur.gotoStart(False)
    text.insertString(cur, "Before image", False)


@native_test
@with_native_doc("writer", reuse=False)
def test_vision_egress_recording_doc_review_off_reject_and_accept_uno(ctx: Any, doc: Any) -> None:
    logo = logo_32_path()
    prev = get_config(_FLAG)
    save({_FLAG: "off"})
    try:
        _reset_doc(doc, ctx)
        graphic = insert_image_at_locator(ctx, doc, logo, width_mm=10, height_mm=10)
        graphic.setName("OcrPic1")
        name = graphic.getName()
        doc.setPropertyValue("RecordChanges", True)
        try:
            outcome = insert_vision_result_into_writer(
                ctx, doc, dict(_OCR_RESULT), params={"image_name": name, "delete_image_after_insert": True}
            )
            assert outcome == "inserted"
            assert doc.getGraphicObjects().hasByName(name) is True
            assert len(doc.getRedlines()) >= 2

            _dispatch(ctx, doc, ".uno:RejectAllTrackedChanges")
            assert "OCR text" not in doc.getText().getString()
            assert doc.getGraphicObjects().hasByName(name) is True
        finally:
            doc.setPropertyValue("RecordChanges", False)

        _reset_doc(doc, ctx)
        graphic2 = insert_image_at_locator(ctx, doc, logo, width_mm=10, height_mm=10)
        graphic2.setName("OcrPic2")
        name2 = graphic2.getName()
        doc.setPropertyValue("RecordChanges", True)
        try:
            outcome2 = insert_vision_result_into_writer(
                ctx, doc, dict(_OCR_RESULT), params={"image_name": name2, "delete_image_after_insert": True}
            )
            assert outcome2 == "inserted"
            _dispatch(ctx, doc, ".uno:AcceptAllTrackedChanges")
            assert "OCR text" in doc.getText().getString()
            assert doc.getGraphicObjects().hasByName(name2) is False
        finally:
            doc.setPropertyValue("RecordChanges", False)
    finally:
        save({_FLAG: prev})


@native_test
@with_native_doc("writer", reuse=False)
def test_vision_egress_record_mode_resolve_all_agent_changes_uno(ctx: Any, doc: Any) -> None:
    logo = logo_32_path()
    prev = get_config(_FLAG)
    save({_FLAG: "record"})
    try:
        _reset_doc(doc, ctx)
        graphic = insert_image_at_locator(ctx, doc, logo, width_mm=10, height_mm=10)
        graphic.setName("ReviewPic1")
        name = graphic.getName()
        outcome = insert_vision_result_into_writer(
            ctx, doc, dict(_OCR_RESULT), params={"image_name": name, "delete_image_after_insert": True}
        )
        assert outcome == "inserted"
        res_reject = _resolve_all_agent_changes(doc, ctx, accept=False)
        assert res_reject.resolved >= 1
        assert "OCR text" not in doc.getText().getString()
        assert doc.getGraphicObjects().hasByName(name) is True

        _reset_doc(doc, ctx)
        graphic2 = insert_image_at_locator(ctx, doc, logo, width_mm=10, height_mm=10)
        graphic2.setName("ReviewPic2")
        name2 = graphic2.getName()
        outcome2 = insert_vision_result_into_writer(
            ctx, doc, dict(_OCR_RESULT), params={"image_name": name2, "delete_image_after_insert": True}
        )
        assert outcome2 == "inserted"
        res_accept = _resolve_all_agent_changes(doc, ctx, accept=True)
        assert res_accept.resolved >= 1
        assert "OCR text" in doc.getText().getString()
        assert doc.getGraphicObjects().hasByName(name2) is False
    finally:
        save({_FLAG: prev})
        if len(doc.getRedlines()):
            _dispatch(ctx, doc, ".uno:AcceptAllTrackedChanges")
