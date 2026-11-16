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
    _seed_three_simple_paras(doc)
    doc.setPropertyValue("RecordChanges", True)
    try:
        res = ApplyDocumentContent().execute(
            tctx,
            target="search",
            old_content="Replace me",
            position="after",
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
        assert set(rl_types) == {"Insert"}, rl_types
    finally:
        doc.setPropertyValue("RecordChanges", False)

