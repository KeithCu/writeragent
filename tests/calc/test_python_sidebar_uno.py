# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""UNO smoke tests for LibrePy Python sidebar cell navigation."""

from __future__ import annotations

from plugin.framework.uno_context import get_desktop
from plugin.testing_runner import native_test, setup, teardown

_test_doc = None
_test_ctx = None


@setup
def setup_python_sidebar_tests(ctx):
    global _test_doc, _test_ctx
    _test_ctx = ctx
    desktop = get_desktop(ctx)
    import uno

    hidden_prop = uno.createUnoStruct(
        "com.sun.star.beans.PropertyValue",
        Name="Hidden",
        Value=True,
    )
    _test_doc = desktop.loadComponentFromURL("private:factory/scalc", "_blank", 0, (hidden_prop,))


@teardown
def teardown_python_sidebar_tests(ctx):
    global _test_doc, _test_ctx
    if _test_doc:
        try:
            _test_doc.close(True)
        except Exception:
            pass
    _test_doc = None
    _test_ctx = None


@native_test
def test_list_and_navigate_python_cell():
    from plugin.calc.navigation import navigate_to_cell
    from plugin.calc.python.cell_discovery import list_python_cells_in_doc

    sheet = _test_doc.getSheets().getByIndex(0)
    sheet.setName("Data")
    cell = sheet.getCellByPosition(1, 2)  # B3
    cell.setFormula('=PY("result = 42")')
    _test_doc.calculateAll()

    found = list_python_cells_in_doc(_test_doc, active_sheet_only=True)
    assert any(c.address.endswith("B3") and "result = 42" in c.code for c in found), found

    assert navigate_to_cell(_test_doc, _test_ctx, "Data.B3") is True
    sel = _test_doc.getCurrentController().getSelection()
    addr = sel.getRangeAddress()
    assert addr.StartColumn == 1
    assert addr.StartRow == 2
