# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
#
# EditReviewSession: the centralized review story for agent edits. Covers: inert when
# disabled; recording + per-change RedlineComment tokens + author attribution + restore
# semantics; completion keyed to THIS session's tokens (user redlines don't block);
# per-change outcomes accepted/rejected/modified/pending; timeout; stop_checker;
# ShowChanges forced on.
import unittest

import uno  # noqa: F401

from plugin.framework.uno_context import execute_dispatch
from plugin.testing_runner import native_test
from plugin.tests.testing_utils import with_native_doc
from plugin.writer.edit_review import EditReviewSession
from tests.writer.doc_builders import accept_all_changes, find_first, list_redlines, reject_all_changes, reset_body


def _replace_fn(doc, old, new):
    """A mutation callable: replace first occurrence of *old* with *new* (clean delete+insert)."""
    def fn():
        found = find_first(doc, old)
        assert found is not None, "mutation target %r not found" % old
        text = found.getText()
        c = text.createTextCursorByRange(found)
        c.setString("")
        text.insertString(c, new, False)
    return fn


def _resolve_at(doc, ctx, needle, accept):
    """Resolve the tracked change in *needle*'s paragraph, as a user would (select + native
    accept/reject dispatch). Local on purpose: this suite must not depend on the UI helpers."""
    f = find_first(doc, needle)
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


@native_test
@with_native_doc("writer")
def test_disabled_session_is_inert_uno(ctx, doc):
    reset_body(doc, ctx, "Alpha paragraph.")
    with EditReviewSession(doc, ctx, enabled=False) as session:
        session.record_mutation(_replace_fn(doc, "Alpha paragraph.", "Alpha edited."))
    assert list_redlines(doc) == [], "disabled session must not record redlines"
    assert find_first(doc, "Alpha edited.") is not None, "edit applied directly"
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


@native_test
@with_native_doc("writer")
def test_bookkeeping_stays_off_the_undo_stack_uno(ctx, doc):
    """The user's first Ctrl+Z after an agent edit must undo the VISIBLE change, not internal
    bookkeeping. Recording locks the document undo manager around tag bookkeeping, so the
    user's undo stack only holds the real edit."""
    reset_body(doc, ctx, "This clause is important.")
    doc.setPropertyValue("RecordChanges", False)
    with EditReviewSession(doc, ctx, enabled=True) as session:
        session.record_mutation(_replace_fn(doc, "This clause is important.", "This clause is essential."))
    um = doc.getUndoManager()
    assert um.isUndoPossible(), "the agent edit must be undoable"
    title = um.getCurrentUndoActionTitle()
    assert "bookmark" not in title.lower(), \
        "internal bookkeeping leaked onto the user's undo stack: top=%r" % title
    # One undo must revert the edit (drop a redline), not no-op on bookkeeping.
    before = len(list_redlines(doc))
    execute_dispatch(ctx, doc.getCurrentController(), ".uno:Undo")
    assert len(list_redlines(doc)) < before, \
        "the first undo must revert the agent edit (had %d redlines)" % before


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


@native_test
@with_native_doc("writer")
def test_outcome_modified_when_user_edits_during_review_uno(ctx, doc):
    reset_body(doc, ctx, "Stable clause here.")
    with EditReviewSession(doc, ctx, enabled=True) as session:
        session.record_mutation(_replace_fn(doc, "Stable clause here.", "Stable clause EDITED."))
    _resolve_at(doc, ctx, "Stable clause EDITED", True)
    # the user reworks the paragraph after resolving (tracking off = silent manual edit)
    f = find_first(doc, "Stable clause EDITED.")
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
    assert calls["n"] >= 2, "pending check and payload must go through the runner, got %d" % calls["n"]


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

        with unittest.mock.patch(
            "plugin.writer.edit_review._string_skipping_redline",
            side_effect=RuntimeError("mock anchor fail"),
        ):
            session.record_mutation(mutate)

    result = session.wait_for_review(timeout=0.1)
    assert result == {"complete": True, "timed_out": False, "changes": []}, result


@native_test
@with_native_doc("writer")
def test_close_during_wait_returns_pending_without_crash_uno(ctx, doc):
    from plugin.framework.errors import document_is_disposed
    from plugin.tests.testing_utils import TestingFactory

    closable = TestingFactory.create_native_doc(ctx, doc_type="writer", hidden=True)
    try:
        reset_body(closable, ctx, "Alpha two three.")
        with EditReviewSession(closable, ctx, enabled=True) as session:
            session.record_mutation(_replace_fn(closable, "two", "2"))
        closable.close(True)
        result = session.wait_for_review(timeout=5, poll=0.05)
        assert result["complete"] is False and result["timed_out"] is False, result
        assert [c["outcome"] for c in result["changes"]] == ["pending"], result
        assert result["changes"][0]["final_text"] == "", result
        assert document_is_disposed(closable) is True
    finally:
        if not document_is_disposed(closable):
            closable.close(True)
    reset_body(doc, ctx, "Still alive.")
    assert find_first(doc, "Still alive.") is not None


@native_test
@with_native_doc("writer")
def test_undo_after_resolve_in_wait_does_not_crash_uno(ctx, doc):
    for accept, expected_body in ((True, "Alpha 2 three."), (False, "Alpha two three.")):
        reset_body(doc, ctx, "Alpha two three.")
        with EditReviewSession(doc, ctx, enabled=True) as session:
            session.record_mutation(_replace_fn(doc, "two", "2"))
        (accept_all_changes if accept else reject_all_changes)(doc, ctx)
        result = session.wait_for_review(timeout=2, poll=0.05)
        assert result["complete"] is True, result
        assert doc.getText().getString() == expected_body
        doc.getUndoManager().undo()
        assert {r["type"] for r in list_redlines(doc)} == {"Delete", "Insert"}
        accept_all_changes(doc, ctx)
        assert doc.getText().getString() == "Alpha 2 three."


@native_test
@with_native_doc("writer")
def test_stream_chunk_and_finish_after_close_do_not_crash_uno(ctx, doc):
    from plugin.framework.errors import DocumentDisposedError, document_is_disposed
    from plugin.tests.testing_utils import TestingFactory
    from plugin.writer.edit_review import WriterStreamedRewriteSession

    closable = TestingFactory.create_native_doc(ctx, doc_type="writer", hidden=True)
    try:
        reset_body(closable, ctx, "Original text.")
        rng = find_first(closable, "Original text.")
        session = WriterStreamedRewriteSession(closable, rng, "Original text.", track_reviewable=True)
        session.append_chunk("2")
        closable.close(True)
        session.append_chunk(" more")
        assert session.last_write_error is None
        with unittest.TestCase().assertRaises(DocumentDisposedError):
            session.finish()
    finally:
        if not document_is_disposed(closable):
            closable.close(True)
    reset_body(doc, ctx, "Still alive.")
    assert find_first(doc, "Still alive.") is not None


@native_test
@with_native_doc("writer")
def test_undo_agent_edit_during_review_reports_rejected_uno(ctx, doc):
    reset_body(doc, ctx, "Alpha two three.")
    with EditReviewSession(doc, ctx, enabled=True) as session:
        session.record_mutation(_replace_fn(doc, "two", "2"))
    doc.getUndoManager().undo()
    result = session.wait_for_review(timeout=2, poll=0.05)
    assert doc.getText().getString() == "Alpha two three."
    assert result["complete"] is True, result
    assert result["changes"][0]["outcome"] == "rejected", result
    assert result["changes"][0]["final_text"] == "two", result


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
            r1 = find_first(doc, "Para one")
            r2 = find_first(doc, "Para two")
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
    from plugin.writer.edit_review import TrackedChangesInSelection, refuse_streamed_selection

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
        refuse_streamed_selection(doc, inner_del)
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
        refuse_streamed_selection(doc, inner_ins)
    assert text.getString() == "StartINSEnd"

    # Collapsed caret inside an Insert is not refused by refuse_streamed_selection.
    caret_ins = text.createTextCursor()
    caret_ins.gotoStart(False)
    caret_ins.goRight(6, False)
    refuse_streamed_selection(doc, caret_ins)


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
                start_hit = find_first(doc, "gone1")
                end_hit = find_first(doc, "gone3 ")
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


@native_test
@with_native_doc("writer")
def test_edited_context_leaves_out_pending_deletions_uno(ctx, doc):
    """edited_context reads as after Accept: neither the replaced text nor another author's deletion shows."""
    from plugin.tests.testing_utils import native_writer_tool_context
    from plugin.writer.apply_document_content import ApplyDocumentContent
    from tests.writer.doc_builders import strike_substring

    reset_body(doc, ctx, "Intro keeps DELINLINE word.", "Third paragraph.", "Tail.")
    strike_substring(doc, doc.getText(), "DELINLINE ", ctx=ctx, author="Alice")
    doc.setPropertyValue("RecordChanges", True)
    try:
        res = ApplyDocumentContent().execute(
            native_writer_tool_context(doc, ctx), target="search", old_content="Third", content="3rd")
    finally:
        doc.setPropertyValue("RecordChanges", False)
    assert res.get("tracked") is True, res
    assert res["edited_context"] == "Intro keeps word.\n3rd paragraph.\nTail.", res


def _shape(doc):
    """Snapshot paragraph styles, adjustments, bold runs, and embedded object counts."""
    from com.sun.star.awt.FontWeight import BOLD  # type: ignore
    from plugin.framework.uno_context import enum_has_more

    paras = []
    enum = doc.getText().createEnumeration()
    while enum_has_more(enum):
        el = enum.nextElement()
        if el.supportsService("com.sun.star.text.TextTable"):
            paras.append(("TABLE", el.getName(), 0, ()))
            continue
        bolds = []
        p_enum = el.createEnumeration()
        while enum_has_more(p_enum):
            part = p_enum.nextElement()
            if part.getPropertyValue("TextPortionType") == "Text":
                if part.getPropertyValue("CharWeight") == BOLD:
                    bolds.append(part.getString())
        paras.append((
            el.getString(),
            el.getPropertyValue("ParaStyleName"),
            el.getPropertyValue("ParaAdjust"),
            tuple(bolds),
        ))
    return {
        "paras": paras,
        "footnotes": doc.getFootnotes().getCount(),
        "fields": len(list(_iter_fields(doc))),
        "frames": doc.getTextFrames().getCount(),
        "bookmarks": doc.getBookmarks().getCount(),
        "tables": doc.getTextTables().getCount(),
    }


def _iter_fields(doc):
    from plugin.framework.uno_context import enum_has_more

    enum = doc.getTextFields().createEnumeration()
    while enum_has_more(enum):
        yield enum.nextElement()


def _build_multi_para(doc, ctx):
    from com.sun.star.awt.FontWeight import BOLD  # type: ignore
    from com.sun.star.style.ParagraphAdjust import CENTER  # type: ignore

    reset_body(doc, ctx, "Intro para one.", "Second quote.", "Third para centered.", "Outro para.")
    text = doc.getText()
    c2 = find_first(doc, "Second quote.")
    assert c2 is not None
    c2.setPropertyValue("ParaStyleName", "Quotations")
    c3 = find_first(doc, "Third para centered.")
    assert c3 is not None
    c3.setPropertyValue("ParaAdjust", CENTER)
    bw = find_first(doc, "quote")
    assert bw is not None
    bw.setPropertyValue("CharWeight", BOLD)
    start = find_first(doc, "para one.")
    end = find_first(doc, "Third para")
    assert start is not None and end is not None
    rng = text.createTextCursorByRange(start.getStart())
    rng.gotoRange(end.getEnd(), True)
    return rng


@native_test
@with_native_doc("writer")
def test_streamed_rewrite_multi_paragraph_reject_restores_uno(ctx, doc):
    from plugin.writer.edit_review import WriterStreamedRewriteSession

    for track_reviewable, rec in ((True, False), (False, True)):
        rng = _build_multi_para(doc, ctx)
        orig_shape = _shape(doc)
        doc.setPropertyValue("RecordChanges", rec)
        try:
            session = WriterStreamedRewriteSession(doc, rng, rng.getString(), track_reviewable=track_reviewable)
            session.append_chunk("Replaced ")
            session.append_chunk("span")
            assert session.finish() is None
            reject_all_changes(doc, ctx)
            assert _shape(doc) == orig_shape
        finally:
            doc.setPropertyValue("RecordChanges", False)

    # Compare Accept against untracked twin.
    rng_twin = _build_multi_para(doc, ctx)
    s_twin = WriterStreamedRewriteSession(doc, rng_twin, rng_twin.getString(), track_reviewable=False)
    s_twin.append_chunk("Replaced span")
    assert s_twin.finish() is None
    twin_text = doc.getText().getString()

    rng_acc = _build_multi_para(doc, ctx)
    s_acc = WriterStreamedRewriteSession(doc, rng_acc, rng_acc.getString(), track_reviewable=True)
    s_acc.append_chunk("Replaced span")
    assert s_acc.finish() is None
    accept_all_changes(doc, ctx)
    assert doc.getText().getString() == twin_text


@native_test
@with_native_doc("writer")
def test_streamed_rewrite_objects_reject_restores_uno(ctx, doc):
    from com.sun.star.awt.FontWeight import BOLD  # type: ignore
    from com.sun.star.text.TextContentAnchorType import AS_CHARACTER  # type: ignore
    from plugin.writer.edit_review import WriterStreamedRewriteSession

    reset_body(doc, ctx, "Before link text bold after.")
    text = doc.getText()
    cur = text.createTextCursor()
    cur.gotoStart(False)
    cur.goRight(7, False)
    fn = doc.createInstance("com.sun.star.text.Footnote")
    fn.setLabel("1")
    text.insertTextContent(cur, fn, False)
    fld = doc.createInstance("com.sun.star.text.TextField.PageNumber")
    text.insertTextContent(cur, fld, False)
    fr = doc.createInstance("com.sun.star.text.TextFrame")
    fr.AnchorType = AS_CHARACTER
    fr.Width = 1000
    fr.Height = 500
    text.insertTextContent(cur, fr, False)
    bm = doc.createInstance("com.sun.star.text.Bookmark")
    bm.Name = "bm_obj"
    text.insertTextContent(cur, bm, False)
    ann = doc.createInstance("com.sun.star.text.TextField.Annotation")
    ann.Content = "note"
    text.insertTextContent(cur, ann, False)

    link_rng = find_first(doc, "link")
    assert link_rng is not None
    link_rng.setPropertyValue("HyperLinkURL", "https://example.org/")
    bold_rng = find_first(doc, "bold")
    assert bold_rng is not None
    bold_rng.setPropertyValue("CharWeight", BOLD)

    orig_shape = _shape(doc)
    sel = text.createTextCursor()
    sel.gotoStart(False)
    sel.gotoEnd(True)
    session = WriterStreamedRewriteSession(doc, sel, sel.getString(), track_reviewable=True)
    session.append_chunk("Clean text")
    assert session.finish() is None
    reject_all_changes(doc, ctx)
    assert _shape(doc) == orig_shape
    link_after = find_first(doc, "link")
    assert link_after is not None
    assert link_after.getPropertyValue("HyperLinkURL") == "https://example.org/"

    # Bold-run selection at paragraph start: Accept keeps bold like the untracked twin.
    reset_body(doc, ctx, "bold after.")
    b_sel = find_first(doc, "bold")
    assert b_sel is not None
    b_sel.setPropertyValue("CharWeight", BOLD)
    s_rec = WriterStreamedRewriteSession(doc, b_sel, b_sel.getString(), track_reviewable=True)
    s_rec.append_chunk("strong")
    assert s_rec.finish() is None
    accept_all_changes(doc, ctx)
    strong_rng = find_first(doc, "strong")
    assert strong_rng is not None
    rec_weight = strong_rng.getPropertyValue("CharWeight")

    reset_body(doc, ctx, "bold after.")
    bold_twin = find_first(doc, "bold")
    assert bold_twin is not None
    bold_twin.setPropertyValue("CharWeight", BOLD)
    s_twin = WriterStreamedRewriteSession(doc, bold_twin, bold_twin.getString(), track_reviewable=False)
    s_twin.append_chunk("strong")
    assert s_twin.finish() is None
    twin_rng = find_first(doc, "strong")
    assert twin_rng is not None
    assert rec_weight == BOLD and rec_weight == twin_rng.getPropertyValue("CharWeight")


@native_test
@with_native_doc("writer")
def test_streamed_append_never_writes_selection_uno(ctx, doc):
    from plugin.writer.edit_review import WriterStreamedAppendSession

    for track_reviewable, rec in ((True, False), (False, True), (False, False)):
        rng = _build_multi_para(doc, ctx)
        orig_shape = _shape(doc)
        doc.setPropertyValue("RecordChanges", rec)
        try:
            session = WriterStreamedAppendSession(doc, rng, rng.getString(), track_reviewable=track_reviewable)
            session.append_chunk(" MORE")
            assert session.finish() is None
            assert "Third para MORE" in doc.getText().getString()
            if track_reviewable or rec:
                reject_all_changes(doc, ctx)
                assert _shape(doc) == orig_shape
            else:
                assert _shape(doc)["paras"][1][1] == "Quotations"
                assert _shape(doc)["paras"][1][3] == ("quote",)
        finally:
            doc.setPropertyValue("RecordChanges", False)


@native_test
@with_native_doc("writer")
def test_streamed_sessions_refuse_table_and_any_redline_uno(ctx, doc):
    from plugin.writer.edit_review import (
        TableInSelection,
        TrackedChangesInSelection,
        WriterStreamedAppendSession,
        WriterStreamedRewriteSession,
    )

    reset_body(doc, ctx, "Before table", "After table")
    text = doc.getText()
    tbl = doc.createInstance("com.sun.star.text.TextTable")
    tbl.initialize(2, 2)
    cur = find_first(doc, "After table")
    assert cur is not None
    text.insertTextContent(cur.getStart(), tbl, False)

    whole = text.createTextCursor()
    whole.gotoStart(False)
    whole.gotoEnd(True)
    undo_before = doc.getUndoManager().getAllUndoActionTitles()
    for cls in (WriterStreamedRewriteSession, WriterStreamedAppendSession):
        with unittest.TestCase().assertRaises(TableInSelection):
            cls(doc, whole, whole.getString(), track_reviewable=True)

    # Partial body selection spanning the table (non-whole-body range).
    before_hit = find_first(doc, "Before table")
    after_hit = find_first(doc, "After table")
    assert before_hit is not None and after_hit is not None
    span_cur = text.createTextCursorByRange(before_hit.getStart())
    span_cur.gotoRange(after_hit.getEnd(), True)
    for cls in (WriterStreamedRewriteSession, WriterStreamedAppendSession):
        with unittest.TestCase().assertRaises(TableInSelection):
            cls(doc, span_cur, span_cur.getString(), track_reviewable=True)

    assert doc.getTextTables().getCount() == 1
    assert doc.getUndoManager().getAllUndoActionTitles() == undo_before
    assert doc.getPropertyValue("RecordChanges") is False

    # Format redline inside selection is refused for both sessions.
    reset_body(doc, ctx, "Plain formatted text.")
    fmt = find_first(doc, "formatted")
    assert fmt is not None
    doc.setPropertyValue("RecordChanges", True)
    try:
        fmt.setPropertyValue("CharWeight", 150.0)
    finally:
        doc.setPropertyValue("RecordChanges", False)
    sel = text.createTextCursor()
    sel.gotoStart(False)
    sel.gotoEnd(True)
    for cls in (WriterStreamedRewriteSession, WriterStreamedAppendSession):
        with unittest.TestCase().assertRaises(TrackedChangesInSelection):
            cls(doc, sel, sel.getString())
    assert [r["type"] for r in list_redlines(doc)] == ["Format"]


@native_test
@with_native_doc("writer")
def test_streamed_rewrite_preserve_breaks_untracked_uno(ctx, doc):
    from plugin.writer.edit_review import WriterStreamedRewriteSession

    reset_body(doc, ctx, "Old one.", "Old two.")
    text = doc.getText()
    sel = text.createTextCursor()
    sel.gotoStart(False)
    sel.gotoEnd(True)
    session = WriterStreamedRewriteSession(
        doc, sel, sel.getString(), track_reviewable=False, preserve_paragraph_breaks=True,
    )
    session.append_chunk("New one.\nNew two.\nNew three\nNew tail.")
    assert session.finish() is None
    assert [p[0] for p in _shape(doc)["paras"]] == ["New one.", "New two.", "New three", "New tail."]
    assert list_redlines(doc) == []
    doc.getUndoManager().undo()
    assert [p[0] for p in _shape(doc)["paras"]] == ["Old one.", "Old two."]


@native_test
@with_native_doc("writer")
def test_streamed_rewrite_scaffold_only_output_leaves_document_uno(ctx, doc):
    from plugin.writer.edit_review import WriterStreamedRewriteSession

    for track_reviewable in (False, True):
        reset_body(doc, ctx, "Untouched text.")
        text = doc.getText()
        sel = text.createTextCursor()
        sel.gotoStart(False)
        sel.gotoEnd(True)
        undo_before = doc.getUndoManager().getAllUndoActionTitles()
        session = WriterStreamedRewriteSession(doc, sel, sel.getString(), track_reviewable=track_reviewable)
        session.append_chunk("END OF ")
        session.append_chunk("DOCUMENT")
        assert session.finish() is None
        assert text.getString() == "Untouched text."
        assert list_redlines(doc) == []
        assert doc.getUndoManager().getAllUndoActionTitles() == undo_before


@native_test
@with_native_doc("writer")
def test_streamed_rewrite_user_typing_midstream_is_recorded_uno(ctx, doc):
    from plugin.writer.edit_review import WriterStreamedRewriteSession
    from plugin.writer.review_scan import is_agent_token

    reset_body(doc, ctx, "Target line.", "Outro para.")
    text = doc.getText()
    sel = find_first(doc, "Target line.")
    assert sel is not None
    doc.setPropertyValue("RecordChanges", True)
    try:
        session = WriterStreamedRewriteSession(doc, sel, sel.getString(), track_reviewable=False)
        session.append_chunk("First ")
        assert doc.getPropertyValue("RecordChanges") is True
        end_cur = text.createTextCursor()
        end_cur.gotoEnd(False)
        text.insertString(end_cur, " USERTYPED", False)
        session.append_chunk("Second")
        assert session.finish() is None
        assert "Outro para. USERTYPED" in text.getString()
        redlines = doc.getRedlines()
        insert_texts = []
        for idx in range(redlines.getCount()):
            rl = redlines.getByIndex(idx)
            if rl.getPropertyValue("RedlineType") == "Insert":
                rs = rl.getPropertyValue("RedlineStart")
                re_end = rl.getPropertyValue("RedlineEnd")
                rcur = rs.getText().createTextCursorByRange(rs)
                rcur.gotoRange(re_end, True)
                insert_texts.append(rcur.getString())
        assert " USERTYPED" in insert_texts
    finally:
        doc.setPropertyValue("RecordChanges", False)

    # Review Record + user toggles Record on mid-stream: stays on, one agent token, Reject restores.
    reset_body(doc, ctx, "Target line.", "Outro para.")
    sel2 = find_first(doc, "Target line.")
    assert sel2 is not None
    session2 = WriterStreamedRewriteSession(doc, sel2, sel2.getString(), track_reviewable=True)
    session2.append_chunk("One ")
    doc.setPropertyValue("RecordChanges", True)
    try:
        session2.append_chunk("Two")
        assert session2.finish() is None
        assert doc.getPropertyValue("RecordChanges") is True
        redlines = list_redlines(doc)
        comments = {r["RedlineComment"] for r in redlines}
        assert len(comments) == 1 and all(is_agent_token(c) for c in comments)
        reject_all_changes(doc, ctx)
        assert [p[0] for p in _shape(doc)["paras"]] == ["Target line.", "Outro para."]
    finally:
        doc.setPropertyValue("RecordChanges", False)


@native_test
@with_native_doc("writer")
def test_streamed_rewrite_abort_clears_redo_uno(ctx, doc):
    from plugin.writer.edit_review import WriterStreamedRewriteSession

    reset_body(doc, ctx, "Original text.")
    text = doc.getText()
    sel = text.createTextCursor()
    sel.gotoStart(False)
    sel.gotoEnd(True)
    session = WriterStreamedRewriteSession(doc, sel, sel.getString(), track_reviewable=True)
    session.append_chunk("One ")
    session.append_chunk("Two")
    session.abort_and_restore()
    assert text.getString() == "Original text."
    assert doc.getUndoManager().isRedoPossible() is False
