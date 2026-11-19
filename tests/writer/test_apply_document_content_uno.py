# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Native UNO integration tests for view-selection preservation in apply_document_content."""

import uno  # noqa: F401

from plugin.testing_runner import native_test
from plugin.tests.testing_utils import native_writer_tool_context, with_native_doc
from plugin.writer.apply_document_content import ApplyDocumentContent


def _reset_three_paras(doc):
    text = doc.getText()
    doc.setPropertyValue("RecordChanges", False)
    cur = text.createTextCursor()
    cur.gotoStart(False)
    cur.gotoEnd(True)
    cur.setString("")
    cur.gotoStart(False)
    text.insertString(cur, "Intro paragraph here.", False)
    text.insertControlCharacter(cur, 0, False)
    text.insertString(cur, "Alpha one. Beta two.", False)
    text.insertControlCharacter(cur, 0, False)
    text.insertString(cur, "Outro paragraph.", False)


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

