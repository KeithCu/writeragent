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
from dataclasses import dataclass
from typing import Any

from plugin.framework.errors import is_disposal, reraise_if_disposed

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


class UndoUnavailable(RuntimeError):
    """Raised when an undo manager cannot be entered or is locked."""


@dataclass(frozen=True)
class UndoOutcome:
    """Result of :func:`commit_or_rollback`.

    ``rolled_back`` is True only when ``rollback_if_top`` actually ran undo.
    ``nothing_landed`` compares the session change count to the pre-mutate snapshot (or is
    True when undo ran). Callers should read it only on failure paths (``error`` or
    ``mutate_error`` set); on success it can be True even when no rollback was needed.
    ``ROLLBACK_FAILED`` in ``error`` means undo itself failed after a failed mutate.
    Document disposal still raises from :func:`commit_or_rollback`; it is never stored
    on ``mutate_error``.
    """

    error: dict[str, Any] | None = None
    rolled_back: bool = False
    nothing_landed: bool = False
    mutate_error: Exception | None = None

    def raise_mutate_error(self) -> None:
        """Re-raise a failed ``mutate`` when the caller uses exception flow."""
        if self.mutate_error is not None:
            raise self.mutate_error


def commit_or_rollback(
    doc: Any,
    title: str,
    mutate: Any,
    *,
    session: Any | None = None,
) -> UndoOutcome:
    """Run mutate inside an undo context with title.

    Missing or locked undo manager raises UndoUnavailable.
    Rolls back on error dict or exception from mutate. Document disposal is re-raised
    immediately (not returned on the outcome). Other mutate exceptions are returned on
    ``UndoOutcome.mutate_error``.

    Optional ``session`` (duck-typed review session with ``changes`` and
    ``discard_changes_since``) lets Writer paths drop review records after rollback
    without importing ``edit_review`` here. The snapshot is ``len(session.changes)`` taken
    before ``mutate()`` runs.

    ``mutate`` returns an error dict or None; any dict is treated as an error.
    """
    group = WriterCompoundUndo.try_enter(doc, title)
    if group is None:
        raise UndoUnavailable("Undo manager is missing, locked, or could not be entered.")

    error: dict[str, Any] | None = None
    exc_cause: Exception | None = None
    rolled_back = False
    changes_snapshot: int | None = None
    if session is not None:
        changes_snapshot = len(session.changes)
    try:
        res = mutate()
        if isinstance(res, dict):
            error = res
    except Exception as exc:
        exc_cause = exc
    finally:
        left = False
        try:
            left = group.close()
        except Exception as exc:
            if is_disposal(exc):
                exc_cause = exc
        if not left and error is None and exc_cause is None and group.leave_error is not None:
            error = {
                "status": "error",
                "code": "UNDO_CONTEXT_LEAK",
                "message": f"Could not close the undo context; the document may stay in one undo group ({group.leave_error}).",
            }
        failed = error is not None or exc_cause is not None
        if failed and left:
            undone = group.rollback_if_top()
            if undone:
                rolled_back = True
                if session is not None and changes_snapshot is not None:
                    # Same rule as the old on_fail callback: discard review records only when
                    # undo actually ran, so a partial edit still in the document keeps them.
                    try:
                        session.discard_changes_since(changes_snapshot)
                    except Exception:
                        log.debug("commit_or_rollback: session discard failed", exc_info=True)
            if group.rollback_error is not None:
                # Keep the original cause and use a code distinct from a clean rollback.
                cause = str(error.get("message")) if error and error.get("message") else str(exc_cause)
                error = {
                    "status": "error",
                    "code": "ROLLBACK_FAILED",
                    "message": f"Rollback failed; the document may be only partly edited ({group.rollback_error}). Cause: {cause}",
                }
                exc_cause = None

    nothing_landed = rolled_back or (
        session is not None and len(session.changes) == changes_snapshot
    )

    if exc_cause is not None:
        reraise_if_disposed(exc_cause)
        return UndoOutcome(
            error=error,
            rolled_back=rolled_back,
            nothing_landed=nothing_landed,
            mutate_error=exc_cause,
        )
    return UndoOutcome(error=error, rolled_back=rolled_back, nothing_landed=nothing_landed)
