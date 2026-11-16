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
    tctx.selection_pin = SelectionPin(selection_cursor_snapshot(doc))

    res_sel = SetSelection().execute(tctx, search_text="Outro")
    assert res_sel.get("status") == "ok", res_sel

    # Move the live caret away after set_selection; the pinned edit must still hit "Outro".
    controller.getViewCursor().gotoStart(False)

    res_apply = ApplyDocumentContent().execute(tctx, target="selection", content="FINAL")
    assert res_apply.get("status") == "ok", res_apply

    body = text.getString().replace("\r\n", "\n").replace("\r", "\n")
    assert "Beta two." in body, repr(body)
    assert "FINAL" in body and "Outro" not in body, repr(body)
