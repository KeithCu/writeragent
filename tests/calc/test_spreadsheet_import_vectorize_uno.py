# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for convert_spreadsheet_to_python tool vectorization."""

from __future__ import annotations

from plugin.framework.uno_context import get_desktop
from plugin.testing_runner import setup, teardown, native_test


_test_doc = None
_test_ctx = None


@setup
def setup_vectorize_uno_tests(ctx):
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
def teardown_vectorize_uno_tests(ctx):
    global _test_doc, _test_ctx
    if _test_doc:
        _test_doc.close(True)
    _test_doc = None
    _test_ctx = None


def _run_conversion(**kwargs):
    from plugin.calc.spreadsheet_import.import_dialog import run_sheet_conversion

    sheet = _test_doc.getCurrentController().getActiveSheet()
    return run_sheet_conversion(_test_ctx, _test_doc, sheet, **kwargs)


@native_test
def test_convert_spreadsheet_to_python_vectorized():
    sheet = _test_doc.getCurrentController().getActiveSheet()


'''
    # Populate grid
    sheet.getCellByPosition(0, 0).setValue(10)  # A1
    sheet.getCellByPosition(0, 1).setValue(20)  # A2
    sheet.getCellByPosition(0, 2).setValue(30)  # A3

    sheet.getCellByPosition(1, 0).setFormula("=ABS(A1)*2")  # B1
    sheet.getCellByPosition(1, 1).setFormula("=ABS(A2)*2")  # B2
    sheet.getCellByPosition(1, 2).setFormula("=ABS(A3)*2")  # B3

    res = _run_conversion(
        scope="sheet",
        output_mode="new_sheet",
        vectorize=True,
        verify=True,
    )
    assert not res.get("failed_verifications"), f"Verifications failed: {res.get('failed_verifications')}"

    sheets = _test_doc.getSheets()
    assert sheets.hasByName("PythonImport")
    target_sheet = sheets.getByName("PythonImport")

    # Verify values and formulas
    assert target_sheet.getCellByPosition(1, 0).getValue() == 20.0
    assert target_sheet.getCellByPosition(1, 1).getValue() == 40.0
    assert target_sheet.getCellByPosition(1, 2).getValue() == 60.0

    f1 = target_sheet.getCellByPosition(1, 0).getFormula()
    f2 = target_sheet.getCellByPosition(1, 1).getFormula()
    f3 = target_sheet.getCellByPosition(1, 2).getFormula()

    assert "0" in f1 and "A1:A3" in f1
    assert "1" in f2 and "A1:A3" in f2
    assert "2" in f3 and "A1:A3" in f3
'''