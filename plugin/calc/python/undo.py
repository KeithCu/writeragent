# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2024 John Balis
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Undo manager lock helpers for Calc Python operations."""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING, Any, Literal, final

from plugin.framework.errors import best_effort, uno_read
from plugin.framework.queue_executor import main
from plugin.framework.thread_guard import unwrap_uno

if TYPE_CHECKING:
    from collections.abc import Callable


@final
class undo_lock:
    """Temporarily hide or lock undo recording during background spill or plot operations
    (lowercase because callers use it like a function: ``with undo_lock(doc):``).

    With ``hide=True`` (default), if an undo action exists (e.g. user just typed ``=PY()``),
    ``enterHiddenUndoContext()`` hides the mutations under the formula's undo action so they
    do not create a separate undo step; if the undo stack is empty, ``um.lock()`` is used.
    With ``hide=False``, ``um.lock()`` is always used when unlocked so nothing is recorded
    and the redo stack is left alone. If ``um.isLocked()`` is already true (e.g. during an
    active Undo/Redo), nothing is locked or unlocked.

    Class form avoids exception masking: generator context managers assign
    ``__traceback__`` on exit, which pyuno routes into the UNO struct
    (cite: ``pyuno/source/module/uno.py:_uno_struct__setattr__``), converting in-flight
    exceptions into ``RuntimeException: Couldn't convert <traceback object> to a UNO type``.
    """

    def __init__(self, doc: Any, *, hide: bool = True) -> None:
        self._doc: Any = doc
        self._hide: bool = hide
        self._um: Any = None
        self._hidden: bool = False
        self._locked: bool = False

    def __enter__(self) -> Any:
        raw_doc = unwrap_uno(self._doc)
        if hasattr(raw_doc, "getUndoManager"):
            um = uno_read(raw_doc.getUndoManager, None)
            if um is not None:
                self._um = um

                def _lock_um() -> None:
                    # Undo and Redo turn recording off through the same flag that unlock() turns on
                    # (SfxUndoManager::ImplUndo, LockGuard, svl/source/undo/undo.cxx), so locking here
                    # would re-enable recording in the middle of an Undo.
                    if um.isLocked():
                        return
                    if self._hide and um.isUndoPossible():
                        um.enterHiddenUndoContext()
                        self._hidden = True
                    else:
                        um.lock()
                        self._locked = True

                best_effort(_lock_um, "enterHiddenUndoContext/lock failed in undo_lock")
        return self._um

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: Any,
    ) -> Literal[False]:
        um = self._um
        if um is not None:
            if self._hidden:
                best_effort(um.leaveUndoContext, "leaveUndoContext failed")
            elif self._locked:
                best_effort(um.unlock, "UndoManager.unlock failed")
        return False


def _undo_callback_action(on_undone: Callable[[], None]) -> Any:
    # UNO is absent in unit tests; import unohelper/XUndoAction only when building an action.
    import unohelper
    from com.sun.star.document import XUndoAction

    class _UndoCallbackAction(unohelper.Base, XUndoAction):
        @property  # type: ignore[misc]
        def Title(self) -> str:  # type: ignore[override]  # ty: ignore[invalid-property-type-override]  # pyright: ignore[reportIncompatibleMethodOverride]
            # Never shown because the merged entry keeps its predecessor's title.
            return "Python plot"

        def undo(self) -> None:
            # A merged group is undone newest-first, so the cell still has its formula here.
            # Posting with force_queue=True defers the check until the whole Undo finishes.
            best_effort(
                partial(main.post, partial(best_effort, on_undone, "undo callback failed"), force_queue=True),
                "undo callback post failed",
            )

        def redo(self) -> None:
            pass

    return _UndoCallbackAction()


def attach_to_newest_undo(um: Any, on_undone: Callable[[], None]) -> None:
    """Merge *on_undone* into the newest undo entry without changing its title or count."""
    if um.isLocked() or um.isRedoPossible():
        return  # leaving a non-empty context clears the redo stack
    um.enterHiddenUndoContext()  # raises EmptyUndoStackException when there is no entry
    try:
        um.addUndoAction(_undo_callback_action(on_undone))
    finally:
        um.leaveUndoContext()
