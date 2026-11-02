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

from collections import Counter
import contextlib
from enum import Enum, auto
import logging
from typing import Any, Generator, NamedTuple

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
    start: Any                 # RedlineStart, or None
    end: Any                   # RedlineEnd, or None
    identity: tuple[Any, ...] | None = None


class RedlineView(NamedTuple):
    reliable: bool             # the scan_redlines bit, once
    total: int
    rows: tuple[RedlineRow, ...]


def agent_tokens(view: RedlineView) -> set[str]:
    """All agent tokens present in view."""
    return {r.token for r in view.rows if r.token is not None}


def _review_message(
    status: ResolveStatus,
    accept: bool = True,
    group_count: int = 0,
) -> str:
    if status == ResolveStatus.RESOLVED:
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

    return _(
        "Could not resolve this change here -- it may share a paragraph with one of"
        " your tracked changes or another agent change. Resolve it from Edit > Track Changes > Manage."
    )


def _resolve_all_message(status: ResolveStatus, resolved: int, total: int) -> str:
    if status == ResolveStatus.UNRELIABLE:
        return _review_message(status)
    if total == 0:
        return _("No agent changes to review in this document.")
    if status == ResolveStatus.RESOLVED:
        return ""
    if status in (ResolveStatus.PARTIAL, ResolveStatus.NO_OP):
        return _review_message(status)
    if resolved == 0:
        return _(
            "None could be resolved here -- each overlaps with one of your own"
            " tracked changes. Resolve them from Edit > Track Changes > Manage."
        )
    return _(
        "Resolved %(resolved)d of %(total)d. The rest overlap with your own tracked changes --"
        " resolve those from Edit > Track Changes > Manage."
    ) % {"resolved": resolved, "total": total}


def scan_redline_view(model: Any) -> RedlineView:
    """Build a snapshot of document redlines without reading change text."""
    rows: list[RedlineRow] = []

    def on_item(rl: Any) -> bool:
        comment_raw = rl.getPropertyValue("RedlineComment")
        comment_str = _review_scan.normalize_redline_comment(comment_raw)
        token = comment_str if (comment_str and _review_scan.is_agent_token(comment_str)) else None
        start = rl.getPropertyValue("RedlineStart")
        end = rl.getPropertyValue("RedlineEnd")

        if token is not None:
            identity = None
        else:
            rid = rl.getPropertyValue("RedlineIdentifier")

            def prop(name: str) -> Any:
                try:
                    return rl.getPropertyValue(name)
                except Exception:
                    return None

            identity = (
                rid,
                prop("RedlineType"),
                prop("RedlineAuthor"),
                _review_scan.redline_date_key(prop("RedlineDateTime")),
                comment_raw,
            )

        rows.append(
            RedlineRow(
                token=token,
                start=start,
                end=end,
                identity=identity,
            )
        )
        return True

    reliable, _seen, total = _review_scan.scan_redlines(model, on_item)
    return RedlineView(reliable=reliable, total=total, rows=tuple(rows))


def resolve_change_at_cursor(
    model: Any,
    ctx: Any,
    accept: bool,
    *,
    token: str | None = None,
    view: RedlineView | None = None,
) -> tuple[bool, str]:
    """Accept (``accept=True``) or reject the AGENT change under the view cursor, as one unit
    (the whole Delete+Insert pair of a replace).

    Only agent changes (wa-review session tokens) are eligible -- the user's own tracked
    changes are left to LibreOffice's native review UI. Returns ``(ok, message)``.
    """
    if view is None:
        view = scan_redline_view(model)
    if token is None:
        token = cursor_in_agent_change(model, view=view)
    if token is None:
        if not view.reliable:
            return False, _review_message(ResolveStatus.UNRELIABLE, accept=accept)
        tokens = agent_tokens(view)
        if not tokens:
            return False, _("No agent changes to review in this document.")
        return False, _("Put the cursor on a highlighted agent change first.")

    res, after_view = resolve_agent_change(model, ctx, token, accept, before=view)
    if res.status == ResolveStatus.RESOLVED:
        return True, _review_message(ResolveStatus.RESOLVED, accept=accept)
    if res.status == ResolveStatus.REFUSED_SIBLING:
        group_res = _resolve_overlapping_agent_changes(model, ctx, token, accept, before=after_view)
        if group_res.status == ResolveStatus.RESOLVED and group_res.resolved_count > 0:
            return True, _review_message(ResolveStatus.RESOLVED, accept=accept, group_count=group_res.resolved_count)
        status = group_res.status if group_res.status in (ResolveStatus.UNRELIABLE, ResolveStatus.PARTIAL) else ResolveStatus.REFUSED_SIBLING
        return False, _review_message(status, accept=accept)
    return False, _review_message(res.status, accept=accept)


def resolve_all_with_feedback(model: Any, ctx: Any, accept: bool) -> tuple[int, str]:
    """Accept/reject all agent changes; return ``(count_resolved, message)``."""
    result = _resolve_all_agent_changes(model, ctx, accept)
    msg = _resolve_all_message(result.status, result.resolved, result.total)
    return result.resolved, msg


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
        and all(r.token is not None for r in view.rows)
    )


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
    except Exception:
        return _BULK_UNRELIABLE_MSG
    count, reliable = _review_scan.agent_change_presence(model)
    if not reliable:
        return _BULK_UNRELIABLE_MSG
    if count > 0:
        return _BULK_AGENT_PRESENT_MSG % count
    return None


def _group_rows_by_text(rows: list[RedlineRow]) -> list[tuple[Any, list[RedlineRow]]]:
    """Group rows by text object identity using uno_same."""
    groups: list[tuple[Any, list[RedlineRow]]] = []
    for r in rows:
        try:
            t = r.start.getText()
        except Exception:
            t = None
        if t is None:
            continue
        found = False
        for g_text, g_rows in groups:
            try:
                if uno_same(t, g_text):
                    g_rows.append(r)
                    found = True
                    break
            except Exception:
                log.debug("inline_review: uno_same failed while grouping redlines", exc_info=True)
                continue
        if not found:
            groups.append((t, [r]))
    return groups


def _span_for_rows(text: Any, rows: list[RedlineRow] | tuple[RedlineRow, ...]) -> tuple[Any, Any] | None:
    """Bounding (start, end) ranges spanning all rows in a text object."""
    try:
        cursor = text.createTextCursorByRange(rows[0].start)
        for r in rows:
            cursor.gotoRange(r.start, True)
            cursor.gotoRange(r.end, True)
        return cursor.getStart(), cursor.getEnd()
    except Exception:
        log.debug("inline_review: _span_for_rows union failed", exc_info=True)
        return None


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

    groups = _group_rows_by_text(matching)
    if len(groups) != 1:
        if len(groups) > 1:
            log.warning("inline_review: change %s spans multiple text objects; reporting no bounds", token)
        return None, None

    text, group_rows = groups[0]
    bounds = _span_for_rows(text, group_rows)
    return bounds or (None, None)


def goto_adjacent_agent_change(model: Any, forward: bool = True) -> str | None:
    """Move the view cursor to the NEXT or PREVIOUS pending agent change from one scan."""
    view = scan_redline_view(model)
    token_rows: dict[str, list[RedlineRow]] = {}
    for r in view.rows:
        if r.token is not None:
            token_rows.setdefault(r.token, []).append(r)
    if not token_rows:
        return None

    # Tip: Build bounds inside each text group. Multi-text tokens get one stop per text object
    # in enumeration order instead of being dropped entirely.
    items: list[tuple[str, Any, Any, Any]] = []
    for tok, matching in token_rows.items():
        groups = _group_rows_by_text(matching)
        for g_text, g_rows in groups:
            if any(r.start is None or r.end is None for r in g_rows):
                continue
            span = _span_for_rows(g_text, g_rows)
            if span is None:
                continue
            items.append((tok, g_text, span[0], span[1]))

    if not items:
        return None
    n = len(items)

    cur_text = None
    cur_start: Any = None
    cur_end: Any = None
    cur_collapsed = False
    try:
        vc = model.getCurrentController().getViewCursor()
        vc_text = vc.getText()
        cur_start = vc_text.createTextCursorByRange(vc.getStart())
        cur_end = vc_text.createTextCursorByRange(vc.getEnd())
        cur_collapsed = bool(vc.isCollapsed())
    except Exception:  # noqa: S110 - cursor read failure leaves cur_text None
        pass
    else:
        cur_text = vc_text

    chosen_idx = None
    if cur_text is None:
        chosen_idx = 0 if forward else n - 1
    else:
        # Tip: a selection counts as "reviewing" a change only when it matches the change's whole span
        # (start and end). A collapsed caret never counts, so the first change stays reachable.
        sel_idx = None
        if not cur_collapsed:
            for i, (_tok, stop_text, left, right) in enumerate(items):
                try:
                    if not uno_same(stop_text, cur_text):
                        continue
                    if (cur_text.compareRegionStarts(cur_start, left) == 0
                            and cur_text.compareRegionEnds(cur_end, right) == 0):
                        sel_idx = i
                        break
                except Exception:
                    log.debug("inline_review: selection span compare failed", exc_info=True)
                    continue
        if sel_idx is not None:
            chosen_idx = (sel_idx + 1) % n if forward else (sel_idx - 1) % n
        else:
            anchor = -1
            for i, (_tok, stop_text, _left, right) in enumerate(items):
                try:
                    if not uno_same(stop_text, cur_text):
                        continue
                except Exception:
                    log.debug("inline_review: uno_same failed while locating the adjacent change", exc_info=True)
                    continue
                try:
                    if cur_text.compareRegionStarts(cur_start, right) != 1:
                        anchor = i
                except Exception:
                    log.debug("inline_review: compareRegionStarts failed while locating the adjacent change", exc_info=True)
                    continue
            if forward:
                chosen_idx = (anchor + 1) % n
            else:
                chosen_idx = anchor % n

    tok, _text, left_range, right_range = items[chosen_idx]
    try:
        view_cursor = model.getCurrentController().getViewCursor()
        left_bound = left_range.getStart()
        right_bound = right_range.getEnd()
        view_cursor.gotoRange(left_bound, False)
        view_cursor.gotoRange(right_bound, True)
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
            if not _span_contains_point(text, span, pos):
                continue
            return r.token
        except Exception:
            log.debug("inline_review: cursor_in_agent_change span compare failed", exc_info=True)
            continue
    return None


def _span_contains_point(text: Any, span: Any, point: Any) -> bool:
    """True if collapsed range ``point`` lies within ``span``'s [start, end] (same ``text``)."""
    if text.compareRegionStarts(point, span) == 1:
        return False
    if text.compareRegionEnds(point, span) == -1:
        return False
    return True


def _user_redline_whitespace_adjacent(text: Any, span: Any, rl_span: Any) -> bool:
    """Return True if span and user redline span are separated only by whitespace.

    Fix: exact retry treated a one-space foreign neighbor as safe.
    What was wrong: #1943 retried exact bounds after paragraph REFUSED_FOREIGN, but
    point containment across a one-space gap evaluates to false, allowing resolve-all
    to mistakenly accept the neighboring edit.
    Why this fixes it: when the character gap between the agent span and a user
    redline consists entirely of whitespace, we treat them as overlapping to refuse
    the exact retry. Gaps with any other characters (words) remain eligible for retry.
    """
    try:
        if text.compareRegionStarts(span.getEnd(), rl_span.getStart()) == 1:
            a_end, b_start = span.getEnd(), rl_span.getStart()
        elif text.compareRegionStarts(rl_span.getEnd(), span.getStart()) == 1:
            a_end, b_start = rl_span.getEnd(), span.getStart()
        else:
            return False
        cursor = text.createTextCursorByRange(a_end)
        cursor.gotoRange(b_start, True)
        gap = cursor.getString()
        return "\n" not in gap and "\r" not in gap and not gap.strip()
    except Exception:
        return False


def _in_other_text(anchor: Any, text: Any) -> bool:
    try:
        return not uno_same(anchor.getText(), text)
    except Exception:
        return False


def _classify_span_conflicts(
    view: RedlineView,
    text: Any,
    span: Any,
    target_token: str | None,
    strict_sibling_edges: bool = False,
    sibling_tokens: set[str] | None = None,
) -> tuple[bool, bool]:
    """Classify redline rows against span. Returns (has_foreign_conflict, has_sibling_conflict)."""
    if not view.reliable:
        return True, False
    has_sibling = False

    def mark(is_sibling: bool, row: RedlineRow, record: bool = False) -> bool:
        nonlocal has_sibling
        if is_sibling:
            has_sibling = True
            if record and sibling_tokens is not None and row.token is not None:
                sibling_tokens.add(row.token)
            return False
        return True

    for r in view.rows:
        if r.token is not None and r.token == target_token:
            continue
        is_sibling = bool(r.token is not None)
        if r.start is None or r.end is None:
            if mark(is_sibling, r):
                return True, False
            continue
        try:
            rl_span = text.createTextCursorByRange(r.start)
            rl_span.gotoRange(r.end, True)
        except Exception:
            if _in_other_text(r.start, text):
                continue
            if mark(is_sibling, r):
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
            if mark(is_sibling, r):
                return True, False
            continue

        if not overlaps and not is_sibling:
            overlaps = _user_redline_whitespace_adjacent(text, span, rl_span)

        if overlaps:
            if mark(is_sibling, r, record=True):
                return True, False
    return False, has_sibling


def _refusal_for_span(
    view: RedlineView,
    text: Any,
    span: Any,
    target_token: str,
    refuse_sibling: bool,
    where: str,
    *,
    strict_sibling_edges: bool = False,
    sibling_tokens: set[str] | None = None,
) -> ResolveStatus | None:
    has_foreign, has_sibling = _classify_span_conflicts(
        view,
        text,
        span,
        target_token,
        strict_sibling_edges=strict_sibling_edges,
        sibling_tokens=sibling_tokens,
    )
    if has_foreign:
        log.debug("inline_review: refusing %s resolve of %s -- user redline conflict", where, target_token)
        return ResolveStatus.REFUSED_FOREIGN
    if refuse_sibling and has_sibling:
        log.debug("inline_review: refusing %s resolve of %s -- sibling conflict", where, target_token)
        return ResolveStatus.REFUSED_SIBLING
    return None


def _tracked_change_command(accept: bool, *, all_redlines: bool = False) -> str:
    if all_redlines:
        return ".uno:AcceptAllTrackedChanges" if accept else ".uno:RejectAllTrackedChanges"
    return ".uno:AcceptTrackedChange" if accept else ".uno:RejectTrackedChange"


def _execute_dispatch(ctx: Any, controller: Any, command: str) -> None:
    smgr = ctx.getServiceManager()
    dispatcher = smgr.createInstanceWithContext("com.sun.star.frame.DispatchHelper", ctx)
    dispatcher.executeDispatch(controller.getFrame(), command, "", 0, ())


def _dispatch_and_scan(
    ctx: Any, controller: Any, model: Any, command: str, token: str
) -> tuple[RedlineView, bool]:
    try:
        _execute_dispatch(ctx, controller, command)
    except Exception:
        log.exception("inline_review: dispatch failed for %s", token)
        return scan_redline_view(model), True
    return scan_redline_view(model), False


def _token_delta(before: RedlineView, after: RedlineView) -> tuple[set[str], bool]:
    """Calculate (removed_agent_tokens, foreign_safe) between two snapshots."""
    # Tip: RedlineIdentifier reuses the SwRangeRedline object address across PopData
    # (sw/source/core/unocore/unoredline.cxx), so an identifier set can stay identical
    # across stacked pops. Compare counts of the full identity key (matching tracking._resolve_outcome).
    before_tokens = agent_tokens(before)
    after_tokens = agent_tokens(after)
    removed = before_tokens - after_tokens

    before_counts = Counter(r.identity for r in before.rows if r.token is None)
    after_counts = Counter(r.identity for r in after.rows if r.token is None)
    foreign_safe = after.reliable and all(after_counts[k] >= before_counts[k] for k in before_counts)
    return removed, foreign_safe


def resolve_agent_change(
    model: Any,
    ctx: Any,
    token: str,
    accept: bool,
    refuse_sibling_agent: bool = True,
    prefer_exact: bool = True,
    before: RedlineView | None = None,
    *,
    allow_paragraph_fallback: bool = True,
) -> tuple[ResolveResult, RedlineView]:
    """Accept/reject ONE agent change by token.

    Returns (ResolveResult, after_view).
    """
    if before is None:
        before = scan_redline_view(model)
    if not before.reliable:
        log.warning("inline_review: refusing resolve of %s -- pre-dispatch snapshot unreliable", token)
        return ResolveResult(ResolveStatus.UNRELIABLE), before

    if token not in agent_tokens(before):
        return ResolveResult(ResolveStatus.NOT_ELIGIBLE), before

    left, right = _build_bounds_from_view(before, token)
    if left is None or right is None:
        return ResolveResult(ResolveStatus.NOT_ELIGIBLE), before

    dispatched = False
    try:
        command = _tracked_change_command(accept)
        text = left.getText()
        controller = model.getCurrentController()
        view_cursor = controller.getViewCursor()

        # 1) Precise path: select EXACTLY this change's marks and dispatch -- resolves only it.
        if prefer_exact:
            exact = text.createTextCursorByRange(left)
            exact.gotoRange(right, True)
            strict_siblings: set[str] = set()
            refusal = _refusal_for_span(
                before,
                text,
                exact,
                token,
                refuse_sibling_agent,
                "inline",
                strict_sibling_edges=True,
                sibling_tokens=strict_siblings,
            )
            if refusal is not None:
                return ResolveResult(refusal), before

            view_cursor.gotoRange(left, False)
            view_cursor.gotoRange(right, True)
            dispatched = True
            after, failed = _dispatch_and_scan(
                ctx, controller, model, command, token
            )
            if failed:
                return ResolveResult(ResolveStatus.PARTIAL), after

            removed, foreign_safe = _token_delta(before, after)

            if token in removed and (removed - {token}) <= strict_siblings and after.reliable and foreign_safe:
                return ResolveResult(ResolveStatus.RESOLVED, len(removed)), after
            if removed or not after.reliable or not foreign_safe:
                log.warning("inline_review: exact resolve of %s had post-dispatch failure (removed=%s, reliable=%s, foreign_safe=%s)",
                            token, removed, after.reliable, foreign_safe)
                return ResolveResult(ResolveStatus.PARTIAL), after

            if not allow_paragraph_fallback:
                log.debug("inline_review: exact resolve of %s was no-op (fallback disabled)", token)
                return ResolveResult(ResolveStatus.NO_OP), after

            log.debug("inline_review: exact resolve of %s was no-op; widening to paragraph", token)

        # 2) Paragraph-wide path (older LibreOffice, or resolve-all): resolves everything in range,
        #    so refuse when a redline that we must not touch shares those paragraphs.
        start = text.createTextCursorByRange(left)
        start.gotoStartOfParagraph(False)
        end = text.createTextCursorByRange(right)
        end.gotoEndOfParagraph(True)
        para_span = text.createTextCursorByRange(start.getStart())
        para_span.gotoRange(end.getEnd(), True)

        refusal = _refusal_for_span(
            before,
            text,
            para_span,
            token,
            refuse_sibling_agent,
            "paragraph",
        )
        if refusal is not None:
            return ResolveResult(refusal), before

        view_cursor.gotoRange(start.getStart(), False)
        view_cursor.gotoRange(end.getEnd(), True)
        dispatched = True
        after, failed = _dispatch_and_scan(
            ctx, controller, model, command, token
        )
        if failed:
            return ResolveResult(ResolveStatus.PARTIAL), after

        after_tokens = agent_tokens(after)
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
def _grouped_resolve_undo(model: Any) -> Generator[None, None, None]:
    """Group dispatches into one undo step; keeps landed edits on failure."""
    um = None
    try:
        candidate = model.getUndoManager()
        if not candidate.isLocked():
            candidate.enterUndoContext(_("Resolve agent changes"))
            um = candidate
        else:
            log.warning("inline_review: undo manager is locked, undo grouping skipped")
    except Exception:
        log.warning("inline_review: could not access undo manager, undo grouping skipped", exc_info=True)
    try:
        yield
    finally:
        if um is not None:
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
    before_tokens = agent_tokens(before)
    if token not in before_tokens:
        return ResolveResult(ResolveStatus.NOT_ELIGIBLE, 0)

    token_bounds: dict[str, tuple[Any, Any]] = {}
    for tok in before_tokens:
        b_left, b_right = _build_bounds_from_view(before, tok)
        if b_left is None or b_right is None:
            return ResolveResult(ResolveStatus.NOT_ELIGIBLE, 0)
        token_bounds[tok] = (b_left, b_right)

    group = frozenset([token])

    dispatched = False
    has_foreign = False
    try:
        text = token_bounds[token][0].getText()
        while True:
            left, right = _build_bounds_from_view(before, group)
            if left is None or right is None:
                return ResolveResult(ResolveStatus.NOT_ELIGIBLE, 0)
            grew = False
            for other in sorted(before_tokens - group):
                o_left, o_right = token_bounds[other]
                try:
                    # Strict overlap (shared text, not just a touching edge): an independent
                    # agent change right next to this one is not resolved along with it.
                    # compareRegionStarts(a, b) == 1 means a starts before b.
                    overlaps = (text.compareRegionStarts(o_left, right) == 1
                                and text.compareRegionStarts(left, o_right) == 1)
                except Exception:
                    if _in_other_text(o_left, text):
                        continue
                    return ResolveResult(ResolveStatus.REFUSED_FOREIGN, 0)
                if overlaps:
                    group = group | {other}
                    grew = True
            if not grew:
                break

        if len(group) < 2:
            return ResolveResult(ResolveStatus.REFUSED_SIBLING, 0)

        span = text.createTextCursorByRange(left)
        span.gotoRange(right, True)
        has_foreign, _unused_sibling = _classify_span_conflicts(before, text, span, None)
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
                has_foreign, _unused_pass_sibling = _classify_span_conflicts(cur_view, text, pass_span, None)
                if has_foreign:
                    break
                view_cursor.gotoRange(left, False)
                view_cursor.gotoRange(right, True)
                try:
                    dispatched = True
                    command = _tracked_change_command(accept)
                    _execute_dispatch(ctx, controller, command)
                except Exception:
                    break
                now = scan_redline_view(model)
                cur_view = now
                if not now.reliable:
                    break
                now_tokens = agent_tokens(now)
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
    entry_tokens = agent_tokens(entry_view)
    total = len(entry_tokens)
    if total == 0:
        return ResolveAllResult(status=ResolveStatus.RESOLVED, resolved=0, total=0)

    if _is_all_redlines_agent(entry_view):
        try:
            controller = model.getCurrentController()
            command = _tracked_change_command(accept, all_redlines=True)
            _execute_dispatch(ctx, controller, command)
        except Exception:
            log.exception("inline_review: resolve-all global dispatch failed")
            return ResolveAllResult(status=ResolveStatus.PARTIAL, resolved=0, total=total)

        after = scan_redline_view(model)
        if not after.reliable:
            return ResolveAllResult(status=ResolveStatus.PARTIAL, resolved=0, total=total)
        after_tokens = agent_tokens(after)
        resolved = max(0, total - len(after_tokens))
        if not after_tokens:
            return ResolveAllResult(status=ResolveStatus.RESOLVED, resolved=total, total=total)
        if len(after_tokens) == total:
            return ResolveAllResult(status=ResolveStatus.NO_OP, resolved=0, total=total)
        return ResolveAllResult(status=ResolveStatus.PARTIAL, resolved=resolved, total=total)

    skip_reasons: dict[str, ResolveStatus] = {}
    dispatched = False
    cur_view = entry_view
    exact_unsupported = False

    with _grouped_resolve_undo(model):
        # Tip: no fixed cap. Each pass resolves the head change or adds it to `skip_reasons`, so pending
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
            cur_tokens = agent_tokens(cur_view)
            pending = sorted(t for t in cur_tokens if t not in skip_reasons)
            if not pending:
                break
            token = pending[0]
            before_count = len(cur_tokens)
            # Resolve-all wants sibling agent changes resolved together; only a user redline blocks it.
            res, next_view = resolve_agent_change(
                model, ctx, token, accept, refuse_sibling_agent=False, prefer_exact=False, before=cur_view
            )
            if res.status == ResolveStatus.REFUSED_FOREIGN and not exact_unsupported:
                # Tip: retry exact bounds; valid only while paragraph REFUSED_FOREIGN is pre-dispatch.
                exact_res, exact_view = resolve_agent_change(
                    model,
                    ctx,
                    token,
                    accept,
                    refuse_sibling_agent=False,
                    prefer_exact=True,
                    before=cur_view,
                    allow_paragraph_fallback=False,
                )
                if exact_res.status == ResolveStatus.NO_OP:
                    exact_unsupported = True
                else:
                    res, next_view = exact_res, exact_view
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
            after_tokens = agent_tokens(cur_view)
            if res.status != ResolveStatus.RESOLVED or len(after_tokens) >= before_count:
                # Tip: skip refused/failed/no-progress changes so the rest resolve and nothing spins.
                log.debug("inline_review: skipping %s in resolve-all (%s)", token, res.status)
                skip_reasons[token] = res.status

    final_tokens = agent_tokens(cur_view)
    # Tip: count tokens that disappeared; one dispatch can clear several siblings.
    resolved = max(0, total - len(final_tokens))

    if resolved == total:
        return ResolveAllResult(status=ResolveStatus.RESOLVED, resolved=total, total=total)
    if skip_reasons and all(s == ResolveStatus.REFUSED_FOREIGN for s in skip_reasons.values()):
        return ResolveAllResult(status=ResolveStatus.REFUSED_FOREIGN, resolved=resolved, total=total)
    if resolved == 0 and not any(s == ResolveStatus.REFUSED_FOREIGN for s in skip_reasons.values()):
        return ResolveAllResult(status=ResolveStatus.NO_OP, resolved=0, total=total)
    return ResolveAllResult(status=ResolveStatus.PARTIAL, resolved=resolved, total=total)
