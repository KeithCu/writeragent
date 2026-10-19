# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Headless tests for the inline-review resolve guards.

These pin, without a live LibreOffice, the two data-safety properties of accepting/rejecting one
tracked change:

  * the overlap guards fail CLOSED: any error determining whether the USER's own (or another
    agent's) redline overlaps the span is treated as "unsafe" (refuse), never "no overlap, proceed".
  * resolve_agent_change reports success only when EXACTLY the target token was resolved, not
    merely when some redline count dropped.

UNO ranges are faked with integer positions; redline geometry is exercised for real.
"""
from collections import Counter
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from plugin.writer.inline_review import (
    RedlineRow,
    RedlineView,
    ResolveResult,
    ResolveStatus,
    _change_bounds,
    _classify_span_conflicts,
    _is_all_redlines_agent,
    _resolve_all_agent_changes,
    _resolve_overlapping_agent_changes,
    _span_contains_point,
    agent_changes,
    agent_self_resolution_block_reason,
    agent_tokens,
    goto_adjacent_agent_change,
    pending_agent_change_count,
    resolve_agent_change,
    resolve_all_agent_changes,
    resolve_all_with_feedback,
    resolve_change_at_cursor,
    scan_redline_view,
)
from plugin.writer.review_scan import TOKEN_PREFIX, redline_identity_key, redline_is_agent_change


def _sign(n):
    return (n > 0) - (n < 0)


class FakeText:
    """compareRegionStarts/Ends with UNO semantics (1 => R1 before R2); optionally raises."""

    def __init__(self, raise_compare=False):
        self.raise_compare = raise_compare

    def createTextCursorByRange(self, r):
        return FakeRange(r.s, r.e)

    def compareRegionStarts(self, r1, r2):
        if self.raise_compare:
            raise RuntimeError("compare boom")
        return _sign(r2.s - r1.s)

    def compareRegionEnds(self, r1, r2):
        if self.raise_compare:
            raise RuntimeError("compare boom")
        return _sign(r2.e - r1.e)


_DEFAULT_TEXT = FakeText()


class FakeRange:
    """A [s, e] range over a shared text, identified by integer positions."""

    def __init__(self, s, e, text=None):
        self.s = s
        self.e = e
        self.text = text

    def getStart(self):
        return FakeRange(self.s, self.s, text=self.text)

    def getEnd(self):
        return FakeRange(self.e, self.e, text=self.text)

    def getText(self):
        return self.text if self.text is not None else _DEFAULT_TEXT

    def gotoRange(self, other, expand):
        if expand:
            self.s = min(self.s, other.s)
            self.e = max(self.e, other.e)
        else:
            self.s = self.e = other.s

    def gotoStartOfParagraph(self, expand):
        if not expand:
            self.e = self.s

    def gotoEndOfParagraph(self, expand):
        if not expand:
            self.s = self.e


class FakeRedline:
    def __init__(self, comment, s, e, raise_on=None):
        self._props = {
            "RedlineComment": comment,
            "RedlineStart": FakeRange(s, s),
            "RedlineEnd": FakeRange(e, e),
            "RedlineType": "Insert",
            "RedlineIdentifier": "%s:%d:%d" % (comment, s, e),
        }
        self._raise_on = raise_on or set()

    def getPropertyValue(self, name):
        if name in self._raise_on:
            raise RuntimeError("prop boom")
        return self._props[name]


class FakeEnum:
    def __init__(self, items):
        self._items = list(items)

    def hasMoreElements(self):
        return bool(self._items)

    def nextElement(self):
        return self._items.pop(0)


class FakeRedlines:
    def __init__(self, items, raise_enum=False, count=None, raise_count=False):
        self._items = items
        self._raise = raise_enum
        self._count = count          # override getCount() to simulate a silent enum truncation
        self._raise_count = raise_count

    def createEnumeration(self):
        if self._raise:
            raise RuntimeError("enum boom")
        return FakeEnum(self._items)

    def getCount(self):
        if self._raise_count:
            raise RuntimeError("count boom")
        return len(self._items) if self._count is None else self._count


class FakeModel:
    def __init__(self, redlines):
        self._rl = redlines

    def getRedlines(self):
        return self._rl


# --------------------------------------------------------------------------- _span_contains_point

def test_span_contains_point_geometry():
    text = FakeText()
    span = FakeRange(10, 20)
    assert _span_contains_point(text, span, FakeRange(15, 15)) is True   # inside
    assert _span_contains_point(text, span, FakeRange(5, 5)) is False    # left
    assert _span_contains_point(text, span, FakeRange(25, 25)) is False  # right


def test_span_contains_point_raises_on_compare_error():
    # must NOT swallow -> callers fail closed.
    with pytest.raises(RuntimeError):
        _span_contains_point(FakeText(raise_compare=True), FakeRange(10, 20), FakeRange(15, 15))


# --------------------------------------------------------------------------- foreign-redline guard

def test_foreign_redline_overlap_detected():
    span = FakeRange(10, 20)
    model = FakeModel(FakeRedlines([FakeRedline("", 15, 25)]))  # user redline (no token) overlaps
    has_foreign, _ = _classify_span_conflicts(scan_redline_view(model), FakeText(), span, None)
    assert has_foreign is True


def test_foreign_redline_no_overlap():
    span = FakeRange(10, 20)
    model = FakeModel(FakeRedlines([FakeRedline("", 30, 40)]))  # user redline far away
    has_foreign, _ = _classify_span_conflicts(scan_redline_view(model), FakeText(), span, None)
    assert has_foreign is False


def test_foreign_redline_ours_is_not_foreign():
    span = FakeRange(10, 20)
    # Our own agent redline overlapping must be skipped BEFORE any comparison (not "foreign").
    model = FakeModel(FakeRedlines([FakeRedline(TOKEN_PREFIX + "s:1", 15, 25)]))
    has_foreign, _ = _classify_span_conflicts(scan_redline_view(model), FakeText(), span, None)
    assert has_foreign is False


def test_foreign_redline_fails_closed_on_enum_error():
    model = FakeModel(FakeRedlines([], raise_enum=True))
    has_foreign, _ = _classify_span_conflicts(scan_redline_view(model), FakeText(), FakeRange(10, 20), None)
    assert has_foreign is True



def test_foreign_redline_fails_closed_on_compare_error():
    span = FakeRange(10, 20)
    model = FakeModel(FakeRedlines([FakeRedline("", 15, 25)]))
    has_foreign, _ = _classify_span_conflicts(scan_redline_view(model), FakeText(raise_compare=True), span, None)
    assert has_foreign is True


def test_foreign_redline_fails_closed_on_silent_truncation():
    # A user redline far from the span (no overlap), but getCount() reports another redline that the
    # enumeration did NOT yield -- it could overlap in the unscanned tail -> fail closed.
    span = FakeRange(10, 20)
    model = FakeModel(FakeRedlines([FakeRedline("", 30, 40)], count=2))  # 1 enumerated, count says 2
    has_foreign, _ = _classify_span_conflicts(scan_redline_view(model), FakeText(), span, None)
    assert has_foreign is True


def test_foreign_redline_with_no_bounds_fails_closed():
    # A user redline we must protect but whose RedlineStart/End is None -> we can't prove it's
    # outside the span -> fail closed, NOT skip-as-safe (None-bounds hole).
    span = FakeRange(10, 20)
    rl = FakeRedline("", 15, 25)
    rl._props["RedlineStart"] = None
    model = FakeModel(FakeRedlines([rl]))
    has_foreign, _ = _classify_span_conflicts(scan_redline_view(model), FakeText(), span, None)
    assert has_foreign is True


def test_foreign_redline_in_other_text_object_is_skipped():
    # A protected redline anchored in a DIFFERENT text object (e.g. a tracked change inside a
    # text box) cannot overlap a span living in ``text``. The body cursor can't address its range
    # -- createTextCursorByRange raises "End of content node doesn't have the proper start node" --
    # so the guard must SKIP it, not fail closed. Otherwise one redline inside a text box blocks
    # resolving EVERY change in the body. Regression test.
    span = FakeRange(10, 20)
    box_text = FakeText()  # the text box's own text object (a different XText)

    class ForeignRange(FakeRange):
        def getText(self):
            return box_text

    class BodyText(FakeText):
        def createTextCursorByRange(self, r):
            if isinstance(r, ForeignRange):
                raise RuntimeError("End of content node doesn't have the proper start node")
            return super().createTextCursorByRange(r)

    rl = FakeRedline("", 15, 25)                       # a USER redline (protected) ...
    rl._props["RedlineStart"] = ForeignRange(15, 15)   # ... but living inside the text box
    rl._props["RedlineEnd"] = ForeignRange(25, 25)
    model = FakeModel(FakeRedlines([rl]))
    has_foreign, _ = _classify_span_conflicts(scan_redline_view(model), BodyText(), span, None)
    assert has_foreign is False


def test_redline_mapping_failure_in_same_text_fails_closed():
    # If mapping the redline into ``text`` fails but it IS in this text (we cannot confirm it sits
    # in another object), stay fail-closed -- never skip a redline we can't prove is elsewhere.
    span = FakeRange(10, 20)

    class BodyText(FakeText):
        def createTextCursorByRange(self, r):
            raise RuntimeError("transient mapping failure")

    body = BodyText()

    class SameTextRange(FakeRange):
        def getText(self):
            return body  # same text object as the one under resolution

    rl = FakeRedline("", 15, 25)
    rl._props["RedlineStart"] = SameTextRange(15, 15)
    rl._props["RedlineEnd"] = SameTextRange(25, 25)
    model = FakeModel(FakeRedlines([rl]))
    has_foreign, _ = _classify_span_conflicts(scan_redline_view(model), body, span, None)
    assert has_foreign is True


# --------------------------------------------------------------------- other-agent (sibling) guard

def test_other_agent_overlap_detected():
    span = FakeRange(10, 20)
    target = TOKEN_PREFIX + "s:1"
    sibling = TOKEN_PREFIX + "s:2"
    model = FakeModel(FakeRedlines([FakeRedline(sibling, 15, 25)]))
    _, has_sibling = _classify_span_conflicts(scan_redline_view(model), FakeText(), span, target)
    assert has_sibling is True


def test_other_agent_touching_edge_strict_only_when_requested():
    span = FakeRange(10, 20)
    target = TOKEN_PREFIX + "s:1"
    sibling = TOKEN_PREFIX + "s:2"
    touching = FakeModel(FakeRedlines([FakeRedline(sibling, 20, 30)]))
    _, has_strict = _classify_span_conflicts(scan_redline_view(touching), FakeText(), span, target, strict_sibling_edges=True)
    assert has_strict is False
    _, has_touching = _classify_span_conflicts(scan_redline_view(touching), FakeText(), span, target)
    assert has_touching is True
    overlapping = FakeModel(FakeRedlines([FakeRedline(sibling, 15, 25)]))
    _, has_overlapping = _classify_span_conflicts(scan_redline_view(overlapping), FakeText(), span, target, strict_sibling_edges=True)
    assert has_overlapping is True


def test_foreign_touching_edge_stays_inclusive():
    # A user Delete that stacks over the agent's inserted text only touches the agent span.
    model = FakeModel(FakeRedlines([FakeRedline("", 20, 30)]))
    has_foreign, _ = _classify_span_conflicts(scan_redline_view(model), FakeText(), FakeRange(10, 20), None)
    assert has_foreign is True


def test_other_agent_same_token_not_flagged():
    span = FakeRange(10, 20)
    target = TOKEN_PREFIX + "s:1"
    model = FakeModel(FakeRedlines([FakeRedline(target, 15, 25)]))  # the target itself, not a sibling
    _, has_sibling = _classify_span_conflicts(scan_redline_view(model), FakeText(), span, target)
    assert has_sibling is False


def test_other_agent_fails_closed_on_error():
    span = FakeRange(10, 20)
    target = TOKEN_PREFIX + "s:1"
    model = FakeModel(FakeRedlines([], raise_enum=True))
    has_foreign, _ = _classify_span_conflicts(scan_redline_view(model), FakeText(), span, target)
    assert has_foreign is True


# --------------------------------------------------------- resolve targets exactly the change

def _resolve(token, token_sets, refuse_sibling=True, prefer_exact=True, foreign_sets=None):
    """Run resolve_agent_change with snapshots driven through scan_redline_view."""
    model = MagicMock()
    token_side = [t if isinstance(t, tuple) else (set(t), True) for t in token_sets]
    def _mk_foreign(fs):
        if fs is None:
            return ([], True)
        if isinstance(fs, tuple):
            items, ok = fs
            return (list(items), ok)
        return (list(fs), True)
    if foreign_sets is not None:
        foreign_side = [_mk_foreign(fs) for fs in foreign_sets]
    else:
        foreign_side = [([], True)]

    state = {"i": 0}
    def _fake_scan(m):
        t_idx = min(state["i"], len(token_side) - 1)
        f_idx = min(state["i"], len(foreign_side) - 1)
        state["i"] += 1
        toks, tok_ok = token_side[t_idx]
        frn, frn_ok = foreign_side[f_idx]
        reliable = bool(tok_ok and frn_ok)
        rows = []
        for i, t in enumerate(sorted(toks)):
            if t == token:
                rows.append(RedlineRow(token=t, identifier=t, start=FakeRange(10, 15), end=FakeRange(15, 20), kind="Insert", text=FakeText()))
            else:
                rows.append(RedlineRow(token=t, identifier=t, start=FakeRange(30 + 10 * i, 35 + 10 * i), end=FakeRange(35 + 10 * i, 40 + 10 * i), kind="Insert", text=FakeText()))
        for j, f in enumerate(frn):
            if isinstance(f, RedlineRow):
                rows.append(f)
            elif isinstance(f, tuple):
                rows.append(RedlineRow(token=None, identifier=f[0], start=FakeRange(100 + 10 * j, 105 + 10 * j), end=FakeRange(105 + 10 * j, 110 + 10 * j), kind=f[1] if len(f) > 1 else "Insert", text=FakeText(), identity=f))
            else:
                rows.append(RedlineRow(token=None, identifier=f, start=FakeRange(100 + 10 * j, 105 + 10 * j), end=FakeRange(105 + 10 * j, 110 + 10 * j), kind="Insert", text=FakeText()))
        return RedlineView(reliable=reliable, total=len(rows), rows=tuple(rows))

    with patch("plugin.writer.inline_review.scan_redline_view", side_effect=_fake_scan), \
         patch("plugin.writer.inline_review._dispatch_resolve"):
        res, _view = resolve_agent_change(model, MagicMock(), token, True,
                                          refuse_sibling_agent=refuse_sibling, prefer_exact=prefer_exact)
        return res.status


def test_resolve_exact_success_only_target_gone():
    assert _resolve("T", [{"T", "S"}, {"S"}]) == ResolveStatus.RESOLVED


def test_resolve_target_plus_collateral_reports_failure():
    # Target AND a sibling vanished -> NOT "exactly one" -> must fail strictly: the command
    # promises to resolve only the target, so resolving extra changes is a violation, not a success.
    assert _resolve("T", [{"T", "S"}, set()]) == ResolveStatus.PARTIAL


def test_resolve_only_sibling_resolved_reports_failure():
    # The target did NOT resolve but a sibling did -> honest failure, do not claim success.
    assert _resolve("T", [{"T", "S"}, {"T"}]) == ResolveStatus.PARTIAL


def test_resolve_refuses_when_user_redline_disappears():
    # The target resolved cleanly by token, BUT one of the user's own redlines vanished -> the
    # dispatch touched the user's data -> must NOT claim success (user-redline protection).
    assert _resolve("T", [{"T", "S"}, {"S"}], foreign_sets=[({"user-rl"}, True), (set(), True)]) == ResolveStatus.PARTIAL


def test_resolve_refuses_when_foreign_snapshot_unreliable():
    # The target resolved by token, but the user-redline snapshot was UNRELIABLE (couldn't enumerate
    # / read an identifier) -> "couldn't verify" must not pass as "nothing lost" -> fail closed.
    assert _resolve("T", [{"T", "S"}, {"S"}], foreign_sets=[(set(), False), (set(), True)]) == ResolveStatus.UNRELIABLE


def test_resolve_refuses_when_before_token_snapshot_unreliable():
    # The BEFORE agent-token snapshot was incomplete (enumeration failed mid-scan). A sibling could
    # be missing from it, so "removed == {target}" can't prove only the target went -> fail closed.
    assert _resolve("T", [({"T", "S"}, False), ({"S"}, True)]) == ResolveStatus.UNRELIABLE


def test_resolve_refuses_when_after_token_snapshot_unreliable():
    # The AFTER agent-token snapshot was incomplete -> we can't trust which changes remain, so even
    # an apparent "exactly the target removed" is not provable -> fail closed.
    assert _resolve("T", [({"T", "S"}, True), ({"S"}, False)]) == ResolveStatus.PARTIAL


def test_resolve_paragraph_path_refuses_when_after_token_snapshot_unreliable():
    # No-op exact dispatch widens to the paragraph path; there, an unreliable after-token snapshot
    # means we can't prove the target is gone -> must not claim success.
    # calls: before(reliable), exact-after(reliable, no-op), paragraph-after(UNRELIABLE).
    assert _resolve("T", [({"T", "S"}, True), ({"T", "S"}, True), ({"S"}, False)]) == ResolveStatus.PARTIAL


def test_resolve_noop_falls_through_to_paragraph_then_succeeds():
    # Exact dispatch changed nothing (old build) -> paragraph path -> target gone, no user redline
    # lost -> success.
    assert _resolve("T", [{"T", "S"}, {"T", "S"}, {"S"}]) == ResolveStatus.RESOLVED


def test_resolve_absent_token_returns_false():
    assert _resolve("T", [{"S"}]) == ResolveStatus.NOT_ELIGIBLE


def test_resolve_paragraph_path_refuses_when_user_redline_lost():
    # The exact dispatch no-ops (widen to paragraph); the paragraph dispatch then makes a user redline
    # disappear (present before, gone after). The paragraph-path foreign-loss guard must refuse,
    # mirroring the exact-path twin. calls: before, exact-after(no-op), paragraph-after.
    assert _resolve(
        "T",
        [({"T", "S"}, True), ({"T", "S"}, True), ({"S"}, True)],
        foreign_sets=[({"u"}, True), ({"u"}, True), (set(), True)],
    ) == ResolveStatus.PARTIAL


def test_resolve_stacked_delete_pop_reported_as_partial():
    # Same foreign identifier before and after, identity changes from Delete to Insert (stacked pop).
    # Exact path must be PARTIAL.
    key_del = ("user-1", "Delete", "Author", None, None)
    key_ins = ("user-1", "Insert", "Author", None, None)
    assert _resolve("T", [{"T", "S"}, {"S"}], foreign_sets=[([key_del], True), ([key_ins], True)]) == ResolveStatus.PARTIAL


def test_resolve_duplicate_foreign_identity_key_lost_reported_as_partial():
    # Two foreign rows with the same full key, one gone after. PARTIAL.
    key = ("user-1", "Delete", "Author", None, None)
    assert _resolve("T", [{"T", "S"}, {"S"}], foreign_sets=[([key, key], True), ([key], True)]) == ResolveStatus.PARTIAL


def test_resolve_foreign_split_piece_added_stays_resolved():
    # Before key kept and a second key added (split piece). Still RESOLVED when the agent token is the only token removed.
    key_orig = ("user-1", "Insert", "Author", (2026, 1, 1, 0, 0, 0, 0), None)
    key_split = ("user-2", "Insert", "Author", (2026, 1, 1, 0, 0, 0, 0), None)
    assert _resolve("T", [{"T", "S"}, {"S"}], foreign_sets=[([key_orig], True), ([key_orig, key_split], True)]) == ResolveStatus.RESOLVED


# ----------------------------------------------------------------- _all_redlines_are_agent

def _agent_rl():
    return FakeRedline(TOKEN_PREFIX + "s:1", 0, 0)


def _user_rl():
    return FakeRedline("", 0, 0)  # no agent token comment


def test_all_redlines_are_agent_true_when_complete_and_all_agent():
    model = FakeModel(FakeRedlines([_agent_rl(), _agent_rl()]))
    assert _is_all_redlines_agent(scan_redline_view(model)) is True


def test_all_redlines_are_agent_false_with_a_user_redline():
    model = FakeModel(FakeRedlines([_agent_rl(), _user_rl()]))
    assert _is_all_redlines_agent(scan_redline_view(model)) is False


def test_all_redlines_are_agent_false_on_silent_truncation():
    # The enumeration yields only the two leading agent redlines but getCount() reports 3 (a user
    # redline sits in the unscanned tail). Count cross-check must fail closed.
    model = FakeModel(FakeRedlines([_agent_rl(), _agent_rl()], count=3))
    assert _is_all_redlines_agent(scan_redline_view(model)) is False


def test_all_redlines_are_agent_false_on_enum_error():
    model = FakeModel(FakeRedlines([_agent_rl()], raise_enum=True))
    assert _is_all_redlines_agent(scan_redline_view(model)) is False


def test_all_redlines_are_agent_false_on_count_error():
    model = FakeModel(FakeRedlines([_agent_rl()], raise_count=True))
    assert _is_all_redlines_agent(scan_redline_view(model)) is False


def test_all_redlines_are_agent_false_on_empty_document():
    model = FakeModel(FakeRedlines([]))
    assert _is_all_redlines_agent(scan_redline_view(model)) is False


def test_scan_redline_view_property_read_counts():
    class _Stamp:
        Year, Month, Day, Hours, Minutes, Seconds, NanoSeconds = 2026, 9, 26, 11, 49, 0, 0

    class CountingFakeRedline(FakeRedline):
        def __init__(self, comment, s, e, author="Author", stamp=None):
            super().__init__(comment, s, e)
            self._props["RedlineAuthor"] = author
            self._props["RedlineDateTime"] = stamp
            self.reads: Counter[str] = Counter()

        def getPropertyValue(self, name):
            self.reads[name] += 1
            return super().getPropertyValue(name)

    agent_rl = CountingFakeRedline(TOKEN_PREFIX + "s:1", 0, 5)
    model_agent = FakeModel(FakeRedlines([agent_rl]))
    view_agent = scan_redline_view(model_agent)
    assert view_agent.reliable is True
    assert agent_rl.reads["RedlineAuthor"] == 0
    assert agent_rl.reads["RedlineDateTime"] == 0
    assert agent_rl.reads["RedlineComment"] == 1
    assert agent_rl.reads["RedlineIdentifier"] == 1
    assert agent_rl.reads["RedlineType"] == 1
    assert view_agent.rows[0].identity is None

    user_rl = CountingFakeRedline("user note", 10, 15, author="Alice", stamp=_Stamp)
    model_user = FakeModel(FakeRedlines([user_rl]))
    view_user = scan_redline_view(model_user)
    assert view_user.reliable is True
    assert view_user.rows[0].identity == redline_identity_key(user_rl)



# -------------------------------------------- token/foreign snapshot completeness (getCount)

def test_agent_change_tokens_reliable_when_complete():
    model = FakeModel(FakeRedlines([_agent_rl(), _user_rl()]))
    view = scan_redline_view(model)
    assert view.reliable is True and agent_tokens(view) == {TOKEN_PREFIX + "s:1"}


def test_agent_change_tokens_unreliable_on_silent_truncation():
    # The enumeration yields 1 redline but getCount() reports 2 (a redline silently dropped) -> the
    # token snapshot can't be trusted for a "removed == {token}" comparison -> reliable False.
    model = FakeModel(FakeRedlines([_agent_rl()], count=2))
    view = scan_redline_view(model)
    assert view.reliable is False


def test_agent_change_tokens_unreliable_on_count_error():
    model = FakeModel(FakeRedlines([_agent_rl()], raise_count=True))
    view = scan_redline_view(model)
    assert view.reliable is False


def test_foreign_redline_ids_reliable_when_complete():
    model = FakeModel(FakeRedlines([_agent_rl(), _user_rl()]))
    view = scan_redline_view(model)
    assert view.reliable is True  # the user redline (no token) is the only foreign id, scan complete


def test_foreign_redline_ids_unreliable_on_silent_truncation():
    # getCount() says 3 but only 2 redlines enumerated -> a user redline may sit in the unscanned
    # tail; we must not later read "no user redline lost" off this incomplete set -> reliable False.
    model = FakeModel(FakeRedlines([_agent_rl(), _user_rl()], count=3))
    view = scan_redline_view(model)
    assert view.reliable is False


def test_foreign_redline_ids_unreliable_on_count_error():
    model = FakeModel(FakeRedlines([_user_rl()], raise_count=True))
    view = scan_redline_view(model)
    assert view.reliable is False


# --------------------------------------------------- _change_bounds fail-closed

def test_change_bounds_returns_span_on_complete_scan():
    # The two marks of a replace (Delete + Insert) share the token; a complete scan returns the
    # union span (non-None), so the caller can resolve the WHOLE change.
    target = TOKEN_PREFIX + "s:1"
    model = FakeModel(FakeRedlines([FakeRedline(target, 10, 15), FakeRedline(target, 15, 20)]))
    left, right = _change_bounds(model, target)
    assert left is not None and right is not None


def test_change_bounds_fail_closed_on_silent_truncation():
    # getCount() says 3 but only the 2 target marks enumerate -> a mark may be in the unscanned tail
    # -> returning a span here could cover only half the change -> fail closed.
    target = TOKEN_PREFIX + "s:1"
    model = FakeModel(FakeRedlines([FakeRedline(target, 10, 15), FakeRedline(target, 15, 20)], count=3))
    assert _change_bounds(model, target) == (None, None)



def test_change_bounds_fail_closed_on_none_target_bounds():
    target = TOKEN_PREFIX + "s:1"
    rl = FakeRedline(target, 15, 20)
    rl._props["RedlineEnd"] = None
    model = FakeModel(FakeRedlines([FakeRedline(target, 10, 15), rl]))
    assert _change_bounds(model, target) == (None, None)


# ------------------------------------------ refuse BEFORE dispatch on an unreliable before-snapshot

def test_resolve_does_not_dispatch_when_before_token_snapshot_unreliable():
    model = MagicMock()
    unreliable_view = RedlineView(
        reliable=False,
        total=1,
        rows=(RedlineRow(token="T", identifier="T", start=FakeRange(10, 15), end=FakeRange(15, 20), kind="Insert", text=FakeText()),),
    )
    with patch("plugin.writer.inline_review.scan_redline_view", return_value=unreliable_view), \
         patch("plugin.writer.inline_review._dispatch_resolve") as dispatch:
        res, _ = resolve_agent_change(model, MagicMock(), "T", True)
    assert res.status == ResolveStatus.UNRELIABLE
    dispatch.assert_not_called()


def test_resolve_does_not_dispatch_when_before_foreign_snapshot_unreliable():
    model = MagicMock()
    unreliable_view = RedlineView(
        reliable=False,
        total=2,
        rows=(
            RedlineRow(token="T", identifier="T", start=FakeRange(10, 15), end=FakeRange(15, 20), kind="Insert", text=FakeText()),
            RedlineRow(token=None, identifier="user", start=FakeRange(30, 40), end=FakeRange(40, 50), kind="Insert", text=FakeText()),
        ),
    )
    with patch("plugin.writer.inline_review.scan_redline_view", return_value=unreliable_view), \
         patch("plugin.writer.inline_review._dispatch_resolve") as dispatch:
        res, _ = resolve_agent_change(model, MagicMock(), "T", True)
    assert res.status == ResolveStatus.UNRELIABLE
    dispatch.assert_not_called()


def test_agent_redlines_does_not_hang_on_mock_enumeration():
    # Bootstrap paths (get_tools -> install_review_toolbar) hit pending_agent_change_count on open
    # documents. MagicMock.hasMoreElements() stays truthy forever unless iteration is capped by
    # getCount() -- an uncapped while-hasMoreElements loop hung pytest (see test_calc_analyze_data).
    doc = MagicMock()
    doc.getRedlines.return_value.getCount.return_value = 0
    enum = MagicMock()
    enum.hasMoreElements.return_value = True
    doc.getRedlines.return_value.createEnumeration.return_value = enum
    assert agent_changes(doc) == []
    assert pending_agent_change_count(doc) == 0


# --------------------------------------------------- agent-self-resolution guard (B3 fix)

def test_block_reason_none_when_no_redlines_api():
    # An object with no getRedlines() -> nothing to clobber -> bulk dispatch (a no-op) is allowed.
    assert agent_self_resolution_block_reason(object()) is None


def test_block_reason_none_on_empty_document():
    assert agent_self_resolution_block_reason(FakeModel(FakeRedlines([]))) is None


def test_block_reason_blocks_when_agent_change_present():
    model = FakeModel(FakeRedlines([_agent_rl()]))
    reason = agent_self_resolution_block_reason(model)
    assert reason is not None and "agent edit" in reason.lower()


def test_block_reason_allows_user_only_redlines():
    # The user's own tracked changes (no token) may be bulk-resolved on request.
    assert agent_self_resolution_block_reason(FakeModel(FakeRedlines([_user_rl()]))) is None


def test_block_reason_fails_closed_on_silent_truncation():
    # Redlines present but the scan can't be trusted -> assume an agent change might hide in the tail.
    model = FakeModel(FakeRedlines([_user_rl()], count=2))
    assert agent_self_resolution_block_reason(model) is not None


def test_block_reason_fails_closed_on_count_error():
    model = FakeModel(FakeRedlines([_user_rl()], raise_count=True))
    assert agent_self_resolution_block_reason(model) is not None


def test_block_reason_none_when_geometry_raises_on_user_comment():
    # Non-agent comment with geometry failures does not block bulk resolve.
    for prop in ("RedlineStart", "RedlineIdentifier", "RedlineType"):
        model = FakeModel(FakeRedlines([FakeRedline("user note", 0, 0, raise_on={prop})]))
        assert agent_self_resolution_block_reason(model) is None


def test_block_reason_fails_closed_when_comment_raises():
    model = FakeModel(FakeRedlines([FakeRedline("user note", 0, 0, raise_on={"RedlineComment"})]))
    reason = agent_self_resolution_block_reason(model)
    assert reason is not None and "reliably" in reason.lower()


def test_block_reason_fails_closed_when_hasattr_raises():
    class RaisingModel:
        def __getattr__(self, name):
            raise RuntimeError("hasattr boom")

    reason = agent_self_resolution_block_reason(RaisingModel())
    assert reason is not None and "reliably" in reason.lower()


def test_inline_review_uno_skips_windows_leftover_view_cursor() -> None:
    """GHA 34687044125: leftover Hidden view cursor hung in _caret_in."""
    src = Path(__file__).with_name("test_inline_review_uno.py").read_text(encoding="utf-8")
    assert "skip_windows_leftover_hidden_load" in src
    assert "inline_review view cursor leftover reuse" in src
    assert "34687044125" in src


def test_redline_is_agent_change_true_for_token():
    assert redline_is_agent_change(_agent_rl()) == (True, True)


def test_redline_is_agent_change_false_for_user():
    assert redline_is_agent_change(_user_rl()) == (False, True)


def test_redline_is_agent_change_unreadable_fails_closed():
    rl = FakeRedline("", 0, 0, raise_on={"RedlineComment"})
    assert redline_is_agent_change(rl) == (False, False)


# ------------------------------------------------ overlapping agent changes resolve together (#36)

class _ViewModel(FakeModel):
    def __init__(self, redlines):
        super().__init__(redlines)
        self.controller = MagicMock()

    def getCurrentController(self):
        return self.controller


def _overlap_doc(*extra):
    t1, t2 = TOKEN_PREFIX + "a:0", TOKEN_PREFIX + "b:0"
    # T1 inserted [0, 10]; T2 later deleted [4, 6] inside it, splitting T1 in two pieces.
    items = [FakeRedline(t1, 0, 4), FakeRedline(t2, 4, 6), FakeRedline(t1, 6, 10), *extra]
    return _ViewModel(FakeRedlines(items)), items, t1, t2


def _dispatch_removing(items, *per_pass):
    """_dispatch_resolve stand-in: pass N removes the redlines whose comment is in per_pass[N]."""
    passes = list(per_pass)

    def _dispatch(ctx, controller, accept):
        gone = passes.pop(0) if passes else set()
        items[:] = [rl for rl in items if rl.getPropertyValue("RedlineComment") not in gone]
    return _dispatch


def test_overlapping_agent_changes_resolve_together():
    model, items, t1, t2 = _overlap_doc()
    with patch("plugin.writer.inline_review._dispatch_resolve", side_effect=_dispatch_removing(items, {t1, t2})):
        assert _resolve_overlapping_agent_changes(model, MagicMock(), t1, True).resolved_count == 2
    assert items == []


def test_overlapping_reject_takes_one_layer_per_pass():
    """Rejecting a Delete stacked on an Insert restores the inserted text first; a second pass
    over what is left of the group finishes it (the first live run left "palavra sobrando ")."""
    model, items, t1, t2 = _overlap_doc()
    fake = _dispatch_removing(items, {t2}, {t1})
    with patch("plugin.writer.inline_review._dispatch_resolve", side_effect=fake) as dispatch:
        assert _resolve_overlapping_agent_changes(model, MagicMock(), t1, False).resolved_count == 2
    assert dispatch.call_count == 2


def test_overlapping_group_refuses_a_user_redline_inside():
    model, items, t1, _t2 = _overlap_doc(FakeRedline("", 7, 8))
    with patch("plugin.writer.inline_review._dispatch_resolve") as dispatch:
        assert _resolve_overlapping_agent_changes(model, MagicMock(), t1, True).resolved_count == 0
    dispatch.assert_not_called()


def test_a_lone_change_is_not_a_group():
    lone = TOKEN_PREFIX + "c:0"
    model = _ViewModel(FakeRedlines([FakeRedline(lone, 0, 4), FakeRedline(TOKEN_PREFIX + "d:0", 8, 9)]))
    with patch("plugin.writer.inline_review._dispatch_resolve") as dispatch:
        assert _resolve_overlapping_agent_changes(model, MagicMock(), lone, True).resolved_count == 0
    dispatch.assert_not_called()


def test_touching_agent_changes_are_not_grouped():
    """Only shared text groups changes: an independent change right next to the clicked one
    (an 'after' insert beside an earlier one) is left alone."""
    t1, t2 = TOKEN_PREFIX + "a:0", TOKEN_PREFIX + "b:0"
    model = _ViewModel(FakeRedlines([FakeRedline(t1, 0, 4), FakeRedline(t2, 4, 8)]))
    with patch("plugin.writer.inline_review._dispatch_resolve") as dispatch:
        assert _resolve_overlapping_agent_changes(model, MagicMock(), t1, True).resolved_count == 0
    dispatch.assert_not_called()


def test_a_pass_that_uncovers_a_user_change_stops_there():
    model, items, t1, t2 = _overlap_doc()

    def first_pass(ctx, controller, accept):
        # Popping T2's layer left a user redline where it was.
        items[:] = [rl for rl in items if rl.getPropertyValue("RedlineComment") != t2] + [FakeRedline("", 4, 6)]

    with patch("plugin.writer.inline_review._dispatch_resolve", side_effect=first_pass) as dispatch:
        res = _resolve_overlapping_agent_changes(model, MagicMock(), t1, False)
        assert res.status == ResolveStatus.PARTIAL and res.resolved_count == 0
    assert dispatch.call_count == 1


class _ClosedDocument:
    """Disposed document: getRedlines() raises. scan_redlines returns unreliable
    before any redline callback. Resolve must not dispatch and must not report success."""

    def getRedlines(self):
        raise RuntimeError("disposed")


def test_closed_document_resolve_fails_closed_without_dispatch():
    model = _ClosedDocument()
    ctx = MagicMock()
    token = TOKEN_PREFIX + "s:1"
    with patch("plugin.writer.inline_review._dispatch_resolve") as dispatch:
        assert resolve_agent_change(model, ctx, token, True)[0].status == ResolveStatus.UNRELIABLE
        assert _resolve_all_agent_changes(model, ctx, True).status == ResolveStatus.UNRELIABLE
        assert resolve_all_agent_changes(model, ctx, True) == 0
        assert _resolve_overlapping_agent_changes(model, ctx, token, True).status == ResolveStatus.UNRELIABLE
    dispatch.assert_not_called()
    # Global AcceptAll/RejectAll uses the service manager, not _dispatch_resolve.
    ctx.getServiceManager.assert_not_called()

    n, msg = resolve_all_with_feedback(model, ctx, True)
    assert n == 0
    assert "nothing was" in msg
    assert "some may have been" not in msg

    reason = agent_self_resolution_block_reason(model)
    assert reason is not None and "reliably" in reason

    assert _is_all_redlines_agent(scan_redline_view(model)) is False
    assert _change_bounds(model, token) == (None, None)
    has_foreign, _ = _classify_span_conflicts(scan_redline_view(model), FakeText(), FakeRange(10, 20), None)
    assert has_foreign is True
    view = scan_redline_view(model)
    assert agent_tokens(view) == set() and view.reliable is False
    assert view.reliable is False


# Per-redline read failures (e.g. the document closed mid-scan) still fail closed:
# scan_redlines turns any on_item exception into reliable=False.

def test_foreign_redline_fails_closed_on_per_redline_error():
    span = FakeRange(10, 20)
    # A user redline whose RedlineStart read blows up -> can't tell if it overlaps -> refuse.
    model = FakeModel(FakeRedlines([FakeRedline("", 15, 25, raise_on={"RedlineStart"})]))
    has_foreign, _ = _classify_span_conflicts(scan_redline_view(model), FakeText(), span, None)
    assert has_foreign is True


def test_all_redlines_are_agent_false_on_unreadable_comment():
    model = FakeModel(FakeRedlines([_agent_rl(), FakeRedline("", 0, 0, raise_on={"RedlineComment"})]))
    assert _is_all_redlines_agent(scan_redline_view(model)) is False


def test_change_bounds_fail_closed_on_unreadable_comment():
    # A redline whose comment can't be read might BE a target mark we'd miss -> fail closed.
    target = TOKEN_PREFIX + "s:1"
    model = FakeModel(FakeRedlines([FakeRedline(target, 10, 15),
                                    FakeRedline("", 0, 0, raise_on={"RedlineComment"})]))
    assert _change_bounds(model, target) == (None, None)


def test_change_bounds_fail_closed_on_unreadable_target_bounds():
    # A TARGET mark whose Start can't be read -> we can't build the complete span -> fail closed
    # (the old code skipped it and could return only the other half).
    target = TOKEN_PREFIX + "s:1"
    model = FakeModel(FakeRedlines([FakeRedline(target, 10, 15),
                                    FakeRedline(target, 15, 20, raise_on={"RedlineStart"})]))
    assert _change_bounds(model, target) == (None, None)


def test_cursor_resolve_extra_token_removed_does_not_call_group_helper_and_returns_partial():
    # Extra token removed on exact path -> PARTIAL -> cursor resolve does NOT run group helper
    model = MagicMock()
    ctx = MagicMock()
    fake_view = RedlineView(reliable=True, total=1, rows=(RedlineRow(token="T", identifier="T", start=FakeRange(10, 15), end=FakeRange(15, 20), kind="Insert", text=FakeText()),))
    with patch("plugin.writer.inline_review.scan_redline_view", return_value=fake_view), \
         patch("plugin.writer.inline_review.cursor_in_agent_change", return_value="T"), \
         patch("plugin.writer.inline_review.resolve_agent_change",
               return_value=(ResolveResult(ResolveStatus.PARTIAL), fake_view)), \
         patch("plugin.writer.inline_review._resolve_overlapping_agent_changes") as group_helper:
        ok, msg = resolve_change_at_cursor(model, ctx, True)
    assert ok is False
    assert "couldn't confirm the result" in msg
    group_helper.assert_not_called()


def test_cursor_resolve_group_dispatch_with_mismatched_after_snapshot_is_partial():
    # Group helper dispatched but after-snapshot mismatched -> PARTIAL -> cursor returns partial message, not hint
    model = MagicMock()
    ctx = MagicMock()
    fake_view = RedlineView(reliable=True, total=2, rows=(
        RedlineRow(token="T", identifier="T", start=FakeRange(10, 15), end=FakeRange(15, 20), kind="Insert", text=FakeText()),
        RedlineRow(token="S", identifier="S", start=FakeRange(15, 18), end=FakeRange(18, 25), kind="Insert", text=FakeText()),
    ))
    with patch("plugin.writer.inline_review.scan_redline_view", return_value=fake_view), \
         patch("plugin.writer.inline_review.cursor_in_agent_change", return_value="T"), \
         patch("plugin.writer.inline_review.resolve_agent_change",
               return_value=(ResolveResult(ResolveStatus.REFUSED_SIBLING), fake_view)), \
         patch("plugin.writer.inline_review._resolve_overlapping_agent_changes",
               return_value=ResolveResult(ResolveStatus.PARTIAL, 0)):
        ok, msg = resolve_change_at_cursor(model, ctx, True)
    assert ok is False
    assert "couldn't confirm the result" in msg
    assert "Edit > Track Changes > Manage" in msg


def test_resolve_all_returns_unreliable_when_first_refused_before_dispatch_and_next_unreliable():
    rows = (RedlineRow(token="T", identifier="T", start=FakeRange(10, 15), end=FakeRange(15, 20), kind="Insert", text=FakeText()),)
    entry_view = RedlineView(reliable=True, total=1, rows=rows)
    unreliable_view = RedlineView(reliable=False, total=1, rows=rows)
    with patch("plugin.writer.inline_review.scan_redline_view", return_value=entry_view), \
         patch("plugin.writer.inline_review.resolve_agent_change",
               return_value=(ResolveResult(ResolveStatus.REFUSED_FOREIGN), unreliable_view)), \
         patch("plugin.writer.inline_review._is_all_redlines_agent", return_value=False):
        res = _resolve_all_agent_changes(MagicMock(), MagicMock(), True)
    assert res.status == ResolveStatus.UNRELIABLE
    assert res.resolved == 0


def test_getString_raises_still_counts_and_navigates_without_getString_calls():
    class ThrowingText(FakeText):
        def getString(self):
            raise RuntimeError("getString boom")

    class ThrowingRange(FakeRange):
        def getText(self):
            return ThrowingText()

    class ThrowingRedline(FakeRedline):
        def __init__(self, comment, s, e):
            super().__init__(comment, s, e)
            self._props["RedlineStart"] = ThrowingRange(s, s)
            self._props["RedlineEnd"] = ThrowingRange(e, e)

    class CountingRedlines:
        def __init__(self, items):
            self._items = items
            self.enums = 0
        def createEnumeration(self):
            self.enums += 1
            return FakeEnum(self._items)
        def getCount(self):
            return len(self._items)

    class CountingDoc(_ViewModel):
        def __init__(self, redlines):
            super().__init__(redlines)
            vc = MagicMock()
            vc.getText.return_value = ThrowingText()
            vc.getStart.return_value = ThrowingRange(0, 0)
            vc.getEnd.return_value = ThrowingRange(0, 0)
            vc.isCollapsed.return_value = True
            self.controller.getViewCursor.return_value = vc

    t1 = TOKEN_PREFIX + "s:1"
    rl = ThrowingRedline(t1, 10, 20)
    redlines = CountingRedlines([rl])
    doc = CountingDoc(redlines)

    assert pending_agent_change_count(doc) == 1
    assert redlines.enums == 1

    tok = goto_adjacent_agent_change(doc, True)
    assert tok == t1
    assert redlines.enums == 2


def test_pending_agent_change_count_includes_token_after_geometry_error():
    # A redline whose geometry raises does not abort the comment walk; later tokens are counted.
    bad_geom = FakeRedline("user note", 0, 5, raise_on={"RedlineStart"})
    good_agent = FakeRedline(TOKEN_PREFIX + "s:2", 10, 20)
    model = FakeModel(FakeRedlines([bad_geom, good_agent]))
    assert pending_agent_change_count(model) == 1


def test_scan_count_exact_resolve_agent_change_success():
    t = TOKEN_PREFIX + "s:1"
    items = [FakeRedline(t, 10, 20)]

    class CountingRedlines:
        def __init__(self):
            self.enums = 0
        def createEnumeration(self):
            self.enums += 1
            return FakeEnum(list(items))
        def getCount(self):
            return len(items)

    model = _ViewModel(CountingRedlines())
    ctx = MagicMock()
    def fake_dispatch(ctx, ctrl, accept):
        items.clear()

    with patch("plugin.writer.inline_review._dispatch_resolve", side_effect=fake_dispatch):
        res, _ = resolve_agent_change(model, ctx, t, True)
    assert res.status == ResolveStatus.RESOLVED
    assert model._rl.enums == 2


def test_scan_count_resolve_change_at_cursor_exact_success():
    t = TOKEN_PREFIX + "s:1"
    items = [FakeRedline(t, 10, 20)]

    class CountingRedlines:
        def __init__(self):
            self.enums = 0
        def createEnumeration(self):
            self.enums += 1
            return FakeEnum(list(items))
        def getCount(self):
            return len(items)

    model = _ViewModel(CountingRedlines())
    vc = MagicMock()
    vc.getText.return_value = FakeText()
    vc.getStart.return_value = FakeRange(15, 15)
    model.controller.getViewCursor.return_value = vc

    def fake_dispatch(ctx, ctrl, accept):
        items.clear()

    with patch("plugin.writer.inline_review._dispatch_resolve", side_effect=fake_dispatch):
        ok, _ = resolve_change_at_cursor(model, MagicMock(), True)
    assert ok is True
    assert model._rl.enums == 2


def test_scan_count_resolve_change_at_cursor_with_provided_view():
    from plugin.writer.inline_review import cursor_in_agent_change

    t = TOKEN_PREFIX + "s:1"
    items = [FakeRedline(t, 10, 20)]

    class CountingRedlines:
        def __init__(self):
            self.enums = 0
        def createEnumeration(self):
            self.enums += 1
            return FakeEnum(list(items))
        def getCount(self):
            return len(items)

    model = _ViewModel(CountingRedlines())
    vc = MagicMock()
    vc.getText.return_value = FakeText()
    vc.getStart.return_value = FakeRange(15, 15)
    model.controller.getViewCursor.return_value = vc

    def fake_dispatch(ctx, ctrl, accept):
        items.clear()

    view = scan_redline_view(model)
    assert model._rl.enums == 1
    token = cursor_in_agent_change(model, view=view)
    assert token == t

    with patch("plugin.writer.inline_review._dispatch_resolve", side_effect=fake_dispatch):
        ok, _ = resolve_change_at_cursor(model, MagicMock(), True, token=token, view=view)
    assert ok is True
    assert model._rl.enums == 2


def test_scan_count_two_change_group_cleared_in_one_dispatch():
    t1 = TOKEN_PREFIX + "a:0"
    t2 = TOKEN_PREFIX + "b:0"
    items = [FakeRedline(t1, 0, 10), FakeRedline(t2, 5, 15)]

    class CountingRedlines:
        def __init__(self):
            self.enums = 0
        def createEnumeration(self):
            self.enums += 1
            return FakeEnum(list(items))
        def getCount(self):
            return len(items)

    model = _ViewModel(CountingRedlines())

    def fake_dispatch(ctx, ctrl, accept):
        items.clear()

    with patch("plugin.writer.inline_review._dispatch_resolve", side_effect=fake_dispatch):
        res = _resolve_overlapping_agent_changes(model, MagicMock(), t1, True)
    assert res.status == ResolveStatus.RESOLVED and res.resolved_count == 2
    assert model._rl.enums == 2


def test_scan_count_resolve_all_fast_path_and_feedback():
    t1 = TOKEN_PREFIX + "a:0"
    t2 = TOKEN_PREFIX + "b:0"
    items = [FakeRedline(t1, 0, 10), FakeRedline(t2, 20, 30)]

    class CountingRedlines:
        def __init__(self):
            self.enums = 0
        def createEnumeration(self):
            self.enums += 1
            return FakeEnum(list(items))
        def getCount(self):
            return len(items)

    model = _ViewModel(CountingRedlines())
    ctx = MagicMock()

    def fake_global(frame, cmd, unused, code, args):
        items.clear()

    ctx.getServiceManager().createInstanceWithContext().executeDispatch.side_effect = fake_global

    n = resolve_all_agent_changes(model, ctx, True)
    assert n == 2
    assert model._rl.enums == 2

    # Feedback on that path
    items[:] = [FakeRedline(t1, 0, 10), FakeRedline(t2, 20, 30)]
    model._rl.enums = 0
    n, _ = resolve_all_with_feedback(model, ctx, True)
    assert n == 2
    assert model._rl.enums == 2


def test_scan_count_resolve_all_mixed_three_changes_with_user_redline():
    t1 = TOKEN_PREFIX + "a:0"
    t2 = TOKEN_PREFIX + "b:0"
    t3 = TOKEN_PREFIX + "c:0"
    user = ""
    items = [
        FakeRedline(t1, 0, 5),
        FakeRedline(t2, 20, 25),
        FakeRedline(t3, 40, 45),
        FakeRedline(user, 100, 105),
    ]

    class CountingRedlines:
        def __init__(self):
            self.enums = 0
        def createEnumeration(self):
            self.enums += 1
            return FakeEnum(list(items))
        def getCount(self):
            return len(items)

    model = _ViewModel(CountingRedlines())
    ctx = MagicMock()

    def fake_dispatch(ctx, ctrl, accept):
        for i, rl in enumerate(items):
            if rl.getPropertyValue("RedlineComment").startswith(TOKEN_PREFIX):
                items.pop(i)
                break

    with patch("plugin.writer.inline_review._dispatch_resolve", side_effect=fake_dispatch):
        n = resolve_all_agent_changes(model, ctx, True)
    assert n == 3
    assert model._rl.enums == 4


def test_resolve_agent_change_gotorange_raises_before_dispatch():
    t = TOKEN_PREFIX + "s:1"
    items = [FakeRedline(t, 10, 20)]

    class ThrowingGotoModel(_ViewModel):
        def __init__(self, redlines):
            super().__init__(redlines)
            vc = MagicMock()
            vc.getText.return_value = FakeText()
            vc.getStart.return_value = FakeRange(15, 15)
            vc.gotoRange.side_effect = RuntimeError("gotoRange boom")
            self.controller.getViewCursor.return_value = vc

    model = ThrowingGotoModel(FakeRedlines(items))
    ctx = MagicMock()
    with patch("plugin.writer.inline_review._dispatch_resolve") as dispatch:
        res, _ = resolve_agent_change(model, ctx, t, True)
        assert res.status == ResolveStatus.UNRELIABLE
        dispatch.assert_not_called()

        ok, msg = resolve_change_at_cursor(model, ctx, True)
        assert ok is False
        assert "some may have been resolved" not in msg
        assert "nothing was changed" in msg.lower()


def test_goto_adjacent_agent_change_returns_none_when_gotorange_raises():
    t1 = TOKEN_PREFIX + "s:1"
    rl = FakeRedline(t1, 10, 20)
    redlines = FakeRedlines([rl])

    class ThrowingGotoDoc(_ViewModel):
        def __init__(self, redlines):
            super().__init__(redlines)
            vc = MagicMock()
            vc.getText.return_value = FakeText()
            vc.getStart.return_value = FakeRange(0, 0)
            vc.getEnd.return_value = FakeRange(0, 0)
            vc.isCollapsed.return_value = True
            vc.gotoRange.side_effect = RuntimeError("gotoRange failed")
            self.controller.getViewCursor.return_value = vc

    doc = ThrowingGotoDoc(redlines)
    tok = goto_adjacent_agent_change(doc, True)
    assert tok is None


def test_scan_count_click_popup_enumerates_once():
    from plugin.writer.review_click_popup import _show_popup_and_resolve

    t = TOKEN_PREFIX + "s:1"
    items = [FakeRedline(t, 10, 20)]

    class CountingRedlines:
        def __init__(self):
            self.enums = 0
        def createEnumeration(self):
            self.enums += 1
            return FakeEnum(list(items))
        def getCount(self):
            return len(items)

    model = _ViewModel(CountingRedlines())
    vc = MagicMock()
    vc.getText.return_value = FakeText()
    vc.getStart.return_value = FakeRange(15, 15)
    model.controller.getViewCursor.return_value = vc

    mock_popup = MagicMock()
    mock_popup.execute.return_value = 0
    mock_smgr = MagicMock()
    mock_smgr.createInstanceWithContext.return_value = mock_popup
    mock_ctx = MagicMock()
    mock_ctx.getServiceManager.return_value = mock_smgr

    with patch("plugin.framework.uno_context.get_ctx", return_value=mock_ctx):
        _show_popup_and_resolve(model, MagicMock(), 100, 100)

    assert model._rl.enums == 1
    mock_popup.execute.assert_called_once()


def test_fast_travel_next_visits_across_different_text_objects():
    # A headless test with two FakeText identities: body change, cell change, later body change,
    # caret in the body between the two body changes, Next returns the cell token.
    body_text = FakeText()
    cell_text = FakeText()

    class CellRange(FakeRange):
        def getText(self):
            return cell_text

    class BodyRange(FakeRange):
        def getText(self):
            return body_text

    t_body1 = TOKEN_PREFIX + "s:1"
    t_cell = TOKEN_PREFIX + "s:2"
    t_body2 = TOKEN_PREFIX + "s:3"

    rl1 = FakeRedline(t_body1, 10, 15)
    rl1._props["RedlineStart"] = BodyRange(10, 10)
    rl1._props["RedlineEnd"] = BodyRange(15, 15)

    rl2 = FakeRedline(t_cell, 5, 10)
    rl2._props["RedlineStart"] = CellRange(5, 5)
    rl2._props["RedlineEnd"] = CellRange(10, 10)

    rl3 = FakeRedline(t_body2, 30, 35)
    rl3._props["RedlineStart"] = BodyRange(30, 30)
    rl3._props["RedlineEnd"] = BodyRange(35, 35)

    model = _ViewModel(FakeRedlines([rl1, rl2, rl3]))
    vc = MagicMock()
    vc.getText.return_value = body_text
    vc.isCollapsed.return_value = True
    vc.getStart.return_value = BodyRange(20, 20)
    vc.getEnd.return_value = BodyRange(20, 20)
    model.controller.getViewCursor.return_value = vc

    next_tok = goto_adjacent_agent_change(model, forward=True)
    assert next_tok == t_cell


