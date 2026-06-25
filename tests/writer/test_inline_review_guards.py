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
from unittest.mock import MagicMock, patch

import pytest

from plugin.writer.inline_review import (
    _agent_change_tokens,
    _all_redlines_are_agent,
    _change_bounds,
    _foreign_redline_ids,
    _foreign_redline_in_span,
    _other_agent_redline_in_span,
    _span_contains_point,
    resolve_agent_change,
)
from plugin.writer.edit_review import TOKEN_PREFIX


def _sign(n):
    return (n > 0) - (n < 0)


class FakeRange:
    """A [s, e] range over a shared text, identified by integer positions."""

    def __init__(self, s, e):
        self.s = s
        self.e = e

    def getStart(self):
        return FakeRange(self.s, self.s)

    def getEnd(self):
        return FakeRange(self.e, self.e)

    def getText(self):
        return FakeText()

    def gotoRange(self, other, expand):
        if expand:
            self.s = min(self.s, other.s)
            self.e = max(self.e, other.e)
        else:
            self.s = self.e = other.s


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
    assert _foreign_redline_in_span(model, FakeText(), span) is True


def test_foreign_redline_no_overlap():
    span = FakeRange(10, 20)
    model = FakeModel(FakeRedlines([FakeRedline("", 30, 40)]))  # user redline far away
    assert _foreign_redline_in_span(model, FakeText(), span) is False


def test_foreign_redline_ours_is_not_foreign():
    span = FakeRange(10, 20)
    # Our own agent redline overlapping must be skipped BEFORE any comparison (not "foreign").
    model = FakeModel(FakeRedlines([FakeRedline(TOKEN_PREFIX + "s:1", 15, 25)]))
    assert _foreign_redline_in_span(model, FakeText(), span) is False


def test_foreign_redline_fails_closed_on_enum_error():
    model = FakeModel(FakeRedlines([], raise_enum=True))
    assert _foreign_redline_in_span(model, FakeText(), FakeRange(10, 20)) is True


def test_foreign_redline_fails_closed_on_per_redline_error():
    span = FakeRange(10, 20)
    # A user redline whose RedlineStart read blows up -> can't tell if it overlaps -> refuse.
    model = FakeModel(FakeRedlines([FakeRedline("", 15, 25, raise_on={"RedlineStart"})]))
    assert _foreign_redline_in_span(model, FakeText(), span) is True


def test_foreign_redline_fails_closed_on_compare_error():
    span = FakeRange(10, 20)
    model = FakeModel(FakeRedlines([FakeRedline("", 15, 25)]))
    assert _foreign_redline_in_span(model, FakeText(raise_compare=True), span) is True


def test_foreign_redline_fails_closed_on_silent_truncation():
    # A user redline far from the span (no overlap), but getCount() reports another redline that the
    # enumeration did NOT yield -- it could overlap in the unscanned tail -> fail closed.
    span = FakeRange(10, 20)
    model = FakeModel(FakeRedlines([FakeRedline("", 30, 40)], count=2))  # 1 enumerated, count says 2
    assert _foreign_redline_in_span(model, FakeText(), span) is True


def test_foreign_redline_with_no_bounds_fails_closed():
    # A user redline we must protect but whose RedlineStart/End is None -> we can't prove it's
    # outside the span -> fail closed, NOT skip-as-safe (None-bounds hole).
    span = FakeRange(10, 20)
    rl = FakeRedline("", 15, 25)
    rl._props["RedlineStart"] = None
    model = FakeModel(FakeRedlines([rl]))
    assert _foreign_redline_in_span(model, FakeText(), span) is True


# --------------------------------------------------------------------- other-agent (sibling) guard

def test_other_agent_overlap_detected():
    span = FakeRange(10, 20)
    target = TOKEN_PREFIX + "s:1"
    sibling = TOKEN_PREFIX + "s:2"
    model = FakeModel(FakeRedlines([FakeRedline(sibling, 15, 25)]))
    assert _other_agent_redline_in_span(model, FakeText(), span, target) is True


def test_other_agent_same_token_not_flagged():
    span = FakeRange(10, 20)
    target = TOKEN_PREFIX + "s:1"
    model = FakeModel(FakeRedlines([FakeRedline(target, 15, 25)]))  # the target itself, not a sibling
    assert _other_agent_redline_in_span(model, FakeText(), span, target) is False


def test_other_agent_fails_closed_on_error():
    span = FakeRange(10, 20)
    target = TOKEN_PREFIX + "s:1"
    model = FakeModel(FakeRedlines([], raise_enum=True))
    assert _other_agent_redline_in_span(model, FakeText(), span, target) is True


# --------------------------------------------------------- resolve targets exactly the change

def _resolve(token, token_sets, refuse_sibling=True, prefer_exact=True, foreign_sets=None):
    """Run resolve_agent_change with the geometry helpers stubbed, driving the snapshots
    (now via the combined _agent_and_foreign... plus the named wrappers for compatibility).

    token_sets still supplies the agent token view; foreign_sets supplies the foreign view.
    We patch both the legacy names (for any wrapper calls) and the combined (4-tuple) that the
    hot paths now call directly. A bare set is treated as (set, True)."""
    import contextlib
    model = MagicMock()
    token_side = [t if isinstance(t, tuple) else (set(t), True) for t in token_sets]
    def _mk_foreign(fs):
        if fs is None:
            return (set(), True)
        return fs if isinstance(fs, tuple) else (set(fs), True)
    if foreign_sets is not None:
        foreign_side = [_mk_foreign(fs) for fs in foreign_sets]
    else:
        foreign_side = [(set(), True)]

    # Use counters so successive calls inside one resolve (before + after) get successive pairs
    # from the supplied lists. Tests drive token and foreign views in parallel.
    state = {"ti": 0, "fi": 0}
    def _combined(model):
        ti = min(state["ti"], len(token_side) - 1)
        fi = min(state["fi"], len(foreign_side) - 1)
        toks, tok_ok = token_side[ti]
        state["ti"] += 1
        frn, frn_ok = foreign_side[fi]
        state["fi"] += 1
        return (set(toks), set(frn), bool(tok_ok), bool(frn_ok))

    foreign_patch = (patch("plugin.writer.inline_review._foreign_redline_ids", side_effect=foreign_side)
                     if foreign_sets is not None
                     else patch("plugin.writer.inline_review._foreign_redline_ids", return_value=(set(), True)))
    with contextlib.ExitStack() as stack:
        stack.enter_context(patch("plugin.writer.inline_review._agent_change_tokens", side_effect=token_side))
        stack.enter_context(patch("plugin.writer.inline_review._agent_and_foreign_redline_snapshot", side_effect=_combined))
        stack.enter_context(patch("plugin.writer.inline_review._change_bounds", return_value=(MagicMock(), MagicMock())))
        stack.enter_context(patch("plugin.writer.inline_review._foreign_redline_in_span", return_value=False))
        stack.enter_context(patch("plugin.writer.inline_review._other_agent_redline_in_span", return_value=False))
        stack.enter_context(patch("plugin.writer.inline_review._dispatch_resolve"))
        stack.enter_context(foreign_patch)
        return resolve_agent_change(model, MagicMock(), token, True,
                                    refuse_sibling_agent=refuse_sibling, prefer_exact=prefer_exact)


def test_resolve_exact_success_only_target_gone():
    assert _resolve("T", [{"T", "S"}, {"S"}]) is True


def test_resolve_target_plus_collateral_reports_failure():
    # Target AND a sibling vanished -> NOT "exactly one" -> must fail strictly: the command
    # promises to resolve only the target, so resolving extra changes is a violation, not a success.
    assert _resolve("T", [{"T", "S"}, set()]) is False


def test_resolve_only_sibling_resolved_reports_failure():
    # The target did NOT resolve but a sibling did -> honest failure, do not claim success.
    assert _resolve("T", [{"T", "S"}, {"T"}]) is False


def test_resolve_refuses_when_user_redline_disappears():
    # The target resolved cleanly by token, BUT one of the user's own redlines vanished -> the
    # dispatch touched the user's data -> must NOT claim success (user-redline protection).
    assert _resolve("T", [{"T", "S"}, {"S"}], foreign_sets=[({"user-rl"}, True), (set(), True)]) is False


def test_resolve_refuses_when_foreign_snapshot_unreliable():
    # The target resolved by token, but the user-redline snapshot was UNRELIABLE (couldn't enumerate
    # / read an identifier) -> "couldn't verify" must not pass as "nothing lost" -> fail closed.
    assert _resolve("T", [{"T", "S"}, {"S"}], foreign_sets=[(set(), False), (set(), True)]) is False


def test_resolve_refuses_when_before_token_snapshot_unreliable():
    # The BEFORE agent-token snapshot was incomplete (enumeration failed mid-scan). A sibling could
    # be missing from it, so "removed == {target}" can't prove only the target went -> fail closed.
    assert _resolve("T", [({"T", "S"}, False), ({"S"}, True)]) is False


def test_resolve_refuses_when_after_token_snapshot_unreliable():
    # The AFTER agent-token snapshot was incomplete -> we can't trust which changes remain, so even
    # an apparent "exactly the target removed" is not provable -> fail closed.
    assert _resolve("T", [({"T", "S"}, True), ({"S"}, False)]) is False


def test_resolve_paragraph_path_refuses_when_after_token_snapshot_unreliable():
    # No-op exact dispatch widens to the paragraph path; there, an unreliable after-token snapshot
    # means we can't prove the target is gone -> must not claim success.
    # calls: before(reliable), exact-after(reliable, no-op), paragraph-after(UNRELIABLE).
    assert _resolve("T", [({"T", "S"}, True), ({"T", "S"}, True), ({"S"}, False)]) is False


def test_resolve_noop_falls_through_to_paragraph_then_succeeds():
    # Exact dispatch changed nothing (old build) -> paragraph path -> target gone, no user redline
    # lost -> success.
    assert _resolve("T", [{"T", "S"}, {"T", "S"}, {"S"}]) is True


def test_resolve_absent_token_returns_false():
    assert _resolve("T", [{"S"}]) is False


def test_resolve_paragraph_path_refuses_when_user_redline_lost():
    # The exact dispatch no-ops (widen to paragraph); the paragraph dispatch then makes a user redline
    # disappear (present before, gone after). The paragraph-path foreign-loss guard must refuse,
    # mirroring the exact-path twin. calls: before, exact-after(no-op), paragraph-after.
    assert _resolve(
        "T",
        [({"T", "S"}, True), ({"T", "S"}, True), ({"S"}, True)],
        foreign_sets=[({"u"}, True), ({"u"}, True), (set(), True)],
    ) is False


# ----------------------------------------------------------------- _all_redlines_are_agent

def _agent_rl():
    return FakeRedline(TOKEN_PREFIX + "s:1", 0, 0)


def _user_rl():
    return FakeRedline("", 0, 0)  # no agent token comment


def test_all_redlines_are_agent_true_when_complete_and_all_agent():
    model = FakeModel(FakeRedlines([_agent_rl(), _agent_rl()]))
    assert _all_redlines_are_agent(model) is True


def test_all_redlines_are_agent_false_with_a_user_redline():
    model = FakeModel(FakeRedlines([_agent_rl(), _user_rl()]))
    assert _all_redlines_are_agent(model) is False


def test_all_redlines_are_agent_false_on_silent_truncation():
    # The enumeration yields only the two leading agent redlines but getCount() reports 3 (a user
    # redline sits in the unscanned tail). Count cross-check must fail closed.
    model = FakeModel(FakeRedlines([_agent_rl(), _agent_rl()], count=3))
    assert _all_redlines_are_agent(model) is False


def test_all_redlines_are_agent_false_on_enum_error():
    model = FakeModel(FakeRedlines([_agent_rl()], raise_enum=True))
    assert _all_redlines_are_agent(model) is False


def test_all_redlines_are_agent_false_on_count_error():
    model = FakeModel(FakeRedlines([_agent_rl()], raise_count=True))
    assert _all_redlines_are_agent(model) is False


def test_all_redlines_are_agent_false_on_empty_document():
    model = FakeModel(FakeRedlines([]))
    assert _all_redlines_are_agent(model) is False


def test_all_redlines_are_agent_false_on_unreadable_comment():
    model = FakeModel(FakeRedlines([_agent_rl(), FakeRedline("", 0, 0, raise_on={"RedlineComment"})]))
    assert _all_redlines_are_agent(model) is False


# -------------------------------------------- token/foreign snapshot completeness (getCount)

def test_agent_change_tokens_reliable_when_complete():
    model = FakeModel(FakeRedlines([_agent_rl(), _user_rl()]))
    tokens, reliable = _agent_change_tokens(model)
    assert reliable is True and tokens == {TOKEN_PREFIX + "s:1"}


def test_agent_change_tokens_unreliable_on_silent_truncation():
    # The enumeration yields 1 redline but getCount() reports 2 (a redline silently dropped) -> the
    # token snapshot can't be trusted for a "removed == {token}" comparison -> reliable False.
    model = FakeModel(FakeRedlines([_agent_rl()], count=2))
    tokens, reliable = _agent_change_tokens(model)
    assert reliable is False


def test_agent_change_tokens_unreliable_on_count_error():
    model = FakeModel(FakeRedlines([_agent_rl()], raise_count=True))
    unused_tokens, reliable = _agent_change_tokens(model)
    assert reliable is False


def test_foreign_redline_ids_reliable_when_complete():
    model = FakeModel(FakeRedlines([_agent_rl(), _user_rl()]))
    ids, reliable = _foreign_redline_ids(model)
    assert reliable is True  # the user redline (no token) is the only foreign id, scan complete


def test_foreign_redline_ids_unreliable_on_silent_truncation():
    # getCount() says 3 but only 2 redlines enumerated -> a user redline may sit in the unscanned
    # tail; we must not later read "no user redline lost" off this incomplete set -> reliable False.
    model = FakeModel(FakeRedlines([_agent_rl(), _user_rl()], count=3))
    unused_ids, reliable = _foreign_redline_ids(model)
    assert reliable is False


def test_foreign_redline_ids_unreliable_on_count_error():
    model = FakeModel(FakeRedlines([_user_rl()], raise_count=True))
    unused_ids, reliable = _foreign_redline_ids(model)
    assert reliable is False


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


def test_change_bounds_fail_closed_on_none_target_bounds():
    target = TOKEN_PREFIX + "s:1"
    rl = FakeRedline(target, 15, 20)
    rl._props["RedlineEnd"] = None
    model = FakeModel(FakeRedlines([FakeRedline(target, 10, 15), rl]))
    assert _change_bounds(model, target) == (None, None)


# ------------------------------------------ refuse BEFORE dispatch on an unreliable before-snapshot

def test_resolve_does_not_dispatch_when_before_token_snapshot_unreliable():
    # The before token snapshot is unreliable -> we must refuse BEFORE _dispatch_resolve, never
    # mutate first and only then return False (the damage would already be done).
    model = MagicMock()
    with patch("plugin.writer.inline_review._agent_change_tokens", return_value=({"T"}, False)), \
         patch("plugin.writer.inline_review._foreign_redline_ids", return_value=(set(), True)), \
         patch("plugin.writer.inline_review._change_bounds", return_value=(MagicMock(), MagicMock())), \
         patch("plugin.writer.inline_review._foreign_redline_in_span", return_value=False), \
         patch("plugin.writer.inline_review._other_agent_redline_in_span", return_value=False), \
         patch("plugin.writer.inline_review._dispatch_resolve") as dispatch:
        result = resolve_agent_change(model, MagicMock(), "T", True)
    assert result is False
    dispatch.assert_not_called()


def test_resolve_does_not_dispatch_when_before_foreign_snapshot_unreliable():
    model = MagicMock()
    with patch("plugin.writer.inline_review._agent_change_tokens", return_value=({"T"}, True)), \
         patch("plugin.writer.inline_review._foreign_redline_ids", return_value=(set(), False)), \
         patch("plugin.writer.inline_review._change_bounds", return_value=(MagicMock(), MagicMock())), \
         patch("plugin.writer.inline_review._foreign_redline_in_span", return_value=False), \
         patch("plugin.writer.inline_review._other_agent_redline_in_span", return_value=False), \
         patch("plugin.writer.inline_review._dispatch_resolve") as dispatch:
        result = resolve_agent_change(model, MagicMock(), "T", True)
    assert result is False
    dispatch.assert_not_called()


def test_agent_redlines_does_not_hang_on_mock_enumeration():
    # Bootstrap paths (get_tools -> install_review_toolbar) hit pending_agent_change_count on open
    # documents. MagicMock.hasMoreElements() stays truthy forever unless iteration is capped by
    # getCount() -- an uncapped while-hasMoreElements loop hung pytest (see test_calc_analyze_data).
    from plugin.writer.inline_review import agent_changes, pending_agent_change_count

    doc = MagicMock()
    doc.getRedlines.return_value.getCount.return_value = 0
    enum = MagicMock()
    enum.hasMoreElements.return_value = True
    doc.getRedlines.return_value.createEnumeration.return_value = enum
    assert agent_changes(doc) == []
    assert pending_agent_change_count(doc) == 0
