# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""UNO tests for get_string_without_tracked_deletions paragraph vs multi-para walks."""

from plugin.doc.text_helpers import get_string_without_tracked_deletions, visible_portions
from plugin.framework.uno_context import enum_has_more, get_desktop
from plugin.testing_runner import native_test
from plugin.tests.testing_utils import TestingFactory, with_native_doc
from plugin.writer.html_export import _visible_portions as html_visible_portions


# com.sun.star.text.ControlCharacter.PARAGRAPH_BREAK
_PARAGRAPH_BREAK = 0
_BOLD = 150.0


def _first_paragraph(doc):
    return doc.getText().createEnumeration().nextElement()


@native_test
def test_has_more_elements_returns_python_bool(ctx):
    """PyUNO maps sal_Bool from hasMoreElements() to bool, not integer 1."""

    def _assert_bool_row(enum, expected):
        value = enum.hasMoreElements()
        assert value is expected
        assert type(value) is bool
        assert enum_has_more(enum) is expected

    with TestingFactory.native_doc(ctx, "writer") as doc:
        enum = doc.getText().createEnumeration()
        _assert_bool_row(enum, True)
        enum.nextElement()
        _assert_bool_row(enum, False)

        desktop = get_desktop(ctx)
        desktop_enum = desktop.getComponents().createEnumeration()
        _assert_bool_row(desktop_enum, True)
        while enum_has_more(desktop_enum):
            desktop_enum.nextElement()
        _assert_bool_row(desktop_enum, False)

    with TestingFactory.native_doc(ctx, "calc") as doc:
        enum = doc.getSheets().createEnumeration()
        _assert_bool_row(enum, True)
        while enum_has_more(enum):
            enum.nextElement()
        _assert_bool_row(enum, False)

    with TestingFactory.native_doc(ctx, "draw") as doc:
        page = doc.getDrawPages().getByIndex(0)
        enum = page.createAnnotationEnumeration()
        assert enum is not None
        _assert_bool_row(enum, False)
        page.createAndInsertAnnotation()
        fresh_enum = page.createAnnotationEnumeration()
        _assert_bool_row(fresh_enum, True)
        fresh_enum.nextElement()
        _assert_bool_row(fresh_enum, False)


@native_test
def test_get_string_without_tracked_deletions_paragraph_bold_run_no_newline(ctx):
    """A paragraph with a bold run must stay one line (portions are not paragraphs)."""
    with TestingFactory.native_doc(ctx, "writer") as doc:
        text = doc.getText()
        cursor = text.createTextCursor()
        text.insertString(cursor, "Paragraph with normal and bold text", False)
        cursor.gotoStart(False)
        cursor.goRight(len("Paragraph with "), False)
        cursor.goRight(len("normal and bold"), True)
        cursor.setPropertyValue("CharWeight", _BOLD)

        para = _first_paragraph(doc)
        assert para.supportsService("com.sun.star.text.Paragraph")
        visible = para.getString()
        got = get_string_without_tracked_deletions(para)
        assert got == visible
        assert "\n" not in got
        assert got == "".join(chunk for _unused, chunk in visible_portions(para))
        assert got == "".join(chunk for _unused, chunk in html_visible_portions(para))

        # A cursor covering that single paragraph is document-mode (one child)
        # and must still match the visible string.
        full = text.createTextCursor()
        full.gotoStart(False)
        full.gotoEnd(True)
        assert get_string_without_tracked_deletions(full) == visible


@native_test
def test_get_string_without_tracked_deletions_multi_para_joins_with_newline(ctx):
    with TestingFactory.native_doc(ctx, "writer") as doc:
        text = doc.getText()
        cursor = text.createTextCursor()
        text.insertString(cursor, "First para", False)
        text.insertControlCharacter(cursor, _PARAGRAPH_BREAK, False)
        text.insertString(cursor, "Second para", False)

        expected = "First para\nSecond para"
        assert get_string_without_tracked_deletions(text) == expected

        full = text.createTextCursor()
        full.gotoStart(False)
        full.gotoEnd(True)
        assert get_string_without_tracked_deletions(full) == expected


def _rect(doc, text=""):
    shape = doc.createInstance("com.sun.star.drawing.RectangleShape")
    doc.getDrawPage().add(shape)
    if text:
        shape.setString(text)
    return shape


@native_test
def test_get_string_without_tracked_deletions_draw_shapes_read_text_uno(ctx):
    """Draw shapes return their text directly via getString."""
    with TestingFactory.native_doc(ctx, "writer") as doc:
        shapes = [_rect(doc) for _idx in range(40)]
        last_shape = _rect(doc, "alpha\nbeta")
        shapes.append(last_shape)

        for _round in range(3):
            for i, s in enumerate(shapes):
                got = get_string_without_tracked_deletions(s)
                if i < 40:
                    assert got == ""
                else:
                    assert got == "alpha\nbeta"
                    assert got == s.getString()


@native_test
def test_selection_cursor_snapshot_follows_edits_uno(ctx):
    from plugin.doc.text_helpers import selection_cursor_snapshot

    with TestingFactory.native_doc(ctx, "writer") as doc:
        text = doc.getText()
        cur = text.createTextCursor()
        text.insertString(cur, "alpha beta gamma", False)

        # Select "beta".
        desc = doc.createSearchDescriptor()
        desc.SearchString = "beta"
        found = doc.findFirst(desc)
        assert found is not None
        controller = doc.getCurrentController()
        controller.select(found)

        pin = selection_cursor_snapshot(doc)
        assert pin is not None
        assert pin.getString() == "beta"

        # 1. Follows edits inserted before it.
        before = text.createTextCursor()
        before.gotoStart(False)
        text.insertString(before, "HEAD ", False)
        assert pin.getString() == "beta"

        # 2. Collapses on setString("").
        pin.setString("")
        assert pin.isCollapsed()

        # 3. Sits after text inserted at it.
        ins = text.createTextCursorByRange(pin.getStart())
        text.insertString(ins, "NEW", False)
        assert text.compareRegionStarts(pin.getStart(), ins.getEnd()) == 0

        # 4. Survives deletion of its surroundings (collapses instead of throwing).
        desc2 = doc.createSearchDescriptor()
        desc2.SearchString = "gamma"
        found2 = doc.findFirst(desc2)
        controller.select(found2)
        pin2 = selection_cursor_snapshot(doc)
        assert pin2 is not None
        surrounding = text.createTextCursor()
        surrounding.gotoStart(False)
        surrounding.gotoEnd(True)
        surrounding.setString("")
        assert pin2.getString() == ""
        assert pin2.isCollapsed()

        # 5. Caret (no selection) yields a collapsed snapshot.
        text.insertString(text.createTextCursor(), "caret test", False)
        vc = controller.getViewCursor()
        vc.gotoStart(False)
        sel = controller.getSelection()
        assert sel.getCount() == 1
        caret_pin = selection_cursor_snapshot(doc)
        assert caret_pin is not None
        assert caret_pin.isCollapsed()


@native_test
def test_multi_para_deletion_hidden_in_helper_uno(ctx):
    from plugin.doc.text_helpers import get_document_length
    from plugin.tests.testing_utils import strike_across_paragraphs, with_native_doc

    @with_native_doc("writer")
    def _run(ctx, doc):
        text = doc.getText()
        strike_across_paragraphs(doc, text)
        expected = "Keep\nStart \n\nTail\nAfter"
        assert get_string_without_tracked_deletions(text) == expected
        assert get_document_length(doc) == len(expected)

    _run(ctx=ctx)


@native_test
def test_multi_para_deletion_hides_table_in_span_uno(ctx):
    from plugin.tests.testing_utils import strike_across_table, with_native_doc

    @with_native_doc("writer")
    def _run(ctx, doc):
        text = doc.getText()
        strike_across_table(doc, text)
        assert "CellGone" not in get_string_without_tracked_deletions(text)

    _run(ctx=ctx)


@native_test
@with_native_doc("writer")
def test_visible_range_text_from_inside_deletion_uno(ctx, doc):
    from plugin.doc.text_helpers import visible_range_text
    from plugin.tests.testing_utils import strike_across_paragraphs

    text = doc.getText()
    strike_across_paragraphs(doc, text)
    enum = text.createEnumeration()
    paras = []
    while enum.hasMoreElements():
        paras.append(enum.nextElement())

    # Middle + end paragraphs (starts inside Delete).
    mid_cur = text.createTextCursorByRange(paras[2].getStart())
    mid_cur.gotoRange(paras[3].getEnd(), True)
    assert visible_range_text(doc, mid_cur) == "\nTail"

    # Inside the start paragraph's deleted tail.
    in_cur = text.createTextCursorByRange(paras[1].getStart())
    in_cur.goRight(len("Start "), False)
    in_cur.gotoRange(paras[1].getEnd(), True)
    assert "gone" not in visible_range_text(doc, in_cur)


@native_test
@with_native_doc("writer")
def test_select_all_after_leading_table_hides_deleted_text_uno(ctx, doc):
    from plugin.doc.text_helpers import get_document_length, paragraph_deletion_flags, visible_range_text
    from plugin.tests.testing_utils import strike_all_after_leading_table

    strike_all_after_leading_table(doc, ctx)
    text = doc.getText()
    assert visible_range_text(doc, text) == "\n\n"
    assert get_document_length(doc) == 1

    enum = text.createEnumeration()
    els = []
    while enum.hasMoreElements():
        els.append(enum.nextElement())
    assert all(paragraph_deletion_flags(doc, els))


@native_test
@with_native_doc("writer")
def test_live_leading_table_before_struck_paragraph_keeps_table_text_uno(ctx, doc):
    text = doc.getText()
    tbl = doc.createInstance("com.sun.star.text.TextTable")
    tbl.initialize(1, 1)
    text.insertTextContent(text.getEnd(), tbl, False)
    tbl.getCellByName("A1").setString("cellA1")
    cur = text.createTextCursor()
    cur.gotoEnd(False)
    text.insertString(cur, "two", False)
    text.insertControlCharacter(cur, 0, False)
    text.insertString(cur, "three", False)

    enum = text.createEnumeration()
    els = []
    while enum.hasMoreElements():
        els.append(enum.nextElement())
    del_cur = text.createTextCursorByRange(els[1].getStart())
    del_cur.gotoRange(els[1].getEnd(), True)
    doc.setPropertyValue("RecordChanges", True)
    try:
        del_cur.setString("")
    finally:
        doc.setPropertyValue("RecordChanges", False)

    assert get_string_without_tracked_deletions(text) == "cellA1\n\nthree"

