# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

from plugin.calc.tabular_egress import insert_tabular_result_into_calc
from plugin.doc.compound_undo import undo_titles
from plugin.testing_runner import native_test
from plugin.tests.testing_utils import with_native_doc


@native_test
@with_native_doc("calc")
def test_insert_tabular_result_is_one_undo_step_uno(ctx, doc):
    from com.sun.star.table.CellContentType import EMPTY

    sheet = doc.getSheets().getByIndex(0)
    before = undo_titles(doc)
    assert before is not None

    grid = [
        ["H1", "H2", "H3"],
        [1, 2, 3],
        [4, 5, 6],
    ]
    rows = insert_tabular_result_into_calc(doc, grid, start_col=0, start_row=0)
    assert rows == 3

    after = undo_titles(doc)
    assert after is not None
    delta = after[: len(after) - len(before)]
    assert delta == ("WriterAgent: Insert result",)

    doc.getUndoManager().undo()
    for r in range(3):
        for c in range(3):
            assert sheet.getCellByPosition(c, r).getType() == EMPTY
