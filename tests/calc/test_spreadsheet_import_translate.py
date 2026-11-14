# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for spreadsheet import P1 formula translation."""

from __future__ import annotations

import cmath
import datetime as dt
import inspect
import math
import re
import numpy as np


import plugin.scripting.calc_functions as calc

from plugin.calc.spreadsheet_import.preprocess import normalize_lo_formula_for_parse
from plugin.calc.spreadsheet_import.translate import FUNCTION_EMITTERS, TranslationError, translate_formula
import pytest


@pytest.mark.parametrize(
    "value, expected",
    [
        pytest.param("=IF(A1>0;B1;C1)", "=IF(A1>0,B1,C1)", id="test_preprocess_semicolon_to_comma"),
        pytest.param('=CONCAT("a;b";A1)', '=CONCAT("a;b",A1)', id="test_preprocess_preserves_quoted_semicolon"),
        pytest.param("='My;Sheet'.A1", "='My;Sheet'.A1", id="test_preprocess_preserves_semicolon_in_quoted_sheet"),
        pytest.param("='O''Brien;X'.A1", "='O''Brien;X'.A1", id="test_preprocess_preserves_semicolon_in_escaped_sheet"),
        pytest.param('="a"";b"', '="a"";b"', id="test_preprocess_preserves_semicolon_in_doubled_quote_text"),
        pytest.param("=SUM({1;2|3;4})", "=SUM({1;2|3;4})", id="test_preprocess_preserves_array_column_separator"),
        pytest.param("=SUM('My;Sheet'.A1;B1)", "=SUM('My;Sheet'.A1,B1)", id="test_preprocess_still_replaces_argument_separator"),
    ],
)
def test_preprocess_semicolon_to_comma(value, expected):
    assert normalize_lo_formula_for_parse(value) == expected

@pytest.mark.parametrize(
    "value, expected",
    [
        pytest.param("=SUM(A1:A10)", "UNSUPPORTED_FUNCTION", id="test_translate_sum_range"),
        pytest.param("not a formula", "PARSE_ERROR", id="test_translate_parse_error"),
    ],
)
def test_translate_sum_range(value, expected):
    result = translate_formula(value, "A1")
    assert not result.ok
    assert result.reason == expected


def test_translate_if_semicolon():
    result = translate_formula("=IF(A1>0;B1;C1)", "A1")
    assert result.ok
    assert "data[1] if" in result.code
    assert result.data_ranges == ["A1", "B1", "C1"]


def test_translate_arithmetic_literal():
    result = translate_formula("=B2*0.1", "A1")
    assert result.ok
    assert "data * 0.1" in result.code
    assert result.data_ranges == ["B2"]


def test_translate_binary_plus():
    result = translate_formula("=B2+C2", "A1")
    assert result.ok
    assert "data[0] + data[1]" in result.code
    assert result.data_ranges == ["B2", "C2"]


def test_translate_unsupported_function():
    result = translate_formula("=OFFSET(A1;1;1)", "A1")
    assert not result.ok
    assert result.reason in ("UNSUPPORTED_FUNCTION", "PARSE_ERROR", "CROSS_SHEET_REF")

def test_translate_row_column_origin():
    assert exec_result(translate_formula("=ROW()", "B5"), []) == 5
    assert exec_result(translate_formula("=COLUMN()", "B5"), []) == 2


def test_translate_cross_sheet_references():
    res = translate_formula("=Sheet2.A1", "A1")
    assert res.ok
    assert res.data_ranges == ["Sheet2.A1"]


def test_add_range_case_insensitive_dedupe():
    from plugin.calc.spreadsheet_import.translate import _CodegenState

    # Unquoted sheet names: Sheet2.A1 and SHEET2.A1 map to one entry;
    # emitted address keeps first-seen display name.
    state = _CodegenState("A1")
    assert state.add_range("Sheet2.A1") == 0
    assert state.add_range("SHEET2.A1") == 0
    assert len(state._ranges) == 1
    assert state.data_ranges == ["Sheet2.A1"]

    # First-seen display name when uppercase is seen first
    state_upper = _CodegenState("A1")
    assert state_upper.add_range("SHEET2.A1") == 0
    assert state_upper.add_range("Sheet2.A1") == 0
    assert len(state_upper._ranges) == 1
    assert state_upper.data_ranges == ["SHEET2.A1"]

    # Quoted mixed-case pair: 'My Sheet'.A1 and 'my sheet'.A1 map to one entry;
    # emitted address keeps first-seen display name.
    state_quoted = _CodegenState("A1")
    assert state_quoted.add_range("'My Sheet'.A1") == 0
    assert state_quoted.add_range("'my sheet'.A1") == 0
    assert state_quoted.add_range("'MY SHEET'.A1") == 0
    assert len(state_quoted._ranges) == 1
    assert state_quoted.data_ranges == ["'My Sheet'.A1"]

    # First-seen display name when lowercase is seen first
    state_quoted_lower = _CodegenState("A1")
    assert state_quoted_lower.add_range("'my sheet'.A1") == 0
    assert state_quoted_lower.add_range("'My Sheet'.A1") == 0
    assert len(state_quoted_lower._ranges) == 1
    assert state_quoted_lower.data_ranges == ["'my sheet'.A1"]


def test_translate_cross_sheet_case_insensitive_dedupe():
    res = translate_formula("=Sheet2.A1+SHEET2.A1", "A1")
    assert res.ok
    assert res.data_ranges == ["Sheet2.A1"]
    assert res.code == "(data + data)"

    res_quoted = translate_formula("='My Sheet'.A1+'my sheet'.A1", "A1")
    assert res_quoted.ok
    assert res_quoted.data_ranges == ["'My Sheet'.A1"]
    assert res_quoted.code == "(data + data)"


def test_translate_and_exec_new_functions():
    import math

    import numpy as np

    base_locs = {"np": np, "calc": calc, "math": math, "dt": dt}

    # 1. SUMIF
    res = translate_formula("=SUMIF(A1:A5; \">10\"; B1:B5)", "A1")
    assert res.ok
    locs = {**base_locs, "data": [[5.0, 12.0, 3.0, 15.0, 8.0], [1.0, 2.0, 3.0, 4.0, 5.0]]}
    exec(f"result = {res.code}", locs)
    assert locs["result"] == 6.0

    # 2. SUMIFS
    res = translate_formula("=SUMIFS(B1:B5; A1:A5; \">5\"; A1:A5; \"<=12\")", "A1")
    assert res.ok
    locs = {**base_locs, "data": [[1.0, 2.0, 3.0, 4.0, 5.0], [5.0, 12.0, 3.0, 15.0, 8.0]]}
    exec(f"result = {res.code}", locs)
    # sum range is B1:B5 (data[0]): 1, 2, 3, 4, 5.
    # criteria range is A1:A5 (data[1]): 5, 12, 3, 15, 8.
    # A1:A5 > 5 and <= 12 are 12 (index 1) and 8 (index 4).
    # Corresponding elements in B1:B5 are 2 and 5. Sum = 7.0.
    assert locs["result"] == 7.0

    # 3. COUNTIF
    res = translate_formula("=COUNTIF(A1:A5; \"<=5\")", "A1")
    assert res.ok
    locs = {**base_locs, "data": [5.0, 12.0, 3.0, 15.0, 8.0]}
    exec(f"result = {res.code}", locs)
    # Elements <= 5 are 5 and 3. count = 2.
    assert locs["result"] == 2.0

    # 4. COUNTIFS
    res = translate_formula("=COUNTIFS(A1:A5; \">5\"; B1:B5; \"<10\")", "A1")
    assert res.ok
    locs = {**base_locs, "data": [[5.0, 12.0, 3.0, 15.0, 8.0], [1.0, 2.0, 3.0, 4.0, 5.0]]}
    exec(f"result = {res.code}", locs)
    # A1:A5 > 5 are 12, 15, 8.
    # Corresponding B1:B5 are 2, 4, 5. All are < 10. Count = 3.
    assert locs["result"] == 3.0

    # 5. AVERAGEIF
    res = translate_formula("=AVERAGEIF(A1:A5; \">5\"; B1:B5)", "A1")
    assert res.ok
    locs = {**base_locs, "data": [[5.0, 12.0, 3.0, 15.0, 8.0], [1.0, 2.0, 3.0, 4.0, 5.0]]}
    exec(f"result = {res.code}", locs)
    # A1:A5 > 5 are 12, 15, 8.
    # Corresponding B1:B5 are 2, 4, 5. Mean = (2+4+5)/3 = 3.6666...
    assert abs(locs["result"] - 11.0 / 3.0) < 1e-9

    # 6. AVERAGEIFS
    res = translate_formula("=AVERAGEIFS(B1:B5; A1:A5; \">5\")", "A1")
    assert res.ok
    locs = {**base_locs, "data": [[1.0, 2.0, 3.0, 4.0, 5.0], [5.0, 12.0, 3.0, 15.0, 8.0]]}
    exec(f"result = {res.code}", locs)
    assert abs(locs["result"] - 11.0 / 3.0) < 1e-9

    # 7. XLOOKUP
    res = translate_formula("=XLOOKUP(\"apple\"; A1:A3; B1:B3; \"Not Found\")", "A1")
    assert res.ok
    locs = {**base_locs, "data": [["pear", "apple", "banana"], [10.0, 20.0, 30.0]]}
    exec(f"result = {res.code}", locs)
    assert locs["result"] == 20.0

    res = translate_formula("=XLOOKUP(\"orange\"; A1:A3; B1:B3; \"Not Found\")", "A1")
    assert res.ok
    locs = {**base_locs, "data": [["pear", "apple", "banana"], [10.0, 20.0, 30.0]]}
    exec(f"result = {res.code}", locs)
    assert locs["result"] == "Not Found"

    # 8. TEXTJOIN
    res = translate_formula("=TEXTJOIN(\", \"; TRUE; A1:A3)", "A1")
    assert res.ok
    locs = {**base_locs, "data": ["apple", "", "banana"]}
    exec(f"result = {res.code}", locs)
    assert locs["result"] == "apple, banana"

    # 9. REGEX
    res = translate_formula("=REGEX(\"123-456\"; \"[0-9]+\"; \"XXX\"; \"g\")", "A1")
    assert res.ok
    locs = dict(base_locs)
    exec(f"result = {res.code}", locs)
    assert locs["result"] == "XXX-XXX"

    # 10. EOMONTH
    # 2026-06-09 is 46182 days from 1899-12-30 (since 2026-06-09 is ordinal 739776. 739776 - 693594 = 46182)
    # EOMONTH(46182; 1) -> End of July 2026 -> 2026-07-31 -> ordinal 739828 -> 739828 - 693594 = 46234
    res = translate_formula("=EOMONTH(46182; 1)", "A1")
    assert res.ok
    locs = dict(base_locs)
    exec(f"result = {res.code}", locs)
    assert locs["result"] == 46234.0

    # 11. NETWORKDAYS
    # 2026-06-08 (Mon) to 2026-06-12 (Fri) should be 5 network days.
    # 2026-06-08 is ordinal 739775 -> 46181
    # 2026-06-12 is ordinal 739779 -> 46185
    res = translate_formula("=NETWORKDAYS(46181; 46185)", "A1")
    assert res.ok
    locs = dict(base_locs)
    exec(f"result = {res.code}", locs)
    assert locs["result"] == 5.0


def test_translate_tier_abc_functions():

    # Tier A
    res = translate_formula("=SUBTOTAL(9; A1:A5)", "A1")
    assert res.ok
    assert "calc.subtotal(" in res.code
    assert exec_result(res, [1.0, 2.0, 3.0, 4.0, 5.0]) == 15.0

    res = translate_formula("=ISBLANK(A1)", "A1")
    assert res.ok
    assert exec_result(res, "") is True
    assert exec_result(res, 5.0) is False

    res = translate_formula("=ISNUMBER(A1)", "A1")
    assert res.ok
    assert exec_result(res, 5.0) is True
    assert exec_result(res, "x") is False

    res = translate_formula("=IFS(A1>10; \"big\"; A1>5; \"mid\"; TRUE; \"small\")", "A1")
    assert res.ok
    assert exec_result(res, 12.0) == "big"
    assert exec_result(res, 7.0) == "mid"
    assert exec_result(res, 1.0) == "small"

    res = translate_formula("=MEDIAN(A1:A5)", "A1")
    assert res.ok
    assert exec_result(res, [1.0, 2.0, 3.0, 4.0, 100.0]) == 3.0

    res = translate_formula("=COUNTBLANK(A1:A4)", "A1")
    assert res.ok
    assert exec_result(res, [1.0, "", None, 4.0]) == 2.0

    res = translate_formula("=ROUNDUP(A1; 1)", "A1")
    assert res.ok
    assert exec_result(res, 1.23) == 1.3

    res = translate_formula("=LOG(A1; 2)", "A1")
    assert res.ok
    assert abs(exec_result(res, 8.0) - 3.0) < 1e-9

    res = translate_formula("=QUOTIENT(A1; B1)", "A1")
    assert res.ok
    assert exec_result(res, [10.0, 3.0]) == 3.0

    res = translate_formula("=SUMPRODUCT(A1:A3; B1:B3)", "A1")
    assert res.ok
    assert exec_result(res, [[1.0, 2.0, 3.0], [4.0, 5.0, 6.0]]) == 32.0

    res = translate_formula("=DATEDIF(46181; 46185; \"D\")", "A1")
    assert res.ok
    assert exec_result(res, []) == 4.0

    # Tier B
    res = translate_formula("=ISTEXT(A1)", "A1")
    assert res.ok
    assert exec_result(res, "hello") is True

    res = translate_formula("=LARGE(A1:A5; 2)", "A1")
    assert res.ok
    assert exec_result(res, [1.0, 5.0, 3.0, 4.0, 2.0]) == 4.0

    res = translate_formula("=SMALL(A1:A5; 2)", "A1")
    assert res.ok
    assert exec_result(res, [1.0, 5.0, 3.0, 4.0, 2.0]) == 2.0

    res = translate_formula("=AVERAGEA(A1:A3)", "A1")
    assert res.ok
    assert exec_result(res, [10.0, "", 20.0]) == 10.0

    res = translate_formula("=EVEN(A1)", "A1")
    assert res.ok
    assert exec_result(res, 3.0) == 4.0

    res = translate_formula("=XMATCH(\"b\"; A1:A3)", "A1")
    assert res.ok
    assert exec_result(res, [["a", "b", "c"]]) == 2.0

    res = translate_formula('=TEXT(B5; "MMMM")', "A1")
    assert res.ok
    assert "calc.fmt" in res.code
    assert "calc.text(" not in res.code
    # 46181 is 2026-06-08. Executing the translated formula used to raise
    # AttributeError because calc.fmt was never exported.
    assert exec_result(res, 46181) == "June"

    res = translate_formula("=ROUNDUP(7692.30769230769; 0)", "A1")
    assert res.ok
    assert "int(" not in res.code

    # Tier C
    res = translate_formula("=FILTER(A1:A5; B1:B5)", "A1")
    assert res.ok
    assert exec_result(res, [[1.0, 2.0, 3.0, 4.0, 5.0], [True, False, True, False, True]]) == [1.0, 3.0, 5.0]

    res = translate_formula("=SORT(A1:A3; 1; -1)", "A1")
    assert res.ok
    assert exec_result(res, [3.0, 1.0, 2.0]) == [3.0, 2.0, 1.0]

    res = translate_formula("=UNIQUE(A1:A5)", "A1")
    assert res.ok
    assert exec_result(res, [1.0, 2.0, 1.0, 3.0, 2.0]) == [1.0, 2.0, 3.0]


def exec_result(res, data):
    import math

    import numpy as np

    locs = {"data": data, "np": np, "calc": calc, "math": math, "dt": dt}
    code = f"result = {res.code}"
    exec(code, locs)
    return locs["result"]


def test_translate_tier_d_functions():
    # Financial
    res = translate_formula("=PMT(0.05/12; 60; 10000)", "A1")
    assert res.ok
    assert "calc.pmt" in res.code
    # PMT(0.05/12, 60, 10000) approx -188.71
    assert abs(exec_result(res, []) - (-188.712336)) < 1e-2

    res = translate_formula("=FV(0.05/12; 60; -200; -10000)", "A1")
    assert res.ok
    assert "calc.fv" in res.code
    # FV approx 26434.80
    assert abs(exec_result(res, []) - 26434.80) < 1.0

    res = translate_formula("=PV(0.05/12; 60; -200; 26434.80)", "A1")
    assert res.ok
    assert "calc.pv" in res.code
    assert abs(exec_result(res, []) - (-10000.0)) < 1.0

    # Math
    res = translate_formula("=MROUND(1.23; 0.5)", "A1")
    assert res.ok
    assert exec_result(res, []) == 1.0

    res = translate_formula("=SUMSQ(A1; B1)", "A1")
    assert res.ok
    assert exec_result(res, [3.0, 4.0]) == 25.0

    # Information
    res = translate_formula("=ISEVEN(4)", "A1")
    assert res.ok
    assert exec_result(res, []) is True

    res = translate_formula("=ISODD(4)", "A1")
    assert res.ok
    assert exec_result(res, []) is False

    # Date/Time
    res = translate_formula("=DAYS(46185; 46181)", "A1")  # 2026-06-12 - 2026-06-08
    assert res.ok
    assert exec_result(res, []) == 4.0

    res = translate_formula("=TIME(12; 0; 0)", "A1")
    assert res.ok
    assert exec_result(res, []) == 0.5

    res = translate_formula("=TRIMMEAN(A1:A5; 0.2)", "A1")
    assert res.ok
    assert exec_result(res, [1.0, 2.0, 3.0, 4.0, 5.0]) == 3.0

    res = translate_formula("=FORECAST(6; A1:A5; B1:B5)", "A1")
    assert res.ok
    # y = [1,2,3,4,5], x = [1,2,3,4,5] -> y = x. for x=6, y=6.
    assert exec_result(res, [[1.0, 2.0, 3.0, 4.0, 5.0], [1.0, 2.0, 3.0, 4.0, 5.0]]) == 6.0


_DB_ROWS = [
    ["Tree", "Height", "Age", "Yield", "Profit"],
    ["Apple", 18.0, 20.0, 14.0, 105.0],
    ["Pear", 12.0, 12.0, 10.0, 96.0],
    ["Cherry", 13.0, 7.0, 8.0, 105.0],
    ["Apple", 14.0, 15.0, 10.0, 75.0],
    ["Pear", 9.0, 8.0, 8.0, 77.0],
    ["Apple", 8.0, 9.0, 6.0, 45.0],
]
_DB_CRIT = [["Tree", "Height"], ["Apple", ">10"]]
_DB_CRIT_SINGLE = [["Tree", "Height"], ["Apple", ">15"]]


@pytest.mark.parametrize(
    "formula, data, expected",
    [
        # Hyperbolic, math, text, date/time, info
        pytest.param("=ACOSH(2)", [], pytest.approx(1.3169578969, abs=1e-9), id="acosh"),
        pytest.param("=ASINH(1)", [], pytest.approx(0.881373587, abs=1e-9), id="asinh"),
        pytest.param("=ATANH(0.5)", [], pytest.approx(0.5493061443, abs=1e-9), id="atanh"),
        pytest.param("=COSH(1)", [], pytest.approx(1.5430806348, abs=1e-9), id="cosh"),
        pytest.param("=SINH(1)", [], pytest.approx(1.1752011936, abs=1e-9), id="sinh"),
        pytest.param("=TANH(1)", [], pytest.approx(0.76159415595, abs=1e-9), id="tanh"),
        pytest.param("=FACT(5)", [], 120.0, id="fact"),
        pytest.param("=COMBIN(5; 2)", [], 10.0, id="combin"),
        pytest.param('=REPT("abc"; 3)', [], "abcabcabc", id="rept"),
        pytest.param('=EXACT("abc"; "ABC")', [], False, id="exact_false"),
        pytest.param('=EXACT("abc"; "abc")', [], True, id="exact_true"),
        pytest.param('=ARABIC("MCMLXXXIV")', [], 1984.0, id="arabic"),
        pytest.param('=DATEVALUE("2023-10-05")', [], float(dt.date(2023, 10, 5).toordinal() - 693594), id="datevalue"),
        pytest.param('=TIMEVALUE("12:00:00")', [], 0.5, id="timevalue"),
        pytest.param("=N(TRUE)", [], 1.0, id="n_bool"),
        pytest.param('=N("abc")', [], 0.0, id="n_str"),
        pytest.param("=TYPE(123)", [], 1.0, id="type_num"),
        pytest.param('=TYPE("abc")', [], 2.0, id="type_str"),
        pytest.param("=TYPE(TRUE)", [], 4.0, id="type_bool"),
        # Lookup, date/time, logic, char, database
        pytest.param('=CHOOSE(2; "a"; "b"; "c")', [], "b", id="choose"),
        pytest.param("=ADDRESS(1; 1)", [], "$A$1", id="address"),
        pytest.param("=AREAS(A1:B10)", [], 1.0, id="areas"),
        pytest.param("=YEARFRAC(45000; 45365)", [], pytest.approx(1.0, abs=0.1), id="yearfrac"),
        pytest.param("=DAYS360(44927; 45292)", [], 360.0, id="days360"),  # 2023-01-01 to 2024-01-01
        pytest.param("=NETWORKDAYS.INTL(46181; 46185; 1)", [], 5.0, id="networkdays_intl"),  # Mon-Fri
        pytest.param("=WORKDAY.INTL(46181; 4; 1)", [], 46185.0, id="workday_intl"),  # Mon + 4 days -> Fri
        pytest.param("=XOR(TRUE; FALSE; TRUE)", [], False, id="xor_false"),
        pytest.param("=XOR(TRUE; FALSE; FALSE)", [], True, id="xor_true"),
        pytest.param("=CHAR(65)", [], "A", id="char"),
        pytest.param('=CODE("A")', [], 65.0, id="code"),
        pytest.param('=DCOUNT(A1:E7; "Yield"; G1:H2)', [_DB_ROWS, _DB_CRIT], 2.0, id="dcount"),
        pytest.param('=DSUM(A1:E7; "Profit"; G1:H2)', [_DB_ROWS, _DB_CRIT], 180.0, id="dsum"),
        pytest.param('=DAVERAGE(A1:E7; "Yield"; G1:H2)', [_DB_ROWS, _DB_CRIT], 12.0, id="daverage"),
        pytest.param('=DMAX(A1:E7; "Height"; G1:H2)', [_DB_ROWS, _DB_CRIT], 18.0, id="dmax"),
        pytest.param('=DMIN(A1:E7; "Height"; G1:H2)', [_DB_ROWS, _DB_CRIT], 14.0, id="dmin"),
        pytest.param('=DCOUNTA(A1:E7; "Tree"; G1:H2)', [_DB_ROWS, _DB_CRIT], 2.0, id="dcounta"),
        pytest.param('=DGET(A1:E7; "Yield"; G1:H2)', [_DB_ROWS, _DB_CRIT], "#NUM!", id="dget_multi"),
        pytest.param('=DGET(A1:E7; "Yield"; G1:H2)', [_DB_ROWS, _DB_CRIT_SINGLE], 14.0, id="dget_single"),
        pytest.param('=DPRODUCT(A1:E7; "Yield"; G1:H2)', [_DB_ROWS, _DB_CRIT], 140.0, id="dproduct"),
        pytest.param('=DSTDEV(A1:E7; "Yield"; G1:H2)', [_DB_ROWS, _DB_CRIT], pytest.approx(np.std([14.0, 10.0], ddof=1), abs=1e-9), id="dstdev"),
        pytest.param('=DSTDEVP(A1:E7; "Yield"; G1:H2)', [_DB_ROWS, _DB_CRIT], pytest.approx(np.std([14.0, 10.0], ddof=0), abs=1e-9), id="dstdevp"),
        pytest.param('=DVAR(A1:E7; "Yield"; G1:H2)', [_DB_ROWS, _DB_CRIT], pytest.approx(np.var([14.0, 10.0], ddof=1), abs=1e-9), id="dvar"),
        pytest.param('=DVARP(A1:E7; "Yield"; G1:H2)', [_DB_ROWS, _DB_CRIT], pytest.approx(np.var([14.0, 10.0], ddof=0), abs=1e-9), id="dvarp"),
        pytest.param("=ISOWEEKNUM(45292)", [], 1.0, id="isoweeknum"),
        pytest.param("=FACTDOUBLE(6)", [], 48.0, id="factdouble"),
        pytest.param("=COMBINA(4; 3)", [], 20.0, id="combina"),
        pytest.param("=AVEDEV(A1:A3)", [2, 4, 9], pytest.approx(8.0 / 3.0, abs=1e-9), id="avedev"),
        pytest.param("=GEOMEAN(A1:A3)", [2, 8, 4], pytest.approx(4.0, abs=1e-9), id="geomean"),
        pytest.param("=HARMEAN(A1:A3)", [2, 4, 1], pytest.approx(3 / (1 / 2.0 + 1 / 4.0 + 1 / 1.0), abs=1e-9), id="harmean"),
        pytest.param("=NPV(0.1; A1:A3)", [100, 200, 300], pytest.approx(100 / 1.1 + 200 / 1.21 + 300 / 1.331, abs=1e-9), id="npv"),
        pytest.param("=IRR(A1:A3)", [-100, 110, 0], pytest.approx(0.1, abs=1e-7), id="irr"),
        # Stats, regression, trig
        pytest.param("=DEVSQ(A1:A3)", [1.0, 3.0, 5.0], 8.0, id="devsq"),
        pytest.param("=KURT(A1:A4)", [1.0, 2.0, 3.0, 4.0], pytest.approx(-1.2, abs=1e-9), id="kurt"),  # Excel/Calc kurtosis of [1,2,3,4] is -1.2
        pytest.param("=SKEW(A1:A3)", [1.0, 2.0, 3.0], pytest.approx(0.0, abs=1e-9), id="skew"),
        pytest.param("=SLOPE(A1:A3; B1:B3)", [[2.0, 4.0, 6.0], [1.0, 2.0, 3.0]], 2.0, id="slope"),
        pytest.param("=INTERCEPT(A1:A3; B1:B3)", [[3.0, 5.0, 7.0], [1.0, 2.0, 3.0]], 1.0, id="intercept"),
        pytest.param("=RSQ(A1:A3; B1:B3)", [[2.0, 4.0, 6.0], [1.0, 2.0, 3.0]], 1.0, id="rsq"),
        pytest.param("=STEYX(A1:A3; B1:B3)", [[2.0, 4.0, 6.0], [1.0, 2.0, 3.0]], 0.0, id="steyx"),
        pytest.param("=ACOT(1)", [], pytest.approx(math.pi / 4, abs=1e-9), id="acot"),
        pytest.param("=ACOTH(2)", [], pytest.approx(0.5 * math.log(3.0), abs=1e-9), id="acoth"),
        pytest.param("=COT(PI()/4)", [], pytest.approx(1.0, abs=1e-9), id="cot"),
        pytest.param("=COTH(1)", [], pytest.approx(1.0 / math.tanh(1.0), abs=1e-9), id="coth"),
        pytest.param("=CSC(PI()/2)", [], pytest.approx(1.0, abs=1e-9), id="csc"),
        pytest.param("=CSCH(1)", [], pytest.approx(1.0 / math.sinh(1.0), abs=1e-9), id="csch"),
        pytest.param("=SEC(0)", [], pytest.approx(1.0, abs=1e-9), id="sec"),
        pytest.param("=SECH(0)", [], pytest.approx(1.0, abs=1e-9), id="sech"),
        # Statistical A, error, engineering, bitwise
        pytest.param("=STDEVA(A1:A3)", [1.0, 2.0, "x"], pytest.approx(1.0, abs=1e-9), id="stdeva"),  # [1, 2, "x"] -> [1, 2, 0]
        pytest.param("=STDEVPA(A1:A3)", [1.0, 2.0, "x"], pytest.approx(0.81649658, abs=1e-7), id="stdevpa"),
        pytest.param("=VARA(A1:A3)", [1.0, 2.0, "x"], pytest.approx(1.0, abs=1e-9), id="vara"),
        pytest.param("=VARPA(A1:A3)", [1.0, 2.0, "x"], pytest.approx(2 / 3.0, abs=1e-9), id="varpa"),  # [1, 2, "x"] -> [1, 2, 0]
        pytest.param("=MAXA(A1:A3)", [-1.0, -2.0, "x"], 0.0, id="maxa"),  # "x" becomes 0.0
        pytest.param("=MINA(A1:A3)", [1.0, 2.0, True], 1.0, id="mina_true"),  # TRUE becomes 1.0
        pytest.param("=MINA(A1:A3)", [1.0, 2.0, False], 0.0, id="mina_false"),  # FALSE becomes 0.0
        pytest.param("=ERF(0.5)", [], pytest.approx(0.520499877, abs=1e-7), id="erf"),
        pytest.param("=ERFC(0.5)", [], pytest.approx(0.479500123, abs=1e-7), id="erfc"),
        pytest.param("=DELTA(5; 5)", [], 1.0, id="delta"),
        pytest.param("=GESTEP(10; 5)", [], 1.0, id="gestep"),
        pytest.param("=SQRTPI(1)", [], pytest.approx(math.sqrt(math.pi), abs=1e-9), id="sqrtpi"),
        pytest.param("=BITAND(6; 3)", [], 2.0, id="bitand"),
        pytest.param("=BITOR(6; 3)", [], 7.0, id="bitor"),
        pytest.param("=BITXOR(6; 3)", [], 5.0, id="bitxor"),
        pytest.param("=BITLSHIFT(1; 3)", [], 8.0, id="bitlshift"),
        pytest.param("=BITRSHIFT(8; 3)", [], 1.0, id="bitrshift"),
        # Group D
        pytest.param("=XIRR(A1:A3, B1:B3)", [[-10000, 2750, 4250], [733042, 733102, 733345]], lambda v: abs(v) > 0, id="xirr"),
        pytest.param("=XNPV(0.1, A1:A3, B1:B3)", [[-10000, 2750, 4250], [733042, 733102, 733345]], lambda v: abs(v) > 0, id="xnpv"),
        pytest.param("=NA()", [], math.isnan, id="na"),
        pytest.param("=AGGREGATE(9, 4, A1:A3)", [1, 2, 3], 6, id="aggregate"),
        pytest.param("=BASE(10, 2)", [], "1010", id="base"),
        pytest.param('=DECIMAL("1010", 2)', [], 10, id="decimal"),
        pytest.param("=MULTINOMIAL(2, 3)", [], 10, id="multinomial"),
        pytest.param("=SERIESSUM(1, 0, 1, A1:A3)", [1, 2, 3], 6, id="seriessum"),
        pytest.param("=FREQUENCY(A1:A5, B1:B2)", [[1, 2, 3, 4, 5], [2, 4]], [2, 2, 1], id="frequency"),
        pytest.param("=GROWTH(A1:A3, B1:B3, C1:C2)", [[10, 20, 40], [1, 2, 3], [4, 5]], lambda v: len(v) == 2, id="growth"),
        # Complex
        pytest.param('=COMPLEX(3; 4; "j")', [], "3+4j", id="complex"),
        pytest.param('=IMABS("3+4i")', [], 5.0, id="imabs"),
        pytest.param('=IMAGINARY("3+4i")', [], 4.0, id="imaginary"),
        pytest.param('=IMARGUMENT("0+1i")', [], pytest.approx(math.pi / 2, abs=1e-9), id="imargument"),
        pytest.param('=IMCONJUGATE("3+4i")', [], "3-4i", id="imconjugate"),
        pytest.param('=IMCOS("1+1i")', [], lambda v: "0.83373" in v, id="imcos"),
        pytest.param('=IMDIV("10+10i"; "2")', [], "5+5i", id="imdiv"),
        pytest.param('=IMEXP("1i")', [], lambda v: "0.5403" in v, id="imexp"),
        pytest.param('=IMLN("2.718281828459045")', [], "1", id="imln"),
        pytest.param('=IMLOG10("100")', [], "2", id="imlog10"),
        pytest.param('=IMLOG2("8")', [], "3", id="imlog2"),
        pytest.param('=IMPOWER("2"; 3)', [], "8", id="impower"),
        pytest.param('=IMPRODUCT("2+2i"; "2-2i")', [], "8", id="improduct"),
        pytest.param('=IMREAL("3+4i")', [], 3.0, id="imreal"),
        pytest.param('=IMSIN("1i")', [], lambda v: "1.1752" in v, id="imsin"),
        # Group E matrix
        pytest.param("=MDETERM(A1:B2)", [[[1, 2], [3, 4]]], pytest.approx(-2.0, abs=1e-9), id="mdeterm"),
        pytest.param("=MINVERSE(A1:B2)", [[[1, 2], [3, 4]]], lambda v: abs(v[0][0] - (-2.0)) < 1e-9, id="minverse"),
        pytest.param("=MMULT(A1:B2; C1:D2)", [[[1, 2], [3, 4]], [[2, 0], [1, 2]]], lambda v: v[0][0] == 4.0, id="mmult"),
        pytest.param("=MTRANS(A1:B2)", [[[1, 2], [3, 4]]], lambda v: v[0][1] == 3.0, id="mtrans"),
        pytest.param("=MUNIT(2)", [], lambda v: v[0][0] == 1.0 and v[0][1] == 0.0, id="munit"),
        # Group F distributions
        pytest.param("=EXPONDIST(1.5; 2; 1)", [], pytest.approx(0.9502129316, abs=1e-6), id="expondist_cdf"),
        pytest.param("=EXPONDIST(1.5; 2; 0)", [], pytest.approx(0.0995741367, abs=1e-6), id="expondist_pdf"),
        pytest.param("=FDIST(1.5; 2; 3)", [], pytest.approx(0.3535533906, abs=1e-6), id="fdist"),
        pytest.param("=FINV(0.3535533906; 2; 3)", [], pytest.approx(1.5, abs=1e-6), id="finv"),
        pytest.param("=FISHER(0.5)", [], pytest.approx(0.5493061443, abs=1e-6), id="fisher"),
        pytest.param("=FISHERINV(0.5493061443)", [], pytest.approx(0.5, abs=1e-6), id="fisherinv"),
        pytest.param("=GAMMA(1.5)", [], pytest.approx(0.8862269255, abs=1e-6), id="gamma"),
        pytest.param("=GAMMADIST(1.5; 2; 3; 1)", [], pytest.approx(0.0902040104, abs=1e-6), id="gammadist"),
        pytest.param("=GAMMAINV(0.0902040104; 2; 3)", [], pytest.approx(1.5, abs=1e-6), id="gammainv"),
        pytest.param("=GAMMALN(1.5)", [], pytest.approx(-0.1207822376, abs=1e-6), id="gammaln"),
        pytest.param("=GAUSS(1.5)", [], pytest.approx(0.4331927987, abs=1e-6), id="gauss"),
        pytest.param("=HYPGEOMDIST(1; 3; 4; 10)", [], pytest.approx(0.5, abs=1e-6), id="hypgeomdist"),
        pytest.param("=LOGINV(0.2975321419; 2; 3)", [], pytest.approx(1.5, abs=1e-6), id="loginv"),
        pytest.param("=LOGNORMDIST(1.5; 2; 3; 1)", [], pytest.approx(0.2975321419, abs=1e-6), id="lognormdist"),
        pytest.param("=NEGBINOMDIST(5; 10; 0.5)", [], pytest.approx(0.0610961914, abs=1e-6), id="negbinomdist"),
        pytest.param("=NORMDIST(1.5; 2; 3; 1)", [], pytest.approx(0.4338161674, abs=1e-6), id="normdist"),
        # Group B financial
        pytest.param("=DOLLARDE(1.02; 16)", [], pytest.approx(1.125, abs=1e-9), id="dollarde"),
        pytest.param("=DOLLARFR(1.125; 16)", [], pytest.approx(1.02, abs=1e-9), id="dollarfr"),
        pytest.param("=DURATION(45292; 47118; 0.08; 0.09; 2; 1)", [], pytest.approx(4.196131, abs=1e-4), id="duration"),
        pytest.param("=EFFECT(0.0525; 4)", [], pytest.approx(0.053542667, abs=1e-5), id="effect"),
        pytest.param("=FVSCHEDULE(10000; A1:A3)", [0.09, 0.11, 0.1], pytest.approx(13308.9, abs=1e-5), id="fvschedule"),
        pytest.param("=INTRATE(45292; 47118; 100000; 105000; 2)", [], pytest.approx(0.0098576, abs=1e-5), id="intrate"),
        pytest.param("=IPMT(0.08/12; 1; 36; 8000)", [], pytest.approx(-53.33333333, abs=1e-5), id="ipmt"),
        pytest.param("=ISPMT(0.08/12; 1; 36; 8000)", [], pytest.approx(-51.8518518, abs=1e-5), id="ispmt"),
        pytest.param("=MDURATION(45292; 47118; 0.08; 0.09; 2; 1)", [], pytest.approx(4.0154364, abs=1e-4), id="mduration"),
        pytest.param("=MIRR(A1:A4; 0.1; 0.12)", [-120000, 39000, 30000, 21000], pytest.approx(-0.048044655249, abs=1e-3), id="mirr"),
        pytest.param("=NOMINAL(0.053543; 4)", [], pytest.approx(0.0525, abs=1e-4), id="nominal"),
        pytest.param("=NPER(0.12/12; -100; -1000; 10000)", [], pytest.approx(60.0821229, abs=1e-5), id="nper"),
        pytest.param("=ODDFPRICE(45292; 47118; 45000; 45500; 0.08; 0.09; 100; 2; 1)", [], pytest.approx(93.483446, abs=1e-4), id="oddfprice"),
        pytest.param("=ODDFYIELD(45292; 47118; 45000; 45500; 0.08; 95; 100; 2; 1)", [], pytest.approx(0.092309, abs=1e-4), id="oddfyield"),
        pytest.param("=ODDLPRICE(45292; 47118; 45000; 0.08; 0.09; 100; 2; 1)", [], pytest.approx(94.5685732, abs=1e-4), id="oddlprice"),
        # P2 & logical/trig/date/lookup
        pytest.param("=CONCAT(A1;B1)", ["foo", "bar"], "foobar", id="concat"),
        pytest.param("=LEFT(A1;2)", "hello", "he", id="left"),
        pytest.param("=LEN(A1)", "hello", 5, id="len"),
        pytest.param("=TODAY()", [], dt.date.today().toordinal() - 693594, id="today"),
        pytest.param("=STDEV(A1:A10)", [1.0, 2.0, 3.0], pytest.approx(1.0, abs=1e-9), id="stdev"),
        pytest.param("=VLOOKUP(A1;B1:C10;2;0)", [2, [[1, 10], [2, 20]]], 20, id="vlookup"),
        pytest.param("=IFERROR(A1; 0)", float("nan"), 0, id="iferror_nan"),
        pytest.param("=IFNA(A1; 1)", float("nan"), 1, id="ifna_nan"),
        pytest.param('=SWITCH(A1; 1; "one"; 2; "two"; "other")', 2, "two", id="switch"),
        pytest.param("=ASIN(A1)", 1.0, pytest.approx(math.pi / 2, abs=1e-9), id="asin"),
        pytest.param("=ATAN2(A1; B1)", [1.0, 1.0], pytest.approx(math.pi / 4, abs=1e-9), id="atan2"),
        pytest.param("=GCD(A1; B1)", [12, 8], 4, id="gcd"),
        pytest.param("=DATE(2023; 10; 5)", [], dt.date(2023, 10, 5).toordinal() - 693594, id="date"),
        pytest.param("=HOUR(A1)", 0.5, 12, id="hour"),
        pytest.param("=ROW(C10:C20)", [], lambda v: list(v) == list(range(10, 21)), id="row_range"),
        pytest.param("=ROWS(A1:B10)", [], 10, id="rows"),
        pytest.param("=COLUMNS(A1:B10)", [], 2, id="columns"),
        # Financial group A & distributions & stats
        pytest.param("=ACCRINT(A1, B1, C1, 0.05, 100, 2)", [45000, 45100, 45200], pytest.approx(2.722222, abs=1e-4), id="accrint"),
        pytest.param("=DB(10000, 1000, 5, 1)", [], pytest.approx(3690.0, abs=1e-4), id="db"),
        pytest.param("=NORMINV(0.5; 0; 1)", [], pytest.approx(0.0, abs=1e-6), id="norminv"),
        pytest.param("=NORMSDIST(1)", [], pytest.approx(0.8413447, abs=1e-6), id="normsdist"),
        pytest.param("=NORMSINV(0.5)", [], pytest.approx(0.0, abs=1e-6), id="normsinv"),
        pytest.param("=PERMUT(5; 2)", [], 20.0, id="permut"),
        pytest.param("=POISSON(2; 2; 0)", [], pytest.approx(0.2706706, abs=1e-6), id="poisson"),
        pytest.param("=STANDARDIZE(42; 40; 1.5)", [], pytest.approx(1.3333333, abs=1e-6), id="standardize"),
        pytest.param("=TDIST(1.96; 60; 2)", [], pytest.approx(0.0546449, abs=1e-6), id="tdist"),
        pytest.param("=TINV(0.05; 60)", [], pytest.approx(2.0002978, abs=1e-6), id="tinv"),
        pytest.param("=WEIBULL(105; 20; 100; 1)", [], pytest.approx(0.9295814, abs=1e-6), id="weibull"),
        pytest.param("=PEARSON(A1:A10; B1:B10)", [[1, 2, 3], [2, 4, 6]], pytest.approx(1.0, abs=1e-6), id="pearson"),
        pytest.param("=PERCENTRANK(A1:A10; 3)", [1, 2, 3, 4, 5], pytest.approx(0.5, abs=1e-6), id="percentrank"),
        pytest.param("=PROB(A1:A10; B1:B10; 1; 2)", [[1, 2, 3], [0.2, 0.5, 0.3]], pytest.approx(0.7, abs=1e-6), id="prob"),
        pytest.param("=TTEST(A1:A10; B1:B10; 2; 1)", [[1, 2, 3], [1, 2, 4]], pytest.approx(0.4226497, abs=1e-6), id="ttest"),
        pytest.param("=ZTEST(A1:A10; 3)", [1, 2, 3, 4, 5], pytest.approx(0.5, abs=1e-6), id="ztest"),
        pytest.param("=ASC(A1)", "ＡＢＣ", "ABC", id="asc"),
        # Group H & I
        pytest.param("=CLEAN(A1)", "a\x00b", "ab", id="clean"),
        pytest.param("=DOLLAR(A1; 2)", 1234.5, "$1,234.50", id="dollar"),
        pytest.param("=ENCODEURL(A1)", "a b", "a%20b", id="encodeurl"),
        pytest.param("=FIXED(A1; 2; TRUE)", 1234.5, "1234.50", id="fixed"),
        pytest.param("=JIS(A1)", "ABC", "ＡＢＣ", id="jis"),
        pytest.param('=NUMBERVALUE(A1; "."; ",")', "1,234.5", 1234.5, id="numbervalue"),
        pytest.param("=T(A1)", "hi", "hi", id="t_text"),
        pytest.param('=TEXTAFTER(A1; "-")', "a-b", "b", id="textafter"),
        pytest.param('=TEXTBEFORE(A1; "-")', "a-b", "a", id="textbefore"),
        pytest.param('=TEXTSPLIT(A1; "-")', "a-b", [["a", "b"]], id="textsplit"),
        pytest.param("=UNICHAR(A1)", 65, "A", id="unichar"),
        pytest.param("=UNICODE(A1)", "A", 65.0, id="unicode"),
        pytest.param("=BESSELI(A1; B1)", [1.5, 1], pytest.approx(0.9816664, abs=1e-6), id="besseli"),
        pytest.param("=BESSELJ(A1; B1)", [1.5, 1], pytest.approx(0.5579365, abs=1e-6), id="besselj"),
        pytest.param("=BESSELK(A1; B1)", [1.5, 1], pytest.approx(0.2773878, abs=1e-6), id="besselk"),
        pytest.param("=BESSELY(A1; B1)", [1.5, 1], pytest.approx(-0.4123086, abs=1e-6), id="bessely"),
        pytest.param('=EUROCONVERT(A1; "ATS"; "EUR")', 100, pytest.approx(7.27, abs=1e-4), id="euroconvert"),
        pytest.param("=IMCOSH(A1)", "1+0i", lambda v: complex(v.replace("i", "j")) == pytest.approx(cmath.cosh(1), abs=1e-6), id="imcosh"),
        pytest.param("=IMCOT(A1)", "1+0i", lambda v: complex(v.replace("i", "j")) == pytest.approx(1 / cmath.tan(1), abs=1e-6), id="imcot"),
        pytest.param("=IMCSC(A1)", "1+0i", lambda v: complex(v.replace("i", "j")) == pytest.approx(1 / cmath.sin(1), abs=1e-6), id="imcsc"),
        pytest.param("=IMCSCH(A1)", "1+0i", lambda v: complex(v.replace("i", "j")) == pytest.approx(1 / cmath.sinh(1), abs=1e-6), id="imcsch"),
        pytest.param("=IMSEC(A1)", "1+0i", lambda v: complex(v.replace("i", "j")) == pytest.approx(1 / cmath.cos(1), abs=1e-6), id="imsec"),
        pytest.param("=IMSECH(A1)", "1+0i", lambda v: complex(v.replace("i", "j")) == pytest.approx(1 / cmath.cosh(1), abs=1e-6), id="imsech"),
        pytest.param("=IMSINH(A1)", "1+0i", lambda v: complex(v.replace("i", "j")) == pytest.approx(cmath.sinh(1), abs=1e-6), id="imsinh"),
        pytest.param("=IMSQRT(A1)", "4", "2", id="imsqrt"),
        pytest.param("=IMSUB(A1; B1)", ["3+4i", "1+2i"], "2+2i", id="imsub"),
        pytest.param("=IMSUM(A1; B1)", ["3+4i", "1+2i"], "4+6i", id="imsum"),
        pytest.param("=IMTAN(A1)", "1+0i", lambda v: complex(v.replace("i", "j")) == pytest.approx(cmath.tan(1), abs=1e-6), id="imtan"),
        pytest.param("=IMTANH(A1)", "1+0i", lambda v: complex(v.replace("i", "j")) == pytest.approx(cmath.tanh(1), abs=1e-6), id="imtanh"),
    ],
)
def test_translate_and_exec_functions(formula, data, expected):
    res = translate_formula(formula, "A1")
    assert res.ok
    val = exec_result(res, data)
    if isinstance(expected, bool):
        assert val is expected
    elif callable(expected):
        assert expected(val)
    else:
        assert val == expected


@pytest.mark.parametrize(
    "formula",
    [
        pytest.param("=BAHTTEXT(A1)", id="bahttext"),
        pytest.param("=YIELD(A1, B1, C1, D1, E1, F1)", id="yield"),
        pytest.param("=ISFORMULA(A1)", id="isformula"),
        pytest.param("=ISREF(A1)", id="isref"),
        pytest.param("=AGGREGATE(14, 4, A1:A3, 1)", id="aggregate_unsupported_fn"),
    ],
)
def test_translate_unsupported_group_d_functions(formula):
    res = translate_formula(formula, "A1")
    assert not res.ok
    assert res.reason == "UNSUPPORTED_FUNCTION"


@pytest.mark.parametrize(
    "formula, expected_substr",
    [
        pytest.param("=BETADIST(0.5; 2; 3)", "calc.betadist(", id="betadist"),
        pytest.param("=BINOMDIST(2; 10; 0.5; 0)", None, id="binomdist"),
        pytest.param("=CONFIDENCE(0.05; 2.5; 50)", None, id="confidence"),
        pytest.param("=LINEST(A1:A3)", None, id="linest"),
        pytest.param("=LOGEST(A1:A3)", None, id="logest"),
        pytest.param("=TREND(A1:A3)", None, id="trend"),
        pytest.param("=BETAINV(0.5; 2; 3)", "calc.betainv(", id="betainv"),
        pytest.param("=CHIDIST(3; 2)", None, id="chidist"),
        pytest.param("=CHIINV(0.5; 2)", None, id="chiinv"),
        pytest.param("=CRITBINOM(10; 0.5; 0.5)", None, id="critbinom"),
    ],
)
def test_translate_group_e_emit_only(formula, expected_substr):
    """scipy is optional in the venv; these helpers return NaN without it, so only check that the formula translates and names the helper."""
    res = translate_formula(formula, "A1")
    assert res.ok
    if expected_substr is not None:
        assert expected_substr in res.code


def test_translate_multiple_arguments_aggregates():
    res = translate_formula("=SUM(C7; C12)", "A1")
    assert not res.ok
    assert res.reason == "UNSUPPORTED_FUNCTION"

    res = translate_formula("=AVERAGE(A1:A3; B1:B3)", "A1")
    assert res.ok
    assert "np.concatenate" in res.code

    res = translate_formula("=PRODUCT(A1; B1)", "A1")
    assert res.ok
    assert "np.prod" in res.code

    res = translate_formula("=COUNT(A1; B1)", "A1")
    assert res.ok
    assert "sum(np.sum" in res.code


def test_translate_emitter_precedence():
    # Bug 1: parenthesize emitted sub-expressions so operator precedence is preserved.
    # MOD(a+b, c) must not emit a + b % c
    res = translate_formula("=MOD(A1+B1, C1)", "A1")
    assert res.ok
    assert res.code == "((data[0] + data[1]) % data[2])"
    # Exec with real values: (10 + 5) % 4 == 3 (whereas 10 + (5 % 4) == 11)
    locs = {"data": [10.0, 5.0, 4.0], "np": np, "calc": calc}
    exec(f"result = {res.code}", locs)
    assert locs["result"] == 3.0

    # 2*MOD(A1, B1) must compute 2 * (a % b), not (2 * a) % b
    res = translate_formula("=2*MOD(A1, B1)", "A1")
    assert res.ok
    assert res.code == "(2 * (data[0] % data[1]))"
    locs = {"data": [7.0, 4.0], "np": np, "calc": calc}
    exec(f"result = {res.code}", locs)
    assert locs["result"] == 6.0  # 2 * (7 % 4) == 6; whereas (2*7) % 4 == 2

    # 1/LOG(A1, 2)
    res = translate_formula("=1/LOG(A1, 2)", "A1")
    assert res.ok
    assert "(1 / (np.log(data) / np.log(2)))" in res.code
    locs = {"data": 8.0, "np": np, "calc": calc}
    exec(f"result = {res.code}", locs)
    assert abs(locs["result"] - (1.0 / 3.0)) < 1e-6

    # 1/ROUNDUP(A1, 1)
    res = translate_formula("=1/ROUNDUP(A1, 1)", "A1")
    assert res.ok
    assert "(1 / " in res.code
    locs = {"data": 1.21, "np": np, "calc": calc}
    exec(f"result = {res.code}", locs)
    assert abs(locs["result"] - (1.0 / 1.3)) < 1e-6

    # 1/POWER(A1, 2)
    res = translate_formula("=1/POWER(A1, 2)", "A1")
    assert res.ok
    assert res.code == "(1 / (data ** 2))"
    locs = {"data": 4.0, "np": np, "calc": calc}
    exec(f"result = {res.code}", locs)
    assert locs["result"] == 1.0 / 16.0

    # 1/QUOTIENT(A1, 2)
    res = translate_formula("=1/QUOTIENT(A1, 2)", "A1")
    assert res.ok
    assert res.code == "(1 / (data // 2))"
    locs = {"data": 10.0, "np": np, "calc": calc}
    exec(f"result = {res.code}", locs)
    assert locs["result"] == 0.2


def test_translate_function_arity_errors():
    # Bug 2: catch IndexError when a function gets too few args and report UNSUPPORTED_ARITY
    for bad_f in ["=MOD(A1)", "=ABS()", "=IF()", "=DATE(2023, 10)", "=ROUND()", "=SWITCH(A1)"]:
        res = translate_formula(bad_f, "A1")
        assert not res.ok, f"Expected failure for {bad_f}"
        assert res.reason == "UNSUPPORTED_ARITY", f"Expected UNSUPPORTED_ARITY for {bad_f}, got {res.reason}"


def test_translate_row_column():
    # Bug 3: ROW/COLUMN strips $ and sheet prefix; an unparseable cell_addr raises unsupported
    # Absolute reference with $
    res = translate_formula("=ROW($A$5)", "A1")
    assert res.ok
    assert res.code == "np.float64(5)"

    # Cross-sheet range reference with sheet prefix
    res = translate_formula("=ROW(Sheet2.A1:A3)", "A1")
    assert res.ok
    assert "1.0, 2.0, 3.0" in res.code

    res = translate_formula("=ROW(Sheet2.$A$1:$A$3)", "A1")
    assert res.ok
    assert "1.0, 2.0, 3.0" in res.code

    # COLUMN with absolute reference
    res = translate_formula("=COLUMN($C$5)", "A1")
    assert res.ok
    assert res.code == "np.float64(3)"

    # COLUMN with cross-sheet range
    res = translate_formula("=COLUMN(Sheet2.B1:D3)", "A1")
    assert res.ok
    assert "2.0, 3.0, 4.0" in res.code

    # Argless ROW/COLUMN with provided cell_addr
    res = translate_formula("=ROW()", cell_addr="B10")
    assert res.ok
    assert res.code == "np.float64(10)"

    res = translate_formula("=COLUMN()", cell_addr="B10")
    assert res.ok
    assert res.code == "np.float64(2)"

    # Argless ROW/COLUMN with unparseable cell_addr fails as unsupported
    res = translate_formula("=ROW()", "ZZ")
    assert not res.ok
    assert res.reason == "UNSUPPORTED_FUNCTION"

    res = translate_formula("=COLUMN()", "ZZ")
    assert not res.ok
    assert res.reason == "UNSUPPORTED_FUNCTION"

    # Non-range argument fails as unsupported
    res = translate_formula("=ROW(123)", "A1")
    assert not res.ok
    assert res.reason == "UNSUPPORTED_FUNCTION"

    res = translate_formula("=ROW(\"abc\")", "A1")
    assert not res.ok
    assert res.reason == "UNSUPPORTED_FUNCTION"


def test_translate_if_two_arguments():
    # Bug 4: IF with 2 arguments (no else branch) emits False for the missing else branch
    res = translate_formula("=IF(A1>0, B1)", "A1")
    assert res.ok
    assert res.code == "(data[1] if (data[0] > 0) else False)"

    locs = {"data": [5.0, 42.0], "np": np, "calc": calc}
    exec(f"result = {res.code}", locs)
    assert locs["result"] == 42.0

    locs = {"data": [-1.0, 42.0], "np": np, "calc": calc}
    exec(f"result = {res.code}", locs)
    assert locs["result"] is False


def test_translate_scientific_notation():
    # Bug 5: scientific notation with signed exponents
    res = translate_formula("=0.5E+3", "A1")
    assert res.ok
    assert res.code == "500"

    res = translate_formula("=10E+3", "A1")
    assert res.ok
    assert res.code == "10000"

    res = translate_formula("=1.E+3", "A1")
    assert res.ok
    assert res.code == "1000"

    res = translate_formula("=12e-3", "A1")
    assert res.ok
    assert res.code == "0.012"


def test_translate_postfix_percent():
    # Bug 6: Postfix % must bind with Excel precedence (tighter than ^ and * /)
    # -50% -> ((-50) * 0.01)
    res = translate_formula("=-50%", "A1")
    assert res.ok
    assert res.code == "((-50) * 0.01)"
    locs = {"np": np, "calc": calc}
    exec(f"result = {res.code}", locs)
    assert locs["result"] == -0.5

    # 2^50% -> 2^(50%) = 2^0.5 = sqrt(2) ~ 1.41421356
    # Not (2^50)*0.01 = 1.125e13
    res = translate_formula("=2^50%", "A1")
    assert res.ok
    assert res.code == "(2 ** (50 * 0.01))"
    locs = {"np": np, "calc": calc}
    exec(f"result = {res.code}", locs)
    assert abs(locs["result"] - math.sqrt(2)) < 1e-6

    # 2*50% -> 2 * (50 * 0.01) = 1.0
    res = translate_formula("=2*50%", "A1")
    assert res.ok
    assert res.code == "(2 * (50 * 0.01))"
    locs = {"np": np, "calc": calc}
    exec(f"result = {res.code}", locs)
    assert locs["result"] == 1.0

    # Cell reference with percent: A1%
    res = translate_formula("=A1%", "A1")
    assert res.ok
    assert res.code == "(data * 0.01)"
    locs = {"data": 25.0, "np": np, "calc": calc}
    exec(f"result = {res.code}", locs)
    assert locs["result"] == 0.25

    # Compound expression with percent: (A1+B1)%
    res = translate_formula("=(A1+B1)%", "A1")
    assert res.ok
    assert res.code == "((data[0] + data[1]) * 0.01)"
    locs = {"data": [20.0, 30.0], "np": np, "calc": calc}
    exec(f"result = {res.code}", locs)
    assert locs["result"] == 0.5


def test_translate_quoted_sheet_names():
    # Bug 7: keep quotes and casing on quoted sheet names ('My Sheet'!A1)
    from plugin.calc.spreadsheet_import.emit import emit_py_formula

    res = translate_formula("='My Sheet'!A1", "A1")
    assert res.ok
    assert res.data_ranges == ["'My Sheet'!A1"]
    py_f = emit_py_formula(res.code, res.data_ranges)
    assert "'My Sheet'.A1" in py_f

    res = translate_formula("='My Sheet'.A1", "A1")
    assert res.ok
    assert res.data_ranges == ["'My Sheet'.A1"]
    py_f = emit_py_formula(res.code, res.data_ranges)
    assert "'My Sheet'.A1" in py_f


def test_calc_helper_name_and_arity_contracts():
    """Verify that every calc.* helper emitted by the translate table is in HELPER_NAMES with valid arity."""
    import plugin.scripting.venv.calc_functions as vcf

    checked: set[str] = set()
    for fn_name, emitter in FUNCTION_EMITTERS.items():
        accepted = 0
        for count in range(11):
            args = [str(i + 1) for i in range(count)]
            try:
                code = emitter(args)
            except TranslationError:
                continue
            accepted += 1
            for helper in re.findall(r"\bcalc\.(\w+)\(", code):
                assert helper in vcf.HELPER_NAMES, f"calc.{helper} (from {fn_name} with {count} args) not in HELPER_NAMES"
                checked.add(helper)
                if code == f"calc.{helper}({', '.join(args)})":
                    inspect.signature(getattr(vcf, helper)).bind(*args)
        assert accepted > 0, f"{fn_name} accepted no argument count in 0..10"

    assert {"py_str", "iferror", "ifna", "subtotal", "fmt", "poisson", "na", "aggregate"} <= checked


def test_translate_value_text_argument():
    res = translate_formula("=VALUE(A1)", "A1")
    assert res.ok
    assert exec_result(res, "3.5") == 3.5


def test_translate_date_text_arguments():
    res = translate_formula('=DATE("2023";"10";"5")', "A1")
    assert res.ok
    assert exec_result(res, []) == float(dt.date(2023, 10, 5).toordinal() - 693594)


def test_translate_mid_and_left_cell_valued_counts():
    res_mid = translate_formula("=MID(A1;B1;C1)", "A1")
    assert res_mid.ok
    # Multiple cell references are passed as a list of arguments matching data[i]
    locs = {"data": ["abcdef", 2, 3], "np": np, "calc": calc, "math": math, "dt": dt}
    exec(f"result = {res_mid.code}", locs)
    assert locs["result"] == "bcd"

    res_left = translate_formula("=LEFT(A1;B1)", "A1")
    assert res_left.ok
    locs = {"data": ["abcdef", 2], "np": np, "calc": calc, "math": math, "dt": dt}
    exec(f"result = {res_left.code}", locs)
    assert locs["result"] == "ab"


def test_translate_int_argument_truncates_toward_zero():
    res = translate_formula("=LEFT(A1;-3.7)", "A1")
    assert res.ok
    assert exec_result(res, "abcdef") == "abc"


def test_translate_does_not_rewrite_text_literals():
    res_t = translate_formula('=T("int(x)")', "A1")
    assert res_t.ok
    assert exec_result(res_t, []) == "int(x)"

    res_concat = translate_formula('=A1&"float(z)"', "A1")
    assert res_concat.ok
    assert exec_result(res_concat, "a") == "afloat(z)"


def test_translate_rounding_calc_parity():
    res = translate_formula("=ROUND(2.5;0)", "A1")
    assert res.ok
    assert exec_result(res, []) == 3

    res = translate_formula("=ROUND(-2.5;0)", "A1")
    assert res.ok
    assert exec_result(res, []) == -3

    res = translate_formula("=ROUND(2.4;0)", "A1")
    assert res.ok
    assert exec_result(res, []) == 2

    res = translate_formula("=ROUND(1234.5;-2)", "A1")
    assert res.ok
    assert exec_result(res, []) == 1200

    res = translate_formula("=ROUNDUP(-1.23;1)", "A1")
    assert res.ok
    assert exec_result(res, []) == -1.3

    res = translate_formula("=ROUNDUP(1.23;1)", "A1")
    assert res.ok
    assert exec_result(res, []) == 1.3

    res = translate_formula("=ROUNDDOWN(-1.23;1)", "A1")
    assert res.ok
    assert exec_result(res, []) == -1.2

    res = translate_formula("=ROUNDDOWN(1.23;1)", "A1")
    assert res.ok
    assert exec_result(res, []) == 1.2


def test_translate_reports_has_function():
    no_fn = translate_formula("=A1*2", "A1")
    assert no_fn.ok and no_fn.has_function is False

    err_no_fn = translate_formula("=1+#N/A", "A1")
    assert not err_no_fn.ok and err_no_fn.has_function is False

    with_fn = translate_formula("=ABS(A1)", "A1")
    assert with_fn.ok and with_fn.has_function is True

    unsupported = translate_formula("=SUM(A1:A3)", "A1")
    assert not unsupported.ok
    assert unsupported.reason == "UNSUPPORTED_FUNCTION"
    assert unsupported.has_function is True

    bad_syntax = translate_formula("=INVALID(++", "A1")
    assert not bad_syntax.ok and bad_syntax.has_function is None


def test_translate_functions_only_skips_function_free_codegen():
    no_fn = translate_formula("=A1*2", "A1", functions_only=True)
    assert no_fn.ok is False
    assert no_fn.code is None
    assert no_fn.reason is None
    assert no_fn.has_function is False

    with_fn = translate_formula("=ABS(A1)", "A1", functions_only=True)
    assert with_fn.ok is True
    assert with_fn.code == "np.abs(data)"
    assert with_fn.has_function is True



