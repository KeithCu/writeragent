# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for spreadsheet import P1 formula translation."""

from __future__ import annotations

import pytest

from plugin.calc.spreadsheet_import.preprocess import normalize_lo_formula_for_parse
from plugin.calc.spreadsheet_import.translate import translate_formula


def test_preprocess_semicolon_to_comma():
    assert normalize_lo_formula_for_parse("=IF(A1>0;B1;C1)") == "=IF(A1>0,B1,C1)"


def test_preprocess_preserves_quoted_semicolon():
    assert normalize_lo_formula_for_parse('=CONCAT("a;b";A1)') == '=CONCAT("a;b",A1)'


def test_translate_sum_range():
    result = translate_formula("=SUM(A1:A10)")
    assert result.ok
    assert result.code == "float(np.sum(data))"
    assert result.data_ranges == ["A1:A10"]


def test_translate_if_semicolon():
    result = translate_formula("=IF(A1>0;B1;C1)")
    assert result.ok
    assert "data[1] if" in result.code
    assert result.data_ranges == ["A1", "B1", "C1"]


def test_translate_arithmetic_literal():
    result = translate_formula("=B2*0.1")
    assert result.ok
    assert "data * 0.1" in result.code
    assert result.data_ranges == ["B2"]


def test_translate_binary_plus():
    result = translate_formula("=B2+C2")
    assert result.ok
    assert "data[0] + data[1]" in result.code
    assert result.data_ranges == ["B2", "C2"]


def test_translate_unsupported_function():
    result = translate_formula("=OFFSET(A1;1;1)")
    assert not result.ok
    assert result.reason in ("UNSUPPORTED_FUNCTION", "PARSE_ERROR", "CROSS_SHEET_REF")


def test_translate_parse_error():
    result = translate_formula("not a formula")
    assert not result.ok
    assert result.reason == "PARSE_ERROR"


def test_translate_p2_functions():
    # Text
    res = translate_formula("=CONCAT(A1;B1)")
    assert res.ok
    assert "concat" in res.code or "CONCAT" in res.code or "join" in res.code

    res = translate_formula("=LEFT(A1;2)")
    assert res.ok
    assert "[:int(2)]" in res.code or "[:2]" in res.code

    res = translate_formula("=LEN(A1)")
    assert res.ok
    assert "len(str(data))" in res.code

    # Date
    res = translate_formula("=TODAY()")
    assert res.ok
    assert res.code == "float(datetime.date.today().toordinal() - 693594)"

    # Statistical
    res = translate_formula("=STDEV(A1:A10)")
    assert res.ok
    assert "np.std" in res.code
    assert "ddof=1" in res.code

    # Lookup & Reference
    res = translate_formula("=VLOOKUP(A1;B1:C10;2;0)")
    assert res.ok
    assert "next" in res.code
    assert "r[int(2)-1]" in res.code or "r[1]" in res.code


def test_translate_p2_logical_trig_date_functions():
    # IFERROR / IFNA
    res = translate_formula("=IFERROR(A1; 0)")
    assert res.ok
    assert "def _iferror(f, alt):" in res.code
    assert "result = _iferror" in res.code

    res = translate_formula("=IFNA(A1; 1)")
    assert res.ok
    assert "def _ifna(f, alt):" in res.code

    # SWITCH
    res = translate_formula("=SWITCH(A1; 1; \"one\"; 2; \"two\"; \"other\")")
    assert res.ok
    assert "('one' if data[0] == 1 else ('two' if data[0] == 2 else 'other'))" in res.code

    # Math/Trig
    res = translate_formula("=ASIN(A1)")
    assert res.ok
    assert "np.arcsin(data)" in res.code

    res = translate_formula("=ATAN2(A1; B1)")
    assert res.ok
    assert "np.arctan2(data[1], data[0])" in res.code

    res = translate_formula("=GCD(A1; B1)")
    assert res.ok
    assert "math.gcd" in res.code

    # Date
    res = translate_formula("=DATE(2023; 10; 5)")
    assert res.ok
    assert "datetime.date(int(2023), int(10), int(5)).toordinal() - 693594" in res.code

    # Time
    res = translate_formula("=HOUR(A1)")
    assert res.ok
    assert "datetime.datetime.fromordinal(693594)" in res.code

    # Row/Col/Rows/Cols
    res = translate_formula("=ROW()", "B5")
    assert res.ok
    assert "float(5)" in res.code

    res = translate_formula("=COLUMN()", "B5")
    assert res.ok
    assert "float(2)" in res.code

    res = translate_formula("=ROW(C10:C20)", "A1")
    assert res.ok
    assert "np.array" in res.code

    res = translate_formula("=ROWS(A1:B10)")
    assert res.ok
    assert res.code == "float(10)"

    res = translate_formula("=COLUMNS(A1:B10)")
    assert res.ok
    assert res.code == "float(2)"


def test_translate_cross_sheet_references():
    res = translate_formula("=Sheet2.A1")
    assert res.ok
    assert res.data_ranges == ["SHEET2.A1"]


