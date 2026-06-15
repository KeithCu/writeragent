# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Inline review (model layer): accept/reject the tracked change under the cursor as one unit,
# resolving a replace's delete+insert pair together. Drives native .uno:Accept/RejectTrackedChange
# on a paragraph-wide selection via the view cursor.
import uno  # noqa: F401

from plugin.testing_runner import native_test, setup, teardown
from plugin.tests.testing_utils import TestingFactory
from plugin.writer.edit_review import EditReviewSession
from plugin.writer.inline_review import (
    agent_changes,
    cursor_in_agent_change,
    has_agent_changes,
    resolve_agent_change,
    resolve_all_agent_changes,
    resolve_change_at_cursor,
)

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
    """Reset the document body to the given paragraphs (no tracking)."""
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
    if len(_doc.getRedlines()):
        _accept_all()


def _accept_all():
    helper = _ctx.getServiceManager().createInstanceWithContext("com.sun.star.frame.DispatchHelper", _ctx)
    helper.executeDispatch(_doc.getCurrentController().getFrame(), ".uno:AcceptAllTrackedChanges", "", 0, ())


def _tracked_replace(old, new):
    """Replace the first occurrence of *old* with *new* as a tracked Delete+Insert."""
    text = _doc.getText()
    found = _find(old)
    assert found is not None, "setup: %r not found" % old
    _doc.setPropertyValue("RecordChanges", True)
    c = text.createTextCursorByRange(found)
    c.setString("")
    text.insertString(c, new, False)
    _doc.setPropertyValue("RecordChanges", False)


def _caret_in(needle):
    f = _find(needle)
    assert f is not None, "caret target %r not found" % needle
    _doc.getCurrentController().getViewCursor().gotoRange(f.getStart(), False)


def _para_with(needle):
    f = _find(needle)
    if f is None:
        return None
    t = f.getText().createTextCursorByRange(f.getStart())
    t.gotoStartOfParagraph(False)
    t.gotoEndOfParagraph(True)
    return t.getString()


def _redline_count():
    n = 0
    e = _doc.getRedlines().createEnumeration()
    while e.hasMoreElements():
        e.nextElement()
        n += 1
    return n


@native_test
def test_has_agent_changes_gates_on_agent_tokens_uno():
    _body("Plain paragraph.")
    assert has_agent_changes(_doc) is False, "clean doc has no agent changes"
    _tracked_replace("Plain paragraph.", "Edited paragraph.")  # user-style redline, no token
    assert has_agent_changes(_doc) is False, "the user's own redline must not count as an agent change"
    _agent_edit(("Edited paragraph.", "Agent edited paragraph."))
    assert has_agent_changes(_doc) is True, "an agent (tokened) change must be detected"
    _body("reset")


@native_test
def test_resolve_accept_keeps_new_and_clears_pair_uno():
    _body("This clause is important.")
    _agent_edit(("This clause is important.", "This clause is essential."))
    _caret_in("essential")
    ok, msg = resolve_change_at_cursor(_doc, _ctx, True)
    assert ok, msg
    assert _para_with("clause") == "This clause is essential.", "accept keeps only the new text, got %r" % _para_with("clause")
    assert _redline_count() == 0, "the whole pair (delete+insert) must be resolved, got %d left" % _redline_count()


@native_test
def test_resolve_reject_restores_old_and_clears_pair_uno():
    _body("This clause is important.")
    _agent_edit(("This clause is important.", "This clause is essential."))
    _caret_in("essential")
    ok, msg = resolve_change_at_cursor(_doc, _ctx, False)
    assert ok, msg
    assert _para_with("clause") == "This clause is important.", "reject restores the old text, got %r" % _para_with("clause")
    assert _redline_count() == 0, "reject must clear the pair, got %d left" % _redline_count()


@native_test
def test_resolve_does_nothing_when_cursor_off_change_uno():
    # change in paragraph A, cursor parked in a DIFFERENT paragraph B -> no-op, friendly message
    _body("First paragraph here.", "Second paragraph here.")
    _agent_edit(("First paragraph here.", "First paragraph edited."))
    _caret_in("Second paragraph")
    before = _redline_count()
    ok, msg = resolve_change_at_cursor(_doc, _ctx, True)
    assert ok is False, "cursor not on the change -> should not resolve, msg=%r" % msg
    assert _redline_count() == before, "a no-op must not touch the other paragraph's change"


@native_test
def test_resolve_reports_when_no_changes_uno():
    _body("Nothing tracked here.")
    ok, msg = resolve_change_at_cursor(_doc, _ctx, True)
    assert ok is False and "No agent changes" in msg, msg


@native_test
def test_resolve_ignores_user_own_redline_uno():
    """The user's own tracked change (no session token) is not the agent UI's business."""
    _body("User paragraph here.")
    _tracked_replace("User paragraph here.", "User edited paragraph.")  # untokened
    _caret_in("User edited")
    before = _redline_count()
    ok, msg = resolve_change_at_cursor(_doc, _ctx, True)
    assert ok is False, "a user redline must not be resolved via the agent UI: %r" % msg
    assert _redline_count() == before, "the user's redline must remain pending"
    _body("reset")


# --- token-based agent-change helpers (used by the click popup and the context menu) ------

def _agent_edit(*pairs):
    """Record each (old, new) replace as ONE tagged agent change via EditReviewSession."""
    with EditReviewSession(_doc, _ctx, enabled=True) as session:
        for old, new in pairs:
            session.record_mutation(_tracked_replace_fn(old, new))
    session.cleanup()
    return session


def _tracked_replace_fn(old, new):
    def fn():
        found = _find(old)
        assert found is not None, "agent edit target %r not found" % old
        text = found.getText()
        c = text.createTextCursorByRange(found)
        c.setString("")
        text.insertString(c, new, False)
    return fn


@native_test
def test_agent_changes_lists_per_change_previews_uno():
    _body("Alpha clause one.", "Beta clause two.")
    _agent_edit(("Alpha clause one.", "Alpha EDITED one."), ("Beta clause two.", "Beta EDITED two."))
    changes = agent_changes(_doc)
    assert len(changes) == 2, changes
    assert changes[0]["old"] == "Alpha clause one." and changes[0]["new"] == "Alpha EDITED one.", changes[0]
    assert changes[1]["old"] == "Beta clause two." and changes[1]["new"] == "Beta EDITED two.", changes[1]


@native_test
def test_agent_changes_ignores_user_redlines_uno():
    _body("User paragraph.", "Agent paragraph.")
    _doc.setPropertyValue("RecordChanges", True)
    _tracked_replace_fn("User paragraph.", "User edited.")()
    _doc.setPropertyValue("RecordChanges", False)
    _agent_edit(("Agent paragraph.", "Agent edited."))
    changes = agent_changes(_doc)
    assert len(changes) == 1 and changes[0]["new"] == "Agent edited.", \
        "only the agent's tagged change is listed: %r" % changes
    _body("reset")


@native_test
def test_resolve_agent_change_by_token_resolves_only_that_pair_uno():
    _body("First clause here.", "Second clause here.")
    _agent_edit(("First clause here.", "First EDITED."), ("Second clause here.", "Second EDITED."))
    changes = agent_changes(_doc)
    assert resolve_agent_change(_doc, _ctx, changes[0]["token"], True) is True
    left = agent_changes(_doc)
    assert len(left) == 1 and left[0]["token"] == changes[1]["token"], \
        "only the targeted change resolves: %r" % left
    assert _para_with("First") == "First EDITED.", _para_with("First")
    _body("reset")


@native_test
def test_resolve_all_agent_changes_spares_user_redlines_uno():
    _body("User paragraph.", "Agent one.", "Agent two.")
    _doc.setPropertyValue("RecordChanges", True)
    _tracked_replace_fn("User paragraph.", "User edited.")()
    _doc.setPropertyValue("RecordChanges", False)
    _agent_edit(("Agent one.", "Agent one EDITED."), ("Agent two.", "Agent two EDITED."))
    n = resolve_all_agent_changes(_doc, _ctx, True)
    assert n == 2, "both agent changes resolved, got %d" % n
    assert agent_changes(_doc) == [], "no agent changes left"
    assert _redline_count() > 0, "the user's own redline must remain pending"
    _body("reset")


@native_test
def test_cursor_in_agent_change_detects_token_uno():
    _body("Plain paragraph.", "Target clause here.")
    session = _agent_edit(("Target clause here.", "Target EDITED here."))
    _caret_in("Target EDITED")
    got = cursor_in_agent_change(_doc)
    assert got == session.changes[0].token, \
        "got=%r want=%r changes=%r" % (got, session.changes[0].token, agent_changes(_doc))
    _caret_in("Plain paragraph")
    assert cursor_in_agent_change(_doc) is None, "caret outside the change -> None"
    _body("reset")


@native_test
def test_resolve_refuses_when_user_redline_shares_paragraph_uno():
    """Token-scoping guarantee for the SAME-paragraph case: the paragraph-wide dispatch resolves
    every redline in the selection, so when one of the user's OWN tracked changes shares a
    paragraph with an agent change the inline resolve refuses and touches NOTHING -- the user
    resolves that one via the native UI. (Existing user-redline tests put them in separate
    paragraphs, where the dispatch never reached them.)"""
    _body("The quick brown fox jumps.")
    _tracked_replace("quick", "fast")     # the user's own (untokened) redline ...
    _agent_edit(("fox", "dog"))           # ... and an agent change in the SAME paragraph
    _caret_in("dog")
    before = _redline_count()
    ok, msg = resolve_change_at_cursor(_doc, _ctx, True)
    assert ok is False, "must refuse when a user redline shares the paragraph: %r" % msg
    assert _redline_count() == before, "neither the agent change nor the user's redline may be touched"
    assert has_agent_changes(_doc), "the agent change stays pending for the native UI"
    _body("reset")


@native_test
def test_resolve_refuses_when_another_agent_change_shares_paragraph_uno():
    """Inline "this change" also refuses when another agent token is in the same paragraph.

    The dispatch selection is paragraph-wide, so accepting one token here would accept the other
    token too and misrepresent the command as a single-change action.
    """
    _body("Alpha beta gamma.")
    _agent_edit(("Alpha", "One"), ("gamma", "three"))
    changes = agent_changes(_doc)
    assert len(changes) == 2, changes
    _caret_in("One")
    before = _redline_count()
    ok, msg = resolve_change_at_cursor(_doc, _ctx, True)
    assert ok is False, "must refuse when another agent change shares the paragraph: %r" % msg
    assert _redline_count() == before, "no same-paragraph agent change should be resolved by the single-change UI"
    assert len(agent_changes(_doc)) == 2, "both agent changes stay pending"
    _body("reset")


@native_test
def test_resolve_all_skips_change_sharing_paragraph_with_user_redline_uno():
    """resolve-all must spare a user redline even when it shares a paragraph with an agent change:
    that change is skipped (left for the native UI) while agent changes in clean paragraphs still
    resolve."""
    _body("The quick brown fox jumps.", "Lonely agent clause here.")
    _tracked_replace("quick", "fast")                         # user redline in paragraph 1
    _agent_edit(("fox", "dog"), ("Lonely agent clause here.", "Lonely agent clause EDITED."))
    user_and_agent_before = _redline_count()
    resolved = resolve_all_agent_changes(_doc, _ctx, True)
    assert resolved == 1, "only the clean-paragraph agent change resolves, got %d" % resolved
    assert has_agent_changes(_doc), "the shared-paragraph agent change is left pending"
    assert _redline_count() < user_and_agent_before, "the clean agent change was resolved"
    _body("reset")
