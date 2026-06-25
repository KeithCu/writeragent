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
from typing import Any

log = logging.getLogger(__name__)

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
    total_tokens, ok = _agent_change_tokens(model)  # reliable count, not best-effort agent_changes
    if not ok:
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
    from plugin.writer.edit_review import TOKEN_PREFIX

    out = []
    try:
        enum = model.getRedlines().createEnumeration()
        while enum.hasMoreElements():
            rl = enum.nextElement()
            try:
                comment = str(rl.getPropertyValue("RedlineComment"))
            except Exception:
                continue
            if comment.startswith(TOKEN_PREFIX):
                out.append((comment, rl))
    except Exception:
        log.debug("inline_review: agent redline scan failed", exc_info=True)
    return out


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
    from plugin.writer.edit_review import TOKEN_PREFIX

    out: set = set()
    try:
        redlines = model.getRedlines()
        total = int(redlines.getCount())
        enum = redlines.createEnumeration()
    except Exception:
        return out, False  # can't enumerate / count -> unreliable snapshot
    reliable = True
    seen = 0
    while True:
        try:
            if not enum.hasMoreElements():
                break
            rl = enum.nextElement()
        except Exception:
            return out, False  # can't advance the enumeration -> unreliable
        seen += 1
        try:
            comment = str(rl.getPropertyValue("RedlineComment"))
        except Exception:
            reliable = False  # can't classify this redline -> token snapshot incomplete
            continue
        if comment.startswith(TOKEN_PREFIX):
            out.add(comment)
    if seen != total:
        reliable = False  # enumeration shorter than getCount() -> snapshot incomplete
    return out, reliable


def _all_redlines_are_agent(model: Any) -> bool:
    """True only if EVERY tracked change in the document is an agent change AND the scan was COMPLETE.

    Gates the global AcceptAll/RejectAll fast path in resolve-all: one global dispatch is safe only
    when there are no user redlines to clobber, and an incomplete scan can't prove that. So any
    enumeration/read failure -- or a single non-agent redline -- returns False, falling back to the
    per-change loop (fail closed). Replaces the old ``len(_agent_redlines) == _redline_count``
    test, which could spuriously match under a correlated mid-enumeration failure (both undercount to
    the same point) and then accept the user's redlines in the unscanned tail.

    The scanned count is cross-checked against the redline collection's own ``getCount()`` so an
    early stop -- ``hasMoreElements()`` going False with redlines still in the tail, i.e. the
    enumeration yields fewer items than getCount() reports -- also fails closed instead of
    green-lighting a whole-document accept."""
    from plugin.writer.edit_review import TOKEN_PREFIX

    try:
        redlines = model.getRedlines()
        total = int(redlines.getCount())
    except Exception:
        return False  # can't establish the true count -> can't prove completeness -> fail closed
    if total <= 0:
        return False  # nothing to resolve via the global path
    try:
        enum = redlines.createEnumeration()
    except Exception:
        return False
    seen = 0
    while True:
        try:
            if not enum.hasMoreElements():
                break
            rl = enum.nextElement()
            comment = str(rl.getPropertyValue("RedlineComment"))
        except Exception:
            return False  # couldn't advance/read a redline -> can't prove all are agent -> fail closed
        if not comment.startswith(TOKEN_PREFIX):
            return False  # a user (non-agent) redline is present
        seen += 1
    # An enumeration shorter than getCount() leaves seen < total; only a COMPLETE all-agent scan passes.
    return seen == total


def _foreign_redline_ids(model: Any) -> tuple[set, bool]:
    """``(identifiers of non-agent redlines, reliable)``. The identifiers are the user's OWN tracked
    changes (plus any we can't classify, counted as foreign). ``reliable`` is False when the snapshot
    is INCOMPLETE -- the enumeration failed, a foreign redline's identifier couldn't be read, or the
    scan yields fewer items than the collection's own ``getCount()`` reports -- so
    a caller can tell "couldn't verify" apart from "no user redlines" and fail CLOSED instead of
    claiming success without proving the user's changes survived."""
    from plugin.writer.edit_review import TOKEN_PREFIX

    out: set = set()
    try:
        redlines = model.getRedlines()
        total = int(redlines.getCount())
        enum = redlines.createEnumeration()
    except Exception:
        return out, False  # can't enumerate / count -> unreliable snapshot
    reliable = True
    seen = 0
    while True:
        try:
            if not enum.hasMoreElements():
                break
            rl = enum.nextElement()
        except Exception:
            return out, False  # can't advance the enumeration -> unreliable
        seen += 1
        try:
            ours = str(rl.getPropertyValue("RedlineComment")).startswith(TOKEN_PREFIX)
        except Exception:
            ours = False  # unclassifiable -> count as foreign so its loss is still detected
        if ours:
            continue
        try:
            out.add(rl.getPropertyValue("RedlineIdentifier"))
        except Exception:
            reliable = False  # a foreign redline we can't track -> snapshot incomplete
    if seen != total:
        reliable = False  # enumeration shorter than getCount() -> snapshot incomplete
    return out, reliable


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
    try:
        redlines = model.getRedlines()
        total = int(redlines.getCount())
        enum = redlines.createEnumeration()
    except Exception:
        return None, None
    ranges = []
    seen = 0
    while True:
        try:
            if not enum.hasMoreElements():
                break
            rl = enum.nextElement()
            comment = str(rl.getPropertyValue("RedlineComment"))
        except Exception:
            return None, None  # can't advance/read a redline -> might miss a target mark -> fail closed
        seen += 1
        if comment != token:
            continue
        try:
            s = rl.getPropertyValue("RedlineStart")
            e = rl.getPropertyValue("RedlineEnd")
        except Exception:
            return None, None  # a TARGET mark we can't read -> incomplete span -> fail closed
        if s is None or e is None:
            return None, None  # a TARGET mark with no bounds -> incomplete span -> fail closed
        ranges.append((s, e))
    if seen != total:
        return None, None  # enumeration shorter than getCount() -> a target mark may be in the tail
    if not ranges:
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
    false "no overlap".
    """
    try:
        redlines = model.getRedlines()
        total = int(redlines.getCount())
        enum = redlines.createEnumeration()
    except Exception:
        log.debug("inline_review: cannot enumerate/count redlines; treating span as unsafe", exc_info=True)
        return True
    seen = 0
    while True:
        try:
            if not enum.hasMoreElements():
                break
            rl = enum.nextElement()
        except Exception:
            log.debug("inline_review: cannot advance redline enumeration; treating span as unsafe", exc_info=True)
            return True
        seen += 1
        try:
            comment = str(rl.getPropertyValue("RedlineComment"))
        except Exception:
            comment = None  # unclassifiable -> let the overlap test decide, don't blindly skip
        if not consider(comment):
            continue  # not a redline we must protect -> safe to skip
        try:
            s = rl.getPropertyValue("RedlineStart")
            e = rl.getPropertyValue("RedlineEnd")
            if s is None or e is None:
                # A redline we must protect whose extent we can't read -> we cannot prove it's
                # outside the span, so fail CLOSED, not skip-as-safe.
                log.debug("inline_review: protected redline has no start/end; treating as unsafe")
                return True
            rl_span = text.createTextCursorByRange(s)
            rl_span.gotoRange(e, True)
            # Overlap iff the redline's start/end falls inside span, or span starts inside it.
            overlaps = (_span_contains_point(text, span, rl_span.getStart())
                        or _span_contains_point(text, span, rl_span.getEnd())
                        or _span_contains_point(text, rl_span, span.getStart()))
        except Exception:
            log.debug("inline_review: redline overlap check failed; treating as unsafe", exc_info=True)
            return True  # can't tell whether it overlaps -> fail closed
        if overlaps:
            return True
    if seen != total:
        # Enumeration shorter than getCount(): a protected redline may overlap in the unscanned tail.
        log.debug("inline_review: redline enumeration truncated (%d of %d); treating span as unsafe",
                  seen, total)
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
    before_tokens, before_tokens_ok = _agent_change_tokens(model)
    if token not in before_tokens:
        return False
    before_foreign, before_ok = _foreign_redline_ids(model)  # the user's own redlines -- never lose
    if not before_tokens_ok or not before_ok:
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
            after_tokens, after_tokens_ok = _agent_change_tokens(model)
            removed = before_tokens - after_tokens
            after_foreign, after_ok = _foreign_redline_ids(model)
            # "Provably exactly the target": both token snapshots were complete AND only the target's
            # token vanished AND the user's own redlines were readable before+after with none lost.
            # Anything we COULDN'T verify (an incomplete snapshot) is failure, never proof.
            tokens_ok = before_tokens_ok and after_tokens_ok
            foreign_safe = before_ok and after_ok and not (before_foreign - after_foreign)
            if removed == {token} and tokens_ok and foreign_safe:
                return True   # exactly the target -- no sibling change, no user redline touched
            if removed or not tokens_ok or not foreign_safe:
                # Touched more than the target (a sibling and/or a user redline), or we couldn't
                # reliably verify which changes remain / that the user's redlines survived. The guards
                # above should make this unreachable; if it happens, never report success.
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
    after_foreign, after_ok = _foreign_redline_ids(model)
    after_tokens, after_tokens_ok = _agent_change_tokens(model)
    if not after_tokens_ok:
        log.warning("inline_review: resolve of %s -- could not reliably verify which agent changes "
                    "remain; not claiming success", token)
        return False
    if not (before_ok and after_ok) or (before_foreign - after_foreign):
        log.warning("inline_review: resolve of %s -- a user redline was lost or could not be "
                    "verified intact; not claiming success", token)
        return False
    return token not in after_tokens


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
    mismatch can never leave changes pending while reporting completion."""
    before_tokens, ok = _agent_change_tokens(model)
    if not ok:
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
            after_tokens, after_ok = _agent_change_tokens(model)
            if not after_ok:
                return _RESOLVE_ALL_UNCONFIRMED
            return max(0, len(before_tokens) - len(after_tokens))  # a dispatch only removes tokens
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
        cur_tokens, ok = _agent_change_tokens(model)
        if not ok:
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
        after_tokens, after_ok = _agent_change_tokens(model)
        if not after_ok:
            return _RESOLVE_ALL_UNCONFIRMED
        if not resolved or len(after_tokens) >= before_count:
            # Refused (a user redline shares its paragraph), failed, or cleared nothing: skip it so
            # the rest still resolve and a stuck change can't spin the loop -- the user handles the
            # skipped one via the native review UI.
            log.debug("inline_review: skipping %s in resolve-all (foreign redline or no change)", token)
            skip.add(token)
    final_tokens, final_ok = _agent_change_tokens(model)
    if not final_ok:
        return _RESOLVE_ALL_UNCONFIRMED if dispatched else _RESOLVE_ALL_UNRELIABLE
    # Count by how many agent tokens actually disappeared, NOT +1 per pass: one paragraph-wide
    # dispatch can clear several sibling agent changes at once (common now that the word-level diff
    # splits an edit into several), so +1 would undercount and make resolve_all_with_feedback report
    # "N of M" wrong.
    return max(0, len(before_tokens) - len(final_tokens))  # tokens only leave; never report negative
