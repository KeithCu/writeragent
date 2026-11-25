# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Native UNO integration tests for view-selection preservation in apply_document_content."""

import uno  # noqa: F401

from plugin.testing_runner import native_test
from plugin.tests.testing_utils import native_writer_tool_context, with_native_doc
from plugin.writer.apply_document_content import ApplyDocumentContent


def _clear_body(doc):
    doc.setPropertyValue("RecordChanges", False)
    doc.getText().setString("")


def _reset_three_paras(doc):
    _clear_body(doc)
    text = doc.getText()
    cur = text.createTextCursor()
    cur.gotoStart(False)
    text.insertString(cur, "Intro paragraph here.", False)
    text.insertControlCharacter(cur, 0, False)
    text.insertString(cur, "Alpha one. Beta two.", False)
    text.insertControlCharacter(cur, 0, False)
    text.insertString(cur, "Outro paragraph.", False)
    assert _body_structure(doc) == [
        "P:Intro paragraph here.",
        "P:Alpha one. Beta two.",
        "P:Outro paragraph.",
    ]


def _select_match(doc, needle: str):
    desc = doc.createSearchDescriptor()
    desc.SearchString = needle
    found = doc.findFirst(desc)
    assert found is not None, f"could not find {needle!r}"
    controller = doc.getCurrentController()
    controller.select(found)
    assert controller.getViewCursor().getString() == needle
    return controller


@native_test
@with_native_doc("writer")
def test_insert_after_selected_text_keeps_view_selection_uno(ctx, doc):
    tctx = native_writer_tool_context(doc, ctx)

    # Block HTML after selection at paragraph end.
    _reset_three_paras(doc)
    controller = _select_match(doc, "Beta two.")
    res = ApplyDocumentContent().execute(
        tctx,
        target="search",
        old_content="Beta two.",
        position="after",
        content="<p>New para.</p>",
    )
    assert res.get("status") == "ok", res
    assert controller.getViewCursor().getString() == "Beta two."
    assert "Beta two.\nNew para." in doc.getText().getString().replace("\r\n", "\n").replace("\r", "\n")

    # Inline content after mid-paragraph selection.
    _reset_three_paras(doc)
    controller = _select_match(doc, "Alpha one.")
    res = ApplyDocumentContent().execute(
        tctx,
        target="search",
        old_content="Alpha one.",
        position="after",
        content=" INS",
    )
    assert res.get("status") == "ok", res
    assert controller.getViewCursor().getString() == "Alpha one."

    # Plain multi-line content after selection at paragraph end.
    _reset_three_paras(doc)
    controller = _select_match(doc, "Beta two.")
    res = ApplyDocumentContent().execute(
        tctx,
        target="search",
        old_content="Beta two.",
        position="after",
        content="New A\n\nNew B",
    )
    assert res.get("status") == "ok", res
    assert controller.getViewCursor().getString() == "Beta two."


@native_test
@with_native_doc("writer")
def test_insert_after_selection_tracked_keeps_view_selection_uno(ctx, doc):
    tctx = native_writer_tool_context(doc, ctx)
    _reset_three_paras(doc)
    doc.setPropertyValue("RecordChanges", True)
    try:
        controller = _select_match(doc, "Beta two.")
        res = ApplyDocumentContent().execute(
            tctx,
            target="search",
            old_content="Beta two.",
            position="after",
            content="<p>New para.</p>",
        )
        assert res.get("status") == "ok", res
        assert controller.getViewCursor().getString() == "Beta two."
    finally:
        doc.setPropertyValue("RecordChanges", False)


@native_test
@with_native_doc("writer")
def test_insert_at_end_keeps_view_selection_uno(ctx, doc):
    tctx = native_writer_tool_context(doc, ctx)
    _reset_three_paras(doc)
    controller = _select_match(doc, "Outro paragraph.")
    res = ApplyDocumentContent().execute(
        tctx,
        target="end",
        content="<p>Tail.</p>",
    )
    assert res.get("status") == "ok", res
    assert controller.getViewCursor().getString() == "Outro paragraph."


@native_test
@with_native_doc("writer")
def test_insert_after_caret_still_moves_caret_uno(ctx, doc):
    tctx = native_writer_tool_context(doc, ctx)
    _reset_three_paras(doc)
    controller = _select_match(doc, "Beta two.")
    view_cursor = controller.getViewCursor()
    view_cursor.collapseToEnd()
    assert view_cursor.isCollapsed()

    res = ApplyDocumentContent().execute(
        tctx,
        target="search",
        old_content="Beta two.",
        position="after",
        content=" INS",
    )
    assert res.get("status") == "ok", res
    view_cursor = controller.getViewCursor()
    assert view_cursor.isCollapsed()
    para_cur = doc.getText().createTextCursorByRange(view_cursor.getStart())
    para_cur.gotoStartOfParagraph(True)
    assert para_cur.getString() == "Alpha one. Beta two. INS"


@native_test
@with_native_doc("writer")
def test_selection_target_uses_send_time_selection_when_caret_moves_uno(ctx, doc):
    from plugin.doc.text_helpers import selection_cursor_snapshot
    from plugin.framework.tool import SelectionPin
    from plugin.writer.get_document_content import GetDocumentContent

    # Pinned context: caret moves to document start, get_content and edit still see Beta two.
    _reset_three_paras(doc)
    controller = _select_match(doc, "Beta two.")
    tctx = native_writer_tool_context(doc, ctx)
    tctx.selection_pin = SelectionPin(selection_cursor_snapshot(doc), doc)
    vc = controller.getViewCursor()
    vc.gotoStart(False)

    res_get = GetDocumentContent().execute(tctx, scope="selection")
    assert res_get.get("status") == "ok", res_get
    assert "Beta two." in res_get.get("content", ""), res_get

    res = ApplyDocumentContent().execute(tctx, target="selection", content="REPLACED")
    assert res.get("status") == "ok", res
    body = doc.getText().getString().replace("\r\n", "\n").replace("\r", "\n")
    assert "Alpha one. REPLACED" in body
    assert "Beta two." not in body
    assert controller.getViewCursor().isCollapsed()
    assert doc.getText().compareRegionStarts(controller.getViewCursor().getStart(), doc.getText().getStart()) == 0

    # Unpinned context (MCP): replacement lands at the live caret (document start).
    _reset_three_paras(doc)
    controller = _select_match(doc, "Beta two.")
    unpinned_ctx = native_writer_tool_context(doc, ctx)
    vc = controller.getViewCursor()
    vc.gotoStart(False)

    res = ApplyDocumentContent().execute(unpinned_ctx, target="selection", content="MCP_")
    assert res.get("status") == "ok", res
    body = doc.getText().getString().replace("\r\n", "\n").replace("\r", "\n")
    assert body.startswith("MCP_Intro paragraph here.")
    assert "Beta two." in body


def _body_paras(doc):
    from plugin.framework.uno_context import enum_has_more

    enum = doc.getText().createEnumeration()
    out = []
    while enum_has_more(enum):
        el = enum.nextElement()
        if hasattr(el, "supportsService") and el.supportsService("com.sun.star.text.Paragraph"):
            out.append(el)
    return out


def _seed_three_simple_paras(doc):
    text = doc.getText()
    doc.setPropertyValue("RecordChanges", False)
    cur = text.createTextCursor()
    cur.gotoStart(False)
    cur.gotoEnd(True)
    cur.setString("")
    cur.gotoStart(False)
    cur.setPropertyValue("ParaStyleName", "Standard")
    text.insertString(cur, "Keep before", False)
    text.insertControlCharacter(cur, 0, False)
    cur.setPropertyValue("ParaStyleName", "Standard")
    text.insertString(cur, "Replace me", False)
    text.insertControlCharacter(cur, 0, False)
    cur.setPropertyValue("ParaStyleName", "Standard")
    text.insertString(cur, "Keep after", False)


@native_test
@with_native_doc("writer")
def test_position_before_after_apply_block_styles_uno(ctx, doc):
    tctx = native_writer_tool_context(doc, ctx)
    content = '<p data-lo-style="Heading2">H</p><p data-lo-para="text-align:center">P</p>'

    for pos in ("before", "after"):
        _seed_three_simple_paras(doc)
        res = ApplyDocumentContent().execute(
            tctx,
            target="search",
            old_content="Replace me",
            position=pos,
            content=content,
        )
        assert res.get("status") == "ok", res
        assert "warnings" not in res, res

        paras = {p.getString(): p for p in _body_paras(doc)}
        assert paras["H"].getPropertyValue("ParaStyleName") == "Heading 2"
        assert paras["P"].getPropertyValue("ParaAdjust") == 3
        assert paras["P"].getPropertyState("ParaAdjust").value == "DIRECT_VALUE"
        for untouched in ("Keep before", "Replace me", "Keep after"):
            assert paras[untouched].getPropertyValue("ParaStyleName") == "Standard"
            assert paras[untouched].getPropertyState("ParaAdjust").value == "DEFAULT_VALUE"


@native_test
@with_native_doc("writer")
def test_position_after_styles_while_recording_stay_insert_only_uno(ctx, doc):
    from plugin.framework.uno_context import enum_has_more

    tctx = native_writer_tool_context(doc, ctx)
    helper = ctx.getServiceManager().createInstanceWithContext("com.sun.star.frame.DispatchHelper", ctx)
    for pos in ("before", "after"):
        _seed_three_simple_paras(doc)
        doc.setPropertyValue("RecordChanges", True)
        try:
            res = ApplyDocumentContent().execute(
                tctx,
                target="search",
                old_content="Replace me",
                position=pos,
                content='<p data-lo-style="Heading2">H</p>',
            )
            assert res.get("status") == "ok", res
            paras = {p.getString(): p for p in _body_paras(doc)}
            assert paras["H"].getPropertyValue("ParaStyleName") == "Heading 2"

            rl_types = []
            enum = doc.getRedlines().createEnumeration()
            while enum_has_more(enum):
                rl = enum.nextElement()
                rl_types.append(str(rl.getPropertyValue("RedlineType")))
            assert set(rl_types) == {"Insert"}, (pos, rl_types)

            helper.executeDispatch(
                doc.getCurrentController().getFrame(),
                ".uno:RejectAllTrackedChanges",
                "",
                0,
                (),
            )
            assert [p.getString() for p in _body_paras(doc)] == [
                "Keep before",
                "Replace me",
                "Keep after",
            ]
        finally:
            doc.setPropertyValue("RecordChanges", False)


@native_test
@with_native_doc("writer")
def test_failed_table_insert_rolls_back_uno(ctx, doc):
    from plugin.framework.config import get_config, save

    tctx = native_writer_tool_context(doc, ctx)
    flag = "doc.agent_edit_review_mode"
    prev = get_config(flag)
    targets = [
        {"target": "full_document"},
        {"target": "end"},
        {"target": "beginning"},
        {"target": "search", "old_content": "Alpha one. Beta two."},
        {"target": "search", "old_content": "Alpha one. Beta two.", "position": "after"},
        {"target": "selection"},
    ]
    try:
        for mode in ("off", "record"):
            save({flag: mode})
            for kwargs in targets:
                _reset_three_paras(doc)
                if kwargs["target"] == "selection":
                    _select_match(doc, "Alpha one. Beta two.")
                before_text = doc.getText().getString()
                res = ApplyDocumentContent().execute(
                    tctx, content=["<p>x</p><table></table>"], **kwargs
                )
                assert res.get("status") == "error", (mode, kwargs, res)
                assert res.get("code") == "TABLE_INSERT_FAILED", (mode, kwargs, res)
                assert doc.getText().getString() == before_text, (mode, kwargs)
                assert doc.getRedlines().getCount() == 0, (mode, kwargs)
                assert doc.getTextTables().getCount() == 0, (mode, kwargs)
    finally:
        save({flag: prev})
        doc.setPropertyValue("RecordChanges", False)


@native_test
@with_native_doc("writer")
def test_full_document_table_tail_under_review_uno(ctx, doc):
    from plugin.framework.config import get_config, save
    from plugin.framework.uno_context import enum_has_more

    tctx = native_writer_tool_context(doc, ctx)
    flag = "doc.agent_edit_review_mode"
    prev = get_config(flag)
    content = ["<h1>H</h1><p>folder</p><table><tr><td>A</td><td>B</td></tr></table>"]
    save({flag: "record"})
    try:
        cur = doc.getText().createTextCursor()
        cur.gotoStart(False)
        cur.gotoEnd(True)
        cur.setString("")

        res = ApplyDocumentContent().execute(tctx, target="full_document", content=content)
        assert res.get("status") == "ok", res
        assert doc.getTextTables().getCount() == 1
        rl_types = []
        enum = doc.getRedlines().createEnumeration()
        while enum_has_more(enum):
            rl_types.append(str(enum.nextElement().getPropertyValue("RedlineType")))
        assert "Insert" in rl_types, rl_types

        res2 = ApplyDocumentContent().execute(tctx, target="full_document", content=content)
        assert res2.get("status") == "ok", res2
        assert doc.getTextTables().getCount() == 2
    finally:
        save({flag: prev})
        doc.setPropertyValue("RecordChanges", False)


def _body_structure(doc):
    from plugin.doc.text_helpers import iter_elements
    from plugin.writer.range_copy import supports_service

    out = []
    for el in iter_elements(doc.getText().createEnumeration()):
        if supports_service(el, "com.sun.star.text.TextTable"):
            out.append("T:" + el.getCellByName("A1").getString())
        else:
            out.append("P:" + el.getString())
    return out


def _setup_leading_tables(doc, count=1, page_desc="First Page", middle=False):
    _clear_body(doc)
    text = doc.getText()
    for idx in range(count):
        if middle and idx == 1:
            cur = text.createTextCursor()
            cur.gotoEnd(False)
            text.insertString(cur, "mid", False)
        tbl = doc.createInstance("com.sun.star.text.TextTable")
        tbl.initialize(1, 1)
        text.insertTextContent(text.getEnd(), tbl, False)
        if idx == 0 and page_desc:
            tbl.setPropertyValue("PageDescName", page_desc)
        tbl.getCellByName("A1").setString(f"old{idx}")
    cur = text.createTextCursor()
    cur.gotoEnd(False)
    text.insertString(cur, "tail", False)
    expected = [f"T:old{idx}" for idx in range(count)]
    if middle and count > 1:
        expected.insert(1, "P:mid")
    expected.append("P:tail")
    assert _body_structure(doc) == expected


@native_test
@with_native_doc("writer")
def test_full_document_leading_tables_off_keeps_page_desc_and_undoes_uno(ctx, doc):
    from plugin.framework.config import get_config, save

    tctx = native_writer_tool_context(doc, ctx)
    flag = "doc.agent_edit_review_mode"
    prev = get_config(flag)
    save({flag: "off"})
    try:
        for count in (1, 2):
            _setup_leading_tables(doc, count=count, page_desc="First Page")
            before = _body_structure(doc)
            res = ApplyDocumentContent().execute(tctx, target="full_document", content="<p>NEW</p>")
            assert res.get("status") == "ok", res
            assert _body_structure(doc) == ["P:NEW"]
            first_para = doc.getText().createEnumeration().nextElement()
            assert first_para.getPropertyValue("PageDescName") == "First Page"
            doc.getUndoManager().undo()
            assert _body_structure(doc) == before
    finally:
        save({flag: prev})


@native_test
@with_native_doc("writer")
def test_full_document_leading_tables_record_accept_reject_and_rewrite_uno(ctx, doc):
    from plugin.framework.config import get_config, save
    from plugin.framework.uno_context import enum_has_more, execute_dispatch

    tctx = native_writer_tool_context(doc, ctx)
    controller = doc.getCurrentController()
    flag = "doc.agent_edit_review_mode"
    prev = get_config(flag)
    save({flag: "record"})
    try:
        for count, middle in ((1, False), (2, False), (2, True)):
            _setup_leading_tables(doc, count=count, page_desc=None, middle=middle)
            res = ApplyDocumentContent().execute(tctx, target="full_document", content="<p>NEW</p>")
            assert res.get("status") == "ok", res
            enum = doc.getRedlines().createEnumeration()
            comments = []
            while enum_has_more(enum):
                comments.append(str(enum.nextElement().getPropertyValue("RedlineComment")))
            assert comments and all(c.startswith("wa-review:") for c in comments), comments
            execute_dispatch(ctx, controller, ".uno:AcceptAllTrackedChanges")
            assert _body_structure(doc) == ["P:NEW", "P:"]

            _setup_leading_tables(doc, count=count, page_desc=None, middle=middle)
            before = _body_structure(doc)
            res = ApplyDocumentContent().execute(tctx, target="full_document", content="<p>NEW</p>")
            assert res.get("status") == "ok", res
            execute_dispatch(ctx, controller, ".uno:RejectAllTrackedChanges")
            assert _body_structure(doc) == before

        for manual_rec in (False, True):
            for draft_html in (
                "<table><tr><td>D1</td></tr></table><p>t1</p>",
                "<table><tr><td>D1</td></tr></table><table><tr><td>D1b</td></tr></table><p>t1</p>",
            ):
                save({flag: "off" if manual_rec else "record"})
                _reset_three_paras(doc)
                if manual_rec:
                    doc.setPropertyValue("RecordChanges", True)
                r1 = ApplyDocumentContent().execute(tctx, target="full_document", content=draft_html)
                assert r1.get("status") == "ok", r1
                r2 = ApplyDocumentContent().execute(tctx, target="full_document", content="<p>D2</p>")
                assert r2.get("status") == "ok", r2
                execute_dispatch(ctx, controller, ".uno:AcceptAllTrackedChanges")
                assert _body_structure(doc) == ["P:D2", "P:"]

                _reset_three_paras(doc)
                if manual_rec:
                    doc.setPropertyValue("RecordChanges", True)
                before_orig = _body_structure(doc)
                r1 = ApplyDocumentContent().execute(tctx, target="full_document", content=draft_html)
                assert r1.get("status") == "ok", r1
                r2 = ApplyDocumentContent().execute(tctx, target="full_document", content="<p>D2</p>")
                assert r2.get("status") == "ok", r2
                execute_dispatch(ctx, controller, ".uno:RejectAllTrackedChanges")
                assert _body_structure(doc) == before_orig
    finally:
        save({flag: prev})
        doc.setPropertyValue("RecordChanges", False)


@native_test
@with_native_doc("writer")
def test_full_document_leading_table_rolls_back_on_import_failure_uno(ctx, doc):
    from unittest.mock import patch
    from plugin.framework.config import get_config, save

    tctx = native_writer_tool_context(doc, ctx)
    flag = "doc.agent_edit_review_mode"
    prev = get_config(flag)
    try:
        for mode in ("off", "record"):
            save({flag: mode})
            _setup_leading_tables(doc, count=1, page_desc=None)
            before = _body_structure(doc)
            with patch(
                "plugin.writer.html_import._import_and_drop_leftover",
                side_effect=RuntimeError("boom"),
            ):
                try:
                    ApplyDocumentContent().execute(tctx, target="full_document", content="<p>NEW</p>")
                    raise AssertionError("expected RuntimeError")
                except RuntimeError:
                    pass
            assert _body_structure(doc) == before, mode
            assert doc.getRedlines().getCount() == 0, mode
    finally:
        save({flag: prev})
        doc.setPropertyValue("RecordChanges", False)


@native_test
@with_native_doc("writer")
def test_full_document_leading_table_with_image_placeholder_uno(ctx, doc):
    import base64
    import os
    import tempfile
    from com.sun.star.text.TextContentAnchorType import AS_CHARACTER, AT_PARAGRAPH
    from plugin.framework.config import get_config, save
    from plugin.framework.uno_context import execute_dispatch
    from plugin.writer.html_export import document_to_content

    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
    )
    tctx = native_writer_tool_context(doc, ctx)
    controller = doc.getCurrentController()
    flag = "doc.agent_edit_review_mode"
    prev = get_config(flag)
    try:
        for mode, anchor_type in (("off", AS_CHARACTER), ("record", AS_CHARACTER), ("record", AT_PARAGRAPH)):
            save({flag: mode})
            _clear_body(doc)
            text = doc.getText()
            tbl = doc.createInstance("com.sun.star.text.TextTable")
            tbl.initialize(1, 1)
            text.insertTextContent(text.getStart(), tbl, False)
            cell = tbl.getCellByName("A1")
            cell.setString("logo")
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
                f.write(png)
                img_path = f.name
            try:
                g = doc.createInstance("com.sun.star.text.TextGraphicObject")
                g.setPropertyValue("GraphicURL", uno.systemPathToFileUrl(img_path))
                g.setPropertyValue("AnchorType", anchor_type)
                g.setPropertyValue("Width", 500)
                g.setPropertyValue("Height", 500)
                cell.insertTextContent(cell.getStart(), g, False)
            finally:
                os.unlink(img_path)
            cur = text.createTextCursor()
            cur.gotoEnd(False)
            text.insertString(cur, "tail", False)

            html = document_to_content(doc, ctx, scope="full")
            assert 'src=""' in html, html
            updated = html.replace("tail", "updated")
            res = ApplyDocumentContent().execute(tctx, target="full_document", content=updated)
            assert res.get("status") == "ok", (mode, anchor_type, res)
            if mode == "record":
                execute_dispatch(ctx, controller, ".uno:AcceptAllTrackedChanges")
            assert doc.getGraphicObjects().getCount() == 1, (mode, anchor_type)
            assert "updated" in doc.getText().getString(), (mode, anchor_type)

        # Case A: record mode, search-replace inside a 1-para cell with AT_PARAGRAPH image
        # and its wrapper in the replacement leaves 1 image before and after Accept.
        import re

        save({flag: "record"})
        _clear_body(doc)
        text = doc.getText()
        tbl = doc.createInstance("com.sun.star.text.TextTable")
        tbl.initialize(1, 1)
        text.insertTextContent(text.getStart(), tbl, False)
        cell = tbl.getCellByName("A1")
        cell.setString("logo")
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            f.write(png)
            img_path = f.name
        try:
            g = doc.createInstance("com.sun.star.text.TextGraphicObject")
            g.setPropertyValue("GraphicURL", uno.systemPathToFileUrl(img_path))
            g.setPropertyValue("AnchorType", AT_PARAGRAPH)
            g.setPropertyValue("Width", 500)
            g.setPropertyValue("Height", 500)
            cell.insertTextContent(cell.getStart(), g, False)
        finally:
            os.unlink(img_path)
        html_a = document_to_content(doc, ctx, scope="full")
        wrapper_match = re.search(
            r'<(div|span)\b[^>]*\bid="[^"]+"[^>]*>\s*(?:<p\b[^>]*>\s*)?<img\b[^>]*\bsrc=""[^>]*>\s*(?:</p>\s*)?</\1>',
            html_a,
        )
        assert wrapper_match is not None, html_a
        replacement = f"{wrapper_match.group(0)}newlogo"
        assert 'src=""' in replacement
        res_sr = ApplyDocumentContent().execute(
            tctx, target="search", old_content="logo", content=replacement
        )
        assert res_sr.get("status") == "ok", res_sr
        assert doc.getGraphicObjects().getCount() == 1
        execute_dispatch(ctx, controller, ".uno:AcceptAllTrackedChanges")
        assert doc.getGraphicObjects().getCount() == 1

        # Case B: record mode, full_document on [T(textless logo cell | company text), tail]
        _clear_body(doc)
        tbl = doc.createInstance("com.sun.star.text.TextTable")
        tbl.initialize(1, 2)
        text.insertTextContent(text.getStart(), tbl, False)
        logo_cell = tbl.getCellByName("A1")
        tbl.getCellByName("B1").setString("company text")
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            f.write(png)
            img_path = f.name
        try:
            g = doc.createInstance("com.sun.star.text.TextGraphicObject")
            g.setPropertyValue("GraphicURL", uno.systemPathToFileUrl(img_path))
            g.setPropertyValue("AnchorType", AT_PARAGRAPH)
            g.setPropertyValue("Width", 500)
            g.setPropertyValue("Height", 500)
            logo_cell.insertTextContent(logo_cell.getStart(), g, False)
        finally:
            os.unlink(img_path)
        cur = text.createTextCursor()
        cur.gotoEnd(False)
        text.insertString(cur, "tail", False)
        html_b = document_to_content(doc, ctx, scope="full")
        assert 'src=""' in html_b, html_b
        res_b = ApplyDocumentContent().execute(
            tctx, target="full_document", content=html_b.replace("tail", "updated")
        )
        assert res_b.get("status") == "ok", res_b
        execute_dispatch(ctx, controller, ".uno:AcceptAllTrackedChanges")
        assert doc.getGraphicObjects().getCount() == 1
    finally:
        save({flag: prev})
        doc.setPropertyValue("RecordChanges", False)


@native_test
@with_native_doc("writer")
def test_selection_spans_table_hidden_section_table_verify_and_titled_pic_uno(ctx, doc):
    import base64
    import os
    import tempfile
    from unittest.mock import patch
    from com.sun.star.text.TextContentAnchorType import AS_CHARACTER, AT_CHARACTER
    from plugin.framework.uno_context import execute_dispatch
    from plugin.writer import html_import
    from plugin.writer.html_export import document_to_content

    tctx = native_writer_tool_context(doc, ctx)
    text = doc.getText()
    controller = doc.getCurrentController()

    # 1. Selection starting in a leading table and ending in body is refused (off & record).
    for record in (False, True):
        _clear_body(doc)
        tbl = doc.createInstance("com.sun.star.text.TextTable")
        tbl.initialize(1, 1)
        text.insertTextContent(text.getStart(), tbl, False)
        tbl.getCellByName("A1").setString("old0")
        cur = text.createTextCursor()
        cur.gotoEnd(False)
        text.insertString(cur, "tail", False)
        execute_dispatch(ctx, controller, ".uno:SelectAll")
        doc.setPropertyValue("RecordChanges", record)
        try:
            res = ApplyDocumentContent().execute(tctx, target="selection", content="<p>NEW</p>")
            assert res.get("status") == "error", res
            assert res.get("code") == "SELECTION_SPANS_TABLE", res
            assert tbl.getCellByName("A1").getString() == "old0"
            assert "tail" in text.getString()
        finally:
            doc.setPropertyValue("RecordChanges", False)

    from plugin.writer.specialized.sections import SectionCreate, SectionSetVisibility

    # 2. Hidden leading section is refused by full_document (plain and with table).
    for with_table in (False, True):
        _clear_body(doc)
        cur = text.createTextCursor()
        cur.gotoStart(False)
        text.insertString(cur, "secret", False)
        cur.gotoEnd(False)
        text.insertControlCharacter(cur, 0, False)
        cur.gotoEnd(False)
        text.insertString(cur, "tail", False)
        sc_res = SectionCreate().execute(tctx, name=f"HiddenTop_{with_table}", locator="paragraph:0")
        assert sc_res["status"] == "ok", sc_res
        if with_table:
            sec = doc.getTextSections().getByName(f"HiddenTop_{with_table}")
            tbl = doc.createInstance("com.sun.star.text.TextTable")
            tbl.initialize(1, 1)
            tbl_cur = text.createTextCursorByRange(sec.getAnchor().getStart())
            tbl_cur.goRight(1, False)
            text.insertTextContent(tbl_cur, tbl, False)
            tbl.getCellByName("A1").setString("secret_tbl")
        sv_res = SectionSetVisibility().execute(tctx, name=f"HiddenTop_{with_table}", visible=False)
        assert sv_res["status"] == "ok", sv_res
        res = ApplyDocumentContent().execute(tctx, target="full_document", content="<p>NEW</p>")
        assert res.get("status") == "error", (res, with_table)
        assert res.get("code") == "HIDDEN_SECTION", res
        assert "secret" in doc.getTextSections().getByName(f"HiddenTop_{with_table}").getAnchor().getString()
        assert "tail" in text.getString()

    # 2b. Visible leading section is allowed, hidden middle section is refused (HIDDEN_SECTION), leading table is allowed.
    _clear_body(doc)
    cur = text.createTextCursor()
    cur.gotoStart(False)
    text.insertString(cur, "vis", False)
    cur.gotoEnd(False)
    text.insertControlCharacter(cur, 0, False)
    cur.gotoEnd(False)
    text.insertString(cur, "tail", False)
    assert SectionCreate().execute(tctx, name="VisTop", locator="paragraph:0")["status"] == "ok"
    assert ApplyDocumentContent().execute(tctx, target="full_document", content="<p>OK1</p>").get("status") == "ok"

    _clear_body(doc)
    cur = text.createTextCursor()
    cur.gotoStart(False)
    text.insertString(cur, "head", False)
    cur.gotoEnd(False)
    text.insertControlCharacter(cur, 0, False)
    cur.gotoEnd(False)
    text.insertString(cur, "mid", False)
    assert SectionCreate().execute(tctx, name="HidMid", locator="paragraph:1")["status"] == "ok"
    assert SectionSetVisibility().execute(tctx, name="HidMid", visible=False)["status"] == "ok"
    mid_res = ApplyDocumentContent().execute(tctx, target="full_document", content="<p>OK2</p>")
    assert mid_res.get("status") == "error", mid_res
    assert mid_res.get("code") == "HIDDEN_SECTION", mid_res
    assert "mid" in doc.getTextSections().getByName("HidMid").getAnchor().getString()

    _clear_body(doc)
    tbl = doc.createInstance("com.sun.star.text.TextTable")
    tbl.initialize(1, 1)
    text.insertTextContent(text.getStart(), tbl, False)
    tbl.getCellByName("A1").setString("t0")
    assert ApplyDocumentContent().execute(tctx, target="full_document", content="<p>OK3</p>").get("status") == "ok"

    # 3. full_document with a stubbed import that loses the table fails with a header table or struck table.
    _clear_body(doc)
    style = doc.getStyleFamilies().getByName("PageStyles").getByName("Standard")
    style.setPropertyValue("HeaderIsOn", True)
    header = style.getPropertyValue("HeaderText")
    htbl = doc.createInstance("com.sun.star.text.TextTable")
    htbl.initialize(1, 1)
    header.insertTextContent(header.getStart(), htbl, False)
    text.setString("body")
    with patch.object(html_import, "insert_html_fragment_at_cursor"):
        res = ApplyDocumentContent().execute(
            tctx, target="full_document", content="<table><tr><td>x</td></tr></table>"
        )
    assert res.get("status") == "error", res
    assert res.get("code") == "TABLE_INSERT_FAILED", res

    style.setPropertyValue("HeaderIsOn", False)
    _clear_body(doc)
    doc.setPropertyValue("RecordChanges", True)
    try:
        tbl = doc.createInstance("com.sun.star.text.TextTable")
        tbl.initialize(1, 1)
        text.insertTextContent(text.getStart(), tbl, False)
        with patch.object(html_import, "insert_html_fragment_at_cursor"):
            res = ApplyDocumentContent().execute(
                tctx, target="full_document", content="<table><tr><td>x</td></tr></table>"
            )
        assert res.get("status") == "error", res
        assert res.get("code") == "TABLE_INSERT_FAILED", res
    finally:
        doc.setPropertyValue("RecordChanges", False)

    # 4. Selection spanning a table replaced by a table succeeds.
    _clear_body(doc)
    text.setString("before")
    cur = text.createTextCursor()
    cur.gotoEnd(False)
    text.insertControlCharacter(cur, 0, False)
    tbl = doc.createInstance("com.sun.star.text.TextTable")
    tbl.initialize(1, 1)
    text.insertTextContent(cur, tbl, False)
    cur.gotoEnd(False)
    text.insertString(cur, "after", False)
    sel_all = text.createTextCursor()
    sel_all.gotoStart(False)
    sel_all.gotoEnd(True)
    controller.select(sel_all)
    res_ok = ApplyDocumentContent().execute(
        tctx, target="selection", content="<table><tr><td>replaced</td></tr></table>"
    )
    assert res_ok.get("status") == "ok", res_ok

    # 5. Titled picture round-trips through full_document without duplicating title text.
    _clear_body(doc)
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
    )
    fd, img_path = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    with open(img_path, "wb") as f:
        f.write(png)
    try:
        text.setString("abc")
        g = doc.createInstance("com.sun.star.text.TextGraphicObject")
        g.setPropertyValue("GraphicURL", uno.systemPathToFileUrl(img_path))
        g.setPropertyValue("AnchorType", AS_CHARACTER)
        g.setPropertyValue("Width", 400)
        g.setPropertyValue("Height", 400)
        text.insertTextContent(text.getEnd(), g, False)
        g.setPropertyValue("Title", "ti")
        html = document_to_content(doc, ctx, scope="full", include_images=False)
        res_pic = ApplyDocumentContent().execute(tctx, target="full_document", content=html)
        assert res_pic.get("status") == "ok", res_pic
        assert doc.getGraphicObjects().getCount() == 1
        assert text.getString().strip() == "abc"

        # 6. Recorded full_document with AT_CHARACTER at the end of a 2-paragraph body
        # deletes the original on Accept (IsDestroyFrameAnchoredAtChar) and keeps 1 copy.
        _clear_body(doc)
        cur = text.createTextCursor()
        cur.gotoStart(False)
        text.insertString(cur, "first", False)
        cur.gotoEnd(False)
        text.insertControlCharacter(cur, 0, False)
        cur.gotoEnd(False)
        text.insertString(cur, "second", False)
        g2 = doc.createInstance("com.sun.star.text.TextGraphicObject")
        g2.setPropertyValue("GraphicURL", uno.systemPathToFileUrl(img_path))
        g2.setPropertyValue("AnchorType", AT_CHARACTER)
        g2.setPropertyValue("Width", 400)
        g2.setPropertyValue("Height", 400)
        text.insertTextContent(text.getEnd(), g2, False)
        g2.setName("BodyEndChar")

        doc.setPropertyValue("RecordChanges", True)
        try:
            html_end = document_to_content(doc, ctx, scope="full", include_images=False)
            res_end = ApplyDocumentContent().execute(
                tctx, target="full_document", content=html_end.replace("second", "SECOND")
            )
            assert res_end.get("status") == "ok", res_end
            execute_dispatch(ctx, controller, ".uno:AcceptAllTrackedChanges")
            assert doc.getGraphicObjects().getCount() == 1
        finally:
            doc.setPropertyValue("RecordChanges", False)
    finally:
        os.unlink(img_path)


@native_test
@with_native_doc("writer")
def test_full_document_bracketed_prose_not_parsed_as_list_uno(ctx, doc):
    tctx = native_writer_tool_context(doc, ctx)

    res1 = ApplyDocumentContent().execute(tctx, target="full_document", content="[see <b>x</b>]")
    assert res1.get("status") == "ok", res1
    assert doc.getText().getString().strip() == "[see x]"

    res2 = ApplyDocumentContent().execute(tctx, target="full_document", content="[TODO] <p>later</p>")
    assert res2.get("status") == "ok", res2
    body = doc.getText().getString()
    assert "[TODO]" in body
    assert "later" in body


@native_test
@with_native_doc("writer")
def test_escaped_markup_stays_text_across_targets_uno(ctx, doc):
    tctx = native_writer_tool_context(doc, ctx)
    tool = ApplyDocumentContent()

    _clear_body(doc)
    res = tool.execute(tctx, target="full_document", content="<p>A &lt;b&gt;lit&lt;/b&gt;</p>")
    assert res.get("status") == "ok", res
    assert "<b>lit</b>" in doc.getText().getString()

    res = tool.execute(tctx, target="end", content="&lt;i&gt;end&lt;/i&gt;")
    assert res.get("status") == "ok", res
    assert "<i>end</i>" in doc.getText().getString()

    res = tool.execute(tctx, target="beginning", content="&lt;u&gt;beg&lt;/u&gt;")
    assert res.get("status") == "ok", res
    assert "<u>beg</u>" in doc.getText().getString()

    res = tool.execute(tctx, target="search", old_content="lit", position="replace", content="&lt;em&gt;rep&lt;/em&gt;")
    assert res.get("status") == "ok", res
    assert "<em>rep</em>" in doc.getText().getString()

    res = tool.execute(tctx, target="search", old_content="<em>rep</em>", position="after", content=" &lt;code&gt;aft&lt;/code&gt;")
    assert res.get("status") == "ok", res
    assert "<code>aft</code>" in doc.getText().getString()

    _select_match(doc, "<u>beg</u>")
    res = tool.execute(tctx, target="selection", content="&lt;b&gt;sel&lt;/b&gt;")
    assert res.get("status") == "ok", res
    assert "<b>sel</b>" in doc.getText().getString()


@native_test
@with_native_doc("writer")
def test_full_document_leading_sections_uno(ctx, doc):
    from plugin.framework.uno_context import execute_dispatch

    tctx = native_writer_tool_context(doc, ctx)
    tool = ApplyDocumentContent()
    text = doc.getText()

    def _make_leading_section(name: str, body: str, *, protected: bool = False):
        _clear_body(doc)
        sec = doc.createInstance("com.sun.star.text.TextSection")
        sec.setName(name)
        sec.setPropertyValue("IsProtected", protected)
        text.insertTextContent(text.getStart(), sec, False)
        cur = text.createTextCursor()
        cur.gotoStart(False)
        text.insertString(cur, body, False)
        return sec

    # 1. Protected leading section (and protected outer with unprotected inner) refuses.
    outer_sec = _make_leading_section("ProtOuter", "Keep protected", protected=False)
    inner_cur = text.createTextCursor()
    inner_cur.gotoStart(False)
    inner_cur.goRight(4, True)
    inner_sec = doc.createInstance("com.sun.star.text.TextSection")
    inner_sec.setName("UnprotInner")
    inner_sec.setPropertyValue("IsProtected", False)
    text.insertTextContent(inner_cur, inner_sec, True)
    assert inner_sec.getParentSection() is not None
    outer_sec.setPropertyValue("IsProtected", True)
    res_prot = tool.execute(tctx, target="full_document", content="<p>Replaced</p>")
    assert res_prot.get("status") == "error", res_prot
    assert res_prot.get("code") == "PROTECTED_SECTION", res_prot
    assert "Keep" in text.getString() and "protected" in text.getString()

    # 2. Nested visible leading sections are replaced outside; PageDescName/PageNumberOffset preserved; undo restores.
    outer_sec.setPropertyValue("IsProtected", False)
    first_cur = text.createTextCursor()
    first_cur.gotoStart(False)
    first_cur.setPropertyValue("PageDescName", "First Page")
    first_cur.setPropertyValue("PageNumberOffset", 7)
    res_vis = tool.execute(tctx, target="full_document", content="<p>Outside</p>")
    assert res_vis.get("status") == "ok", res_vis
    assert text.getString().strip() == "Outside"
    assert doc.getTextSections().getCount() == 0
    check_cur = text.createTextCursor()
    check_cur.gotoStart(False)
    assert check_cur.getPropertyValue("PageDescName") == "First Page"
    assert check_cur.getPropertyValue("PageNumberOffset") == 7
    doc.getUndoManager().undo()
    assert "Keep" in text.getString() and "protected" in text.getString()
    assert doc.getTextSections().getCount() == 2

    # 3. Leading TOC is replaced outside the index.
    _clear_body(doc)
    toc = doc.createInstance("com.sun.star.text.ContentIndex")
    text.insertTextContent(text.getStart(), toc, False)
    res_toc = tool.execute(tctx, target="full_document", content="<p>No TOC</p>")
    assert res_toc.get("status") == "ok", res_toc
    assert text.getString().strip() == "No TOC"
    assert doc.getTextSections().getCount() == 0

    # 4. Recorded leading section: Reject restores original text; new paragraph sits outside section.
    _make_leading_section("RecLead1", "Original")
    doc.setPropertyValue("RecordChanges", True)
    try:
        res_rec = tool.execute(tctx, target="full_document", content="<p>NewRec</p>")
        assert res_rec.get("status") == "ok", res_rec
        c1 = text.createTextCursor()
        c1.gotoStart(False)
        assert c1.getPropertyValue("TextSection") is None
        assert doc.getTextSections().getCount() == 1
        execute_dispatch(ctx, doc.getCurrentController(), ".uno:RejectAllTrackedChanges")
        assert "Original" in text.getString()
    finally:
        doc.setPropertyValue("RecordChanges", False)

    # 5. Recorded leading section (rebuilt fresh): Accept keeps new text outside section.
    _make_leading_section("RecLead2", "Original2")
    doc.setPropertyValue("RecordChanges", True)
    try:
        res_rec2 = tool.execute(tctx, target="full_document", content="<p>Accepted</p>")
        assert res_rec2.get("status") == "ok", res_rec2
        c2 = text.createTextCursor()
        c2.gotoStart(False)
        assert c2.getPropertyValue("TextSection") is None
        assert doc.getTextSections().getCount() == 1
        execute_dispatch(ctx, doc.getCurrentController(), ".uno:AcceptAllTrackedChanges")
        assert "Accepted" in text.getString()
        assert "Original2" not in text.getString()
        assert doc.getTextSections().getCount() == 0
    finally:
        doc.setPropertyValue("RecordChanges", False)


@native_test
@with_native_doc("writer")
def test_selection_object_and_cell_block_refuse_uno(ctx, doc):
    tctx = native_writer_tool_context(doc, ctx)
    tool = ApplyDocumentContent()
    _clear_body(doc)
    text = doc.getText()
    controller = doc.getCurrentController()

    frame = doc.createInstance("com.sun.star.text.TextFrame")
    frame.setName("SelFrame")
    frame.setPropertyValue("Width", 1000)
    frame.setPropertyValue("Height", 1000)
    text.insertTextContent(text.getStart(), frame, False)
    controller.select(frame)
    res_frame = tool.execute(tctx, target="selection", content="x")
    assert res_frame.get("status") == "error", res_frame
    assert res_frame.get("code") == "SELECTION_NOT_TEXT", res_frame

    _clear_body(doc)
    tbl = doc.createInstance("com.sun.star.text.TextTable")
    tbl.initialize(2, 2)
    text.insertTextContent(text.getStart(), tbl, False)
    controller.select(tbl.getCellRangeByName("A1:B1"))
    res_cells = tool.execute(tctx, target="selection", content="x")
    assert res_cells.get("status") == "error", res_cells
    assert res_cells.get("code") == "SELECTION_NOT_TEXT", res_cells

    _clear_body(doc)
    line = doc.createInstance("com.sun.star.drawing.LineShape")
    line.setSize(uno.createUnoStruct("com.sun.star.awt.Size", 1000, 0))
    text.insertTextContent(text.getStart(), line, False)
    controller.select(line)
    res_line = tool.execute(tctx, target="selection", content="x")
    assert res_line.get("status") == "error", res_line
    assert res_line.get("code") == "SELECTION_NOT_TEXT", res_line

    _clear_body(doc)
    ole = doc.createInstance("com.sun.star.text.TextEmbeddedObject")
    ole.setPropertyValue("CLSID", "078B7ABA-54FC-457F-8551-6147e776a997")
    text.insertTextContent(text.getStart(), ole, False)
    controller.select(ole)
    res_ole = tool.execute(tctx, target="selection", content="x")
    assert res_ole.get("status") == "error", res_ole
    assert res_ole.get("code") == "SELECTION_NOT_TEXT", res_ole


@native_test
def test_selection_in_print_preview_refuses_uno(ctx):
    from plugin.framework.errors import best_effort
    from plugin.framework.uno_context import execute_dispatch, get_desktop

    desktop = get_desktop(ctx)
    doc = desktop.loadComponentFromURL("private:factory/swriter", "_blank", 0, ())
    try:
        doc.getText().setString("Hello preview")
        execute_dispatch(ctx, doc.getCurrentController(), ".uno:PrintPreview")
        res = ApplyDocumentContent().execute(
            native_writer_tool_context(doc, ctx),
            target="selection",
            content="INS",
        )
        assert res.get("status") == "error", res
        assert res.get("code") == "SELECTION_NOT_TEXT", res
        assert doc.getText().getString() == "Hello preview"
    finally:
        best_effort(lambda: doc.close(True), "close preview writer doc")


@native_test
@with_native_doc("writer")
def test_before_after_markup_decodes_html5_entities_uno(ctx, doc):
    tctx = native_writer_tool_context(doc, ctx)
    tool = ApplyDocumentContent()
    for pos, content in (
        ("before", "<b>&check; &lt;b&gt; &sect 2o R&#36;5</b> "),
        ("after", "<p>&check; &lt;b&gt; &sect 2o R&#36;5</p>"),
    ):
        _clear_body(doc)
        doc.getText().setString("anchor")
        res = tool.execute(tctx, target="search", old_content="anchor", position=pos, content=content)
        assert res.get("status") == "ok", res
        body = doc.getText().getString()
        assert "✓" in body, body
        assert "<b>" in body, body
        assert "R$5" in body, body
        assert "&check;" not in body, body


@native_test
@with_native_doc("writer")
def test_linked_picture_round_trip_uno(ctx, doc):
    import base64
    import os
    import re
    import tempfile
    from com.sun.star.text.TextContentAnchorType import AS_CHARACTER, AT_CHARACTER, AT_PARAGRAPH
    from plugin.writer.html_export import document_to_content

    tctx = native_writer_tool_context(doc, ctx)
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
    )
    fd, img_path = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    with open(img_path, "wb") as f:
        f.write(png)
    try:
        text = doc.getText()

        def _setup_pic(anchor_type, url: str, name: str, *, link_name: str = "", link_target: str = "") -> None:
            _clear_body(doc)
            while doc.getGraphicObjects().getCount():
                doc.getGraphicObjects().getByIndex(0).dispose()
            cur = text.createTextCursor()
            cur.gotoStart(False)
            cur.gotoEnd(True)
            cur.setPropertyToDefault("HyperLinkURL")
            cur.setPropertyToDefault("HyperLinkName")
            cur.setPropertyToDefault("HyperLinkTarget")
            text.setString("before")
            gfx = doc.createInstance("com.sun.star.text.TextGraphicObject")
            gfx.setPropertyValue("GraphicURL", uno.systemPathToFileUrl(img_path))
            gfx.setPropertyValue("AnchorType", anchor_type)
            gfx.setPropertyValue("Width", 400)
            gfx.setPropertyValue("Height", 400)
            gfx.setPropertyValue("HyperLinkURL", url)
            if link_name:
                gfx.setPropertyValue("HyperLinkName", link_name)
            if link_target:
                gfx.setPropertyValue("HyperLinkTarget", link_target)
            text.insertTextContent(text.getStart(), gfx, False)
            gfx.setName(name)

        # AT_PARAGRAPH: keep, change href, and remove <a> via full_document (clearing name and target).
        _setup_pic(
            AT_PARAGRAPH,
            "https://example.org/orig",
            "LinkedPara",
            link_name="origName",
            link_target="_blank",
        )
        html = document_to_content(doc, ctx, scope="full", include_images=False)
        res = ApplyDocumentContent().execute(tctx, target="full_document", content=html)
        assert res.get("status") == "ok", res
        assert doc.getGraphicObjects().getCount() == 1
        assert doc.getGraphicObjects().getByIndex(0).getPropertyValue("HyperLinkURL") == "https://example.org/orig"

        html_changed = html.replace("https://example.org/orig", "https://example.org/changed")
        res_chg = ApplyDocumentContent().execute(tctx, target="full_document", content=html_changed)
        assert res_chg.get("status") == "ok", res_chg
        assert doc.getGraphicObjects().getByIndex(0).getPropertyValue("HyperLinkURL") == "https://example.org/changed"

        html_unlinked = re.sub(r"<a\b[^>]*>(.*?)</a>", r"\1", html_changed, flags=re.DOTALL)
        res_rm = ApplyDocumentContent().execute(tctx, target="full_document", content=html_unlinked)
        assert res_rm.get("status") == "ok", res_rm
        pic = doc.getGraphicObjects().getByIndex(0)
        assert pic.getPropertyValue("HyperLinkURL") == ""
        assert pic.getPropertyValue("HyperLinkName") == ""
        assert pic.getPropertyValue("HyperLinkTarget") == ""

        # AT_CHARACTER via full_document.
        _setup_pic(AT_CHARACTER, "https://example.org/atchar", "LinkedAtChar")
        html_at = document_to_content(doc, ctx, scope="full", include_images=False)
        res_at = ApplyDocumentContent().execute(tctx, target="full_document", content=html_at)
        assert res_at.get("status") == "ok", res_at
        assert doc.getGraphicObjects().getByIndex(0).getPropertyValue("HyperLinkURL") == "https://example.org/atchar"

        # AS_CHARACTER via selection.
        _setup_pic(AS_CHARACTER, "https://example.org/aschar", "LinkedAsChar")
        html_as = document_to_content(doc, ctx, scope="full", include_images=False)
        sel = text.createTextCursor()
        sel.gotoStart(False)
        sel.gotoEnd(True)
        doc.getCurrentController().select(sel)
        res_as = ApplyDocumentContent().execute(tctx, target="selection", content=html_as)
        assert res_as.get("status") == "ok", res_as
        assert doc.getGraphicObjects().getByIndex(0).getPropertyValue("HyperLinkURL") == "https://example.org/aschar"

        # Wrapping an unlinked picture's wrapper in <a href target="_blank"> for each anchor type.
        for anchor_type, name, tag in (
            (AT_PARAGRAPH, "WrapPara", "div"),
            (AT_CHARACTER, "WrapAtChar", "div"),
            (AS_CHARACTER, "WrapAsChar", "span"),
        ):
            _setup_pic(anchor_type, "", name)
            html_plain = document_to_content(doc, ctx, scope="full", include_images=False)
            wrapped = re.sub(
                rf'(<{tag}\b[^>]*\bid="{name}"[^>]*>.*?</{tag}>)',
                r'<a href="https://example.org/wrapped" target="_blank">\1</a>',
                html_plain,
                count=1,
                flags=re.DOTALL,
            )
            assert "https://example.org/wrapped" in wrapped, html_plain
            res_w = ApplyDocumentContent().execute(tctx, target="full_document", content=wrapped)
            assert res_w.get("status") == "ok", res_w
            pic = doc.getGraphicObjects().getByIndex(0)
            assert pic.getPropertyValue("HyperLinkURL") == "https://example.org/wrapped"
            assert pic.getPropertyValue("HyperLinkTarget") == "_blank"
            body = text.getString()
            assert body == "before", (anchor_type, repr(body))
            reexp = document_to_content(doc, ctx, scope="full", include_images=False)
            assert reexp.count("<a ") == 1, reexp

        # Unlinked AS_CHARACTER picture inside a text link stays unlinked while text keeps its link.
        _setup_pic(AS_CHARACTER, "", "InsideTextLink")
        html_in = document_to_content(doc, ctx, scope="full", include_images=False)
        text_linked = re.sub(
            r'(<div\b[^>]*data-lo-style="Standard"[^>]*>)(.*?)(</div>)',
            r'\1<a href="https://example.org/text">\2</a>\3',
            html_in,
            count=1,
            flags=re.DOTALL,
        )
        assert "https://example.org/text" in text_linked, html_in
        res_tl = ApplyDocumentContent().execute(tctx, target="full_document", content=text_linked)
        assert res_tl.get("status") == "ok", res_tl
        assert doc.getGraphicObjects().getByIndex(0).getPropertyValue("HyperLinkURL") == ""
        desc = doc.createSearchDescriptor()
        desc.SearchString = "before"
        found_txt = doc.findFirst(desc)
        url_got = found_txt.getPropertyValue("HyperLinkURL")
        assert url_got == "https://example.org/text", (url_got, text_linked)

        # Recorded AS_CHARACTER wrap: only Insert/Delete redlines; Accept leaves one linked picture.
        _setup_pic(AS_CHARACTER, "", "RecWrapAsChar")
        html_rec = document_to_content(doc, ctx, scope="full", include_images=False)
        wrapped_rec = re.sub(
            r'(<span\b[^>]*\bid="RecWrapAsChar"[^>]*>.*?</span>)',
            r'<a href="https://example.org/rec" target="_blank">\1</a>',
            html_rec,
            count=1,
            flags=re.DOTALL,
        )
        doc.setPropertyValue("RecordChanges", True)
        try:
            res_rw = ApplyDocumentContent().execute(tctx, target="full_document", content=wrapped_rec)
            assert res_rw.get("status") == "ok", res_rw
            from plugin.framework.uno_context import enum_has_more, execute_dispatch

            rl_types: set[str] = set()
            rls = doc.getRedlines().createEnumeration()
            while enum_has_more(rls):
                rl_types.add(str(rls.nextElement().getPropertyValue("RedlineType")))
            assert rl_types == {"Insert", "Delete"}, rl_types
            gfx_objs = doc.getGraphicObjects()
            assert any(
                gfx_objs.getByIndex(i).getPropertyValue("HyperLinkURL") == "https://example.org/rec"
                and gfx_objs.getByIndex(i).getPropertyValue("HyperLinkTarget") == "_blank"
                for i in range(gfx_objs.getCount())
            )
            execute_dispatch(ctx, doc.getCurrentController(), ".uno:AcceptAllTrackedChanges")
            assert doc.getGraphicObjects().getCount() == 1
            pic = doc.getGraphicObjects().getByIndex(0)
            assert pic.getPropertyValue("HyperLinkURL") == "https://example.org/rec"
            assert pic.getPropertyValue("HyperLinkTarget") == "_blank"
        finally:
            doc.setPropertyValue("RecordChanges", False)
    finally:
        os.unlink(img_path)


def _body_para_strings(doc) -> list[str]:
    from plugin.doc.text_helpers import iter_elements

    return [
        el.getString()
        for el in iter_elements(doc.getText().createEnumeration())
        if el.supportsService("com.sun.star.text.Paragraph")
    ]


@native_test
@with_native_doc("writer", reuse=False)
def test_full_document_refuses_protected_middle_section_uno(ctx, doc):
    from plugin.doc.text_helpers import iter_elements

    tctx = native_writer_tool_context(doc, ctx)
    tool = ApplyDocumentContent()
    text = doc.getText()
    text.setString("")
    cur = text.createTextCursor()
    for idx, word in enumerate(("Head", "Middle", "Tail")):
        if idx:
            text.insertControlCharacter(cur, 0, False)
        text.insertString(cur, word, False)
    els = list(iter_elements(text.createEnumeration()))
    sec_cur = text.createTextCursorByRange(els[1].getStart())
    sec_cur.gotoRange(els[1].getEnd(), True)
    sec = doc.createInstance("com.sun.star.text.TextSection")
    sec.setName("ProtMid")
    sec.setPropertyValue("IsProtected", True)
    text.insertTextContent(sec_cur, sec, True)
    els = list(iter_elements(text.createEnumeration()))
    assert els[-1].getPropertyValue("TextSection") is None
    before_body = text.getString()

    for rec in (False, True):
        doc.setPropertyValue("RecordChanges", rec)
        res = tool.execute(tctx, target="full_document", content="<p>Replaced</p>")
        assert res.get("status") == "error", res
        assert res.get("code") == "PROTECTED_SECTION", res
        assert "ProtMid" in res.get("message", "")
        assert text.getString() == before_body
    doc.setPropertyValue("RecordChanges", False)


@native_test
@with_native_doc("writer", reuse=False)
def test_full_document_refuses_protected_table_only_leading_section_uno(ctx, doc):
    from plugin.doc.text_helpers import iter_elements

    tctx = native_writer_tool_context(doc, ctx)
    tool = ApplyDocumentContent()
    text = doc.getText()
    text.setString("")
    text.insertString(text.getEnd(), "cover", False)
    text.insertControlCharacter(text.getEnd(), 0, False)
    text.insertString(text.getEnd(), "body", False)
    els = list(iter_elements(text.createEnumeration()))
    sec = doc.createInstance("com.sun.star.text.TextSection")
    sec.setName("ProtTblSec")
    scur = text.createTextCursorByRange(els[0].getStart())
    scur.gotoRange(els[0].getEnd(), True)
    text.insertTextContent(scur, sec, True)
    tbl = doc.createInstance("com.sun.star.text.TextTable")
    tbl.initialize(1, 1)
    text.insertTextContent(text.getStart(), tbl, False)
    tbl.getCellByName("A1").setString("Cell")
    sec.setPropertyValue("IsProtected", True)
    list(iter_elements(text.createEnumeration()))[1].dispose()

    els = list(iter_elements(text.createEnumeration()))
    assert els[0].supportsService("com.sun.star.text.TextTable")
    assert els[0].getPropertyValue("TextSection") is not None
    assert els[1].getPropertyValue("TextSection") is None

    for rec in (False, True):
        doc.setPropertyValue("RecordChanges", rec)
        res = tool.execute(tctx, target="full_document", content="<p>Replaced</p>")
        assert res.get("status") == "error", res
        assert res.get("code") == "PROTECTED_SECTION", res
        assert "ProtTblSec" in res.get("message", "")
        assert tbl.getCellByName("A1").getString() == "Cell"
    doc.setPropertyValue("RecordChanges", False)


@native_test
@with_native_doc("writer", reuse=False)
def test_full_document_refuses_protected_section_in_table_cell_uno(ctx, doc):
    tctx = native_writer_tool_context(doc, ctx)
    tool = ApplyDocumentContent()
    text = doc.getText()
    text.setString("")
    tbl = doc.createInstance("com.sun.star.text.TextTable")
    tbl.initialize(1, 1)
    text.insertTextContent(text.getStart(), tbl, False)
    cell = tbl.getCellByName("A1")
    cell.setString("InCell")
    ccur = cell.createTextCursor()
    ccur.gotoStart(False)
    ccur.gotoEnd(True)
    sec = doc.createInstance("com.sun.star.text.TextSection")
    sec.setName("ProtCellSec")
    sec.setPropertyValue("IsProtected", True)
    cell.insertTextContent(ccur, sec, True)

    for rec in (False, True):
        doc.setPropertyValue("RecordChanges", rec)
        res = tool.execute(tctx, target="full_document", content="<p>Replaced</p>")
        assert res.get("status") == "error", res
        assert res.get("code") == "PROTECTED_SECTION", res
        assert "ProtCellSec" in res.get("message", "")
        assert cell.getString() == "InCell"
    doc.setPropertyValue("RecordChanges", False)


@native_test
@with_native_doc("writer", reuse=False)
def test_full_document_allows_protected_header_section_uno(ctx, doc):
    tctx = native_writer_tool_context(doc, ctx)
    tool = ApplyDocumentContent()
    style = doc.getStyleFamilies().getByName("PageStyles").getByName("Standard")
    style.setPropertyValue("HeaderIsOn", True)
    htext = style.getPropertyValue("HeaderText")
    htext.setString("HeaderText")
    hcur = htext.createTextCursor()
    hcur.gotoStart(False)
    hcur.gotoEnd(True)
    sec = doc.createInstance("com.sun.star.text.TextSection")
    sec.setName("ProtHeaderSec")
    sec.setPropertyValue("IsProtected", True)
    htext.insertTextContent(hcur, sec, True)

    doc.getText().setString("BodyOld")
    res = tool.execute(tctx, target="full_document", content="<p>BodyNew</p>")
    assert res.get("status") == "ok", res
    assert doc.getText().getString().strip() == "BodyNew"
    assert doc.getTextSections().hasByName("ProtHeaderSec")


@native_test
@with_native_doc("writer", reuse=False)
def test_full_document_allows_hidden_footer_section_uno(ctx, doc):
    tctx = native_writer_tool_context(doc, ctx)
    tool = ApplyDocumentContent()
    style = doc.getStyleFamilies().getByName("PageStyles").getByName("Standard")
    style.setPropertyValue("FooterIsOn", True)
    ftext = style.getPropertyValue("FooterText")
    ftext.setString("FooterText")
    fcur = ftext.createTextCursor()
    fcur.gotoStart(False)
    fcur.gotoEnd(True)
    sec = doc.createInstance("com.sun.star.text.TextSection")
    sec.setName("HidFooterSec")
    sec.setPropertyValue("IsVisible", False)
    ftext.insertTextContent(fcur, sec, True)

    doc.getText().setString("BodyOld")
    res = tool.execute(tctx, target="full_document", content="<p>BodyNew</p>")
    assert res.get("status") == "ok", res
    assert doc.getText().getString().strip() == "BodyNew"
    assert doc.getTextSections().hasByName("HidFooterSec")


@native_test
@with_native_doc("writer", reuse=False)
def test_full_document_allows_sections_in_header_footer_containers_uno(ctx, doc):
    tctx = native_writer_tool_context(doc, ctx)
    tool = ApplyDocumentContent()
    style = doc.getStyleFamilies().getByName("PageStyles").getByName("Standard")
    style.setPropertyValue("HeaderIsOn", True)
    style.setPropertyValue("FooterIsOn", True)
    htext = style.getPropertyValue("HeaderText")
    ftext = style.getPropertyValue("FooterText")

    def _add_section(target_text, name: str, content: str, *, protected: bool = True, visible: bool = True) -> None:
        target_text.setString(content)
        cur = target_text.createTextCursor()
        cur.gotoStart(False)
        cur.gotoEnd(True)
        sec = doc.createInstance("com.sun.star.text.TextSection")
        sec.setName(name)
        sec.setPropertyValue("IsProtected", protected)
        sec.setPropertyValue("IsVisible", visible)
        target_text.insertTextContent(cur, sec, True)

    # Header table cell with protected section.
    htbl1 = doc.createInstance("com.sun.star.text.TextTable")
    htbl1.initialize(1, 1)
    htext.insertTextContent(htext.getEnd(), htbl1, False)
    hcell1 = htbl1.getCellByName("A1")
    _add_section(hcell1, "HCellProtSec", "HeaderCellProt")

    # Second header table cell with hidden section.
    htbl2 = doc.createInstance("com.sun.star.text.TextTable")
    htbl2.initialize(1, 1)
    htext.insertTextContent(htext.getEnd(), htbl2, False)
    _add_section(htbl2.getCellByName("A1"), "HCellHidSec", "HeaderCellHid", protected=False, visible=False)

    # Header text frame with protected section.
    hframe = doc.createInstance("com.sun.star.text.TextFrame")
    htext.insertTextContent(htext.getEnd(), hframe, False)
    _add_section(hframe.getText(), "HFrameProtSec", "HeaderFrameProt")

    # Footer table cell hosting a nested 1x1 table whose cell holds a protected section.
    ftbl = doc.createInstance("com.sun.star.text.TextTable")
    ftbl.initialize(1, 1)
    ftext.insertTextContent(ftext.getEnd(), ftbl, False)
    fcell = ftbl.getCellByName("A1")
    fnest = doc.createInstance("com.sun.star.text.TextTable")
    fnest.initialize(1, 1)
    fcell.insertTextContent(fcell.getEnd(), fnest, False)
    fncell = fnest.getCellByName("A1")
    _add_section(fncell, "FNestProtSec", "FooterNestProt")

    try:
        for rec in (False, True):
            doc.getText().setString("BodyOld")
            doc.setPropertyValue("RecordChanges", rec)
            res = tool.execute(tctx, target="full_document", content="<p>BodyNew</p>")
            assert res.get("status") == "ok", res
            assert _body_para_strings(doc)[0] == "BodyNew"
            sections = doc.getTextSections()
            for name in ("HCellProtSec", "HCellHidSec", "HFrameProtSec", "FNestProtSec"):
                assert sections.hasByName(name), name
            assert hcell1.getString() == "HeaderCellProt"
            assert fncell.getString() == "FooterNestProt"
            assert hframe.getText().getString() == "HeaderFrameProt"
    finally:
        doc.setPropertyValue("RecordChanges", False)


@native_test
@with_native_doc("writer", reuse=False)
def test_full_document_refuses_protected_section_in_body_frame_uno(ctx, doc):
    from com.sun.star.text.TextContentAnchorType import AT_PAGE

    tctx = native_writer_tool_context(doc, ctx)
    tool = ApplyDocumentContent()
    text = doc.getText()

    def _setup_frame(*, at_page: bool):
        _clear_body(doc)
        text.setString("BodyOld")
        frame = doc.createInstance("com.sun.star.text.TextFrame")
        if at_page:
            frame.setPropertyValue("AnchorType", AT_PAGE)
            frame.setPropertyValue("AnchorPageNo", 1)
        text.insertTextContent(text.getEnd(), frame, False)
        ftext = frame.getText()
        ftext.setString("FrameProt")
        fcur = ftext.createTextCursor()
        fcur.gotoStart(False)
        fcur.gotoEnd(True)
        sec = doc.createInstance("com.sun.star.text.TextSection")
        sec.setName("BodyFrameProtSec")
        sec.setPropertyValue("IsProtected", True)
        ftext.insertTextContent(fcur, sec, True)
        return frame

    for at_page in (False, True):
        frame = _setup_frame(at_page=at_page)
        assert (frame.getPropertyValue("ParentText") is None) == at_page
        assert doc.getTextFrames().getCount() == 1
        res = tool.execute(tctx, target="full_document", content="<p>BodyNew</p>")
        assert res.get("status") == "error", res
        assert res.get("code") == "PROTECTED_SECTION", res
        assert "BodyFrameProtSec" in res.get("message", "")
        assert frame.getText().getString() == "FrameProt"


@native_test
@with_native_doc("writer", reuse=False)
def test_full_document_recorded_leading_section_page_style_uno(ctx, doc):
    from plugin.framework.uno_context import execute_dispatch

    tctx = native_writer_tool_context(doc, ctx)
    tool = ApplyDocumentContent()
    text = doc.getText()

    def _setup_cover() -> None:
        _clear_body(doc)
        sec = doc.createInstance("com.sun.star.text.TextSection")
        sec.setName("Cover")
        text.insertTextContent(text.getStart(), sec, False)
        cur = text.createTextCursor()
        cur.gotoStart(False)
        text.insertString(cur, "CoverText", False)
        cur.gotoStart(False)
        cur.setPropertyValue("PageDescName", "First Page")
        cur.setPropertyValue("PageNumberOffset", 7)

    # Accept keeps page style and offset and removes the section.
    _setup_cover()
    doc.setPropertyValue("RecordChanges", True)
    try:
        res = tool.execute(tctx, target="full_document", content="<p>N1</p>")
        assert res.get("status") == "ok", res
        execute_dispatch(ctx, doc.getCurrentController(), ".uno:AcceptAllTrackedChanges")
        assert _body_para_strings(doc)[0] == "N1"
        c = text.createTextCursor()
        c.gotoStart(False)
        assert c.getPropertyValue("PageDescName") == "First Page"
        assert c.getPropertyValue("PageNumberOffset") == 7
        assert doc.getTextSections().getCount() == 0
    finally:
        doc.setPropertyValue("RecordChanges", False)

    # Reject restores old paragraphs with one leading empty paragraph.
    _setup_cover()
    doc.setPropertyValue("RecordChanges", True)
    try:
        res2 = tool.execute(tctx, target="full_document", content="<p>N1</p>")
        assert res2.get("status") == "ok", res2
        execute_dispatch(ctx, doc.getCurrentController(), ".uno:RejectAllTrackedChanges")
        assert _body_para_strings(doc) == ["", "CoverText", ""]
    finally:
        doc.setPropertyValue("RecordChanges", False)


@native_test
@with_native_doc("writer", reuse=False)
def test_full_document_recorded_leading_toc_accept_removes_index_uno(ctx, doc):
    from plugin.framework.uno_context import execute_dispatch

    tctx = native_writer_tool_context(doc, ctx)
    tool = ApplyDocumentContent()
    text = doc.getText()
    _clear_body(doc)
    toc = doc.createInstance("com.sun.star.text.ContentIndex")
    text.insertTextContent(text.getStart(), toc, False)

    doc.setPropertyValue("RecordChanges", True)
    try:
        res = tool.execute(tctx, target="full_document", content="<p>N1</p><p>N2</p>")
        assert res.get("status") == "ok", res
        execute_dispatch(ctx, doc.getCurrentController(), ".uno:AcceptAllTrackedChanges")
        assert doc.getDocumentIndexes().getCount() == 0
        assert _body_para_strings(doc) == ["N1", "N2", ""]
    finally:
        doc.setPropertyValue("RecordChanges", False)


@native_test
@with_native_doc("writer", reuse=False)
def test_full_document_recorded_trailing_section_is_struck_uno(ctx, doc):
    from plugin.framework.uno_context import execute_dispatch

    tctx = native_writer_tool_context(doc, ctx)
    tool = ApplyDocumentContent()
    text = doc.getText()

    def _setup_trailing() -> None:
        _clear_body(doc)
        cur = text.createTextCursor()
        text.insertString(cur, "head", False)
        text.insertControlCharacter(cur, 0, False)
        text.insertString(cur, "body", False)
        text.insertControlCharacter(cur, 0, False)
        tail_start = cur.getStart()
        text.insertString(cur, "tail", False)
        scur = text.createTextCursorByRange(tail_start)
        scur.gotoRange(cur.getEnd(), True)
        sec = doc.createInstance("com.sun.star.text.TextSection")
        sec.setName("TailSec")
        text.insertTextContent(scur, sec, True)

    _setup_trailing()
    doc.setPropertyValue("RecordChanges", True)
    try:
        res = tool.execute(tctx, target="full_document", content="<p>N1</p>")
        assert res.get("status") == "ok", res
        doc.getUndoManager().undo()
        assert _body_para_strings(doc) == ["head", "body", "tail"]
        assert doc.getTextSections().getCount() == 1

        res2 = tool.execute(tctx, target="full_document", content="<p>N1</p>")
        assert res2.get("status") == "ok", res2
        execute_dispatch(ctx, doc.getCurrentController(), ".uno:RejectAllTrackedChanges")
        assert _body_para_strings(doc) == ["head", "body", "tail", ""]
        assert doc.getTextSections().getCount() == 1
    finally:
        doc.setPropertyValue("RecordChanges", False)

    _setup_trailing()
    doc.setPropertyValue("RecordChanges", True)
    try:
        res3 = tool.execute(tctx, target="full_document", content="<p>N1</p>")
        assert res3.get("status") == "ok", res3
        execute_dispatch(ctx, doc.getCurrentController(), ".uno:AcceptAllTrackedChanges")
        assert _body_para_strings(doc) == ["N1", ""]
        assert doc.getTextSections().getCount() == 0
    finally:
        doc.setPropertyValue("RecordChanges", False)

    # Track Changes off also removes the trailing section.
    _setup_trailing()
    res4 = tool.execute(tctx, target="full_document", content="<p>Off</p>")
    assert res4.get("status") == "ok", res4
    assert doc.getTextSections().getCount() == 0
    assert _body_para_strings(doc) == ["Off"]


@native_test
@with_native_doc("writer", reuse=False)
def test_full_document_recorded_table_only_leading_section_uno(ctx, doc):
    from plugin.doc.text_helpers import iter_elements
    from plugin.framework.uno_context import execute_dispatch

    tctx = native_writer_tool_context(doc, ctx)
    tool = ApplyDocumentContent()
    text = doc.getText()

    def _setup_tbl_sec() -> None:
        _clear_body(doc)
        text.insertString(text.getEnd(), "cover", False)
        text.insertControlCharacter(text.getEnd(), 0, False)
        text.insertString(text.getEnd(), "body", False)
        els = list(iter_elements(text.createEnumeration()))
        sec = doc.createInstance("com.sun.star.text.TextSection")
        sec.setName("LeadTblSec")
        scur = text.createTextCursorByRange(els[0].getStart())
        scur.gotoRange(els[0].getEnd(), True)
        text.insertTextContent(scur, sec, True)
        tbl = doc.createInstance("com.sun.star.text.TextTable")
        tbl.initialize(1, 1)
        text.insertTextContent(text.getStart(), tbl, False)
        tbl.getCellByName("A1").setString("Cell")
        list(iter_elements(text.createEnumeration()))[1].dispose()

        els = list(iter_elements(text.createEnumeration()))
        assert els[0].supportsService("com.sun.star.text.TextTable")
        assert els[0].getPropertyValue("TextSection") is not None
        assert els[1].getPropertyValue("TextSection") is None

    # Recorded Accept leaves no table and no section.
    _setup_tbl_sec()
    doc.setPropertyValue("RecordChanges", True)
    try:
        res = tool.execute(tctx, target="full_document", content="<p>New</p>")
        assert res.get("status") == "ok", res
        execute_dispatch(ctx, doc.getCurrentController(), ".uno:AcceptAllTrackedChanges")
        assert doc.getTextTables().getCount() == 0
        assert doc.getTextSections().getCount() == 0
        assert "New" in text.getString()
    finally:
        doc.setPropertyValue("RecordChanges", False)

    # Recorded Reject restores the table with its cell text and the section.
    _setup_tbl_sec()
    doc.setPropertyValue("RecordChanges", True)
    try:
        res2 = tool.execute(tctx, target="full_document", content="<p>New</p>")
        assert res2.get("status") == "ok", res2
        execute_dispatch(ctx, doc.getCurrentController(), ".uno:RejectAllTrackedChanges")
        assert doc.getTextTables().getCount() == 1
        assert doc.getTextSections().getCount() == 1
        assert doc.getTextTables().getByIndex(0).getCellByName("A1").getString() == "Cell"
    finally:
        doc.setPropertyValue("RecordChanges", False)

    # Track Changes off: result is only the new text, and Undo restores table and section.
    _setup_tbl_sec()
    res3 = tool.execute(tctx, target="full_document", content="<p>Off</p>")
    assert res3.get("status") == "ok", res3
    assert doc.getTextTables().getCount() == 0
    assert doc.getTextSections().getCount() == 0
    assert _body_para_strings(doc) == ["Off"]
    doc.getUndoManager().undo()
    assert doc.getTextTables().getCount() == 1
    assert doc.getTextSections().getCount() == 1
    assert doc.getTextTables().getByIndex(0).getCellByName("A1").getString() == "Cell"


@native_test
@with_native_doc("writer")
def test_outline_picture_link_round_trips_full_document_uno(ctx, doc):
    import base64
    import os
    import tempfile
    import uno
    from com.sun.star.text.TextContentAnchorType import AS_CHARACTER
    from plugin.writer.html_export import document_to_content

    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
    )
    fd, img_path = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    with open(img_path, "wb") as f:
        f.write(png)
    try:
        text = doc.getText()
        text.setString("SecHead\n")
        cur = text.createTextCursor()
        cur.gotoStart(False)
        cur.setPropertyValue("ParaStyleName", "Heading 1")
        g = doc.createInstance("com.sun.star.text.TextGraphicObject")
        g.setPropertyValue("GraphicURL", uno.systemPathToFileUrl(img_path))
        g.setPropertyValue("AnchorType", AS_CHARACTER)
        g.setPropertyValue("Width", 400)
        g.setPropertyValue("Height", 400)
        text.insertTextContent(text.getEnd(), g, False)
        g.setName("OutlinePic")
        g.setPropertyValue("HyperLinkURL", "#1.SecHead|outline")

        html = document_to_content(doc, ctx, scope="full", include_images=False)
        assert "#1.SecHead|outline" in html
        res = ApplyDocumentContent().execute(native_writer_tool_context(doc, ctx), target="full_document", content=html)
        assert res.get("status") == "ok", res
        assert doc.getGraphicObjects().getByIndex(0).getPropertyValue("HyperLinkURL") == "#1.SecHead|outline"
    finally:
        os.unlink(img_path)


@native_test
@with_native_doc("writer")
def test_linked_picture_inside_text_link_keeps_trailing_link_uno(ctx, doc):
    import base64
    import os
    import tempfile
    import uno
    from com.sun.star.text.TextContentAnchorType import AS_CHARACTER, AT_CHARACTER

    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
    )
    fd, img_path = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    with open(img_path, "wb") as f:
        f.write(png)
    try:
        for anchor_type in (AS_CHARACTER, AT_CHARACTER):
            _clear_body(doc)
            text = doc.getText()
            g = doc.createInstance("com.sun.star.text.TextGraphicObject")
            g.setPropertyValue("GraphicURL", uno.systemPathToFileUrl(img_path))
            g.setPropertyValue("AnchorType", anchor_type)
            g.setPropertyValue("Width", 400)
            g.setPropertyValue("Height", 400)
            text.insertTextContent(text.getStart(), g, False)
            g.setName("NestedPic")
            g.setPropertyValue("HyperLinkURL", "https://pic/l")

            html = (
                '<p data-lo-style="Standard"><a href="https://text/l">before '
                '<a href="https://pic/l"><span id="NestedPic"><img src=""/></span></a> after</a></p>'
            )
            res = ApplyDocumentContent().execute(native_writer_tool_context(doc, ctx), target="full_document", content=html)
            assert res.get("status") == "ok", res
            desc = doc.createSearchDescriptor()
            desc.SearchString = "after"
            found = doc.findFirst(desc)
            assert found is not None
            assert found.getPropertyValue("HyperLinkURL") == "https://text/l", anchor_type
            assert doc.getGraphicObjects().getByIndex(0).getPropertyValue("HyperLinkURL") == "https://pic/l"
    finally:
        os.unlink(img_path)


@native_test
@with_native_doc("writer")
def test_recorded_full_document_at_paragraph_surviving_picture_link_uno(ctx, doc):
    import base64
    import os
    import tempfile
    import uno
    from com.sun.star.text.TextContentAnchorType import AT_PARAGRAPH
    from plugin.writer.html_export import document_to_content

    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
    )
    fd, img_path = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    with open(img_path, "wb") as f:
        f.write(png)
    try:
        text = doc.getText()
        text.setString("only para")
        g = doc.createInstance("com.sun.star.text.TextGraphicObject")
        g.setPropertyValue("GraphicURL", uno.systemPathToFileUrl(img_path))
        g.setPropertyValue("AnchorType", AT_PARAGRAPH)
        g.setPropertyValue("Width", 400)
        g.setPropertyValue("Height", 400)
        text.insertTextContent(text.getEnd(), g, False)
        g.setName("SurvPic")
        g.setPropertyValue("HyperLinkURL", "https://pic/old")

        html = document_to_content(doc, ctx, scope="full", include_images=False).replace(
            "https://pic/old", "https://pic/new"
        )
        doc.setPropertyValue("RecordChanges", True)
        try:
            res = ApplyDocumentContent().execute(native_writer_tool_context(doc, ctx), target="full_document", content=html)
            assert res.get("status") == "ok", res
            assert doc.getGraphicObjects().getByName("SurvPic").getPropertyValue("HyperLinkURL") == "https://pic/new"
            doc.getUndoManager().undo()
            assert doc.getGraphicObjects().getByName("SurvPic").getPropertyValue("HyperLinkURL") == "https://pic/old"
        finally:
            doc.setPropertyValue("RecordChanges", False)
    finally:
        os.unlink(img_path)


@native_test
@with_native_doc("writer")
def test_full_document_write_of_dataless_ole_refused_uno(ctx, doc):
    from plugin.writer.html_export import document_to_content

    text = doc.getText()
    ole = doc.createInstance("com.sun.star.text.TextEmbeddedObject")
    ole.setPropertyValue("CLSID", "12DCAE26-281F-416F-a234-c3086127382e")
    text.insertTextContent(text.getEnd(), ole, False)

    html = document_to_content(doc, ctx, scope="full", include_images=False)
    res = ApplyDocumentContent().execute(native_writer_tool_context(doc, ctx), target="full_document", content=html)
    assert res.get("status") == "error", res
    assert "embedded object" in res.get("message", ""), res
    assert doc.getEmbeddedObjects().getCount() == 1


@native_test
@with_native_doc("writer")
def test_record_mode_pending_review_matches_redlines_uno(ctx, doc):
    from plugin.framework.config import get_config, save

    tctx = native_writer_tool_context(doc, ctx)
    tool = ApplyDocumentContent()
    text = doc.getText()
    flag = "doc.agent_edit_review_mode"
    prev = get_config(flag)
    try:
        save({flag: "record"})

        # (a) Replacement equal to match leaves zero redlines and no pending_review flag.
        _clear_body(doc)
        text.setString("hello MATCH world")
        res_noop = tool.execute(tctx, target="search", old_content="MATCH", content="MATCH")
        assert res_noop["status"] == "ok", res_noop
        assert "pending_review" not in res_noop
        assert doc.getRedlines().getCount() == 0
        assert text.getString() == "hello MATCH world"

        # (b) Real replacement leaves a redline and pending_review=True.
        res_edit = tool.execute(tctx, target="search", old_content="MATCH", content="OTHER")
        assert res_edit["status"] == "ok", res_edit
        assert res_edit["pending_review"] is True
        assert doc.getRedlines().getCount() > 0

        # (c) Delete stacked on a user-recorded insertion stays untagged but still reports pending_review.
        _clear_body(doc)
        text.setString("hello world")
        doc.setPropertyValue("RecordChanges", True)
        try:
            cur = text.createTextCursor()
            cur.gotoStart(False)
            cur.goRight(5, False)
            text.insertString(cur, " USERINS", False)
        finally:
            doc.setPropertyValue("RecordChanges", False)
        res_stack = tool.execute(tctx, target="search", old_content=" USERINS", content="")
        assert res_stack["status"] == "ok", res_stack
        assert res_stack["pending_review"] is True
        redlines = doc.getRedlines()
        deletes = [
            redlines.getByIndex(i)
            for i in range(redlines.getCount())
            if redlines.getByIndex(i).RedlineType == "Delete"
        ]
        assert deletes
        assert all(not str(rl.RedlineComment or "").startswith("wa-review:") for rl in deletes)
    finally:
        save({flag: prev})
        doc.setPropertyValue("RecordChanges", False)


@native_test
@with_native_doc("writer", reuse=False)
def test_markup_in_control_and_input_field_refused_uno(ctx, doc):
    from plugin.writer.apply_document_content import ApplyDocumentContent

    tctx = native_writer_tool_context(doc, ctx)
    tool = ApplyDocumentContent()
    text = doc.getText()

    # ContentControl: <b>NEW</b> via search and selection raises MARKUP_IN_CONTROL; plain text works.
    _clear_body(doc)
    text.setString("xx CTRL yy")
    cur = text.createTextCursor()
    cur.gotoStart(False)
    cur.goRight(3, False)
    cur.goRight(4, True)
    text.insertTextContent(cur, doc.createInstance("com.sun.star.text.ContentControl"), True)

    res_search = tool.execute_safe(tctx, target="search", old_content="CTRL", content="<b>NEW</b>")
    assert res_search["status"] == "error" and res_search["code"] == "MARKUP_IN_CONTROL", res_search
    assert text.getString() == "xx CTRL yy"

    sel_cur = text.createTextCursor()
    sel_cur.gotoStart(False)
    sel_cur.goRight(4, False)  # past "xx " + control start mark
    sel_cur.goRight(4, True)
    doc.getCurrentController().select(sel_cur)
    res_sel = tool.execute_safe(tctx, target="selection", content="<b>NEW</b>")
    assert res_sel["status"] == "error" and res_sel["code"] == "MARKUP_IN_CONTROL", res_sel
    assert text.getString() == "xx CTRL yy"

    res_plain = tool.execute_safe(tctx, target="search", old_content="CTRL", content="PLAIN")
    assert res_plain["status"] == "ok", res_plain
    assert text.getString() == "xx PLAIN yy"

    # Input field: <b>NEW</b> via search and selection raises MARKUP_IN_CONTROL; text unchanged.
    _clear_body(doc)
    cur = text.createTextCursor()
    text.insertString(cur, "xx ", False)
    fld = doc.createInstance("com.sun.star.text.TextField.Input")
    fld.setPropertyValue("Content", "CTRL")
    text.insertTextContent(cur, fld, False)
    text.insertString(cur, " yy", False)

    res_inp_search = tool.execute_safe(tctx, target="search", old_content="CTRL", content="<b>NEW</b>")
    assert res_inp_search["status"] == "error" and res_inp_search["code"] == "MARKUP_IN_CONTROL", res_inp_search
    assert text.getString() == "xx CTRL yy"

    sd = doc.createSearchDescriptor()
    sd.SearchString = "TR"
    inner_hit = doc.findFirst(sd)
    assert inner_hit is not None
    doc.getCurrentController().select(inner_hit)
    res_inp_sel = tool.execute_safe(tctx, target="selection", content="<b>NEW</b>")
    assert res_inp_sel["status"] == "error" and res_inp_sel["code"] == "MARKUP_IN_CONTROL", res_inp_sel
    assert text.getString() == "xx CTRL yy"


@native_test
def test_full_document_refuses_shown_conditional_section_uno(ctx):
    from plugin.tests.testing_utils import TestingFactory
    from plugin.writer.apply_document_content import ApplyDocumentContent

    tool = ApplyDocumentContent()

    for leading in (True, False):
        for record in (False, True):
            with TestingFactory.native_doc(ctx, doc_type="writer", reuse=False) as case_doc:
                tctx = native_writer_tool_context(case_doc, ctx)
                text = case_doc.getText()
                if leading:
                    text.setString("CondBody\rAfter")
                    cur = text.createTextCursor()
                    cur.gotoStart(False)
                    cur.goRight(len("CondBody"), True)
                else:
                    text.setString("Before\rCondBody\rAfter")
                    cur = text.createTextCursor()
                    cur.gotoStart(False)
                    cur.goRight(len("Before\r"), False)
                    cur.goRight(len("CondBody"), True)
                sec = case_doc.createInstance("com.sun.star.text.TextSection")
                sec.setName("ShownCondSec")
                sec.setPropertyValue("IsVisible", False)
                sec.setPropertyValue("Condition", "1 == 2")
                text.insertTextContent(cur, sec, True)

                before_text = text.getString()
                case_doc.setPropertyValue("RecordChanges", record)
                try:
                    res = tool.execute_safe(tctx, target="full_document", content="<p>Replaced</p>")
                finally:
                    case_doc.setPropertyValue("RecordChanges", False)
                assert res["status"] == "error" and res["code"] == "HIDDEN_SECTION", (leading, record, res)
                assert text.getString() == before_text
                kept = case_doc.getTextSections().getByName("ShownCondSec")
                assert kept.getPropertyValue("IsVisible") is False
                assert kept.getPropertyValue("Condition") == "1 == 2"


@native_test
def test_apply_content_refuses_crossing_deletion_and_handles_all_matches_uno(ctx):
    from plugin.doc.text_helpers import visible_range_text
    from plugin.tests.testing_utils import TestingFactory
    from plugin.writer.apply_document_content import ApplyDocumentContent

    tool = ApplyDocumentContent()

    def _setup_crossing(case_doc, content_str: str):
        text = case_doc.getText()
        text.setString(content_str)
        cur = text.createTextCursor()
        cur.gotoStart(False)
        cur.goRight(len("Word "), False)
        cur.goRight(len("here "), True)
        case_doc.setPropertyValue("RecordChanges", True)
        try:
            cur.setString("")
        finally:
            case_doc.setPropertyValue("RecordChanges", False)
        case_doc.getRedlines().getByIndex(0).setPropertyValue("RedlineComment", "keep-me")

    for record in (False, True):
        with TestingFactory.native_doc(ctx, doc_type="writer", reuse=False) as case_doc:
            _setup_crossing(case_doc, "Word here there")
            orig_author = case_doc.getRedlines().getByIndex(0).getPropertyValue("RedlineAuthor")
            tctx = native_writer_tool_context(case_doc, ctx)
            case_doc.setPropertyValue("RecordChanges", record)
            try:
                res = tool.execute_safe(tctx, target="search", old_content="Word there", content="Word here")
            finally:
                case_doc.setPropertyValue("RecordChanges", False)
            assert res["status"] == "error" and res["code"] == "MATCH_CROSSES_DELETION", (record, res)
            assert visible_range_text(case_doc, case_doc.getText()) == "Word there"
            assert case_doc.getRedlines().getCount() == 1
            assert case_doc.getRedlines().getByIndex(0).getPropertyValue("RedlineAuthor") == orig_author
            assert case_doc.getRedlines().getByIndex(0).getPropertyValue("RedlineComment") == "keep-me"

    with TestingFactory.native_doc(ctx, doc_type="writer", reuse=False) as case_doc:
        # Place 20 clean matches before one crossing match so occurrence=20 sits outside replaceable.
        t = case_doc.getText()
        t.setString(("Word there " * 20) + "Word here there")
        c = t.createTextCursor()
        c.gotoStart(False)
        c.goRight(len("Word there " * 20) + len("Word "), False)
        c.goRight(len("here "), True)
        case_doc.setPropertyValue("RecordChanges", True)
        try:
            c.setString("")
        finally:
            case_doc.setPropertyValue("RecordChanges", False)
        tctx = native_writer_tool_context(case_doc, ctx)
        dry_past = tool.execute_safe(
            tctx, target="search", old_content="Word there", content="Clean", dry_run=True, occurrence=20
        )
        assert dry_past["status"] == "ok", dry_past
        assert dry_past["selected_match"].get("edit_refused") == "MATCH_CROSSES_DELETION", dry_past["selected_match"]

    with TestingFactory.native_doc(ctx, doc_type="writer", reuse=False) as case_doc:
        _setup_crossing(case_doc, "Word here there and Word there")
        tctx = native_writer_tool_context(case_doc, ctx)

        dry = tool.execute_safe(
            tctx, target="search", old_content="Word there", content="Clean", dry_run=True, occurrence=0
        )
        assert dry["status"] == "ok", dry
        assert dry["replaceable"][0].get("edit_refused") == "MATCH_CROSSES_DELETION", dry["replaceable"]
        assert "edit_refused" not in dry["replaceable"][1], dry["replaceable"]
        assert dry["selected_match"].get("edit_refused") == "MATCH_CROSSES_DELETION", dry["selected_match"]

        res_all = tool.execute_safe(tctx, target="search", old_content="Word there", content="Clean", all_matches=True)
        assert res_all["status"] == "ok", res_all
        assert res_all["replaced_count"] == 1, res_all
        assert "1 more match(es) run across a pending tracked deletion" in res_all["message"], res_all
        assert visible_range_text(case_doc, case_doc.getText()) == "Word there and Clean"

        res_after = tool.execute_safe(
            tctx, target="search", old_content="Word there", content="!", position="after"
        )
        assert res_after["status"] == "ok", res_after
        assert visible_range_text(case_doc, case_doc.getText()) == "Word there! and Clean"

    with TestingFactory.native_doc(ctx, doc_type="writer", reuse=False) as case_doc:
        _setup_crossing(case_doc, "Word here there and Word there")
        tctx = native_writer_tool_context(case_doc, ctx)
        res_occ = tool.execute_safe(
            tctx, target="search", old_content="Word there", content="Second", occurrence=1
        )
        assert res_occ["status"] == "ok", res_occ
        assert visible_range_text(case_doc, case_doc.getText()) == "Word there and Second"
