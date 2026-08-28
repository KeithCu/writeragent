# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for Monaco editor save modes (formula vs plain text)."""

from __future__ import annotations

from plugin.calc.python.editor import (
    _apply_cell_save,
    _resolve_code_ref_cell,
    build_editor_formula_save,
    editor_load_save_as_plain,
)
from plugin.calc.python.formula_edit import parse_python_formula, py_code_arg_is_cell_ref
from plugin.tests.testing_utils import CalcCellStub, CalcDocStub


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
    assert result == '=PY("x = 1")'


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
    assert result["save_as_plain"] is True
    assert code_cell.getString() == "result = 99"
    reparsed = parse_python_formula(formula_cell.getFormula())
    assert reparsed is not None
    assert py_code_arg_is_cell_ref(reparsed.code)
    assert "C1:C10" in formula_cell.getFormula()
    assert "$A$1" in formula_cell.getFormula()
    assert formula_cell.getFormula().count(")") == 1
    assert "result = 99" not in formula_cell.getFormula()
    assert doc.calculate_all_count == 1


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

