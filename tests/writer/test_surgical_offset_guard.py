# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Headless tests for the surgical tracked-change offset safety net.

The surgical path in ``_record_preserve_replace`` places each sub-edit by counting characters
(``_go_right``) from the block start, then replaces. If the cursor ever lands short (goRight stops
early), the edit would hit the WRONG characters and silently corrupt the document. These tests pin,
WITHOUT a live LibreOffice (a fake UNO text/cursor), that:

  * ``_go_right`` reports whether the FULL move happened (True) or it stopped short (False),
  * a sub-edit whose offset can't be reached aborts surgical and falls back to ONE whole-block
    replace (never a misplaced edit),
  * the happy path records exactly the surgical sub-edits, and
  * an offset that drifts AFTER the pre-flight validation fails LOUD (raises) instead of corrupting.

The real splitter (``word_diff_split.split_change``) is used for real; only UNO is faked.
"""
from unittest.mock import MagicMock, patch

import pytest

from plugin.writer.content import (
    _AGENT_EDIT_UNDO_TITLE,
    _GO_RIGHT_CHUNK,
    _SURGICAL_UNDO_TITLE,
    _go_right,
    _record_html_atomically,
    _record_preserve_replace,
)
from plugin.framework.errors import ToolExecutionError
from plugin.writer.word_diff_split import split_change

_THRESHOLD = 0.6


class FakeCursor:
    """A model text cursor over *block*. ``max_reach`` is the furthest char index goRight can reach
    (to simulate an early stop); ``shift`` skews getString to simulate offset drift."""

    def __init__(self, block, max_reach, shift=0):
        self.block = block
        self.max_reach = max_reach
        self.shift = shift
        self.start = 0
        self.end = 0

    def goRight(self, n, expand):
        target = self.end + n
        reached = min(target, self.max_reach)
        if expand:
            self.end = reached
        else:
            self.start = self.end = reached
        return reached == target

    def getString(self):
        return self.block[self.start + self.shift:self.end + self.shift]


class FakeText:
    def __init__(self, block, max_reach=None, drift_after=None):
        self.block = block
        self.max_reach = len(block) if max_reach is None else max_reach
        self.drift_after = drift_after
        self.created = 0

    def createTextCursorByRange(self, anchor):
        self.created += 1
        shift = 1 if (self.drift_after is not None and self.created > self.drift_after) else 0
        return FakeCursor(self.block, self.max_reach, shift)


class FakeFound:
    def __init__(self, block, max_reach=None, drift_after=None):
        self.block = block
        self._text = FakeText(block, max_reach, drift_after)

    def getString(self):
        return self.block

    def getText(self):
        return self._text

    def getStart(self):
        return "ANCHOR"


class FakeSession:
    """Records every record_mutation call and applies its fn (to exercise apply_se).

    A record is appended only AFTER its fn succeeds -- mirroring the real session, where apply_fn
    raising means no ChangeRecord lands -- so ``changes`` reflects exactly the sub-edits that took,
    which the rollback then trims back via ``discard_changes_since``."""

    def __init__(self):
        self.calls = []  # (original_preview, proposed_preview)
        self.changes = []
        self.discarded_to = None

    def record_mutation(self, fn, original_preview=None, proposed_preview=None):
        fn()  # if this raises, nothing is recorded (real session appends only after apply_fn returns)
        self.calls.append((original_preview, proposed_preview))
        self.changes.append((original_preview, proposed_preview))

    def discard_changes_since(self, count):
        self.discarded_to = count
        del self.changes[count:]


class FakeUndoManager:
    """Faithful-enough XUndoManager: models the undo STACK so the per-batch-unique title and the
    empty-context-discard-on-leave semantics are exercised for real, not hard-coded.

    ``record_action()`` (wired through the replace mock for each landed mutation) records an action
    in the open context; ``leaveUndoContext`` pushes a NON-empty context's title onto the stack and
    DISCARDS an empty one (matching the UNO contract). ``prior`` seeds the stack with actions that
    predate the batch (e.g. an earlier successful surgical match, or an unrelated user action)."""

    def __init__(self, prior=(), locked=False):
        self.stack = list(prior)   # titles already on the stack, oldest..newest
        self.entered = []
        self.leaves = 0
        self.undos = 0
        self._locked = locked
        self._open = []            # [title, action_count] per currently-open context

    def isLocked(self):
        return self._locked

    def enterUndoContext(self, title):
        self.entered.append(title)
        self._open.append([title, 0])

    def record_action(self):
        if self._open:
            self._open[-1][1] += 1

    def leaveUndoContext(self):
        self.leaves += 1
        title, n = self._open.pop()
        if n > 0:
            self.stack.append(title)        # non-empty context lands as one combined action
            if self._open:                  # nested -> counts as an action of its parent
                self._open[-1][1] += 1
        # an empty context is discarded (nothing pushed) -- the UNO contract

    def getAllUndoActionTitles(self):       # newest-first, per UNO
        return tuple(reversed(self.stack))

    def undo(self):
        self.undos += 1
        if self.stack:
            self.stack.pop()


class FakeDoc:
    def __init__(self, undo_mgr):
        self._um = undo_mgr

    def getUndoManager(self):
        return self._um


def _wire_replace(um, replace_script):
    """A replace_preserving_format stand-in: each call consumes the next entry of *replace_script*
    (an Exception instance/class -> raise it; anything else -> success). On success it records an
    action in *um*'s open context so the fake's stack reflects what actually landed. No script ->
    every call succeeds."""
    script = list(replace_script) if replace_script is not None else None
    state = {"i": 0}

    def fake_replace(*a, **k):
        outcome = None
        if script is not None and state["i"] < len(script):
            outcome = script[state["i"]]
        state["i"] += 1
        if isinstance(outcome, BaseException) or (isinstance(outcome, type) and issubclass(outcome, BaseException)):
            raise outcome
        if um is not None:
            um.record_action()
        return None

    return fake_replace


def _run(found, old, new, max_runs=10_000, threshold=_THRESHOLD, doc=None, replace_side_effect=None,
         session=None, split_author=True):
    session = session if session is not None else FakeSession()
    if doc is None:
        doc = FakeDoc(FakeUndoManager())  # a real (unlocked) faithful undo manager by default
    um = getattr(doc, "_um", None)
    # The tuning knobs are plain module constants now (env-overridable, not config keys); pin them so
    # each test is deterministic. _SPLIT_AUTHOR_COLORS default True == today's two-step behaviour; set
    # False to exercise the one-color path.
    with patch("plugin.writer.content._block_safe_for_surgical", return_value=True), \
         patch("plugin.writer.content._WORD_DIFF_THRESHOLD", threshold), \
         patch("plugin.writer.content._MAX_SURGICAL_RUNS", max_runs), \
         patch("plugin.writer.content._SPLIT_AUTHOR_COLORS", split_author), \
         patch("plugin.writer.format.replace_preserving_format",
               side_effect=_wire_replace(um, replace_side_effect)) as mock_replace:
        _record_preserve_replace(session, doc, found, new, MagicMock(), split=True)
    return session, mock_replace


# --------------------------------------------------------------------------- _go_right

def test_go_right_full_move_returns_true():
    c = FakeCursor("x" * 100, max_reach=100)
    assert _go_right(c, 50, False) is True
    assert c.start == 50


def test_go_right_partial_move_returns_false():
    # goRight can only reach 30 of the requested 50 -> must report failure, not silently stop.
    c = FakeCursor("x" * 100, max_reach=30)
    assert _go_right(c, 50, False) is False


def test_go_right_consumes_full_count_past_short_cap():
    # A count larger than the C++ short chunk must still fully consume when reachable.
    big = _GO_RIGHT_CHUNK * 3 + 7
    c = FakeCursor("x" * (big + 10), max_reach=big + 10)
    assert _go_right(c, big, False) is True
    assert c.start == big


# --------------------------------------------------------------------------- surgical happy path

def test_surgical_happy_path_records_each_sub_edit():
    old = "the quick brown fox jumps over the lazy dog"
    new = "the fast brown fox jumps over the sleepy dog"
    result = split_change(old, new, _THRESHOLD)
    assert result.is_surgical and len(result.sub_edits) >= 2  # sanity: this IS the surgical case

    session, mock_replace = _run(FakeFound(old), old, new)

    # one recorded mutation per sub-edit, no whole-block fallback
    assert len(session.calls) == len(result.sub_edits)
    assert mock_replace.call_count == len(result.sub_edits)
    recorded = {(o, p) for o, p in session.calls}
    expected = {(se.old_text, se.new_text) for se in result.sub_edits}
    assert recorded == expected


def test_surgical_insertion_sub_edit_validates_empty_old_text():
    # A pure insertion (old_start == old_end, empty old_text) must pass the pre-flight, not fall back.
    old = "hello world"
    new = "hello big world"
    result = split_change(old, new, _THRESHOLD)
    assert result.is_surgical and any(se.old_text == "" for se in result.sub_edits)

    session, mock_replace = _run(FakeFound(old), old, new)
    assert len(session.calls) == len(result.sub_edits)
    assert mock_replace.call_count == len(result.sub_edits)


# --------------------------------------------------------------------------- fallback / fail-loud

def test_unreachable_offset_falls_back_to_whole_block():
    old = "the quick brown fox jumps over the lazy dog"
    new = "the fast brown fox jumps over the sleepy dog"
    # Cursor can only reach char 10; the second sub-edit (deep in the block) is unreachable.
    session, mock_replace = _run(FakeFound(old, max_reach=10), old, new)

    # Pre-flight aborts surgical -> exactly ONE whole-block replace of the entire old text.
    assert len(session.calls) == 1
    assert mock_replace.call_count == 1
    original_preview, proposed_preview = session.calls[0]
    assert original_preview == old
    assert proposed_preview == new


def test_too_many_surgical_runs_falls_back_to_block():
    # A very scattered edit (3 changed runs) with the cap at 2 must land as ONE whole-block change.
    old = "a b c d e f g"
    new = "a X c Y e Z g"
    result = split_change(old, new, _THRESHOLD)
    assert result.is_surgical and len(result.sub_edits) == 3  # sanity: 3 surgical runs

    session, mock_replace = _run(FakeFound(old), old, new, max_runs=2)
    assert len(session.calls) == 1                 # one whole-block mutation, not 3
    assert session.calls[0][0] == old              # original_preview is the whole old text
    assert mock_replace.call_count == 1


def test_surgical_runs_at_cap_stay_surgical():
    old = "a b c d e f g"
    new = "a X c Y e Z g"
    session, mock_replace = _run(FakeFound(old), old, new, max_runs=3)
    assert len(session.calls) == 3                 # at the cap -> still surgical, one per run
    assert mock_replace.call_count == 3


def test_threshold_flows_into_block_vs_surgical_decision():
    # the CONFIGURED threshold must actually change the block-vs-surgical OUTCOME end-to-end
    # through _record_preserve_replace, not just be readable. Same edit (~0.36 changed fraction):
    old = "the quick brown fox jumps over the lazy dog"
    new = "the fast brown fox jumps over the sleepy dog"
    # threshold BELOW the change fraction -> one whole block.
    block_session, block_replace = _run(FakeFound(old), old, new, threshold=0.3)
    assert len(block_session.calls) == 1
    assert block_replace.call_count == 1
    # threshold ABOVE the change fraction -> surgical sub-edits.
    surg_session, surg_replace = _run(FakeFound(old), old, new, threshold=0.6)
    assert len(surg_session.calls) == 2
    assert surg_replace.call_count == 2


def test_offset_drift_after_preflight_raises():
    old = "the quick brown fox jumps over the lazy dog"
    new = "the fast brown fox jumps over the sleepy dog"
    n_edits = len(split_change(old, new, _THRESHOLD).sub_edits)
    # Honest cursors during the pre-flight pass (n_edits creations), drifted ones afterward (apply).
    found = FakeFound(old, drift_after=n_edits)
    with pytest.raises(RuntimeError, match="drifted"):
        _run(found, old, new)


# --------------------------------------------------------- atomicity: rollback on mid-apply failure

def test_midapply_failure_undoes_batch_and_trims_partial_records():
    # Two sub-edits pass the pre-flight; the FIRST applied (right-to-left) replace lands, the SECOND
    # raises (insertString failing after the delete). The batch must be rolled back as a unit: undo
    # the grouped context and drop the partial change record, then re-raise (all-or-nothing).
    old = "the quick brown fox jumps over the lazy dog"
    new = "the fast brown fox jumps over the sleepy dog"
    assert len(split_change(old, new, _THRESHOLD).sub_edits) == 2  # sanity: exactly two sub-edits
    um = FakeUndoManager()

    with pytest.raises(RuntimeError, match="insert boom"):
        _run(FakeFound(old), old, new, doc=FakeDoc(um),
             replace_side_effect=[None, RuntimeError("insert boom")])

    assert len(um.entered) == 1 and um.entered[0].startswith(_SURGICAL_UNDO_TITLE)  # grouped, unique title
    assert um.leaves == 1                        # context closed exactly once (rollback path)
    assert um.undos == 1                         # our context was on top -> undone
    assert um.stack == []                        # the batch's action was undone off the stack


def test_midapply_failure_session_records_rolled_back():
    # Same scenario, asserting the SESSION side: the one change recorded before the failure is
    # discarded back to the pre-batch count so no orphan reviewable change survives a failed batch.
    old = "the quick brown fox jumps over the lazy dog"
    new = "the fast brown fox jumps over the sleepy dog"
    um = FakeUndoManager()
    session = FakeSession()

    with pytest.raises(RuntimeError, match="boom"):
        _run(FakeFound(old), old, new, doc=FakeDoc(um), session=session,
             replace_side_effect=[None, RuntimeError("boom")])

    assert session.discarded_to == 0     # trimmed back to the pre-batch length
    assert session.changes == []         # the partial record is gone


def test_failed_undo_keeps_partial_records_rather_than_orphaning():
    # A later sub-edit fails AND the rollback undo() itself fails: the partial edit is still live, so
    # its change record must be KEPT (not trimmed) -- dropping it would orphan a tagged redline with
    # no reviewable change. The original error is still re-raised.
    old = "the quick brown fox jumps over the lazy dog"
    new = "the fast brown fox jumps over the sleepy dog"

    class ExplodingUndoManager(FakeUndoManager):
        def undo(self):
            self.undos += 1
            raise RuntimeError("undo unavailable")

    um = ExplodingUndoManager()
    session = FakeSession()
    with pytest.raises(RuntimeError, match="boom"):
        _run(FakeFound(old), old, new, doc=FakeDoc(um), session=session,
             replace_side_effect=[None, RuntimeError("boom")])

    assert um.undos == 1                 # undo was attempted
    assert session.discarded_to is None  # but records were NOT trimmed (undo failed)
    assert len(session.changes) == 1     # the partial record survives for review/cleanup


def test_first_subedit_failure_skips_undo_when_context_empty():
    # The FIRST applied sub-edit fails before any mutation lands -> the undo context is empty and
    # discarded on leave, so OUR title is NOT on top. We must NOT call undo() (it would revert an
    # unrelated user action); we still re-raise and there is nothing to trim.
    old = "the quick brown fox jumps over the lazy dog"
    new = "the fast brown fox jumps over the sleepy dog"
    um = FakeUndoManager(prior=["Typing"])  # a prior, unrelated user action on the stack

    with pytest.raises(RuntimeError, match="boom"):
        _run(FakeFound(old), old, new, doc=FakeDoc(um),
             replace_side_effect=[RuntimeError("boom")])

    assert um.leaves == 1
    assert um.undos == 0          # context empty -> our title not on top -> do not clobber the user
    assert um.stack == ["Typing"]  # the user's prior action is intact


def test_prior_successful_batch_not_undone_by_empty_failed_batch():
    # in an all_matches loop an earlier surgical batch succeeds and leaves its
    # uniquely-titled context on top of the undo stack. A LATER batch whose first sub-edit fails before
    # mutating has an EMPTY context that is discarded on leave -- exposing the earlier batch's title on
    # top. A CONSTANT title would match and undo that good edit; the per-batch-unique title must not.
    old = "the quick brown fox jumps over the lazy dog"
    new = "the fast brown fox jumps over the sleepy dog"
    um = FakeUndoManager()
    session = FakeSession()

    # Batch 1: applies cleanly (both sub-edits land) -> its context is pushed onto the stack.
    _run(FakeFound(old), old, new, doc=FakeDoc(um), session=session)
    assert um.undos == 0 and len(um.stack) == 1   # batch 1's completed context on top
    m1_title = um.stack[0]
    m1_change_count = len(session.changes)
    assert m1_change_count == 2

    # Batch 2: first sub-edit fails before mutating -> empty context -> discarded on leave.
    with pytest.raises(RuntimeError, match="boom"):
        _run(FakeFound(old), old, new, doc=FakeDoc(um), session=session,
             replace_side_effect=[RuntimeError("boom")])

    assert um.undos == 0                          # batch 1 was NOT undone
    assert um.stack == [m1_title]                 # batch 1's context still on the stack
    assert len(session.changes) == m1_change_count  # batch 1's records intact, none added/removed
    assert um.entered[0] != um.entered[1]         # the two batches received DISTINCT unique titles


def test_no_undo_manager_falls_back_to_whole_block():
    # If the document exposes no usable undo manager, the surgical multi-edit path can't guarantee
    # all-or-nothing, so it degrades to ONE whole-block replace (a single mutation) rather than
    # applying several unrollback-able tracked changes (fallback).
    old = "the quick brown fox jumps over the lazy dog"
    new = "the fast brown fox jumps over the sleepy dog"

    class NoUndoDoc:
        def getUndoManager(self):
            raise RuntimeError("no undo manager here")

    session, mock_replace = _run(FakeFound(old), old, new, doc=NoUndoDoc())
    assert len(session.calls) == 1            # exactly one whole-block mutation
    assert mock_replace.call_count == 1
    assert session.calls[0][0] == old         # original_preview is the whole old text


def test_locked_undo_manager_falls_back_to_whole_block():
    # a LOCKED undo manager silently ignores enterUndoContext, so a surgical
    # batch would record no context and have NO rollback. Detect the lock (isLocked) and degrade to
    # one whole-block replace rather than apply a multi-edit batch we cannot undo.
    old = "the quick brown fox jumps over the lazy dog"
    new = "the fast brown fox jumps over the sleepy dog"
    um = FakeUndoManager(locked=True)

    session, mock_replace = _run(FakeFound(old), old, new, doc=FakeDoc(um))

    assert um.entered == []                    # never opened a context (it would be a no-op)
    assert len(session.calls) == 1             # exactly one whole-block mutation
    assert mock_replace.call_count == 1
    assert session.calls[0][0] == old


def test_leave_context_failure_on_success_does_not_roll_back():
    # leaveUndoContext() is always attempted, but if it FAILS on the success path the
    # applied edits must stand -- no undo of valid changes, no crash; the failure is surfaced via log.
    old = "the quick brown fox jumps over the lazy dog"
    new = "the fast brown fox jumps over the sleepy dog"

    class LeaveFailsUndoManager(FakeUndoManager):
        def leaveUndoContext(self):
            self.leaves += 1
            raise RuntimeError("leave boom")

    um = LeaveFailsUndoManager()
    session, _ = _run(FakeFound(old), old, new, doc=FakeDoc(um))

    assert um.leaves == 1            # the leave was attempted (enter/leave pairing honored)
    assert um.undos == 0             # successful edits were NOT rolled back by a failed leave
    assert len(session.calls) == 2   # both sub-edits applied and kept
    assert len(session.changes) == 2
    assert session.discarded_to is None


# ------------------------------------------------- configurable split-author coloring (consistency)

# A whole-block change (no shared words -> 100% changed -> above threshold) so split_change never
# goes surgical and _record_preserve_replace takes the _whole() path.
_WB_OLD = "the quick brown fox"
_WB_NEW = "a completely different unrelated statement here now"


def test_whole_block_split_author_on_opens_context_for_two_step():
    # The KEY consistency fix: with split-author coloring ON (default), even a WHOLE-BLOCK replace is
    # wrapped in an undo context and asks format for the two-step (so it renders in two colors, like a
    # surgical edit). Before, whole-block was always the single-author single-op (one color).
    assert not split_change(_WB_OLD, _WB_NEW, _THRESHOLD).is_surgical  # sanity: whole-block path
    um = FakeUndoManager()
    session, mock_replace = _run(FakeFound(_WB_OLD), _WB_OLD, _WB_NEW, doc=FakeDoc(um), split_author=True)

    assert len(session.calls) == 1                      # one whole-block mutation
    assert len(um.entered) == 1                         # opened ONE undo context for atomicity
    assert um.entered[0].startswith(_SURGICAL_UNDO_TITLE)
    assert um.leaves == 1 and um.undos == 0             # closed cleanly; no rollback on success
    _, kwargs = mock_replace.call_args
    assert kwargs["in_undo_context"] is True            # tells format the two-step is safe...
    assert kwargs["split_author"] is True               # ... and to use it (two colors)


def test_whole_block_split_author_off_stays_atomic_no_context():
    # Coloring OFF -> the whole-block replace stays the single atomic setString (one color), opening
    # no undo context at all (nothing to roll back).
    um = FakeUndoManager()
    session, mock_replace = _run(FakeFound(_WB_OLD), _WB_OLD, _WB_NEW, doc=FakeDoc(um), split_author=False)

    assert len(session.calls) == 1
    assert um.entered == []                             # one color -> no undo context needed
    _, kwargs = mock_replace.call_args
    assert kwargs["in_undo_context"] is False           # single atomic setString
    assert kwargs["split_author"] is False


def test_whole_block_split_author_on_locked_manager_falls_back_to_atomic():
    # Coloring ON but the undo manager is LOCKED (enterUndoContext is a no-op): we can't guarantee a
    # rollback for the two-step, so degrade to the atomic single-op (one color) -- never a
    # non-atomic edit. split_author still reflects config; format's gate (needs BOTH flags) renders
    # one color anyway because in_undo_context is False.
    um = FakeUndoManager(locked=True)
    session, mock_replace = _run(FakeFound(_WB_OLD), _WB_OLD, _WB_NEW, doc=FakeDoc(um), split_author=True)

    assert len(session.calls) == 1
    assert um.entered == []                             # locked -> enterUndoContext refused
    _, kwargs = mock_replace.call_args
    assert kwargs["in_undo_context"] is False           # fell back to the atomic single-op


def test_surgical_split_author_off_threads_one_color_inside_context():
    # Surgical path honours the SAME coloring choice: with coloring off, each sub-edit replace is the
    # atomic single-op (one color) but still INSIDE the grouped undo context (atomicity unchanged).
    old = "the quick brown fox jumps over the lazy dog"
    new = "the fast brown fox jumps over the sleepy dog"
    um = FakeUndoManager()
    session, mock_replace = _run(FakeFound(old), old, new, doc=FakeDoc(um), split_author=False)

    assert len(session.calls) >= 2                      # surgical: per-changed-run sub-edits
    assert um.entered                                   # surgical always groups in one context
    for _, kwargs in mock_replace.call_args_list:
        assert kwargs["in_undo_context"] is True        # inside the batch context...
        assert kwargs["split_author"] is False          # ... but one color (atomic single-op)


def test_surgical_split_author_on_threads_two_color_inside_context():
    # Complement: with coloring on (default) the surgical sub-edits ask for the two-step (two colors).
    old = "the quick brown fox jumps over the lazy dog"
    new = "the fast brown fox jumps over the sleepy dog"
    session, mock_replace = _run(FakeFound(old), old, new, split_author=True)

    assert len(session.calls) >= 2
    for _, kwargs in mock_replace.call_args_list:
        assert kwargs["in_undo_context"] is True
        assert kwargs["split_author"] is True


# ------------------------------------------- _record_html_atomically (HTML/import delete-then-insert)
# These paths (replace_full_document, replace_single_range_with_content, selection insert) setString("")
# then run a separate HTML import that can throw. record_mutation opens no context, so the wrapper must
# group the whole edit in one undo context and roll back a failed import (no stranded deletion), or
# refuse before mutating when no rollback is available.

def test_html_atomic_rolls_back_partial_edit_on_import_failure():
    um = FakeUndoManager()
    session = FakeSession()

    def mutate():
        um.record_action()                       # the setString("") delete lands in the open context
        raise RuntimeError("import boom")        # then the HTML import fails after the delete

    with pytest.raises(RuntimeError, match="import boom"):
        _record_html_atomically(session, FakeDoc(um), mutate, True, proposed_preview="x")

    assert len(um.entered) == 1 and um.entered[0].startswith(_AGENT_EDIT_UNDO_TITLE)
    assert um.leaves == 1
    assert um.undos == 1            # the partial deletion was undone (document restored)
    assert session.changes == []   # nothing registered as a reviewable change


def test_html_atomic_success_opens_and_closes_context():
    um = FakeUndoManager()
    session = FakeSession()
    _record_html_atomically(session, FakeDoc(um), lambda: um.record_action(), True, proposed_preview="x")

    assert len(um.entered) == 1 and um.entered[0].startswith(_AGENT_EDIT_UNDO_TITLE)
    assert um.leaves == 1 and um.undos == 0   # closed cleanly; no rollback on success
    assert len(session.changes) == 1


def test_html_atomic_refuses_when_no_undo_manager():
    # Recording a reviewable change with no usable manager: refuse BEFORE mutating (an HTML import has
    # no atomic single-op fallback), so the document is never half-edited.
    class NoUndoDoc:
        def getUndoManager(self):
            raise RuntimeError("no undo manager")

    session = FakeSession()
    calls = {"n": 0}
    with pytest.raises(ToolExecutionError):
        _record_html_atomically(session, NoUndoDoc(), lambda: calls.__setitem__("n", calls["n"] + 1),
                                True, proposed_preview="x")
    assert calls["n"] == 0          # refused before running the mutation
    assert session.changes == []


def test_html_atomic_refuses_when_undo_manager_locked():
    um = FakeUndoManager(locked=True)
    session = FakeSession()
    calls = {"n": 0}
    with pytest.raises(ToolExecutionError):
        _record_html_atomically(session, FakeDoc(um), lambda: calls.__setitem__("n", calls["n"] + 1),
                                True, proposed_preview="x")
    assert um.entered == []         # never opened (a locked context is a no-op)
    assert calls["n"] == 0          # never mutated
    assert session.changes == []


def test_html_not_recording_runs_directly_without_context():
    # No review contract -> run the mutation directly (today's behaviour), never touching the undo
    # manager and never refusing.
    class NoUndoDoc:
        def getUndoManager(self):
            raise AssertionError("must not be consulted when not recording")

    session = FakeSession()
    calls = {"n": 0}
    _record_html_atomically(session, NoUndoDoc(), lambda: calls.__setitem__("n", calls["n"] + 1),
                            False, proposed_preview="x")
    assert calls["n"] == 1
    assert len(session.changes) == 1
