# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for spreadsheet import =PY() emission."""

from __future__ import annotations

from plugin.calc.spreadsheet_import.emit import emit_py_formula
from plugin.calc.spreadsheet_import.extract import py_formula_semantics
from plugin.calc.spreadsheet_import.translate import translate_formula


def test_emit_py_formula_semicolons():
    formula = emit_py_formula("result = float(np.sum(data))", ["A1:A10"])
    assert formula.startswith('=PY("')
    assert ";A1:A10)" in formula
    assert "," not in formula.split(";", 1)[-1]


def test_emit_multi_range():
    formula = emit_py_formula("result = float(data[0] + data[1])", ["B2", "C2"])
    assert ";B2;C2)" in formula


def test_emit_round_trip_semantics():
    translation = translate_formula("=SUM(B2:B3)")
    assert translation.ok
    emitted = emit_py_formula(translation.code, translation.data_ranges or [])
    semantics = py_formula_semantics(emitted)
    assert semantics is not None
    code, data_args = semantics
    assert "np.sum" in code
    assert data_args == ["B2:B3"]


def test_emit_vectorized_column():
    from plugin.calc.spreadsheet_import.models import CellRecord, SheetModel
    from plugin.calc.spreadsheet_import.emit import build_converted_output_model

    cells = {
        "A1": CellRecord(address="A1", type="constant", value=10, formula=None, number_format=None),
        "A2": CellRecord(address="A2", type="constant", value=20, formula=None, number_format=None),
        "A3": CellRecord(address="A3", type="constant", value=30, formula=None, number_format=None),
        "B1": CellRecord(address="B1", type="formula", value=20, formula="=ABS(A1)*2", number_format=None),
        "B2": CellRecord(address="B2", type="formula", value=40, formula="=ABS(A2)*2", number_format=None),
        "B3": CellRecord(address="B3", type="formula", value=60, formula="=ABS(A3)*2", number_format=None),
    }
    model = SheetModel(sheet_name="Sheet1", used_range="A1:B3", cells=cells)
    output, report = build_converted_output_model(model, vectorize=True)

    assert output.cells["B1"].formula is not None
    assert "0" in output.cells["B1"].formula and "A1:A3" in output.cells["B1"].formula
    assert output.cells["B2"].formula is not None
    assert "1" in output.cells["B2"].formula and "A1:A3" in output.cells["B2"].formula
    assert output.cells["B3"].formula is not None
    assert "2" in output.cells["B3"].formula and "A1:A3" in output.cells["B3"].formula
    assert len(report.converted) == 3

