# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Split authoring for agent tracked changes: insertions and deletions get DIFFERENT authors
so LibreOffice's "show changes by author" coloring shows them in two distinct colors.

A redline's author is captured at creation time and is READ-ONLY afterward (setting
RedlineAuthor on an existing redline is a silent no-op), so the only way to author a replace's
Insert and Delete differently is to swap the office author BETWEEN the deletion and the
insertion. EditReviewSession makes the INSERT author the default for the whole edit via
``begin()``; each replace primitive wraps its ``setString("")`` deletion in ``deletion_author()``,
which flips the office author to the DELETE author for that moment and back. All of this runs
synchronously on the main thread, so the brief intermediate author state is never observed.

The actual colors are LibreOffice's automatic per-author choice (we cannot pick them) -- the two
authors just guarantee two distinct colors. Inert (no swaps) unless ``begin()`` armed this thread.
"""

from __future__ import annotations

import contextlib
import logging
import threading
from typing import Any

log = logging.getLogger(__name__)

# Distinct authors -> distinct by-author colors. Both read as the agent in the Manage dialog.
INSERT_AUTHOR = "WriterAgent"
DELETE_AUTHOR = "WriterAgent (deletions)"

_state = threading.local()


def _author_access(ctx: Any) -> Any:
    from com.sun.star.beans import NamedValue

    smgr = ctx.getServiceManager()
    provider = smgr.createInstanceWithContext("com.sun.star.configuration.ConfigurationProvider", ctx)
    node = NamedValue()
    node.Name = "nodepath"
    node.Value = "/org.openoffice.UserProfile/Data"
    return provider.createInstanceWithArguments(
        "com.sun.star.configuration.ConfigurationUpdateAccess", (node,))


def _set_office_author(ctx: Any, given: str) -> bool:
    try:
        access = _author_access(ctx)
        access.setPropertyValue("givenname", given)
        access.setPropertyValue("sn", "")
        access.commitChanges()
        return True
    except Exception:
        log.debug("review_authors: could not set office author", exc_info=True)
        return False


def begin(ctx: Any, insert_author: str = INSERT_AUTHOR, delete_author: str = DELETE_AUTHOR):
    """Capture the prior office author, set the INSERT author as the default, and arm
    ``deletion_author()`` on this thread. Returns the prior ``(given, sn)`` for ``end()``, or None.
    """
    prior = None
    try:
        access = _author_access(ctx)
        prior = (str(access.getPropertyValue("givenname")), str(access.getPropertyValue("sn")))
        access.setPropertyValue("givenname", insert_author)
        access.setPropertyValue("sn", "")
        access.commitChanges()
    except Exception:
        log.debug("review_authors.begin failed", exc_info=True)
        prior = None
    _state.ctx = ctx
    _state.insert = insert_author
    _state.delete = delete_author
    return prior


def end(ctx: Any, prior) -> None:
    """Disarm split authoring and restore the prior office author."""
    _state.ctx = None
    _state.insert = None
    _state.delete = None
    if prior is None:
        return
    try:
        access = _author_access(ctx)
        access.setPropertyValue("givenname", prior[0])
        access.setPropertyValue("sn", prior[1])
        access.commitChanges()
    except Exception:
        log.warning("review_authors.end: failed to restore office author %r", prior, exc_info=True)


@contextlib.contextmanager
def deletion_author():
    """Author the tracked deletion inside this block as the DELETE author, then restore the
    INSERT author. A no-op unless ``begin()`` armed split authoring on this thread."""
    ctx = getattr(_state, "ctx", None)
    delete = getattr(_state, "delete", None)
    insert = getattr(_state, "insert", None)
    if ctx is None or not delete:
        yield
        return
    if not _set_office_author(ctx, delete):
        yield
        return
    try:
        yield
    finally:
        _set_office_author(ctx, insert or "")
