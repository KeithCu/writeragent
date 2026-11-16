# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for column vectorization logic."""

from __future__ import annotations

import pytest

from plugin.calc.spreadsheet_import.models import CellRecord, SheetModel, TranslationResult
from plugin.calc.spreadsheet_import.translate import translate_formula
from plugin.calc.spreadsheet_import.vectorize import detect_vectorized_columns, is_elementwise_code


def _translate_model(model: SheetModel) -> dict[str, TranslationResult | None]:
    return {
        addr: translate_formula(rec.formula, cell_addr=addr, functions_only=True)
        for addr, rec in sorted(model.cells.items())
        if rec.type in ("formula", "error") and rec.formula
    }


def test_detect_vectorized_columns():
    cells = {
        "A1": CellRecord(address="A1", type="constant", value=10, formula=None, number_format=None),
        "A2": CellRecord(address="A2", type="constant", value=20, formula=None, number_format=None),
        "A3": CellRecord(address="A3", type="constant", value=30, formula=None, number_format=None),
        "A4": CellRecord(address="A4", type="constant", value=40, formula=None, number_format=None),
        "A5": CellRecord(address="A5", type="constant", value=50, formula=None, number_format=None),
        "A6": CellRecord(address="A6", type="constant", value=60, formula=None, number_format=None),
        "B1": CellRecord(address="B1", type="formula", value=20, formula="=ABS(A1)*2", number_format=None),
        "B2": CellRecord(address="B2", type="formula", value=40, formula="=ABS(A2)*2", number_format=None),
        "B3": CellRecord(address="B3", type="formula", value=60, formula="=ABS(A3)*2", number_format=None),
        "B4": CellRecord(address="B4", type="formula", value=99, formula="=99", number_format=None),
        "B5": CellRecord(address="B5", type="formula", value=100, formula="=ABS(A5)*2", number_format=None),
        "B6": CellRecord(address="B6", type="formula", value=120, formula="=ABS(A6)*2", number_format=None),
    }
    model = SheetModel(sheet_name="Sheet1", used_range="A1:B6", cells=cells)
    translations = _translate_model(model)
    runs = detect_vectorized_columns(model, translations)
    assert len(runs) == 2
    assert runs[0].cells == ["B1", "B2", "B3"]
    assert runs[0].column == "A1:A3"
    assert runs[1].cells == ["B5", "B6"]
    assert runs[1].column == "A5:A6"


def test_detect_skips_columns_one_shared_py_cannot_compute():
    cells = {
        "A1": CellRecord(address="A1", type="constant", value=1, formula=None, number_format=None),
        "A2": CellRecord(address="A2", type="constant", value=2, formula=None, number_format=None),
        "A3": CellRecord(address="A3", type="constant", value=3, formula=None, number_format=None),
        "C1": CellRecord(address="C1", type="constant", value=10, formula=None, number_format=None),
        "C2": CellRecord(address="C2", type="constant", value=20, formula=None, number_format=None),
        "H1": CellRecord(address="H1", type="constant", value="alpha", formula=None, number_format=None),
        "H2": CellRecord(address="H2", type="constant", value="beta", formula=None, number_format=None),
        "J1": CellRecord(address="J1", type="constant", value=1, formula=None, number_format=None),
        "J2": CellRecord(address="J2", type="constant", value="oops", formula=None, number_format=None),
        # Absolute ref
        "B1": CellRecord(address="B1", type="formula", value=2, formula="=ABS($A$1)*2", number_format=None),
        "B2": CellRecord(address="B2", type="formula", value=2, formula="=ABS($A$1)*2", number_format=None),
        # Shifted window
        "D1": CellRecord(address="D1", type="formula", value=1.5, formula="=AVERAGE(A1:A2)", number_format=None),
        "D2": CellRecord(address="D2", type="formula", value=2.5, formula="=AVERAGE(A2:A3)", number_format=None),
        # Two refs
        "E1": CellRecord(address="E1", type="formula", value=11, formula="=ABS(A1)+ABS(C1)", number_format=None),
        "E2": CellRecord(address="E2", type="formula", value=22, formula="=ABS(A2)+ABS(C2)", number_format=None),
        # Cross-sheet ref
        "F1": CellRecord(address="F1", type="formula", value=1, formula="=ABS(Sheet2.A1)", number_format=None),
        "F2": CellRecord(address="F2", type="formula", value=2, formula="=ABS(Sheet2.A2)", number_format=None),
        # Text helper
        "G1": CellRecord(address="G1", type="formula", value="alp", formula="=LEFT(H1;3)", number_format=None),
        "G2": CellRecord(address="G2", type="formula", value="bet", formula="=LEFT(H2;3)", number_format=None),
        # Mixed text in referenced column
        "K1": CellRecord(address="K1", type="formula", value=1, formula="=ABS(J1)", number_format=None),
        "K2": CellRecord(address="K2", type="formula", value=0, formula="=ABS(J2)", number_format=None),
    }
    model = SheetModel(sheet_name="Sheet1", used_range="A1:K3", cells=cells)
    translations = _translate_model(model)
    assert detect_vectorized_columns(model, translations) == []


def test_detect_skips_same_column_recurrence():
    cells = {
        "B1": CellRecord(address="B1", type="constant", value=100, formula=None, number_format=None),
        "B2": CellRecord(address="B2", type="formula", value=105, formula="=ROUND(B1*1.05;2)", number_format=None),
        "B3": CellRecord(address="B3", type="formula", value=110.25, formula="=ROUND(B2*1.05;2)", number_format=None),
        "B4": CellRecord(address="B4", type="formula", value=115.76, formula="=ROUND(B3*1.05;2)", number_format=None),
    }
    model = SheetModel(sheet_name="Sheet1", used_range="B1:B4", cells=cells)
    assert detect_vectorized_columns(model, _translate_model(model)) == []


@pytest.mark.parametrize(
    "formula, expected",
    [
        ("=ABS(A1)*2", True),
        ("=ROUND(A1;2)", True),
        ("=ROUNDUP(A1;0)", True),
        ("=LOG(A1;2)", True),
        ("=MOD(A1;3)", True),
        ("=POWER(A1;2)", True),
        ("=SQRT(A1)>1", True),
        ("=-INT(A1)+5%", True),
        ("=CEILING(A1;5)", True),
        ("=DEGREES(A1)", True),
        ("=LEFT(A1;3)", False),
        ('=TEXT(A1;"0.0")', False),
        ("=IF(A1>0;1;0)", False),
        ("=AVERAGE(A1:A3)", False),
        ('=ABS(A1)&"x"', False),
        ("=LEN(A1)", False),
        ("=MAX(A1:A3)", False),
        ("=ROW(A1)+1", False),
    ],
)
def test_is_elementwise_code(formula: str, expected: bool):
    res = translate_formula(formula, cell_addr="B1", functions_only=True)
    assert res.ok and res.code is not None
    assert is_elementwise_code(res.code) is expected
