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
