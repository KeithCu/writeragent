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


def _addr_tuple(cell):
    addr = cell.getCellAddress()
    return (int(addr.Sheet), int(addr.Column), int(addr.Row))


def _follow_save_live(doc, formula_cell, code_cell, new_code, data_binding_text):
    from plugin.calc.python.editor import _apply_cell_save
    from plugin.calc.python.formula_edit import parse_python_formula, py_code_arg_is_cell_ref

    stored = str(formula_cell.getFormula() or "")
    parts = parse_python_formula(stored)
    assert parts is not None, stored
    assert py_code_arg_is_cell_ref(parts.code), parts.code
    result = _apply_cell_save(
        doc,
        formula_cell,
        parsed_parts=parts,
        new_code=new_code,
        save_as_plain=False,
        data_binding_text=data_binding_text,
        code_cell=code_cell,
        code_ref=parts.code,
    )
    assert result.get("ok") is True, result
    return stored, parts, str(formula_cell.getFormula() or "")


@native_test
@with_native_doc("calc")
def test_follow_sheet_qualified_refs(ctx, doc):
    """Sheet2.A1 vs Sheet1.A1 must not collapse; quoted sheet names resolve; missing sheets do not."""
    from plugin.calc.python.editor import _resolve_code_ref_cell
    from plugin.calc.python.formula_edit import parse_python_formula, py_code_arg_is_cell_ref

    sheets = doc.getSheets()
    if not sheets.hasByName("Sheet2"):
        sheets.insertNewByName("Sheet2", sheets.getCount())
    if not sheets.hasByName("My Sheet"):
        sheets.insertNewByName("My Sheet", sheets.getCount())
    sheet1 = sheets.getByIndex(0)
    sheet2 = sheets.getByName("Sheet2")
    named = sheets.getByName("My Sheet")

    code_a1 = sheet2.getCellByPosition(0, 0)
    formula_a1 = sheet1.getCellByPosition(0, 0)
    code_a1.setString("result = 7")
    formula_a1.setFormula("=PY(Sheet2.A1)")
    _unused_stored, parts, after = _follow_save_live(doc, formula_a1, code_a1, "result = 8", "")
    resolved = _resolve_code_ref_cell(doc, parts.code)
    assert resolved is not None
    assert _addr_tuple(resolved) == _addr_tuple(code_a1)
    assert _addr_tuple(resolved) != _addr_tuple(formula_a1)
    assert code_a1.getString() == "result = 8"
    assert "Sheet2" in after
    reparsed = parse_python_formula(after)
    assert reparsed is not None
    assert py_code_arg_is_cell_ref(reparsed.code)
    assert after.count(")") == 1

    code_b2 = named.getCellByPosition(1, 1)
    formula_b1 = sheet1.getCellByPosition(1, 0)
    code_b2.setString("result = 3")
    formula_b1.setFormula("=PY('My Sheet'.$B$2; C1:C1)")
    _unused_stored, quoted_parts, quoted_after = _follow_save_live(
        doc, formula_b1, code_b2, "result = 4", "D1:D1"
    )
    resolved_b2 = _resolve_code_ref_cell(doc, quoted_parts.code)
    assert resolved_b2 is not None
    assert _addr_tuple(resolved_b2) == _addr_tuple(code_b2)
    assert code_b2.getString() == "result = 4"
    assert "My Sheet" in quoted_after
    assert "D1" in quoted_after
    assert quoted_after.count(")") == 1

    assert _resolve_code_ref_cell(doc, "Missing.A1") is None


@native_test
@with_native_doc("calc")
def test_follow_data_binding_change_clear_and_two_ranges(ctx, doc):
    """Data lives on the formula cell: change, clear, and two-range rewrite keep the code ref."""
    from plugin.calc.python.formula_edit import parse_python_formula, py_code_arg_is_cell_ref

    sheet = doc.getSheets().getByIndex(0)
    code_cell = sheet.getCellByPosition(0, 0)
    formula_cell = sheet.getCellByPosition(1, 0)
    code_cell.setString("result = np.sum(data)")
    formula_cell.setFormula("=PY($A$1; C1:C1)")
    _unused, parts, after_change = _follow_save_live(
        doc, formula_cell, code_cell, "result = np.mean(data)", "D1:D1"
    )
    assert "$A$1" in after_change or parts.code.replace("$", "") in after_change.replace("$", "")
    if "$" in parts.code:
        assert "$" in after_change, after_change
    assert "D1" in after_change
    assert "C1" not in after_change
    assert after_change.count(")") == 1
    assert "np.mean" not in after_change
    assert code_cell.getString() == "result = np.mean(data)"

    formula_cell.setFormula("=PY($A$1; C1:C1)")
    _unused, unused_parts, after_clear = _follow_save_live(
        doc, formula_cell, code_cell, "result = 2", ""
    )
    assert "C1" not in after_clear
    assert "$A$1" in after_clear or "A1" in after_clear
    assert after_clear.count(")") == 1
    assert not after_clear.endswith("))")
    reparsed = parse_python_formula(after_clear)
    assert reparsed is not None
    assert py_code_arg_is_cell_ref(reparsed.code)

    formula_cell.setFormula("=PY($A$1; B1:B2; C1:C2)")
    _unused, unused_two, after_two = _follow_save_live(
        doc, formula_cell, code_cell, "result = data", "D1:D2, E1:E2"
    )
    assert "D1" in after_two and "E1" in after_two
    assert "B1" not in after_two
    assert after_two.count(")") == 1


@native_test
@with_native_doc("calc")
def test_follow_python_alias_and_relative_a1(ctx, doc):
    """=PYTHON($A$1) and relative =PY(A1) follow; mixed $ tokens stay on data rewrite."""
    from plugin.calc.python.formula_edit import parse_python_formula, py_code_arg_is_cell_ref

    sheet = doc.getSheets().getByIndex(0)
    code_cell = sheet.getCellByPosition(0, 0)
    formula_cell = sheet.getCellByPosition(1, 0)
    code_cell.setString("result = 5")
    formula_cell.setFormula("=PYTHON($A$1)")
    before = str(formula_cell.getFormula() or "")
    _unused, parts, after_alias = _follow_save_live(
        doc, formula_cell, code_cell, "result = 6", ""
    )
    assert code_cell.getString() == "result = 6"
    assert after_alias == before
    assert py_code_arg_is_cell_ref(parse_python_formula(after_alias).code)

    formula_cell.setFormula("=PY(A1; C1:C1)")
    _unused_rel, rel_parts, after_rel = _follow_save_live(
        doc, formula_cell, code_cell, "result = 1", "D1:D1"
    )
    token = rel_parts.code
    assert token.replace("$", "") == "A1"
    assert token in after_rel
    assert "D1" in after_rel
    assert after_rel.count(")") == 1

    formula_cell.setFormula("=PY($A1; C1:C1)")
    _unused, mixed_parts, after_mixed = _follow_save_live(
        doc, formula_cell, code_cell, "result = 1", "E1:E1"
    )
    assert mixed_parts.code in after_mixed
    assert "E1" in after_mixed
    assert after_mixed.count(")") == 1

