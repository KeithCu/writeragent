# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Selection: target='selection' must not silently append at the document end, and set_selection
lets a headless client pick a passage. No LibreOffice required."""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


from plugin.writer.selection import SetSelection
from plugin.writer.target_resolver import resolve_target_cursor


# ---- C1: target='selection' raises instead of silently going to end ----------

def _ctx_with_controller(controller):
    text = MagicMock()
    cursor = MagicMock()
    text.createTextCursor.return_value = cursor
    doc = MagicMock()
    doc.getText.return_value = text
    doc.getCurrentController.return_value = controller
    ctx = SimpleNamespace(doc=doc, ctx=MagicMock(), services={"config": MagicMock()})
    return ctx, cursor


def test_selection_raises_when_both_selection_and_viewcursor_fail_not_gotoEnd():
    controller = MagicMock()
    controller.getSelection.side_effect = RuntimeError("boom")
    controller.getViewCursor.side_effect = RuntimeError("no view")
    ctx, cursor = _ctx_with_controller(controller)
    with pytest.raises(ValueError) as ei:
        resolve_target_cursor(ctx, "selection", None)
    assert "set_selection" in str(ei.value) or "search" in str(ei.value)
    cursor.gotoEnd.assert_not_called()  # the bug was a silent gotoEnd(False)


def test_selection_falls_back_to_view_cursor_never_end():
    # A selection whose getCount blows up must not fail the call; fall to the view cursor
    # (spanned in ITS text object), and NEVER append at the document end.
    controller = MagicMock()
    bad_sel = MagicMock()
    bad_sel.getCount.side_effect = RuntimeError("unreadable selection")
    controller.getSelection.return_value = bad_sel
    vc_cursor = MagicMock()
    controller.getViewCursor.return_value.getText.return_value.createTextCursorByRange.return_value = vc_cursor
    ctx, body_cursor = _ctx_with_controller(controller)
    result = resolve_target_cursor(ctx, "selection", None)
    assert result is vc_cursor
    body_cursor.gotoEnd.assert_not_called()


def test_selection_uses_explicit_selection_range():
    rng = MagicMock()
    rng.getStart.return_value = MagicMock()
    rng.getEnd.return_value = MagicMock()
    rng.getText.return_value.compareRegionStarts.return_value = -1
    sel_cursor = MagicMock()
    rng.getText.return_value.createTextCursorByRange.return_value = sel_cursor
    sel = MagicMock()
    sel.getCount.return_value = 1
    sel.getByIndex.return_value = rng
    controller = MagicMock()
    controller.getSelection.return_value = sel
    ctx, body_cursor = _ctx_with_controller(controller)
    result = resolve_target_cursor(ctx, "selection", None)
    assert result is sel_cursor           # built in the selection's own text (cell/frame-safe)
    body_cursor.gotoEnd.assert_not_called()


def test_selection_skips_empty_ranges_on_multiple_selection():
    rng0 = MagicMock()
    rng0.getStart.return_value = MagicMock()
    rng0.getEnd.return_value = MagicMock()
    rng0.getText.return_value.compareRegionStarts.return_value = 0
    rng0.getString.return_value = ""
    rng1 = MagicMock()
    rng1.getStart.return_value = MagicMock()
    rng1.getEnd.return_value = MagicMock()
    rng1.getText.return_value.compareRegionStarts.return_value = -1
    rng1.getString.return_value = "highlighted"

    sel_cursor = MagicMock()
    rng1.getText.return_value.createTextCursorByRange.return_value = sel_cursor

    sel = MagicMock()
    sel.getCount.return_value = 2

    def by_index(i):
        if i == 0: return rng0
        return rng1

    sel.getByIndex.side_effect = by_index
    controller = MagicMock()
    controller.getSelection.return_value = sel
    ctx, body_cursor = _ctx_with_controller(controller)
    result = resolve_target_cursor(ctx, "selection", None)

    assert result is sel_cursor
    body_cursor.gotoEnd.assert_not_called()


def test_selection_target_disposed_raises_document_disposed():
    from plugin.framework.errors import DocumentDisposedError

    class DisposedException(Exception):
        pass

    controller = MagicMock()
    controller.getSelection.side_effect = DisposedException("closed")
    ctx, _ = _ctx_with_controller(controller)
    with pytest.raises(DocumentDisposedError):
        resolve_target_cursor(ctx, "selection", None)


# ---- C2: set_selection -------------------------------------------------------

def _selection_ctx():
    doc = MagicMock()
    controller = MagicMock()
    doc.getCurrentController.return_value = controller
    ctx = SimpleNamespace(doc=doc, ctx=MagicMock(), services={})
    return ctx, doc, controller


def test_set_selection_by_search_text_selects_and_reports():
    ctx, doc, controller = _selection_ctx()
    found = MagicMock()
    found.getString.return_value = "clause 3.2"
    with patch("plugin.writer.selection.find_occurrence_range", return_value=found):
        res = SetSelection().execute(ctx, search_text="clause 3.2")
    assert res["status"] == "ok" and res["selected_text"] == "clause 3.2"
    controller.select.assert_called_once_with(found)


def test_set_selection_occurrence_walks_findNext():
    ctx, doc, controller = _selection_ctx()
    second = MagicMock()
    second.getString.return_value = "hit2"
    with patch("plugin.writer.selection.find_occurrence_range", return_value=second) as mock_find:
        res = SetSelection().execute(ctx, search_text="hit", occurrence=1)
    assert res["status"] == "ok" and res["selected_text"] == "hit2"
    controller.select.assert_called_once_with(second)
    mock_find.assert_called_once_with(doc, "hit", occurrence=1, case_sensitive=True)


def test_set_selection_no_match_errors():
    ctx, doc, controller = _selection_ctx()
    with patch("plugin.writer.selection.find_occurrence_range", return_value=None), \
         patch("plugin.writer.selection.drawing_shape_containing", return_value=None):
        res = SetSelection().execute(ctx, search_text="nope")
    assert res["status"] == "error" and "No match" in res["message"]
    controller.select.assert_not_called()


def test_set_selection_shape_match_returns_shape_message():
    ctx, doc, controller = _selection_ctx()
    with patch("plugin.writer.selection.find_occurrence_range", return_value=None), \
         patch("plugin.writer.selection.drawing_shape_containing", return_value="Box 1"):
        res = SetSelection().execute(ctx, search_text="in_shape")
    assert res["status"] == "error"
    assert "shapes toolset" in res["message"]
    assert "Box 1" in res["message"]
    controller.select.assert_not_called()


def test_set_selection_occurrence_out_of_range():
    ctx, doc, controller = _selection_ctx()
    with patch("plugin.writer.selection.find_occurrence_range", side_effect=IndexError("occurrence 3 out of range (found 2 match(es), use 0..1).")):
        res = SetSelection().execute(ctx, search_text="hit", occurrence=3)
    assert res["status"] == "error"
    assert "occurrence 3 out of range (found 2 match(es), use 0..1)." in res["message"]
    controller.select.assert_not_called()


def test_set_selection_requires_a_target():
    ctx, doc, controller = _selection_ctx()
    res = SetSelection().execute(ctx)
    assert res["status"] == "error" and "search_text" in res["message"]


def test_set_selection_char_range_requires_both():
    ctx, doc, controller = _selection_ctx()
    res = SetSelection().execute(ctx, char_start=5)
    assert res["status"] == "error" and "char_start and char_end" in res["message"]


def test_set_selection_mixed_args_error():
    ctx, doc, controller = _selection_ctx()
    res = SetSelection().execute(ctx, search_text="clause", char_start=0)
    assert res["status"] == "error"
    assert "Cannot combine" in res["message"]


def test_set_selection_char_range_bool_error():
    ctx, doc, controller = _selection_ctx()
    res = SetSelection().execute(ctx, char_start=True, char_end=10)
    assert res["status"] == "error"
    assert "integer" in res["message"]


def test_set_selection_char_range_negative_error():
    ctx, doc, controller = _selection_ctx()
    res = SetSelection().execute(ctx, char_start=-1, char_end=10)
    assert res["status"] == "error"
    assert "non-negative" in res["message"]


def test_set_selection_char_range_reversed_error():
    ctx, doc, controller = _selection_ctx()
    res = SetSelection().execute(ctx, char_start=15, char_end=10)
    assert res["status"] == "error"
    assert "exceed" in res["message"]


def test_set_selection_char_range_start_equals_end_error():
    ctx, doc, controller = _selection_ctx()
    res = SetSelection().execute(ctx, char_start=10, char_end=10)
    assert res["status"] == "error"
    controller.select.assert_not_called()


def test_set_selection_char_range_start_past_end_no_select():
    ctx, doc, controller = _selection_ctx()
    rng = MagicMock()
    rng.getString.return_value = ""
    with patch("plugin.writer.selection.get_text_cursor_at_range_reached", return_value=(rng, False)):
        res = SetSelection().execute(ctx, char_start=100, char_end=110)
    assert res["status"] == "error"
    controller.select.assert_not_called()


def test_set_selection_char_range_truncated_end_omits_char_end_and_sets_warning():
    ctx, doc, controller = _selection_ctx()
    rng = MagicMock()
    rng.getString.return_value = "truncated"
    with patch("plugin.writer.selection.get_text_cursor_at_range_reached", return_value=(rng, False)):
        res = SetSelection().execute(ctx, char_start=10, char_end=50)
    assert res["status"] == "ok"
    assert res["char_start"] == 10
    assert "char_end" not in res
    assert "warning" in res
    assert res["length"] == len("truncated")
    controller.select.assert_called_once_with(rng)


def test_set_selection_by_char_offsets_happy_path():
    ctx, doc, controller = _selection_ctx()
    rng = MagicMock()
    rng.getString.return_value = "faixa"
    with patch("plugin.writer.selection.get_text_cursor_at_range_reached", return_value=(rng, True)):
        res = SetSelection().execute(ctx, char_start=10, char_end=15)
    assert res["status"] == "ok" and res["selected_text"] == "faixa"
    assert res["char_start"] == 10 and res["char_end"] == 15
    assert "warning" not in res
    controller.select.assert_called_once_with(rng)


def test_set_selection_getString_disposed_is_document_disposed():
    from plugin.framework.errors import DocumentDisposedError

    ctx, doc, controller = _selection_ctx()
    rng = MagicMock()
    rng.getString.side_effect = DocumentDisposedError("gone")
    with patch("plugin.writer.selection.get_text_cursor_at_range_reached", return_value=(rng, True)):
        res = SetSelection().execute_safe(ctx, char_start=10, char_end=15)
    assert res["status"] == "error"
    assert res["code"] == "DOCUMENT_DISPOSED"


def test_selection_target_spans_in_the_ranges_own_text():
    """A selection inside a table cell/frame lives in a different XText: the resolver must build
    the cursor there (a body-cursor gotoRange raises a raw UNO RuntimeException that escapes
    callers expecting ValueError)."""
    rng = MagicMock()
    rng.getStart.return_value = MagicMock()
    rng.getEnd.return_value = MagicMock()
    rng.getText.return_value.compareRegionStarts.return_value = -1
    cell_cursor = MagicMock()
    rng.getText.return_value.createTextCursorByRange.return_value = cell_cursor
    sel = MagicMock()
    sel.getCount.return_value = 1
    sel.getByIndex.return_value = rng
    controller = MagicMock()
    controller.getSelection.return_value = sel
    ctx, body_cursor = _ctx_with_controller(controller)
    result = resolve_target_cursor(ctx, "selection", None)
    assert result is cell_cursor          # built via rng.getText(), not the body cursor
    body_cursor.gotoRange.assert_not_called()
    body_cursor.gotoEnd.assert_not_called()


def test_selection_target_wraps_span_failure_in_valueerror():
    import pytest as _pytest

    rng = MagicMock()
    rng.getStart.return_value = MagicMock()
    rng.getEnd.return_value = MagicMock()
    rng.getText.return_value.compareRegionStarts.return_value = -1
    rng.getText.return_value.createTextCursorByRange.side_effect = RuntimeError("cross-text")
    sel = MagicMock()
    sel.getCount.return_value = 1
    sel.getByIndex.return_value = rng
    controller = MagicMock()
    controller.getSelection.return_value = sel
    ctx, _ = _ctx_with_controller(controller)
    with _pytest.raises(ValueError):
        resolve_target_cursor(ctx, "selection", None)


def test_set_selection_is_core_read_only():
    assert SetSelection.tier == "core" and SetSelection.is_mutation is False


def test_apply_document_content_selection_failure_not_silent():
    from plugin.framework.config import set_configs
    set_configs({'doc.agent_edit_review_mode': 'none'})
    from plugin.framework.errors import ToolExecutionError
    from plugin.writer import format as format_support
    from plugin.writer.apply_document_content import ApplyDocumentContent

    ctx = MagicMock()
    ctx.doc.getUndoManager.return_value.isLocked.return_value = False
    ctx.services.get.return_value = MagicMock()
    with patch("plugin.writer.apply_document_content.selection_anchor", return_value=MagicMock()), \
         patch.object(format_support, "insert_content_at_position",
                      side_effect=ToolExecutionError("Could not resolve the current selection")):
        # _execute maps ToolExecutionError to the standard tool error dict so chat/MCP
        # see status=error (nested-table follow-up). Raising here was the old contract
        # and failed PR CI 35451880001 after that change.
        result = ApplyDocumentContent().execute(ctx, content="x", target="selection")
    assert result["status"] == "error"
    assert "selection" in result.get("message", "")


def test_set_selection_disposal_is_document_disposed():
    from plugin.framework.errors import DocumentDisposedError

    ctx, doc, _ = _selection_ctx()
    doc.getCurrentController.side_effect = DocumentDisposedError("gone")
    res = SetSelection().execute_safe(ctx, search_text="test")
    assert res["status"] == "error"
    assert res["code"] == "DOCUMENT_DISPOSED"


def test_set_selection_markup_search_miss_shows_stripped_string():
    ctx, doc, controller = _selection_ctx()
    with patch("plugin.writer.selection.find_occurrence_range", return_value=None), \
         patch("plugin.writer.selection.drawing_shape_containing", return_value=None):
        res = SetSelection().execute(ctx, search_text="<b>missing_word</b>")
    assert res["status"] == "error"
    assert "missing_word" in res["message"]
    assert "<b>" not in res["message"]


def test_set_selection_non_int_occurrence_does_not_touch_controller():
    ctx, doc, controller = _selection_ctx()
    res = SetSelection().execute(ctx, search_text="hello", occurrence="first")
    assert res["status"] == "error"
    assert res["message"] == "occurrence must be a non-negative integer."
    controller.select.assert_not_called()
    doc.getCurrentController.assert_not_called()



