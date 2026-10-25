"""Regression tests for footnote/endnote deletion mechanics.

Pins the behavior documented in ``FootnotesDelete``
(plugin/writer/specialized/footnotes.py): clearing the reference-mark character
(``note.getAnchor().setString("")``) drops the whole note -- supplier entry,
reference mark, and note text.
"""

from types import SimpleNamespace

from plugin.testing_runner import native_test
from plugin.tests.testing_utils import with_native_doc
from plugin.writer.specialized.footnotes import FootnotesDelete


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


@native_test
@with_native_doc("writer")
def test_footnote_delete_via_anchor_clears_supplier(ctx, doc):
    """The shipped FootnotesDelete idiom clears a footnote and its reference mark."""
    doc.getText().setString("probe text ")
    note = _insert_note(doc, "footnote", "note body")
    assert _supplier(doc, "footnote").getCount() == 1
    assert _body_text(doc) == "probe text 1"

    note.getAnchor().setString("")

    _assert_single_note_deleted(doc, "footnote")


@native_test
@with_native_doc("writer")
def test_endnote_delete_via_anchor_clears_supplier(ctx, doc):
    """The shipped FootnotesDelete idiom clears an endnote and its reference mark."""
    doc.getText().setString("probe text ")
    note = _insert_note(doc, "endnote", "note body")
    assert _supplier(doc, "endnote").getCount() == 1
    assert _body_text(doc) == "probe text i"

    note.getAnchor().setString("")

    _assert_single_note_deleted(doc, "endnote")


@native_test
@with_native_doc("writer")
def test_delete_one_of_two_keeps_the_other(ctx, doc):
    """Deletion is surgical: the surviving note and its mark are untouched."""
    doc.getText().setString("probe text ")
    note1 = _insert_note(doc, "footnote", "first")
    _insert_note(doc, "footnote", "second")
    assert _body_text(doc) == "probe text 12"

    note1.getAnchor().setString("")

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

    redlines = []
    e = doc.getRedlines().createEnumeration()
    while e.hasMoreElements():
        redlines.append(str(e.nextElement().getPropertyValue("RedlineType")))
    assert "Delete" in redlines, f"anchor-clear under RecordChanges must record a Delete redline, got {redlines}"

    # Reject the tracked deletion: the note (and its mark) come back.
    helper = ctx.getServiceManager().createInstanceWithContext("com.sun.star.frame.DispatchHelper", ctx)
    frame = doc.getCurrentController().getFrame()
    helper.executeDispatch(frame, ".uno:RejectAllTrackedChanges", "", 0, ())
    assert _supplier(doc, "footnote").getCount() == 1
    assert _body_text(doc) == "probe text 1", f"reject must restore the mark, got {_body_text(doc)!r}"


@native_test
@with_native_doc("writer")
def test_delete_tool_reports_a_tracked_deletion(ctx, doc):
    doc.getText().setString("probe text ")
    _insert_note(doc, "footnote", "note body")
    doc.setPropertyValue("RecordChanges", True)
    try:
        res = FootnotesDelete().execute(SimpleNamespace(doc=doc), note="footnote", index=0)
    finally:
        doc.setPropertyValue("RecordChanges", False)

    assert res["status"] == "ok"
    assert res.get("tracked") is True
    assert _supplier(doc, "footnote").getCount() == 1
