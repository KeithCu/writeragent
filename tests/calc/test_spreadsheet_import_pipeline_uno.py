# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""UNO tests for the spreadsheet import pipeline."""

from __future__ import annotations

from plugin.testing_runner import native_test
from plugin.tests.testing_utils import with_native_doc


def _run_conversion(doc, _ctx, **kwargs):
    from plugin.calc.spreadsheet_import.pipeline import run_sheet_conversion

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


@native_test
@with_native_doc("calc")
def test_convert_vectorized_columns_verify(ctx, doc):
    sheet = doc.getCurrentController().getActiveSheet()

    for row in range(3):
        val = row + 1
        sheet.getCellByPosition(0, row).setValue(val)  # A1:A3 = 1, 2, 3
        sheet.getCellByPosition(1, row).setValue(val * 10)  # B1:B3 = 10, 20, 30
        sheet.getCellByPosition(7, row).setString(f"txt{val}")  # H1:H3 = txt1..3
        r1 = row + 1
        sheet.getCellByPosition(2, row).setFormula(f"=ABS(A{r1})*2")  # C
        sheet.getCellByPosition(3, row).setFormula(f"=ABS(A{r1})+ABS(B{r1})")  # D
        sheet.getCellByPosition(4, row).setFormula(f"=LEFT(H{r1};3)")  # E
        sheet.getCellByPosition(5, row).setFormula(f'=LEFT(H{r1};2)&"A{r1}"')  # F
        sheet.getCellByPosition(6, row).setFormula(f"=LEN(H{r1})")  # G

    res = _run_conversion(
        doc,
        ctx,
        scope="sheet",
        output_mode="new_sheet",
        vectorize=True,
        verify=True,
    )
    assert res["failed_verifications"] == [], f"Verification mismatches: {res['failed_verifications']}"
    target_sheet = doc.getSheets().getByName("PythonImport")
    c1_formula = target_sheet.getCellByPosition(2, 0).getFormula()
    assert "A1:A3" in c1_formula, f"Expected vectorized A1:A3 in C1, got {c1_formula}"
