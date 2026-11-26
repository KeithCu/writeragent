# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""UNO tests: vision OCR HTML insert preserves the selected embedded graphic."""

from __future__ import annotations


from plugin.doc.visual_helpers import list_graphic_objects
from plugin.framework.uno_context import enum_has_more
from plugin.testing_runner import native_test
from plugin.tests.testing_utils import logo_32_path, with_native_doc
from plugin.vision.vision_egress import prepare_vision_writer_insert
from plugin.writer.html_import import insert_html_at_cursor
from plugin.writer.images.image_tools import insert_image_at_locator

_VISION_HTML_FIXTURE = "<p>Vision OCR line after image.</p>"



@native_test
@with_native_doc("writer")
def test_vision_html_insert_keeps_selected_graphic(ctx, doc):
    graphic = insert_image_at_locator(ctx, doc, logo_32_path(), width_mm=20, height_mm=20)
    assert graphic is not None, "failed to insert fixture image"

    controller = doc.getCurrentController()
    controller.select(graphic)

    before = list_graphic_objects(doc)
    assert len(before) == 1, f"expected one graphic before insert, got {before!r}"

    cursor = prepare_vision_writer_insert(doc, ctx)
    insert_html_at_cursor(doc, ctx, cursor, _VISION_HTML_FIXTURE, apply_styles=False)

    after = list_graphic_objects(doc)
    assert len(after) == 1, f"graphic was removed during HTML insert: before={before!r} after={after!r}"
    assert "Vision OCR line after image." in doc.getText().getString()


@native_test
@with_native_doc("writer")
def test_vision_html_insert_after_each_graphic_preserves_intervening_text(ctx, doc):
    """Select a range covering two images + text; insert by name must not delete the text."""
    from plugin.doc.visual_helpers import graphic_objects_in_selection

    text = doc.getText()
    text.setString("")

    cursor = text.createTextCursor()
    text.insertString(cursor, "BEFORE ", False)

    g1 = insert_image_at_locator(ctx, doc, logo_32_path(), width_mm=15, height_mm=15)
    assert g1 is not None
    name1 = g1.getName()

    cursor.gotoEnd(False)
    intervening = "BETWEEN_IMAGES"
    text.insertString(cursor, intervening, False)

    g2 = insert_image_at_locator(ctx, doc, logo_32_path(), width_mm=15, height_mm=15)
    assert g2 is not None
    name2 = g2.getName()

    cursor.gotoEnd(False)
    text.insertString(cursor, " AFTER", False)

    # Select whole document so both graphics are in range.
    view = doc.getCurrentController().getViewCursor()
    view.gotoStart(False)
    view.gotoEnd(True)

    pairs = graphic_objects_in_selection(doc)
    names = [n for n, _unused in pairs]
    assert name1 in names and name2 in names, f"expected both graphics in selection, got {names!r}"

    html1 = "<p>OCR_ONE</p>"
    html2 = "<p>OCR_TWO</p>"
    for name, html in ((name1, html1), (name2, html2)):
        insert_cursor = prepare_vision_writer_insert(doc, ctx, image_name=name)
        insert_html_at_cursor(doc, ctx, insert_cursor, html, apply_styles=False)

    body = doc.getText().getString()
    assert intervening in body, f"intervening text was deleted during multi OCR insert: {body!r}"
    assert "OCR_ONE" in body and "OCR_TWO" in body
    assert len(list_graphic_objects(doc)) >= 2
    idx_before = body.find("BEFORE ")
    idx_ocr_one = body.find("OCR_ONE")
    idx_between = body.find(intervening)
    idx_ocr_two = body.find("OCR_TWO")
    idx_after = body.find(" AFTER")
    for label, idx in (
        ("BEFORE ", idx_before),
        ("OCR_ONE", idx_ocr_one),
        (intervening, idx_between),
        ("OCR_TWO", idx_ocr_two),
        (" AFTER", idx_after),
    ):
        assert idx >= 0, f"{label!r} not found in {body!r}"
    assert idx_before < idx_ocr_one < idx_between < idx_ocr_two < idx_after, (
        f"expected strict document order, got {body!r}"
    )


def _paragraphs_with_graphics(text):
    """Return (paragraph_string, has_frame_portion) in document order."""
    rows = []
    enum = text.createEnumeration()
    while enum_has_more(enum):
        para = enum.nextElement()
        has_frame = False
        try:
            portions = para.createEnumeration()
            while enum_has_more(portions):
                portion = portions.nextElement()
                if str(portion.getPropertyValue("TextPortionType") or "") == "Frame":
                    has_frame = True
                    break
        except Exception:
            pass
        string = para.getString() if hasattr(para, "getString") else ""
        rows.append((string, has_frame))
    return rows


@native_test
@with_native_doc("writer")
def test_as_char_ocr_insert_after_graphic_keeps_following_text(ctx, doc):
    """As-char insert (insert_image_at_locator) must not split following text or add an empty para above the image."""
    from plugin.doc.visual_helpers import anchor_type_index, safe_get_property

    text = doc.getText()
    text.setString("")
    cursor = text.createTextCursor()
    text.insertString(cursor, "HEAD", False)

    graphic = insert_image_at_locator(ctx, doc, logo_32_path(), width_mm=15, height_mm=15)
    assert graphic is not None, "failed to insert as-char fixture image"
    assert anchor_type_index(safe_get_property(graphic, "AnchorType", None)) == 1, "expected AS_CHARACTER"
    graphic.setName("ocr-aschar")

    cursor.gotoEnd(False)
    text.insertString(cursor, "TAIL", False)

    insert_cursor = prepare_vision_writer_insert(doc, ctx, image_name="ocr-aschar")
    insert_html_at_cursor(doc, ctx, insert_cursor, "<p>OCR_HERE</p>", apply_styles=False)

    body = text.getString()
    assert "HEAD" in body and "TAIL" in body and "OCR_HERE" in body, body
    # goRight(1) on an already-after-graphic cursor splits TAIL into T / AIL.
    assert "TAIL" in body, f"following text was split: {body!r}"
    assert body.find("HEAD") < body.find("OCR_HERE") < body.find("TAIL"), f"expected HEAD, OCR, TAIL order in {body!r}"
    assert any(name == "ocr-aschar" for name, _unused in list_graphic_objects(doc))

    paras = _paragraphs_with_graphics(text)
    graphic_paras = [string for string, has_frame in paras if has_frame]
    assert graphic_paras, f"graphic paragraph disappeared: {paras!r} body={body!r}"
    # A break *before* the graphic would move HEAD into the preceding paragraph.
    assert any("HEAD" in string for string in graphic_paras), f"empty or split paragraph before the as-char graphic: {paras!r} body={body!r}"
