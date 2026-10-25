# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for Monaco editor save modes (formula vs plain text)."""

from __future__ import annotations

from typing import Any

from plugin.calc.python.editor import (
    _apply_cell_save,
    _resolve_code_ref_cell,
    editor_load_save_as_plain,
)
from plugin.calc.python.formula_edit import (
    build_editor_formula_save,
    parse_python_formula,
    py_code_arg_is_cell_ref,
)
from plugin.tests.testing_utils import CalcCellStub, CalcDocStub, CalcSheetStub


def test_editor_load_save_as_plain_python_formula():
    parts = parse_python_formula('=PYTHON("x"; A1:B10)')
    assert parts is not None
    assert editor_load_save_as_plain(parsed_parts=parts, initial_code="x") is False


def test_editor_load_save_as_plain_plain_string_cell():
    assert editor_load_save_as_plain(parsed_parts=None, initial_code="np.mean(data)") is True


def test_editor_load_save_as_plain_empty_cell():
    assert editor_load_save_as_plain(parsed_parts=None, initial_code="") is False
    assert editor_load_save_as_plain(parsed_parts=None, initial_code="   ") is False


def test_editor_load_save_as_plain_follow_code_ref():
    parts = parse_python_formula("=PY($A$1; C1:C10)")
    assert parts is not None
    assert editor_load_save_as_plain(
        parsed_parts=parts, initial_code="result = 1", follow_code_ref=True
    ) is True


def test_build_editor_formula_save_new_cell_with_data_binding():
    result = build_editor_formula_save(
        parsed_parts=None,
        new_code="np.mean(data)",
        cell_has_unparsed_python=False,
        data_binding_text="A1:A10",
    )
    assert result == '=PY("np.mean(data)";A1:A10)'


def test_build_editor_formula_save_multi_range_from_textbox():
    result = build_editor_formula_save(
        parsed_parts=None,
        new_code="sum(d) for d in ranges",
        cell_has_unparsed_python=False,
        data_binding_text="A1:A5, C1:C5",
    )
    assert isinstance(result, str)
    assert "A1:A5" in result
    assert "C1:C5" in result


def test_build_editor_formula_save_clear_data_binding():
    parts = parse_python_formula('=PYTHON("x"; A1:B10)')
    assert parts is not None
    result = build_editor_formula_save(
        parsed_parts=parts,
        new_code="x = 1",
        cell_has_unparsed_python=False,
        data_binding_text="",
    )
    assert result == '=PYTHON("x = 1")'


def test_apply_cell_save_with_data_binding():
    doc = CalcDocStub()
    cell = CalcCellStub()

    result = _apply_cell_save(
        doc,
        cell,
        parsed_parts=None,
        new_code="np.sum(data)",
        save_as_plain=False,
        data_binding_text="D1:D10",
    )

    assert result == {"type": "saved", "ok": True, "save_as_plain": False}
    formula = cell.getFormula()
    assert "D1:D10" in formula
    assert "np.sum(data)" in formula
    assert doc.calculate_all_count == 1


def test_build_editor_formula_save_new_cell():
    result = build_editor_formula_save(
        parsed_parts=None,
        new_code="np.mean(data)",
        cell_has_unparsed_python=False,
    )
    assert result == '=PY("np.mean(data)")'


def test_build_editor_formula_save_preserves_data_suffix():
    parts = parse_python_formula('=PYTHON("x"; A1:B10)')
    assert parts is not None
    result = build_editor_formula_save(
        parsed_parts=parts,
        new_code="np.sum(data)",
        cell_has_unparsed_python=False,
    )
    assert isinstance(result, str)
    assert "A1:B10" in result
    assert 'np.sum(data)' in result
    reparsed = parse_python_formula(result)
    assert reparsed is not None
    assert reparsed.code == "np.sum(data)"


def test_build_editor_formula_save_unparsed_python_returns_error():
    result = build_editor_formula_save(
        parsed_parts=None,
        new_code="x = 1",
        cell_has_unparsed_python=True,
    )
    assert isinstance(result, dict)
    assert result["type"] == "error"


def test_apply_cell_save_formula_mode():
    doc = CalcDocStub()
    cell = CalcCellStub()
    parts = parse_python_formula('=PYTHON("old"; C1:C5)')
    assert parts is not None

    result = _apply_cell_save(
        doc,
        cell,
        parsed_parts=parts,
        new_code="new",
        save_as_plain=False,
    )

    assert result == {"type": "saved", "ok": True, "save_as_plain": False}
    formula = cell.getFormula()
    assert formula.startswith("=")
    assert cell.getString() == ""
    assert "C1:C5" in formula
    assert "new" in formula
    reparsed = parse_python_formula(formula)
    assert reparsed is not None
    assert reparsed.code == "new"
    assert doc.calculate_all_count == 1


def test_apply_cell_save_plain_text_mode():
    doc = CalcDocStub()
    cell = CalcCellStub()
    parts = parse_python_formula('=PYTHON("old"; C1:C5)')
    assert parts is not None
    code = "np.mean(data)\n"

    result = _apply_cell_save(
        doc,
        cell,
        parsed_parts=parts,
        new_code=code,
        save_as_plain=True,
    )

    assert result["type"] == "saved"
    assert result["ok"] is True
    assert result["save_as_plain"] is True
    assert "Saved without =PY()" in result["status_ok_text"]
    assert cell.getString() == code
    assert cell.getFormula() == ""
    assert doc.calculate_all_count == 1


def test_follow_code_ref_save_writes_a1_keeps_formula_ref():
    doc = CalcDocStub()
    sheet = doc.getSheets().getByIndex(0)
    code_cell = sheet.getCellByPosition(0, 0)
    formula_cell = sheet.getCellByPosition(1, 0)
    code_cell.setString("result = 42")
    formula_cell.setFormula("=PY($A$1; C1:C10)")
    parts = parse_python_formula(formula_cell.getFormula())
    assert parts is not None
    assert parts.code == "$A$1"

    resolved = _resolve_code_ref_cell(doc, parts.code)
    assert resolved is code_cell

    result = _apply_cell_save(
        doc,
        formula_cell,
        parsed_parts=parts,
        new_code="result = 99",
        save_as_plain=False,
        data_binding_text="C1:C10",
        code_cell=code_cell,
        code_ref=parts.code,
    )
    assert result["ok"] is True
    assert result["save_as_plain"] is False
    assert "without =PY()" not in result.get("status_ok_text", "")
    assert "Saved code in $A$1" in result.get("status_ok_text", "")
    assert formula_cell.getFormula().startswith("=PY(")
    assert code_cell.getString() == "result = 99"
    reparsed = parse_python_formula(formula_cell.getFormula())
    assert reparsed is not None
    assert py_code_arg_is_cell_ref(reparsed.code)
    assert "C1:C10" in formula_cell.getFormula()
    assert "$A$1" in formula_cell.getFormula()
    assert formula_cell.getFormula().count(")") == 1
    assert "result = 99" not in formula_cell.getFormula()
    assert doc.calculate_all_count == 1


def test_follow_plain_flag_still_sends_data_text():
    doc = CalcDocStub()
    sheet = doc.getSheets().getByIndex(0)
    code_cell = sheet.getCellByPosition(0, 0)
    formula_cell = sheet.getCellByPosition(1, 0)
    code_cell.setString("result = 42")
    formula_cell.setFormula("=PY($A$1; C1:C10)")
    parts = parse_python_formula(formula_cell.getFormula())
    assert parts is not None
    assert parts.code == "$A$1"

    resolved = _resolve_code_ref_cell(doc, parts.code)
    assert resolved is code_cell

    result = _apply_cell_save(
        doc,
        formula_cell,
        parsed_parts=parts,
        new_code="result = 99",
        save_as_plain=True,
        data_binding_text="C1:C10",
        code_cell=code_cell,
        code_ref=parts.code,
    )
    assert result["ok"] is True
    assert code_cell.getString() == "result = 99"
    formula = formula_cell.getFormula()
    assert "$A$1" in formula
    assert "C1:C10" in formula
    assert formula.count(")") == 1
    assert "result = 99" not in formula


def test_follow_code_ref_save_can_change_data_binding():
    doc = CalcDocStub()
    sheet = doc.getSheets().getByIndex(0)
    code_cell = sheet.getCellByPosition(0, 0)
    formula_cell = sheet.getCellByPosition(1, 0)
    code_cell.setString("result = np.sum(data)")
    formula_cell.setFormula("=PY($A$1; C1:C10)")
    parts = parse_python_formula(formula_cell.getFormula())
    assert parts is not None

    result = _apply_cell_save(
        doc,
        formula_cell,
        parsed_parts=parts,
        new_code="result = np.mean(data)",
        save_as_plain=False,
        data_binding_text="D1:D20",
        code_cell=code_cell,
        code_ref=parts.code,
    )
    assert result["ok"] is True
    assert code_cell.getString() == "result = np.mean(data)"
    formula = formula_cell.getFormula()
    assert "$A$1" in formula
    assert "D1:D20" in formula
    assert "C1:C10" not in formula
    assert "np.mean" not in formula
    assert formula.count(")") == 1


def test_follow_code_ref_empty_data_keeps_formula_and_one_paren():
    doc = CalcDocStub()
    sheet = doc.getSheets().getByIndex(0)
    code_cell = sheet.getCellByPosition(0, 0)
    formula_cell = sheet.getCellByPosition(1, 0)
    code_cell.setString("result = 42")
    formula_cell.setFormula("=PY($A$1)")
    parts = parse_python_formula(formula_cell.getFormula())
    assert parts is not None
    original = formula_cell.getFormula()

    result = _apply_cell_save(
        doc,
        formula_cell,
        parsed_parts=parts,
        new_code="result = 99",
        save_as_plain=False,
        data_binding_text="",
        code_cell=code_cell,
        code_ref=parts.code,
    )
    assert result["ok"] is True
    assert code_cell.getString() == "result = 99"
    assert formula_cell.getFormula() == original
    assert formula_cell.getFormula().count(")") == 1
    assert not str(formula_cell.getFormula()).endswith("))")


def _follow_save(formula, new_code, data_binding_text):
    doc = CalcDocStub()
    formula_sheet = doc.getSheets().getByIndex(0)
    formula_cell = formula_sheet.getCellByPosition(1, 0)
    formula_cell.setFormula(formula)
    parts = parse_python_formula(formula_cell.getFormula())
    assert parts is not None
    resolved = _resolve_code_ref_cell(doc, parts.code)
    assert resolved is not None
    result = _apply_cell_save(
        doc,
        formula_cell,
        parsed_parts=parts,
        new_code=new_code,
        save_as_plain=False,
        data_binding_text=data_binding_text,
        code_cell=resolved,
        code_ref=parts.code,
    )
    assert result["ok"] is True
    return doc, formula_cell, resolved, parts


def test_follow_preserves_relative_and_mixed_abs_tokens_on_data_change():
    for token in ("A1", "$A1", "A$1", "$A$1"):
        _unused_doc, formula_cell, code_cell, parts = _follow_save(
            f"=PY({token}; C1:C10)",
            "result = 1",
            "D1:D5",
        )
        assert parts.code == token
        formula = formula_cell.getFormula()
        assert token in formula
        assert "D1:D5" in formula
        assert "C1:C10" not in formula
        assert formula.count(")") == 1
        assert code_cell.getString() == "result = 1"


def test_follow_sheet_qualified_ref_resolves_other_sheet():
    sheet1 = CalcSheetStub("Sheet1")
    sheet2 = CalcSheetStub("Sheet2")
    doc = CalcDocStub(sheets=[sheet1, sheet2])
    code_cell = sheet2.getCellByPosition(0, 0)
    code_cell.setString("result = 7")
    formula_cell = sheet1.getCellByPosition(1, 0)
    formula_cell.setFormula("=PY(Sheet2.A1; C1:C10)")
    parts = parse_python_formula(formula_cell.getFormula())
    assert parts is not None
    assert parts.code == "Sheet2.A1"
    resolved = _resolve_code_ref_cell(doc, parts.code)
    assert resolved is code_cell

    result = _apply_cell_save(
        doc,
        formula_cell,
        parsed_parts=parts,
        new_code="result = 8",
        save_as_plain=False,
        data_binding_text="D1:D2",
        code_cell=code_cell,
        code_ref=parts.code,
    )
    assert result["ok"] is True
    assert code_cell.getString() == "result = 8"
    formula = formula_cell.getFormula()
    assert "Sheet2.A1" in formula
    assert "D1:D2" in formula
    assert formula.count(")") == 1


def test_follow_quoted_sheet_name_resolves():
    data_sheet = CalcSheetStub("My Sheet")
    formula_sheet = CalcSheetStub("Sheet1")
    doc = CalcDocStub(sheets=[formula_sheet, data_sheet])
    code_cell = data_sheet.getCellByPosition(1, 1)
    code_cell.setString("result = 3")
    formula_cell = formula_sheet.getCellByPosition(0, 0)
    formula_cell.setFormula("=PY('My Sheet'.$B$2; C1:C10)")
    parts = parse_python_formula(formula_cell.getFormula())
    assert parts is not None
    resolved = _resolve_code_ref_cell(doc, parts.code)
    assert resolved is code_cell

    result = _apply_cell_save(
        doc,
        formula_cell,
        parsed_parts=parts,
        new_code="result = 4",
        save_as_plain=False,
        data_binding_text="D1:D2",
        code_cell=code_cell,
        code_ref=parts.code,
    )
    assert result["ok"] is True
    assert code_cell.getString() == "result = 4"
    formula = formula_cell.getFormula()
    assert "'My Sheet'.$B$2" in formula
    assert "D1:D2" in formula
    assert formula.count(")") == 1


def test_follow_apostrophe_sheet_name_resolves():
    data_sheet = CalcSheetStub("O'Brien")
    formula_sheet = CalcSheetStub("Sheet1")
    doc = CalcDocStub(sheets=[formula_sheet, data_sheet])
    code_cell = data_sheet.getCellByPosition(1, 1)
    code_cell.setString("result = 3")
    formula_cell = formula_sheet.getCellByPosition(0, 0)
    formula_cell.setFormula("=PY('O''Brien'.$B$2; C1:C10)")
    parts = parse_python_formula(formula_cell.getFormula())
    assert parts is not None
    resolved = _resolve_code_ref_cell(doc, parts.code)
    assert resolved is code_cell

    result = _apply_cell_save(
        doc,
        formula_cell,
        parsed_parts=parts,
        new_code="result = 4",
        save_as_plain=False,
        data_binding_text="D1:D2",
        code_cell=code_cell,
        code_ref=parts.code,
    )
    assert result["ok"] is True
    assert code_cell.getString() == "result = 4"
    formula = formula_cell.getFormula()
    assert "'O''Brien'.$B$2" in formula
    assert "D1:D2" in formula
    assert formula.count(")") == 1


def test_follow_missing_sheet_resolves_none():
    doc = CalcDocStub()
    assert _resolve_code_ref_cell(doc, "Missing.A1") is None


def test_follow_clear_data_rewrites_to_ref_only():
    _unused_doc, formula_cell, code_cell, unused_parts = _follow_save(
        "=PY($A$1; C1:C10)",
        "result = 2",
        "",
    )
    formula = formula_cell.getFormula()
    assert code_cell.getString() == "result = 2"
    assert "C1:C10" not in formula
    assert "$A$1" in formula
    assert formula.count(")") == 1
    assert not formula.endswith("))")
    reparsed = parse_python_formula(formula)
    assert reparsed is not None
    assert py_code_arg_is_cell_ref(reparsed.code)


def test_follow_two_data_ranges_round_trip_and_edit():
    _unused_doc, formula_cell, code_cell, unused_parts = _follow_save(
        "=PY($A$1; B1:B10; C1:C10)",
        "result = data",
        "B1:B10, C1:C10",
    )
    assert "B1:B10" in formula_cell.getFormula()
    assert "C1:C10" in formula_cell.getFormula()
    assert code_cell.getString() == "result = data"

    _unused_doc, formula_cell, code_cell, unused_parts = _follow_save(
        "=PY($A$1; B1:B10; C1:C10)",
        "result = data",
        "D1:D5, E1:E5",
    )
    formula = formula_cell.getFormula()
    assert "$A$1" in formula
    assert "D1:D5" in formula
    assert "E1:E5" in formula
    assert "B1:B10" not in formula
    assert formula.count(")") == 1


def test_follow_python_alias_empty_data_keeps_formula():
    _unused_doc, formula_cell, code_cell, unused_parts = _follow_save(
        "=PYTHON($A$1)",
        "result = 4",
        "",
    )
    assert code_cell.getString() == "result = 4"
    assert formula_cell.getFormula() == "=PYTHON($A$1)"
    assert formula_cell.getFormula().count(")") == 1


def test_follow_alias_data_change_keeps_prefix():
    cases = (
        ("=PYTHON($A$1; C1:C10)", "=PYTHON("),
        ("=py($A$1; C1:C10)", "=py("),
        ("=PYTHON ($A$1; C1:C10)", "=PYTHON ("),
        ("=ORG.EXTENSION.WRITERAGENT.PYTHONFUNCTION.PYTHON($A$1; C1:C10)", "=ORG.EXTENSION.WRITERAGENT.PYTHONFUNCTION.PYTHON("),
    )
    for formula_in, expected_prefix in cases:
        _unused_doc, formula_cell, _unused_code_cell, _unused_parts = _follow_save(
            formula_in,
            "result = 1",
            "D1:D5",
        )
        formula = formula_cell.getFormula()
        assert formula.startswith(expected_prefix)
        assert "$A$1" in formula
        assert "D1:D5" in formula
        assert "C1:C10" not in formula
        assert formula.count(")") == 1


def test_text_cell_py_literal_is_not_a_formula():
    from unittest.mock import MagicMock
    from plugin.calc.python.editor import (
        _cell_has_unparsed_python,
        _load_cell_editor_code,
        _parse_cell_python_formula,
        editor_load_save_as_plain,
    )

    cell1 = MagicMock()
    cell1.getType.return_value = 2
    cell1.getFormula.return_value = "PY(x)"
    cell1.getPropertyValue.return_value = "PY(x)"
    cell1.getString.return_value = "PY(x)"

    code1, parts1, _source1 = _load_cell_editor_code(cell1)
    assert code1 == "PY(x)"
    assert parts1 is None
    assert _cell_has_unparsed_python(cell1) is False
    assert editor_load_save_as_plain(parsed_parts=None, initial_code="PY(x)") is True

    cell2 = MagicMock()
    cell2.getType.return_value = 2
    cell2.getFormula.return_value = '=PY("x")'
    cell2.getPropertyValue.return_value = '=PY("x")'
    cell2.getString.return_value = "literal text"

    code2, parts2, _source2 = _load_cell_editor_code(cell2)
    assert code2 == "literal text"
    assert parts2 is None

    cell_stub = CalcCellStub()
    cell_stub.setFormula('=PY("result = 1")')
    code_stub, parts_stub, _source_stub = _load_cell_editor_code(cell_stub)
    assert code_stub == "result = 1"
    assert parts_stub is not None

    mock_cell = MagicMock()
    mock_cell.getFormula.return_value = "PY(x)"
    _c, mock_parts, _s = _parse_cell_python_formula(mock_cell)
    assert mock_parts is None


def test_get_active_calc_cell_uses_selection_sheet_index():
    from types import SimpleNamespace
    from unittest.mock import MagicMock
    from plugin.calc.python.editor import _get_active_calc_cell

    sheet0 = CalcSheetStub("Sheet1")
    sheet1 = CalcSheetStub("Sheet2")
    sheet1.getCellByPosition(0, 0).setFormula('=PY("result = 2")')

    doc = CalcDocStub(sheets=[sheet0, sheet1], active_sheet=sheet0)

    ctrl = MagicMock()
    ctrl.getActiveSheet.return_value = sheet0
    ctrl.getModel.return_value = doc

    sel = MagicMock()
    sel.getRangeAddress.return_value = SimpleNamespace(
        Sheet=1, StartColumn=0, StartRow=0, EndColumn=0, EndRow=0
    )
    ctrl.getSelection.return_value = sel
    doc.getCurrentController = lambda: ctrl

    ctx = MagicMock()
    desktop = MagicMock()
    desktop.getCurrentComponent.return_value = doc
    desktop.getCurrentFrame.return_value.getController.return_value.getModel.return_value = doc
    smgr = MagicMock()
    smgr.createInstanceWithContext.return_value = desktop
    ctx.ServiceManager = smgr

    res = _get_active_calc_cell(ctx)
    assert res is not None
    model, cell, formula = res
    assert formula == '=PY("result = 2")'
    assert cell._sheet is sheet1


def test_get_active_calc_cell_bad_sheet_does_not_use_active_sheet():
    from types import SimpleNamespace
    from unittest.mock import MagicMock
    from plugin.calc.python.editor import _get_active_calc_cell

    sheet0 = CalcSheetStub("Sheet1")
    sheet1 = CalcSheetStub("Sheet2")
    sheet1.getCellByPosition(0, 0).setFormula('=PY("result = 2")')

    # Case 1: Sheet=9 (only two sheets)
    doc1 = CalcDocStub(sheets=[sheet0, sheet1], active_sheet=sheet0)
    ctrl1 = MagicMock()
    ctrl1.getActiveSheet.return_value = sheet0
    ctrl1.getModel.return_value = doc1

    sel1 = MagicMock()
    sel1.getRangeAddress.return_value = SimpleNamespace(
        Sheet=9, StartColumn=0, StartRow=0, EndColumn=0, EndRow=0
    )
    ctrl1.getSelection.return_value = sel1
    doc1.getCurrentController = lambda: ctrl1

    ctx1 = MagicMock()
    desktop1 = MagicMock()
    desktop1.getCurrentComponent.return_value = doc1
    desktop1.getCurrentFrame.return_value.getController.return_value.getModel.return_value = doc1
    smgr1 = MagicMock()
    smgr1.createInstanceWithContext.return_value = desktop1
    ctx1.ServiceManager = smgr1

    assert _get_active_calc_cell(ctx1) is None

    # Case 2: Sheet=1 but getByIndex raises RuntimeError
    doc2 = CalcDocStub(sheets=[sheet0, sheet1], active_sheet=sheet0)
    mock_sheets = MagicMock()
    mock_sheets.getCount.return_value = 2
    mock_sheets.getByIndex.side_effect = RuntimeError("sheet access failure")
    doc2.getSheets = lambda: mock_sheets

    ctrl2 = MagicMock()
    ctrl2.getActiveSheet.return_value = sheet0
    ctrl2.getModel.return_value = doc2

    sel2 = MagicMock()
    sel2.getRangeAddress.return_value = SimpleNamespace(
        Sheet=1, StartColumn=0, StartRow=0, EndColumn=0, EndRow=0
    )
    ctrl2.getSelection.return_value = sel2
    doc2.getCurrentController = lambda: ctrl2

    ctx2 = MagicMock()
    desktop2 = MagicMock()
    desktop2.getCurrentComponent.return_value = doc2
    desktop2.getCurrentFrame.return_value.getController.return_value.getModel.return_value = doc2
    smgr2 = MagicMock()
    smgr2.createInstanceWithContext.return_value = desktop2
    ctx2.ServiceManager = smgr2

    assert _get_active_calc_cell(ctx2) is None


def test_follow_save_uses_one_undo_context():
    doc = CalcDocStub()
    sheet = doc.getSheets().getByIndex(0)
    code_cell = sheet.getCellByPosition(0, 0)
    formula_cell = sheet.getCellByPosition(1, 0)
    code_cell.setString("result = 42")
    formula_cell.setFormula("=PY($A$1; C1:C10)")
    parts = parse_python_formula(formula_cell.getFormula())
    assert parts is not None

    calls: list[tuple[str, Any]] = []

    class UndoManagerStub:
        def enterUndoContext(self, title: str) -> None:
            calls.append(("enter", title))

        def leaveUndoContext(self) -> None:
            calls.append(("leave", None))

        def isInContext(self) -> bool:
            return False

        def isUndoEnabled(self) -> bool:
            return True

    um = UndoManagerStub()
    doc.getUndoManager = lambda: um

    result = _apply_cell_save(
        doc,
        formula_cell,
        parsed_parts=parts,
        new_code="result = 99",
        save_as_plain=False,
        data_binding_text="C1:C10",
        code_cell=code_cell,
        code_ref=parts.code,
    )

    assert result["ok"] is True
    enters = [c for c in calls if c[0] == "enter"]
    leaves = [c for c in calls if c[0] == "leave"]
    assert len(enters) == 1
    assert len(leaves) == 1
    assert calls[-1][0] == "leave"
    assert enters[0][1] and isinstance(enters[0][1], str)
    assert code_cell.getString() == "result = 99"
    assert formula_cell.getFormula().startswith("=PY(")


def test_plain_save_reconciles_geometric_registry():
    from unittest.mock import patch

    doc = CalcDocStub()
    cell = CalcCellStub()
    with patch("plugin.calc.python.geometric_recalc.after_py_cell_save") as mock_after:
        result = _apply_cell_save(
            doc,
            cell,
            parsed_parts=None,
            new_code="x = 10",
            save_as_plain=True,
        )
        assert result["ok"] is True
        mock_after.assert_called_once_with(doc, cell)


def test_formula_save_skips_unparsed_probe_when_data_present():
    from unittest.mock import patch
    import plugin.calc.python.editor as pe

    doc = CalcDocStub()
    cell = CalcCellStub()

    with patch.object(pe, "_cell_has_unparsed_python", return_value=False) as mock_probe:
        result = _apply_cell_save(
            doc,
            cell,
            parsed_parts=None,
            new_code="x = 1",
            save_as_plain=False,
            data_binding_text="A1:A5",
        )
        assert result["ok"] is True
        mock_probe.assert_not_called()

        result2 = _apply_cell_save(
            doc,
            cell,
            parsed_parts=None,
            new_code="x = 2",
            save_as_plain=False,
            data_binding_text=None,
        )
        assert result2["ok"] is True
        mock_probe.assert_called_once_with(cell)


