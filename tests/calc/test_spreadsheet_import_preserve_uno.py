# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""UNO tests for spreadsheet import preserve (live PY formulas)."""

from __future__ import annotations

from plugin.testing_runner import native_test
from plugin.tests.testing_utils import with_native_doc


@native_test
@with_native_doc("calc")
def test_preserve_live_py_formulas_round_trip(ctx, doc):
    from plugin.calc.address_utils import format_address
    from plugin.calc.python.formula_edit import PyFormula
    from plugin.calc.spreadsheet_import.emit import build_converted_output_model
    from plugin.calc.spreadsheet_import.ingest import ingest_sheet
    from plugin.calc.spreadsheet_import.preserve import apply_output_to_sheet, enrich_number_formats

    sheet = doc.getSheets().getByIndex(0)
    sheet.getCellByPosition(0, 0).setValue(10.0)
    sheet.getCellByPosition(0, 1).setValue(20.0)
    sheet.getCellByPosition(1, 0).setFormula("=SUM(A1:A2)")
    sheet.getCellByPosition(1, 1).setFormula('=PYTHON("np.sum(data)";A1:A2)')

    source_model = ingest_sheet(sheet)
    assert source_model.cells["B2"].type == "py_formula"

    enrich_number_formats(sheet, source_model)
    output, _report = build_converted_output_model(source_model, vectorize=False)
    sheets = doc.getSheets()
    sheets.insertNewByName("PythonImport", sheets.getCount())
    target = sheets.getByName("PythonImport")
    apply_output_to_sheet(target, output)

    assert output.cells["A1"].value == 10.0
    assert output.cells["A2"].value == 20.0
    assert target.getCellByPosition(1, 0).getFormula() == "=SUM(A1:A2)"

    col, row = 1, 1
    src_formula = sheet.getCellByPosition(col, row).getFormula()
    tgt_formula = target.getCellByPosition(col, row).getFormula()
    src_p = PyFormula.parse(src_formula)
    tgt_p = PyFormula.parse(tgt_formula)
    assert src_p is not None and tgt_p is not None
    assert (src_p.code, src_p.data_args()) == (tgt_p.code, tgt_p.data_args())
    assert format_address(col, row) == "B2"
