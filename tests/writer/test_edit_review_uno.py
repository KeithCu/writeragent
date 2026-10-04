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
import uno  # noqa: F401

from plugin.testing_runner import native_test
from plugin.tests.testing_utils import with_native_doc
from plugin.writer.edit_review import EditReviewSession, _BOOKMARK_PREFIX

_PARA_BREAK = uno.getConstantByName("com.sun.star.text.ControlCharacter.PARAGRAPH_BREAK")


def _find(doc, needle):
    sd = doc.createSearchDescriptor()
    sd.setSearchString(needle)
    return doc.findFirst(sd)


def _body(doc, ctx, *paragraphs):
    text = doc.getText()
    doc.setPropertyValue("RecordChanges", False)
    cur = text.createTextCursor()
    cur.gotoStart(False)
    cur.gotoEnd(True)
    cur.setString("")
    cur.gotoStart(False)
    for i, para in enumerate(paragraphs):
        if i:
            text.insertControlCharacter(cur, _PARA_BREAK, False)
        text.insertString(cur, para, False)
    if _redlines(doc):
        helper = ctx.getServiceManager().createInstanceWithContext("com.sun.star.frame.DispatchHelper", ctx)
        helper.executeDispatch(doc.getCurrentController().getFrame(), ".uno:AcceptAllTrackedChanges", "", 0, ())


def _redlines(doc):
    out = []
    e = doc.getRedlines().createEnumeration()
    while e.hasMoreElements():
        rl = e.nextElement()
        entry = {"type": rl.getPropertyValue("RedlineType")}
        for prop in ("RedlineComment", "RedlineAuthor"):
            try:
                entry[prop] = str(rl.getPropertyValue(prop))
            except Exception:
                entry[prop] = ""
        out.append(entry)
    return out


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
    helper = ctx.getServiceManager().createInstanceWithContext("com.sun.star.frame.DispatchHelper", ctx)
    command = ".uno:AcceptTrackedChange" if accept else ".uno:RejectTrackedChange"
    helper.executeDispatch(doc.getCurrentController().getFrame(), command, "", 0, ())


def _wa_bookmarks(doc):
    return [n for n in doc.getBookmarks().getElementNames() if n.startswith(_BOOKMARK_PREFIX)]


@native_test
@with_native_doc("writer")
def test_disabled_session_is_inert_uno(ctx, doc):
    _body(doc, ctx, "Alpha paragraph.")
    with EditReviewSession(doc, ctx, enabled=False) as session:
        session.record_mutation(_replace_fn(doc, "Alpha paragraph.", "Alpha edited."))
    assert _redlines(doc) == [], "disabled session must not record redlines"
    assert _find(doc, "Alpha edited.") is not None, "edit applied directly"
    result = session.wait_for_review(timeout=0.1)
    assert result == {"complete": True, "timed_out": False, "changes": []}, result


@native_test
@with_native_doc("writer")
def test_records_tags_author_and_restores_uno(ctx, doc):
    _body(doc, ctx, "Alpha paragraph.")
    doc.setPropertyValue("ShowChanges", False)
    with EditReviewSession(doc, ctx, enabled=True) as session:
        assert doc.getPropertyValue("ShowChanges") is True, "session start must make markup visible"
        session.record_mutation(_replace_fn(doc, "Alpha paragraph.", "Alpha edited."))
    rls = _redlines(doc)
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
    _body(doc, ctx, "This clause is important.")
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
    before = len(_redlines(doc))
    helper = ctx.getServiceManager().createInstanceWithContext("com.sun.star.frame.DispatchHelper", ctx)
    helper.executeDispatch(doc.getCurrentController().getFrame(), ".uno:Undo", "", 0, ())
    assert len(_redlines(doc)) < before, \
        "the first undo must revert the agent edit, not toggle an invisible bookmark (had %d redlines)" % before


@native_test
@with_native_doc("writer")
def test_user_redlines_do_not_block_completion_uno(ctx, doc):
    _body(doc, ctx, "User paragraph.", "Agent paragraph.")
    # the USER has their own pending tracked change before the agent edits
    doc.setPropertyValue("RecordChanges", True)
    _replace_fn(doc, "User paragraph.", "User edited paragraph.")()
    doc.setPropertyValue("RecordChanges", False)
    with EditReviewSession(doc, ctx, enabled=True) as session:
        session.record_mutation(_replace_fn(doc, "Agent paragraph.", "Agent edited paragraph."))
    untagged = [r for r in _redlines(doc) if not r["RedlineComment"].startswith("wa-review:")]
    assert len(untagged) == 2, "the user's own redlines must NOT get the session token: %r" % _redlines(doc)
    _resolve_at(doc, ctx, "Agent edited", True)  # resolve only the agent's change
    result = session.wait_for_review(timeout=2, poll=0.05)
    assert result["complete"] is True, "user's pending redline must not block completion: %r" % result
    assert [r for r in _redlines(doc) if not r["RedlineComment"].startswith("wa-review:")], \
        "the user's redline must still be pending (untouched)"
    _body(doc, ctx, "reset")  # clear the leftover user redline for the next test


@native_test
@with_native_doc("writer")
def test_outcomes_accept_and_reject_per_change_uno(ctx, doc):
    _body(doc, ctx, "First clause here.", "Second clause here.")
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
    _body(doc, ctx, "Stable clause here.")
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
    _body(doc, ctx, "Waiting clause here.")
    with EditReviewSession(doc, ctx, enabled=True) as session:
        session.record_mutation(_replace_fn(doc, "Waiting clause here.", "Waiting clause EDITED."))
    result = session.wait_for_review(timeout=0.3, poll=0.05)  # nobody reviews
    assert result["complete"] is False and result["timed_out"] is True, result
    assert result["changes"][0]["outcome"] == "pending", result
    assert _wa_bookmarks(doc) == [], "bookmarks cleaned even on timeout"
    _body(doc, ctx, "reset")  # clear the unresolved change


@native_test
@with_native_doc("writer")
def test_stop_checker_aborts_wait_uno(ctx, doc):
    _body(doc, ctx, "Abort clause here.")
    with EditReviewSession(doc, ctx, enabled=True) as session:
        session.record_mutation(_replace_fn(doc, "Abort clause here.", "Abort clause EDITED."))
    result = session.wait_for_review(timeout=30, poll=0.05, stop_checker=lambda: True)
    assert result["complete"] is False and result["timed_out"] is False, \
        "stop_checker abort is not a timeout: %r" % result
    _body(doc, ctx, "reset")


@native_test
@with_native_doc("writer")
def test_wait_for_review_routes_uno_via_runner_uno(ctx, doc):
    """Off-main-thread callers (MCP HTTP / chat worker) pass uno_runner=execute_on_main_thread;
    every document touch in the wait loop must flow through it."""
    _body(doc, ctx, "Runner clause here.")
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
    _body(doc, ctx, "Tracked clause here.")
    doc.setPropertyValue("RecordChanges", True)
    with EditReviewSession(doc, ctx, enabled=True) as session:
        session.record_mutation(_replace_fn(doc, "Tracked clause here.", "Tracked clause EDITED."))
    assert doc.getPropertyValue("RecordChanges") is True, "user's ON state must be preserved"
    doc.setPropertyValue("RecordChanges", False)
    assert session.changes, "change recorded under user tracking too"
    session.cleanup()
    _body(doc, ctx, "reset")


@native_test
@with_native_doc("writer")
def test_exception_restores_recording_and_author_uno(ctx, doc):
    _body(doc, ctx, "Crash clause here.")
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
    _body(doc, ctx, "Anchor failure clause here.")
    doc.setPropertyValue("RecordChanges", False)
    with EditReviewSession(doc, ctx, enabled=True) as session:
        def mutate():
            # Apply an edit
            t = doc.getText()
            c = t.createTextCursor()
            c.gotoStart(False)
            c.gotoEndOfParagraph(True)
            c.setString("Edited.")
            # Cause anchor failure by clearing bookmarks
            for name in doc.getBookmarks().getElementNames():
                doc.getBookmarks().getByName(name).dispose()
        session.record_mutation(mutate)

    # Wait should not hang and should not block completion due to the untagged edit
    result = session.wait_for_review(timeout=0.1)
    # The session is fully complete (empty set of changes) since the anchor failed
    assert result == {"complete": True, "timed_out": False, "changes": []}, result
