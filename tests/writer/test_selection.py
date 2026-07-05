# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Selection: target='selection' must not silently append at the document end, and set_selection
lets a headless client pick a passage. No LibreOffice required."""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from plugin.tests.testing_utils import setup_uno_mocks
setup_uno_mocks()

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


# ---- C2: set_selection -------------------------------------------------------

def _selection_ctx():
    doc = MagicMock()
    controller = MagicMock()
    doc.getCurrentController.return_value = controller
    ctx = SimpleNamespace(doc=doc)
    return ctx, doc, controller


def test_set_selection_by_search_text_selects_and_reports():
    ctx, doc, controller = _selection_ctx()
    found = MagicMock()
    found.getString.return_value = "clause 3.2"
    doc.findFirst.return_value = found
    res = SetSelection().execute(ctx, search_text="clause 3.2")
    assert res["status"] == "ok" and res["selected_text"] == "clause 3.2"
    controller.select.assert_called_once_with(found)


def test_set_selection_occurrence_walks_findNext():
    ctx, doc, controller = _selection_ctx()
    first, second = MagicMock(), MagicMock()
    second.getString.return_value = "hit2"
    doc.findFirst.return_value = first
    doc.findNext.return_value = second
    res = SetSelection().execute(ctx, search_text="hit", occurrence=1)
    assert res["status"] == "ok" and res["selected_text"] == "hit2"
    controller.select.assert_called_once_with(second)


def test_set_selection_no_match_errors():
    ctx, doc, controller = _selection_ctx()
    doc.findFirst.return_value = None
    res = SetSelection().execute(ctx, search_text="nope")
    assert res["status"] == "error" and "No match" in res["message"]
    controller.select.assert_not_called()


def test_set_selection_requires_a_target():
    ctx, doc, controller = _selection_ctx()
    res = SetSelection().execute(ctx)
    assert res["status"] == "error" and "search_text" in res["message"]


def test_set_selection_char_range_requires_both():
    ctx, doc, controller = _selection_ctx()
    res = SetSelection().execute(ctx, char_start=5)
    assert res["status"] == "error" and "char_start and char_end" in res["message"]


def test_set_selection_by_char_offsets_happy_path():
    from unittest.mock import patch

    ctx, doc, controller = _selection_ctx()
    rng = MagicMock()
    rng.getString.return_value = "faixa"
    with patch("plugin.doc.document_helpers.get_text_cursor_at_range", return_value=rng):
        res = SetSelection().execute(ctx, char_start=10, char_end=15)
    assert res["status"] == "ok" and res["selected_text"] == "faixa"
    assert res["char_start"] == 10 and res["char_end"] == 15
    controller.select.assert_called_once_with(rng)


def test_selection_target_spans_in_the_ranges_own_text():
    """A selection inside a table cell/frame lives in a different XText: the resolver must build
    the cursor there (a body-cursor gotoRange raises a raw UNO RuntimeException that escapes
    callers expecting ValueError)."""
    rng = MagicMock()
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
    from plugin.framework.errors import ToolExecutionError
    from plugin.writer import format as format_support
    from plugin.writer.content import ApplyDocumentContent

    ctx = MagicMock()
    ctx.doc.getUndoManager.return_value.isLocked.return_value = False
    ctx.services.get.return_value = MagicMock()
    with patch("plugin.writer.content._selection_anchor", return_value=MagicMock()), \
         patch.object(format_support, "insert_content_at_position",
                      side_effect=ToolExecutionError("Could not resolve the current selection")):
        with pytest.raises(ToolExecutionError, match="selection"):
            ApplyDocumentContent().execute(ctx, content="x", target="selection")
