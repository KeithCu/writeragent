# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2024 John Balis
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Undo manager lock helpers for Calc Python operations."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Generator

from plugin.framework.errors import best_effort, uno_read
from plugin.framework.thread_guard import unwrap_uno


@contextmanager
def _undo_lock(doc: Any) -> Generator[Any, None, None]:
    """Temporarily hide or lock undo recording during background spill operations.

    If an undo action exists (e.g. user just typed =PY()), enterHiddenUndoContext()
    hides the spill mutations under the formula's undo action so the spill does not
    create a separate undo step. If the undo stack is empty, um.lock() is used.
    """
    um = None
    hidden = False
    locked = False
    raw_doc = unwrap_uno(doc)
    if hasattr(raw_doc, "getUndoManager"):
        um = uno_read(raw_doc.getUndoManager, None)
        if um is not None:

            def _lock_um() -> None:
                nonlocal hidden, locked
                if um.isUndoPossible():
                    um.enterHiddenUndoContext()
                    hidden = True
                else:
                    um.lock()
                    locked = True

            best_effort(_lock_um, "enterHiddenUndoContext/lock failed in _undo_lock")
    try:
        yield um
    finally:
        if um is not None:
            if hidden:
                best_effort(um.leaveUndoContext, "leaveUndoContext failed")
            elif locked:
                best_effort(um.unlock, "UndoManager.unlock failed")
