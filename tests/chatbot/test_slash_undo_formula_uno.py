# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""UNO tests for /formula slash handler."""

from types import SimpleNamespace

from plugin.chatbot.slash_undo_formula import (
    build_slash_formula_prompt,
    insert_slash_formula_on_main,
)
from plugin.doc import agent_undo as au
from plugin.testing_runner import native_test
from plugin.tests.testing_utils import TestingFactory


@native_test
def test_slash_formula_setformula_semicolon_on_calc(ctx):
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


@native_test
def test_slash_formula_insert_then_undo_on_calc(ctx):
    host = SimpleNamespace(append_output=lambda _line: None)

    with TestingFactory.native_doc(ctx, "calc") as doc:
        sheet = doc.getSheets().getByIndex(0)
        sheet.getCellByPosition(0, 0).setString("A")
        sheet.getCellByPosition(0, 1).setValue(1.0)
        sheet.getCellByPosition(0, 2).setValue(2.0)
        cc = doc.getCurrentController()
        cell = sheet.getCellByPosition(1, 1)
        cc.select(cell)
        insert_slash_formula_on_main(
            host,
            model=doc,
            cell=cell,
            target_addr="B2",
            chunks=["=SUM(A2;A3)"],
        )
        assert str(cell.getFormula() or "").startswith("=SUM")
        msg = au.undo_last_agent_edit(doc)
        assert "1 steps" in msg
        assert str(cell.getFormula() or "") == ""
