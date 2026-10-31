# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""UNO smoke tests for LibrePy Python sidebar cell navigation."""

from __future__ import annotations

from plugin.testing_runner import native_test
from plugin.tests.testing_utils import with_native_doc


@native_test
@with_native_doc("calc")
def test_list_and_navigate_python_cell(ctx, doc):
    from plugin.calc.navigation import navigate_to_cell
    from plugin.calc.python.cell_discovery import list_python_cells_in_doc

    sheet = doc.getSheets().getByIndex(0)
    sheet.setName("Data")
    cell = sheet.getCellByPosition(1, 2)  # B3
    cell.setFormula('=PY("result = 42")')
    doc.calculateAll()

    found = list_python_cells_in_doc(doc)
    assert any(c.address.endswith("B3") and "result = 42" in c.code for c in found), found

    assert navigate_to_cell(doc, ctx, "Data.B3") is True
    sel = doc.getCurrentController().getSelection()
    addr = sel.getRangeAddress()
    assert addr.StartColumn == 1
    assert addr.StartRow == 2
