# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Native tests: Calc and Writer document pooling (default) and reuse=False fresh docs."""

from plugin.testing_runner import native_test
from plugin.tests.testing_utils import with_native_doc

# Writer tests record the pooled doc's RuntimeUID; reader tests assert they got the
# *same* doc, so the emptiness checks prove reset-on-reuse rather than a fresh document.
_calc_uids: list = []
_writer_uids: list = []
_writer_shape_uids: list = []
_writer_style_seen: list = []


def _runtime_uid(doc) -> str:
    try:
        return str(doc.RuntimeUID)
    except Exception:
        return str(doc.getURL())


@native_test
@with_native_doc("calc")
def test_calc_reuse_writes_then_leaves_dirt(ctx, doc):
    sheet = doc.getSheets().getByIndex(0)
    sheet.getCellByPosition(0, 0).setString("leftover-calc")
    from plugin.scripting.document_scripts import set_calc_init_script

    set_calc_init_script(doc, "def double(x):\n    return x * 2")
    _calc_uids.append(_runtime_uid(doc))


@native_test
@with_native_doc("calc")
def test_calc_reuse_next_test_sees_empty_sheet(ctx, doc):
    sheet = doc.getSheets().getByIndex(0)
    assert sheet.getCellByPosition(0, 0).getString() == ""
    assert _calc_uids, "run test_calc_reuse_writes_then_leaves_dirt first (pairs share the pooled doc)"
    assert _runtime_uid(doc) == _calc_uids[0], "expected reused Calc doc, got a different RuntimeUID"
    from plugin.scripting.document_scripts import get_calc_init_script

    assert (get_calc_init_script(doc) or "").strip() == ""
    assert sheet.getCharts().getCount() == 0


@native_test
@with_native_doc("writer", reuse=True)
def test_writer_reuse_writes_then_leaves_dirt(ctx, doc):
    from plugin.writer.html_import import insert_content_at_position

    insert_content_at_position(
        doc,
        ctx,
        '<h2 style="font-size: 14pt; font-weight: bold;">SECTION HEADING</h2>'
        '<p>Body paragraph text.</p>',
        "end",
    )
    _writer_uids.append(_runtime_uid(doc))


@native_test
@with_native_doc("writer", reuse=True)
def test_writer_reuse_next_test_sees_empty_text(ctx, doc):
    assert doc.getText().getString() == ""
    assert _writer_uids, "run test_writer_reuse_writes_then_leaves_dirt first (pairs share the pooled doc)"
    assert _runtime_uid(doc) == _writer_uids[0], "expected reused Writer doc, got a different RuntimeUID"
    cursor = doc.getText().createTextCursor()
    cursor.gotoStart(False)
    weight = float(cursor.getPropertyValue("CharWeight"))
    assert weight < 135.0, f"empty para leaked bold CharWeight={weight!r}"
    para = str(cursor.getPropertyValue("ParaStyleName") or "")
    assert para in ("", "Standard"), f"empty para leaked style {para!r}"


@native_test
@with_native_doc("writer")
def test_writer_reuse_leaves_free_shape_on_draw_page(ctx, doc):
    from com.sun.star.awt import Size
    from com.sun.star.text.TextContentAnchorType import AT_PAGE

    shape = doc.createInstance("com.sun.star.drawing.CustomShape")
    shape.setSize(Size(2000, 2000))
    doc.getDrawPage().add(shape)
    # A page-anchored shape is not anchored in the body text, so wiping the text leaves it.
    shape.setPropertyValue("AnchorType", AT_PAGE)
    shape.setString("leftover shape text")
    assert doc.getDrawPage().getCount() == 1
    _writer_shape_uids.append(_runtime_uid(doc))


@native_test
@with_native_doc("writer")
def test_writer_reuse_next_test_sees_empty_draw_page(ctx, doc):
    assert _writer_shape_uids, "run test_writer_reuse_leaves_free_shape_on_draw_page first (pairs share the pooled doc)"
    assert _runtime_uid(doc) == _writer_shape_uids[0], "expected reused Writer doc, got a different RuntimeUID"
    assert doc.getDrawPage().getCount() == 0, "free shape leaked across the pooled Writer wipe"


_STYLE_PROPS = ("CharHeight", "CharFontName", "NumberingStyleName")


@native_test
@with_native_doc("writer")
def test_writer_reuse_rewrites_builtin_heading_style(ctx, doc):
    heading = doc.getStyleFamilies().getByName("ParagraphStyles").getByName("Heading 1")
    original = tuple(heading.getPropertyValue(name) for name in _STYLE_PROPS)
    heading.setPropertyValue("CharHeight", 33.0)
    heading.setPropertyValue("CharFontName", "Liberation Mono")
    heading.setPropertyValue("NumberingStyleName", "")
    assert heading.getPropertyValue("CharHeight") == 33.0
    _writer_style_seen.append((_runtime_uid(doc), original))


@native_test
@with_native_doc("writer")
def test_writer_reuse_next_test_sees_original_builtin_heading_style(ctx, doc):
    assert _writer_style_seen, "run test_writer_reuse_rewrites_builtin_heading_style first (pairs share the pooled doc)"
    uid, original = _writer_style_seen[0]
    assert _runtime_uid(doc) == uid, "expected reused Writer doc, got a different RuntimeUID"
    heading = doc.getStyleFamilies().getByName("ParagraphStyles").getByName("Heading 1")
    now = tuple(heading.getPropertyValue(name) for name in _STYLE_PROPS)
    assert now == original, "built-in style rewrite leaked across the pooled Writer wipe"


@native_test
@with_native_doc("writer")
def test_writer_pool_reset_builds_no_fixed_pitch_font_styles(ctx, doc):
    # Reading every built-in style builds Preformatted Text, which brings a fixed-pitch
    # Liberation Mono into the document; a paragraph font set by name then exports as
    # Liberation Mono1 in a full read.
    from plugin.writer import html_export

    text = doc.getText()
    text.setString("Mono paragraph")
    cursor = text.createTextCursor()
    cursor.gotoStart(False)
    cursor.gotoEndOfParagraph(True)
    cursor.setPropertyValue("CharFontName", "Liberation Mono")
    content = html_export.document_to_content(doc, ctx, scope="full")
    assert "font-family:Liberation Mono" in content, content
    assert "Liberation Mono1" not in content, content


@native_test
@with_native_doc("writer")
def test_writer_default_reuse_writes_then_leaves_dirt(ctx, doc):
    from plugin.writer.html_import import insert_content_at_position

    insert_content_at_position(
        doc,
        ctx,
        '<h2 style="font-size: 14pt; font-weight: bold;">SECTION HEADING</h2>'
        '<p>Body paragraph text.</p>',
        "end",
    )
    _writer_uids.append(_runtime_uid(doc))


@native_test
@with_native_doc("writer")
def test_writer_default_reuse_next_test_sees_empty_text(ctx, doc):
    assert doc.getText().getString() == ""
    assert _writer_uids, "run test_writer_default_reuse_writes_then_leaves_dirt first (pairs share the pooled doc)"
    assert _runtime_uid(doc) == _writer_uids[-1], "expected reused Writer doc, got a different RuntimeUID"
    cursor = doc.getText().createTextCursor()
    cursor.gotoStart(False)
    weight = float(cursor.getPropertyValue("CharWeight"))
    assert weight < 135.0, f"empty para leaked bold CharWeight={weight!r}"
    para = str(cursor.getPropertyValue("ParaStyleName") or "")
    assert para in ("", "Standard"), f"empty para leaked style {para!r}"


@native_test
@with_native_doc("calc", reuse=False)
def test_calc_reuse_false_still_empty(ctx, doc):
    assert doc.getSheets().getByIndex(0).getCellByPosition(0, 0).getString() == ""


@native_test
@with_native_doc("writer", reuse=False)
def test_writer_reuse_false_still_empty(ctx, doc):
    assert doc.getText().getString() == ""
