# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
#
# EditReviewSession: the centralized review story for agent edits. Covers: inert when
# disabled; recording + per-change RedlineComment tokens + author attribution + restore
# semantics; completion keyed to THIS session's tokens (user redlines don't block);
# per-change outcomes accepted/rejected/modified/pending; timeout; stop_checker;
# bookmark cleanup; ShowChanges forced on.
import unittest

import uno  # noqa: F401

from plugin.framework.uno_context import execute_dispatch
from plugin.testing_runner import native_test
from plugin.tests.testing_utils import with_native_doc
from plugin.writer.edit_review import EditReviewSession, _BOOKMARK_PREFIX
from tests.writer.doc_builders import accept_all_changes, list_redlines, reject_all_changes, reset_body


def _find(doc, needle):
    sd = doc.createSearchDescriptor()
    sd.setSearchString(needle)
    return doc.findFirst(sd)


def _replace_fn(doc, old, new):
    """A mutation callable: replace first occurrence of *old* with *new* (clean delete+insert)."""
    def fn():
        found = _find(doc, old)
        assert found is not None, "mutation target %r not found" % old
        text = found.getText()
        c = text.createTextCursorByRange(found)
        c.setString("")
        text.insertString(c, new, False)
    return fn


def _resolve_at(doc, ctx, needle, accept):
    """Resolve the tracked change in *needle*'s paragraph, as a user would (select + native
    accept/reject dispatch). Local on purpose: this suite must not depend on the UI helpers."""
    f = _find(doc, needle)
    assert f is not None, "resolve target %r not found" % needle
    text = f.getText()
    para = text.createTextCursorByRange(f.getStart())
    para.gotoStartOfParagraph(False)
    para.gotoEndOfParagraph(True)
    view_cursor = doc.getCurrentController().getViewCursor()
    view_cursor.gotoRange(para.getStart(), False)
    view_cursor.gotoRange(para.getEnd(), True)
    command = ".uno:AcceptTrackedChange" if accept else ".uno:RejectTrackedChange"
    execute_dispatch(ctx, doc.getCurrentController(), command)


def _wa_bookmarks(doc):
    return [n for n in doc.getBookmarks().getElementNames() if n.startswith(_BOOKMARK_PREFIX)]


@native_test
@with_native_doc("writer")
def test_disabled_session_is_inert_uno(ctx, doc):
    reset_body(doc, ctx, "Alpha paragraph.")
    with EditReviewSession(doc, ctx, enabled=False) as session:
        session.record_mutation(_replace_fn(doc, "Alpha paragraph.", "Alpha edited."))
    assert list_redlines(doc) == [], "disabled session must not record redlines"
    assert _find(doc, "Alpha edited.") is not None, "edit applied directly"
    result = session.wait_for_review(timeout=0.1)
    assert result == {"complete": True, "timed_out": False, "changes": []}, result


@native_test
@with_native_doc("writer")
def test_records_tags_author_and_restores_uno(ctx, doc):
    reset_body(doc, ctx, "Alpha paragraph.")
    doc.setPropertyValue("ShowChanges", False)
    with EditReviewSession(doc, ctx, enabled=True) as session:
        assert doc.getPropertyValue("ShowChanges") is True, "session start must make markup visible"
        session.record_mutation(_replace_fn(doc, "Alpha paragraph.", "Alpha edited."))
    rls = list_redlines(doc)
    assert len(rls) == 2, "a replace records a Delete+Insert pair, got %r" % rls
    token = session.changes[0].token
    assert token.startswith("wa-review:"), token
    assert all(r["RedlineComment"] == token for r in rls), "BOTH redlines carry the change token: %r" % rls
    assert all(r["RedlineAuthor"] == "WriterAgent" for r in rls), "agent attribution: %r" % rls
    assert doc.getPropertyValue("RecordChanges") is False, "prior OFF state restored"
    session.cleanup()


@native_test
@with_native_doc("writer")
def test_bookkeeping_stays_off_the_undo_stack_uno(ctx, doc):
    """The user's first Ctrl+Z after an agent edit must undo the VISIBLE change, not toggle our
    invisible wa_review anchor bookmarks. Recording + cleanup lock the document undo manager
    around the tag/bookmark bookkeeping, so the user's undo stack only holds the real edit."""
    reset_body(doc, ctx, "This clause is important.")
    doc.setPropertyValue("RecordChanges", False)
    with EditReviewSession(doc, ctx, enabled=True) as session:
        session.record_mutation(_replace_fn(doc, "This clause is important.", "This clause is essential."))
    session.cleanup()
    assert _wa_bookmarks(doc) == [], "cleanup must leave no anchor bookmarks behind"
    um = doc.getUndoManager()
    assert um.isUndoPossible(), "the agent edit must be undoable"
    title = um.getCurrentUndoActionTitle()
    assert "bookmark" not in title.lower() and _BOOKMARK_PREFIX not in title, \
        "internal bookkeeping leaked onto the user's undo stack: top=%r" % title
    # One undo must revert the edit (drop a redline), not no-op on an invisible bookmark.
    before = len(list_redlines(doc))
    execute_dispatch(ctx, doc.getCurrentController(), ".uno:Undo")
    assert len(list_redlines(doc)) < before, \
        "the first undo must revert the agent edit, not toggle an invisible bookmark (had %d redlines)" % before


@native_test
@with_native_doc("writer")
def test_user_redlines_do_not_block_completion_uno(ctx, doc):
    reset_body(doc, ctx, "User paragraph.", "Agent paragraph.")
    # the USER has their own pending tracked change before the agent edits
    doc.setPropertyValue("RecordChanges", True)
    _replace_fn(doc, "User paragraph.", "User edited paragraph.")()
    doc.setPropertyValue("RecordChanges", False)
    with EditReviewSession(doc, ctx, enabled=True) as session:
        session.record_mutation(_replace_fn(doc, "Agent paragraph.", "Agent edited paragraph."))
    untagged = [r for r in list_redlines(doc) if not r["RedlineComment"].startswith("wa-review:")]
    assert len(untagged) == 2, "the user's own redlines must NOT get the session token: %r" % list_redlines(doc)
    _resolve_at(doc, ctx, "Agent edited", True)  # resolve only the agent's change
    result = session.wait_for_review(timeout=2, poll=0.05)
    assert result["complete"] is True, "user's pending redline must not block completion: %r" % result
    assert [r for r in list_redlines(doc) if not r["RedlineComment"].startswith("wa-review:")], \
        "the user's redline must still be pending (untouched)"
    reset_body(doc, ctx, "reset")  # clear the leftover user redline for the next test


@native_test
@with_native_doc("writer")
def test_outcomes_accept_and_reject_per_change_uno(ctx, doc):
    reset_body(doc, ctx, "First clause here.", "Second clause here.")
    with EditReviewSession(doc, ctx, enabled=True) as session:
        session.record_mutation(_replace_fn(doc, "First clause here.", "First clause EDITED."),
                                original_preview="First clause here.", proposed_preview="First clause EDITED.")
        session.record_mutation(_replace_fn(doc, "Second clause here.", "Second clause EDITED."))
    _resolve_at(doc, ctx, "First clause EDITED", True)    # accept change #1
    _resolve_at(doc, ctx, "Second clause EDITED", False)  # reject change #2
    result = session.wait_for_review(timeout=2, poll=0.05)
    assert result["complete"] is True and result["timed_out"] is False, result
    outcomes = [c["outcome"] for c in result["changes"]]
    assert outcomes == ["accepted", "rejected"], "per-change outcomes: %r" % result
    assert result["changes"][0]["original_preview"] == "First clause here."
    assert result["changes"][0]["proposed_preview"] == "First clause EDITED."
    assert _wa_bookmarks(doc) == [], "anchor bookmarks must be cleaned after review"


@native_test
@with_native_doc("writer")
def test_outcome_modified_when_user_edits_during_review_uno(ctx, doc):
    reset_body(doc, ctx, "Stable clause here.")
    with EditReviewSession(doc, ctx, enabled=True) as session:
        session.record_mutation(_replace_fn(doc, "Stable clause here.", "Stable clause EDITED."))
    _resolve_at(doc, ctx, "Stable clause EDITED", True)
    # the user reworks the paragraph after resolving (tracking off = silent manual edit)
    f = _find(doc, "Stable clause EDITED.")
    c = f.getText().createTextCursorByRange(f)
    c.setString("Something else entirely.")
    result = session.wait_for_review(timeout=2, poll=0.05)
    assert result["changes"][0]["outcome"] == "modified", \
        "user edit during review must report modified, got %r" % result


@native_test
@with_native_doc("writer")
def test_timeout_reports_pending_uno(ctx, doc):
    reset_body(doc, ctx, "Waiting clause here.")
    with EditReviewSession(doc, ctx, enabled=True) as session:
        session.record_mutation(_replace_fn(doc, "Waiting clause here.", "Waiting clause EDITED."))
    result = session.wait_for_review(timeout=0.3, poll=0.05)  # nobody reviews
    assert result["complete"] is False and result["timed_out"] is True, result
    assert result["changes"][0]["outcome"] == "pending", result
    assert _wa_bookmarks(doc) == [], "bookmarks cleaned even on timeout"
    reset_body(doc, ctx, "reset")  # clear the unresolved change


@native_test
@with_native_doc("writer")
def test_stop_checker_aborts_wait_uno(ctx, doc):
    from plugin.framework.send_cancellation import SendCancellation

    reset_body(doc, ctx, "Abort clause here.")
    with EditReviewSession(doc, ctx, enabled=True) as session:
        session.record_mutation(_replace_fn(doc, "Abort clause here.", "Abort clause EDITED."))
    scope = SendCancellation()
    scope.cancel()
    result = session.wait_for_review(timeout=30, poll=0.05, cancel=scope)
    assert result["complete"] is False and result["timed_out"] is False, \
        "cancel abort is not a timeout: %r" % result
    reset_body(doc, ctx, "reset")


@native_test
@with_native_doc("writer")
def test_wait_for_review_routes_uno_via_runner_uno(ctx, doc):
    """Off-main-thread callers (MCP HTTP / chat worker) pass uno_runner=main.call;
    every document touch in the wait loop must flow through it."""
    reset_body(doc, ctx, "Runner clause here.")
    with EditReviewSession(doc, ctx, enabled=True) as session:
        session.record_mutation(_replace_fn(doc, "Runner clause here.", "Runner clause EDITED."))
    _resolve_at(doc, ctx, "Runner clause EDITED", True)
    calls = {"n": 0}

    def runner(fn):
        calls["n"] += 1
        return fn()

    result = session.wait_for_review(timeout=2, poll=0.05, uno_runner=runner)
    assert result["complete"] is True and result["changes"][0]["outcome"] == "accepted", result
    assert calls["n"] >= 3, "pending check, payload, and cleanup must go through the runner, got %d" % calls["n"]


@native_test
@with_native_doc("writer")
def test_prior_recording_on_is_preserved_uno(ctx, doc):
    reset_body(doc, ctx, "Tracked clause here.")
    doc.setPropertyValue("RecordChanges", True)
    with EditReviewSession(doc, ctx, enabled=True) as session:
        session.record_mutation(_replace_fn(doc, "Tracked clause here.", "Tracked clause EDITED."))
    assert doc.getPropertyValue("RecordChanges") is True, "user's ON state must be preserved"
    doc.setPropertyValue("RecordChanges", False)
    assert session.changes, "change recorded under user tracking too"
    session.cleanup()
    reset_body(doc, ctx, "reset")


@native_test
@with_native_doc("writer")
def test_exception_restores_recording_and_author_uno(ctx, doc):
    reset_body(doc, ctx, "Crash clause here.")
    try:
        with EditReviewSession(doc, ctx, enabled=True):
            assert doc.getPropertyValue("RecordChanges") is True
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert doc.getPropertyValue("RecordChanges") is False, "recording restored on exception"

@native_test
@with_native_doc("writer")
def test_anchor_failure_leaves_unregistered_does_not_hang_uno(ctx, doc):
    reset_body(doc, ctx, "Anchor failure clause here.")
    doc.setPropertyValue("RecordChanges", False)

    with EditReviewSession(doc, ctx, enabled=True) as session:
        def mutate():
            t = doc.getText()
            c = t.createTextCursor()
            c.gotoStart(False)
            c.gotoEndOfParagraph(True)
            c.setString("Edited.")

        class DocWrapper:
            def __getattr__(self, item):
                if item == "createInstance":
                    def createInstance(name):
                        if name == "com.sun.star.text.Bookmark":
                            raise RuntimeError("mock anchor fail")
                        return doc.createInstance(name)
                    return createInstance
                return getattr(doc, item)

        session.doc = DocWrapper()
        try:
            session.record_mutation(mutate)
        finally:
            session.doc = doc

    result = session.wait_for_review(timeout=0.1)
    assert result == {"complete": True, "timed_out": False, "changes": []}, result


@native_test
@with_native_doc("writer")
def test_bookmark_reverted_with_edit_undo_uno(ctx, doc):
    """Anchor bookmark is in the same undo context as the edit, so Undo removes both."""
    reset_body(doc, ctx, "This clause is important.")
    with EditReviewSession(doc, ctx, enabled=True) as session:
        session.record_mutation(_replace_fn(doc, "This clause is important.", "This clause is essential."))
    assert len(_wa_bookmarks(doc)) == 1, "anchor bookmark should exist before review"
    um = doc.getUndoManager()
    assert um.isUndoPossible()
    um.undo()
    assert _wa_bookmarks(doc) == [], "undo must revert the anchor bookmark along with the edit"
    assert len(list_redlines(doc)) == 0, "undo must revert the redline"


@native_test
@with_native_doc("writer")
def test_whole_paragraph_and_leading_table_review_outcomes_uno(ctx, doc):
    from plugin.writer.edit_review import EditReviewSession, record_html_atomically
    from plugin.writer.html_import import replace_single_range_with_content

    for accept, expected_outcome in ((True, "accepted"), (False, "rejected")):
        while doc.getTextTables().getCount() > 0:
            doc.getTextTables().getByIndex(0).dispose()
        reset_body(doc, ctx, "Intro", "Para one", "Para two", "Outro")
        session = EditReviewSession(doc, ctx, enabled=True)
        with session:
            r1 = _find(doc, "Para one")
            r2 = _find(doc, "Para two")
            assert r1 is not None and r2 is not None
            record_html_atomically(
                session,
                doc,
                lambda: replace_single_range_with_content(doc, r1, "<p>Replaced one</p>", ctx),
            )
            record_html_atomically(
                session,
                doc,
                lambda: replace_single_range_with_content(doc, r2, "<table><tr><td>Cell</td></tr></table>", ctx),
            )
            assert len(session.changes) == 2, session.changes
            resolve_all = accept_all_changes if accept else reject_all_changes
            resolve_all(doc, ctx)
            payload = session.wait_for_review(0)
        assert payload["complete"] is True, payload
        assert [c["outcome"] for c in payload["changes"]] == [expected_outcome, expected_outcome], payload


@native_test
@with_native_doc("writer")
def test_edit_review_formula_only_change_reports_accepted_and_rejected_uno(ctx, doc):
    from plugin.writer.math.math_mml_convert import insert_writer_math_formula

    text = doc.getText()
    for accept, expected_outcome in ((False, "rejected"), (True, "accepted")):
        text.setString("base")
        with EditReviewSession(doc, ctx, enabled=True) as session:
            cur = text.createTextCursor()
            cur.gotoEnd(False)
            session.record_mutation(lambda: insert_writer_math_formula(doc, cur, "x^2", display_block=False))
            assert len(session.changes) == 1
            resolve_all = accept_all_changes if accept else reject_all_changes
            resolve_all(doc, ctx)
            payload = session.wait_for_review(0)
        assert payload["complete"] is True, payload
        assert payload["changes"][0]["outcome"] == expected_outcome, payload


@native_test
@with_native_doc("writer")
def test_refuse_tracked_insert_or_delete_blocks_interior_selection_uno(ctx, doc):
    from plugin.writer.edit_review import TrackedChangesInSelection, refuse_tracked_insert_or_delete

    text = doc.getText()
    text.setString("ABCDE")
    del_cur = text.createTextCursor()
    del_cur.gotoStart(False)
    del_cur.goRight(1, False)
    del_cur.goRight(3, True)
    doc.setPropertyValue("RecordChanges", True)
    try:
        del_cur.setString("")
    finally:
        doc.setPropertyValue("RecordChanges", False)

    inner_del = text.createTextCursor()
    inner_del.gotoStart(False)
    inner_del.goRight(2, False)
    inner_del.goRight(1, True)
    with unittest.TestCase().assertRaises(TrackedChangesInSelection):
        refuse_tracked_insert_or_delete(doc, inner_del)
    assert text.getString() == "ABCDE"

    # Interior selection inside a tracked Insert.
    text.setString("StartEnd")
    ins_cur = text.createTextCursor()
    ins_cur.gotoStart(False)
    ins_cur.goRight(5, False)
    doc.setPropertyValue("RecordChanges", True)
    try:
        text.insertString(ins_cur, "INS", False)
    finally:
        doc.setPropertyValue("RecordChanges", False)

    inner_ins = text.createTextCursor()
    inner_ins.gotoStart(False)
    inner_ins.goRight(6, False)
    inner_ins.goRight(1, True)
    with unittest.TestCase().assertRaises(TrackedChangesInSelection):
        refuse_tracked_insert_or_delete(doc, inner_ins)
    assert text.getString() == "StartINSEnd"

    # Collapsed caret inside an Insert is not refused by refuse_tracked_insert_or_delete.
    caret_ins = text.createTextCursor()
    caret_ins.gotoStart(False)
    caret_ins.goRight(6, False)
    refuse_tracked_insert_or_delete(doc, caret_ins)


@native_test
@with_native_doc("writer")
def test_cross_paragraph_change_reports_accepted_and_rejected_uno(ctx, doc):
    from plugin.doc.text_helpers import normalize_linebreaks

    text = doc.getText()
    for accept, expected_outcome in ((True, "accepted"), (False, "rejected")):
        reset_body(doc, ctx, "Start gone1", "gone2", "gone3 End")
        with EditReviewSession(doc, ctx, enabled=True) as session:
            def mutate():
                cur = text.createTextCursor()
                cur.gotoStart(False)
                text.insertString(cur, "NEW ", False)
                start_hit = _find(doc, "gone1")
                end_hit = _find(doc, "gone3 ")
                assert start_hit is not None and end_hit is not None
                del_cur = text.createTextCursorByRange(start_hit.getStart())
                del_cur.gotoRange(end_hit.getEnd(), True)
                del_cur.setString("")
                cur.gotoEnd(False)
                text.insertString(cur, " MORE", False)

            session.record_mutation(mutate)
            assert len(session.changes) == 1
            change = session.changes[0]
            resolve_all = accept_all_changes if accept else reject_all_changes
            resolve_all(doc, ctx)
            payload = session.wait_for_review(0)
        assert payload["complete"] is True, payload
        assert payload["changes"][0]["outcome"] == expected_outcome, payload
        expected_text = change.accepted_text if accept else change.rejected_text
        assert expected_text == normalize_linebreaks(text.getString())
