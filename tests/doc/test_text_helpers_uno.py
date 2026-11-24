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
def test_read_visible_body_offsets_uno(ctx):
    from plugin.doc.text_helpers import read_visible_body
    from plugin.tests.testing_utils import strike_across_paragraphs, strike_across_table, with_native_doc

    @with_native_doc("writer")
    def _run(ctx, doc):
        text = doc.getText()
        strike_across_paragraphs(doc, text)
        whole = text.createTextCursor()
        whole.gotoStart(False)
        whole.gotoEnd(True)

        enum = text.createEnumeration()
        paras = []
        while enum_has_more(enum):
            paras.append(enum.nextElement())
        after_start = paras[-1].getStart()
        end_pos = text.getEnd()

        body_text, offsets, _struck = read_visible_body(doc, [after_start, end_pos])
        assert body_text == get_string_without_tracked_deletions(whole)
        assert offsets == [18, 23]

        tbl = strike_across_table(doc, text)
        whole.gotoStart(False)
        whole.gotoEnd(True)
        cell_pos = tbl.getCellByName("A1").getStart()
        tbl_text, tbl_offsets, _struck2 = read_visible_body(doc, [cell_pos, text.getEnd()])
        assert tbl_text == get_string_without_tracked_deletions(whole)
        assert tbl_offsets == [None, 14]

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
    assert get_document_length(doc) == 2

    enum = text.createEnumeration()
    els = []
    while enum.hasMoreElements():
        els.append(enum.nextElement())
    assert all(paragraph_deletion_flags(doc, els))


@native_test
@with_native_doc("writer")
def test_live_leading_table_before_struck_paragraph_keeps_table_text_uno(ctx, doc):
    from plugin.doc.text_helpers import get_document_length, visible_range_text

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

    assert visible_range_text(doc, text) == "cellA1\n\nthree"
    assert get_document_length(doc) == len("cellA1\n\nthree")


@native_test
@with_native_doc("writer")
def test_visible_body_covers_leading_and_trailing_table_uno(ctx, doc):
    from plugin.doc.text_helpers import read_visible_body, visible_range_text

    body = doc.getText()
    for cell_val, add_para in (("cA1", "Fin"), ("zA1", "")):
        tbl = doc.createInstance("com.sun.star.text.TextTable")
        tbl.initialize(1, 1)
        body.insertTextContent(body.getEnd(), tbl, False)
        tbl.getCellByName("A1").setString(cell_val)
        cur = body.createTextCursor()
        cur.gotoEnd(False)
        if add_para:
            body.insertString(cur, add_para, False)

    text, offsets, _ = read_visible_body(doc, [body.getEnd()])
    assert text == visible_range_text(doc, body)
    assert text == "cA1\nFin\nzA1\n"
    assert offsets == [len(text)]


@native_test
@with_native_doc("writer")
def test_visible_body_range_round_trips_uno(ctx, doc):
    from plugin.doc.text_helpers import (
        DeletionState,
        TrackedDeletions,
        is_text_table,
        iter_elements,
        normalize_linebreaks,
        read_visible_body,
        table_visible_text,
        visible_body_range,
        visible_range_text,
    )
    from plugin.framework.errors import ToolExecutionError
    from plugin.tests.testing_utils import (
        strike_across_paragraphs,
        strike_across_table,
        strike_all_after_leading_table,
    )

    def _clear():
        while doc.getTextTables().getCount() > 0:
            doc.getTextTables().getByIndex(0).dispose()
        doc.getText().setString("")

    def _build_rich_layout():
        _clear()
        body = doc.getText()
        lead = doc.createInstance("com.sun.star.text.TextTable")
        lead.initialize(1, 1)
        body.insertTextContent(body.getEnd(), lead, False)
        lead.getCellByName("A1").setString("cA1")

        cur = body.createTextCursor()
        cur.gotoEnd(False)
        body.insertString(cur, "ab😀", False)
        field = doc.createInstance("com.sun.star.text.TextField.DateTime")
        body.insertTextContent(cur, field, False)
        fn = doc.createInstance("com.sun.star.text.Footnote")
        fn.setLabel("1")
        body.insertTextContent(cur, fn, False)
        body.insertControlCharacter(cur, 1, False)  # LINE_BREAK
        body.insertString(cur, "漢字", False)
        cur.goLeft(2, True)
        cur.setPropertyValue("RubyText", "かんじ")
        cur.collapseToEnd()
        body.insertString(cur, "STRUCK", False)
        cur.goLeft(len("STRUCK"), True)
        doc.setPropertyValue("RecordChanges", True)
        try:
            cur.setString("")
        finally:
            doc.setPropertyValue("RecordChanges", False)
        assert doc.getRedlines().getCount() == 1
        cur.gotoEnd(False)
        body.insertString(cur, "ef", False)

        tail = doc.createInstance("com.sun.star.text.TextTable")
        tail.initialize(1, 1)
        body.insertTextContent(body.getEnd(), tail, False)
        tail.getCellByName("A1").setString("zA1")

    layouts = [
        _build_rich_layout,
        lambda: (_clear(), strike_across_paragraphs(doc, doc.getText())),
        lambda: (_clear(), strike_across_table(doc, doc.getText())),
        lambda: (_clear(), strike_all_after_leading_table(doc, ctx)),
    ]

    for setup in layouts:
        setup()
        body = doc.getText()
        text, _, _ = read_visible_body(doc)
        assert text == visible_range_text(doc, body)
        state = DeletionState(TrackedDeletions(doc).state_at(body).in_delete)
        table_ranges = []
        pos = 0
        for el in iter_elements(body.createEnumeration()):
            if is_text_table(el):
                chunk = normalize_linebreaks(table_visible_text(el, state.in_delete))
                table_ranges.append((pos, pos + len(chunk)))
            else:
                chunk = normalize_linebreaks(visible_range_text(doc, el))
                # Update state across paragraphs by scanning el's portions
                from plugin.doc.text_helpers import visible_portions
                for _p in visible_portions(el, state=state):
                    pass
            pos += len(chunk) + 1

        def _in_table(off: int) -> bool:
            return any(ts <= off <= te for ts, te in table_ranges)

        pairs = [(i, i + 1) for i in range(len(text))]
        non_table = [i for i in range(len(text) + 1) if not _in_table(i)]
        if len(non_table) >= 2:
            pairs.append((non_table[0], non_table[-1]))
            pairs.append((non_table[0], non_table[len(non_table) // 2]))
            pairs.append((non_table[len(non_table) // 2], non_table[-1]))

        for start, end in pairs:
            if _in_table(start) or _in_table(end):
                try:
                    visible_body_range(doc, start, end)
                except ToolExecutionError:
                    continue
                raise AssertionError(f"Expected ToolExecutionError for table offset ({start}, {end})")
            found = visible_body_range(doc, start, end)
            assert found is not None
            _, got_offsets, _ = read_visible_body(doc, [found.cursor.getStart(), found.cursor.getEnd()])
            assert got_offsets == [found.start, found.end]
            assert text[found.start:found.end] == found.text


@native_test
@with_native_doc("writer")
def test_get_selection_text_hides_tracked_deletions_and_skips_table_range_uno(ctx, doc):
    from plugin.doc.text_helpers import get_selection_text
    from plugin.tests.testing_utils import strike_across_paragraphs

    text = doc.getText()
    strike_across_paragraphs(doc, text)
    enum = text.createEnumeration()
    paras = []
    while enum.hasMoreElements():
        paras.append(enum.nextElement())

    controller = doc.getCurrentController()
    rng_cur = text.createTextCursorByRange(paras[2].getStart())
    rng_cur.gotoRange(text.getEnd(), True)
    controller.select(rng_cur)
    assert get_selection_text(doc) == "\nTail\nAfter"

    para2_cur = text.createTextCursorByRange(paras[2].getStart())
    para2_cur.gotoRange(paras[2].getEnd(), True)
    controller.select(para2_cur)
    assert get_selection_text(doc) == ""

    tbl = doc.createInstance("com.sun.star.text.TextTable")
    tbl.initialize(2, 2)
    text.insertTextContent(text.getEnd(), tbl, False)
    controller.select(tbl.getCellRangeByName("A1:B2"))
    assert get_selection_text(doc) is None


@native_test
@with_native_doc("writer")
def test_content_control_and_in_content_metadata_visible_reads_uno(ctx, doc):
    from plugin.doc.text_helpers import get_document_length, read_visible_body, visible_body_range
    from plugin.framework.html_stripper import html_to_search_text
    from plugin.writer.html_export import document_to_content

    for service in ("com.sun.star.text.ContentControl", "com.sun.star.text.InContentMetadata"):
        text = doc.getText()
        text.setString("xx CTRL yy")
        cur = text.createTextCursor()
        cur.gotoStart(False)
        cur.goRight(3, False)
        cur.goRight(4, True)
        ctrl = doc.createInstance(service)
        text.insertTextContent(cur, ctrl, True)

        body = read_visible_body(doc)[0]
        assert body == "xx CTRL yy"
        assert get_document_length(doc) == 10
        rng = visible_body_range(doc, 3, 7)
        assert rng is not None
        assert rng.cursor.getString() == "CTRL"

    # Tracked deletion inside ContentControl: range HTML text matches full HTML text.
    text = doc.getText()
    text.setString("xx CTRL yy")
    cur = text.createTextCursor()
    cur.gotoStart(False)
    cur.goRight(3, False)
    cur.goRight(4, True)
    ctrl = doc.createInstance("com.sun.star.text.ContentControl")
    text.insertTextContent(cur, ctrl, True)
    doc.setPropertyValue("RecordChanges", True)
    try:
        del_rng = visible_body_range(doc, 4, 6)
        assert del_rng is not None
        del_rng.cursor.setString("")
    finally:
        doc.setPropertyValue("RecordChanges", False)

    assert read_visible_body(doc)[0] == "xx CL yy"
    full_txt = html_to_search_text(document_to_content(doc, ctx, scope="full")).strip()
    range_txt = html_to_search_text(document_to_content(doc, ctx, scope="range", range_start=0, range_end=8)).strip()
    assert full_txt == "xx CL yy"
    assert range_txt == full_txt
