# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for resolve_target_cursor."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from plugin.writer.target_resolver import resolve_target_cursor


def _ctx():
    text = MagicMock()
    cursor = MagicMock()
    text.createTextCursor.return_value = cursor
    doc = MagicMock()
    doc.getText.return_value = text
    return SimpleNamespace(doc=doc, ctx=MagicMock(), services=MagicMock()), cursor


def test_resolve_target_cursor_unknown_target_raises():
    ctx, _ = _ctx()
    with pytest.raises(ValueError, match=r"Unknown target 'start'; use end, selection, beginning, full_document or search\."):
        resolve_target_cursor(ctx, "start", "some text")


def test_resolve_target_cursor_valid_targets():
    ctx, cursor = _ctx()

    c_end = resolve_target_cursor(ctx, "end", None)
    assert c_end is cursor
    cursor.gotoEnd.assert_called_with(False)

    cursor.reset_mock()
    c_beg = resolve_target_cursor(ctx, "beginning", None)
    assert c_beg is cursor
    cursor.gotoStart.assert_called_with(False)

    cursor.reset_mock()
    c_full = resolve_target_cursor(ctx, "full_document", None)
    assert c_full is cursor
    cursor.gotoStart.assert_called_with(False)
    cursor.gotoEnd.assert_called_with(True)
