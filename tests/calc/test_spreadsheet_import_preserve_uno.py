# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""UNO tests for spreadsheet import preserve (live PY formulas)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from plugin.testing_runner import native_test
from plugin.tests.testing_utils import with_native_doc

REPO_ROOT = Path(__file__).resolve().parents[2]
_GEN_PATH = REPO_ROOT / "scripts" / "generate_serialization_spreadsheet.py"


def _load_generator():
    if not _GEN_PATH.is_file():
        return None
    spec = importlib.util.spec_from_file_location("generate_serialization_spreadsheet", _GEN_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@native_test
@with_native_doc("calc")
def test_preserve_live_py_formulas_round_trip(ctx, doc):
    from plugin.calc.address_utils import format_address
    from plugin.calc.python.cell_discovery import is_py_formula_text
    from plugin.calc.spreadsheet_import.extract import py_formula_semantics
    from plugin.calc.spreadsheet_import.import_dialog import run_sheet_conversion
    from plugin.calc.spreadsheet_import.ingest import ingest_sheet

    sheet = doc.getSheets().getByIndex(0)
    sheet.getCellByPosition(0, 0).setValue(10.0)
    sheet.getCellByPosition(0, 1).setValue(20.0)
    sheet.getCellByPosition(1, 0).setFormula("=SUM(A1:A2)")
    sheet.getCellByPosition(1, 1).setFormula('=PYTHON("np.sum(data)";A1:A2)')

    source_model = ingest_sheet(sheet)
    assert source_model.cells["B2"].type == "py_formula"

    run_sheet_conversion(doc, sheet, vectorize=False, verify=False)
    target = doc.getSheets().getByName("PythonImport")

    assert target.getCellByPosition(0, 0).getValue() == 10.0
    assert target.getCellByPosition(0, 1).getValue() == 20.0
    assert is_py_formula_text(target.getCellByPosition(1, 0).getFormula())

    col, row = 1, 1
    src_formula = sheet.getCellByPosition(col, row).getFormula()
    tgt_formula = target.getCellByPosition(col, row).getFormula()
    assert py_formula_semantics(src_formula) == py_formula_semantics(tgt_formula)
    assert format_address(col, row) == "B2"
