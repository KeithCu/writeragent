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
    _change_bounds,
    agent_changes,
    cursor_in_agent_change,
    goto_adjacent_agent_change,
    has_agent_changes,
    pending_agent_change_count,
    resolve_agent_change,
    resolve_all_agent_changes,
    resolve_all_with_feedback,
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
def test_resolve_all_counts_siblings_cleared_together_uno():
    """resolve-all must count EVERY agent change that disappears, even when one paragraph-wide
    dispatch clears several siblings in the same clean paragraph at once (a user redline elsewhere
    forces the per-change path). The old +1-per-pass undercounted -> false 'Resolved 1 of 2'.
    (ported from feat 4c7afb98; more relevant now that an edit is split into several.)"""
    _body("The quick brown fox.", "Lonely user paragraph.")
    _agent_edit(("quick", "fast"), ("fox", "dog"))                     # two agent changes, SAME para 1
    _tracked_replace("Lonely user paragraph.", "Lonely user EDITED.")  # user redline in para 2 -> mixed path
    n = resolve_all_agent_changes(_doc, _ctx, True)
    assert n == 2, "both same-paragraph agent changes must be counted, got %d" % n
    assert not has_agent_changes(_doc), "no agent change left pending"
    _body("reset")


@native_test
def test_resolve_all_global_path_when_only_agent_redlines_uno():
    """Gate: a document holding ONLY agent changes (no user redlines) takes the global
    AcceptAll fast path (_all_redlines_are_agent True) -- every change resolves and counts, leaving
    zero redlines. The mixed-redline per-change loop is covered by the *spares_user_redlines* tests;
    this pins the OTHER branch and that its (now reliability-checked) count is honest."""
    _body("The quick brown fox.", "Another clean clause here.")
    _agent_edit(("quick", "fast"), ("Another clean clause here.", "Another EDITED clause here."))
    n_changes = len(agent_changes(_doc))
    assert n_changes >= 2, "precondition: multiple agent changes, got %d" % n_changes
    assert _redline_count() > 0, "precondition: redlines present"
    n = resolve_all_agent_changes(_doc, _ctx, True)
    assert n == n_changes, "global path must resolve AND count every agent change, got %d of %d" % (n, n_changes)
    assert not has_agent_changes(_doc), "no agent change left pending"
    assert _redline_count() == 0, "global AcceptAll must clear all redlines, got %d" % _redline_count()
    _body("reset")


@native_test
def test_snapshot_reliable_with_table_cell_redline_uno():
    """Robustness: the snapshot helpers fail closed when the redline enumeration is shorter
    than getRedlines().getCount() (a silent truncation). That guard must NOT misfire on a HEALTHY doc
    whose redlines live in a NON-BODY container -- here a table cell. getRedlines() is one flat table
    spanning body + cells, so getCount() must equal the enumeration length; this pins that, proving
    resolve never spuriously refuses on documents containing tables."""
    from plugin.writer.inline_review import _agent_change_tokens, _foreign_redline_ids

    _body("Body clause one here.")
    text = _doc.getText()
    cur = text.createTextCursor()
    cur.gotoEnd(False)
    text.insertControlCharacter(cur, _PARA_BREAK, False)
    table = _doc.createInstance("com.sun.star.text.TextTable")
    table.initialize(1, 1)
    text.insertTextContent(cur, table, False)
    cell_text = table.getCellByName("A1").getText()
    cell_text.setString("Cell clause two here.")

    # A tracked change in the BODY and another inside the TABLE CELL.
    _tracked_replace("Body clause one here.", "Body clause ONE here.")
    _doc.setPropertyValue("RecordChanges", True)
    cc = cell_text.createTextCursor()
    cc.gotoStart(False)
    cc.gotoEnd(True)
    cc.setString("")
    cell_text.insertString(cc, "Cell clause TWO here.", False)
    _doc.setPropertyValue("RecordChanges", False)

    assert _redline_count() >= 2, "precondition: redlines in both the body and the table cell"
    assert _redline_count() == len(_doc.getRedlines()), "enumeration length must equal getCount()"
    _, tokens_reliable = _agent_change_tokens(_doc)
    _, foreign_reliable = _foreign_redline_ids(_doc)
    assert tokens_reliable is True, "a table-cell redline must NOT make the token snapshot 'unreliable'"
    assert foreign_reliable is True, "a table-cell redline must NOT make the foreign snapshot 'unreliable'"
    _accept_all()
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
def test_resolve_spares_user_redline_in_same_paragraph_uno():
    """The precise (exact-bounds) resolve accepts the agent change WITHOUT touching the user's
    own (non-overlapping) tracked change in the SAME paragraph. Modern LibreOffice resolves only
    the selected marks, so -- unlike the old paragraph-wide dispatch -- we no longer refuse here.
    The OVERLAPPING case is refused -- covered by
    test_resolve_refuses_when_user_redline_overlaps_change_uno."""
    _body("The quick brown fox jumps.")
    _tracked_replace("quick", "fast")     # the user's own (untokened) redline ...
    _agent_edit(("fox", "dog"))           # ... and an agent change in the SAME paragraph
    before = _redline_count()
    _caret_in("dog")
    ok, msg = resolve_change_at_cursor(_doc, _ctx, True)
    assert ok, "the agent change must resolve precisely, sparing the user's redline: %r" % msg
    assert not has_agent_changes(_doc), "the agent change was accepted"
    assert _redline_count() < before, "the agent change's marks were resolved"
    assert _redline_count() >= 1 and _find("fast") is not None, \
        "the user's own redline must remain pending and untouched, got %d redlines" % _redline_count()
    _body("reset")


@native_test
def test_resolve_refuses_when_user_redline_overlaps_change_uno():
    """SAFETY (the data-loss guard): when one of the USER's own tracked changes OVERLAPS the
    agent change's exact span, the precise resolve must REFUSE -- an exact-bounds dispatch would
    resolve the user's redline too. Resolve by token to force the _foreign_redline_in_span guard.
    Nothing may be touched. (This is the branch the spares-user test does NOT cover.)"""
    _body("The quick brown fox.")
    _agent_edit(("fox", "dog"))               # agent change fox -> dog (tokened)
    _tracked_replace("dog", "cat")            # user edits the agent's inserted word -> overlapping redline
    changes = agent_changes(_doc)
    assert changes, "agent change should still be listed"
    token = changes[0]["token"]
    before = _redline_count()
    ok = resolve_agent_change(_doc, _ctx, token, True)
    assert ok is False, "must refuse to resolve an agent change whose exact span overlaps a user redline"
    assert _redline_count() == before, "nothing may be resolved -- the user's redline must survive intact"
    _body("reset")


@native_test
def test_resolve_one_of_several_agent_changes_in_paragraph_uno():
    """Core: with several agent changes in ONE paragraph, accepting one resolves ONLY it and
    leaves the others pending. Modern LibreOffice resolves the exact selection -- the old code
    refused this dense case and forced 'accept all'."""
    _body("Alpha beta gamma delta.")
    _agent_edit(("Alpha", "One"), ("gamma", "Three"))
    assert len(agent_changes(_doc)) == 2, agent_changes(_doc)
    _caret_in("One")
    ok, msg = resolve_change_at_cursor(_doc, _ctx, True)
    assert ok, "accepting one of several same-paragraph agent changes must work now: %r" % msg
    left = agent_changes(_doc)
    assert len(left) == 1 and left[0]["new"] == "Three", "only the targeted change resolved: %r" % left
    _body("reset")


@native_test
def test_resolve_reject_one_of_several_agent_changes_in_paragraph_uno():
    """Rejecting one of several same-paragraph agent changes restores only that word and leaves
    the others pending."""
    _body("Alpha beta gamma delta.")
    _agent_edit(("Alpha", "One"), ("gamma", "Three"))
    _caret_in("Three")
    ok, msg = resolve_change_at_cursor(_doc, _ctx, False)
    assert ok, "rejecting one of several must work: %r" % msg
    left = agent_changes(_doc)
    assert len(left) == 1 and left[0]["new"] == "One", "only the targeted change rejected: %r" % left
    assert _find("gamma") is not None, "reject restored the original 'gamma'"
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


@native_test
def test_resolve_all_with_feedback_reports_skipped_then_silent_when_clean_uno():
    """resolve_all_with_feedback must surface a user-facing note when some changes are skipped
    (they share a paragraph with the user's own redline) -- otherwise the menu/popup click looks
    like it did nothing. A fully clean resolve-all returns an empty message (no nag)."""
    # Mixed: one agent change shares paragraph 1 with a user redline (skipped), one is clean.
    _body("The quick brown fox jumps.", "Lonely agent clause here.")
    _tracked_replace("quick", "fast")
    _agent_edit(("fox", "dog"), ("Lonely agent clause here.", "Lonely agent clause EDITED."))
    n, msg = resolve_all_with_feedback(_doc, _ctx, True)
    assert n == 1, "only the clean-paragraph agent change resolves, got %d" % n
    assert msg and "Manage" in msg, "a skipped change must produce a user-facing message: %r" % msg

    # All-clean run -> no message.
    _body("Clean one.", "Clean two.")
    _agent_edit(("Clean one.", "Clean one EDITED."), ("Clean two.", "Clean two EDITED."))
    n2, msg2 = resolve_all_with_feedback(_doc, _ctx, True)
    assert n2 == 2 and msg2 == "", "all-clean resolve-all returns no message, got n=%d msg=%r" % (n2, msg2)
    _body("reset")


# --- fast-travel: count + next/previous navigation -------------------------------------

def _three_changes():
    # Leading plain paragraph so the document start is unambiguously BEFORE every change
    # (otherwise the cursor at offset 0 coincides with the first change's start).
    _body("Intro paragraph, no change.",
          "First clause here.", "Second clause here.", "Third clause here.")
    _agent_edit(("First clause here.", "Alpha one."),
                ("Second clause here.", "Beta two."),
                ("Third clause here.", "Gamma three."))
    return [c["token"] for c in agent_changes(_doc)]  # document order


@native_test
def test_pending_count_reflects_agent_changes_uno():
    assert pending_agent_change_count(_doc) == 0, "clean doc has 0 pending"
    _three_changes()
    assert pending_agent_change_count(_doc) == 3, pending_agent_change_count(_doc)
    _body("reset")


@native_test
def test_fast_travel_next_visits_in_order_and_cycles_uno():
    order = _three_changes()
    assert len(order) == 3, order
    _doc.getCurrentController().getViewCursor().gotoRange(_doc.getText().getStart(), False)
    visited = [goto_adjacent_agent_change(_doc, True) for _ in range(4)]
    assert visited == [order[0], order[1], order[2], order[0]], \
        "next must walk doc order then cycle: %r vs order %r" % (visited, order)
    _body("reset")


@native_test
def test_fast_travel_prev_visits_in_reverse_and_cycles_uno():
    order = _three_changes()
    _doc.getCurrentController().getViewCursor().gotoRange(_doc.getText().getEnd(), False)
    visited = [goto_adjacent_agent_change(_doc, False) for _ in range(4)]
    assert visited == [order[2], order[1], order[0], order[2]], \
        "prev must walk reverse then cycle: %r vs order %r" % (visited, order)
    _body("reset")


@native_test
def test_fast_travel_next_from_caret_at_change_start_does_not_skip_uno():
    """A bare (collapsed) caret resting at a change's start -- e.g. where an agent edit left it --
    must let the user START at that change: Next goes to IT, not the one after. Regression for the
    fast-travel bug where 'strictly after the cursor' skipped the change under the caret."""
    order = _three_changes()
    vc = _doc.getCurrentController().getViewCursor()
    # Caret exactly at the FIRST change's start.
    left0, _ = _change_bounds(_doc, order[0])
    vc.gotoRange(left0, False)
    assert goto_adjacent_agent_change(_doc, True) == order[0], "must not skip the change at the caret (first)"
    # Caret exactly at the MIDDLE change's start.
    left1, _ = _change_bounds(_doc, order[1])
    vc.gotoRange(left1, False)
    assert goto_adjacent_agent_change(_doc, True) == order[1], "must not skip the change at the caret (middle)"
    # But once a change is SELECTED (reviewing it), Next must STEP to the following change.
    assert goto_adjacent_agent_change(_doc, True) == order[2], "selected change -> Next advances"
    _body("reset")


@native_test
def test_fast_travel_none_when_no_changes_uno():
    _body("Nothing tracked here.")
    assert pending_agent_change_count(_doc) == 0
    assert goto_adjacent_agent_change(_doc, True) is None, "no changes -> nowhere to travel"
    _body("reset")
