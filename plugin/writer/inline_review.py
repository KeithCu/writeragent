# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Inline review of tracked changes.

Accept or reject the tracked change the cursor sits on as ONE unit. A text replace records two
redlines -- a Delete (struck old text) and an Insert (new text) -- which LibreOffice's native UI
lets you resolve independently, so accepting one and rejecting the other yields an incoherent
result. Here we resolve every tracked change in the cursor's paragraph together (a replace's
delete+insert live in the same paragraph), driving the native ``.uno:AcceptTrackedChange`` /
``.uno:RejectTrackedChange`` on a paragraph-wide selection.

Selecting a RANGE and dispatching (rather than selecting a single redline's anchor) is deliberate:
a pure Delete redline has an empty anchor, so the per-redline ``select(anchor)`` path in
``tracking.py`` cannot target it; a range selection covers both marks of the pair.

This is the model layer; the right-click context menu in ``change_context_menu.py`` calls it.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)


def _redline_count(model: Any) -> int:
    """Number of tracked changes (redlines) currently in the document."""
    n = 0
    try:
        enum = model.getRedlines().createEnumeration()
        while enum.hasMoreElements():
            enum.nextElement()
            n += 1
    except Exception:
        log.debug("inline_review: could not enumerate redlines", exc_info=True)
    return n


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
        return False, "Could not resolve the agent change at the cursor."
    return True, "Change accepted." if accept else "Change rejected."


# --- agent-change helpers (used by the click popup and the context menu) -------------------
#
# Every redline an EditReviewSession records carries a RedlineComment "wa-review:<session>:<n>"
# (see edit_review.TOKEN_PREFIX); one token = one logical change (a replace's Delete+Insert
# pair shares it). These helpers see the document purely through those tokens, so the user's
# own tracked changes are never listed or resolved.


def _agent_redlines(model: Any) -> list:
    """[(token, redline)] for every redline tagged by an EditReviewSession."""
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
    """Bounding (start, end) text ranges spanning all redlines of one change, or (None, None).

    Builds the union with ``cursor.gotoRange(..., expand=True)`` rather than
    ``compareRegionStarts``: the latter is unreliable on redline ranges (a tracked DELETE's text
    is not in the normal flow), which collapsed the selection for replace changes (Insert+Delete).
    """
    ranges = []
    for comment, rl in _agent_redlines(model):
        if comment != token:
            continue
        try:
            s = rl.getPropertyValue("RedlineStart")
            e = rl.getPropertyValue("RedlineEnd")
        except Exception:
            continue
        if s is not None and e is not None:
            ranges.append((s, e))
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
        log.debug("inline_review: _change_bounds union failed", exc_info=True)
        return ranges[0]


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


def resolve_agent_change(model: Any, ctx: Any, token: str, accept: bool) -> bool:
    """Accept/reject ONE agent change (both marks of its pair) by token.

    The dispatch selection is expanded to whole PARAGRAPHS around the change: empirically,
    .uno:Accept/RejectTrackedChange does not resolve redlines when the selection matches the
    redline bounds exactly, but does on a paragraph-wide selection. Limitation: another
    tracked change sharing the same paragraph resolves along with it.
    """
    before = len(_agent_redlines(model))
    left, right = _change_bounds(model, token)
    if left is None:
        return False
    try:
        text = left.getText()
        start = text.createTextCursorByRange(left)
        start.gotoStartOfParagraph(False)
        end = text.createTextCursorByRange(right)
        end.gotoEndOfParagraph(True)
        view_cursor = model.getCurrentController().getViewCursor()
        view_cursor.gotoRange(start.getStart(), False)
        view_cursor.gotoRange(end.getEnd(), True)
        controller = model.getCurrentController()
        smgr = ctx.getServiceManager()
        dispatcher = smgr.createInstanceWithContext("com.sun.star.frame.DispatchHelper", ctx)
        command = ".uno:AcceptTrackedChange" if accept else ".uno:RejectTrackedChange"
        dispatcher.executeDispatch(controller.getFrame(), command, "", 0, ())
    except Exception:
        log.exception("inline_review: resolve_agent_change dispatch failed")
        return False
    return len(_agent_redlines(model)) < before


def resolve_all_agent_changes(model: Any, ctx: Any, accept: bool) -> int:
    """Accept/reject every pending agent change (the user's own redlines stay untouched).
    Returns how many changes were resolved."""
    changes = agent_changes(model)
    if not changes:
        return 0

    # Common case -- the document holds ONLY the agent's redlines: a single global
    # AcceptAll/RejectAll dispatch is reliable. (A per-change dispatch loop only resolves
    # the FIRST change per call in the live view: the second .uno:AcceptTrackedChange needs
    # the event loop to cycle, which it cannot inside one synchronous handler.)
    if len(_agent_redlines(model)) == _redline_count(model):
        try:
            controller = model.getCurrentController()
            smgr = ctx.getServiceManager()
            dispatcher = smgr.createInstanceWithContext("com.sun.star.frame.DispatchHelper", ctx)
            command = ".uno:AcceptAllTrackedChanges" if accept else ".uno:RejectAllTrackedChanges"
            dispatcher.executeDispatch(controller.getFrame(), command, "", 0, ())
            return len(changes) if not agent_changes(model) else 0
        except Exception:
            log.exception("inline_review: resolve-all global dispatch failed")
            return 0

    # User redlines are mixed in: resolve agent changes one per pass, flushing pending VCL
    # events between dispatches so each one actually takes effect in the live view.
    resolved = 0
    try:
        toolkit = ctx.getServiceManager().createInstanceWithContext("com.sun.star.awt.Toolkit", ctx)
    except Exception:
        toolkit = None
    for _ in range(200):  # generous upper bound; each pass resolves one change
        changes = agent_changes(model)
        if not changes:
            break
        if not resolve_agent_change(model, ctx, changes[0]["token"], accept):
            log.warning("inline_review: could not resolve %s; stopping", changes[0]["token"])
            break
        resolved += 1
        if toolkit is not None:
            try:
                toolkit.processEventsToIdle()
            except Exception:
                toolkit = None
    return resolved
