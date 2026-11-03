# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for agent undo snapshots (/undo)."""

from __future__ import annotations

from unittest.mock import MagicMock

from plugin.doc import agent_undo as au


class FakeUndoManager:
    def __init__(self, titles: list[str]) -> None:
        self._titles = list(titles)
        self.undo_calls = 0

    def getAllUndoActionTitles(self) -> list[str]:
        return list(self._titles)

    def undo(self) -> None:
        self.undo_calls += 1
        if self._titles:
            self._titles.pop(0)


def _doc(titles: list[str]) -> MagicMock:
    doc = MagicMock()
    doc.getUndoManager.return_value = FakeUndoManager(titles)
    return doc


def test_record_and_undo_single_step():
    doc = _doc(["Old"])
    au.record_agent_turn_before(doc)
    doc.getUndoManager.return_value._titles = ["Agent", "Old"]
    au.record_agent_turn_after(doc)
    msg = au.undo_last_agent_edit(doc)
    assert "1 steps" in msg
    assert doc.getUndoManager.return_value._titles == ["Old"]
    assert au.undo_last_agent_edit(doc) == "There is no agent edit to undo."


def test_compute_undo_steps_full_stack():
    before = ("a", "b", "c", "d", "e")
    after = ("new", "a", "b", "c", "d")
    assert au.compute_undo_steps(before, after) == 1
    after_full = ("x", "y") + before[:3]
    assert au.compute_undo_steps(before, after_full) == 2


def test_refuses_when_titles_changed_since_agent_edit():
    doc = _doc(["Old"])
    au.record_agent_turn_before(doc)
    doc.getUndoManager.return_value._titles = ["Agent", "Old"]
    au.record_agent_turn_after(doc)
    doc.getUndoManager.return_value._titles = ["User", "Agent", "Old"]
    msg = au.undo_last_agent_edit(doc)
    assert "edited since" in msg
    assert doc.getUndoManager.return_value.undo_calls == 0


def test_skips_record_when_stack_unchanged():
    doc = _doc(["Only"])
    au.record_agent_turn_before(doc)
    au.record_agent_turn_after(doc)
    assert au.undo_last_agent_edit(doc) == "There is no agent edit to undo."


def test_clear_record():
    doc = _doc([])
    au.record_agent_turn_before(doc)
    doc.getUndoManager.return_value._titles = ["Agent"]
    au.record_agent_turn_after(doc)
    au.clear_agent_undo_record(doc)
    assert au.undo_last_agent_edit(doc) == "There is no agent edit to undo."
