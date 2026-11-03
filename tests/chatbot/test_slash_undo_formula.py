# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for /undo and /formula slash handlers."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from plugin.chatbot.slash_undo_formula import (
    FORMULA_UNDO_TITLE,
    SlashFormulaPromptError,
    build_slash_formula_prompt,
    clean_formula_model_reply,
    insert_slash_formula_on_main,
)


def test_clean_formula_model_reply():
    assert clean_formula_model_reply("```\n=SUM(A1:A5)\n```") == "=SUM(A1:A5)"
    assert clean_formula_model_reply("  =A1+B1  ") == "=A1+B1"
    assert clean_formula_model_reply("not a formula") == "not a formula"


def test_build_slash_formula_prompt_uses_data_array_and_range_top_left():
    header_range = MagicMock()
    header_range.getDataArray.return_value = (("Name", "Qty"),)
    sheet = MagicMock()
    sheet.getName.return_value = "Sheet1"
    sheet.getCellByPosition.side_effect = lambda col, row: MagicMock(name=f"cell_{col}_{row}")
    sheet.getCellRangeByPosition.return_value = header_range
    cursor = MagicMock()
    used = SimpleNamespace(StartColumn=0, StartRow=0, EndColumn=1, EndRow=5)
    cursor.getRangeAddress.return_value = used
    sheet.createCursor.return_value = cursor

    selection = MagicMock()
    selection.getRangeAddress.return_value = SimpleNamespace(StartColumn=2, StartRow=3, EndColumn=4, EndRow=6)
    view = SimpleNamespace(ActiveSheet=sheet, getSelection=lambda: selection)
    model = SimpleNamespace(CurrentController=view)

    prompt, target, _cell = build_slash_formula_prompt(model, "total qty")
    assert target == "C4"
    sheet.getCellByPosition.assert_called_with(2, 3)
    assert "Name | Qty" in prompt
    assert "semicolon" in prompt
    assert "C4" in prompt


def test_build_slash_formula_prompt_non_cell_selection():
    view = SimpleNamespace(ActiveSheet=MagicMock(), getSelection=lambda: None)
    model = SimpleNamespace(CurrentController=view)
    try:
        build_slash_formula_prompt(model, "x")
        assert False, "expected SlashFormulaPromptError"
    except SlashFormulaPromptError as exc:
        assert "Select a cell" in str(exc)


def test_insert_slash_formula_on_main_uses_undo_context():
    cell = MagicMock()
    cell.getError.return_value = 0
    model = MagicMock()
    host = MagicMock()
    formula = "=SUM(A1:A2)"
    recorded: list[str] = []

    def fake_commit(doc: object, title: str, fn: object) -> None:
        from plugin.framework.i18n import _

        assert doc is model
        assert title == _(FORMULA_UNDO_TITLE)
        fn()
        return None

    with (
        patch("plugin.doc.compound_undo.commit_or_rollback", side_effect=fake_commit),
        patch("plugin.doc.agent_undo.record_agent_turn_before", side_effect=lambda d: recorded.append("before")),
        patch("plugin.doc.agent_undo.record_agent_turn_after", side_effect=lambda d: recorded.append("after")),
    ):
        insert_slash_formula_on_main(
            host,
            model=model,
            cell=cell,
            target_addr="B2",
            chunks=[formula],
        )

    cell.setFormula.assert_called_once_with(formula)
    assert recorded == ["before", "after"]
    host.append_output.assert_called_once()
    assert formula in host.append_output.call_args[0][0]


def test_insert_slash_formula_reports_cell_error():
    cell = MagicMock()
    cell.getError.return_value = 508
    host = MagicMock()
    with patch("plugin.doc.compound_undo.commit_or_rollback", return_value=None):
        with patch("plugin.doc.agent_undo.record_agent_turn_before"):
            with patch("plugin.doc.agent_undo.record_agent_turn_after"):
                insert_slash_formula_on_main(
                    host,
                    model=MagicMock(),
                    cell=cell,
                    target_addr="A1",
                    chunks=["=SUM(A1;A2)"],
                )
    assert "error" in host.append_output.call_args[0][0].lower()
