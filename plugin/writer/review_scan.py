# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Fail-closed redline enumeration and agent identity (token and author) helpers.

``edit_review`` owns tagging/session wait; ``inline_review`` owns accept/reject/goto.
This module is the shared scan + identity layer only — not tracking.py tools — plus
``RedlinesShown``, which both resolve paths (``inline_review`` and ``tracking_manage``) enter.
"""

from __future__ import annotations

import datetime
import logging
import re
from typing import Any, Callable

from plugin.doc.text_helpers import iter_elements_limited
from plugin.framework.errors import is_disposal
from plugin.writer.review_authors import AGENT_AUTHORS

log = logging.getLogger(__name__)

TOKEN_PREFIX = "wa-review:"
_TOKEN_RE = re.compile(r"\A" + re.escape(TOKEN_PREFIX) + r"[^\s:]+:\d+\Z")
_SAME_TURN = datetime.timedelta(seconds=10)  # covers one tool call's edits plus slack; untagged agent inserts with text are excluded by _stacked_on_someone_else


class RedlineScanAbort(Exception):
    """Stop ``scan_redlines`` immediately (enumeration can't continue safely)."""


def make_agent_token(session_id: str, change_index: int) -> str:
    """``wa-review:<session>:<n>``."""
    return "%s%s:%d" % (TOKEN_PREFIX, session_id, change_index)


def session_token_prefix(session_id: str) -> str:
    """``wa-review:<session>:``."""
    return "%s%s:" % (TOKEN_PREFIX, session_id)


def normalize_redline_comment(comment: Any) -> str | None:
    """Comment text with the trailing newline LibreOffice adds on ODF load removed.

    The token is saved as ``<text:p>wa-review:..</text:p>`` and LibreOffice reads it
    back as ``"wa-review:..\n"``. Without this strip the exact token regex rejects it,
    and a reopened document shows no agent changes.
    """
    if comment is None:
        return None
    return str(comment).rstrip("\r\n")


def is_agent_token(comment: str | None) -> bool:
    """True only for an exact ``wa-review:<session>:<n>`` token (no whitespace, no extra colons, numeric ``<n>``).

    A trailing newline from ODF load is ignored (see ``normalize_redline_comment``).
    """
    norm = normalize_redline_comment(comment)
    return norm is not None and _TOKEN_RE.match(norm) is not None


def agent_review_token(author: Any, comment: Any) -> str | None:
    """Normalized exact token when *author* is an agent author, else ``None``.

    A user's comment can be token-shaped, so a token counts only on an agent author's redline.
    """
    if author not in AGENT_AUTHORS or not is_agent_token(comment):
        return None
    return normalize_redline_comment(comment)


def read_redline_comment(redline: Any) -> tuple[str | None, bool]:
    """``(comment or None, readable)``."""
    try:
        raw = redline.getPropertyValue("RedlineComment")
    except Exception:
        return None, False
    return normalize_redline_comment(raw), True


def redline_is_agent_change(redline: Any) -> tuple[bool, bool]:
    """``(RedlineAuthor in AGENT_AUTHORS, author_readable)``. Fail-closed if unreadable.

    Checks author regardless of token so a DOCX-reloaded change, a style Format/ParagraphFormat
    redline, a table-only insert, and a deletion stacked on another author's insertion all count.
    """
    try:
        author = redline.getPropertyValue("RedlineAuthor")
    except Exception:
        return False, False
    return author in AGENT_AUTHORS, True


# RedlineDisplayType INSERTED_AND_REMOVED (offapi com/sun/star/document/RedlineDisplayType.idl).
_SHOW_INSERTED_AND_REMOVED = 2


class RedlinesShown:
    """Show deleted text (``RedlineDisplayType`` 2) for the ``with`` body, then put the document's own type back.

    Type 1 hides deletions and type 3 insertions in the model (``SwRangeRedline::Hide`` /
    ``ShowOriginal``, ``sw/source/core/doc/docredln.cxx``): a hidden redline has no end,
    so a selection over a replace covers only its visible half. Writer switches to type 2
    before it resolves anything (``DocumentRedlineManager::AcceptRedline`` /
    ``RejectRedline``, ``sw/source/core/doc/DocumentRedlineManager.cxx``), so a resolve that
    selected in hidden mode resolves only the half it selected. Redline positions and order
    change with the type (``SetRedlineFlags`` resorts the table), so read redlines again
    inside the body. ``changed`` is True when the body runs with a type the caller did not
    see. A class rather than ``@contextmanager``: see ``text_helpers.KeepViewSelection``.
    """

    def __init__(self, doc: Any) -> None:
        self._doc: Any = doc
        self._saved: Any = None
        self.changed: bool = False

    def __enter__(self) -> RedlinesShown:
        try:
            saved = self._doc.getPropertyValue("RedlineDisplayType")
            if saved != _SHOW_INSERTED_AND_REMOVED:
                self._doc.setPropertyValue("RedlineDisplayType", _SHOW_INSERTED_AND_REMOVED)
                self._saved = saved
                self.changed = True
        except Exception as exc:
            if not is_disposal(exc):
                raise
            # Closed since the caller's last scan: the scan in the body fails closed next, so a
            # resolve still reports "nothing was changed" instead of raising out of the menu action.
            log.debug("deleted text not shown: document closed")
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: Any,
    ) -> None:
        if not self.changed:
            return
        try:
            self._doc.setPropertyValue("RedlineDisplayType", self._saved)
        except Exception as restore_exc:
            if is_disposal(restore_exc):
                # Closed while resolving: there is no display left to restore.
                log.debug("RedlineDisplayType not restored: document closed")
                return
            log.exception("could not restore RedlineDisplayType")


def scan_redlines(doc: Any, on_item: Callable[[Any], bool]) -> tuple[bool, int, int]:
    """Fail-closed redline enumeration. Returns ``(reliable, seen, total)``.

    Calls ``on_item(rl)`` for each redline. Return True when the item was classified; False when it
    could not be (marks the scan unreliable but continues). Raise ``RedlineScanAbort`` to abort
    immediately (returns ``reliable=False``). Any other exception from ``on_item`` also aborts
    with ``reliable=False``.

    Also marks unreliable when ``seen != total``.
    """
    try:
        redlines = doc.getRedlines()
        total = int(redlines.getCount())
        enum = redlines.createEnumeration()
    except Exception:
        # Incomplete or dead doc: unreliable, NOT "zero pending / review complete".
        # EditReviewSession._pending_tokens probes document_is_disposed before calling this; do not fold dispose into this.
        return False, 0, 0
    reliable = True
    seen = 0
    # Cap iterations at getCount(), not hasMoreElements() alone: auto-mocked UNO enumerations
    # (pytest MagicMock) return a truthy hasMoreElements forever and would hang otherwise.
    try:
        for rl in iter_elements_limited(enum, total):
            seen += 1
            if not on_item(rl):
                reliable = False
    except Exception:
        # A per-redline read failed (for example the document closed mid-scan).
        # Fail closed in one place so callbacks can read properties directly.
        return False, seen, total
    if seen != total:
        reliable = False
    return reliable, seen, total


def document_has_tracked_deletions(doc: Any) -> bool:
    """True when *doc* holds at least one pending tracked deletion (best effort)."""
    found = False

    def on_item(rl: Any) -> bool:
        nonlocal found
        try:
            is_delete = rl.getPropertyValue("RedlineType") == "Delete"
        except Exception:
            return True  # one unreadable redline does not hide the others
        if is_delete:
            found = True
            raise RedlineScanAbort()
        return True

    scan_redlines(doc, on_item)
    return found


def agent_change_presence(
    model: Any, *, stop_at_first: bool = False, include_untagged: bool = False
) -> tuple[int, bool]:
    """Check for agent changes without building a full view. Returns ``(count, reliable)``.

    The toolbar and menu use the default (tagged agent changes only, skipping the author read
    when no token is present); the self-resolution guard passes ``include_untagged=True`` to
    count untagged agent-authored redlines as well.
    When ``stop_at_first`` is True, returns ``(1, True)`` on the first matching change seen
    without enumerating further. A full walk with no agent changes returns ``(0, True)``.
    Any enumeration or property read error returns ``(count_so_far, False)``.
    """
    tokens: set[str] = set()
    untagged = 0

    def on_item(rl: Any) -> bool:
        nonlocal untagged
        comment, readable = read_redline_comment(rl)
        if not readable:
            return False
        has_token = is_agent_token(comment)
        if not include_untagged and not has_token:
            return True
        author = rl.getPropertyValue("RedlineAuthor")
        token = agent_review_token(author, comment)
        if token is not None:
            tokens.add(token)
        elif include_untagged and author in AGENT_AUTHORS:
            untagged += 1
        else:
            return True
        if stop_at_first:
            raise RedlineScanAbort()
        return True

    reliable, _seen, _total = scan_redlines(model, on_item)
    if stop_at_first and (tokens or untagged):
        return 1, True
    return len(tokens) + untagged, reliable


def redline_date_key(dt: Any) -> tuple[Any, ...] | None:
    """Extract (Year, Month, Day, Hours, Minutes, Seconds, NanoSeconds) from RedlineDateTime."""
    if dt is None:
        return None
    return (dt.Year, dt.Month, dt.Day, dt.Hours, dt.Minutes, dt.Seconds, dt.NanoSeconds)


_UNREAD = object()


def redline_identity_key(
    rl: Any, *, strict_date: bool = False, comment: Any = _UNREAD, author: Any = _UNREAD
) -> tuple[Any, ...]:
    """``(RedlineIdentifier, type, author, date, comment)`` of one redline.

    Only the identifier must be readable (a failure raises, so the caller marks the scan
    unreliable); the other fields read as None when they fail.
    When strict_date is True, an unreadable RedlineDateTime raises so callers can fail the scan
    (no split-piece check without it).
    *comment* is the raw ``RedlineComment`` a caller has already read (``inline_review.scan_redline_view``
    reads it once to classify the row, and re-reading it would double the per-redline UNO reads
    on every scan); likewise *author*. When omitted, each is read here and is None if the read fails.
    tracking_manage uses this whole key, not ``RedlineIdentifier`` alone, because a
    stacked Delete reuses the identifier.
    """
    rid = rl.getPropertyValue("RedlineIdentifier")

    def prop(name: str) -> Any:
        try:
            return rl.getPropertyValue(name)
        except Exception:
            if strict_date and name == "RedlineDateTime":
                raise
            return None

    comment_val = prop("RedlineComment") if comment is _UNREAD else comment
    author_val = prop("RedlineAuthor") if author is _UNREAD else author
    return (
        rid,
        prop("RedlineType"),
        author_val,
        redline_date_key(prop("RedlineDateTime")),
        comment_val,
    )


def _stacked_on_someone_else(rl: Any) -> bool:
    """True when *rl* sits on top of another author's pending change (``RedlineSuccessorData``
    is the layer underneath) that is not a tagged agent change (token and agent author).

    Tagging such a stack would mark the user's layer too: the comment reads the top
    layer only, so 'Reject all agent changes' would pop the agent's Delete and then
    reject the user's Insert under it.
    """
    try:
        below = rl.getPropertyValue("RedlineSuccessorData")
    except Exception:
        # Unreadable layer: fail closed (treat as stacked, so the redline is not tagged).
        return True
    if not isinstance(below, (tuple, list)) or not below:
        return False
    fields = {p.Name: p.Value for p in below}
    return agent_review_token(fields.get("RedlineAuthor"), fields.get("RedlineComment")) is None


def _split_piece(
    key: tuple[Any, ...],
    before_sigs: set[tuple[Any, ...]],
    lo: tuple[int, ...],
    hi: tuple[int, ...],
) -> bool:
    """True when *key* is a tail piece split off an existing redline rather than a new edit.

    Without a date the split-piece test cannot tell a piece from a new redline.
    Redline dates have whole-second resolution (``SwRedlineData::SwRedlineData``,
    ``sw/source/core/doc/docredln.cxx``). An untagged agent redline dated within ``[lo, hi]``
    (a 10 s window up to ``now + 1 s``, bounding both past edits and future DOCX/clock skew)
    has the same signature as a new redline of this edit, so it is the edit's own. A year-0
    (undated) old redline compares below ``lo``, so it stays a piece. Another author's redline,
    a tagged piece, or an older/future untagged agent redline (for example from a reloaded DOCX)
    keeps the split-piece rule, so its text is never tagged as this change.
    """
    if key[3] is None or key[1:] not in before_sigs:
        return False
    if key[2] in AGENT_AUTHORS and not is_agent_token(key[4]):
        return not (lo <= tuple(key[3][:6]) <= hi)
    return True


def snapshot_redline_ids(doc: Any) -> tuple[set[Any], bool]:
    """``(set of current redline keys, reliable)`` — snapshot BEFORE an edit, for ``new_redlines_since``.

    ``reliable`` is False when the snapshot is incomplete. Callers must refuse to tag on an
    unreliable snapshot so a user redline is never stamped as an agent change.
    """
    keys: set[Any] = set()

    def on_item(rl: Any) -> bool:
        try:
            keys.add(redline_identity_key(rl, strict_date=True))
        except Exception:
            return False
        return True

    reliable = scan_redlines(doc, on_item)[0]
    return keys, reliable


def new_redlines_since(doc: Any, before: set[Any]) -> tuple[list[Any], bool]:
    """Redlines the edit made since the *before* snapshot (``snapshot_redline_ids``), plus scan reliability.

    "New" is not "RedlineIdentifier not seen before": the identifier
    is not a stable id. Deleting text that is another author's
    tracked insertion stacks our Delete on that SAME redline (same
    identifier), so the deletion is never tagged and never shows up
    as an agent change. Deleting inside the middle of
    someone else's insertion splits it, and the tail piece gets a
    NEW identifier, so the user's own text would be tagged as an
    agent change (Accept/Reject would then resolve it). A redline
    counts as old only if its whole key (identifier, type, author,
    date, comment) is unchanged, and a new-identifier redline with
    the same type, author, date and comment as an old one is a piece
    split off it, not ours (checked live: the piece keeps all four).
    The exception is an untagged agent-authored redline dated within
    the last 10 s (local time, matching ``GetSystemDateTime`` in
    ``tools/source/datetime/systemdatetime.cxx``), which belongs to
    the current edit rather than an older split piece.
    An agent change stacked on another author's pending change is
    left untagged (``_stacked_on_someone_else``): fail closed, it
    reads as the user's and is never resolved in bulk.
    """
    before_sigs = {key[1:] for key in before}
    now = datetime.datetime.now()
    lo = (now - _SAME_TURN).timetuple()[:6]
    hi = (now + datetime.timedelta(seconds=1)).timetuple()[:6]
    out: list[Any] = []

    def on_item(rl: Any) -> bool:
        try:
            key = redline_identity_key(rl, strict_date=True)
        except Exception:
            return False
        if key not in before and not _split_piece(key, before_sigs, lo, hi) and not _stacked_on_someone_else(rl):
            out.append(rl)
        return True

    reliable = scan_redlines(doc, on_item)[0]
    return out, reliable
