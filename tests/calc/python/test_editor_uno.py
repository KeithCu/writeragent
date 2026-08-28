# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""UNO: Edit Python in Cell follow-ref save keeps =PY($A$1) as a cell ref."""

from __future__ import annotations

from plugin.testing_runner import native_test
from plugin.tests.testing_utils import with_native_doc


@native_test
@with_native_doc("calc")
def test_follow_ref_save_writes_code_cell_keeps_absolute_ref(ctx, doc):
    """Live Calc must keep $A$1 after follow-ref save (format_py_data_range strips $)."""
    from plugin.calc.python.editor import _apply_cell_save, _resolve_code_ref_cell
    from plugin.calc.python.formula_edit import parse_python_formula, py_code_arg_is_cell_ref

    sheet = doc.getSheets().getByIndex(0)
    code_cell = sheet.getCellByPosition(0, 0)
    formula_cell = sheet.getCellByPosition(1, 0)
    data_cell = sheet.getCellByPosition(2, 0)
    code_cell.setString("result = 42")
    data_cell.setValue(1)
    formula_cell.setFormula("=PY($A$1; C1:C1)")

    stored = str(formula_cell.getFormula() or "")
    parts = parse_python_formula(stored)
    assert parts is not None, stored
    assert py_code_arg_is_cell_ref(parts.code), parts.code

    resolved = _resolve_code_ref_cell(doc, parts.code)
    assert resolved is not None
    addr = resolved.getCellAddress()
    assert int(addr.Column) == 0 and int(addr.Row) == 0

    result = _apply_cell_save(
        doc,
        formula_cell,
        parsed_parts=parts,
        new_code="result = 99",
        save_as_plain=False,
        data_binding_text="C1:C1",
        code_cell=code_cell,
        code_ref=parts.code,
    )
    assert result.get("ok") is True
    assert code_cell.getString() == "result = 99"
    after = str(formula_cell.getFormula() or "")
    reparsed = parse_python_formula(after)
    assert reparsed is not None, after
    assert py_code_arg_is_cell_ref(reparsed.code), after
    assert "99" not in after
    # Same A1 (absolute $ kept when Calc still has it on the original formula).
    assert reparsed.code.replace("$", "") == parts.code.replace("$", "")
    if "$" in parts.code:
        assert "$" in reparsed.code, after
    assert "C1" in after


def _py_cell_error_code(cell) -> int:
    """Calc error constant, or 0 when the cell is not an error."""
    try:
        from com.sun.star.table import CellContentType

        if cell.getType() != CellContentType.VALUE:
            # FORMULA that failed still reports VALUE with an error code.
            pass
        err = int(cell.getError())
        return err
    except Exception:
        return 0


@native_test
@with_native_doc("calc")
def test_follow_ref_save_empty_data_still_evaluates(ctx, doc):
    """Native follow save always sends Data: '' — rewriting =PY($A$1) must not 508."""
    from plugin.calc.python.editor import _apply_cell_save
    from plugin.calc.python.formula_edit import parse_python_formula, py_code_arg_is_cell_ref

    sheet = doc.getSheets().getByIndex(0)
    code_cell = sheet.getCellByPosition(0, 0)
    formula_cell = sheet.getCellByPosition(1, 0)
    code_cell.setString("result = 42")
    formula_cell.setFormula("=PY($A$1)")
    doc.calculateAll()
    before_formula = str(formula_cell.getFormula() or "")
    before_err = _py_cell_error_code(formula_cell)
    before_val = formula_cell.getValue()
    parts = parse_python_formula(before_formula)
    assert parts is not None, before_formula
    assert py_code_arg_is_cell_ref(parts.code), parts.code

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
    assert result.get("ok") is True
    assert code_cell.getString() == "result = 99"
    after = str(formula_cell.getFormula() or "")
    reparsed = parse_python_formula(after)
    assert reparsed is not None, after
    assert py_code_arg_is_cell_ref(reparsed.code), after
    assert after.count(")") == 1, after
    assert not after.endswith("))"), after
    doc.calculateAll()
    after_err = _py_cell_error_code(formula_cell)
    after_val = formula_cell.getValue()
    assert after_err == 0, (
        f"follow-ref save turned {before_formula!r} (err={before_err}, val={before_val}) "
        f"into {after!r} (err={after_err}, val={after_val})"
    )
    # Headless PY($A$1) may not run the worker (val stays 0); GUI does. When
    # setup evaluated, save must update the result.
    if float(before_val) == 42.0:
        assert float(after_val) == 99.0, f"after {after!r} val={after_val} err={after_err}"

