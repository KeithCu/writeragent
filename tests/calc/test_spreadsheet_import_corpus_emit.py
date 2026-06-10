# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Corpus-style emission checks (no LibreOffice required)."""

from __future__ import annotations

from plugin.calc.python_formula_edit import inline_py_code_has_lexer_collisions
from plugin.calc.spreadsheet_import.emit import build_converted_output_model, emit_py_formula
from plugin.calc.spreadsheet_import.ingest import ingest_from_arrays
from plugin.calc.spreadsheet_import.models import CellRecord, SheetModel
from plugin.calc.spreadsheet_import.translate import translate_formula


def _assert_lexer_safe_formula(formula: str | None) -> None:
    assert formula is not None
    assert formula.startswith("=PY(")
    assert not inline_py_code_has_lexer_collisions(formula)


def test_corpus_sum_left_as_calc():
    res = translate_formula("=SUM(C5:C6)")
    assert not res.ok
    assert res.reason == "UNSUPPORTED_FUNCTION"


def test_corpus_text_month_emission():
    res = translate_formula('=TEXT(B5; "MMMM")')
    assert res.ok
    formula = emit_py_formula(res.code, res.data_ranges or [])
    _assert_lexer_safe_formula(formula)
    assert "xl.fmt" in formula


def test_corpus_roundup_emission():
    res = translate_formula("=ROUNDUP(C4; 0)")
    assert res.ok
    formula = emit_py_formula(res.code, res.data_ranges or [])
    _assert_lexer_safe_formula(formula)
    assert "np.ceil" in formula


def test_corpus_ratio_arithmetic_emission():
    res = translate_formula("=C4/C5")
    assert res.ok
    formula = emit_py_formula(res.code, res.data_ranges or [])
    _assert_lexer_safe_formula(formula)


def test_vectorize_skipped_for_cross_sheet_sumifs():
    cells = {
        "C8": CellRecord(
            address="C8",
            type="formula",
            value=0,
            formula='=SUMIFS(Actual.F:F;Actual.C:C;\'Dashboard Finished\'.C4;Actual.D:D;B8)',
            number_format=None,
        ),
        "C9": CellRecord(
            address="C9",
            type="formula",
            value=0,
            formula='=SUMIFS(Actual.F:F;Actual.C:C;\'Dashboard Finished\'.C4;Actual.D:D;B9)',
            number_format=None,
        ),
    }
    model = SheetModel(sheet_name="Dashboard Finished", used_range="C8:C9", cells=cells)
    output, report = build_converted_output_model(model, vectorize=True)
    assert len(report.converted) == 2
    for addr in ("C8", "C9"):
        formula = output.cells[addr].formula
        _assert_lexer_safe_formula(formula)
        assert ";0)" not in formula or formula.count(";") < 6


def test_clip_whole_column_data_ranges():
    bounds = {"ACTUAL": (5, 54)}  # end_col, end_row (0-based row 54 → row 55)
    formula = emit_py_formula(
        "xl.sumifs(data[0], data[1], data[2], data[3], data[4])",
        ["ACTUAL.F:F", "ACTUAL.C:C", "DASHBOARD FINISHED.C4", "ACTUAL.D:D", "B8"],
        sheet_bounds=bounds,
        current_sheet="Dashboard Finished",
    )
    assert "Actual.F1:F55" in formula or "ACTUAL.F1:F55" in formula
    assert "F:F" not in formula


def test_income_statement_style_grid():
    model = ingest_from_arrays(
        sheet_name="Income Statement",
        start_col=2,
        start_row=4,
        data_array=[[100.0, 200.0], [300.0, 400.0]],
        formula_array=[
            ["=AVERAGE(C5:C6)", "=AVERAGE(D5:D6)"],
            ["", ""],
        ],
    )
    output, report = build_converted_output_model(model)
    assert len(report.converted) == 2
    for addr in report.converted:
        _assert_lexer_safe_formula(output.cells[addr].formula)
