# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Native UNO integration tests for set_selection updating the turn's SelectionPin."""

import uno  # noqa: F401

from plugin.doc.text_helpers import selection_cursor_snapshot
from plugin.framework.tool import SelectionPin
from plugin.testing_runner import native_test
from plugin.tests.testing_utils import native_writer_tool_context, with_native_doc
from plugin.writer.apply_document_content import ApplyDocumentContent
from plugin.writer.selection import SetSelection


@native_test
@with_native_doc("writer")
def test_set_selection_updates_pin_for_later_apply_content_uno(ctx, doc):
    text = doc.getText()
    cur = text.createTextCursor()
    text.insertString(cur, "Alpha one. ", False)
    text.insertString(cur, "Beta two. ", False)
    text.insertString(cur, "Outro paragraph.", False)

    desc = doc.createSearchDescriptor()
    desc.SearchString = "Beta two."
    found = doc.findFirst(desc)
    assert found is not None
    controller = doc.getCurrentController()
    controller.select(found)

    tctx = native_writer_tool_context(doc, ctx)
    tctx.selection_pin = SelectionPin(selection_cursor_snapshot(doc), doc)

    res_sel = SetSelection().execute(tctx, search_text="Outro")
    assert res_sel.get("status") == "ok", res_sel

    # Move the live caret away after set_selection; the pinned edit must still hit "Outro".
    controller.getViewCursor().gotoStart(False)

    res_apply = ApplyDocumentContent().execute(tctx, target="selection", content="FINAL")
    assert res_apply.get("status") == "ok", res_apply

    body = text.getString().replace("\r\n", "\n").replace("\r", "\n")
    assert "Beta two." in body, repr(body)
    assert "FINAL" in body and "Outro" not in body, repr(body)


def _build_cross_tool_doc(doc):
    text = doc.getText()
    lead = doc.createInstance("com.sun.star.text.TextTable")
    lead.initialize(1, 1)
    text.insertTextContent(text.getEnd(), lead, False)
    lead.getCellByName("A1").setString("LeadA1")

    cur = text.createTextCursor()
    cur.gotoEnd(False)
    field = doc.createInstance("com.sun.star.text.TextField.DateTime")
    text.insertTextContent(cur, field, False)
    text.insertString(cur, " ", False)
    fn = doc.createInstance("com.sun.star.text.Footnote")
    fn.setLabel("1")
    text.insertTextContent(cur, fn, False)
    text.insertString(cur, " ", False)
    text.insertString(cur, "DeletedWord ", False)
    cur.goLeft(len("DeletedWord "), True)
    doc.setPropertyValue("RecordChanges", True)
    try:
        cur.setString("")
    finally:
        doc.setPropertyValue("RecordChanges", False)
    assert doc.getRedlines().getCount() == 1
    cur.gotoEnd(False)
    text.insertString(cur, "TargetOne", False)

    mid = doc.createInstance("com.sun.star.text.TextTable")
    mid.initialize(1, 1)
    text.insertTextContent(text.getEnd(), mid, False)
    mid.getCellByName("A1").setString("MidA1")

    cur.gotoEnd(False)
    text.insertString(cur, "TargetTwo", False)


@native_test
@with_native_doc("writer")
def test_cross_tool_offsets_search_select_content_and_length_uno(ctx, doc):
    import re
    from plugin.doc.text_helpers import visible_range_text
    from plugin.writer.get_document_content import GetDocumentContent
    from plugin.writer.search import SearchInDocument

    _build_cross_tool_doc(doc)
    tctx = native_writer_tool_context(doc, ctx)
    controller = doc.getCurrentController()

    full = GetDocumentContent().execute(tctx, scope="full")
    assert full["document_length"] == len(visible_range_text(doc, doc.getText()))

    for word in ("TargetOne", "TargetTwo"):
        s_res = SearchInDocument().execute(tctx, pattern=word, return_offsets=True)
        assert s_res["status"] == "ok" and len(s_res["ranges"]) == 1, s_res
        r = s_res["ranges"][0]
        assert r["text"] == word

        sel_res = SetSelection().execute(tctx, char_start=r["start"], char_end=r["end"])
        assert sel_res["status"] == "ok", sel_res
        assert controller.getSelection().getByIndex(0).getString() == word

        c_res = GetDocumentContent().execute(tctx, scope="range", start=r["start"], end=r["end"])
        assert c_res["status"] == "ok", c_res
        plain = re.sub(r"<[^>]+>", "", c_res["content"]).strip()
        assert plain == word


@native_test
@with_native_doc("writer")
def test_set_selection_offset_in_leading_table_errors_and_preserves_selection_uno(ctx, doc):
    _build_cross_tool_doc(doc)
    tctx = native_writer_tool_context(doc, ctx)
    controller = doc.getCurrentController()

    ok = SetSelection().execute(tctx, search_text="TargetOne")
    assert ok["status"] == "ok", ok

    err = SetSelection().execute_safe(tctx, char_start=0, char_end=3)
    assert err["status"] == "error"
    assert err["code"] == "INVALID_PARAM"
    assert controller.getSelection().getByIndex(0).getString() == "TargetOne"


@native_test
@with_native_doc("writer")
def test_set_selection_search_reports_visible_text_uno(ctx, doc):
    text = doc.getText()
    text.setString("Word OLD here")
    doc.setPropertyValue("RecordChanges", True)
    try:
        cur = text.createTextCursor()
        cur.gotoStart(False)
        cur.goRight(len("Word "), False)
        cur.goRight(len("OLD "), True)
        cur.setString("")
    finally:
        doc.setPropertyValue("RecordChanges", False)

    tctx = native_writer_tool_context(doc, ctx)
    res = SetSelection().execute(tctx, search_text="Word OLD here")
    assert res["status"] == "ok", res
    assert res["selected_text"] == "Word here"
    assert res["length"] == 9

    # Chained two-paragraph needle over struck text.
    from tests.writer.test_search_uno import _build_chain_deletion_doc

    _build_chain_deletion_doc(doc, text)

    chain_needle = "Line 1\nLine 2 match"
    res_chain = SetSelection().execute(tctx, search_text=chain_needle)
    assert res_chain["status"] == "ok", res_chain
    assert res_chain["selected_text"] == chain_needle
    assert res_chain["length"] == len(chain_needle)


@native_test
@with_native_doc("writer")
def test_set_selection_crossing_deletion_and_occurrence_uno(ctx, doc):
    text = doc.getText()
    text.setString("Word OLD here and Word here")
    doc.setPropertyValue("RecordChanges", True)
    try:
        cur = text.createTextCursor()
        cur.gotoStart(False)
        cur.goRight(len("Word "), False)
        cur.goRight(len("OLD "), True)
        cur.setString("")
    finally:
        doc.setPropertyValue("RecordChanges", False)

    tctx = native_writer_tool_context(doc, ctx)
    res0 = SetSelection().execute(tctx, search_text="Word here")
    assert res0["status"] == "ok", res0
    assert res0["selected_text"] == "Word here"

    res1 = SetSelection().execute(tctx, search_text="Word here", occurrence=1)
    assert res1["status"] == "ok", res1
    assert res1["selected_text"] == "Word here"
    vc = doc.getCurrentController().getViewCursor()
    lead = text.createTextCursorByRange(vc.getStart())
    lead.gotoStartOfParagraph(True)
    assert lead.getString() == "Word OLD here and "
