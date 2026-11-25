# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

from plugin.doc.compound_undo import UndoStepOrRollback, WriterCompoundUndo, commit_or_rollback, undo_titles
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
@with_native_doc("writer", reuse=False)
def test_nested_failure_after_write_is_not_nothing_landed_uno(ctx, doc):
    text = doc.getText()
    outer = WriterCompoundUndo(doc, "Outer")
    assert outer.entered
    try:
        def insert_and_fail():
            text.insertString(text.getEnd(), "PARTIAL", False)
            raise RuntimeError("boom")

        outcome = commit_or_rollback(doc, "Inner", insert_and_fail)
        assert outcome.rolled_back is False
        assert outcome.nothing_landed is False
        assert "PARTIAL" in text.getString()
    finally:
        outer.close()

    outcome_outer = commit_or_rollback(
        doc, "EmptyOutermost", lambda: (_ for _ in ()).throw(RuntimeError("refused"))
    )
    assert outcome_outer.rolled_back is False
    assert outcome_outer.nothing_landed is True


@native_test
@with_native_doc("writer", reuse=False)
def test_edit_that_clears_the_undo_stack_is_not_nothing_landed_uno(ctx, doc):
    um = doc.getUndoManager()
    um.reset()
    text = doc.getText()
    text.insertString(text.getEnd(), "Prev", False)
    assert len(undo_titles(doc) or ()) == 1

    page_styles = doc.getStyleFamilies().getByName("PageStyles")
    standard = page_styles.getByName("Standard")

    def write_clear_and_fail():
        text.insertString(text.getEnd(), "PARTIAL", False)
        standard.setPropertyValue("HeaderIsOn", True)
        raise RuntimeError("boom")

    outcome = commit_or_rollback(doc, "ClearStack", write_clear_and_fail)
    assert outcome.rolled_back is False
    assert outcome.nothing_landed is False
    assert "PARTIAL" in text.getString()
