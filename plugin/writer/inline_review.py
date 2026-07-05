# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Inline review of tracked changes.

Accept or reject the tracked change the cursor sits on as ONE unit. A text replace records two
redlines -- a Delete (struck old text) and an Insert (new text) -- which must be resolved
together (accepting one and rejecting the other yields an incoherent result).

We select the change's EXACT bounds (both marks of its pair) and drive the native
``.uno:AcceptTrackedChange`` / ``.uno:RejectTrackedChange`` on that range. On modern LibreOffice
(>=25.04 / 26.x) this resolves ONLY the targeted change, so a paragraph may hold several agent
changes that are each accepted/rejected individually. On older builds where an exact-bounds
dispatch is a no-op, we fall back to a paragraph-wide selection (which resolves everything in
range, so it refuses when another tracked change shares the paragraph).

Selecting a RANGE rather than a single redline's anchor is deliberate: a pure Delete redline has
an empty anchor, so the per-redline ``select(anchor)`` path in ``tracking.py`` cannot target it;
a range selection covers both marks of the pair. The user's own tracked changes are never touched.

This is the model layer; the right-click context menu in ``change_context_menu.py`` calls it.
"""

from __future__ import annotations

import logging
from typing import Any, NamedTuple

log = logging.getLogger(__name__)


class _RedlineSnapshot(NamedTuple):
    """Result of one pass over the document's redlines.

    Used for fail-closed safety decisions when accepting/rejecting agent changes.
    The two reliability flags are independent so that tests can simulate partial
    failures (e.g. could read all agent tokens but not foreign identifiers).
    """

    agent_tokens: set[str]
    foreign_ids: set[str]
    tokens_reliable: bool
    foreign_reliable: bool

# Shown when a conservative resolve refuses because the change shares a paragraph with the
# user's own tracked change (or another agent change). Surfaced via show_review_message() so a
# click that resolves nothing on purpose isn't silent.
_RESOLVE_REFUSED_HINT = (
    "Could not resolve this change here -- it may share a paragraph with one of"
    " your tracked changes or another agent change. Resolve it from Edit > Track Changes > Manage."
)


def has_agent_changes(model: Any) -> bool:
    """True if the document holds any pending AGENT change (gates the review UI entries)."""
    return bool(_agent_redlines(model))


def resolve_change_at_cursor(model: Any, ctx: Any, accept: bool) -> tuple[bool, str]:
    """Accept (``accept=True``) or reject the AGENT change under the view cursor, as one unit
    (the whole Delete+Insert pair of a replace).

    Only agent changes (wa-review session tokens) are eligible -- the user's own tracked
    changes are left to LibreOffice's native review UI. Returns ``(ok, message)``; ``ok`` is
    False with a friendly message when the cursor is not on an agent change.
    """
    token = cursor_in_agent_change(model)
    if token is None:
        if not _agent_redlines(model):
            return False, "No agent changes to review in this document."
        return False, "Put the cursor on a highlighted agent change first."
    if not resolve_agent_change(model, ctx, token, accept):
        return False, _RESOLVE_REFUSED_HINT
    return True, "Change accepted." if accept else "Change rejected."


def resolve_all_with_feedback(model: Any, ctx: Any, accept: bool) -> tuple[int, str]:
    """Accept/reject all agent changes; return ``(count_resolved, message)``. The message is a
    user-facing note when some or all changes were skipped -- they share a paragraph with one of
    the user's own redlines, which resolve-all deliberately won't touch -- and empty when
    everything resolved (or there was nothing to do)."""
    unreliable_msg = ("Couldn't read this document's tracked changes reliably just now -- nothing was"
                      " changed. Please try again.")
    unconfirmed_msg = ("Started resolving changes but couldn't confirm the result -- some may have been"
                       " resolved. Please review remaining changes in Edit > Track Changes > Manage.")
    # Use the convenience wrapper for a tokens-only query (addresses point 4 in the simplification plan).
    total_tokens = _reliable_agent_tokens(model)
    if total_tokens is None:
        return 0, unreliable_msg
    total = len(total_tokens)
    if total == 0:
        return 0, "No agent changes to review in this document."
    n = resolve_all_agent_changes(model, ctx, accept)
    if n == _RESOLVE_ALL_UNRELIABLE:
        return 0, unreliable_msg       # nothing was dispatched -> honest "nothing changed"
    if n == _RESOLVE_ALL_UNCONFIRMED:
        return 0, unconfirmed_msg      # a dispatch ran -> never claim "nothing changed"
    if n >= total:
        return n, ""
    if n == 0:
        return 0, ("None could be resolved here -- each shares a paragraph with one of your own"
                   " tracked changes. Resolve them from Edit > Track Changes > Manage.")
    return n, ("Resolved %d of %d. The rest share a paragraph with your own tracked changes --"
               " resolve those from Edit > Track Changes > Manage." % (n, total))


def show_review_message(ctx: Any, message: str) -> None:
    """Surface a review outcome (e.g. a conservative refusal) to the user so a click that
    resolves nothing isn't silent. No-op on an empty message; best-effort -- UI feedback must
    never break the resolve itself."""
    if not message:
        return
    try:
        from plugin.chatbot.dialogs import msgbox

        msgbox(ctx, "Review agent changes", message)
    except Exception:
        log.debug("inline_review: could not show review message %r", message, exc_info=True)


# --- agent-change helpers (used by the click popup and the context menu) -------------------
#
# Every redline an EditReviewSession records carries a RedlineComment "wa-review:<session>:<n>"
# (see edit_review.TOKEN_PREFIX); one token = one logical change (a replace's Delete+Insert
# pair shares it). These helpers see the document purely through those tokens, so the user's
# own tracked changes are never listed or resolved.


def _agent_redlines(model: Any) -> list:
    """[(token, redline)] for every redline tagged by an EditReviewSession.

    BEST-EFFORT and DISPLAY-ONLY: on an enumeration error it returns whatever it gathered so far
    (a partial list). That is fine for the listing/navigation/bounds callers, which tolerate a
    short list, but it must NOT back a success/safety decision. The data-safety paths use the
    reliability-aware ``_agent_change_tokens`` (post-resolve verification) and
    ``_all_redlines_are_agent`` (global-dispatch guard), which fail CLOSED on an incomplete scan."""
    from plugin.writer.edit_review import TOKEN_PREFIX, _scan_redlines

    out: list = []

    def on_item(rl: Any) -> bool:
        try:
            comment = str(rl.getPropertyValue("RedlineComment"))
        except Exception:
            return True  # unreadable -> skip (best-effort display scan)
        if comment.startswith(TOKEN_PREFIX):
            out.append((comment, rl))
        return True

    _scan_redlines(model, on_item)  # reliable flag ignored -- display-only helper
    return out


def _agent_and_foreign_redline_snapshot(model: Any) -> _RedlineSnapshot:
    """Single redline enumeration for agent review safety decisions.

    One pass over getRedlines() produces both the agent token view and the foreign
    (user redline) id view, plus independent reliability flags.

    This replaces what used to be multiple separate full-table enumerations.
    """
    from plugin.writer.edit_review import TOKEN_PREFIX, _scan_redlines

    agents: set = set()
    foreign: set = set()
    t_rel = True
    f_rel = True

    def on_item(rl: Any) -> bool:
        nonlocal t_rel, f_rel
        try:
            comment = str(rl.getPropertyValue("RedlineComment"))
            is_agent = comment.startswith(TOKEN_PREFIX)
        except Exception:
            # Unreadable comment on *any* redline makes both views incomplete.
            t_rel = f_rel = False
            return False
        if is_agent:
            agents.add(comment)
            return True
        # Non-agent (user) redline: we need its identifier for the foreign set.
        try:
            foreign.add(rl.getPropertyValue("RedlineIdentifier"))
        except Exception:
            f_rel = False
            return False
        return True

    scan_rel = _scan_redlines(model, on_item)[0]
    return _RedlineSnapshot(
        agents, foreign, (t_rel and scan_rel), (f_rel and scan_rel)
    )


def _reliable_agent_tokens(model: Any) -> set | None:
    """Convenience: the set of agent tokens if the snapshot is reliable, else None.

    Lets call sites read `if (toks := _reliable_agent_tokens(m)) is None or token not in toks:`
    while the full `(set, bool)` form (and its explicit ok flag) is kept for places that log
    the reason or are exercised by existing patches."""
    toks, ok = _agent_change_tokens(model)
    return toks if ok else None


def _agent_change_tokens(model: Any) -> tuple[set, bool]:
    """``(set of agent-change tokens on the document's redlines, reliable)``. Used after a resolve
    dispatch to verify that EXACTLY the target change was resolved.

    ``reliable`` is False when the snapshot is INCOMPLETE -- the enumeration can't start or finish, a
    redline's RedlineComment can't be read (so we can't tell whether it is one of our tokens), or the
    scan yields fewer items than the collection's own ``getCount()`` reports. A
    partial token set makes ``before - after`` unsound (a sibling missing from the BEFORE snapshot
    could be silently resolved and still compute ``removed == {token}``), so the caller must treat an
    unreliable snapshot as failure -- never as proof that only the target was removed. Enumerates
    independently of ``_agent_redlines`` precisely so this guarantee does not ride on a fail-open
    helper.

    On the ``seen != total`` check (shared by the redline-scan helpers below): comparing the items
    the enumeration yields against ``getCount()`` is a DEFENSIVE INVARIANT, not a known bug.
    ``getRedlines()`` is one flat table and this mismatch has NOT been observed in real LibreOffice
    (a native test confirms count equals enumeration length across body and table-cell containers).
    It is kept as cheap fail-closed insurance: if it ever did happen, the cost would be silent loss
    of a user's own redline, so it is worth a count comparison to guard against."""
    snap = _agent_and_foreign_redline_snapshot(model)
    return snap.agent_tokens, snap.tokens_reliable


def _all_redlines_are_agent(model: Any) -> bool:
    """True only if EVERY tracked change in the document is an agent change AND the scan was COMPLETE.

    Gates the global AcceptAll/RejectAll fast path in resolve-all: one global dispatch is safe only
    when there are no user redlines to clobber, and an incomplete scan can't prove that. So any
    enumeration/read failure -- or a single non-agent redline -- returns False, falling back to the
    per-change loop (fail closed). Replaces the old ``len(_agent_redlines) == _redline_count``
    test, which could spuriously match under a correlated mid-enumeration failure (both undercount to
    the same point) and then accept the user's redlines in the unscanned tail.

    Implemented via the combined snapshot: any foreign ID present means not-all-agent.
    The scanned count cross-check and early-abort semantics are provided by _scan_redlines.
    """
    try:
        if int(model.getRedlines().getCount()) <= 0:
            return False
    except Exception:
        return False
    snap = _agent_and_foreign_redline_snapshot(model)
    # Reliable + zero foreigns => every redline we saw carried an agent token.
    return snap.foreign_reliable and len(snap.foreign_ids) == 0


# --- Agent-self-resolution guard (used by the agent-facing track_changes_* tools) -------------
# BUG (reported): the agent accepted its OWN edits. wa-review changes are recorded for the HUMAN
# to accept/reject in the review UI; the agent must never resolve them. HOW IT HAPPENED:
# tracking.py's accept/reject tools drive the native .uno:Accept/Reject(All)TrackedChanges, which
# is blind to the wa-review token, and they are reachable by the LLM (domain='tracking'), so a tool
# call could resolve the very changes the same turn just made. WHY THIS FIXES IT: these two
# token-scoped, fail-closed gates let the tools refuse the agent's own changes while still allowing
# the user's own redlines to be resolved on request.

_BULK_UNRELIABLE_MSG = (
    "Couldn't read this document's tracked changes reliably, so accept/reject-all is blocked to "
    "avoid resolving agent edits that are awaiting your review. Resolve changes in LibreOffice "
    "(Edit > Track Changes > Manage)."
)
_BULK_AGENT_PRESENT_MSG = (
    "This document has %d agent edit(s) awaiting your review (WriterAgent tracked changes). The "
    "agent must not accept or reject its own edits -- you resolve those in the review popup or "
    "Edit > Track Changes > Manage. accept-all / reject-all run only when no agent changes are pending."
)


def redline_is_agent_change(redline: Any) -> tuple[bool, bool]:
    """``(is a wa-review agent change, comment_readable)`` for ONE redline.

    Used by the single-index accept/reject tools. A redline whose RedlineComment can't be read
    returns ``(False, False)`` so the caller fails CLOSED (refuse rather than risk resolving an
    agent change we couldn't classify)."""
    from plugin.writer.edit_review import TOKEN_PREFIX
    try:
        comment = str(redline.getPropertyValue("RedlineComment"))
    except Exception:
        return False, False
    return comment.startswith(TOKEN_PREFIX), True


def agent_self_resolution_block_reason(model: Any) -> str | None:
    """Refusal message if a BULK accept/reject must NOT run, else ``None``.

    A blanket AcceptAll/RejectAll would resolve the agent's own wa-review changes, which only the
    human may do, so refuse whenever the document holds (or might hold) any agent change. Fail
    CLOSED: a document that exposes redlines but can't be scanned reliably is treated as "agent
    changes may be present". A document with NO redline table (or an empty one) is allowed -- the
    native dispatch is a harmless no-op and there is nothing of the user's to clobber. A document
    whose only redlines are the USER's own is allowed (the agent may resolve those on request);
    only the agent's own edits are protected."""
    try:
        if not hasattr(model, "getRedlines"):
            return None
        count = int(model.getRedlines().getCount())
    except Exception:
        return _BULK_UNRELIABLE_MSG
    if count <= 0:
        return None
    snap = _agent_and_foreign_redline_snapshot(model)
    if not snap.tokens_reliable:
        return _BULK_UNRELIABLE_MSG
    if snap.agent_tokens:
        return _BULK_AGENT_PRESENT_MSG % len(snap.agent_tokens)
    return None


def _foreign_redline_ids(model: Any) -> tuple[set, bool]:
    """``(identifiers of non-agent redlines, reliable)``. The identifiers are the user's OWN tracked
    changes (plus any we can't classify, counted as foreign). ``reliable`` is False when the snapshot
    is INCOMPLETE -- the enumeration failed, a foreign redline's identifier couldn't be read, or the
    scan yields fewer items than the collection's own ``getCount()`` reports -- so
    a caller can tell "couldn't verify" apart from "no user redlines" and fail CLOSED instead of
    claiming success without proving the user's changes survived."""
    snap = _agent_and_foreign_redline_snapshot(model)
    return snap.foreign_ids, snap.foreign_reliable


def agent_changes(model: Any) -> list[dict]:
    """Pending agent changes, one entry per logical change (grouped by token), in document
    order of first appearance: ``{"token", "old", "new"}``. The old/new preview texts are what
    a review surface needs to present a change; the tests also use them to pin the grouping of
    a change's Delete+Insert pair under one token."""
    grouped: dict[str, dict] = {}
    for token, rl in _agent_redlines(model):
        entry = grouped.setdefault(token, {"token": token, "old": "", "new": ""})
        try:
            kind = str(rl.getPropertyValue("RedlineType"))
            start = rl.getPropertyValue("RedlineStart")
            end = rl.getPropertyValue("RedlineEnd")
            if start is None or end is None:
                continue
            span = start.getText().createTextCursorByRange(start)
            span.gotoRange(end, True)
            text = span.getString()
            if kind == "Delete":
                entry["old"] += text
            elif kind == "Insert":
                entry["new"] += text
        except Exception:
            continue
    return list(grouped.values())


def _change_bounds(model: Any, token: str):
    """Bounding (start, end) text ranges spanning ALL redlines of one change, or (None, None).

    Builds the union with ``cursor.gotoRange(..., expand=True)`` rather than ``compareRegionStarts``:
    the latter is unreliable on redline ranges (a tracked DELETE's text is not in the normal flow),
    which collapsed the selection for replace changes (Insert+Delete).

    Enumerates COMPLETELY and fails CLOSED (returns (None, None) so the caller refuses): a partial or
    best-effort scan could miss the Insert OR the Delete mark of a replace and hand back HALF the
    change's span -- the dispatch would then resolve only half, and if the caller later refuses by
    conflict the document is left partially resolved. So any enumeration/count error, count/enumeration
    mismatch (enumeration yields fewer items than getCount() reports), unreadable comment, or a target
    mark with unreadable / None bounds -> (None, None)."""
    from plugin.writer.edit_review import _RedlineScanAbort, _scan_redlines

    ranges: list = []

    def on_item(rl: Any) -> bool:
        try:
            comment = str(rl.getPropertyValue("RedlineComment"))
        except Exception:
            raise _RedlineScanAbort()
        if comment != token:
            return True
        try:
            s = rl.getPropertyValue("RedlineStart")
            e = rl.getPropertyValue("RedlineEnd")
        except Exception:
            raise _RedlineScanAbort()
        if s is None or e is None:
            raise _RedlineScanAbort()
        ranges.append((s, e))
        return True

    reliable = _scan_redlines(model, on_item)[0]
    if not reliable or not ranges:
        return None, None
    try:
        text = ranges[0][0].getText()
        cursor = text.createTextCursorByRange(ranges[0][0])
        for s, e in ranges:
            cursor.gotoRange(s, True)  # expand=True grows the selection in either direction
            cursor.gotoRange(e, True)
        return cursor.getStart(), cursor.getEnd()
    except Exception:
        # Returning the first mark only would let the caller resolve/navigate HALF a logical change
        # (a replace's delete OR insert). Fail safe: report no bounds so the caller refuses.
        log.debug("inline_review: _change_bounds union failed; reporting no bounds", exc_info=True)
        return None, None


def goto_agent_change(model: Any, token: str) -> bool:
    """Move the view cursor to (and select) the given change so the user sees it."""
    left, right = _change_bounds(model, token)
    if left is None:
        return False
    try:
        view_cursor = model.getCurrentController().getViewCursor()
        view_cursor.gotoRange(left, False)
        view_cursor.gotoRange(right, True)
        return True
    except Exception:
        log.debug("inline_review: goto_agent_change failed", exc_info=True)
        return False


def pending_agent_change_count(model: Any) -> int:
    """How many agent changes are still pending review -- the toolbar's live counter."""
    return len(agent_changes(model))


def goto_adjacent_agent_change(model: Any, forward: bool = True) -> str | None:
    """Move the view cursor to the NEXT (``forward``) or PREVIOUS pending agent change, in document
    order, cycling at the ends. Returns the token jumped to, or None when there are no agent
    changes. Drives the toolbar's Next/Previous fast-travel.

    The user must be able to START at the very first change. The tricky part: a bare caret sitting
    at a change's start (e.g. where the agent's edit left it) must NOT be mistaken for "already
    reviewing that change", or Next would skip it. So we distinguish two states by the SELECTION:

    * The user is REVIEWING a change  -> the change is SELECTED (only this function selects a whole
      change). Next/Previous then STEP to the adjacent change.
    * The caret is loose (collapsed)  -> Next goes to the first change whose END is after the caret
      (the change the caret is inside, or the next one ahead -- never skipping); Previous goes to
      the last change starting before the caret. A caret at the very top therefore reaches change #0.
    """
    changes = agent_changes(model)  # document order of first appearance
    if not changes:
        return None
    items = []  # (token, left_range, right_range) in document order
    for c in changes:
        left, right = _change_bounds(model, c["token"])
        if left is None or right is None:
            # KNOWN GAP: changes listed by agent_changes() but with unreadable bounds are skipped
            # from the Prev/Next cycle. The toolbar counter (pending_agent_change_count) can still
            # count them, so the user may see N pending changes but only M are reachable via
            # fast-travel. A future fix could log here and/or fall back to paragraph-level goto.
            continue
        try:
            t = left.getText()
            items.append((c["token"], t.createTextCursorByRange(left), t.createTextCursorByRange(right)))
        except Exception:
            continue
    if not items:
        return None
    n = len(items)

    try:
        vc = model.getCurrentController().getViewCursor()
        text = vc.getText()
        cur_start = text.createTextCursorByRange(vc.getStart())
        cur_end = text.createTextCursorByRange(vc.getEnd())
        try:
            cur_collapsed = bool(vc.isCollapsed())
        except Exception:
            cur_collapsed = text.compareRegionStarts(vc.getStart(), vc.getEnd()) == 0
    except Exception:
        text = None
        cur_start = None
        cur_end = None
        cur_collapsed = True

    chosen_idx = None
    if text is None or cur_start is None:
        chosen_idx = 0 if forward else n - 1
    else:
        # Reviewing a change? Only when the selection matches the change's WHOLE span -- both its
        # start AND end. A collapsed caret never counts (so the first change stays reachable), and a
        # manual partial selection that merely starts at a change's start must not count either, or
        # Next/Previous would step away from a change the user didn't actually select.
        sel_idx = None
        if not cur_collapsed:
            for i, (tok, left, right) in enumerate(items):
                try:
                    if (text.compareRegionStarts(cur_start, left) == 0
                            and cur_end is not None
                            and text.compareRegionEnds(cur_end, right) == 0):
                        sel_idx = i
                        break
                except Exception:
                    continue
        if sel_idx is not None:
            chosen_idx = (sel_idx + 1) % n if forward else (sel_idx - 1) % n
        elif forward:
            for i, (tok, left, right) in enumerate(items):
                try:
                    if text.compareRegionStarts(cur_start, right) == 1:  # caret before this change's end
                        chosen_idx = i
                        break
                except Exception:
                    continue
            if chosen_idx is None:
                chosen_idx = 0  # caret past the last change -> cycle to the first
        else:
            for i in range(n - 1, -1, -1):
                try:
                    if text.compareRegionStarts(items[i][1], cur_start) == 1:  # change start before caret
                        chosen_idx = i
                        break
                except Exception:
                    continue
            if chosen_idx is None:
                chosen_idx = n - 1  # caret before the first change -> cycle to the last
    token = items[chosen_idx][0]
    goto_agent_change(model, token)
    return token


def cursor_in_agent_change(model: Any) -> str | None:
    """Token of the agent change the view cursor currently sits in, else None."""
    try:
        cursor = model.getCurrentController().getViewCursor()
        text = cursor.getText()
        # Compare via a model text cursor: view-cursor-owned ranges can fail XTextRangeCompare.
        pos = text.createTextCursorByRange(cursor.getStart())
    except Exception:
        return None
    for token, rl in _agent_redlines(model):
        try:
            s = rl.getPropertyValue("RedlineStart")
            e = rl.getPropertyValue("RedlineEnd")
            if s is None or e is None:
                continue
            # Compare cursor-to-cursor: XTextRangeCompare can refuse mixed range flavors
            # (a plain text cursor vs a redline-owned range), so wrap the redline in a
            # text-cursor span first.
            span = text.createTextCursorByRange(s)
            span.gotoRange(e, True)
            if text.compareRegionStarts(pos, span) == 1:  # pos before the span starts
                continue
            if text.compareRegionEnds(pos, span) == -1:  # pos after the span ends
                continue
            return token
        except Exception:
            continue
    return None


def _span_contains_point(text: Any, span: Any, point: Any) -> bool:
    """True if collapsed range ``point`` lies within ``span``'s [start, end] (same ``text``).

    Raises (does NOT swallow) on a comparison failure, so the overlap guards can fail CLOSED rather
    than mistake an uncomparable range for "no overlap".
    """
    if text.compareRegionStarts(point, span) == 1:   # point starts before span -> left of it
        return False
    if text.compareRegionEnds(point, span) == -1:    # point ends after span -> right of it
        return False
    return True


def _span_has_redline(model: Any, text: Any, span: Any, consider) -> bool:
    """True if any redline that ``consider(comment)`` selects overlaps ``span`` -- OR if that can't
    be ruled out. ``consider`` receives the redline's RedlineComment (or None when it can't be read)
    and returns whether that redline is one the dispatch must NOT touch.

    Data-safety guard: it fails CLOSED. A redline we don't need to protect, or that we can
    prove sits outside ``span``, is skipped; anything we must protect but can't overlap-check (its
    position is unreadable / a comparison raises) is treated as a possible overlap and returns True.
    Crucially, a redline clearly OUTSIDE the span is skipped regardless of whether its comment was
    readable -- so one unreadable redline elsewhere never permanently blocks resolution.

    The scan is also cross-checked against the collection's ``getCount()``: a count/enumeration
    mismatch (the enumeration yielding fewer items than getCount() reports) could hide an overlapping
    user/sibling redline in the unscanned tail, so a short scan returns True (unsafe) instead of a
    false "no overlap". Same ``seen != total`` invariant as ``edit_review._scan_redlines``."""
    from plugin.writer.edit_review import _RedlineScanAbort, _scan_redlines

    # We drive the *central* enumerator so the boilerplate (getCount, cap at total, hasMore/next
    # handling, seen!=total, Abort) is not duplicated. Because the moment we discover a protector
    # we want to stop and declare "unsafe" (without needing the rest of the table), we use a flag
    # + Abort for early exit. The Abort path forces reliable=False from _scan; we distinguish
    # "found a real protector" from "a read error occurred" via the flag.
    unsafe = [False]

    def on_item(rl: Any) -> bool:
        try:
            comment = str(rl.getPropertyValue("RedlineComment"))
        except Exception:
            comment = None  # unclassifiable -> let consider decide
        if not consider(comment):
            return True  # not something we must protect; keep scanning

        try:
            s = rl.getPropertyValue("RedlineStart")
            e = rl.getPropertyValue("RedlineEnd")
        except Exception:
            # Can't read this protected redline's bounds -> can't prove it is outside span.
            log.debug("inline_review: protected redline bounds unreadable; treating as unsafe", exc_info=True)
            unsafe[0] = True
            raise _RedlineScanAbort()
        if s is None or e is None:
            # Protected redline whose bounds are unreadable -> cannot prove it is outside span.
            log.debug("inline_review: protected redline has no start/end; treating as unsafe")
            unsafe[0] = True
            raise _RedlineScanAbort()

        # Map the redline into THIS text. A redline anchored in a DIFFERENT text object (text
        # frame/box, header, footer, footnote) than ``text`` is not addressable here --
        # ``text.createTextCursorByRange(s)`` raises "End of content node doesn't have the proper
        # start node" -- and cannot overlap a span that lives in ``text``, so skip it. Treating
        # this as unsafe would block EVERY change in ``text`` whenever a single tracked change
        # lives outside it (e.g. inside a text box). Only fail closed when we cannot confirm the
        # range is in another text object, so a genuine same-text mapping failure stays protected.
        try:
            rl_span = text.createTextCursorByRange(s)
            rl_span.gotoRange(e, True)
        except Exception:
            try:
                in_other_text = (s.getText() != text)
            except Exception:
                in_other_text = False
            if in_other_text:
                return True  # different text object -> cannot overlap this span -> keep scanning
            log.debug("inline_review: redline overlap check failed; treating as unsafe", exc_info=True)
            unsafe[0] = True
            raise _RedlineScanAbort()

        try:
            overlaps = (_span_contains_point(text, span, rl_span.getStart())
                        or _span_contains_point(text, span, rl_span.getEnd())
                        or _span_contains_point(text, rl_span, span.getStart()))
        except Exception:
            log.debug("inline_review: redline overlap check failed; treating as unsafe", exc_info=True)
            unsafe[0] = True
            raise _RedlineScanAbort()

        if overlaps:
            unsafe[0] = True
            raise _RedlineScanAbort()
        return True

    reliable = _scan_redlines(model, on_item)[0]
    # If we aborted because we found a protector, unsafe[0] is set (treat as unsafe even though
    # reliable came back False). Any other unreliable scan (enum error, truncation) also unsafe.
    if unsafe[0]:
        return True
    if not reliable:
        log.debug("inline_review: cannot enumerate/count redlines or scan truncated; treating span as unsafe")
        return True
    return False


def _foreign_redline_in_span(model: Any, text: Any, span: Any) -> bool:
    """True if any redline NOT tagged by an EditReviewSession (i.e. one of the USER's own) overlaps
    ``span``, or if that can't be ruled out. An accept/reject dispatch over ``span`` resolves every
    redline it covers, so it must never touch the user's own change. Fails closed (see _span_has_redline)."""
    from plugin.writer.edit_review import TOKEN_PREFIX
    # Protect every redline not confirmed to be one of ours (unreadable comment -> protect).
    return _span_has_redline(model, text, span,
                             lambda c: c is None or not c.startswith(TOKEN_PREFIX))


def _other_agent_redline_in_span(model: Any, text: Any, span: Any, token: str) -> bool:
    """True if a DIFFERENT agent change token (not ``token``) overlaps ``span``, or if that can't be
    ruled out. The inline "this change" command is only truthful when the dispatch resolves exactly
    one logical change; a sibling sharing the span would be resolved too. Fails closed."""
    from plugin.writer.edit_review import TOKEN_PREFIX
    # Protect every redline that is (or might be) one of ours but a DIFFERENT token.
    return _span_has_redline(model, text, span,
                             lambda c: c is None or (c.startswith(TOKEN_PREFIX) and c != token))


def _dispatch_resolve(ctx: Any, controller: Any, accept: bool) -> None:
    """Drive the native accept/reject dispatch on the controller's current selection."""
    smgr = ctx.getServiceManager()
    dispatcher = smgr.createInstanceWithContext("com.sun.star.frame.DispatchHelper", ctx)
    command = ".uno:AcceptTrackedChange" if accept else ".uno:RejectTrackedChange"
    dispatcher.executeDispatch(controller.getFrame(), command, "", 0, ())


def resolve_agent_change(model: Any, ctx: Any, token: str, accept: bool,
                         refuse_sibling_agent: bool = True, prefer_exact: bool = True) -> bool:
    """Accept/reject ONE agent change (both marks of its pair) by token.

    Modern LibreOffice (>=25.04 / 26.x) resolves a tracked change when the selection covers
    exactly that redline's marks, so we first select the change's EXACT bounds and dispatch --
    this resolves ONLY the target, letting a paragraph hold several agent changes that are each
    accepted/rejected on their own. We refuse when one of the USER's own redlines -- or
    another agent change -- overlaps that exact span, and verify afterward that EXACTLY the target
    change was resolved.

    Fallback (``prefer_exact=False``, or an older LibreOffice where an exact-bounds dispatch is a
    no-op): widen the selection to whole PARAGRAPHS. That dispatch resolves EVERY redline in the
    range, so we refuse when a foreign -- or, unless ``refuse_sibling_agent=False``, another agent
    -- change shares those paragraphs. ``resolve_all`` passes ``refuse_sibling_agent=False`` and
    ``prefer_exact=False`` (resolving sibling agent changes together is the point there).
    """
    before = _agent_and_foreign_redline_snapshot(model)
    if token not in before.agent_tokens:
        return False
    if not before.tokens_reliable or not before.foreign_reliable:
        # The PRE-dispatch redline snapshot is incomplete (enumeration error or a count/enumeration
        # mismatch): we can neither prove the span is clear of collateral nor verify the outcome. Refuse BEFORE
        # any dispatch -- never mutate first and discover the damage afterwards.
        log.warning("inline_review: refusing resolve of %s -- pre-dispatch redline snapshot unreliable", token)
        return False
    left, right = _change_bounds(model, token)
    if left is None:
        return False
    try:
        text = left.getText()
        controller = model.getCurrentController()
        view_cursor = controller.getViewCursor()

        # 1) Precise path: select EXACTLY this change's marks and dispatch -- resolves only it.
        if prefer_exact:
            exact = text.createTextCursorByRange(left)
            exact.gotoRange(right, True)
            if _foreign_redline_in_span(model, text, exact):
                log.debug("inline_review: refusing inline resolve of %s -- a user redline overlaps it", token)
                return False
            if refuse_sibling_agent and _other_agent_redline_in_span(model, text, exact, token):
                log.debug("inline_review: refusing inline resolve of %s -- another agent change overlaps it", token)
                return False
            view_cursor.gotoRange(left, False)
            view_cursor.gotoRange(right, True)
            _dispatch_resolve(ctx, controller, accept)
            after = _agent_and_foreign_redline_snapshot(model)
            removed = before.agent_tokens - after.agent_tokens
            tokens_ok = before.tokens_reliable and after.tokens_reliable
            foreign_safe = (
                before.foreign_reliable
                and after.foreign_reliable
                and not (before.foreign_ids - after.foreign_ids)
            )
            # "Provably exactly the target": token snapshots complete + only target vanished +
            # user's own redlines provably untouched (foreign_safe). The pre-dispatch span guards
            # already make collateral vanishing rare; we still require the full checks for the
            # documented contract and so that "user redline lost" tests continue to pass.
            if removed == {token} and tokens_ok and foreign_safe:
                return True   # exactly the target -- no sibling change, no user redline touched
            if removed or not tokens_ok or not foreign_safe:
                # POST-DISPATCH FAILURE: dispatch already ran above, so the document may be partially
                # resolved even though we return False. Pre-dispatch guards make this rare; we do not
                # attempt undo() here (LibreOffice undo grouping is not reliably tied to one dispatch).
                # Callers should surface a user message when appropriate; a future improvement could
                # try undo or report "some changes may have been partially resolved".
                log.warning("inline_review: exact resolve of %s affected extra changes %s (token-"
                            "snapshot reliable=%s, user-redlines provably-safe=%s); not claiming "
                            "success", token, removed - {token}, tokens_ok, foreign_safe)
                return False
            # Nothing changed AND the snapshot was reliable: this build no-ops on an exact-bounds
            # selection -- widen to paragraph.
            log.debug("inline_review: exact-bounds resolve of %s was a no-op; widening to paragraph", token)

        # 2) Paragraph-wide path (older LibreOffice, or resolve-all): resolves everything in range,
        #    so refuse when a redline that we must not touch shares those paragraphs.
        start = text.createTextCursorByRange(left)
        start.gotoStartOfParagraph(False)
        end = text.createTextCursorByRange(right)
        end.gotoEndOfParagraph(True)
        para_span = text.createTextCursorByRange(start.getStart())
        para_span.gotoRange(end.getEnd(), True)
        if _foreign_redline_in_span(model, text, para_span):
            log.debug("inline_review: refusing inline resolve of %s -- a user redline shares its paragraph", token)
            return False
        if refuse_sibling_agent and _other_agent_redline_in_span(model, text, para_span, token):
            log.debug("inline_review: refusing inline resolve of %s -- another agent change shares its paragraph", token)
            return False
        view_cursor.gotoRange(start.getStart(), False)
        view_cursor.gotoRange(end.getEnd(), True)
        _dispatch_resolve(ctx, controller, accept)
    except Exception:
        log.exception("inline_review: resolve_agent_change dispatch failed")
        return False
    # The paragraph path may resolve sibling AGENT changes together (resolve_all), but it must still
    # never touch the USER's own redlines. Refuse to claim success if one disappeared OR if we
    # couldn't reliably verify they survived (unreliable snapshot -> fail closed).
    after = _agent_and_foreign_redline_snapshot(model)
    if not after.tokens_reliable:
        log.warning("inline_review: resolve of %s -- could not reliably verify which agent changes "
                    "remain; not claiming success", token)
        return False
    if not (before.foreign_reliable and after.foreign_reliable) or (before.foreign_ids - after.foreign_ids):
        log.warning("inline_review: resolve of %s -- a user redline was lost or could not be "
                    "verified intact; not claiming success", token)
        return False
    return token not in after.agent_tokens


# Sentinels (negative so they never collide with a real resolved-count):
_RESOLVE_ALL_UNRELIABLE = -1   # snapshot unreliable BEFORE any dispatch -> nothing was changed
_RESOLVE_ALL_UNCONFIRMED = -2  # a dispatch already ran but the final state couldn't be confirmed


def resolve_all_agent_changes(model: Any, ctx: Any, accept: bool) -> int:
    """Accept/reject every pending agent change (the user's own redlines stay untouched).

    Returns how many changes were resolved, or a negative sentinel: ``_RESOLVE_ALL_UNRELIABLE`` (-1)
    when the snapshot was unreliable BEFORE any dispatch (genuinely nothing changed -- safe to retry),
    or ``_RESOLVE_ALL_UNCONFIRMED`` (-2) when a dispatch already ran but the result couldn't be
    confirmed (some changes MAY have been resolved -- the caller must not claim "nothing changed").
    Loop control, the completion check, and the count all run off the reliability-aware
    ``_agent_change_tokens`` (NOT the best-effort ``agent_changes``), so a count/enumeration
    mismatch can never leave changes pending while reporting completion.

    (Internally powered by the one-pass _agent_and_foreign_redline_snapshot.)"""
    before = _agent_and_foreign_redline_snapshot(model)
    before_tokens = before.agent_tokens
    if not before.tokens_reliable:
        log.warning("inline_review: resolve-all aborted -- the redline snapshot is unreliable")
        return _RESOLVE_ALL_UNRELIABLE  # nothing dispatched yet
    if not before_tokens:
        return 0

    # Common case -- the document holds ONLY the agent's redlines: a single global
    # AcceptAll/RejectAll dispatch is reliable. (A per-change dispatch loop only resolves
    # the FIRST change per call in the live view: the second .uno:AcceptTrackedChange needs
    # the event loop to cycle, which it cannot inside one synchronous handler.)
    if _all_redlines_are_agent(model):
        try:
            controller = model.getCurrentController()
            smgr = ctx.getServiceManager()
            dispatcher = smgr.createInstanceWithContext("com.sun.star.frame.DispatchHelper", ctx)
            command = ".uno:AcceptAllTrackedChanges" if accept else ".uno:RejectAllTrackedChanges"
            dispatcher.executeDispatch(controller.getFrame(), command, "", 0, ())
            # Confirm completion against a RELIABLE token snapshot. The dispatch ALREADY mutated, so
            # an unreliable after-scan is UNCONFIRMED (changes may have resolved), never "nothing".
            after = _agent_and_foreign_redline_snapshot(model)
            if not after.tokens_reliable:
                return _RESOLVE_ALL_UNCONFIRMED
            return max(0, len(before_tokens) - len(after.agent_tokens))  # a dispatch only removes tokens
        except Exception:
            log.exception("inline_review: resolve-all global dispatch failed")
            return _RESOLVE_ALL_UNCONFIRMED  # the global dispatch may have partially applied

    # User redlines are mixed in: resolve agent changes one per pass.
    # Note: We do not call processEventsToIdle() between dispatches here because it blocks
    # indefinitely if the event loop is never completely idle (e.g. active animations, cursors,
    # or background tasks), causing LibreOffice to hang.
    skip: set[str] = set()  # changes that share a paragraph with a user redline (or clear nothing)
    dispatched = False  # has any resolve attempt run? -> distinguishes "nothing changed" from partial
    # No fixed cap: each pass either resolves the head change (a token leaves the snapshot) or skips
    # it (added to `skip`), so the pending set -- tokens minus skip -- strictly shrinks and the loop
    # always terminates. The snapshot is RE-READ reliably each pass; an unreliable read aborts the
    # whole operation -- UNCONFIRMED once we've dispatched (changes may already be resolved), else
    # UNRELIABLE -- rather than terminate early and silently leave changes pending.
    while True:
        cur = _agent_and_foreign_redline_snapshot(model)
        cur_tokens = cur.agent_tokens
        if not cur.tokens_reliable:
            log.warning("inline_review: resolve-all aborted mid-pass -- the redline snapshot is unreliable")
            return _RESOLVE_ALL_UNCONFIRMED if dispatched else _RESOLVE_ALL_UNRELIABLE
        pending = sorted(t for t in cur_tokens if t not in skip)  # sorted -> deterministic order
        if not pending:
            break
        token = pending[0]
        before_count = len(cur_tokens)
        # resolve-all WANTS sibling agent changes in a paragraph resolved together -- only a USER
        # redline there blocks it (checked inside via _foreign_redline_in_span). Single
        # "resolve this change" keeps refuse_sibling_agent=True so it stays truthful.
        resolved = resolve_agent_change(model, ctx, token, accept, refuse_sibling_agent=False, prefer_exact=False)
        dispatched = True  # resolve_agent_change may mutate even when it ultimately returns False
        after = _agent_and_foreign_redline_snapshot(model)
        if not after.tokens_reliable:
            return _RESOLVE_ALL_UNCONFIRMED
        if not resolved or len(after.agent_tokens) >= before_count:
            # Refused (a user redline shares its paragraph), failed, or cleared nothing: skip it so
            # the rest still resolve and a stuck change can't spin the loop -- the user handles the
            # skipped one via the native review UI.
            log.debug("inline_review: skipping %s in resolve-all (foreign redline or no change)", token)
            skip.add(token)
    final = _agent_and_foreign_redline_snapshot(model)
    if not final.tokens_reliable:
        return _RESOLVE_ALL_UNCONFIRMED if dispatched else _RESOLVE_ALL_UNRELIABLE
    # Count by how many agent tokens actually disappeared, NOT +1 per pass: one paragraph-wide
    # dispatch can clear several sibling agent changes at once (common now that the word-level diff
    # splits an edit into several), so +1 would undercount and make resolve_all_with_feedback report
    # "N of M" wrong.
    return max(0, len(before_tokens) - len(final.agent_tokens))  # tokens only leave; never report negative
