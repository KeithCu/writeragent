# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2024 John Balis
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Undo manager lock helpers for Calc Python operations."""

from __future__ import annotations

from typing import Any, Literal, final

from plugin.framework.errors import best_effort, uno_read
from plugin.framework.thread_guard import unwrap_uno


@final
class undo_lock:
    """Temporarily hide or lock undo recording during background spill operations
    (named lowercase for call-site compatibility).

    If an undo action exists (e.g. user just typed =PY()), enterHiddenUndoContext()
    hides the spill mutations under the formula's undo action so the spill does not
    create a separate undo step. If the undo stack is empty, um.lock() is used.

    Class form avoids exception masking: generator context managers assign
    ``__traceback__`` on exit, which pyuno routes into the UNO struct
    (cite: ``pyuno/source/module/uno.py:_uno_struct__setattr__``), converting in-flight
    exceptions into ``RuntimeException: Couldn't convert <traceback object> to a UNO type``.
    """

    def __init__(self, doc: Any) -> None:
        self._doc: Any = doc
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
                    if um.isUndoPossible():
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
