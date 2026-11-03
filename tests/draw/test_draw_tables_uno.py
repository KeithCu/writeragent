# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Native UNO tests for Draw/Impress TableShape operations."""

from __future__ import annotations

import json
from typing import Any

from plugin.draw.tables import iter_table_shapes
from plugin.testing_runner import native_test
from plugin.tests.testing_utils import TestingFactory, with_native_doc


def _exec_tool(doc: Any, ctx: Any, name: str, args: dict[str, Any]) -> str:
    res = TestingFactory.execute_tool(doc, ctx, name, args, doc_type="draw")
    return json.dumps(res) if isinstance(res, dict) else res


@native_test
@with_native_doc("draw")
def test_draw_table_insert_and_read(ctx: Any, doc: Any) -> None:
    res = json.loads(
        _exec_tool(
            doc,
            ctx,
            "table_insert",
            {"rows": 2, "columns": 2, "data": [["A", "B"], ["C", "D"]]},
        )
    )
    assert res.get("status") == "ok", res
    assert res.get("cells_written") == 4, res

    res_list = json.loads(_exec_tool(doc, ctx, "table_list", {}))
    assert res_list.get("status") == "ok", res_list
    tables = res_list.get("tables", [])
    assert len(tables) == 1, res_list
    assert tables[0].get("rows") == 2
    assert tables[0].get("cols") == 2

    res_cells = json.loads(_exec_tool(doc, ctx, "table_get_cells", {}))
    assert res_cells.get("status") == "ok", res_cells
    assert res_cells.get("matrix") == [["A", "B"], ["C", "D"]]


@native_test
@with_native_doc("draw")
def test_draw_table_cell_get_string_available(ctx: Any, doc: Any) -> None:
    res = json.loads(
        _exec_tool(
            doc,
            ctx,
            "table_insert",
            {"rows": 2, "columns": 2, "data": [["A", "B"], ["C", "D"]]},
        )
    )
    assert res.get("status") == "ok", res

    shapes = iter_table_shapes(doc)
    assert len(shapes) > 0, "No table shape found"
    entry = shapes[0]
    cell = entry["model"].getCellByPosition(0, 0)
    assert callable(getattr(cell, "getString", None))
    assert cell.getString() == "A"


@native_test
@with_native_doc("draw")
def test_draw_table_rejects_oversized_data(ctx: Any, doc: Any) -> None:
    page = doc.getCurrentController().getCurrentPage()
    if page is None:
        page = doc.getDrawPages().getByIndex(0)
    shape_count_before = page.getCount()
    undo_mgr = doc.getUndoManager()
    undo_count_before = len(undo_mgr.getAllUndoActionTitles())

    res = json.loads(
        _exec_tool(
            doc,
            ctx,
            "table_insert",
            {"rows": 1, "columns": 1, "data": [["A", "B"]]},
        )
    )
    assert res.get("status") == "error", res
    assert "data row 0 has 2 columns" in res.get("message", "")
    assert page.getCount() == shape_count_before
    assert len(undo_mgr.getAllUndoActionTitles()) == undo_count_before


@native_test
@with_native_doc("draw")
def test_draw_table_grows_to_20x10(ctx: Any, doc: Any) -> None:
    res = json.loads(
        _exec_tool(
            doc,
            ctx,
            "table_insert",
            {"rows": 20, "columns": 10, "data": [["corner"]]},
        )
    )
    assert res.get("status") == "ok", res

    res_list = json.loads(_exec_tool(doc, ctx, "table_list", {}))
    assert res_list.get("status") == "ok", res_list
    tables = res_list.get("tables", [])
    assert len(tables) == 1, res_list
    assert tables[0].get("rows") == 20
    assert tables[0].get("cols") == 10

    res_cells = json.loads(_exec_tool(doc, ctx, "table_get_cells", {}))
    assert res_cells.get("status") == "ok", res_cells
    matrix = res_cells.get("matrix", [])
    assert len(matrix) == 20
    assert all(len(row) == 10 for row in matrix)
    assert matrix[0][0] == "corner"


@native_test
@with_native_doc("draw")
def test_draw_table_structure_row_column_and_delete(ctx: Any, doc: Any) -> None:
    res = json.loads(
        _exec_tool(
            doc,
            ctx,
            "table_insert",
            {"rows": 2, "columns": 2, "data": [["A", "B"], ["C", "D"]]},
        )
    )
    assert res.get("status") == "ok", res

    res_row = json.loads(
        _exec_tool(
            doc,
            ctx,
            "manage_table_structure",
            {"action": "insert", "axis": "row", "index": 0},
        )
    )
    assert res_row.get("status") == "ok", res_row
    assert res_row.get("rows") == 3
    assert res_row.get("cols") == 2

    res_col = json.loads(
        _exec_tool(
            doc,
            ctx,
            "manage_table_structure",
            {"action": "insert", "axis": "column", "index": 0},
        )
    )
    assert res_col.get("status") == "ok", res_col
    assert res_col.get("rows") == 3
    assert res_col.get("cols") == 3

    res_del_col = json.loads(
        _exec_tool(
            doc,
            ctx,
            "manage_table_structure",
            {"action": "delete", "axis": "column", "index": 0},
        )
    )
    assert res_del_col.get("status") == "ok", res_del_col
    assert res_del_col.get("rows") == 3
    assert res_del_col.get("cols") == 2

    res_del_row = json.loads(
        _exec_tool(
            doc,
            ctx,
            "manage_table_structure",
            {"action": "delete", "axis": "row", "index": 0},
        )
    )
    assert res_del_row.get("status") == "ok", res_del_row
    assert res_del_row.get("rows") == 2
    assert res_del_row.get("cols") == 2

    res_del = json.loads(_exec_tool(doc, ctx, "table_delete", {}))
    assert res_del.get("status") == "ok", res_del

    res_list = json.loads(_exec_tool(doc, ctx, "table_list", {}))
    assert res_list.get("status") == "ok", res_list
    assert res_list.get("tables") == []


@native_test
@with_native_doc("draw")
def test_draw_table_insert_is_one_undo_step(ctx: Any, doc: Any) -> None:
    undo_mgr = doc.getUndoManager()
    titles_before = list(undo_mgr.getAllUndoActionTitles())

    res = json.loads(
        _exec_tool(
            doc,
            ctx,
            "table_insert",
            {"rows": 2, "columns": 2, "data": [["A", "B"], ["C", "D"]]},
        )
    )
    assert res.get("status") == "ok", res

    titles_after = list(undo_mgr.getAllUndoActionTitles())
    assert len(titles_after) == len(titles_before) + 1, (titles_before, titles_after)

    newest = titles_after[0]
    label = getattr(newest, "Title", newest)
    assert "WriterAgent: Insert table" in str(label), titles_after
    # SvxDrawPage::add records no undo action, so Ctrl+Z reverts the table edits but not the shape.
