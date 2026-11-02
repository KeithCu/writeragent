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

from plugin.framework.errors import reraise_if_disposed

log = logging.getLogger(__name__)


def _is_undo_locked(um: Any) -> bool:
    fn = getattr(um, "isLocked", None)
    if fn is None:
        return False
    return fn() is True


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
    leave_error: BaseException | None
    rollback_error: BaseException | None
    _titles_before: tuple[str, ...] | None

    def __init__(self, doc: Any, title: str, *, _warn: bool = True) -> None:
        self._title = title
        self._undo_manager = None
        self._open = False
        self.entered = False
        self.leave_error = None
        self.rollback_error = None
        self._titles_before = None
        try:
            if not hasattr(doc, "getUndoManager"):
                if _warn:
                    log.warning("WriterCompoundUndo: doc has no getUndoManager, undo grouping skipped (title=%r)", title)
                return
            um = doc.getUndoManager()
            if um is None:
                if _warn:
                    log.warning("WriterCompoundUndo: getUndoManager() returned None, undo grouping skipped (title=%r)", title)
                return
            if _is_undo_locked(um):
                if _warn:
                    log.warning("WriterCompoundUndo: undo manager is locked, undo grouping skipped (title=%r)", title)
                return
            try:
                self._titles_before = tuple(um.getAllUndoActionTitles() or ())
            except Exception as titles_e:
                reraise_if_disposed(titles_e)
                self._titles_before = None
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
            log.debug("WriterCompoundUndo: context entered %r", title)
        except Exception as e:
            reraise_if_disposed(e)
            if _warn:
                log.warning("WriterCompoundUndo: enterUndoContext failed, undo grouping disabled (title=%r): %s", title, e)

    @classmethod
    def try_enter(cls, doc: Any, title: str) -> WriterCompoundUndo | None:
        inst = cls(doc, title, _warn=False)
        return inst if inst.entered else None

    def __enter__(self) -> WriterCompoundUndo:
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
            reraise_if_disposed(e)
            self.leave_error = e
            # Keep _open so a later close() can retry; clearing it leaves the context open.
            log.exception("leaveUndoContext failed (title=%r)", self._title)
            return False
        self._open = False
        return True

    def rollback_if_top(self) -> bool:
        """Roll back the undo context if it is the top action on the undo stack."""
        if not self.entered or self._open or self._titles_before is None or self._undo_manager is None:
            return False
        try:
            raw = self._undo_manager.getAllUndoActionTitles()
            after = tuple(raw or ())
            if after != self._titles_before and after and after[0] == self._title:
                self._undo_manager.undo()
                return True
            return False
        except Exception as e:
            reraise_if_disposed(e)
            self.rollback_error = e
            log.exception("rollback_if_top failed (title=%r)", self._title)
            return False
