# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Custom undo action for Draw/Impress text shape edits.

Draw/Impress TextShape.setString does not push child undo actions like Calc cell.setString,
so XUndoManager.leaveUndoContext discards empty compound contexts. This module provides
a custom XUndoAction that captures original text and registers with the document's
UndoManager to produce one Ctrl+Z step for Edit Selection / Extend Selection.
"""

from __future__ import annotations

import logging
import typing


log = logging.getLogger(__name__)


def DrawTextEditUndoAction(text_objects: list[tuple[typing.Any, str]], final_texts: list[str], title: str):  # type: ignore[no-untyped-def]
    """Factory for custom undo action for Draw/Impress text shape edits.
    
    Returns an XUndoAction implementation that captures original text and registers with
    the document's UndoManager to produce one Ctrl+Z step.
    
    Args:
        text_objects: List of (text_obj, original_text) tuples
        final_texts: List of final text values after edits (filled during streaming)
        title: Undo action title
    """
    import unohelper
    from com.sun.star.document import XUndoAction
    
    class _DrawTextEditUndoActionImpl(unohelper.Base, XUndoAction):
        """Custom undo action for Draw/Impress text shape edits."""

        def __init__(self) -> None:
            self._text_objects: list[tuple[typing.Any, str]] = text_objects
            self._final_texts: list[str] = final_texts
            self._title: str = title

        @property  # type: ignore[misc]
        def Title(self) -> str:  # type: ignore[override]  # ty: ignore[invalid-property-type-override]  # pyright: ignore[reportIncompatibleMethodOverride]
            """XUndoAction.Title (readonly). Python UNO requires a property; getTitle() alone fails addUndoAction."""
            return self._title

        def undo(self) -> None:
            """Restore original text to all text objects."""
            for text_obj, original_text in self._text_objects:
                try:
                    text_obj.setString(original_text)
                except Exception:
                    log.debug("DrawTextEditUndoAction.undo: setString failed for one object", exc_info=True)

        def redo(self) -> None:
            """Re-apply final text to all text objects."""
            for i, (text_obj, _original) in enumerate(self._text_objects):
                if i < len(self._final_texts):
                    try:
                        text_obj.setString(self._final_texts[i])
                    except Exception:
                        log.debug("DrawTextEditUndoAction.redo: setString failed for one object", exc_info=True)
    
    return _DrawTextEditUndoActionImpl()


def register_draw_text_edit_undo(
    doc: typing.Any,
    compound: typing.Any,
    text_obj_originals: list[tuple[typing.Any, str]],
    final_texts: list[str],
    title: str,
) -> None:
    """Register :func:`DrawTextEditUndoAction` on the document undo manager.

    Call only while ``compound.entered`` is true (inside ``WriterCompoundUndo``).
    Raises on failure so callers do not mutate text without a real undo step.
    """
    if not compound.entered:
        return
    um = doc.getUndoManager()
    if um is None:
        raise RuntimeError("getUndoManager() returned None after enterUndoContext")
    um.addUndoAction(DrawTextEditUndoAction(text_obj_originals, final_texts, title))
