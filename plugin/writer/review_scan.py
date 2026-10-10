# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Fail-closed redline enumeration and wa-review token helpers.

``edit_review`` owns tagging/session wait; ``inline_review`` owns accept/reject/goto.
This module is the shared scan + token layer only — not tracking.py tools.
"""

from __future__ import annotations

from typing import Any, Callable


TOKEN_PREFIX = "wa-review:"


class RedlineScanAbort(Exception):
    """Stop ``scan_redlines`` immediately (enumeration can't continue safely)."""


def make_agent_token(session_id: str, change_index: int) -> str:
    """``wa-review:<session>:<n>``."""
    return "%s%s:%d" % (TOKEN_PREFIX, session_id, change_index)


def session_token_prefix(session_id: str) -> str:
    """``wa-review:<session>:``."""
    return "%s%s:" % (TOKEN_PREFIX, session_id)


def is_agent_token(comment: str | None) -> bool:
    """True when *comment* is a non-empty string starting with ``TOKEN_PREFIX``."""
    return bool(comment) and str(comment).startswith(TOKEN_PREFIX)


def read_redline_comment(redline: Any) -> tuple[str | None, bool]:
    """``(comment or None, readable)``."""
    try:
        raw = redline.getPropertyValue("RedlineComment")
    except Exception:
        return None, False
    if raw is None:
        return None, True
    return str(raw), True


def redline_is_agent_change(redline: Any) -> tuple[bool, bool]:
    """``(is wa-review, comment_readable)``. Fail-closed if the comment is unreadable."""
    comment, readable = read_redline_comment(redline)
    if not readable:
        return False, False
    return is_agent_token(comment), True


def scan_redlines(doc: Any, on_item: Callable[[Any], bool]) -> tuple[bool, int, int]:
    """Fail-closed redline enumeration. Returns ``(reliable, seen, total)``.

    Calls ``on_item(rl)`` for each redline. Return True when the item was classified; False when it
    could not be (marks the scan unreliable but continues). Raise ``RedlineScanAbort`` to abort
    immediately (returns ``reliable=False``).

    Also marks unreliable when ``seen != total``.
    """
    try:
        redlines = doc.getRedlines()
        total = int(redlines.getCount())
        enum = redlines.createEnumeration()
    except Exception:
        # Incomplete or dead doc: unreliable, NOT "zero pending / review complete".
        # wait_for_review probes is_document_disposed separately; do not fold dispose into this.
        return False, 0, 0
    if total < 0:
        return False, 0, total
    reliable = True
    seen = 0
    # Cap iterations at getCount(), not hasMoreElements() alone: auto-mocked UNO enumerations
    # (pytest MagicMock) return a truthy hasMoreElements forever and would hang otherwise.
    while seen < total:
        try:
            if not enum.hasMoreElements():
                break
            rl = enum.nextElement()
        except Exception:
            return False, seen, total
        seen += 1
        try:
            if not on_item(rl):
                reliable = False
        except RedlineScanAbort:
            return False, seen, total
    if seen != total:
        reliable = False
    return reliable, seen, total


def _redline_key(rl: Any) -> tuple[Any, ...]:
    """``(RedlineIdentifier, type, author, date, comment)`` of one redline.

    Only the identifier must be readable (a failure raises, so the caller marks the scan
    unreliable); the other fields read as None when they fail.
    """
    rid = rl.getPropertyValue("RedlineIdentifier")

    def prop(name: str) -> Any:
        try:
            return rl.getPropertyValue(name)
        except Exception:
            return None

    dt = prop("RedlineDateTime")
    when = None if dt is None else tuple(
        getattr(dt, f, None) for f in ("Year", "Month", "Day", "Hours", "Minutes", "Seconds", "NanoSeconds"))
    return (rid, prop("RedlineType"), prop("RedlineAuthor"), when, prop("RedlineComment"))


def _stacked_on_someone_else(rl: Any) -> bool:
    """True when *rl* sits on top of another author's pending change (``RedlineSuccessorData``
    is the layer underneath) that is not an agent change.

    Tagging such a stack marks the user's layer as the agent's too: the comment reads the top
    layer only, so "Reject all agent changes" popped the agent's Delete and then rejected the
    user's own Insert under it -- the user's text was gone (checked live).
    """
    try:
        below = rl.getPropertyValue("RedlineSuccessorData")
    except Exception:
        return False
    if not isinstance(below, (tuple, list)) or not below:
        return False
    comment = next((p.Value for p in below if getattr(p, "Name", "") == "RedlineComment"), "")
    return not is_agent_token(comment)


def snapshot_redline_ids(doc: Any) -> tuple[set[Any], bool]:
    """``(set of current redline keys, reliable)`` — snapshot BEFORE an edit, for ``new_redlines_since``.

    ``reliable`` is False when the snapshot is incomplete. Callers must refuse to tag on an
    unreliable snapshot so a user redline is never stamped as an agent change.
    """
    keys: set[Any] = set()

    def on_item(rl: Any) -> bool:
        try:
            keys.add(_redline_key(rl))
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
    as an agent change (relato #34). Deleting inside the middle of
    someone else's insertion splits it, and the tail piece gets a
    NEW identifier, so the user's own text would be tagged as an
    agent change (Accept/Reject would then resolve it). A redline
    counts as old only if its whole key (identifier, type, author,
    date, comment) is unchanged, and a new-identifier redline with
    the same type, author, date and comment as an old one is a piece
    split off it, not ours (checked live: the piece keeps all four).
    An agent change stacked on another author's pending change is
    left untagged (``_stacked_on_someone_else``): fail closed, it
    reads as the user's and is never resolved in bulk.
    """
    before_sigs = {key[1:] for key in before}
    out: list[Any] = []

    def on_item(rl: Any) -> bool:
        try:
            key = _redline_key(rl)
        except Exception:
            return False
        # Without a date the split-piece test cannot tell a piece from a new redline.
        split_piece = key[3] is not None and key[1:] in before_sigs
        if key not in before and not split_piece and not _stacked_on_someone_else(rl):
            out.append(rl)
        return True

    reliable = scan_redlines(doc, on_item)[0]
    return out, reliable
