# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""UNO: geometric splice round-trips live getFormula / setFormula.

Probe + assertions. Confirms Calc's stored spelling (equals, $ , prefix)
so rebuild_formula_with_data_args does not guess from CalcDocStub.
"""

from __future__ import annotations

from plugin.testing_runner import native_test
from plugin.tests.testing_utils import with_native_doc


def _store(cell, formula: str) -> str:
    cell.setFormula(formula)
    return str(cell.getFormula() or "")


@native_test
@with_native_doc("calc")
def test_geometric_formula_io_roundtrip(ctx, doc):
    from plugin.calc.python.formula_edit import (
        parse_python_formula,
        py_formula_has_unquoted_code_ref,
    )
    from plugin.calc.python.geometric_recalc import (
        formula_data_args,
        rebuild_formula_with_data_args,
    )

    sheet = doc.getSheets().getByIndex(0)
    a1 = sheet.getCellByPosition(0, 0)
    a2 = sheet.getCellByPosition(0, 1)
    b1 = sheet.getCellByPosition(1, 0)
    c5 = sheet.getCellByPosition(2, 4)
    a1.setString("result = 1")
    c5.setValue(5)

    # --- Probe: what does getFormula actually store? ---
    # Confirmed on Classic (Linux): =py("…"; $C$5) — has '=', lowercase py,
    # keeps $, no sheet prefix, not OriginalName. Splice uses parts.prefix.
    samples = {
        "quoted_abs": _store(a2, '=PY("y"; $C$5)'),
        "quoted_range": _store(b1, '=PY("np.mean(data)"; B1:B10)'),
        "code_in_cell": _store(sheet.getCellByPosition(3, 0), "=PY($A$1; B1:B10)"),
    }
    print("geometric getFormula probe:", samples)

    # 1) =PY("y"; $C$5) attach predecessor — $C$5 stays absolute.
    stored = samples["quoted_abs"]
    parts = parse_python_formula(stored)
    assert parts is not None, stored
    assert parts.prefix, stored
    old_args = formula_data_args(stored)
    assert old_args is not None, stored
    rebuilt = rebuild_formula_with_data_args(stored, old_args + ["A1"])
    assert rebuilt is not None
    assert rebuilt.startswith("="), rebuilt
    assert rebuilt.startswith(parts.prefix), (parts.prefix, rebuilt)
    a2.setFormula(rebuilt)
    after = str(a2.getFormula() or "")
    reparsed = parse_python_formula(after)
    assert reparsed is not None, after
    after_args = formula_data_args(after)
    assert after_args is not None and len(after_args) >= 2, after
    # Absolute user ref must survive setFormula+getFormula (Calc may keep $C$5
    # or add a sheet; do not accept a relative C5 if $ was stored).
    first_arg = after_args[0]
    stored_first = old_args[0]
    if "$" in stored_first:
        assert "$" in first_arg, (stored, rebuilt, after)
        assert first_arg.replace("$", "").upper().endswith("C5"), after
    else:
        # Calc itself dropped $ on first store — splice must not invent a
        # different cell, and must still parse.
        assert first_arg.replace("$", "").upper().endswith("C5"), after

    # 2) Quoted code + range: splice appends a pred; getFormula still parses.
    stored_range = samples["quoted_range"]
    range_parts = parse_python_formula(stored_range)
    assert range_parts is not None, stored_range
    range_args = formula_data_args(stored_range)
    assert range_args is not None, stored_range
    rebuilt_range = rebuild_formula_with_data_args(stored_range, range_args + ["A1"])
    assert rebuilt_range is not None
    assert rebuilt_range.startswith("="), rebuilt_range
    assert "np.mean(data)" in rebuilt_range
    b1.setFormula(rebuilt_range)
    after_range = str(b1.getFormula() or "")
    again = parse_python_formula(after_range)
    assert again is not None, after_range
    assert again.code == "np.mean(data)"
    after_range_args = formula_data_args(after_range)
    assert after_range_args is not None and len(after_range_args) == len(range_args) + 1, after_range

    # 3) Unquoted code-in-cell keeps the unquoted token (or Calc's stored form).
    stored_ref = samples["code_in_cell"]
    assert py_formula_has_unquoted_code_ref(stored_ref), stored_ref
    ref_parts = parse_python_formula(stored_ref)
    assert ref_parts is not None, stored_ref
    ref_args = formula_data_args(stored_ref)
    assert ref_args is not None, stored_ref
    rebuilt_ref = rebuild_formula_with_data_args(stored_ref, ref_args + ["C5"])
    assert rebuilt_ref is not None
    assert '"$A$1"' not in rebuilt_ref
    assert '"A1"' not in rebuilt_ref
    sheet.getCellByPosition(3, 0).setFormula(rebuilt_ref)
    after_ref = str(sheet.getCellByPosition(3, 0).getFormula() or "")
    assert py_formula_has_unquoted_code_ref(after_ref), after_ref
    after_ref_parts = parse_python_formula(after_ref)
    assert after_ref_parts is not None, after_ref
    assert after_ref_parts.code.replace("$", "").upper().endswith("A1"), after_ref
    if "$" in ref_parts.code:
        assert "$" in after_ref_parts.code, after_ref


def _pred(formula: str) -> str | None:
    from plugin.calc.python.geometric_recalc import formula_data_args, local_a1

    args = formula_data_args(formula)
    if not args:
        return None
    return local_a1(args[-1])


def _enable_geometric_flag():
    import plugin.calc.python.geometric_recalc as geo

    previous = geo.geometric_flag_enabled
    geo.geometric_flag_enabled = lambda: True
    return previous


def _restore_geometric_flag(previous) -> None:
    import plugin.calc.python.geometric_recalc as geo

    geo.geometric_flag_enabled = previous
    geo.reset_geometric_runtime_for_tests()


def _flush(ctx, doc, sheet) -> None:
    from plugin.calc.python.sheet_modify import flush_sheet_modify_pass_for_tests

    flush_sheet_modify_pass_for_tests(ctx, doc, sheet)


@native_test
@with_native_doc("calc")
def test_geometric_insert_delete_undo_three_cell_column(ctx, doc):
    """Phase 3: insert/delete retargets; successor-becomes-first removes the field; undo restores."""
    from plugin.calc.python.geometric_recalc import reset_geometric_runtime_for_tests

    reset_geometric_runtime_for_tests()
    previous = _enable_geometric_flag()
    try:
        sheet = doc.getSheets().getByIndex(0)
        a1 = sheet.getCellByPosition(0, 0)
        a2 = sheet.getCellByPosition(0, 1)
        a3 = sheet.getCellByPosition(0, 2)
        a1.setFormula('=PY("first")')
        a3.setFormula('=PY("third")')
        _flush(ctx, doc, sheet)
        assert _pred(str(a3.getFormula() or "")) == "A1", a3.getFormula()
        assert _pred(str(a1.getFormula() or "")) is None

        # 1) Insert PY in the middle → successor names the new predecessor.
        a2.setFormula('=PY("mid")')
        _flush(ctx, doc, sheet)
        assert _pred(str(a2.getFormula() or "")) == "A1", a2.getFormula()
        assert _pred(str(a3.getFormula() or "")) == "A2", a3.getFormula()

        # 2) Delete middle → successor retargets to A1.
        a2.setFormula("")
        _flush(ctx, doc, sheet)
        assert _pred(str(a3.getFormula() or "")) == "A1", a3.getFormula()

        # 3) Undo restores the inserted cell and A3's ;A2 (hidden under the delete).
        um = doc.getUndoManager()
        assert um is not None
        if um.isUndoPossible():
            um.undo()
            # Either one undo restores both (hidden) or the user cell is back.
            restored = str(a2.getFormula() or "")
            if "PY" in restored.upper() or "PYTHON" in restored.upper():
                _flush(ctx, doc, sheet)
                assert _pred(str(a3.getFormula() or "")) == "A2", (
                    a2.getFormula(),
                    a3.getFormula(),
                )

        # Successor-becomes-first → remove-field.
        a2.setFormula("")
        a1.setFormula("")
        _flush(ctx, doc, sheet)
        assert _pred(str(a3.getFormula() or "")) is None, a3.getFormula()
    finally:
        _restore_geometric_flag(previous)


@native_test
@with_native_doc("calc")
def test_geometric_cap_hit_sheet_stays_unchained(ctx, doc):
    """Cap-hit: skip the whole sheet, do not chain the first 100, one msgbox."""
    from unittest.mock import patch

    from plugin.calc.python.cell_discovery import _MAX_PYTHON_CELLS_FOUND
    from plugin.calc.python.geometric_recalc import reset_geometric_runtime_for_tests

    reset_geometric_runtime_for_tests()
    previous = _enable_geometric_flag()
    try:
        sheet = doc.getSheets().getByIndex(0)
        for i in range(_MAX_PYTHON_CELLS_FOUND + 1):
            sheet.getCellByPosition(0, i).setFormula(f'=PY("x{i}")')
        with patch("plugin.chatbot.dialogs.msgbox") as mock_box:
            _flush(ctx, doc, sheet)
            assert mock_box.call_count == 1
        # First successor stays unchained (whole sheet skipped).
        a2 = str(sheet.getCellByPosition(0, 1).getFormula() or "")
        assert _pred(a2) is None, a2
        a101 = str(sheet.getCellByPosition(0, _MAX_PYTHON_CELLS_FOUND).getFormula() or "")
        assert _pred(a101) is None, a101
    finally:
        _restore_geometric_flag(previous)
