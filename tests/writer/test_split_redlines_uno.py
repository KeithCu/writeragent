# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
#
# A small edit (1-2 words) inside a long block must land as TIGHT surgical redlines
# -- one reviewable change per changed run -- instead of a whole-paragraph delete+insert. A
# large edit (> threshold of words changed) still lands as a single clean block change. The
# split is internal: the agent issues one edit and simply gets one outcome per sub-change.
import uno  # noqa: F401

from plugin.testing_runner import native_test, setup, teardown
from plugin.tests.testing_utils import TestingFactory
from plugin.writer.content import _record_preserve_replace
from plugin.writer.edit_review import EditReviewSession
from plugin.writer.inline_review import agent_changes, resolve_agent_change

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
    if len(_doc.getRedlines()):
        _accept_all()


def _accept_all():
    helper = _ctx.getServiceManager().createInstanceWithContext("com.sun.star.frame.DispatchHelper", _ctx)
    helper.executeDispatch(_doc.getCurrentController().getFrame(), ".uno:AcceptAllTrackedChanges", "", 0, ())


def _para_with(needle):
    f = _find(needle)
    if f is None:
        return None
    t = f.getText().createTextCursorByRange(f.getStart())
    t.gotoStartOfParagraph(False)
    t.gotoEndOfParagraph(True)
    return t.getString()


_LONG = "The quick brown fox jumps over the lazy dog near the river bank today."


@native_test
def test_split_small_change_makes_tight_surgical_changes_uno():
    """Two non-adjacent word tweaks in a long paragraph -> TWO tight surgical changes (just the
    words), not one whole-paragraph delete+insert."""
    _body(_LONG)
    found = _find(_LONG)
    new = _LONG.replace("quick", "fast").replace("lazy", "sleepy")
    with EditReviewSession(_doc, _ctx, enabled=True) as session:
        _record_preserve_replace(session, _doc, found, new, _ctx, True)
    session.cleanup()
    changes = agent_changes(_doc)
    assert len(changes) == 2, "a 2-word tweak must split into 2 surgical changes: %r" % changes
    assert sorted(c["old"] for c in changes) == ["lazy", "quick"], changes
    assert sorted(c["new"] for c in changes) == ["fast", "sleepy"], changes
    _body("reset")


@native_test
def test_split_adjacent_words_agglutinate_into_one_change_uno():
    """Two ADJACENT changed words collapse into ONE surgical change (consecutive run)."""
    _body(_LONG)
    found = _find(_LONG)
    new = _LONG.replace("quick brown", "slow grey")
    with EditReviewSession(_doc, _ctx, enabled=True) as session:
        _record_preserve_replace(session, _doc, found, new, _ctx, True)
    session.cleanup()
    changes = agent_changes(_doc)
    assert len(changes) == 1, "adjacent changed words must agglutinate into 1 change: %r" % changes
    assert changes[0]["old"] == "quick brown" and changes[0]["new"] == "slow grey", changes[0]
    _body("reset")


@native_test
def test_split_changes_resolve_individually_uno():
    """Each surgical change is its own reviewable unit -- accept one, the other stays."""
    _body(_LONG)
    found = _find(_LONG)
    new = _LONG.replace("quick", "fast").replace("lazy", "sleepy")
    with EditReviewSession(_doc, _ctx, enabled=True) as session:
        _record_preserve_replace(session, _doc, found, new, _ctx, True)
    session.cleanup()
    changes = agent_changes(_doc)
    assert len(changes) == 2, changes
    tok = [c["token"] for c in changes if c["new"] == "fast"][0]
    assert resolve_agent_change(_doc, _ctx, tok, True) is True, "one surgical change must resolve alone"
    left = agent_changes(_doc)
    assert len(left) == 1 and left[0]["new"] == "sleepy", "the other surgical change stays pending: %r" % left
    _body("reset")


@native_test
def test_split_large_change_stays_single_block_uno():
    """A change above the threshold (most words changed) stays ONE clean block edit."""
    _body("alpha beta gamma")
    found = _find("alpha beta gamma")
    with EditReviewSession(_doc, _ctx, enabled=True) as session:
        _record_preserve_replace(session, _doc, found, "totally different replacement words", _ctx, True)
    session.cleanup()
    changes = agent_changes(_doc)
    assert len(changes) == 1, "a >threshold change must stay a single block change: %r" % changes
    _body("reset")


@native_test
def test_split_accept_all_reconstructs_new_text_uno():
    """Accepting every surgical change yields exactly the agent's intended new text."""
    para = "The quick brown fox jumps over the lazy dog."
    new = "The fast brown fox leaps over the lazy dog."  # quick->fast, jumps->leaps
    _body(para)
    found = _find(para)
    with EditReviewSession(_doc, _ctx, enabled=True) as session:
        _record_preserve_replace(session, _doc, found, new, _ctx, True)
    session.cleanup()
    assert len(agent_changes(_doc)) == 2, agent_changes(_doc)
    _accept_all()
    assert _para_with("brown") == new, "accepting all surgical edits must yield the new text, got %r" % _para_with("brown")
    _body("reset")


@native_test
def test_split_off_when_not_recording_single_change_uno():
    """With split disabled (review recording off) the edit is one whole replace, as before."""
    _body(_LONG)
    found = _find(_LONG)
    new = _LONG.replace("quick", "fast").replace("lazy", "sleepy")
    with EditReviewSession(_doc, _ctx, enabled=True) as session:
        _record_preserve_replace(session, _doc, found, new, _ctx, False)  # split=False
    session.cleanup()
    changes = agent_changes(_doc)
    assert len(changes) == 1, "split=False must keep the old single-change behaviour: %r" % changes
    _body("reset")


@native_test
def test_diff_threshold_default_constant_uno():
    """The surgical-vs-block threshold is a named module constant (env-overridable via
    WRITERAGENT_AGENT_EDIT_DIFF_THRESHOLD, NOT a settings-UI config key), defaulting to 0.6."""
    from plugin.writer.content import _WORD_DIFF_THRESHOLD
    assert _WORD_DIFF_THRESHOLD == 0.6, "_WORD_DIFF_THRESHOLD should default to 0.6"


@native_test
def test_review_payload_includes_final_text_uno():
    """Per-change final_text is the CHANGE's OWN region (scoped to its anchor span), not
    the whole paragraph -- so neighbouring changes in the same paragraph don't contaminate each
    other's report, and a change deep in a long paragraph is never truncated out of the preview."""
    _body("Intro.", _LONG)
    found = _find(_LONG)
    new = _LONG.replace("quick", "fast").replace("lazy", "sleepy")
    with EditReviewSession(_doc, _ctx, enabled=True) as session:
        _record_preserve_replace(session, _doc, found, new, _ctx, True)
        payload = session._review_payload(complete=False, timed_out=False)
    session.cleanup()
    changes = payload["changes"]
    assert len(changes) == 2, changes
    fts = [c.get("final_text") or "" for c in changes]
    joined = " ".join(fts)
    # Each change reports its own inserted word; struck old text is skipped (no "quickfast" glue).
    assert "fast" in joined and "sleepy" in joined, fts
    assert "quickfast" not in joined and "sleepylazy" not in joined, fts
    # Scoped per change -> the two reports differ and neither carries the OTHER change's word
    # (nor the unrelated "brown" between them, which the old whole-paragraph final_text included).
    fast_ft = next(ft for ft in fts if "fast" in ft)
    sleepy_ft = next(ft for ft in fts if "sleepy" in ft)
    assert "sleepy" not in fast_ft and "brown" not in fast_ft, fast_ft
    assert "fast" not in sleepy_ft and "brown" not in sleepy_ft, sleepy_ft
    _body("reset")


@native_test
def test_e2e_split_navigate_resolve_report_uno():
    """End-to-end (logical, not GUI): agent edit changes 2 words in a long paragraph -> splits into
    2 surgical changes -> fast-travel visits them -> accept one individually ->
    per-change report shows outcome + final_text."""
    from plugin.writer.inline_review import (goto_adjacent_agent_change,
                                             pending_agent_change_count, resolve_agent_change)
    _body("Intro.", _LONG)
    found = _find(_LONG)
    new = _LONG.replace("quick", "fast").replace("lazy", "sleepy")
    with EditReviewSession(_doc, _ctx, enabled=True) as session:
        _record_preserve_replace(session, _doc, found, new, _ctx, True)
        assert pending_agent_change_count(_doc) == 2, "split into 2 surgical changes"
        _doc.getCurrentController().getViewCursor().gotoRange(_doc.getText().getStart(), False)
        first = goto_adjacent_agent_change(_doc, True)
        assert first is not None, "fast-travel must land on a change"
        assert resolve_agent_change(_doc, _ctx, first, True) is True, "accept that one alone"
        assert pending_agent_change_count(_doc) == 1, "the other stays pending"
        payload = session._review_payload(complete=False, timed_out=False)
    session.cleanup()
    chs = payload["changes"]
    assert len(chs) == 2, chs
    # Per-change outcome is now scoped to the change's own region, so a PARTIAL resolution
    # of two changes sharing a paragraph reports the accepted one as "accepted" and the other as
    # "pending" -- the per-change outcome is reliable, no longer a whole-paragraph approximation.
    by_outcome = {c["outcome"]: c for c in chs}
    assert set(by_outcome) == {"accepted", "pending"}, chs
    assert "fast" in (by_outcome["accepted"]["final_text"] or ""), by_outcome["accepted"]
    assert "sleepy" in (by_outcome["pending"]["final_text"] or ""), by_outcome["pending"]
    _body("reset")


@native_test
def test_pure_insertion_and_deletion_outcomes_uno():
    """Edge: pure-insertion and pure-deletion changes (the redline shapes the scoping fix
    changed, not covered by the replace tests) report outcome + final_text from their own scoped
    anchor span."""
    # Pure insertion, accepted -> "accepted", inserted word present in final_text.
    _body("The lazy dog.")
    with EditReviewSession(_doc, _ctx, enabled=True) as session:
        _record_preserve_replace(session, _doc, _find("dog"), "dog runs", _ctx, True)
        chs = agent_changes(_doc)
        assert len(chs) == 1 and "runs" in chs[0]["new"], chs
        assert resolve_agent_change(_doc, _ctx, chs[0]["token"], True) is True
        ins = session._review_payload(complete=False, timed_out=False)["changes"][0]
    session.cleanup()
    assert ins["outcome"] == "accepted" and "runs" in (ins["final_text"] or ""), ins

    # Pure deletion, rejected -> "rejected", restored word present in final_text.
    _body("The lazy dog.")
    with EditReviewSession(_doc, _ctx, enabled=True) as session:
        _record_preserve_replace(session, _doc, _find("lazy "), "", _ctx, True)
        chs = agent_changes(_doc)
        assert len(chs) == 1 and "lazy" in chs[0]["old"], chs
        assert resolve_agent_change(_doc, _ctx, chs[0]["token"], False) is True
        dele = session._review_payload(complete=False, timed_out=False)["changes"][0]
    session.cleanup()
    assert dele["outcome"] == "rejected" and "lazy" in (dele["final_text"] or ""), dele
    _body("reset")


@native_test
def test_split_three_runs_resolve_individually_and_reconstruct_uno():
    """THREE surgical changes in one paragraph -> three independently reviewable changes. This
    exercises the real right-to-left offset application for 3+ runs (the central risk, previously
    only smoke-tested with two words): resolve the middle one alone, then accept the rest and confirm
    the paragraph reconstructs to the agent's text exactly."""
    base = "alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu"
    _body("Intro.", base)
    found = _find(base)
    new = base.replace("beta", "BETA").replace("delta", "DELTA").replace("zeta", "ZETA")
    with EditReviewSession(_doc, _ctx, enabled=True) as session:
        _record_preserve_replace(session, _doc, found, new, _ctx, True)
        chs = agent_changes(_doc)
        assert len(chs) == 3, chs   # three independent surgical runs
        mid = [c for c in chs if "DELTA" in c["new"]][0]
        assert resolve_agent_change(_doc, _ctx, mid["token"], True) is True  # accept the middle alone
        assert len(agent_changes(_doc)) == 2, "the other two stay pending"
    session.cleanup()
    _accept_all()  # accept the remaining two
    assert _para_with("alpha") == new, "paragraph must reconstruct to the agent's text exactly"
    _body("reset")


@native_test
def test_final_text_not_truncated_for_deep_change_uno():
    """A change deep in a long paragraph (well past the preview cap) is still shown in
    final_text, because final_text is the change's OWN region, not the paragraph's first N chars."""
    long_para = ("alpha " * 120) + "TARGETWORD " + ("omega " * 40)  # TARGETWORD sits ~720 chars in
    _body("Intro.", long_para)
    found = _find(long_para)
    new = long_para.replace("TARGETWORD", "REPLACEMENT")
    with EditReviewSession(_doc, _ctx, enabled=True) as session:
        _record_preserve_replace(session, _doc, found, new, _ctx, True)
        payload = session._review_payload(complete=False, timed_out=False)
    session.cleanup()
    chs = payload["changes"]
    assert len(chs) == 1, chs
    ft = chs[0]["final_text"] or ""
    assert "REPLACEMENT" in ft, "deep change must appear in final_text (N2): %r" % ft
    _body("reset")


@native_test
def test_block_safe_for_surgical_guard_uno():
    """The surgical path only runs on a clean SINGLE-paragraph plain-text block; otherwise
    char offsets diverge from cursor stops, so it must fall back to the whole-block replace."""
    from plugin.writer.content import _block_safe_for_surgical
    _body("Just some plain words here.")
    assert _block_safe_for_surgical(_find("Just some plain words here.")) is True, "clean block is safe"
    # a range spanning TWO paragraphs -> unsafe
    _body("First para here.", "Second para here.")
    cur = _doc.getText().createTextCursor()
    cur.gotoStart(False)
    cur.gotoEnd(True)
    assert _block_safe_for_surgical(cur) is False, "multi-paragraph block must be unsafe"
    # a block that already holds a tracked change (struck text) -> unsafe
    _body("Alpha beta gamma delta.")
    _doc.setPropertyValue("RecordChanges", True)
    f = _find("beta")
    tc = f.getText().createTextCursorByRange(f)
    tc.setString("")
    f.getText().insertString(tc, "BETA", False)
    _doc.setPropertyValue("RecordChanges", False)
    para = _find("Alpha")
    pc = para.getText().createTextCursorByRange(para.getStart())
    pc.gotoStartOfParagraph(False)
    pc.gotoEndOfParagraph(True)
    assert _block_safe_for_surgical(pc) is False, "block with a pre-existing tracked change must be unsafe"
    _accept_all()
    _body("reset")


@native_test
def test_go_right_chunks_past_short_cap_uno():
    """_go_right moves/selects correctly PAST goRight's 32767 short cap (chunked). This is
    the offset helper the surgical path uses; before the fix a raw goRight(>32767) overflowed.
    Tested directly because the surgical path needs a search range (which we can't build for a
    35000-char block via findFirst)."""
    from plugin.writer.content import _go_right
    _body(("word " * 7000) + "TARGET tail.")    # ~35000 chars, one paragraph, past the short cap
    cur = _doc.getText().createTextCursor()
    cur.gotoStart(False)
    _go_right(cur, len("word " * 7000), False)   # move to offset 35000 (> 32767)
    _go_right(cur, len("TARGET"), True)          # select the next word
    assert cur.getString() == "TARGET", "chunked goRight landed at the wrong offset: %r" % cur.getString()
    _body("reset")


@native_test
def test_surgical_partial_apply_rolls_back_atomically_uno():
    """Atomicity: when a surgical sub-edit fails AFTER an earlier one already landed (a
    replace deletes then inserts -- insertString can throw post-delete), the WHOLE batch must roll
    back. Proves the two UNO semantics the fix relies on and unit fakes can't: (a) undoing the
    grouped context removes the first sub-edit's tagged redlines, (b) the orphan anchor bookmark is
    cleaned -- so the document is byte-for-byte pristine and the failure is surfaced, not swallowed."""
    import plugin.writer.format as fmt

    base = "The quick brown fox jumps over the lazy dog near the river."
    _body(base)
    found = _find(base)
    new = base.replace("quick", "fast").replace("lazy", "sleepy")  # exactly two surgical sub-edits

    redlines_before = len(_doc.getRedlines())
    bookmarks_before = set(_doc.getBookmarks().getElementNames())

    real = fmt.replace_preserving_format
    state = {"n": 0}

    def flaky(*a, **k):
        state["n"] += 1
        if state["n"] == 2:  # second applied sub-edit: simulate insertString failing after the delete
            raise RuntimeError("simulated mid-apply failure")
        return real(*a, **k)

    raised = False
    fmt.replace_preserving_format = flaky
    try:
        with EditReviewSession(_doc, _ctx, enabled=True) as session:
            _record_preserve_replace(session, _doc, found, new, _ctx, True)
    except RuntimeError as e:
        raised = True
        assert "mid-apply failure" in str(e), e
    finally:
        fmt.replace_preserving_format = real
    session.cleanup()

    assert raised, "a mid-apply sub-edit failure must propagate (honest failure), not be swallowed"
    assert state["n"] == 2, "expected 1 ok + 1 failing apply, got %d calls" % state["n"]
    # All-or-nothing: document rolled back to the original.
    assert _para_with("fox") == base, "paragraph must be rolled back to the original: %r" % _para_with("fox")
    assert len(_doc.getRedlines()) == redlines_before, \
        "the first sub-edit's tracked change must be undone (no redlines left): %d" % len(_doc.getRedlines())
    assert set(_doc.getBookmarks().getElementNames()) == bookmarks_before, \
        "no orphan anchor bookmark may survive the rollback"
    assert len(session.changes) == 0, "partial change records must be trimmed: %r" % session.changes
    _body("reset")


@native_test
def test_surgical_first_subedit_failure_preserves_prior_undo_uno():
    """Empty-context safety: if the FIRST surgical sub-edit fails BEFORE mutating anything,
    the undo context is empty and discarded on leave -- so the rollback must NOT call undo() (that
    would revert the user's PRIOR action). Proves the empty-context-discard semantics with a real
    undoable user edit that must survive the failed batch."""
    import plugin.writer.format as fmt

    _body("Alpha beta gamma delta epsilon zeta.")
    # A real, undoable user edit BEFORE the agent batch -- it MUST survive the rollback.
    _doc.setPropertyValue("RecordChanges", False)
    text = _doc.getText()
    cur = text.createTextCursor()
    cur.gotoEnd(False)
    text.insertString(cur, " USEREDIT", False)
    assert "USEREDIT" in _para_with("Alpha"), "precondition: the user edit is present"

    base = "Alpha beta gamma delta epsilon zeta. USEREDIT"
    found = _find(base)
    new = base.replace("beta", "BETA").replace("delta", "DELTA")  # two surgical sub-edits

    real = fmt.replace_preserving_format

    def always_fail(*a, **k):
        raise RuntimeError("first sub-edit fails before mutating")

    raised = False
    fmt.replace_preserving_format = always_fail
    try:
        with EditReviewSession(_doc, _ctx, enabled=True) as session:
            _record_preserve_replace(session, _doc, found, new, _ctx, True)
    except RuntimeError:
        raised = True
    finally:
        fmt.replace_preserving_format = real
    session.cleanup()

    assert raised, "the failure must propagate"
    assert "USEREDIT" in _para_with("Alpha"), \
        "rollback wrongly reverted the user's prior action: %r" % _para_with("Alpha")
    assert len(session.changes) == 0, "nothing was applied, so no change records: %r" % session.changes
    _body("reset")


@native_test
def test_prior_surgical_batch_survives_later_empty_failed_batch_uno():
    """Two surgical batches in one session (the all_matches case) on a REAL undo manager.
    Batch 1 applies and leaves its uniquely-titled context on the undo stack; batch 2's first sub-edit
    fails before mutating, so its context is EMPTY and discarded on leave -- exposing batch 1's title
    on top. The per-batch-unique title must stop batch 2's rollback from undoing batch 1's good edit
    (a constant title would match and revert it)."""
    import plugin.writer.format as fmt

    b1 = "The quick brown fox jumps over the lazy dog."
    b2 = "A nimble red cat leaps across the small wooden fence."
    _body(b1, b2)
    new1 = b1.replace("quick", "fast").replace("lazy", "sleepy")
    new2 = b2.replace("nimble", "quick").replace("small", "tiny")

    real = fmt.replace_preserving_format
    raised = False
    with EditReviewSession(_doc, _ctx, enabled=True) as session:
        _record_preserve_replace(session, _doc, _find(b1), new1, _ctx, True)  # batch 1 succeeds
        changes_after_b1 = len(session.changes)
        assert changes_after_b1 >= 1, "batch 1 must record at least one change"

        def always_fail(*a, **k):  # batch 2's first sub-edit fails before mutating -> empty context
            raise RuntimeError("batch2 boom")

        fmt.replace_preserving_format = always_fail
        try:
            _record_preserve_replace(session, _doc, _find(b2), new2, _ctx, True)
        except RuntimeError:
            raised = True
        finally:
            fmt.replace_preserving_format = real

        assert raised, "batch 2's failure must propagate"
        assert len(session.changes) == changes_after_b1, "batch 2 must not drop batch 1's records"
    session.cleanup()

    # Batch 1's edit must still be present (NOT reverted by batch 2's rollback). _para_with skips the
    # tracked deletions, so the live paragraph reads as the post-accept text.
    assert "fast" in (_para_with("brown") or ""), \
        "batch 1's surgical edit was wrongly reverted by batch 2's rollback: %r" % _para_with("brown")
    _body("reset")


@native_test
def test_softpagebreak_offset_safe_whitelist_uno():
    """An automatic SoftPageBreak (layout-only -- contributes 0 chars to getString() and is
    not a goRight stop; verified live that navigating getString offsets across one lands exactly
    right) must NOT force whole-block, so a paragraph that merely straddles a page boundary keeps
    its surgical sub-edits. Real content portions DO shift offsets and must keep forcing block.
    Pins the offset-safe whitelist (a layout-dependent behavioural test would be flaky headless)."""
    from plugin.writer.content import _OFFSET_SAFE_PORTION_TYPES
    assert "Text" in _OFFSET_SAFE_PORTION_TYPES
    assert "SoftPageBreak" in _OFFSET_SAFE_PORTION_TYPES, \
        "SoftPageBreak is offset-safe and must be allowed (else page-straddling paragraphs lose surgical)"
    for content_portion in ("TextField", "Footnote", "Bookmark", "Ruby", "Frame", "Redline"):
        assert content_portion not in _OFFSET_SAFE_PORTION_TYPES, \
            "%s shifts getString offsets vs goRight and must keep forcing whole-block" % content_portion
