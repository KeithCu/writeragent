# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Pure unit tests for Calc array-formula detection (no soffice)."""

from __future__ import annotations

from plugin.calc.array_formula import returns_array, top_level_calls
import pytest


@pytest.mark.parametrize(
    "value, value_2, value_3, expected",
    [
        pytest.param("=SUM(FILTER(A1:A9;B1:B9>0))", "SUM", "=SUM(FILTER(A1:A9;B1:B9>0))", False, id="test_sum_of_filter_is_scalar_top_level"),
        pytest.param("=SORT(FILTER(A1:A9;B1:B9>0))", "SORT", "=SORT(FILTER(A1:A9;B1:B9>0))", True, id="test_sort_of_filter_is_array"),
        pytest.param("=MOD.FUNC(A1)", "MOD.FUNC", "=MOD.FUNC(A1)", False, id="test_sheet_qualified_name_is_one_token"),
    ],
)
def test_sum_of_filter_is_scalar_top_level(value, value_2, value_3, expected):
    assert top_level_calls(value) == [value_2]
    assert returns_array(value_3) is expected

def test_plain_arithmetic_is_not_array():
    assert returns_array("=A1+A2") is False
    assert top_level_calls("=A1+A2") == []


@pytest.mark.parametrize(
    "value, value_2",
    [
        pytest.param('="FILTER("', '="FILTER("', id="test_quoted_filter_is_not_a_call"),
        pytest.param('="He said ""FILTER("""', '="He said ""FILTER("""', id="test_escaped_quotes_do_not_open_a_call"),
    ],
)
def test_quoted_filter_is_not_a_call(value, value_2):
    assert top_level_calls(value) == []
    assert returns_array(value_2) is False

def test_py_formula_is_not_array_even_if_string_mentions_filter():
    formula = '=PY("result = data[data.flag]; FILTER leftover"; A1:B9)'
    assert top_level_calls(formula) == ["PY"]
    assert returns_array(formula) is False

def test_forced_overrides_detection():
    assert returns_array("=A1+A2", forced=True) is True
    assert returns_array("=SORT(A1:A9)", forced=False) is False
    assert returns_array("not a formula", forced=True) is True
