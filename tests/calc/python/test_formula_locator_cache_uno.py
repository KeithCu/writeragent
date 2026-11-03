# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""UNO tests for formula locator with contiguous formulas and array formulas."""

from __future__ import annotations

from plugin.calc.python.formula_locator_cache import locate_formula_cell_in_doc
from plugin.testing_runner import native_test
from plugin.tests.testing_utils import with_native_doc


@native_test
@with_native_doc("calc")
def test_locate_fill_down_ambiguous_and_array_formula_unique(ctx, doc):
    sheet = doc.getSheets().getByIndex(0)

    # 1. Fill A1:A3 with the same =PY() formula -> locate must be ambiguous (None)
    code_fill = "result = 10"
    formula_fill = f'=PY("{code_fill}")'
    for r in range(3):
        sheet.getCellByPosition(0, r).setFormula(formula_fill)

    located_fill = locate_formula_cell_in_doc(doc, code_fill)
    assert located_fill is None, f"Expected ambiguous locate for fill-down, got: {located_fill}"

    # 2. Enter an array formula over A5:B6 -> locate must return top-left A5 (col=0, row=4)
    code_arr = "result = [[1, 2], [3, 4]]"
    formula_arr = f'=PY("{code_arr}")'
    arr_range = sheet.getCellRangeByPosition(0, 4, 1, 5)
    arr_range.setArrayFormula(formula_arr)

    located_arr = locate_formula_cell_in_doc(doc, code_arr)
    assert located_arr is not None, "Expected array formula to be located"
    found_sheet, found_cell, (r, c) = located_arr
    assert (r, c) == (4, 0), f"Expected top-left (row 4, col 0), got: ({r}, {c})"
