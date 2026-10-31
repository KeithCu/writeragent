# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Pure unit tests for Calc array-formula detection (no soffice)."""

from __future__ import annotations

from plugin.calc.array_formula import _scan_calls, returns_array
import pytest


def _top_level_calls(formula: str) -> list[str]:
    return [name for name, depth in _scan_calls(formula) if depth == 0]


@pytest.mark.parametrize(
    "value, value_2, value_3, expected",
    [
        pytest.param("=SUM(FILTER(A1:A9;B1:B9>0))", "SUM", "=SUM(FILTER(A1:A9;B1:B9>0))", False, id="test_sum_of_filter_is_scalar_top_level"),
        pytest.param("=SORT(FILTER(A1:A9;B1:B9>0))", "SORT", "=SORT(FILTER(A1:A9;B1:B9>0))", True, id="test_sort_of_filter_is_array"),
        pytest.param("=MOD.FUNC(A1)", "MOD.FUNC", "=MOD.FUNC(A1)", False, id="test_sheet_qualified_name_is_one_token"),
    ],
)
def test_sum_of_filter_is_scalar_top_level(value, value_2, value_3, expected):
    assert _top_level_calls(value) == [value_2]
    assert returns_array(value_3) is expected

def test_plain_arithmetic_is_not_array():
    assert returns_array("=A1+A2") is False
    assert _top_level_calls("=A1+A2") == []


@pytest.mark.parametrize(
    "value, value_2",
    [
        pytest.param('="FILTER("', '="FILTER("', id="test_quoted_filter_is_not_a_call"),
        pytest.param('="He said ""FILTER("""', '="He said ""FILTER("""', id="test_escaped_quotes_do_not_open_a_call"),
    ],
)
def test_quoted_filter_is_not_a_call(value, value_2):
    assert _top_level_calls(value) == []
    assert returns_array(value_2) is False

def test_py_formula_is_not_array_even_if_string_mentions_filter():
    formula = '=PY("result = data[data.flag]; FILTER leftover"; A1:B9)'
    assert _top_level_calls(formula) == ["PY"]
    assert returns_array(formula) is False

def test_forced_overrides_detection():
    assert returns_array("=A1+A2", forced=True) is True
    assert returns_array("=SORT(A1:A9)", forced=False) is False
    assert returns_array("not a formula", forced=True) is True


def test_nested_array_calls_and_passthroughs():
    assert returns_array('=IF(A1;SORT(B1:B9);"")') is True
    assert returns_array('=IFERROR(SORT(B1:B9);"")') is True
    assert returns_array('=LET(x;1;SORT(B1:B9))') is True
    assert returns_array('=_xlfn.SORT(B1:B9)') is True
    assert returns_array("=SUM(FILTER(A1:A9;B1:B9>0))") is False


def test_quoted_sheet_names_with_parenthesis():
    formula = "=SUM('Q1 (draft'.A1:A3)+SORT(B1:B3)"
    assert _top_level_calls(formula) == ["SUM", "SORT"]


def test_result_range_clamps_bounds():
    from plugin.calc.array_formula import plan_target
    from plugin.calc.address_utils import MAX_COL, MAX_ROW
    from plugin.framework.errors import CalcError

    # Out of bounds raises CalcError
    with pytest.raises(CalcError, match="sheet bounds"):
        plan_target(MAX_COL, 0, MAX_COL, 0, 1, 2)

    with pytest.raises(CalcError, match="sheet bounds"):
        plan_target(0, MAX_ROW, 0, MAX_ROW, 2, 1)


@pytest.mark.parametrize(
    "func_name",
    ["LINEST", "LOGEST", "TREND", "GROWTH", "MUNIT", "TEXTSPLIT"],
)
def test_missing_array_returners(func_name):
    assert returns_array(f"={func_name}(A1:B2)") is True
