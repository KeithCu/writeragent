# WriterAgent — Draw edit selection against live UNO.
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

from typing import Any
from unittest.mock import patch

from plugin.doc.visual_helpers import _controller_selection
from plugin.draw.editselection import do_draw_extend_edit, iter_draw_rewrite_targets
from plugin.draw.tables import _cell_string, _table_model, iter_table_shapes
from plugin.draw.transform_engine import _set_shape_text
from plugin.framework.i18n import _
from plugin.testing_runner import native_test
from plugin.tests.testing_utils import TestingFactory, with_native_doc


def _config(key: str) -> object:
    return {
        "extend_selection_system_prompt": "extend system",
        "edit_selection_system_prompt": "edit system",
        "chatbot.extend_selection_max_tokens": 50,
        "chatbot.edit_selection_max_new_tokens": 7,
    }.get(key, 0 if "max" in key else "")


def _fake_stream_completion_tasks(
    ctx: Any,
    client: Any,
    tasks: list[Any],
    prepare_task: Any,
    on_finished: Any,
    completion_out: dict[str, Any] | None = None,
    **_unused: Any,
) -> None:
    apply_chunk, _on_error = prepare_task(tasks[0])
    apply_chunk("Rewritten", False)
    on_finished()
    if completion_out is not None:
        completion_out["finish_reason"] = "stop"


@native_test
@with_native_doc("impress")
def test_impress_text_shape_selection_target(ctx: Any, doc: Any) -> None:
    """Edit Selection on Impress registers one undo step that restores prior text."""
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

    title = _("WriterAgent: Edit Selection (Draw)")
    with patch("plugin.draw.editselection.get_config", side_effect=_config), patch(
        "plugin.draw.editselection.create_validated_client", return_value=object()
    ), patch(
        "plugin.draw.editselection.stream_completion_tasks", side_effect=_fake_stream_completion_tasks
    ), patch("plugin.doc.agent_undo.record_agent_turn_before"), patch(
        "plugin.doc.agent_undo.record_agent_turn_after"
    ):
        do_draw_extend_edit(ctx, doc, is_edit=True, preset_instruction="Rewrite.")

    assert targets[0].getString() == "Rewritten"

    um = doc.getUndoManager()
    undo_titles = um.getAllUndoActionTitles()
    assert len(undo_titles) > 0, "undo stack should have actions"
    assert undo_titles[0] == title, f"top undo action title should be {title!r}, got {undo_titles[0]!r}"

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
