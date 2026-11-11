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


def _with_doc_key(doc: MagicMock, uid: str):
    from unittest.mock import patch

    return patch("plugin.doc.agent_undo.compute_doc_key", return_value=f"uid:{uid}")


def test_record_and_undo_single_step():
    doc = _doc(["Old"])
    with _with_doc_key(doc, "a"):
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
    with _with_doc_key(doc, "b"):
        au.record_agent_turn_before(doc)
        doc.getUndoManager.return_value._titles = ["Agent", "Old"]
        au.record_agent_turn_after(doc)
        doc.getUndoManager.return_value._titles = ["User", "Agent", "Old"]
        msg = au.undo_last_agent_edit(doc)
        assert "edited since" in msg
        assert doc.getUndoManager.return_value.undo_calls == 0


def test_skips_record_when_stack_unchanged():
    doc = _doc(["Only"])
    with _with_doc_key(doc, "c"):
        au.record_agent_turn_before(doc)
        au.record_agent_turn_after(doc)
        assert au.undo_last_agent_edit(doc) == "There is no agent edit to undo."


def test_clear_record():
    doc = _doc([])
    with _with_doc_key(doc, "d"):
        au.record_agent_turn_before(doc)
        doc.getUndoManager.return_value._titles = ["Agent"]
        au.record_agent_turn_after(doc)
        au.clear_agent_undo_record(doc)
        assert au.undo_last_agent_edit(doc) == "There is no agent edit to undo."


def test_dict_keyed_by_doc_key_not_object_identity():
    um = FakeUndoManager(["Old"])
    doc_a = MagicMock()
    doc_b = MagicMock()
    doc_a.getUndoManager.return_value = um
    doc_b.getUndoManager.return_value = um
    with _with_doc_key(doc_a, "same"):
        au.record_agent_turn_before(doc_a)
        um._titles = ["Agent", "Old"]
        au.record_agent_turn_after(doc_b)
        msg = au.undo_last_agent_edit(doc_a)
        assert "1 steps" in msg


def test_cancel_pending_agent_turn():
    doc = _doc(["Only"])
    with _with_doc_key(doc, "e"):
        au.record_agent_turn_before(doc)
        au.cancel_pending_agent_turn(doc)
        au.record_agent_turn_after(doc)
        assert au.undo_last_agent_edit(doc) == "There is no agent edit to undo."


def test_unreadable_titles_record_nothing():
    doc = MagicMock()
    doc.getUndoManager.return_value = None
    with _with_doc_key(doc, "unreadable"):
        au.record_agent_turn_before(doc)
        au.record_agent_turn_after(doc)
        assert au.undo_last_agent_edit(doc) == "There is no agent edit to undo."
