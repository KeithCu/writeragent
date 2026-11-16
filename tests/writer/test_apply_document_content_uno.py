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

