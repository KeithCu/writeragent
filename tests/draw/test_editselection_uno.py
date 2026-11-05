# WriterAgent — Draw edit selection against live UNO.
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

from typing import Any

from plugin.doc.compound_undo import WriterCompoundUndo
from plugin.doc.visual_helpers import _controller_selection
from plugin.draw.editselection import iter_draw_rewrite_targets
from plugin.draw.tables import _cell_string, _table_model, iter_table_shapes
from plugin.draw.transform_engine import _set_shape_text
from plugin.framework.i18n import _
from plugin.testing_runner import native_test
from plugin.tests.testing_utils import TestingFactory, with_native_doc


@native_test
@with_native_doc("impress")
def test_impress_text_shape_selection_target(ctx: Any, doc: Any) -> None:
    """Test that iter_draw_rewrite_targets finds text shapes for editing with custom undo action.
    
    Verifies the target selection logic, that setString can modify text, and that
    a custom XUndoAction registered via addUndoAction produces one undo step that
    restores the original text.
    """
    from plugin.draw.draw_undo import DrawTextEditUndoAction
    
    page = doc.getDrawPages().getByIndex(0)
    shape = doc.createInstance("com.sun.star.drawing.TextShape")
    page.add(shape)
    _set_shape_text(shape, "Hello")
    controller = doc.getCurrentController()
    if hasattr(controller, "setCurrentPage"):
        controller.setCurrentPage(page)
    controller.select(shape)

    targets, saw_table = iter_draw_rewrite_targets(_controller_selection(doc))
    assert saw_table is False
    assert len(targets) == 1
    assert targets[0].getString() == "Hello"

    # Verify that setString works (text can be changed)
    targets[0].setString("Modified")
    assert targets[0].getString() == "Modified"
    
    # Test custom undo action within compound undo context
    title = _("WriterAgent: Edit Selection (Draw)")
    undo = WriterCompoundUndo(doc, title)
    assert undo.entered, "compound undo should enter on Impress"
    
    # Register custom undo action
    text_obj_originals = [(targets[0], "Hello")]
    final_texts = ["Changed again"]
    custom_undo_action = DrawTextEditUndoAction(text_obj_originals, final_texts, title)
    um = doc.getUndoManager()
    um.addUndoAction(custom_undo_action)
    
    targets[0].setString("Changed again")
    undo.close()
    
    assert targets[0].getString() == "Changed again"
    
    # Verify undo stack has actions
    undo_titles = um.getAllUndoActionTitles()
    assert len(undo_titles) > 0, "undo stack should have actions"
    assert undo_titles[0] == title, f"top undo action title should be {title!r}, got {undo_titles[0]!r}"
    
    # Undo should restore original text
    um.undo()
    assert targets[0].getString() == "Hello", "undo should restore original text 'Hello'"


@native_test
@with_native_doc("draw")
def test_draw_table_selection_saw_table_no_targets(ctx: Any, doc: Any) -> None:
    res = TestingFactory.execute_tool(
        doc,
        ctx,
        "table_insert",
        {"rows": 2, "columns": 2, "data": [["A", "B"], ["C", "D"]]},
        doc_type="draw",
    )
    if not isinstance(res, dict):
        import json

        res = json.loads(res)
    assert res.get("status") == "ok", res

    tables = iter_table_shapes(doc)
    assert len(tables) == 1, tables
    table_shape = tables[0]["shape"]
    model = _table_model(table_shape)
    assert model is not None

    controller = doc.getCurrentController()
    controller.select(table_shape)

    targets, saw_table = iter_draw_rewrite_targets(_controller_selection(doc))
    assert saw_table is True
    assert targets == []

    assert _cell_string(model.getCellByPosition(0, 0)) == "A"
