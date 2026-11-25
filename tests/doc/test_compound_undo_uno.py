# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

from plugin.doc.compound_undo import UndoStepOrRollback, undo_titles
from plugin.testing_runner import native_test
from plugin.tests.testing_utils import with_native_doc


@native_test
@with_native_doc("calc")
def test_undo_step_or_rollback_restores_calc_cells_uno(ctx, doc):
    from com.sun.star.table.CellContentType import EMPTY

    sheet = doc.getSheets().getByIndex(0)
    a1 = sheet.getCellByPosition(0, 0)
    b1 = sheet.getCellByPosition(1, 0)
    a1.setString("keep")

    before = undo_titles(doc)
    assert before is not None

    try:
        with UndoStepOrRollback(doc, "WriterAgent: Probe rollback"):
            a1.setString("")
            b1.setValue(5)
            raise RuntimeError("boom")
    except RuntimeError as exc:
        assert str(exc) == "boom"
    else:
        raise AssertionError("Expected RuntimeError to propagate")

    assert a1.getString() == "keep"
    assert b1.getType() == EMPTY
    assert undo_titles(doc) == before


@native_test
@with_native_doc("writer")
def test_commit_or_rollback_restores_selection_pin_uno(ctx, doc):
    from plugin.doc.compound_undo import commit_or_rollback
    from plugin.doc.text_helpers import selection_cursor_snapshot
    from plugin.framework.tool import SelectionPin
    from plugin.writer.target_resolver import cursor_spanning_range

    text = doc.getText()
    text.setString("hello world!")
    cur = text.createTextCursor()
    cur.gotoStart(False)
    cur.goRight(6, False)
    cur.goRight(5, True)
    doc.getCurrentController().select(cur)
    pin = SelectionPin(selection_cursor_snapshot(doc), doc=doc)
    assert pin.range.getString() == "world"

    def mutate():
        cursor_spanning_range(pin.range).setString("")
        raise RuntimeError("boom")

    outcome = commit_or_rollback(doc, "WriterAgent: Probe pin rollback", mutate, selection_pin=pin)
    assert outcome.rolled_back is True
    assert text.getString() == "hello world!"
    assert pin.range.getString() == "world"
