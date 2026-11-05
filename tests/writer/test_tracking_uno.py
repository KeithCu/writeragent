# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Native UNO integration tests for Writer track changes tools."""

from plugin.testing_runner import native_test
from plugin.tests.testing_utils import TestingFactory, with_native_doc


@native_test
@with_native_doc("writer")
def test_writer_track_changes_show_toggle_uno(ctx, doc):
    """TrackChangesShow sets RedlineDisplayType and updates ShowChanges without touching ShowChangesInMargin."""
    from plugin.app import get_services
    from plugin.writer.tracking import TrackChangesShow

    controller = doc.getCurrentController()
    view_settings = controller.getViewSettings()

    saved_display_type = doc.getPropertyValue("RedlineDisplayType")
    saved_margin = view_settings.getPropertyValue("ShowChangesInMargin")

    tctx = TestingFactory.create_context(
        doc=doc, ctx=ctx, env="native", doc_type="writer", services=get_services()
    )

    try:
        # show=False -> type 1, ShowChanges False
        res_hide = TrackChangesShow().execute(tctx, show=False)
        assert res_hide.get("status") == "ok", res_hide
        assert doc.getPropertyValue("RedlineDisplayType") == 1
        assert doc.getPropertyValue("ShowChanges") is False
        assert view_settings.getPropertyValue("ShowChangesInMargin") == saved_margin

        # show=True -> type 2, ShowChanges True
        res_show = TrackChangesShow().execute(tctx, show=True)
        assert res_show.get("status") == "ok", res_show
        assert doc.getPropertyValue("RedlineDisplayType") == 2
        assert doc.getPropertyValue("ShowChanges") is True
        assert view_settings.getPropertyValue("ShowChangesInMargin") == saved_margin
    finally:
        # Restore original values so reused document is left clean
        doc.setPropertyValue("RedlineDisplayType", saved_display_type)
        view_settings.setPropertyValue("ShowChangesInMargin", saved_margin)


@native_test
@with_native_doc("writer")
def test_accept_body_redline_with_table_cell_redline_uno(ctx, doc):
    import uno
    from plugin.app import get_services
    from plugin.writer.tracking import ManageTrackedChanges, TrackChangesList

    _para_break = uno.getConstantByName("com.sun.star.text.ControlCharacter.PARAGRAPH_BREAK")

    tctx = TestingFactory.create_context(
        doc=doc, ctx=ctx, env="native", doc_type="writer", services=get_services()
    )

    text = doc.getText()
    doc.setPropertyValue("RecordChanges", False)
    cur = text.createTextCursor()
    cur.gotoStart(False)
    cur.gotoEnd(True)
    cur.setString("")
    cur.gotoStart(False)
    text.insertString(cur, "Body clause one here.", False)

    cur.gotoEnd(False)
    text.insertControlCharacter(cur, _para_break, False)
    table = doc.createInstance("com.sun.star.text.TextTable")
    table.initialize(1, 1)
    text.insertTextContent(cur, table, False)
    cell_text = table.getCellByName("A1").getText()
    cell_text.setString("Cell clause two here.")

    # A tracked change in the BODY and another inside the TABLE CELL (user edits, no wa-review comment)
    doc.setPropertyValue("RecordChanges", True)
    c = text.createTextCursor()
    c.gotoEnd(False)
    text.insertString(c, " Body user edit.", False)

    cc = cell_text.createTextCursor()
    cc.gotoEnd(False)
    cell_text.insertString(cc, " Cell user edit.", False)
    doc.setPropertyValue("RecordChanges", False)

    list_res = TrackChangesList().execute(tctx)
    assert list_res.get("status") == "ok", list_res
    changes = list_res.get("changes", [])

    body_row = next(c for c in changes if "Body user edit" in c.get("text", ""))
    body_idx = body_row["index"]

    accept_res = ManageTrackedChanges().execute(tctx, action="accept", index=body_idx)
    assert accept_res.get("status") == "ok", accept_res

    list_after = TrackChangesList().execute(tctx)
    assert list_after.get("status") == "ok", list_after
    changes_after = list_after.get("changes", [])

    assert any("Cell user edit" in c.get("text", "") for c in changes_after), "cell redline must remain"
    assert not any("Body user edit" in c.get("text", "") for c in changes_after), "body redline must be gone"


@native_test
@with_native_doc("writer")
def test_writer_accept_all_clears_redlines_uno(ctx, doc):
    from plugin.app import get_services
    from plugin.writer.tracking import ManageTrackedChanges

    tctx = TestingFactory.create_context(
        doc=doc, ctx=ctx, env="native", doc_type="writer", services=get_services()
    )

    text = doc.getText()
    doc.setPropertyValue("RecordChanges", False)
    cur = text.createTextCursor()
    cur.gotoStart(False)
    cur.gotoEnd(True)
    cur.setString("")
    cur.gotoStart(False)
    text.insertString(cur, "Initial text.", False)

    # One user redline
    doc.setPropertyValue("RecordChanges", True)
    cur.gotoEnd(False)
    text.insertString(cur, " User tracked change.", False)
    doc.setPropertyValue("RecordChanges", False)

    assert doc.getRedlines().getCount() > 0, "precondition: user redline present"

    res = ManageTrackedChanges().execute(tctx, action="accept_all")
    assert res.get("status") == "ok", res
    assert doc.getRedlines().getCount() == 0
