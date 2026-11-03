# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for scratch Writer creation and cleanup helpers."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from plugin.framework.errors import DocumentDisposedError
from plugin.framework.scratch_writer import clear_writer_body, new_blank_writer


def _scratch_doc(text="", tables=(), frames=(), shapes=0):
    """A MagicMock Writer with real-looking containers (a bare MagicMock never empties)."""
    doc = MagicMock()
    body = MagicMock()
    current_text = [text]
    body.getString.side_effect = lambda: current_text[0]
    body.setString.side_effect = lambda s: current_text.__setitem__(0, s)
    doc.getText.return_value = body

    def _container(names):
        c = MagicMock()
        items = {n: MagicMock() for n in names}
        remaining = set(names)
        c.getElementNames.side_effect = lambda: list(remaining)
        c.hasByName.side_effect = lambda n: n in remaining
        c.getByName.side_effect = lambda n: items[n]
        c.getCount.side_effect = lambda: len(remaining)
        for name, item in items.items():
            item.dispose.side_effect = lambda n=name: remaining.discard(n)
        return c, items

    doc.getTextTables.return_value, tbls = _container(tables)
    doc.getTextFrames.return_value, frms = _container(frames)
    page = MagicMock()
    count = [shapes]
    page.getCount.side_effect = lambda: count[0]
    page.remove.side_effect = lambda _s: count.__setitem__(0, count[0] - 1)
    doc.getDrawPage.return_value = page
    return doc, body, tbls, frms, count


def test_new_blank_writer_returns_guarded_document():
    doc = MagicMock()
    desktop = MagicMock()
    desktop.loadComponentFromURL.return_value = doc
    sentinel = object()
    with (
        patch("plugin.framework.scratch_writer.get_desktop", return_value=desktop),
        patch("plugin.framework.scratch_writer.clear_writer_body"),
        patch("plugin.framework.thread_guard.guard_uno", return_value=sentinel) as guard,
    ):
        assert new_blank_writer(MagicMock()) is sentinel
    guard.assert_called_once_with(doc)


def test_new_blank_writer_returns_none_when_template_text_survives():
    doc = MagicMock()
    doc.getText.return_value.getString.return_value = "AO DOUTO JUIZO"
    desktop = MagicMock()
    desktop.loadComponentFromURL.return_value = doc
    with (
        patch("plugin.framework.scratch_writer.get_desktop", return_value=desktop),
        patch("plugin.framework.scratch_writer.clear_writer_body", return_value=False),
    ):
        assert new_blank_writer(MagicMock()) is None
        doc.close.assert_called_once_with(True)


def test_new_blank_writer_keeps_an_already_empty_body():
    doc = MagicMock()
    doc.getText.return_value.getString.return_value = ""
    desktop = MagicMock()
    desktop.loadComponentFromURL.return_value = doc
    sentinel = object()
    with (
        patch("plugin.framework.scratch_writer.get_desktop", return_value=desktop),
        patch("plugin.framework.scratch_writer.clear_writer_body", return_value=True),
        patch("plugin.framework.thread_guard.guard_uno", return_value=sentinel),
    ):
        assert new_blank_writer(MagicMock()) is sentinel


def test_clear_writer_body_empties_a_default_template_scratch_doc():
    """A scratch Writer must not carry the user's default template.

    Regression for the "ghost block": a firm whose default template is its
    petition model got that model's header glued into range reads and
    plain-text conversions, because the factory URL honours the template and
    the scratch body was never emptied.
    """
    doc, body, _, _, _ = _scratch_doc(text="AO DOUTO JUIZO DO XXXX\nParte autora: xxxxx")
    assert clear_writer_body(doc) is True
    body.setString.assert_called_with("")


def test_clear_writer_body_drops_a_letterhead_with_no_text():
    """An empty table and a page-anchored logo have an empty body string; testing the
    text alone left the table in the scratch doc and it came back in range reads."""
    doc, _, tbls, frms, count = _scratch_doc(text="", tables=("Table1",), frames=("Frame1",), shapes=2)
    assert clear_writer_body(doc) is True
    tbls["Table1"].dispose.assert_called_once()
    frms["Frame1"].dispose.assert_called_once()
    assert count[0] == 0


def test_clear_writer_body_leaves_an_already_empty_doc_alone():
    for empty in ("", "   \n  "):
        doc, _, _, _, _ = _scratch_doc(text=empty)
        assert clear_writer_body(doc) is True


def test_clear_writer_body_cannot_spin_on_a_page_that_never_empties():
    """A remove that silently fails must not loop forever on the main thread."""
    doc, _, _, _, _ = _scratch_doc()
    stuck = MagicMock()
    stuck.getCount.return_value = 3  # remove() never changes it
    doc.getDrawPage.return_value = stuck
    clear_writer_body(doc)
    assert stuck.remove.call_count == 3


def test_clear_writer_body_reraises_disposal():
    """A disposed scratch doc must not look like an empty one."""
    class DisposedException(Exception):
        pass

    doc = MagicMock()
    doc.getTextTables.side_effect = DisposedException("dead")
    with pytest.raises(DocumentDisposedError):
        clear_writer_body(doc)


def test_clear_writer_body_survives_a_hostile_doc():
    """Never let a scratch-buffer cleanup take down the caller."""
    doc = MagicMock()
    doc.getText.side_effect = RuntimeError("disposed")
    assert clear_writer_body(doc) is False
    assert clear_writer_body(None) is False


def test_new_blank_writer_returns_none_and_closes_when_body_unreadable():
    doc = MagicMock()
    doc.getText.return_value.getString.side_effect = Exception("failed")
    desktop = MagicMock()
    desktop.loadComponentFromURL.return_value = doc
    with (
        patch("plugin.framework.scratch_writer.get_desktop", return_value=desktop),
        patch("plugin.framework.scratch_writer.clear_writer_body", return_value=False),
    ):
        assert new_blank_writer(MagicMock()) is None
        doc.close.assert_called_once_with(True)


def test_clear_writer_body_continues_past_remove_failures():
    doc, _, _, _, _ = _scratch_doc()
    stuck = MagicMock()

    shape2 = MagicMock()
    shape2.name = 'fail'
    shape1 = MagicMock()
    shape1.name = 'success'
    shape0 = MagicMock()
    shape0.name = 'fail'

    count = [3]

    def mock_get_count():
        return count[0]

    stuck.getCount.side_effect = mock_get_count

    def mock_remove(shape):
        if shape.name == 'fail':
            raise Exception("Cannot remove")
        else:
            count[0] -= 1

    stuck.getByIndex.side_effect = lambda i: {2: shape2, 1: shape1, 0: shape0}[i]
    stuck.remove.side_effect = mock_remove

    doc.getDrawPage.return_value = stuck
    removed = clear_writer_body(doc)
    assert stuck.remove.call_count == 3
    assert count[0] == 2
    assert removed is False


def test_new_blank_writer_returns_none_when_table_removed_but_set_string_fails():
    doc, body, tbls, _, _ = _scratch_doc(text="template header", tables=("Table1",))
    body.setString.side_effect = RuntimeError("setString failed")
    assert clear_writer_body(doc) is False
    tbls["Table1"].dispose.assert_called_once()

    doc2, body2, _, _, _ = _scratch_doc(text="template header", tables=("Table1",))
    body2.setString.side_effect = RuntimeError("setString failed")
    desktop = MagicMock()
    desktop.loadComponentFromURL.return_value = doc2
    with patch("plugin.framework.scratch_writer.get_desktop", return_value=desktop):
        assert new_blank_writer(MagicMock()) is None
        doc2.close.assert_called_once_with(True)


def test_new_blank_writer_returns_none_if_load_fails():
    desktop = MagicMock()
    desktop.loadComponentFromURL.return_value = None
    with patch("plugin.framework.scratch_writer.get_desktop", return_value=desktop):
        assert new_blank_writer() is None
