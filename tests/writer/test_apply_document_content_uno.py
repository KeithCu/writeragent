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
    tctx.selection_pin = SelectionPin(selection_cursor_snapshot(doc))
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

