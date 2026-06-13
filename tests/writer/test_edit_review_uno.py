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

from plugin.testing_runner import native_test, setup, teardown
from plugin.tests.testing_utils import TestingFactory
from plugin.writer.edit_review import EditReviewSession, _BOOKMARK_PREFIX

_PARA_BREAK = uno.getConstantByName("com.sun.star.text.ControlCharacter.PARAGRAPH_BREAK")

_doc = None
_ctx = None


@setup
def my_setup(ctx):
    global _doc, _ctx
    _ctx = ctx
    _doc = TestingFactory.create_native_doc(ctx, doc_type="writer", hidden=True)


@teardown
def my_teardown(ctx):
    global _doc
    if _doc:
        _doc.close(True)
    _doc = None


def _find(needle):
    sd = _doc.createSearchDescriptor()
    sd.setSearchString(needle)
    return _doc.findFirst(sd)


def _body(*paragraphs):
    text = _doc.getText()
    _doc.setPropertyValue("RecordChanges", False)
    cur = text.createTextCursor()
    cur.gotoStart(False)
    cur.gotoEnd(True)
    cur.setString("")
    cur.gotoStart(False)
    for i, para in enumerate(paragraphs):
        if i:
            text.insertControlCharacter(cur, _PARA_BREAK, False)
        text.insertString(cur, para, False)
    if _redlines():
        helper = _ctx.getServiceManager().createInstanceWithContext("com.sun.star.frame.DispatchHelper", _ctx)
        helper.executeDispatch(_doc.getCurrentController().getFrame(), ".uno:AcceptAllTrackedChanges", "", 0, ())


def _redlines():
    out = []
    e = _doc.getRedlines().createEnumeration()
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


def _replace_fn(old, new):
    """A mutation callable: replace first occurrence of *old* with *new* (clean delete+insert)."""
    def fn():
        found = _find(old)
        assert found is not None, "mutation target %r not found" % old
        text = found.getText()
        c = text.createTextCursorByRange(found)
        c.setString("")
        text.insertString(c, new, False)
    return fn


def _resolve_at(needle, accept):
    """Resolve the tracked change in *needle*'s paragraph, as a user would (select + native
    accept/reject dispatch). Local on purpose: this suite must not depend on the UI helpers."""
    f = _find(needle)
    assert f is not None, "resolve target %r not found" % needle
    text = f.getText()
    para = text.createTextCursorByRange(f.getStart())
    para.gotoStartOfParagraph(False)
    para.gotoEndOfParagraph(True)
    view_cursor = _doc.getCurrentController().getViewCursor()
    view_cursor.gotoRange(para.getStart(), False)
    view_cursor.gotoRange(para.getEnd(), True)
    helper = _ctx.getServiceManager().createInstanceWithContext("com.sun.star.frame.DispatchHelper", _ctx)
    command = ".uno:AcceptTrackedChange" if accept else ".uno:RejectTrackedChange"
    helper.executeDispatch(_doc.getCurrentController().getFrame(), command, "", 0, ())


def _wa_bookmarks():
    return [n for n in _doc.getBookmarks().getElementNames() if n.startswith(_BOOKMARK_PREFIX)]


@native_test
def test_disabled_session_is_inert_uno():
    _body("Alpha paragraph.")
    with EditReviewSession(_doc, _ctx, enabled=False) as session:
        session.record_mutation(_replace_fn("Alpha paragraph.", "Alpha edited."))
    assert _redlines() == [], "disabled session must not record redlines"
    assert _find("Alpha edited.") is not None, "edit applied directly"
    result = session.wait_for_review(timeout=0.1)
    assert result == {"complete": True, "timed_out": False, "changes": []}, result


@native_test
def test_records_tags_author_and_restores_uno():
    _body("Alpha paragraph.")
    _doc.setPropertyValue("ShowChanges", False)
    with EditReviewSession(_doc, _ctx, enabled=True) as session:
        assert _doc.getPropertyValue("ShowChanges") is True, "session start must make markup visible"
        session.record_mutation(_replace_fn("Alpha paragraph.", "Alpha edited."))
    rls = _redlines()
    assert len(rls) == 2, "a replace records a Delete+Insert pair, got %r" % rls
    token = session.changes[0].token
    assert token.startswith("wa-review:"), token
    assert all(r["RedlineComment"] == token for r in rls), "BOTH redlines carry the change token: %r" % rls
    assert all(r["RedlineAuthor"] == "WriterAgent" for r in rls), "agent attribution: %r" % rls
    assert _doc.getPropertyValue("RecordChanges") is False, "prior OFF state restored"
    session.cleanup()


@native_test
def test_bookkeeping_stays_off_the_undo_stack_uno():
    """The user's first Ctrl+Z after an agent edit must undo the VISIBLE change, not toggle our
    invisible wa_review anchor bookmarks. Recording + cleanup lock the document undo manager
    around the tag/bookmark bookkeeping, so the user's undo stack only holds the real edit."""
    _body("This clause is important.")
    _doc.setPropertyValue("RecordChanges", False)
    with EditReviewSession(_doc, _ctx, enabled=True) as session:
        session.record_mutation(_replace_fn("This clause is important.", "This clause is essential."))
    session.cleanup()
    assert _wa_bookmarks() == [], "cleanup must leave no anchor bookmarks behind"
    um = _doc.getUndoManager()
    assert um.isUndoPossible(), "the agent edit must be undoable"
    title = um.getCurrentUndoActionTitle()
    assert "bookmark" not in title.lower() and _BOOKMARK_PREFIX not in title, \
        "internal bookkeeping leaked onto the user's undo stack: top=%r" % title
    # One undo must revert the edit (drop a redline), not no-op on an invisible bookmark.
    before = len(_redlines())
    helper = _ctx.getServiceManager().createInstanceWithContext("com.sun.star.frame.DispatchHelper", _ctx)
    helper.executeDispatch(_doc.getCurrentController().getFrame(), ".uno:Undo", "", 0, ())
    assert len(_redlines()) < before, \
        "the first undo must revert the agent edit, not toggle an invisible bookmark (had %d redlines)" % before


@native_test
def test_user_redlines_do_not_block_completion_uno():
    _body("User paragraph.", "Agent paragraph.")
    # the USER has their own pending tracked change before the agent edits
    _doc.setPropertyValue("RecordChanges", True)
    _replace_fn("User paragraph.", "User edited paragraph.")()
    _doc.setPropertyValue("RecordChanges", False)
    with EditReviewSession(_doc, _ctx, enabled=True) as session:
        session.record_mutation(_replace_fn("Agent paragraph.", "Agent edited paragraph."))
    untagged = [r for r in _redlines() if not r["RedlineComment"].startswith("wa-review:")]
    assert len(untagged) == 2, "the user's own redlines must NOT get the session token: %r" % _redlines()
    _resolve_at("Agent edited", True)  # resolve only the agent's change
    result = session.wait_for_review(timeout=2, poll=0.05)
    assert result["complete"] is True, "user's pending redline must not block completion: %r" % result
    assert [r for r in _redlines() if not r["RedlineComment"].startswith("wa-review:")], \
        "the user's redline must still be pending (untouched)"
    _body("reset")  # clear the leftover user redline for the next test


@native_test
def test_outcomes_accept_and_reject_per_change_uno():
    _body("First clause here.", "Second clause here.")
    with EditReviewSession(_doc, _ctx, enabled=True) as session:
        session.record_mutation(_replace_fn("First clause here.", "First clause EDITED."),
                                original_preview="First clause here.", proposed_preview="First clause EDITED.")
        session.record_mutation(_replace_fn("Second clause here.", "Second clause EDITED."))
    _resolve_at("First clause EDITED", True)    # accept change #1
    _resolve_at("Second clause EDITED", False)  # reject change #2
    result = session.wait_for_review(timeout=2, poll=0.05)
    assert result["complete"] is True and result["timed_out"] is False, result
    outcomes = [c["outcome"] for c in result["changes"]]
    assert outcomes == ["accepted", "rejected"], "per-change outcomes: %r" % result
    assert result["changes"][0]["original_preview"] == "First clause here."
    assert result["changes"][0]["proposed_preview"] == "First clause EDITED."
    assert _wa_bookmarks() == [], "anchor bookmarks must be cleaned after review"


@native_test
def test_outcome_modified_when_user_edits_during_review_uno():
    _body("Stable clause here.")
    with EditReviewSession(_doc, _ctx, enabled=True) as session:
        session.record_mutation(_replace_fn("Stable clause here.", "Stable clause EDITED."))
    _resolve_at("Stable clause EDITED", True)
    # the user reworks the paragraph after resolving (tracking off = silent manual edit)
    f = _find("Stable clause EDITED.")
    c = f.getText().createTextCursorByRange(f)
    c.setString("Something else entirely.")
    result = session.wait_for_review(timeout=2, poll=0.05)
    assert result["changes"][0]["outcome"] == "modified", \
        "user edit during review must report modified, got %r" % result


@native_test
def test_timeout_reports_pending_uno():
    _body("Waiting clause here.")
    with EditReviewSession(_doc, _ctx, enabled=True) as session:
        session.record_mutation(_replace_fn("Waiting clause here.", "Waiting clause EDITED."))
    result = session.wait_for_review(timeout=0.3, poll=0.05)  # nobody reviews
    assert result["complete"] is False and result["timed_out"] is True, result
    assert result["changes"][0]["outcome"] == "pending", result
    assert _wa_bookmarks() == [], "bookmarks cleaned even on timeout"
    _body("reset")  # clear the unresolved change


@native_test
def test_stop_checker_aborts_wait_uno():
    _body("Abort clause here.")
    with EditReviewSession(_doc, _ctx, enabled=True) as session:
        session.record_mutation(_replace_fn("Abort clause here.", "Abort clause EDITED."))
    result = session.wait_for_review(timeout=30, poll=0.05, stop_checker=lambda: True)
    assert result["complete"] is False and result["timed_out"] is False, \
        "stop_checker abort is not a timeout: %r" % result
    _body("reset")


@native_test
def test_wait_for_review_routes_uno_via_runner_uno():
    """Off-main-thread callers (MCP HTTP / chat worker) pass uno_runner=execute_on_main_thread;
    every document touch in the wait loop must flow through it."""
    _body("Runner clause here.")
    with EditReviewSession(_doc, _ctx, enabled=True) as session:
        session.record_mutation(_replace_fn("Runner clause here.", "Runner clause EDITED."))
    _resolve_at("Runner clause EDITED", True)
    calls = {"n": 0}

    def runner(fn):
        calls["n"] += 1
        return fn()

    result = session.wait_for_review(timeout=2, poll=0.05, uno_runner=runner)
    assert result["complete"] is True and result["changes"][0]["outcome"] == "accepted", result
    assert calls["n"] >= 3, "pending check, payload, and cleanup must go through the runner, got %d" % calls["n"]


@native_test
def test_prior_recording_on_is_preserved_uno():
    _body("Tracked clause here.")
    _doc.setPropertyValue("RecordChanges", True)
    with EditReviewSession(_doc, _ctx, enabled=True) as session:
        session.record_mutation(_replace_fn("Tracked clause here.", "Tracked clause EDITED."))
    assert _doc.getPropertyValue("RecordChanges") is True, "user's ON state must be preserved"
    _doc.setPropertyValue("RecordChanges", False)
    assert session.changes, "change recorded under user tracking too"
    session.cleanup()
    _body("reset")


@native_test
def test_exception_restores_recording_and_author_uno():
    _body("Crash clause here.")
    try:
        with EditReviewSession(_doc, _ctx, enabled=True):
            assert _doc.getPropertyValue("RecordChanges") is True
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert _doc.getPropertyValue("RecordChanges") is False, "recording restored on exception"
