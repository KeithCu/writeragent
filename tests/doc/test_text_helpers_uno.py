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
def test_removes_anchored_matches_accept_all_uno(ctx, doc):
    import base64
    import os
    import tempfile
    from com.sun.star.text.TextContentAnchorType import AS_CHARACTER, AT_CHARACTER, AT_PARAGRAPH
    from plugin.doc.text_helpers import TrackedDeletions
    from plugin.framework.uno_context import execute_dispatch

    png_bytes = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
    )
    fd, img_path = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    with open(img_path, "wb") as f:
        f.write(png_bytes)
    try:
        provider = ctx.getServiceManager().createInstanceWithContext("com.sun.star.graphic.GraphicProvider", ctx)
        from com.sun.star.beans import PropertyValue
        pv = PropertyValue()
        pv.Name = "URL"
        pv.Value = "file://" + img_path
        graphic = provider.queryGraphic((pv,))

        def _check(name, expected, setup_fn, *, target_fn=None):
            doc.setPropertyValue("RecordChanges", False)
            text = doc.getText() if target_fn is None else target_fn()
            text.setString("")
            gfx = doc.createInstance("com.sun.star.text.TextGraphicObject")
            gfx.setPropertyValue("Graphic", graphic)
            gfx.setPropertyValue("Width", 200)
            gfx.setPropertyValue("Height", 200)
            setup_fn(text, gfx)
            gfx.setName(name)
            predicted = TrackedDeletions(doc).removes_anchored(gfx)
            execute_dispatch(ctx, doc.getCurrentController(), ".uno:AcceptAllTrackedChanges")
            actual = not doc.getGraphicObjects().hasByName(name)
            assert predicted is expected and actual is expected, (
                f"{name}: expected={expected}, predicted={predicted}, actual={actual}"
            )

        def _delete_range(text, start_para, start_off, end_para, end_off):
            enum = text.createEnumeration()
            paras = []
            while enum.hasMoreElements():
                paras.append(enum.nextElement())
            c = text.createTextCursorByRange(paras[start_para].getStart())
            if start_off:
                c.goRight(start_off, False)
            end_c = text.createTextCursorByRange(paras[end_para].getStart())
            if end_off:
                end_c.goRight(end_off, False)
            c.gotoRange(end_c, True)
            doc.setPropertyValue("RecordChanges", True)
            c.setString("")
            doc.setPropertyValue("RecordChanges", False)

        def _set_paras(text, lines):
            text.setString("")
            c = text.createTextCursor()
            for idx, line in enumerate(lines):
                if idx:
                    text.insertControlCharacter(c, 0, False)
                if line:
                    text.insertString(c, line, False)

        # AT_PARAGRAPH cases
        def _para_mid(text, gfx):
            _set_paras(text, ["one", "two"])
            gfx.setPropertyValue("AnchorType", AT_PARAGRAPH)
            text.insertTextContent(text.getStart(), gfx, False)
            _delete_range(text, 0, 2, 1, 2)

        def _para_first_of_multi(text, gfx):
            _set_paras(text, ["one", "two"])
            gfx.setPropertyValue("AnchorType", AT_PARAGRAPH)
            text.insertTextContent(text.getStart(), gfx, False)
            _delete_range(text, 0, 0, 1, 1)

        def _para_last_of_multi(text, gfx):
            _set_paras(text, ["one", "two"])
            gfx.setPropertyValue("AnchorType", AT_PARAGRAPH)
            text.insertTextContent(text.getEnd(), gfx, False)
            _delete_range(text, 0, 1, 1, 3)

        def _para_break_only(text, gfx):
            _set_paras(text, ["one", "two"])
            gfx.setPropertyValue("AnchorType", AT_PARAGRAPH)
            text.insertTextContent(text.getStart(), gfx, False)
            _delete_range(text, 0, 3, 1, 0)

        def _para_empty_first(text, gfx):
            _set_paras(text, ["", "two"])
            gfx.setPropertyValue("AnchorType", AT_PARAGRAPH)
            text.insertTextContent(text.getStart(), gfx, False)
            _delete_range(text, 0, 0, 1, 1)

        def _para_single_whole(text, gfx):
            _set_paras(text, ["one"])
            gfx.setPropertyValue("AnchorType", AT_PARAGRAPH)
            text.insertTextContent(text.getStart(), gfx, False)
            _delete_range(text, 0, 0, 0, 3)

        def _para_single_not_whole(text, gfx):
            _set_paras(text, ["aa", "bb"])
            gfx.setPropertyValue("AnchorType", AT_PARAGRAPH)
            text.insertTextContent(text.getStart(), gfx, False)
            _delete_range(text, 0, 0, 0, 2)

        # AT_CHARACTER cases
        def _at_char(offset):
            def _fn(text, gfx):
                text.setString("abcd")
                gfx.setPropertyValue("AnchorType", AT_CHARACTER)
                c = text.createTextCursor()
                c.gotoStart(False)
                if offset:
                    c.goRight(offset, False)
                text.insertTextContent(c, gfx, False)
                _delete_range(text, 0, 1, 0, 3)
            return _fn

        def _header_text():
            styles = doc.getStyleFamilies().getByName("PageStyles")
            s_name = "Standard" if styles.hasByName("Standard") else styles.getElementNames()[0]
            style = styles.getByName(s_name)
            style.setPropertyValue("HeaderIsOn", True)
            return style.getPropertyValue("HeaderText")

        def _para_table_then_struck(text, gfx):
            tbl = doc.createInstance("com.sun.star.text.TextTable")
            tbl.initialize(1, 1)
            text.insertTextContent(text.getStart(), tbl, False)
            cur = text.createTextCursor()
            cur.gotoEnd(False)
            text.insertString(cur, "Acme", False)
            gfx.setPropertyValue("AnchorType", AT_PARAGRAPH)
            text.insertTextContent(text.getEnd(), gfx, False)
            del_c = text.createTextCursorByRange(text.getStart())
            del_c.gotoEnd(True)
            doc.setPropertyValue("RecordChanges", True)
            del_c.setString("")
            doc.setPropertyValue("RecordChanges", False)

        def _para_two_empty_break(text, gfx):
            _set_paras(text, ["", ""])
            gfx.setPropertyValue("AnchorType", AT_PARAGRAPH)
            text.insertTextContent(text.getStart(), gfx, False)
            _delete_range(text, 0, 0, 1, 0)

        # AS_CHARACTER cases
        def _as_char(offset, del_end=4):
            def _fn(text, gfx):
                text.setString("abcd")
                gfx.setPropertyValue("AnchorType", AS_CHARACTER)
                c = text.createTextCursor()
                c.gotoStart(False)
                if offset:
                    c.goRight(offset, False)
                text.insertTextContent(c, gfx, False)
                # Anchor sits before the character at `offset`, shifting remaining chars by 1.
                _delete_range(text, 0, 1, 0, del_end)
            return _fn

        _check("p_mid", False, _para_mid)
        _check("p_first", True, _para_first_of_multi)
        _check("p_last", True, _para_last_of_multi)
        _check("p_break", False, _para_break_only)
        _check("p_empty_first", True, _para_empty_first)
        _check("p_single_whole", True, _para_single_whole)
        _check("p_single_whole_hdr", True, _para_single_whole, target_fn=_header_text)
        _check("p_single_not_whole", False, _para_single_not_whole)
        _check("p_table_then_struck_hdr", False, _para_table_then_struck, target_fn=_header_text)
        _check("p_two_empty_break", True, _para_two_empty_break)
        _check("at_char_inside", True, _at_char(2))
        _check("at_char_start", False, _at_char(1))
        _check("at_char_end", False, _at_char(3))
        _check("as_char_start", True, _as_char(1))
        _check("as_char_end", True, _as_char(3))
        _check("as_char_after", False, _as_char(4, del_end=4))
    finally:
        if os.path.exists(img_path):
            os.unlink(img_path)


@native_test
@with_native_doc("writer")
def test_content_control_and_in_content_metadata_visible_reads_uno(ctx, doc):
    from plugin.doc.text_helpers import build_heading_tree, get_document_length, read_visible_body, visible_body_range
    from plugin.framework.html_stripper import html_to_search_text
    from plugin.writer.html_export import document_to_content

    for service in (
        "com.sun.star.text.ContentControl",
        "com.sun.star.text.InContentMetadata",
        "com.sun.star.text.textfield.MetadataField",
    ):
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

    # Tracked deletion inside ContentControl and MetadataField: range HTML text matches full HTML text.
    for service in ("com.sun.star.text.ContentControl", "com.sun.star.text.textfield.MetadataField"):
        text = doc.getText()
        text.setString("xx CTRL yy")
        cur = text.createTextCursor()
        cur.gotoStart(False)
        cur.setPropertyValue("ParaStyleName", "Heading 1")
        cur.goRight(3, False)
        cur.goRight(4, True)
        ctrl = doc.createInstance(service)
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
        tree = build_heading_tree(doc)
        assert [c["text"] for c in tree["children"]] == ["xx CL yy"]


@native_test
@with_native_doc("writer")
def test_visible_range_text_after_astral_uno(ctx, doc):
    from plugin.doc.text_helpers import read_visible_body, visible_body_range, visible_range_text

    text = doc.getText()
    cur = text.createTextCursor()
    text.insertString(cur, "😀😀b", False)
    text.insertControlCharacter(cur, _PARAGRAPH_BREAK, False)
    text.insertString(cur, "second", False)

    first_para = _first_paragraph(doc)
    b_cur = text.createTextCursorByRange(first_para.getStart())
    b_cur.goRight(2, False)
    b_cur.gotoRange(first_para.getEnd(), True)
    assert visible_range_text(doc, b_cur) == "b"

    tail_cur = text.createTextCursorByRange(first_para.getEnd())
    tail_cur.gotoEnd(True)
    assert visible_range_text(doc, tail_cur) == "\nsecond"

    # Single paragraph with astral prefix.
    text.setString("😀abcdef")
    cur = text.createTextCursor()
    cur.gotoStart(False)
    cur.goRight(2, False)
    cur.gotoEnd(True)
    assert visible_range_text(doc, cur) == "bcdef"

    # Astral text, a field, and a tracked deletion in one paragraph.
    text.setString("")
    cur = text.createTextCursor()
    text.insertString(cur, "😀a ", False)
    field = doc.createInstance("com.sun.star.text.TextField.Author")
    field.setPropertyValue("IsFixed", True)
    field.setPropertyValue("Content", "Ann")
    text.insertTextContent(cur, field, False)
    text.insertString(cur, " GONE 😀z", False)
    doc.setPropertyValue("RecordChanges", True)
    try:
        del_cur = text.createTextCursorByRange(cur)
        del_cur.goLeft(len(" 😀z"), False)
        del_cur.goLeft(len("GONE "), True)
        del_cur.setString("")
    finally:
        doc.setPropertyValue("RecordChanges", False)

    body = read_visible_body(doc)[0]
    for a, b in ((0, 3), (1, 6), (2, 7), (3, 9), (7, len(body))):
        rng = visible_body_range(doc, a, b)
        assert rng is not None
        assert visible_range_text(doc, rng.cursor) == body[a:b]


@native_test
@with_native_doc("writer")
def test_visible_range_text_of_search_hit_in_cell_uno(ctx, doc):
    from plugin.doc.text_helpers import visible_range_text

    tbl = doc.createInstance("com.sun.star.text.TextTable")
    tbl.initialize(1, 1)
    doc.getText().insertTextContent(doc.getText().getEnd(), tbl, False)
    tbl.getCellByName("A1").setString("😀😀xx needle yy")

    sd = doc.createSearchDescriptor()
    sd.SearchString = "needle"
    found = doc.findFirst(sd)
    assert found is not None
    assert visible_range_text(doc, found) == "needle"


@native_test
@with_native_doc("writer")
def test_visible_range_text_table_edge_and_field_selections_uno(ctx, doc):
    from plugin.doc.text_helpers import live_selection_range, visible_range_text
    from plugin.framework.uno_context import execute_dispatch, uno_same

    text = doc.getText()
    controller = doc.getCurrentController()

    # Ctrl+A in a document starting with a table.
    tbl = doc.createInstance("com.sun.star.text.TextTable")
    tbl.initialize(1, 1)
    text.insertTextContent(text.getEnd(), tbl, False)
    tbl.getCellByName("A1").setString("T0")
    cur = text.createTextCursor()
    cur.gotoEnd(False)
    text.insertString(cur, "tail", False)

    cell = tbl.getCellByName("A1")
    controller.select(cell.getStart())
    for _ in range(3):
        execute_dispatch(ctx, controller, ".uno:SelectAll")
    sel = live_selection_range(controller)
    assert sel is not None and not uno_same(sel.getText(), text)
    assert "tail" in sel.getString()
    # "T0" without "tail" is the known enumeration limit when Ctrl+A starts in a leading table.
    assert visible_range_text(doc, sel) == "T0"

    # Selection inside an Input field.
    text.setString("Dear ")
    cur = text.createTextCursor()
    cur.gotoEnd(False)
    inp = doc.createInstance("com.sun.star.text.TextField.Input")
    inp.setPropertyValue("Content", "Customer")
    text.insertTextContent(cur, inp, False)
    text.insertString(cur, " name", False)

    inp_cur = text.createTextCursor()
    inp_cur.gotoStart(False)
    inp_cur.goRight(len("Dear ") + 1, False)
    inp_cur.goRight(len("Custom"), True)
    got_inp = visible_range_text(doc, inp_cur)
    assert got_inp == "Custom", (got_inp, inp_cur.getString())

    # Selection starting inside a MetadataField.
    text.setString("xx inner yy")
    mcur = text.createTextCursor()
    mcur.gotoStart(False)
    mcur.goRight(len("xx "), False)
    mcur.goRight(len("inner"), True)
    meta = doc.createInstance("com.sun.star.text.textfield.MetadataField")
    text.insertTextContent(mcur, meta, True)

    tail_cur = text.createTextCursor()
    tail_cur.gotoStart(False)
    tail_cur.goRight(len("xx in") + 1, False)
    tail_cur.gotoEnd(True)
    assert visible_range_text(doc, tail_cur) == "ner yy"


def _insert_and_strike_last_gone(doc, xtext):
    doc.setPropertyValue("RecordChanges", False)
    cur = xtext.createTextCursor()
    cur.gotoEnd(False)
    xtext.insertString(cur, "gone live gone", False)
    doc.setPropertyValue("RecordChanges", True)
    try:
        cur.gotoEnd(False)
        cur.goLeft(len("gone"), True)
        cur.setString("")
    finally:
        doc.setPropertyValue("RecordChanges", False)


@native_test
@with_native_doc("writer")
def test_search_hides_deletions_in_every_text_uno(ctx, doc):
    page_styles = doc.getStyleFamilies().getByName("PageStyles")
    std_page = page_styles.getByName("Standard")
    std_page.setPropertyValue("HeaderIsOn", True)
    _insert_and_strike_last_gone(doc, std_page.getPropertyValue("HeaderText"))

    body = doc.getText()
    footnote = doc.createInstance("com.sun.star.text.Footnote")
    body.insertTextContent(body.getEnd(), footnote, False)
    _insert_and_strike_last_gone(doc, footnote)

    outer = doc.createInstance("com.sun.star.text.TextTable")
    outer.initialize(1, 2)
    body.insertTextContent(body.getEnd(), outer, False)
    _insert_and_strike_last_gone(doc, outer.getCellByName("A1"))

    nested = doc.createInstance("com.sun.star.text.TextTable")
    nested.initialize(1, 1)
    b1 = outer.getCellByName("B1")
    b1.insertTextContent(b1.getEnd(), nested, False)
    _insert_and_strike_last_gone(doc, nested.getCellByName("A1"))

    _insert_and_strike_last_gone(doc, body)

    res = TestingFactory.execute_tool(doc, ctx, "search", {"pattern": "gone"}, doc_type="writer")
    assert res["status"] == "ok"
    assert res["count"] == 5
    assert len(res["matches"]) == 5
    for match in res["matches"]:
        assert match["context"].count("gone") == 1
