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

    list_res = TrackChangesList().execute(tctx, start_index=0, max_changes=100)
    assert list_res.get("status") == "ok", list_res
    changes = list_res.get("changes", [])

    body_row = next(c for c in changes if "Body user edit" in c.get("text", ""))
    body_idx = body_row["index"]

    accept_res = ManageTrackedChanges().execute(tctx, action="accept", index=body_idx)
    assert accept_res.get("status") == "ok", accept_res

    list_after = TrackChangesList().execute(tctx, start_index=0, max_changes=100)
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


def _reset_and_record(doc):
    from plugin.framework.config import get_config, save

    prev = get_config("doc.agent_edit_review_mode")
    doc.setPropertyValue("RecordChanges", False)
    doc.getText().setString("")
    save({"doc.agent_edit_review_mode": "record"})
    return prev


@native_test
@with_native_doc("writer")
def test_model_cannot_resolve_agent_changes_after_docx_reload_uno(ctx, doc):
    import os
    import tempfile
    import uno
    from plugin.app import get_services
    from plugin.framework.config import save
    from plugin.framework.uno_context import create_property_value
    from plugin.writer.apply_document_content import ApplyDocumentContent
    from plugin.writer.tracking import ManageTrackedChanges
    from tests.writer.doc_builders import reject_all_changes

    prev = _reset_and_record(doc)
    reloaded = None
    try:
        doc.getText().setString("The quick brown fox jumps over the lazy dog.")
        tctx = TestingFactory.create_context(
            doc=doc, ctx=ctx, env="native", doc_type="writer", services=get_services()
        )
        res = ApplyDocumentContent().execute(tctx, target="search", old_content="quick", content="fast")
        assert res.get("status") == "ok", res

        with tempfile.TemporaryDirectory() as tmpdir:
            docx_path = os.path.join(tmpdir, "reload.docx")
            docx_url = uno.systemPathToFileUrl(docx_path)
            doc.storeToURL(docx_url, (create_property_value("FilterName", "MS Word 2007 XML"),))
            desktop = ctx.getServiceManager().createInstanceWithContext("com.sun.star.frame.Desktop", ctx)
            reloaded = desktop.loadComponentFromURL(
                docx_url, "_blank", 0, (create_property_value("Hidden", True),)
            )
            r_tctx = TestingFactory.create_context(
                doc=reloaded, ctx=ctx, env="native", doc_type="writer", services=get_services()
            )
            assert ManageTrackedChanges().execute(r_tctx, action="accept_all")["status"] == "error"
            assert ManageTrackedChanges().execute(r_tctx, action="accept", index=0)["status"] == "error"

            reject_all_changes(reloaded, ctx)
            reloaded_text = reloaded.getText().getString()
            assert "quick" in reloaded_text
            assert "fast" not in reloaded_text
            assert reloaded.getRedlines().getCount() == 0
    finally:
        if reloaded is not None:
            reloaded.close(True)
        save({"doc.agent_edit_review_mode": prev})


@native_test
@with_native_doc("writer")
def test_model_cannot_resolve_untagged_style_redlines_uno(ctx, doc):
    import uno
    from plugin.app import get_services
    from plugin.framework.config import save
    from plugin.writer.styles import ApplyStyle, StyleFormatText
    from plugin.writer.tracking import ManageTrackedChanges
    from tests.writer.doc_builders import body_paragraphs, reject_all_changes

    _para_break = uno.getConstantByName("com.sun.star.text.ControlCharacter.PARAGRAPH_BREAK")
    prev = _reset_and_record(doc)
    try:
        text = doc.getText()
        cur = text.createTextCursor()
        text.insertString(cur, "The quick brown fox.", False)
        text.insertControlCharacter(cur, _para_break, False)
        text.insertString(cur, "Second paragraph.", False)
        orig_para_style = body_paragraphs(text)[1].getPropertyValue("ParaStyleName")

        tctx = TestingFactory.create_context(
            doc=doc, ctx=ctx, env="native", doc_type="writer", services=get_services()
        )
        fmt_res = StyleFormatText().execute(
            tctx, properties={"CharWeight": 150}, target="search", old_content="brown fox", all_matches=False
        )
        assert fmt_res.get("status") == "ok", fmt_res
        sty_res = ApplyStyle().execute(
            tctx, style="Heading 2", family="ParagraphStyles", target="search", old_content="Second paragraph.", all_matches=False
        )
        assert sty_res.get("status") == "ok", sty_res

        assert ManageTrackedChanges().execute(tctx, action="accept_all")["status"] == "error"
        for idx in range(doc.getRedlines().getCount()):
            assert ManageTrackedChanges().execute(tctx, action="reject", index=idx)["status"] == "error"

        reject_all_changes(doc, ctx)
        p0_cur = body_paragraphs(text)[0].createEnumeration().nextElement()
        assert float(p0_cur.getPropertyValue("CharWeight")) == 100.0
        assert body_paragraphs(text)[1].getPropertyValue("ParaStyleName") == orig_para_style
    finally:
        save({"doc.agent_edit_review_mode": prev})


@native_test
@with_native_doc("writer")
def test_model_cannot_resolve_table_only_insert_uno(ctx, doc):
    from plugin.app import get_services
    from plugin.framework.config import save
    from plugin.writer.specialized.tables import TableInsert
    from plugin.writer.tracking import ManageTrackedChanges

    prev = _reset_and_record(doc)
    try:
        doc.getCurrentController().getViewCursor().gotoStart(False)
        tctx = TestingFactory.create_context(
            doc=doc, ctx=ctx, env="native", doc_type="writer", services=get_services()
        )
        res = TableInsert().execute(tctx, rows=2, columns=2)
        assert res.get("status") == "ok", res
        assert ManageTrackedChanges().execute(tctx, action="reject_all")["status"] == "error"
        assert doc.getTextTables().getCount() == 1
    finally:
        save({"doc.agent_edit_review_mode": prev})


@native_test
@with_native_doc("writer")
def test_model_cannot_resolve_delete_stacked_on_user_insert_uno(ctx, doc):
    from plugin.app import get_services
    from plugin.framework.config import save
    from plugin.writer import review_authors
    from plugin.writer.apply_document_content import ApplyDocumentContent
    from plugin.writer.tracking import ManageTrackedChanges

    prev = _reset_and_record(doc)
    try:
        text = doc.getText()
        text.setString("The cat is ")
        cur = text.createTextCursor()
        cur.gotoEnd(False)
        prior = review_authors.begin(ctx)
        doc.setPropertyValue("RecordChanges", True)
        try:
            assert review_authors.set_office_author(ctx, "Alice") is True
            text.insertString(cur, "very sleepy and slow.", False)
        finally:
            doc.setPropertyValue("RecordChanges", False)
            review_authors.end(ctx, prior)

        tctx = TestingFactory.create_context(
            doc=doc, ctx=ctx, env="native", doc_type="writer", services=get_services()
        )
        res = ApplyDocumentContent().execute(tctx, target="search", old_content="sleepy and ", content="")
        assert res.get("status") == "ok", res

        before_text = text.getString()
        before_count = doc.getRedlines().getCount()
        assert ManageTrackedChanges().execute(tctx, action="accept_all")["status"] == "error"
        assert text.getString() == before_text
        assert doc.getRedlines().getCount() == before_count
    finally:
        save({"doc.agent_edit_review_mode": prev})


def _user_replace_and_delete(doc, ctx):
    """A user's replace (quick -> fast) and deletion ("Second "), recorded without agent tokens."""
    from tests.writer.doc_builders import find_first, reset_body

    reset_body(doc, ctx, "The quick brown fox.", "Second para.")
    doc.setPropertyValue("RecordChanges", True)
    quick = find_first(doc, "quick")
    quick.getText().insertString(quick.getEnd(), "fast", False)
    find_first(doc, "quick").setString("")
    find_first(doc, "Second ").setString("")
    doc.setPropertyValue("RecordChanges", False)


def _listed_index(tctx, redline_type, text):
    from plugin.writer.tracking import TrackChangesList

    listed = TrackChangesList().execute(tctx, start_index=0, max_changes=100)
    assert listed.get("status") == "ok", listed
    return next(c["index"] for c in listed["changes"] if c["RedlineType"] == redline_type and c.get("text") == text)


@native_test
@with_native_doc("writer")
def test_manage_by_index_with_deletions_hidden_uno(ctx, doc):
    """tracking_list indexes read with deletions hidden resolve the listed change and keep the display type."""
    from plugin.app import get_services
    from plugin.writer.tracking import ManageTrackedChanges, redline_text

    tctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native", doc_type="writer", services=get_services())
    try:
        _user_replace_and_delete(doc, ctx)
        doc.setPropertyValue("RedlineDisplayType", 1)
        for action, redline_type, text in (("accept", "Delete", "quick"), ("reject", "Insert", "fast")):
            res = ManageTrackedChanges().execute(tctx, action=action, index=_listed_index(tctx, redline_type, text))
            assert res.get("status") == "ok", res
            assert doc.getPropertyValue("RedlineDisplayType") == 1
        doc.setPropertyValue("RedlineDisplayType", 2)
        left = [(r.getPropertyValue("RedlineType"), redline_text(r)) for r in _redlines(doc)]
        assert left == [("Delete", "Second ")], left
        assert doc.getText().getString().startswith("The  brown fox.")
    finally:
        doc.setPropertyValue("RedlineDisplayType", 2)


@native_test
@with_native_doc("writer")
def test_recording_and_markup_toggles_warn_when_changes_pending_uno(ctx, doc):
    from plugin.app import get_services
    from plugin.writer.tracking import TrackChangesShow, TrackChangesStart, TrackChangesStop
    from tests.writer.doc_builders import reset_body

    tctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native", doc_type="writer", services=get_services())
    try:
        reset_body(doc, ctx, "No changes here.")
        assert "warning" not in TrackChangesStop().execute(tctx)
        _user_replace_and_delete(doc, ctx)
        for res in (TrackChangesStart().execute(tctx), TrackChangesStop().execute(tctx), TrackChangesShow().execute(tctx, show=False)):
            assert res.get("status") == "ok", res
            assert "warning" in res, res
    finally:
        doc.setPropertyValue("RecordChanges", False)
        doc.setPropertyValue("RedlineDisplayType", 2)


def _redlines(doc):
    from plugin.framework.uno_context import enum_has_more

    out = []
    e = doc.getRedlines().createEnumeration()
    while enum_has_more(e):
        out.append(e.nextElement())
    return out


@native_test
@with_native_doc("writer", reuse=False)
def test_single_resolve_of_one_cell_of_a_tracked_row_refused_uno(ctx, doc):
    """tracking_manage by index refuses one cell of another author's row deletion and leaves the row."""
    from plugin.framework.uno_context import execute_dispatch
    from plugin.writer.tracking import ManageTrackedChanges
    from tests.writer.doc_builders import insert_named_table, list_redlines, recording_as

    table = insert_named_table(doc, "T1", 3, 3, {n: n.lower() for n in ("A1", "B1", "C1", "A2", "B2", "C2", "A3", "B3", "C3")})
    controller = doc.getCurrentController()
    with recording_as(doc, ctx):
        controller.select(table.getCellByName("A2").getEnd())
        controller.getSelection()
        execute_dispatch(ctx, controller, ".uno:DeleteRows")
    before = list_redlines(doc)
    assert len(before) == 3, before
    tctx = TestingFactory.create_context(doc=doc, ctx=ctx, env="native", doc_type="writer")
    for action in ("accept", "reject"):
        res = ManageTrackedChanges().execute_safe(tctx, action=action, index=0)
        assert res["status"] == "error", res
    assert list_redlines(doc) == before
    assert table.getRows().getByIndex(1).getPropertyValue("HasTextChangesOnly") is False
