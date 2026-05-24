# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for Monaco editor save modes (formula vs plain text)."""

from __future__ import annotations

from unittest.mock import MagicMock

from plugin.calc.python_editor import (
    _apply_cell_save,
    build_editor_formula_save,
)
from plugin.calc.python_formula_edit import parse_python_formula


def test_build_editor_formula_save_new_cell_with_data_binding():
    result = build_editor_formula_save(
        parsed_parts=None,
        new_code="np.mean(data)",
        cell_has_unparsed_python=False,
        data_binding_text="A1:A10",
    )
    assert result == '=PYTHON("np.mean(data)";A1:A10)'


def test_build_editor_formula_save_multi_range_from_textbox():
    result = build_editor_formula_save(
        parsed_parts=None,
        new_code="sum(d) for d in data_list",
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
    doc = MagicMock()
    cell = MagicMock()

    result = _apply_cell_save(
        doc,
        cell,
        parsed_parts=None,
        new_code="np.sum(data)",
        save_as_plain=False,
        data_binding_text="D1:D10",
    )

    assert result == {"type": "saved", "ok": True, "save_as_plain": False}
    formula = cell.setFormula.call_args[0][0]
    assert "D1:D10" in formula
    assert "np.sum(data)" in formula
    doc.calculateAll.assert_called_once()


def test_build_editor_formula_save_new_cell():
    result = build_editor_formula_save(
        parsed_parts=None,
        new_code="np.mean(data)",
        cell_has_unparsed_python=False,
    )
    assert result == '=PYTHON("np.mean(data)")'


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
    doc = MagicMock()
    cell = MagicMock()
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
    cell.setFormula.assert_called_once()
    cell.setString.assert_not_called()
    formula = cell.setFormula.call_args[0][0]
    assert "C1:C5" in formula
    assert "new" in formula
    reparsed = parse_python_formula(formula)
    assert reparsed is not None
    assert reparsed.code == "new"
    doc.calculateAll.assert_called_once()


def test_apply_cell_save_plain_text_mode():
    doc = MagicMock()
    cell = MagicMock()
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

    assert result == {"type": "saved", "ok": True, "save_as_plain": True}
    cell.setString.assert_called_once_with(code)
    cell.setFormula.assert_not_called()
    doc.calculateAll.assert_called_once()
