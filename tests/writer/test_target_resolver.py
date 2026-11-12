# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for resolve_target_cursor."""

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from plugin.framework.tool import ToolBase
from plugin.writer.target_resolver import insert_at_target, resolve_target_cursor


def _ctx():
    text = MagicMock()
    cursor = MagicMock()
    text.createTextCursor.return_value = cursor
    doc = MagicMock()
    doc.getDrawPage.return_value = None
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
    with pytest.raises(ValueError, match="old_content not found in document") as exc_info:
        resolve_target_cursor(ctx, "search", "needle")
    assert "shorter, unique substring" in str(exc_info.value)


def test_search_miss_inside_shape_names_the_shape(monkeypatch):
    ctx, _ = _ctx()
    monkeypatch.setattr("plugin.writer.target_resolver.search_mod.find_first_range", lambda doc, s: None)
    monkeypatch.setattr("plugin.writer.search.drawing_shape_containing", lambda doc, s, **k: "Frame 1")
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

    monkeypatch.setattr("plugin.writer.target_resolver.search_mod.find_first_range", fake_find)
    resolve_target_cursor(ctx, "search", "<b>needle</b>")
    assert captured.get("search_string") == "needle"


def test_search_oversized_old_content_raises_value_error(monkeypatch):
    ctx, _ = _ctx()
    monkeypatch.setattr("plugin.writer.target_resolver.search_mod.find_first_range", lambda doc, s: None)
    monkeypatch.setattr("plugin.writer.target_resolver.search_mod.old_content_covers_most", lambda doc, n: True)
    with pytest.raises(ValueError, match="target='full_document'"):
        resolve_target_cursor(ctx, "search", "half or more of doc")


def test_search_miss_includes_closest_mismatch(monkeypatch):
    ctx, _ = _ctx()
    monkeypatch.setattr("plugin.writer.target_resolver.search_mod.find_first_range", lambda doc, s: None)
    monkeypatch.setattr(
        "plugin.writer.target_resolver.search_mod.search_closest_mismatch",
        lambda doc, s, lenient=True: {"message": "closest match starts 'needle…', differs at character 3: l vs x", "closest_match": "needle…", "diff_at": 3},
    )
    with pytest.raises(ValueError) as exc_info:
        resolve_target_cursor(ctx, "search", "needle")
    err_msg = str(exc_info.value)
    assert "old_content not found in document" in err_msg
    assert "closest match starts 'needle…', differs at character 3: l vs x" in err_msg


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


def test_resolve_target_cursor_case_sensitive_forwarding(monkeypatch):
    ctx, _ = _ctx()
    calls = []

    def fake_find(doc, search_string, *, case_sensitive=False):
        calls.append((search_string, case_sensitive))
        found = MagicMock()
        return found

    monkeypatch.setattr("plugin.writer.target_resolver.search_mod.find_first_range", fake_find)

    # Omitted case_sensitive defaults to False
    resolve_target_cursor(ctx, "search", "needle")
    assert calls[-1] == ("needle", False)

    # Explicit case_sensitive=True forwards True
    resolve_target_cursor(ctx, "search", "needle", case_sensitive=True)
    assert calls[-1] == ("needle", True)

    # Explicit case_sensitive=False forwards False
    resolve_target_cursor(ctx, "search", "needle", case_sensitive=False)
    assert calls[-1] == ("needle", False)


class _DummyTool(ToolBase):
    def execute(self, ctx: Any, **kwargs: Any) -> dict[str, Any]:
        return {}


def _insert_ctx():
    doc = MagicMock()
    undo_mgr = MagicMock()
    undo_mgr.isLocked.return_value = False
    entered: dict[str, str] = {}
    undo_mgr.enterUndoContext.side_effect = lambda title: entered.setdefault("title", title)
    undo_mgr.getAllUndoActionTitles.side_effect = lambda: (entered.get("title"),) if "title" in entered else ()
    doc.getUndoManager.return_value = undo_mgr
    ctx = SimpleNamespace(doc=doc, ctx=MagicMock(), services=MagicMock())
    return ctx, undo_mgr


def test_insert_at_target_search_deletes_then_inserts_inside_the_bracket(monkeypatch):
    ctx, undo_mgr = _insert_ctx()
    tool = _DummyTool()
    cursor = MagicMock()
    call_order: list[Any] = []
    undo_mgr.enterUndoContext.side_effect = lambda title: call_order.append(("enterUndoContext", title))
    undo_mgr.leaveUndoContext.side_effect = lambda: call_order.append("leaveUndoContext")
    cursor.setString.side_effect = lambda s: call_order.append(("setString", s))

    def fake_insert(c: Any) -> None:
        call_order.append(("insert", c))
        return None

    monkeypatch.setattr("plugin.writer.target_resolver.resolve_target_cursor", lambda *a, **k: cursor)

    res = insert_at_target(tool, ctx, {"target": "search", "old_content": "needle"}, fake_insert, what="the thing")
    assert res is None
    assert call_order[0][0] == "enterUndoContext"
    assert call_order[1] == ("setString", "")
    assert call_order[2] == ("insert", cursor)
    assert call_order[3] == "leaveUndoContext"


def test_insert_at_target_replace_match_false_keeps_the_match(monkeypatch):
    ctx, _ = _insert_ctx()
    tool = _DummyTool()
    cursor = MagicMock()
    inserted_cursors: list[Any] = []

    monkeypatch.setattr("plugin.writer.target_resolver.resolve_target_cursor", lambda *a, **k: cursor)

    def fake_insert(c: Any) -> None:
        inserted_cursors.append(c)
        return None

    res = insert_at_target(
        tool,
        ctx,
        {"target": "search", "old_content": "needle"},
        fake_insert,
        what="the thing",
        replace_match=False,
    )
    assert res is None
    cursor.setString.assert_not_called()
    assert inserted_cursors == [cursor]


def test_insert_at_target_non_search_is_one_undo_step(monkeypatch):
    ctx, undo_mgr = _insert_ctx()
    tool = _DummyTool()
    cursor = MagicMock()
    monkeypatch.setattr("plugin.writer.target_resolver.resolve_target_cursor", lambda *a, **k: cursor)

    # Default undo_title starts with "WriterAgent edit#"
    res = insert_at_target(tool, ctx, {"target": "end"}, lambda c: None, what="the thing")
    assert res is None
    cursor.setString.assert_not_called()
    undo_mgr.enterUndoContext.assert_called_once()
    assert str(undo_mgr.enterUndoContext.call_args[0][0]).startswith("WriterAgent edit#")

    # Explicit undo_title uses the provided string
    undo_mgr.reset_mock()
    res = insert_at_target(tool, ctx, {"target": "end"}, lambda c: None, what="the thing", undo_title="Custom Title")
    assert res is None
    cursor.setString.assert_not_called()
    undo_mgr.enterUndoContext.assert_called_once_with("Custom Title")


def test_insert_at_target_resolver_error_is_the_tool_error(monkeypatch):
    ctx, undo_mgr = _insert_ctx()
    tool = _DummyTool()
    monkeypatch.setattr("plugin.writer.target_resolver.resolve_target_cursor", MagicMock(side_effect=ValueError("nope")))

    res = insert_at_target(tool, ctx, {"target": "search", "old_content": "needle"}, lambda c: None, what="the thing")
    assert res == {"status": "error", "code": "TOOL_EXECUTION_ERROR", "message": "nope"}
    undo_mgr.enterUndoContext.assert_not_called()


def test_insert_at_target_insert_failure_rolls_back_with_code(monkeypatch):
    ctx, undo_mgr = _insert_ctx()
    tool = _DummyTool()
    cursor = MagicMock()
    monkeypatch.setattr("plugin.writer.target_resolver.resolve_target_cursor", lambda *a, **k: cursor)

    def bad_insert(c: Any) -> None:
        raise RuntimeError("boom")

    res = insert_at_target(tool, ctx, {"target": "end"}, bad_insert, what="the thing", code="INSERT_FRAME_FAILED")
    assert res is not None
    assert res["status"] == "error"
    assert res["code"] == "INSERT_FRAME_FAILED"
    assert res["message"] == "Failed to insert the thing; the change was rolled back (boom)."
    undo_mgr.undo.assert_called_once()


def test_insert_at_target_locked_undo_changes_nothing(monkeypatch):
    ctx, undo_mgr = _insert_ctx()
    undo_mgr.isLocked.return_value = True
    tool = _DummyTool()
    cursor = MagicMock()
    monkeypatch.setattr("plugin.writer.target_resolver.resolve_target_cursor", lambda *a, **k: cursor)

    res = insert_at_target(tool, ctx, {"target": "search", "old_content": "needle"}, lambda c: None, what="the thing")
    assert res is not None
    assert res["status"] == "error"
    assert res["code"] == "UNDO_UNAVAILABLE"
    assert "the document was not changed" in res["message"]
    cursor.setString.assert_not_called()


def test_insert_at_target_insert_error_dict_is_returned_after_rollback(monkeypatch):
    ctx, undo_mgr = _insert_ctx()
    tool = _DummyTool()
    cursor = MagicMock()
    monkeypatch.setattr("plugin.writer.target_resolver.resolve_target_cursor", lambda *a, **k: cursor)

    err_dict = {"status": "error", "code": "INVALID_PARAM", "message": "already exists"}
    res = insert_at_target(tool, ctx, {"target": "end"}, lambda c: err_dict, what="the thing")
    assert res == err_dict
    undo_mgr.undo.assert_called_once()


