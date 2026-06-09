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
    assert result.code == "result = float(np.sum(data))"
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
