# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Compound undo context helper for grouping document mutations into a single undo step.

LibrePy and WriterAgent both use this to wrap document modifications into one Ctrl+Z step
without depending on ``edit_review.py``.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)


class WriterCompoundUndo:
    """Wrap ``XUndoManager.enterUndoContext`` / ``leaveUndoContext`` for one Ctrl+Z step.

    Prefer ``with WriterCompoundUndo(doc, title):``. Explicit :meth:`close` remains
    for streamed sessions that open in ``__init__`` and finish later. Safe to call
    ``close`` multiple times.

    Known limitation: the streamed sessions keep this undo context open for the whole
    stream, so a keystroke or other edit the user makes in the document while the agent
    is streaming lands in the agent's undo step (one Ctrl+Z reverts both). We deliberately
    do NOT block document input while streaming; closing the context per batch instead
    would split the agent edit into many undo steps.
    """

    _title: str
    _undo_manager: Any | None
    _open: bool
    entered: bool

    def __init__(self, doc: Any, title: str) -> None:
        self._title = title
        self._undo_manager = None
        self._open = False
        self.entered = False
        try:
            if not hasattr(doc, "getUndoManager"):
                log.warning("WriterCompoundUndo: doc has no getUndoManager, undo grouping skipped (title=%r)", title)
                return
            um = doc.getUndoManager()
            if um is None:
                log.warning("WriterCompoundUndo: getUndoManager() returned None, undo grouping skipped (title=%r)", title)
                return
            # Bugfix: what was wrong: enterUndoContext on a locked XUndoManager silently failed,
            # but left entered=True / _open=True, causing mismatched leave and inaccurate status.
            # How it happened: XUndoManager ignores enterUndoContext when locked without raising.
            # Why this change fixes it: checks isLocked() upfront and aborts grouping early.
            if hasattr(um, "isLocked") and um.isLocked():
                log.warning("WriterCompoundUndo: undo manager is locked, undo grouping skipped (title=%r)", title)
                return
            # Probe undo manager state to detect prior unclosed contexts (best-effort; UNO may not expose these).
            try:
                is_in_ctx = um.isInContext()
                undo_enabled = um.isUndoEnabled()
                log.debug("WriterCompoundUndo: pre-enter state isInContext=%s isUndoEnabled=%s (title=%r)", is_in_ctx, undo_enabled, title)
            except Exception as probe_e:
                log.debug("WriterCompoundUndo: could not probe undo manager state: %s", probe_e)
            um.enterUndoContext(title)
            self._undo_manager = um
            self._open = True
            self.entered = True
            # Log after success so we always see this when the context is live.
            log.debug("WriterCompoundUndo: context entered %r", title)
        except Exception as e:
            from plugin.framework.errors import reraise_if_disposed

            # Bugfix: what was wrong: enterUndoContext swallowed DisposedException behind warning log.
            # How it happened: broad except Exception caught DocumentDisposedError/DisposedException.
            # Why this change fixes it: re-raises disposed exceptions per project error policy.
            reraise_if_disposed(e)
            # Upgrade from debug to warning so failures are visible without debug logging.
            # "Insert $1" in the undo menu means this context was never opened.
            log.warning("WriterCompoundUndo: enterUndoContext failed, undo grouping disabled (title=%r): %s", title, e)

    def __enter__(self) -> "WriterCompoundUndo":
        return self

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: Any) -> None:
        # Do not swallow exceptions; only close the undo group.
        self.close()

    def close(self) -> bool:
        """End the compound undo context. True when no context remains open."""
        if not self._open:
            log.debug("WriterCompoundUndo.close: already closed or never opened (title=%r)", self._title)
            return True
        um = self._undo_manager
        if um is None:
            self._open = False
            return True
        try:
            log.debug("WriterCompoundUndo: leaving context %r", self._title)
            um.leaveUndoContext()
        except Exception as e:
            from plugin.framework.errors import reraise_if_disposed

            # Bugfix: what was wrong: leaveUndoContext swallowed DisposedException behind error log.
            # How it happened: broad except Exception caught DocumentDisposedError/DisposedException.
            # Why this change fixes it: re-raises disposed exceptions per project error policy.
            reraise_if_disposed(e)
            # Keep _open/_undo_manager so a later close() can retry; clearing them
            # here left the undo context open for the rest of the session.
            log.exception("leaveUndoContext failed (title=%r)", self._title)
            return False
        self._open = False
        self._undo_manager = None
        return True
