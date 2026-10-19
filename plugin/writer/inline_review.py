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

import contextlib
from enum import Enum, auto
import logging
from typing import Any, Callable, Iterator, NamedTuple

from plugin.framework.errors import is_real_disposal
from plugin.framework.i18n import _
from plugin.framework.uno_context import uno_same
from plugin.writer import review_scan as _review_scan

log = logging.getLogger(__name__)


class ResolveStatus(Enum):
    RESOLVED = auto()
    NOT_ELIGIBLE = auto()
    UNRELIABLE = auto()
    REFUSED_FOREIGN = auto()
    REFUSED_SIBLING = auto()
    NO_OP = auto()
    PARTIAL = auto()


class ResolveResult(NamedTuple):
    status: ResolveStatus
    resolved_count: int = 0


class ResolveAllResult(NamedTuple):
    status: ResolveStatus
    resolved: int
    total: int


class RedlineRow(NamedTuple):
    token: str | None          # full wa-review comment, or None
    identifier: Any            # raw RedlineIdentifier; do not str() it
    start: Any                 # RedlineStart, or None
    end: Any                   # RedlineEnd, or None
    kind: str | None           # RedlineType, or None
    text: Any                  # start.getText() when start is readable, else None


class RedlineView(NamedTuple):
    reliable: bool             # the scan_redlines bit, once
    total: int
    rows: tuple[RedlineRow, ...]


def _review_message(
    status: ResolveStatus,
    accept: bool = True,
    resolved: int = 0,
    total: int = 0,
    is_all: bool = False,
    group_count: int = 0,
) -> str:
    if is_all and total == 0 and status != ResolveStatus.UNRELIABLE:
        return _("No agent changes to review in this document.")

    if status == ResolveStatus.RESOLVED:
        if is_all:
            return ""
        if group_count > 0:
            if accept:
                return _("%d overlapping agent changes accepted together.") % group_count
            return _("%d overlapping agent changes rejected together.") % group_count
        return _("Change accepted.") if accept else _("Change rejected.")

    if status == ResolveStatus.UNRELIABLE:
        return _(
            "Couldn't read this document's tracked changes reliably just now -- nothing was"
            " changed. Please try again."
        )

    if status == ResolveStatus.PARTIAL:
        return _(
            "Started resolving changes but couldn't confirm the result -- some may have been"
            " resolved. Please review remaining changes in Edit > Track Changes > Manage."
        )

    if status == ResolveStatus.NO_OP:
        return _("Nothing was changed. Resolve remaining changes from Edit > Track Changes > Manage.")

    if status in (ResolveStatus.REFUSED_FOREIGN, ResolveStatus.REFUSED_SIBLING, ResolveStatus.NOT_ELIGIBLE):
        if is_all:
            if resolved == 0:
                return _(
                    "None could be resolved here -- each shares a paragraph with one of your own"
                    " tracked changes. Resolve them from Edit > Track Changes > Manage."
                )
            return _(
                "Resolved %d of %d. The rest share a paragraph with your own tracked changes --"
                " resolve those from Edit > Track Changes > Manage."
            ) % (resolved, total)
        return _(
            "Could not resolve this change here -- it may share a paragraph with one of"
            " your tracked changes or another agent change. Resolve it from Edit > Track Changes > Manage."
        )

    return _(
        "Could not resolve this change here -- it may share a paragraph with one of"
        " your tracked changes or another agent change. Resolve it from Edit > Track Changes > Manage."
    )


def scan_redline_view(model: Any) -> RedlineView:
    """Build a snapshot of document redlines without reading change text."""
    rows: list[RedlineRow] = []

    def on_item(rl: Any) -> bool:
        comment_raw = rl.getPropertyValue("RedlineComment")
        comment_str = str(comment_raw) if comment_raw is not None else None
        token = comment_str if (comment_str and _review_scan.is_agent_token(comment_str)) else None
        identifier = rl.getPropertyValue("RedlineIdentifier")
        start = rl.getPropertyValue("RedlineStart")
        end = rl.getPropertyValue("RedlineEnd")
        kind_raw = rl.getPropertyValue("RedlineType")
        kind = str(kind_raw) if kind_raw is not None else None
        text = None
        if start is not None:
            try:
                text = start.getText()
            except Exception:
                text = None
        rows.append(RedlineRow(token=token, identifier=identifier, start=start, end=end, kind=kind, text=text))
        return True

    reliable, _seen, total = _review_scan.scan_redlines(model, on_item)
    return RedlineView(reliable=reliable, total=total, rows=tuple(rows))


def _agent_change_tokens(model: Any) -> tuple[set[str], bool]:  # pyright: ignore[reportUnusedFunction]  # unit-tested guard
    view = scan_redline_view(model)
    return {r.token for r in view.rows if r.token is not None}, view.reliable


def _foreign_redline_ids(model: Any) -> tuple[set[str], bool]:  # pyright: ignore[reportUnusedFunction]  # unit-tested guard
    view = scan_redline_view(model)
    return {r.identifier for r in view.rows if r.token is None}, view.reliable


def has_agent_changes(model: Any) -> bool:
    """True if the document holds any pending AGENT change (gates the review UI entries)."""
    view = scan_redline_view(model)
    return any(r.token is not None for r in view.rows)


def resolve_change_at_cursor(model: Any, ctx: Any, accept: bool) -> tuple[bool, str]:
    """Accept (``accept=True``) or reject the AGENT change under the view cursor, as one unit
    (the whole Delete+Insert pair of a replace).

    Only agent changes (wa-review session tokens) are eligible -- the user's own tracked
    changes are left to LibreOffice's native review UI. Returns ``(ok, message)``.
    """
    token = cursor_in_agent_change(model)
    if token is None:
        if not has_agent_changes(model):
            return False, _("No agent changes to review in this document.")
        return False, _("Put the cursor on a highlighted agent change first.")

    res, after_view = resolve_agent_change(model, ctx, token, accept)
    if res.status == ResolveStatus.RESOLVED:
        return True, _review_message(ResolveStatus.RESOLVED, accept=accept)
    if res.status == ResolveStatus.REFUSED_SIBLING:
        group_res = _resolve_overlapping_agent_changes(model, ctx, token, accept, before=after_view)
        if group_res.status == ResolveStatus.RESOLVED and group_res.resolved_count > 0:
            return True, _review_message(ResolveStatus.RESOLVED, accept=accept, group_count=group_res.resolved_count)
        if group_res.status == ResolveStatus.PARTIAL:
            return False, _review_message(ResolveStatus.PARTIAL, accept=accept)
        return False, _review_message(ResolveStatus.REFUSED_SIBLING, accept=accept)
    if res.status == ResolveStatus.UNRELIABLE:
        return False, _review_message(ResolveStatus.UNRELIABLE, accept=accept)
    if res.status == ResolveStatus.PARTIAL:
        return False, _review_message(ResolveStatus.PARTIAL, accept=accept)
    if res.status == ResolveStatus.NO_OP:
        return False, _review_message(ResolveStatus.NO_OP, accept=accept)
    return False, _review_message(res.status, accept=accept)


def resolve_all_with_feedback(model: Any, ctx: Any, accept: bool) -> tuple[int, str]:
    """Accept/reject all agent changes; return ``(count_resolved, message)``."""
    result = _resolve_all_agent_changes(model, ctx, accept)
    msg = _review_message(
        result.status,
        accept=accept,
        resolved=result.resolved,
        total=result.total,
        is_all=True,
    )
    return max(0, result.resolved), msg


def show_review_message(ctx: Any, message: str) -> None:
    """Surface a review outcome to the user so a click that resolves nothing isn't silent."""
    if not message:
        return
    from plugin.chatbot.dialogs import msgbox

    msgbox(ctx, _("Review agent changes"), message)


# --- agent-change helpers (used by the click popup and the context menu) -------------------
#
# Every redline an EditReviewSession records carries a RedlineComment "wa-review:<session>:<n>"
# (see review_scan.TOKEN_PREFIX); one token = one logical change (a replace's Delete+Insert)
# pair shares it). These helpers see the document purely through those tokens, so the user's
# own tracked changes are never listed or resolved.
def _is_all_redlines_agent(view: RedlineView) -> bool:
    """True only if every tracked change in the view is an agent change and scan was complete."""
    return (
        view.reliable
        and view.total > 0
        and len(view.rows) == view.total
        and any(r.token is not None for r in view.rows)
        and all(r.token is not None for r in view.rows)
    )


def _all_redlines_are_agent(model: Any) -> bool:  # pyright: ignore[reportUnusedFunction]  # unit-tested guard
    """True only if EVERY tracked change in the document is an agent change AND scan was COMPLETE."""
    try:
        if int(model.getRedlines().getCount()) <= 0:
            return False
    except Exception:
        return False
    return _is_all_redlines_agent(scan_redline_view(model))


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


def agent_self_resolution_block_reason(model: Any) -> str | None:
    """Refusal message if a BULK accept/reject must NOT run, else ``None``."""
    try:
        if not hasattr(model, "getRedlines"):
            return None
        count = int(model.getRedlines().getCount())
    except Exception:
        return _BULK_UNRELIABLE_MSG
    if count <= 0:
        return None
    view = scan_redline_view(model)
    if not view.reliable:
        return _BULK_UNRELIABLE_MSG
    agent_tokens = {r.token for r in view.rows if r.token is not None}
    if agent_tokens:
        return _BULK_AGENT_PRESENT_MSG % len(agent_tokens)
    return None


def agent_changes(model: Any) -> list[dict[str, Any]]:
    """Pending agent changes, one entry per logical change (grouped by token), in document
    order of first appearance: ``{"token", "old", "new"}``.

    Primarily a preview helper for tests.
    """
    view = scan_redline_view(model)
    grouped: dict[str, dict[str, Any]] = {}
    for r in view.rows:
        if r.token is None:
            continue
        entry = grouped.setdefault(r.token, {"token": r.token, "old": "", "new": ""})
        if r.start is None or r.end is None:
            continue
        try:
            t = r.text if r.text is not None else r.start.getText()
            span = t.createTextCursorByRange(r.start)
            span.gotoRange(r.end, True)
            text = span.getString()
            if r.kind == "Delete":
                entry["old"] += text
            elif r.kind == "Insert":
                entry["new"] += text
        except Exception:
            continue
    return list(grouped.values())


def _build_bounds_from_view(view: RedlineView, token: str | frozenset[str]) -> tuple[Any | None, Any | None]:
    """Bounding (start, end) text ranges spanning all redlines of token in view, or (None, None)."""
    if not view.reliable:
        return None, None
    wanted = frozenset([token]) if isinstance(token, str) else token
    matching = [r for r in view.rows if r.token in wanted]
    if not matching:
        return None, None
    # Half a change (a replace's delete OR insert) must not be resolved: no bounds, caller refuses.
    if any(r.start is None or r.end is None for r in matching):
        return None, None
    try:
        first_start = matching[0].start
        text = first_start.getText()
        cursor = text.createTextCursorByRange(first_start)
        for r in matching:
            cursor.gotoRange(r.start, True)
            cursor.gotoRange(r.end, True)
        return cursor.getStart(), cursor.getEnd()
    except Exception:
        log.debug("inline_review: _build_bounds_from_view union failed; reporting no bounds", exc_info=True)
        return None, None


def _change_bounds(model: Any, token: str | frozenset[str], view: RedlineView | None = None) -> tuple[Any | None, Any | None]:
    """Bounding (start, end) text ranges spanning ALL redlines of one change, or (None, None)."""
    if view is None:
        view = scan_redline_view(model)
    return _build_bounds_from_view(view, token)


def goto_agent_change(model: Any, token: str, view: RedlineView | None = None) -> bool:
    """Move the view cursor to (and select) the given change so the user sees it."""
    left, right = _change_bounds(model, token, view=view)
    if left is None or right is None:
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
    view = scan_redline_view(model)
    return len({r.token for r in view.rows if r.token is not None})


def goto_adjacent_agent_change(model: Any, forward: bool = True) -> str | None:
    """Move the view cursor to the NEXT or PREVIOUS pending agent change from one scan."""
    view = scan_redline_view(model)
    seen: set[str] = set()
    tokens: list[str] = []
    for r in view.rows:
        if r.token is not None and r.token not in seen:
            seen.add(r.token)
            tokens.append(r.token)
    if not tokens:
        return None

    items: list[tuple[str, Any, Any]] = []
    for tok in tokens:
        # Known gap: changes without readable bounds still count in the toolbar but are skipped here.
        left, right = _build_bounds_from_view(view, tok)
        if left is None or right is None:
            continue
        try:
            t = left.getText()
            items.append((tok, t.createTextCursorByRange(left), t.createTextCursorByRange(right)))
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
        cur_collapsed = bool(vc.isCollapsed())
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
            for i, (_tok, left, right) in enumerate(items):
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
            for i, (_tok, left, right) in enumerate(items):
                try:
                    if text.compareRegionStarts(cur_start, right) == 1:
                        chosen_idx = i
                        break
                except Exception:
                    continue
            if chosen_idx is None:
                chosen_idx = 0
        else:
            for i in range(n - 1, -1, -1):
                try:
                    if text.compareRegionStarts(items[i][1], cur_start) == 1:
                        chosen_idx = i
                        break
                except Exception:
                    continue
            if chosen_idx is None:
                chosen_idx = n - 1

    tok, left_range, right_range = items[chosen_idx]
    try:
        view_cursor = model.getCurrentController().getViewCursor()
        view_cursor.gotoRange(left_range.getStart(), False)
        view_cursor.gotoRange(right_range.getEnd(), True)
    except Exception:
        log.debug("inline_review: goto_adjacent_agent_change failed to move cursor", exc_info=True)
        return None
    return tok


def cursor_in_agent_change(model: Any, view: RedlineView | None = None) -> str | None:
    """Token of the agent change the view cursor currently sits in, else None."""
    try:
        cursor = model.getCurrentController().getViewCursor()
        text = cursor.getText()
        # Compare via a model text cursor: view-cursor-owned ranges can fail XTextRangeCompare.
        pos = text.createTextCursorByRange(cursor.getStart())
    except Exception:
        return None
    if view is None:
        view = scan_redline_view(model)
    for r in view.rows:
        if r.token is None or r.start is None or r.end is None:
            continue
        try:
            span = text.createTextCursorByRange(r.start)
            span.gotoRange(r.end, True)
            if text.compareRegionStarts(pos, span) == 1:
                continue
            if text.compareRegionEnds(pos, span) == -1:
                continue
            return r.token
        except Exception:
            continue
    return None


def _span_contains_point(text: Any, span: Any, point: Any) -> bool:
    """True if collapsed range ``point`` lies within ``span``'s [start, end] (same ``text``)."""
    if text.compareRegionStarts(point, span) == 1:
        return False
    if text.compareRegionEnds(point, span) == -1:
        return False
    return True


def _classify_span_conflicts(
    rows: tuple[RedlineRow, ...],
    text: Any,
    span: Any,
    target_token: str | None,
    strict_sibling_edges: bool = False,
) -> tuple[bool, bool]:
    """Classify redline rows against span. Returns (has_foreign_conflict, has_sibling_conflict)."""
    has_sibling = False
    for r in rows:
        if r.token is not None and target_token is not None and r.token == target_token:
            continue
        is_sibling = bool(r.token is not None)
        if r.start is None or r.end is None:
            if is_sibling:
                has_sibling = True
            else:
                return True, False
            continue
        try:
            rl_span = text.createTextCursorByRange(r.start)
            rl_span.gotoRange(r.end, True)
        except Exception:
            try:
                r_text = r.text if r.text is not None else r.start.getText()
                in_other_text = not uno_same(r_text, text)
            except Exception:
                in_other_text = False
            if in_other_text:
                continue
            if is_sibling:
                has_sibling = True
            else:
                return True, False
            continue

        try:
            if is_sibling and strict_sibling_edges:
                overlaps = (text.compareRegionStarts(rl_span.getStart(), span.getEnd()) == 1
                            and text.compareRegionStarts(span.getStart(), rl_span.getEnd()) == 1)
            else:
                overlaps = (_span_contains_point(text, span, rl_span.getStart())
                            or _span_contains_point(text, span, rl_span.getEnd())
                            or _span_contains_point(text, rl_span, span.getStart()))
        except Exception:
            if is_sibling:
                has_sibling = True
            else:
                return True, False
            continue

        if overlaps:
            if is_sibling:
                has_sibling = True
            else:
                return True, False
    return False, has_sibling


def _span_has_redline(model: Any, text: Any, span: Any, consider: Callable[[str | None], bool], strict_edges: bool = False) -> bool:
    """Compatibility helper for unit tests checking individual spans."""
    view = scan_redline_view(model)
    if not view.reliable:
        return True
    for r in view.rows:
        if not consider(r.token):
            continue
        # A protected redline with no start/end cannot be proven outside the span.
        if r.start is None or r.end is None:
            return True
        try:
            rl_span = text.createTextCursorByRange(r.start)
            rl_span.gotoRange(r.end, True)
        except Exception:
            try:
                r_text = r.text if r.text is not None else r.start.getText()
                in_other = not uno_same(r_text, text)
            except Exception:
                in_other = False
            if in_other:
                continue
            return True
        try:
            if strict_edges:
                overlaps = (text.compareRegionStarts(rl_span.getStart(), span.getEnd()) == 1
                            and text.compareRegionStarts(span.getStart(), rl_span.getEnd()) == 1)
            else:
                overlaps = (_span_contains_point(text, span, rl_span.getStart())
                            or _span_contains_point(text, span, rl_span.getEnd())
                            or _span_contains_point(text, rl_span, span.getStart()))
        except Exception:
            return True
        if overlaps:
            return True
    return False


def _foreign_redline_in_span(model: Any, text: Any, span: Any) -> bool:  # pyright: ignore[reportUnusedFunction]  # unit-tested guard
    return _span_has_redline(model, text, span,
                             lambda c: c is None or not _review_scan.is_agent_token(c))


def _other_agent_redline_in_span(  # pyright: ignore[reportUnusedFunction]  # unit-tested guard
    model: Any, text: Any, span: Any, token: str, strict_edges: bool = False
) -> bool:
    return _span_has_redline(model, text, span,
                             lambda c: c is None or (_review_scan.is_agent_token(c) and c != token),
                             strict_edges=strict_edges)


def _execute_dispatch(ctx: Any, controller: Any, command: str) -> None:
    smgr = ctx.getServiceManager()
    dispatcher = smgr.createInstanceWithContext("com.sun.star.frame.DispatchHelper", ctx)
    dispatcher.executeDispatch(controller.getFrame(), command, "", 0, ())


def _dispatch_resolve(ctx: Any, controller: Any, accept: bool) -> None:
    """Drive the native accept/reject dispatch on the controller's current selection."""
    command = ".uno:AcceptTrackedChange" if accept else ".uno:RejectTrackedChange"
    _execute_dispatch(ctx, controller, command)


def _token_delta(before: RedlineView, after: RedlineView) -> tuple[set[str], bool]:
    """Calculate (removed_agent_tokens, foreign_safe) between two snapshots."""
    before_tokens = {r.token for r in before.rows if r.token is not None}
    after_tokens = {r.token for r in after.rows if r.token is not None}
    removed = before_tokens - after_tokens
    before_foreign = {r.identifier for r in before.rows if r.token is None}
    after_foreign = {r.identifier for r in after.rows if r.token is None}
    foreign_safe = after.reliable and not (before_foreign - after_foreign)
    return removed, foreign_safe


def resolve_agent_change(
    model: Any,
    ctx: Any,
    token: str,
    accept: bool,
    refuse_sibling_agent: bool = True,
    prefer_exact: bool = True,
    before: RedlineView | None = None,
) -> tuple[ResolveResult, RedlineView]:
    """Accept/reject ONE agent change by token.

    Returns (ResolveResult, after_view).
    """
    if before is None:
        before = scan_redline_view(model)
    if not before.reliable:
        log.warning("inline_review: refusing resolve of %s -- pre-dispatch snapshot unreliable", token)
        return ResolveResult(ResolveStatus.UNRELIABLE), before

    agent_tokens = {r.token for r in before.rows if r.token is not None}
    if token not in agent_tokens:
        return ResolveResult(ResolveStatus.NOT_ELIGIBLE), before

    left, right = _build_bounds_from_view(before, token)
    if left is None or right is None:
        return ResolveResult(ResolveStatus.NOT_ELIGIBLE), before

    dispatched = False
    try:
        text = left.getText()
        controller = model.getCurrentController()
        view_cursor = controller.getViewCursor()

        # 1) Exact-bounds path
        # 1) Precise path: select EXACTLY this change's marks and dispatch -- resolves only it.
        if prefer_exact:
            exact = text.createTextCursorByRange(left)
            exact.gotoRange(right, True)
            has_foreign, has_sibling = _classify_span_conflicts(
                before.rows, text, exact, token, strict_sibling_edges=True
            )
            if has_foreign:
                log.debug("inline_review: refusing inline resolve of %s -- user redline overlaps", token)
                return ResolveResult(ResolveStatus.REFUSED_FOREIGN), before
            if refuse_sibling_agent and has_sibling:
                log.debug("inline_review: refusing inline resolve of %s -- sibling agent change overlaps", token)
                return ResolveResult(ResolveStatus.REFUSED_SIBLING), before

            view_cursor.gotoRange(left, False)
            view_cursor.gotoRange(right, True)
            try:
                dispatched = True
                _dispatch_resolve(ctx, controller, accept)
            except Exception:
                log.exception("inline_review: exact dispatch failed for %s", token)
                after = scan_redline_view(model)
                return ResolveResult(ResolveStatus.PARTIAL), after

            after = scan_redline_view(model)
            removed, foreign_safe = _token_delta(before, after)

            if removed == {token} and after.reliable and foreign_safe:
                return ResolveResult(ResolveStatus.RESOLVED, 1), after
            if removed or not after.reliable or not foreign_safe:
                log.warning("inline_review: exact resolve of %s had post-dispatch failure (removed=%s, reliable=%s, foreign_safe=%s)",
                            token, removed, after.reliable, foreign_safe)
                return ResolveResult(ResolveStatus.PARTIAL), after

            log.debug("inline_review: exact resolve of %s was no-op; widening to paragraph", token)

        # 2) Paragraph-wide path
        # 2) Paragraph-wide path (older LibreOffice, or resolve-all): resolves everything in range,
        #    so refuse when a redline that we must not touch shares those paragraphs.
        start = text.createTextCursorByRange(left)
        start.gotoStartOfParagraph(False)
        end = text.createTextCursorByRange(right)
        end.gotoEndOfParagraph(True)
        para_span = text.createTextCursorByRange(start.getStart())
        para_span.gotoRange(end.getEnd(), True)

        has_foreign, has_sibling = _classify_span_conflicts(
            before.rows, text, para_span, token, strict_sibling_edges=False
        )
        if has_foreign:
            log.debug("inline_review: refusing paragraph resolve of %s -- user redline in paragraph", token)
            return ResolveResult(ResolveStatus.REFUSED_FOREIGN), before
        if refuse_sibling_agent and has_sibling:
            log.debug("inline_review: refusing paragraph resolve of %s -- sibling in paragraph", token)
            return ResolveResult(ResolveStatus.REFUSED_SIBLING), before

        view_cursor.gotoRange(start.getStart(), False)
        view_cursor.gotoRange(end.getEnd(), True)
        try:
            dispatched = True
            _dispatch_resolve(ctx, controller, accept)
        except Exception:
            log.exception("inline_review: paragraph dispatch failed for %s", token)
            after = scan_redline_view(model)
            return ResolveResult(ResolveStatus.PARTIAL), after

        after = scan_redline_view(model)
        after_tokens = {r.token for r in after.rows if r.token is not None}
        removed, foreign_safe = _token_delta(before, after)

        if token not in after_tokens and after.reliable and foreign_safe:
            return ResolveResult(ResolveStatus.RESOLVED, 1), after
        if token in after_tokens and after.reliable and foreign_safe and not removed:
            return ResolveResult(ResolveStatus.NO_OP), after
        return ResolveResult(ResolveStatus.PARTIAL), after
    except Exception as exc:
        if is_real_disposal(exc) and not dispatched:
            log.warning("inline_review: resolve_agent_change aborted on disposed document for %s", token)
            return ResolveResult(ResolveStatus.UNRELIABLE), before
        log.exception("inline_review: resolve_agent_change failed")
        return ResolveResult(ResolveStatus.PARTIAL if dispatched else ResolveStatus.UNRELIABLE), before


@contextlib.contextmanager
def _grouped_resolve_undo(model: Any) -> Iterator[None]:
    """Group dispatches into one undo step; keeps landed edits on failure."""
    um = None
    entered = False
    try:
        if hasattr(model, "getUndoManager"):
            candidate = model.getUndoManager()
            if candidate is not None:
                is_locked = False
                if hasattr(candidate, "isLocked"):
                    try:
                        is_locked = bool(candidate.isLocked())
                    except Exception:
                        is_locked = False
                if not is_locked:
                    try:
                        candidate.enterUndoContext(_("Resolve agent changes"))
                        um = candidate
                        entered = True
                    except Exception:
                        log.warning("inline_review: enterUndoContext failed, undo grouping skipped", exc_info=True)
                else:
                    log.warning("inline_review: undo manager is locked, undo grouping skipped")
    except Exception:
        log.warning("inline_review: could not access undo manager, undo grouping skipped", exc_info=True)
    try:
        yield
    finally:
        if entered and um is not None:
            try:
                um.leaveUndoContext()
            except Exception:
                log.warning("inline_review: leaveUndoContext failed", exc_info=True)


def _resolve_overlapping_agent_changes(
    model: Any, ctx: Any, token: str, accept: bool, before: RedlineView | None = None
) -> ResolveResult:
    """Accept/reject *token* together with the agent changes that overlap it."""
    if before is None:
        before = scan_redline_view(model)
    if not before.reliable:
        return ResolveResult(ResolveStatus.UNRELIABLE, 0)
    before_tokens = {r.token for r in before.rows if r.token is not None}
    if token not in before_tokens:
        return ResolveResult(ResolveStatus.NOT_ELIGIBLE, 0)

    token_bounds: dict[str, tuple[Any, Any]] = {}
    for tok in before_tokens:
        b_left, b_right = _build_bounds_from_view(before, tok)
        if b_left is None or b_right is None:
            return ResolveResult(ResolveStatus.NOT_ELIGIBLE, 0)
        token_bounds[tok] = (b_left, b_right)

    group = frozenset([token])
    left, right = token_bounds[token]

    dispatched = False
    has_foreign = False
    try:
        text = left.getText()
        grew = True
        while grew:
            grew = False
            left, right = _build_bounds_from_view(before, group)
            if left is None or right is None:
                return ResolveResult(ResolveStatus.NOT_ELIGIBLE, 0)
            for other in sorted(before_tokens - group):
                o_left, o_right = token_bounds[other]
                try:
                    # Strict overlap (shared text, not just a touching edge): an independent
                    # agent change right next to this one is not resolved along with it.
                    # compareRegionStarts(a, b) == 1 means a starts before b.
                    overlaps = (text.compareRegionStarts(o_left, right) == 1
                                and text.compareRegionStarts(left, o_right) == 1)
                except Exception:
                    try:
                        in_other = not uno_same(o_left.getText(), text)
                    except Exception:
                        in_other = False
                    if in_other:
                        continue
                    return ResolveResult(ResolveStatus.REFUSED_FOREIGN, 0)
                if overlaps:
                    group = group | {other}
                    grew = True

        if len(group) < 2:
            return ResolveResult(ResolveStatus.REFUSED_SIBLING, 0)

        span = text.createTextCursorByRange(left)
        span.gotoRange(right, True)
        has_foreign, _unused_sibling = _classify_span_conflicts(before.rows, text, span, None)
        if has_foreign:
            return ResolveResult(ResolveStatus.REFUSED_FOREIGN, 0)

        controller = model.getCurrentController()
        view_cursor = controller.getViewCursor()
        pending = group
        cur_view = before

        with _grouped_resolve_undo(model):
            # A Delete stacked on an Insert is undone one layer per dispatch: rejecting it first
            # restores the inserted text, which still carries the other change -- dispatch again
            # over what is left of the group while each pass makes progress (checked live).
            for _unused in range(len(group) + 1):
                left, right = _build_bounds_from_view(cur_view, pending)
                if left is None or right is None:
                    break
                pass_span = text.createTextCursorByRange(left)
                pass_span.gotoRange(right, True)
                has_foreign, _unused_pass_sibling = _classify_span_conflicts(cur_view.rows, text, pass_span, None)
                if has_foreign:
                    break
                view_cursor.gotoRange(left, False)
                view_cursor.gotoRange(right, True)
                try:
                    dispatched = True
                    _dispatch_resolve(ctx, controller, accept)
                except Exception:
                    break
                now = scan_redline_view(model)
                cur_view = now
                if not now.reliable:
                    break
                now_tokens = {r.token for r in now.rows if r.token is not None}
                left_over = pending & now_tokens
                if not left_over or left_over == pending:
                    break
                pending = frozenset(left_over)

        now = cur_view
        removed, foreign_safe = _token_delta(before, now)

        if removed == group and now.reliable and foreign_safe:
            return ResolveResult(ResolveStatus.RESOLVED, len(group))

        if dispatched:
            log.warning("inline_review: overlapping resolve of %s removed %s (expected %s); partial result",
                        token, removed, group)
            return ResolveResult(ResolveStatus.PARTIAL, 0)
        return ResolveResult(ResolveStatus.REFUSED_FOREIGN if has_foreign else ResolveStatus.REFUSED_SIBLING, 0)
    except Exception as exc:
        if is_real_disposal(exc) and not dispatched:
            log.warning("inline_review: overlapping resolve aborted on disposed document for %s", token)
            return ResolveResult(ResolveStatus.UNRELIABLE, 0)
        log.exception("inline_review: resolving overlapping agent changes of %s failed", token)
        return ResolveResult(ResolveStatus.PARTIAL if dispatched else ResolveStatus.UNRELIABLE, 0)


def _resolve_all_agent_changes(model: Any, ctx: Any, accept: bool) -> ResolveAllResult:
    """Accept/reject all agent changes; returns ResolveAllResult."""
    entry_view = scan_redline_view(model)
    if not entry_view.reliable:
        log.warning("inline_review: resolve-all aborted -- redline snapshot unreliable")
        return ResolveAllResult(status=ResolveStatus.UNRELIABLE, resolved=0, total=0)
    entry_tokens = {r.token for r in entry_view.rows if r.token is not None}
    total = len(entry_tokens)
    if total == 0:
        return ResolveAllResult(status=ResolveStatus.RESOLVED, resolved=0, total=0)

    if _is_all_redlines_agent(entry_view):
        try:
            controller = model.getCurrentController()
            command = ".uno:AcceptAllTrackedChanges" if accept else ".uno:RejectAllTrackedChanges"
            _execute_dispatch(ctx, controller, command)
        except Exception:
            log.exception("inline_review: resolve-all global dispatch failed")
            return ResolveAllResult(status=ResolveStatus.PARTIAL, resolved=0, total=total)

        after = scan_redline_view(model)
        if not after.reliable:
            return ResolveAllResult(status=ResolveStatus.PARTIAL, resolved=0, total=total)
        after_tokens = {r.token for r in after.rows if r.token is not None}
        resolved = max(0, total - len(after_tokens))
        if not after_tokens:
            return ResolveAllResult(status=ResolveStatus.RESOLVED, resolved=total, total=total)
        if len(after_tokens) == total:
            return ResolveAllResult(status=ResolveStatus.NO_OP, resolved=0, total=total)
        return ResolveAllResult(status=ResolveStatus.PARTIAL, resolved=resolved, total=total)

    skip: set[str] = set()
    skip_reasons: dict[str, ResolveStatus] = {}
    dispatched = False
    cur_view = entry_view

    with _grouped_resolve_undo(model):
        # Tip: no fixed cap. Each pass resolves the head change or adds it to `skip`, so pending
        # strictly shrinks. An unreliable re-read aborts (PARTIAL once dispatched, else UNRELIABLE)
        # instead of silently leaving changes pending.
        while True:
            if not cur_view.reliable:
                log.warning("inline_review: resolve-all aborted mid-pass -- redline snapshot unreliable")
                return ResolveAllResult(
                    status=ResolveStatus.PARTIAL if dispatched else ResolveStatus.UNRELIABLE,
                    resolved=0,
                    total=total,
                )
            cur_tokens = {r.token for r in cur_view.rows if r.token is not None}
            pending = sorted(t for t in cur_tokens if t not in skip)
            if not pending:
                break
            token = pending[0]
            before_count = len(cur_tokens)
            # Resolve-all wants sibling agent changes resolved together; only a user redline blocks it.
            res, next_view = resolve_agent_change(
                model, ctx, token, accept, refuse_sibling_agent=False, prefer_exact=False, before=cur_view
            )
            # A dispatch was attempted once status is in this set.
            if res.status in (ResolveStatus.RESOLVED, ResolveStatus.NO_OP, ResolveStatus.PARTIAL):
                dispatched = True
            cur_view = next_view
            if not cur_view.reliable:
                return ResolveAllResult(
                    status=ResolveStatus.PARTIAL if dispatched else ResolveStatus.UNRELIABLE,
                    resolved=0,
                    total=total,
                )
            after_tokens = {r.token for r in cur_view.rows if r.token is not None}
            if res.status != ResolveStatus.RESOLVED or len(after_tokens) >= before_count:
                # Tip: skip refused/failed/no-progress changes so the rest resolve and nothing spins.
                log.debug("inline_review: skipping %s in resolve-all (%s)", token, res.status)
                skip.add(token)
                skip_reasons[token] = res.status

    final_tokens = {r.token for r in cur_view.rows if r.token is not None}
    # Tip: count tokens that disappeared; one dispatch can clear several siblings.
    resolved = max(0, total - len(final_tokens))

    if resolved == total:
        return ResolveAllResult(status=ResolveStatus.RESOLVED, resolved=total, total=total)
    if skip_reasons and all(s == ResolveStatus.REFUSED_FOREIGN for s in skip_reasons.values()):
        return ResolveAllResult(status=ResolveStatus.REFUSED_FOREIGN, resolved=resolved, total=total)
    if resolved == 0 and not any(s == ResolveStatus.REFUSED_FOREIGN for s in skip_reasons.values()):
        return ResolveAllResult(status=ResolveStatus.NO_OP, resolved=0, total=total)
    return ResolveAllResult(status=ResolveStatus.PARTIAL, resolved=resolved, total=total)


def resolve_all_agent_changes(model: Any, ctx: Any, accept: bool) -> int:
    """Accept/reject every pending agent change (the user's own redlines stay untouched)."""
    result = _resolve_all_agent_changes(model, ctx, accept)
    return max(0, result.resolved)
