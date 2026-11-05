# WriterAgent — Draw edit selection against live UNO.
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

from typing import Any

from plugin.doc.compound_undo import WriterCompoundUndo
from plugin.draw.editselection import iter_draw_rewrite_targets
from plugin.framework.i18n import _
from plugin.testing_runner import native_test
from plugin.tests.testing_utils import TestingFactory, with_native_doc


@native_test
@with_native_doc("impress")
def test_impress_text_shape_selection_target(ctx: Any, doc: Any) -> None:
    page = doc.getDrawPages().getByIndex(0)
    shape = doc.createInstance("com.sun.star.drawing.TextShape")
    shape.setString("Hello")
    page.add(shape)
    controller = doc.getCurrentController()
    controller.select(shape)

    targets, saw_table = iter_draw_rewrite_targets(controller.getSelection())
    assert saw_table is False
    assert len(targets) == 1
    assert targets[0].getString() == "Hello"

    title = _("WriterAgent: Edit Selection (Draw)")
    undo = WriterCompoundUndo(doc, title)
    targets[0].setString("Hi")
    undo.close()

    undo_mgr = doc.getUndoManager()
    titles = undo_mgr.getAllUndoActionTitles()
    assert titles[0] == title
    undo_mgr.undo()
    assert targets[0].getString() == "Hello"


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

    page = doc.getDrawPages().getByIndex(0)
    table_shape = page.getByIndex(page.getCount() - 1)
    controller = doc.getCurrentController()
    controller.select(table_shape)

    targets, saw_table = iter_draw_rewrite_targets(controller.getSelection())
    assert saw_table is True
    assert targets == []

    cell = table_shape.getCellByPosition(0, 0)
    assert cell.getString() == "A"
