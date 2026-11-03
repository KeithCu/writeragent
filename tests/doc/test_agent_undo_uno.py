# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""UNO tests for agent undo snapshots (/undo)."""

from plugin.doc import agent_undo as au
from plugin.testing_runner import native_test
from plugin.tests.testing_utils import TestingFactory


@native_test
def test_agent_undo_writer_round_trip(ctx):
    with TestingFactory.native_doc(ctx, "writer") as doc:
        text = doc.getText()
        cursor = text.createTextCursor()
        text.insertString(cursor, "Before agent", False)
        au.record_agent_turn_before(doc)
        cursor.gotoEnd(False)
        text.insertString(cursor, " after agent", False)
        au.record_agent_turn_after(doc)
        msg = au.undo_last_agent_edit(doc)
        assert "steps" in msg
        assert "after agent" not in doc.getText().getString()


@native_test
def test_slash_formula_setformula_semicolon_on_calc(ctx):
    from plugin.chatbot.slash_undo_formula import build_slash_formula_prompt

    with TestingFactory.native_doc(ctx, "calc") as doc:
        sheet = doc.getSheets().getByIndex(0)
        sheet.getCellByPosition(0, 0).setString("A")
        sheet.getCellByPosition(0, 1).setValue(1.0)
        sheet.getCellByPosition(0, 2).setValue(2.0)
        cc = doc.getCurrentController()
        cc.select(sheet.getCellByPosition(1, 1))
        _prompt, target, cell = build_slash_formula_prompt(doc, "sum column A")
        assert target == "B2"
        cell.setFormula("=SUM(A2;A3)")
        assert int(cell.getError()) == 0
