# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for plugin.calc.python.undo."""

from __future__ import annotations

from unittest.mock import MagicMock
import pytest

from plugin.calc.python.undo import attach_to_newest_undo, undo_lock
from tests.uno_fakes import PyUnoSimulatedException


def test_undo_lock_enters_hidden_when_undo_possible() -> None:
    doc = MagicMock()
    um = MagicMock()
    um.isLocked.return_value = False
    um.isUndoPossible.return_value = True
    doc.getUndoManager.return_value = um

    with undo_lock(doc) as yielded_um:
        assert yielded_um is um
        um.enterHiddenUndoContext.assert_called_once()
        um.lock.assert_not_called()

    um.leaveUndoContext.assert_called_once()
    um.unlock.assert_not_called()


def test_undo_lock_locks_when_undo_not_possible() -> None:
    doc = MagicMock()
    um = MagicMock()
    um.isLocked.return_value = False
    um.isUndoPossible.return_value = False
    doc.getUndoManager.return_value = um

    with undo_lock(doc) as yielded_um:
        assert yielded_um is um
        um.lock.assert_called_once()
        um.enterHiddenUndoContext.assert_not_called()

    um.unlock.assert_called_once()
    um.leaveUndoContext.assert_not_called()


def test_undo_lock_without_hide_locks_a_nonempty_stack() -> None:
    doc = MagicMock()
    um = MagicMock()
    um.isLocked.return_value = False
    um.isUndoPossible.return_value = True
    doc.getUndoManager.return_value = um

    with undo_lock(doc, hide=False) as yielded_um:
        assert yielded_um is um
        um.lock.assert_called_once()
        um.enterHiddenUndoContext.assert_not_called()

    um.unlock.assert_called_once()
    um.leaveUndoContext.assert_not_called()


def test_undo_lock_leaves_a_locked_manager_alone() -> None:
    doc = MagicMock()
    um = MagicMock()
    um.isLocked.return_value = True
    um.isUndoPossible.return_value = True
    doc.getUndoManager.return_value = um

    with undo_lock(doc, hide=False) as yielded_um:
        assert yielded_um is um
        um.lock.assert_not_called()
        um.enterHiddenUndoContext.assert_not_called()

    um.unlock.assert_not_called()
    um.leaveUndoContext.assert_not_called()


def test_attach_to_newest_undo_skips_when_redo_is_pending() -> None:
    um = MagicMock()
    um.isLocked.return_value = False
    um.isRedoPossible.return_value = True

    attach_to_newest_undo(um, lambda: None)

    um.enterHiddenUndoContext.assert_not_called()
    um.addUndoAction.assert_not_called()


def test_undo_lock_propagates_uno_exception_unchanged() -> None:
    doc = MagicMock()
    um = MagicMock()
    doc.getUndoManager.return_value = um

    exc = PyUnoSimulatedException("DisposedException")
    with pytest.raises(PyUnoSimulatedException) as ei:
        with undo_lock(doc):
            raise exc

    assert ei.value is exc
