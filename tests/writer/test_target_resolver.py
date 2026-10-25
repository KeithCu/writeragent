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


def test_search_hit_returns_cursor_in_match_text(monkeypatch):
    ctx, _ = _ctx()
    found = MagicMock()
    monkeypatch.setattr("plugin.writer.target_resolver.search_mod.find_first_range", lambda doc, s: found)
    res = resolve_target_cursor(ctx, "search", "needle")
    match_cursor = found.getText.return_value.createTextCursorByRange.return_value
    assert res is match_cursor
    match_cursor.gotoRange.assert_called_once_with(found.getEnd(), True)


def test_search_miss_uses_generic_hint(monkeypatch):
    ctx, _ = _ctx()
    monkeypatch.setattr("plugin.writer.target_resolver.search_mod.find_first_range", lambda doc, s: None)
    monkeypatch.setattr("plugin.writer.target_resolver.search_mod.drawing_shape_containing", lambda doc, s: None)
    with pytest.raises(ValueError, match="old_content not found in document") as exc_info:
        resolve_target_cursor(ctx, "search", "needle")
    assert "shorter, unique substring" in str(exc_info.value)


def test_search_miss_inside_shape_names_the_shape(monkeypatch):
    ctx, _ = _ctx()
    monkeypatch.setattr("plugin.writer.target_resolver.search_mod.find_first_range", lambda doc, s: None)
    monkeypatch.setattr("plugin.writer.target_resolver.search_mod.drawing_shape_containing", lambda doc, s: "Frame 1")
    with pytest.raises(ValueError) as exc_info:
        resolve_target_cursor(ctx, "search", "needle")
    err_msg = str(exc_info.value)
    assert "drawing shape" in err_msg
    assert "Frame 1" in err_msg


def test_search_converts_markup_before_find(monkeypatch):
    ctx, _ = _ctx()
    captured = {}
    found = MagicMock()

    def fake_find(doc, search_string):
        captured["search_string"] = search_string
        return found

    monkeypatch.setattr("plugin.writer.target_resolver.plain_text_if_markup", lambda text, uno_ctx, config_svc: "plain needle")
    monkeypatch.setattr("plugin.writer.target_resolver.search_mod.find_first_range", fake_find)
    resolve_target_cursor(ctx, "search", "<b>needle</b>")
    assert captured.get("search_string") == "plain needle"


def test_search_whitespace_only_old_content_errors(monkeypatch):
    ctx, _ = _ctx()
    mock_find = MagicMock()
    monkeypatch.setattr("plugin.writer.target_resolver.search_mod.find_first_range", mock_find)
    with pytest.raises(ValueError, match="old_content is empty after normalization"):
        resolve_target_cursor(ctx, "search", "\u00a0 \t ")
    mock_find.assert_not_called()


def test_search_none_old_content_errors(monkeypatch):
    ctx, _ = _ctx()
    mock_find = MagicMock()
    monkeypatch.setattr("plugin.writer.target_resolver.search_mod.find_first_range", mock_find)
    with pytest.raises(ValueError, match="old_content is empty after normalization"):
        resolve_target_cursor(ctx, "search", None)
    mock_find.assert_not_called()


def test_search_span_failure_becomes_value_error(monkeypatch):
    ctx, _ = _ctx()
    found = MagicMock()
    found.getText.return_value.createTextCursorByRange.side_effect = RuntimeError("no span")
    monkeypatch.setattr("plugin.writer.target_resolver.search_mod.find_first_range", lambda doc, s: found)
    with pytest.raises(ValueError, match="Could not span the search match"):
        resolve_target_cursor(ctx, "search", "needle")


def test_search_span_disposed_raises_document_disposed(monkeypatch):
    from plugin.framework.errors import DocumentDisposedError

    class DisposedException(Exception):
        pass

    ctx, _ = _ctx()
    found = MagicMock()
    found.getText.return_value.createTextCursorByRange.side_effect = DisposedException("closed")
    monkeypatch.setattr("plugin.writer.target_resolver.search_mod.find_first_range", lambda doc, s: found)
    with pytest.raises(DocumentDisposedError):
        resolve_target_cursor(ctx, "search", "needle")
