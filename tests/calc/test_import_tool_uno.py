# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for convert_spreadsheet_to_python tool."""

from __future__ import annotations

from plugin.testing_runner import native_test
from plugin.tests.testing_utils import with_native_doc


def _run_conversion(doc, _ctx, **kwargs):
    from plugin.calc.spreadsheet_import.import_dialog import run_sheet_conversion

    sheet = doc.getCurrentController().getActiveSheet()
    return run_sheet_conversion(doc, sheet, **kwargs)


@native_test
@with_native_doc("calc")
def test_convert_spreadsheet_to_python_basic(ctx, doc):
    sheet = doc.getCurrentController().getActiveSheet()

    # Populate grid
    sheet.getCellByPosition(0, 0).setValue(10)  # A1
    sheet.getCellByPosition(0, 1).setValue(20)  # A2
    sheet.getCellByPosition(1, 0).setFormula("=ABS(A1)+ABS(A2)")  # B1
    sheet.getCellByPosition(1, 1).setFormula("=AVERAGE(A1:A2)")  # B2

    res = _run_conversion(
        doc,
        ctx,
        scope="sheet",
        output_mode="new_sheet",
        vectorize=False,
        verify=False,
    )
    report = res.get("report", {})
    assert len(report.get("converted", [])) >= 2, f"Expected conversion, got report: {report}"

    # Verify sheet PythonImport was created
    sheets = doc.getSheets()
    assert sheets.hasByName("PythonImport")
    target_sheet = sheets.getByName("PythonImport")

    # Check that formulas became =PY(...)
    from plugin.calc.python.cell_discovery import is_py_formula_text
    assert is_py_formula_text(target_sheet.getCellByPosition(1, 0).getFormula())
    assert is_py_formula_text(target_sheet.getCellByPosition(1, 1).getFormula())
