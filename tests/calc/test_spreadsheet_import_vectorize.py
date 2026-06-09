# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for column vectorization logic."""

from __future__ import annotations

from plugin.calc.spreadsheet_import.models import CellRecord, SheetModel
from plugin.calc.spreadsheet_import.vectorize import (
    detect_vectorized_columns,
    r1c1_to_a1,
    to_r1c1,
    vectorize_range,
)


def test_to_r1c1():
    assert to_r1c1("=A2*2", "B2") == "=R[0]C[-1]*2"
    assert to_r1c1("=SUM(A1:A10)", "B2") == "=SUM(R[-1]C[-1]:R[8]C[-1])"
    assert to_r1c1("=$A$1+B$2", "B2") == "=R1C1+R2C[0]"


def test_r1c1_to_a1():
    assert r1c1_to_a1("=R[0]C[-1]*2", "B2") == "=A2*2"
    assert r1c1_to_a1("=SUM(R[-1]C[-1]:R[8]C[-1])", "B2") == "=SUM(A1:A10)"
    assert r1c1_to_a1("=R1C1+R2C[0]", "B2") == "=$A$1+B$2"


def test_detect_vectorized_columns():
    cells = {
        "A1": CellRecord(address="A1", type="constant", value=10, formula=None, number_format=None),
        "A2": CellRecord(address="A2", type="constant", value=20, formula=None, number_format=None),
        "A3": CellRecord(address="A3", type="constant", value=30, formula=None, number_format=None),
        "B1": CellRecord(address="B1", type="formula", value=20, formula="=A1*2", number_format=None),
        "B2": CellRecord(address="B2", type="formula", value=40, formula="=A2*2", number_format=None),
        "B3": CellRecord(address="B3", type="formula", value=60, formula="=A3*2", number_format=None),
    }
    model = SheetModel(sheet_name="Sheet1", used_range="A1:B3", cells=cells)
    groups = detect_vectorized_columns(model)
    assert "B1" in groups
    assert groups["B1"] == ["B1", "B2", "B3"]


def test_vectorize_range():
    assert vectorize_range("R[0]C[-1]", "B2", "B4") == "A2:A4"
    assert vectorize_range("R[-1]C[-1]:R[8]C[-1]", "B2", "B4") == "A1:A12"

