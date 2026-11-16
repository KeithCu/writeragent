# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""UNO tests for Calc sheet-name quoting in formulas."""

from plugin.testing_runner import native_test
from plugin.tests.testing_utils import with_native_doc


@native_test
@with_native_doc("calc")
def test_qualified_address_computes_in_formula_uno(ctx, doc):
    from plugin.calc.address_utils import qualify_a1

    del ctx
    sheets = doc.getSheets()
    base_sheet = sheets.getByIndex(0)
    names = ("Q1-2026", "2024", "Data Sheet", "O'Brien")
    inserted: list[str] = []
    try:
        for idx, name in enumerate(names):
            if not sheets.hasByName(name):
                sheets.insertNewByName(name, sheets.getCount())
                inserted.append(name)
            sheets.getByName(name).getCellByPosition(1, 1).setValue(7.0)

            cell = base_sheet.getCellByPosition(0, idx)
            cell.setFormula("=" + qualify_a1(name, "B2"))
            assert cell.getError() == 0, f"{name}: formula error {cell.getError()}"
            assert cell.getValue() == 7.0, f"{name}: expected 7.0, got {cell.getValue()}"
    finally:
        for name in inserted:
            if sheets.hasByName(name):
                sheets.removeByName(name)
