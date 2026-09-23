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


def snapshot_redline_ids(doc: Any) -> tuple[set[Any], bool]:
    """``(set of current RedlineIdentifiers, reliable)`` — snapshot BEFORE an edit.

    ``reliable`` is False when the snapshot is incomplete. Callers must refuse to tag on an
    unreliable snapshot so a user redline is never stamped as an agent change.
    """
    ids: set[Any] = set()

    def on_item(rl: Any) -> bool:
        try:
            ids.add(rl.getPropertyValue("RedlineIdentifier"))
        except Exception:
            return False
        return True

    reliable = scan_redlines(doc, on_item)[0]
    return ids, reliable


def new_redlines_since(doc: Any, before_ids: set[Any]) -> tuple[list[Any], bool]:
    """Redlines whose ``RedlineIdentifier`` is not in *before_ids*, plus scan reliability."""
    out: list[Any] = []

    def on_item(rl: Any) -> bool:
        try:
            rid = rl.getPropertyValue("RedlineIdentifier")
        except Exception:
            return False
        if rid not in before_ids:
            out.append(rl)
        return True

    reliable = scan_redlines(doc, on_item)[0]
    return out, reliable
