"""Live UNO tests for footnote/endnote deletion and pinned-selection insertion.

The deletion tests run ``FootnotesDelete`` on a live document and check both the
supplier and the visible body text. One test pins the underlying anchor-clear
mechanism under change recording.
"""

from plugin.framework.config import get_config, save
from plugin.testing_runner import native_test
from plugin.tests.testing_utils import native_writer_tool_context, pinned_writer_tool_context, with_native_doc
from plugin.writer.specialized.footnotes import FootnotesDelete, FootnotesEdit, FootnotesInsert
from tests.writer.doc_builders import list_redline_types, reject_all_changes


def _insert_note(doc, note_type, text):
    """Mirror FootnotesInsert.execute: create, insert at end, set text."""
    service_name = "com.sun.star.text.Footnote" if note_type == "footnote" else "com.sun.star.text.Endnote"
    note = doc.createInstance(service_name)
    cursor = doc.getText().createTextCursor()
    cursor.gotoEnd(False)
    doc.getText().insertTextContent(cursor, note, False)
    note.setString(text)
    return note


def _supplier(doc, note_type):
    return doc.getFootnotes() if note_type == "footnote" else doc.getEndnotes()


def _body_text(doc):
    return doc.getText().getString()


def _assert_single_note_deleted(doc, note_type):
    """Shared assertions: one fresh note was deleted -- supplier empty, the
    reference-mark character gone from the body (a single note's mark is the
    deterministic "1" for footnotes / "i" for endnotes)."""
    assert _supplier(doc, note_type).getCount() == 0, "note must leave the supplier"
    assert _body_text(doc) == "probe text ", f"reference mark must vanish from body, got {_body_text(doc)!r}"


def _delete_first_note(doc, ctx, note_type):
    res = FootnotesDelete().execute(native_writer_tool_context(doc, ctx), note=note_type, index=0)
    assert res["status"] == "ok", res
    assert "tracked" not in res, res


@native_test
@with_native_doc("writer")
def test_footnote_delete_clears_supplier(ctx, doc):
    """FootnotesDelete clears a footnote and its reference mark."""
    doc.getText().setString("probe text ")
    _insert_note(doc, "footnote", "note body")
    assert _supplier(doc, "footnote").getCount() == 1
    assert _body_text(doc) == "probe text 1"

    _delete_first_note(doc, ctx, "footnote")

    _assert_single_note_deleted(doc, "footnote")


@native_test
@with_native_doc("writer")
def test_endnote_delete_clears_supplier(ctx, doc):
    """FootnotesDelete clears an endnote and its reference mark."""
    doc.getText().setString("probe text ")
    _insert_note(doc, "endnote", "note body")
    assert _supplier(doc, "endnote").getCount() == 1
    assert _body_text(doc) == "probe text i"

    _delete_first_note(doc, ctx, "endnote")

    _assert_single_note_deleted(doc, "endnote")


@native_test
@with_native_doc("writer")
def test_delete_one_of_two_keeps_the_other(ctx, doc):
    """Deletion is surgical: the surviving note and its mark are untouched."""
    doc.getText().setString("probe text ")
    _insert_note(doc, "footnote", "first")
    _insert_note(doc, "footnote", "second")
    assert _body_text(doc) == "probe text 12"

    _delete_first_note(doc, ctx, "footnote")

    assert _supplier(doc, "footnote").getCount() == 1, "the other note must survive"
    survivor = _supplier(doc, "footnote").getByIndex(0)
    assert survivor.getString() == "second"
    # The surviving note renumbers to 1 after the first is removed.
    assert _body_text(doc) == "probe text 1"


@native_test
@with_native_doc("writer")
def test_anchor_delete_records_redline_when_recording_on(ctx, doc):
    """With RecordChanges on, the anchor-clear records a tracked deletion and the
    note survives until the change is rejected/accepted (never silently dropped)."""
    doc.getText().setString("probe text ")
    note = _insert_note(doc, "footnote", "note body")

    doc.setPropertyValue("RecordChanges", True)
    try:
        note.getAnchor().setString("")
    finally:
        doc.setPropertyValue("RecordChanges", False)

    # The footnote is still there, only marked for deletion.
    assert _supplier(doc, "footnote").getCount() == 1, "note must survive as a tracked deletion"

    redlines = list_redline_types(doc)
    assert "Delete" in redlines, f"anchor-clear under RecordChanges must record a Delete redline, got {redlines}"

    # Reject the tracked deletion: the note (and its mark) come back.
    reject_all_changes(doc, ctx)
    assert _supplier(doc, "footnote").getCount() == 1
    assert _body_text(doc) == "probe text 1", f"reject must restore the mark, got {_body_text(doc)!r}"


@native_test
@with_native_doc("writer")
def test_delete_tool_reports_a_tracked_deletion(ctx, doc):
    doc.getText().setString("probe text ")
    _insert_note(doc, "footnote", "note body")
    doc.setPropertyValue("RecordChanges", True)
    try:
        res = FootnotesDelete().execute(native_writer_tool_context(doc, ctx), note="footnote", index=0)
    finally:
        doc.setPropertyValue("RecordChanges", False)

    assert res["status"] == "ok"
    assert res.get("tracked") is True
    assert res.get("pending_review") is True
    assert "review_mode" not in res
    assert "marked for deletion" in res["message"]
    assert list_redline_types(doc) == ["Delete"]
    assert _supplier(doc, "footnote").getCount() == 1


@native_test
@with_native_doc("writer")
def test_footnote_insert_uses_pinned_selection_uno(ctx, doc):
    from plugin.writer.specialized.footnotes import FootnotesInsert

    text = doc.getText()
    cur = text.createTextCursor()
    text.insertString(cur, "Alpha beta gamma", False)
    text.insertControlCharacter(cur, 0, False)
    text.insertString(cur, "Second para", False)
    sd = doc.createSearchDescriptor()
    sd.SearchString = "beta"
    found = doc.findFirst(sd)
    assert found is not None
    controller = doc.getCurrentController()
    controller.select(found)

    tctx = pinned_writer_tool_context(doc, ctx)
    controller.getViewCursor().gotoEnd(False)

    res = FootnotesInsert().execute(tctx, note="footnote", text="Pinned note")
    assert res["status"] == "ok", res
    assert doc.getFootnotes().getCount() == 1

    anchor = doc.getFootnotes().getByIndex(0).getAnchor()
    before = anchor.getText().createTextCursorByRange(anchor.getStart())
    before.gotoStart(True)
    assert before.getString() == "Alpha beta"
    assert "Second para" in text.getString()


def _redlines_info(doc):
    rls = doc.getRedlines()
    return [
        (
            rls.getByIndex(i).getPropertyValue("RedlineType"),
            rls.getByIndex(i).getPropertyValue("RedlineAuthor"),
            rls.getByIndex(i).getPropertyValue("RedlineComment"),
        )
        for i in range(rls.getCount())
    ]


@native_test
@with_native_doc("writer")
def test_footnote_delete_in_record_mode_is_a_tagged_agent_change_uno(ctx, doc):
    doc.getText().setString("probe text ")
    _insert_note(doc, "footnote", "note body")
    assert doc.getPropertyValue("RecordChanges") is False

    prev = get_config("doc.agent_edit_review_mode")
    save({"doc.agent_edit_review_mode": "record"})
    try:
        res = FootnotesDelete().execute(native_writer_tool_context(doc, ctx), note="footnote", index=0)
    finally:
        save({"doc.agent_edit_review_mode": prev})

    assert res["status"] == "ok", res
    assert res.get("tracked") is True
    assert res.get("pending_review") is True
    assert res.get("review_mode") == "record"
    assert _supplier(doc, "footnote").getCount() == 1
    rls = _redlines_info(doc)
    assert len(rls) == 1
    assert rls[0][0] == "Delete"
    assert rls[0][1] == "WriterAgent"
    assert rls[0][2].startswith("wa-review:")
    assert doc.getPropertyValue("RecordChanges") is False

    reject_all_changes(doc, ctx)
    assert _body_text(doc) == "probe text 1"


@native_test
@with_native_doc("writer")
def test_footnote_insert_in_record_mode_rejects_to_no_note_uno(ctx, doc):
    doc.getText().setString("probe text ")
    doc.getCurrentController().getViewCursor().gotoEnd(False)

    prev = get_config("doc.agent_edit_review_mode")
    save({"doc.agent_edit_review_mode": "record"})
    try:
        res = FootnotesInsert().execute(
            native_writer_tool_context(doc, ctx),
            note="footnote",
            text="inserted note",
            occurrence=0,
        )
    finally:
        save({"doc.agent_edit_review_mode": prev})

    assert res["status"] == "ok", res
    assert res.get("tracked") is True
    rls = _redlines_info(doc)
    assert rls and all(author == "WriterAgent" for _t, author, _c in rls), rls

    reject_all_changes(doc, ctx)
    assert _supplier(doc, "footnote").getCount() == 0
    assert _body_text(doc) == "probe text "


@native_test
@with_native_doc("writer")
def test_footnote_edit_text_and_label_in_record_mode_uno(ctx, doc):
    doc.getText().setString("probe text ")
    note = _insert_note(doc, "footnote", "old body")

    prev = get_config("doc.agent_edit_review_mode")
    save({"doc.agent_edit_review_mode": "record"})
    try:
        res = FootnotesEdit().execute(
            native_writer_tool_context(doc, ctx),
            note="footnote",
            index=0,
            text="new body",
            label="*",
        )
    finally:
        save({"doc.agent_edit_review_mode": prev})

    assert res["status"] == "ok", res
    assert res.get("tracked") is False
    assert res.get("pending_review") is True
    assert "warning" in res

    reject_all_changes(doc, ctx)
    assert note.getString() == "old body"
    assert note.getLabel() == "*"


@native_test
@with_native_doc("writer")
def test_footnote_insert_and_edit_off_mode_no_tracked_or_warning_uno(ctx, doc):
    doc.getText().setString("probe text ")
    doc.getCurrentController().getViewCursor().gotoEnd(False)
    assert doc.getPropertyValue("RecordChanges") is False

    prev = get_config("doc.agent_edit_review_mode")
    save({"doc.agent_edit_review_mode": "off"})
    try:
        tctx = native_writer_tool_context(doc, ctx)
        ins_res = FootnotesInsert().execute(
            tctx,
            note="footnote",
            text="plain note",
            occurrence=0,
        )
        edit_res = FootnotesEdit().execute(
            tctx,
            note="footnote",
            index=0,
            text="updated note",
            label="*",
        )
    finally:
        save({"doc.agent_edit_review_mode": prev})

    for res in (ins_res, edit_res):
        assert res["status"] == "ok", res
        assert "tracked" not in res, res
        assert "pending_review" not in res, res
        assert "warning" not in res, res


@native_test
@with_native_doc("writer")
def test_footnote_list_hides_tracked_deletion_uno(ctx, doc):
    from plugin.writer.specialized.footnotes import FootnotesList

    doc.getText().setString("Body ")
    note = _insert_note(doc, "footnote", "note velha nova")
    cur = note.createTextCursor()
    cur.gotoStart(False)
    cur.goRight(len("note "), False)
    cur.goRight(len("velha "), True)
    doc.setPropertyValue("RecordChanges", True)
    try:
        cur.setString("")
    finally:
        doc.setPropertyValue("RecordChanges", False)

    res = FootnotesList().execute(native_writer_tool_context(doc, ctx), note="footnote")
    assert res["status"] == "ok", res
    assert res["notes"][0]["text"] == "note nova"
