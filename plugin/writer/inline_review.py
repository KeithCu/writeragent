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

# Shown when a conservative resolve refuses because the change shares a paragraph with the
# user's own tracked change (or another agent change). Surfaced via show_review_message() so a
# click that resolves nothing on purpose isn't silent.
_RESOLVE_REFUSED_HINT = (
    "Could not resolve this change here -- it may share a paragraph with one of"
    " your tracked changes or another agent change. Resolve it from Edit > Track Changes > Manage."
)


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
        return False, _RESOLVE_REFUSED_HINT
    return True, "Change accepted." if accept else "Change rejected."


def resolve_all_with_feedback(model: Any, ctx: Any, accept: bool) -> tuple[int, str]:
    """Accept/reject all agent changes; return ``(count_resolved, message)``. The message is a
    user-facing note when some or all changes were skipped -- they share a paragraph with one of
    the user's own redlines, which resolve-all deliberately won't touch -- and empty when
    everything resolved (or there was nothing to do)."""
    total = len(agent_changes(model))
    if total == 0:
        return 0, "No agent changes to review in this document."
    n = resolve_all_agent_changes(model, ctx, accept)
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


def _span_contains_point(text: Any, span: Any, point: Any) -> bool:
    """True if collapsed range ``point`` lies within ``span``'s [start, end] (same ``text``)."""
    try:
        if text.compareRegionStarts(point, span) == 1:   # point starts before span -> left of it
            return False
        if text.compareRegionEnds(point, span) == -1:    # point ends after span -> right of it
            return False
        return True
    except Exception:
        return False


def _foreign_redline_in_span(model: Any, text: Any, span: Any) -> bool:
    """True if any redline NOT tagged by an EditReviewSession overlaps ``span``.

    The inline resolve drives a paragraph-wide ``.uno`` dispatch that resolves EVERY redline in
    the range, so when a user-owned tracked change shares those paragraphs we refuse rather than
    silently accept/reject it. Best-effort: a detection failure falls back to "no foreign
    redline" so the common case (the user has none of their own) is never blocked.
    """
    from plugin.writer.edit_review import TOKEN_PREFIX

    try:
        enum = model.getRedlines().createEnumeration()
    except Exception:
        return False
    while enum.hasMoreElements():
        try:
            rl = enum.nextElement()
            if str(rl.getPropertyValue("RedlineComment")).startswith(TOKEN_PREFIX):
                continue  # one of ours -- safe to resolve
            s = rl.getPropertyValue("RedlineStart")
            e = rl.getPropertyValue("RedlineEnd")
            if s is None or e is None:
                continue
            rl_span = text.createTextCursorByRange(s)
            rl_span.gotoRange(e, True)
            # Overlap iff the foreign redline's start/end falls inside span, or span starts inside it.
            if (_span_contains_point(text, span, rl_span.getStart())
                    or _span_contains_point(text, span, rl_span.getEnd())
                    or _span_contains_point(text, rl_span, span.getStart())):
                return True
        except Exception:
            continue
    return False


def _other_agent_redline_in_span(model: Any, text: Any, span: Any, token: str) -> bool:
    """True if another agent change token overlaps ``span``.

    The inline "this change" command is only truthful when the paragraph-wide dispatch contains
    one logical agent change. If a second wa-review token shares the paragraph, LibreOffice would
    resolve both, so we refuse and leave that dense case to the native Manage dialog.
    """
    from plugin.writer.edit_review import TOKEN_PREFIX

    try:
        enum = model.getRedlines().createEnumeration()
    except Exception:
        return False
    while enum.hasMoreElements():
        try:
            rl = enum.nextElement()
            comment = str(rl.getPropertyValue("RedlineComment"))
            if not comment.startswith(TOKEN_PREFIX) or comment == token:
                continue
            s = rl.getPropertyValue("RedlineStart")
            e = rl.getPropertyValue("RedlineEnd")
            if s is None or e is None:
                continue
            rl_span = text.createTextCursorByRange(s)
            rl_span.gotoRange(e, True)
            if (_span_contains_point(text, span, rl_span.getStart())
                    or _span_contains_point(text, span, rl_span.getEnd())
                    or _span_contains_point(text, rl_span, span.getStart())):
                return True
        except Exception:
            continue
    return False


def resolve_agent_change(model: Any, ctx: Any, token: str, accept: bool, refuse_sibling_agent: bool = True) -> bool:
    """Accept/reject ONE agent change (both marks of its pair) by token.

    ``refuse_sibling_agent`` (default True) makes the single "resolve this change" command refuse
    when ANOTHER agent change shares the paragraph, so it never silently resolves two. resolve-all
    passes False -- there, resolving sibling agent changes together is the whole point; only a
    USER redline in the paragraph still blocks it.

    The dispatch selection is expanded to whole PARAGRAPHS around the change: empirically,
    .uno:Accept/RejectTrackedChange does not resolve redlines when the selection matches the
    redline bounds exactly, but does on a paragraph-wide selection. Because that dispatch
    resolves EVERY redline in the selection, we first refuse (return False) when a redline that
    isn't ours shares those paragraphs -- the user's own tracked changes are never touched.
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
        # Never resolve a paragraph that also holds one of the user's own tracked changes:
        # the dispatch below would accept/reject it too. Leave that case to the native review UI.
        para_span = text.createTextCursorByRange(start.getStart())
        para_span.gotoRange(end.getEnd(), True)
        if _foreign_redline_in_span(model, text, para_span):
            log.debug("inline_review: refusing inline resolve of %s -- a user redline shares its paragraph", token)
            return False
        if refuse_sibling_agent and _other_agent_redline_in_span(model, text, para_span, token):
            log.debug("inline_review: refusing inline resolve of %s -- another agent change shares its paragraph", token)
            return False
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
    skip: set[str] = set()  # changes that share a paragraph with a user redline (or failed)
    try:
        toolkit = ctx.getServiceManager().createInstanceWithContext("com.sun.star.awt.Toolkit", ctx)
    except Exception:
        toolkit = None
    for _ in range(200):  # generous upper bound; each pass resolves one change
        pending = [c for c in agent_changes(model) if c["token"] not in skip]
        if not pending:
            break
        token = pending[0]["token"]
        # resolve-all WANTS sibling agent changes in a paragraph resolved together -- only a USER
        # redline there blocks it (checked inside via _foreign_redline_in_span). Single
        # "resolve this change" keeps refuse_sibling_agent=True so it stays truthful.
        if not resolve_agent_change(model, ctx, token, accept, refuse_sibling_agent=False):
            # Refused (shares a paragraph with the user's own redline) or failed: skip it and keep
            # resolving the rest -- the user handles the skipped one via the native review UI.
            log.debug("inline_review: skipping %s in resolve-all (foreign redline or failure)", token)
            skip.add(token)
            continue
        resolved += 1
        if toolkit is not None:
            try:
                toolkit.processEventsToIdle()
            except Exception:
                toolkit = None
    return resolved
