# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for spreadsheet import P1 formula translation."""

from __future__ import annotations

import datetime as dt
import math
import numpy as np

import pytest

import plugin.scripting.calc_functions as xl

from plugin.calc.spreadsheet_import.preprocess import normalize_lo_formula_for_parse
from plugin.calc.spreadsheet_import.translate import translate_formula


def test_preprocess_semicolon_to_comma():
    assert normalize_lo_formula_for_parse("=IF(A1>0;B1;C1)") == "=IF(A1>0,B1,C1)"


def test_preprocess_preserves_quoted_semicolon():
    assert normalize_lo_formula_for_parse('=CONCAT("a;b";A1)') == '=CONCAT("a;b",A1)'


def test_translate_sum_range():
    result = translate_formula("=SUM(A1:A10)")
    assert not result.ok
    assert result.reason == "UNSUPPORTED_FUNCTION"


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
    assert "[:((2)//1)]" in res.code or "[:2]" in res.code

    res = translate_formula("=LEN(A1)")
    assert res.ok
    assert "len(xl.py_str(data))" in res.code

    # Date
    res = translate_formula("=TODAY()")
    assert res.ok
    assert "dt.date.today().toordinal() - 693594" in res.code
    assert "float(" not in res.code

    # Statistical
    res = translate_formula("=STDEV(A1:A10)")
    assert res.ok
    assert "np.std" in res.code
    assert "ddof=1" in res.code

    # Lookup & Reference
    res = translate_formula("=VLOOKUP(A1;B1:C10;2;0)")
    assert res.ok
    assert "next" in res.code
    assert "r[" in res.code and "-1]" in res.code


def test_translate_p2_logical_trig_date_functions():
    # IFERROR / IFNA
    res = translate_formula("=IFERROR(A1; 0)")
    assert res.ok
    assert "xl.iferror" in res.code
    assert "def " not in res.code

    res = translate_formula("=IFNA(A1; 1)")
    assert res.ok
    assert "xl.ifna" in res.code

    # SWITCH
    res = translate_formula("=SWITCH(A1; 1; \"one\"; 2; \"two\"; \"other\")")
    assert res.ok
    assert "('one' if data == 1 else ('two' if data == 2 else 'other'))" in res.code

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
    assert "dt.date" in res.code and "toordinal() - 693594" in res.code
    assert "int(" not in res.code

    # Time
    res = translate_formula("=HOUR(A1)")
    assert res.ok
    assert "dt.datetime.fromordinal(693594)" in res.code

    # Row/Col/Rows/Cols
    res = translate_formula("=ROW()", "B5")
    assert res.ok
    assert "5" in res.code
    assert "float(" not in res.code

    res = translate_formula("=COLUMN()", "B5")
    assert res.ok
    assert "2" in res.code
    assert "float(" not in res.code

    res = translate_formula("=ROW(C10:C20)", "A1")
    assert res.ok
    assert "np.array" in res.code

    res = translate_formula("=ROWS(A1:B10)")
    assert res.ok
    assert "10" in res.code
    assert "float(" not in res.code

    res = translate_formula("=COLUMNS(A1:B10)")
    assert res.ok
    assert "2" in res.code
    assert "float(" not in res.code


def test_translate_cross_sheet_references():
    res = translate_formula("=Sheet2.A1")
    assert res.ok
    assert res.data_ranges == ["SHEET2.A1"]


def test_translate_and_exec_new_functions():
    import math

    import numpy as np

    base_locs = {"np": np, "xl": xl, "math": math, "dt": dt}

    # 1. SUMIF
    res = translate_formula("=SUMIF(A1:A5; \">10\"; B1:B5)")
    assert res.ok
    locs = {**base_locs, "data": [[5.0, 12.0, 3.0, 15.0, 8.0], [1.0, 2.0, 3.0, 4.0, 5.0]]}
    exec(f"result = {res.code}", locs)
    assert locs["result"] == 6.0

    # 2. SUMIFS
    res = translate_formula("=SUMIFS(B1:B5; A1:A5; \">5\"; A1:A5; \"<=12\")")
    assert res.ok
    locs = {**base_locs, "data": [[1.0, 2.0, 3.0, 4.0, 5.0], [5.0, 12.0, 3.0, 15.0, 8.0]]}
    exec(f"result = {res.code}", locs)
    # sum range is B1:B5 (data[0]): 1, 2, 3, 4, 5.
    # criteria range is A1:A5 (data[1]): 5, 12, 3, 15, 8.
    # A1:A5 > 5 and <= 12 are 12 (index 1) and 8 (index 4).
    # Corresponding elements in B1:B5 are 2 and 5. Sum = 7.0.
    assert locs["result"] == 7.0

    # 3. COUNTIF
    res = translate_formula("=COUNTIF(A1:A5; \"<=5\")")
    assert res.ok
    locs = {**base_locs, "data": [5.0, 12.0, 3.0, 15.0, 8.0]}
    exec(f"result = {res.code}", locs)
    # Elements <= 5 are 5 and 3. count = 2.
    assert locs["result"] == 2.0

    # 4. COUNTIFS
    res = translate_formula("=COUNTIFS(A1:A5; \">5\"; B1:B5; \"<10\")")
    assert res.ok
    locs = {**base_locs, "data": [[5.0, 12.0, 3.0, 15.0, 8.0], [1.0, 2.0, 3.0, 4.0, 5.0]]}
    exec(f"result = {res.code}", locs)
    # A1:A5 > 5 are 12, 15, 8.
    # Corresponding B1:B5 are 2, 4, 5. All are < 10. Count = 3.
    assert locs["result"] == 3.0

    # 5. AVERAGEIF
    res = translate_formula("=AVERAGEIF(A1:A5; \">5\"; B1:B5)")
    assert res.ok
    locs = {**base_locs, "data": [[5.0, 12.0, 3.0, 15.0, 8.0], [1.0, 2.0, 3.0, 4.0, 5.0]]}
    exec(f"result = {res.code}", locs)
    # A1:A5 > 5 are 12, 15, 8.
    # Corresponding B1:B5 are 2, 4, 5. Mean = (2+4+5)/3 = 3.6666...
    assert abs(locs["result"] - 11.0 / 3.0) < 1e-9

    # 6. AVERAGEIFS
    res = translate_formula("=AVERAGEIFS(B1:B5; A1:A5; \">5\")")
    assert res.ok
    locs = {**base_locs, "data": [[1.0, 2.0, 3.0, 4.0, 5.0], [5.0, 12.0, 3.0, 15.0, 8.0]]}
    exec(f"result = {res.code}", locs)
    assert abs(locs["result"] - 11.0 / 3.0) < 1e-9

    # 7. XLOOKUP
    res = translate_formula("=XLOOKUP(\"apple\"; A1:A3; B1:B3; \"Not Found\")")
    assert res.ok
    locs = {**base_locs, "data": [["pear", "apple", "banana"], [10.0, 20.0, 30.0]]}
    exec(f"result = {res.code}", locs)
    assert locs["result"] == 20.0

    res = translate_formula("=XLOOKUP(\"orange\"; A1:A3; B1:B3; \"Not Found\")")
    assert res.ok
    locs = {**base_locs, "data": [["pear", "apple", "banana"], [10.0, 20.0, 30.0]]}
    exec(f"result = {res.code}", locs)
    assert locs["result"] == "Not Found"

    # 8. TEXTJOIN
    res = translate_formula("=TEXTJOIN(\", \"; TRUE; A1:A3)")
    assert res.ok
    locs = {**base_locs, "data": ["apple", "", "banana"]}
    exec(f"result = {res.code}", locs)
    assert locs["result"] == "apple, banana"

    # 9. REGEX
    res = translate_formula("=REGEX(\"123-456\"; \"[0-9]+\"; \"XXX\"; \"g\")")
    assert res.ok
    locs = dict(base_locs)
    exec(f"result = {res.code}", locs)
    assert locs["result"] == "XXX-XXX"

    # 10. EOMONTH
    # 2026-06-09 is 46182 days from 1899-12-30 (since 2026-06-09 is ordinal 739776. 739776 - 693594 = 46182)
    # EOMONTH(46182; 1) -> End of July 2026 -> 2026-07-31 -> ordinal 739828 -> 739828 - 693594 = 46234
    res = translate_formula("=EOMONTH(46182; 1)")
    assert res.ok
    locs = dict(base_locs)
    exec(f"result = {res.code}", locs)
    assert locs["result"] == 46234.0

    # 11. NETWORKDAYS
    # 2026-06-08 (Mon) to 2026-06-12 (Fri) should be 5 network days.
    # 2026-06-08 is ordinal 739775 -> 46181
    # 2026-06-12 is ordinal 739779 -> 46185
    res = translate_formula("=NETWORKDAYS(46181; 46185)")
    assert res.ok
    locs = dict(base_locs)
    exec(f"result = {res.code}", locs)
    assert locs["result"] == 5.0


def test_translate_tier_abc_functions():
    import numpy as np

    # Tier A
    res = translate_formula("=SUBTOTAL(9; A1:A5)")
    assert res.ok
    assert "xl.subtotal(" in res.code
    assert exec_result(res, [1.0, 2.0, 3.0, 4.0, 5.0]) == 15.0

    res = translate_formula("=ISBLANK(A1)")
    assert res.ok
    assert exec_result(res, "") is True
    assert exec_result(res, 5.0) is False

    res = translate_formula("=ISNUMBER(A1)")
    assert res.ok
    assert exec_result(res, 5.0) is True
    assert exec_result(res, "x") is False

    res = translate_formula("=IFS(A1>10; \"big\"; A1>5; \"mid\"; TRUE; \"small\")")
    assert res.ok
    assert exec_result(res, 12.0) == "big"
    assert exec_result(res, 7.0) == "mid"
    assert exec_result(res, 1.0) == "small"

    res = translate_formula("=MEDIAN(A1:A5)")
    assert res.ok
    assert exec_result(res, [1.0, 2.0, 3.0, 4.0, 100.0]) == 3.0

    res = translate_formula("=COUNTBLANK(A1:A4)")
    assert res.ok
    assert exec_result(res, [1.0, "", None, 4.0]) == 2.0

    res = translate_formula("=ROUNDUP(A1; 1)")
    assert res.ok
    assert exec_result(res, 1.23) == 1.3

    res = translate_formula("=LOG(A1; 2)")
    assert res.ok
    assert abs(exec_result(res, 8.0) - 3.0) < 1e-9

    res = translate_formula("=QUOTIENT(A1; B1)")
    assert res.ok
    assert exec_result(res, [10.0, 3.0]) == 3.0

    res = translate_formula("=SUMPRODUCT(A1:A3; B1:B3)")
    assert res.ok
    assert exec_result(res, [[1.0, 2.0, 3.0], [4.0, 5.0, 6.0]]) == 32.0

    res = translate_formula("=DATEDIF(46181; 46185; \"D\")")
    assert res.ok
    assert exec_result(res, []) == 4.0

    # Tier B
    res = translate_formula("=ISTEXT(A1)")
    assert res.ok
    assert exec_result(res, "hello") is True

    res = translate_formula("=LARGE(A1:A5; 2)")
    assert res.ok
    assert exec_result(res, [1.0, 5.0, 3.0, 4.0, 2.0]) == 4.0

    res = translate_formula("=SMALL(A1:A5; 2)")
    assert res.ok
    assert exec_result(res, [1.0, 5.0, 3.0, 4.0, 2.0]) == 2.0

    res = translate_formula("=AVERAGEA(A1:A3)")
    assert res.ok
    assert exec_result(res, [10.0, "", 20.0]) == 10.0

    res = translate_formula("=EVEN(A1)")
    assert res.ok
    assert exec_result(res, 3.0) == 4.0

    res = translate_formula("=XMATCH(\"b\"; A1:A3)")
    assert res.ok
    assert exec_result(res, [["a", "b", "c"]]) == 2.0

    res = translate_formula('=TEXT(B5; "MMMM")')
    assert res.ok
    assert "xl.fmt" in res.code
    assert "xl.text(" not in res.code

    res = translate_formula("=ROUNDUP(7692.30769230769; 0)")
    assert res.ok
    assert "int(" not in res.code

    # Tier C
    res = translate_formula("=FILTER(A1:A5; B1:B5)")
    assert res.ok
    assert exec_result(res, [[1.0, 2.0, 3.0, 4.0, 5.0], [True, False, True, False, True]]) == [1.0, 3.0, 5.0]

    res = translate_formula("=SORT(A1:A3; 1; -1)")
    assert res.ok
    assert exec_result(res, [3.0, 1.0, 2.0]) == [3.0, 2.0, 1.0]

    res = translate_formula("=UNIQUE(A1:A5)")
    assert res.ok
    assert exec_result(res, [1.0, 2.0, 1.0, 3.0, 2.0]) == [1.0, 2.0, 3.0]


def exec_result(res, data):
    import math

    import numpy as np

    locs = {"data": data, "np": np, "xl": xl, "math": math, "dt": dt}
    code = f"result = {res.code}"
    exec(code, locs)
    return locs["result"]


def test_translate_tier_d_functions():
    # Financial
    res = translate_formula("=PMT(0.05/12; 60; 10000)")
    assert res.ok
    assert "xl.pmt" in res.code
    # PMT(0.05/12, 60, 10000) approx -188.71
    assert abs(exec_result(res, []) - (-188.712336)) < 1e-2

    res = translate_formula("=FV(0.05/12; 60; -200; -10000)")
    assert res.ok
    assert "xl.fv" in res.code
    # FV approx 26434.80
    assert abs(exec_result(res, []) - 26434.80) < 1.0

    res = translate_formula("=PV(0.05/12; 60; -200; 26434.80)")
    assert res.ok
    assert "xl.pv" in res.code
    assert abs(exec_result(res, []) - (-10000.0)) < 1.0

    # Math
    res = translate_formula("=MROUND(1.23; 0.5)")
    assert res.ok
    assert exec_result(res, []) == 1.0

    res = translate_formula("=SUMSQ(A1; B1)")
    assert res.ok
    assert exec_result(res, [3.0, 4.0]) == 25.0

    # Information
    res = translate_formula("=ISEVEN(4)")
    assert res.ok
    assert exec_result(res, []) is True

    res = translate_formula("=ISODD(4)")
    assert res.ok
    assert exec_result(res, []) is False

    # Date/Time
    res = translate_formula("=DAYS(46185; 46181)")  # 2026-06-12 - 2026-06-08
    assert res.ok
    assert exec_result(res, []) == 4.0

    res = translate_formula("=TIME(12; 0; 0)")
    assert res.ok
    assert exec_result(res, []) == 0.5

    res = translate_formula("=TRIMMEAN(A1:A5; 0.2)")
    assert res.ok
    assert exec_result(res, [1.0, 2.0, 3.0, 4.0, 5.0]) == 3.0

    res = translate_formula("=FORECAST(6; A1:A5; B1:B5)")
    assert res.ok
    # y = [1,2,3,4,5], x = [1,2,3,4,5] -> y = x. for x=6, y=6.
    assert exec_result(res, [[1.0, 2.0, 3.0, 4.0, 5.0], [1.0, 2.0, 3.0, 4.0, 5.0]]) == 6.0


def test_translate_new_15_functions():
    # 1-6. Hyperbolic
    res = translate_formula("=ACOSH(2)")
    assert res.ok
    assert abs(exec_result(res, []) - 1.3169578969) < 1e-9

    res = translate_formula("=ASINH(1)")
    assert res.ok
    assert abs(exec_result(res, []) - 0.881373587) < 1e-9

    res = translate_formula("=ATANH(0.5)")
    assert res.ok
    assert abs(exec_result(res, []) - 0.5493061443) < 1e-9

    res = translate_formula("=COSH(1)")
    assert res.ok
    assert abs(exec_result(res, []) - 1.5430806348) < 1e-9

    res = translate_formula("=SINH(1)")
    assert res.ok
    assert abs(exec_result(res, []) - 1.1752011936) < 1e-9

    res = translate_formula("=TANH(1)")
    assert res.ok
    assert abs(exec_result(res, []) - 0.76159415595) < 1e-9

    # 7. FACT
    res = translate_formula("=FACT(5)")
    assert res.ok
    assert exec_result(res, []) == 120.0

    # 8. COMBIN
    res = translate_formula("=COMBIN(5; 2)")
    assert res.ok
    assert exec_result(res, []) == 10.0

    # 9. REPT
    res = translate_formula("=REPT(\"abc\"; 3)")
    assert res.ok
    assert exec_result(res, []) == "abcabcabc"

    # 10. EXACT
    res = translate_formula("=EXACT(\"abc\"; \"ABC\")")
    assert res.ok
    assert exec_result(res, []) is False
    res = translate_formula("=EXACT(\"abc\"; \"abc\")")
    assert exec_result(res, []) is True

    # 11. ARABIC
    res = translate_formula("=ARABIC(\"MCMLXXXIV\")")
    assert res.ok
    assert exec_result(res, []) == 1984.0

    # 12. DATEVALUE
    res = translate_formula("=DATEVALUE(\"2023-10-05\")")
    assert res.ok
    assert exec_result(res, []) == float(dt.date(2023, 10, 5).toordinal() - 693594)

    # 13. TIMEVALUE
    res = translate_formula("=TIMEVALUE(\"12:00:00\")")
    assert res.ok
    assert exec_result(res, []) == 0.5

    # 14. N
    res = translate_formula("=N(TRUE)")
    assert res.ok
    assert exec_result(res, []) == 1.0
    res = translate_formula("=N(\"abc\")")
    assert exec_result(res, []) == 0.0

    # 15. TYPE
    res = translate_formula("=TYPE(123)")
    assert res.ok
    assert exec_result(res, []) == 1.0
    res = translate_formula("=TYPE(\"abc\")")
    assert exec_result(res, []) == 2.0
    res = translate_formula("=TYPE(TRUE)")
    assert exec_result(res, []) == 4.0


def test_translate_15_more_functions():
    # 1. CHOOSE
    res = translate_formula("=CHOOSE(2; \"a\"; \"b\"; \"c\")")
    assert res.ok
    assert exec_result(res, []) == "b"

    # 2. ADDRESS
    res = translate_formula("=ADDRESS(1; 1)")
    assert res.ok
    assert exec_result(res, []) == "$A$1"

    # 3. AREAS
    res = translate_formula("=AREAS(A1:B10)")
    assert res.ok
    assert exec_result(res, []) == 1.0

    # 4. YEARFRAC
    res = translate_formula("=YEARFRAC(45000; 45365)")  # approx 1 year
    assert res.ok
    assert abs(exec_result(res, []) - 1.0) < 0.1

    # 5. DAYS360
    res = translate_formula("=DAYS360(44927; 45292)")  # 2023-01-01 to 2024-01-01
    assert res.ok
    assert exec_result(res, []) == 360.0

    # 6. NETWORKDAYS.INTL
    res = translate_formula("=NETWORKDAYS.INTL(46181; 46185; 1)")  # Mon-Fri
    assert res.ok
    assert exec_result(res, []) == 5.0

    # 7. WORKDAY.INTL
    res = translate_formula("=WORKDAY.INTL(46181; 4; 1)")  # Mon + 4 days -> Fri
    assert res.ok
    assert exec_result(res, []) == 46185.0

    # 8. XOR
    res = translate_formula("=XOR(TRUE; FALSE; TRUE)")
    assert res.ok
    assert exec_result(res, []) is False
    res = translate_formula("=XOR(TRUE; FALSE; FALSE)")
    assert exec_result(res, []) is True

    # 9. CHAR
    res = translate_formula("=CHAR(65)")
    assert res.ok
    assert exec_result(res, []) == "A"

    # 10. CODE
    res = translate_formula("=CODE(\"A\")")
    assert res.ok
    assert exec_result(res, []) == 65.0

    # 11-15. Database Functions
    db = [
        ["Tree", "Height", "Age", "Yield", "Profit"],
        ["Apple", 18.0, 20.0, 14.0, 105.0],
        ["Pear", 12.0, 12.0, 10.0, 96.0],
        ["Cherry", 13.0, 7.0, 8.0, 105.0],
        ["Apple", 14.0, 15.0, 10.0, 75.0],
        ["Pear", 9.0, 8.0, 8.0, 77.0],
        ["Apple", 8.0, 9.0, 6.0, 45.0],
    ]
    crit = [["Tree", "Height"], ["Apple", ">10"]]
    # DCOUNT
    res = translate_formula("=DCOUNT(A1:E7; \"Yield\"; G1:H2)")
    assert res.ok
    assert exec_result(res, [db, crit]) == 2.0

    # DSUM
    res = translate_formula("=DSUM(A1:E7; \"Profit\"; G1:H2)")
    assert res.ok
    assert exec_result(res, [db, crit]) == 180.0

    # DAVERAGE
    res = translate_formula("=DAVERAGE(A1:E7; \"Yield\"; G1:H2)")
    assert res.ok
    assert exec_result(res, [db, crit]) == 12.0

    # DMAX
    res = translate_formula("=DMAX(A1:E7; \"Height\"; G1:H2)")
    assert res.ok
    assert exec_result(res, [db, crit]) == 18.0

    # DMIN
    res = translate_formula("=DMIN(A1:E7; \"Height\"; G1:H2)")
    assert res.ok
    assert exec_result(res, [db, crit]) == 14.0


def test_translate_extra_15_functions():
    # 1. DCOUNTA
    db = [
        ["Tree", "Height", "Age", "Yield", "Profit"],
        ["Apple", 18.0, 20.0, 14.0, 105.0],
        ["Pear", 12.0, 12.0, 10.0, 96.0],
        ["Cherry", 13.0, 7.0, 8.0, 105.0],
        ["Apple", 14.0, 15.0, 10.0, 75.0],
        ["Pear", 9.0, 8.0, 8.0, 77.0],
        ["Apple", 8.0, 9.0, 6.0, 45.0],
    ]
    crit = [["Tree", "Height"], ["Apple", ">10"]]

    res = translate_formula("=DCOUNTA(A1:E7; \"Tree\"; G1:H2)")
    assert res.ok
    assert exec_result(res, [db, crit]) == 2.0

    # 2. DGET
    res = translate_formula("=DGET(A1:E7; \"Yield\"; G1:H2)")
    assert res.ok
    assert exec_result(res, [db, crit]) == "#NUM!"
    crit_single = [["Tree", "Height"], ["Apple", ">15"]]
    assert exec_result(res, [db, crit_single]) == 14.0

    # 3. DPRODUCT
    res = translate_formula("=DPRODUCT(A1:E7; \"Yield\"; G1:H2)")
    assert res.ok
    assert exec_result(res, [db, crit]) == 140.0

    # 4. DSTDEV
    res = translate_formula("=DSTDEV(A1:E7; \"Yield\"; G1:H2)")
    assert res.ok
    assert abs(exec_result(res, [db, crit]) - np.std([14.0, 10.0], ddof=1)) < 1e-9

    # 5. DSTDEVP
    res = translate_formula("=DSTDEVP(A1:E7; \"Yield\"; G1:H2)")
    assert res.ok
    assert abs(exec_result(res, [db, crit]) - np.std([14.0, 10.0], ddof=0)) < 1e-9

    # 6. DVAR
    res = translate_formula("=DVAR(A1:E7; \"Yield\"; G1:H2)")
    assert res.ok
    assert abs(exec_result(res, [db, crit]) - np.var([14.0, 10.0], ddof=1)) < 1e-9

    # 7. DVARP
    res = translate_formula("=DVARP(A1:E7; \"Yield\"; G1:H2)")
    assert res.ok
    assert abs(exec_result(res, [db, crit]) - np.var([14.0, 10.0], ddof=0)) < 1e-9

    # 8. ISOWEEKNUM
    res = translate_formula("=ISOWEEKNUM(45292)")
    assert res.ok
    assert exec_result(res, []) == 1.0

    # 9. FACTDOUBLE
    res = translate_formula("=FACTDOUBLE(6)")
    assert res.ok
    assert exec_result(res, []) == 48.0

    # 10. COMBINA
    res = translate_formula("=COMBINA(4; 3)")
    assert res.ok
    assert exec_result(res, []) == 20.0

    # 11. AVEDEV
    res = translate_formula("=AVEDEV(A1:A3)")
    assert res.ok
    assert abs(exec_result(res, [2, 4, 9]) - 8.0/3.0) < 1e-9

    # 12. GEOMEAN
    res = translate_formula("=GEOMEAN(A1:A3)")
    assert res.ok
    assert abs(exec_result(res, [2, 8, 4]) - 4.0) < 1e-9

    # 13. HARMEAN
    res = translate_formula("=HARMEAN(A1:A3)")
    assert res.ok
    assert abs(exec_result(res, [2, 4, 1]) - (3 / (1/2.0 + 1/4.0 + 1/1.0))) < 1e-9

    # 14. NPV
    res = translate_formula("=NPV(0.1; A1:A3)")
    assert res.ok
    expected = 100/1.1 + 200/1.21 + 300/1.331
    assert abs(exec_result(res, [100, 200, 300]) - expected) < 1e-9

    # 15. IRR
    res = translate_formula("=IRR(A1:A3)")
    assert res.ok
    assert abs(exec_result(res, [-100, 110, 0]) - 0.1) < 1e-7


def test_translate_15_more_more_functions():
    # 1. DEVSQ
    res = translate_formula("=DEVSQ(A1:A3)")
    assert res.ok
    assert exec_result(res, [1.0, 3.0, 5.0]) == 8.0  # mean=3, dev=[-2,0,2], sq=[4,0,4], sum=8

    # 2. KURT
    res = translate_formula("=KURT(A1:A4)")
    assert res.ok
    # Excel/Calc kurtosis of [1,2,3,4] is -1.2
    assert abs(exec_result(res, [1.0, 2.0, 3.0, 4.0]) - (-1.2)) < 1e-9

    # 3. SKEW
    res = translate_formula("=SKEW(A1:A3)")
    assert res.ok
    # SKEW of symmetric [1,2,3] is 0
    assert abs(exec_result(res, [1.0, 2.0, 3.0]) - 0.0) < 1e-9

    # 4. SLOPE
    res = translate_formula("=SLOPE(A1:A3; B1:B3)")
    assert res.ok
    # y = [2,4,6], x = [1,2,3] -> slope = 2
    assert exec_result(res, [[2.0, 4.0, 6.0], [1.0, 2.0, 3.0]]) == 2.0

    # 5. INTERCEPT
    res = translate_formula("=INTERCEPT(A1:A3; B1:B3)")
    assert res.ok
    # y = [3,5,7], x = [1,2,3] -> y = 2x + 1 -> intercept = 1
    assert exec_result(res, [[3.0, 5.0, 7.0], [1.0, 2.0, 3.0]]) == 1.0

    # 6. RSQ
    res = translate_formula("=RSQ(A1:A3; B1:B3)")
    assert res.ok
    assert exec_result(res, [[2.0, 4.0, 6.0], [1.0, 2.0, 3.0]]) == 1.0

    # 7. STEYX
    res = translate_formula("=STEYX(A1:A3; B1:B3)")
    assert res.ok
    # Perfectly linear -> 0
    assert exec_result(res, [[2.0, 4.0, 6.0], [1.0, 2.0, 3.0]]) == 0.0

    # 8. ACOT
    res = translate_formula("=ACOT(1)")
    assert res.ok
    assert abs(exec_result(res, []) - math.pi / 4) < 1e-9

    # 9. ACOTH
    res = translate_formula("=ACOTH(2)")
    assert res.ok
    assert abs(exec_result(res, []) - 0.5 * math.log(3.0)) < 1e-9

    # 10. COT
    res = translate_formula("=COT(PI()/4)")
    assert res.ok
    assert abs(exec_result(res, []) - 1.0) < 1e-9

    # 11. COTH
    res = translate_formula("=COTH(1)")
    assert res.ok
    assert abs(exec_result(res, []) - 1.0 / math.tanh(1.0)) < 1e-9

    # 12. CSC
    res = translate_formula("=CSC(PI()/2)")
    assert res.ok
    assert abs(exec_result(res, []) - 1.0) < 1e-9

    # 13. CSCH
    res = translate_formula("=CSCH(1)")
    assert res.ok
    assert abs(exec_result(res, []) - 1.0 / math.sinh(1.0)) < 1e-9

    # 14. SEC
    res = translate_formula("=SEC(0)")
    assert res.ok
    assert abs(exec_result(res, []) - 1.0) < 1e-9

    # 15. SECH
    res = translate_formula("=SECH(0)")
    assert res.ok
    assert abs(exec_result(res, []) - 1.0) < 1e-9


def test_translate_yet_more_functions():
    # 1-4. Statistical A
    res = translate_formula("=STDEVA(A1:A3)")
    assert res.ok
    # [1, 2, "x"] -> [1, 2, 0] -> std ddof=1 approx 1.0
    assert abs(exec_result(res, [1.0, 2.0, "x"]) - 1.0) < 1e-9

    res = translate_formula("=STDEVPA(A1:A3)")
    assert res.ok
    # [1, 2, 0] -> std ddof=0 approx 0.81649658
    assert abs(exec_result(res, [1.0, 2.0, "x"]) - 0.81649658) < 1e-7

    res = translate_formula("=VARA(A1:A3)")
    assert res.ok
    assert abs(exec_result(res, [1.0, 2.0, "x"]) - 1.0) < 1e-9

    res = translate_formula("=VARPA(A1:A3)")
    assert res.ok
    assert abs(exec_result(res, [1.0, 2.0, "x"]) - 2/3.0) < 1e-9

    # 5-6. MAXA/MINA
    res = translate_formula("=MAXA(A1:A3)")
    assert res.ok
    assert exec_result(res, [-1.0, -2.0, "x"]) == 0.0  # "x" becomes 0.0

    res = translate_formula("=MINA(A1:A3)")
    assert res.ok
    assert exec_result(res, [1.0, 2.0, True]) == 1.0  # TRUE becomes 1.0
    assert exec_result(res, [1.0, 2.0, False]) == 0.0 # FALSE becomes 0.0

    # 7-8. Error Functions
    res = translate_formula("=ERF(0.5)")
    assert res.ok
    assert abs(exec_result(res, []) - 0.520499877) < 1e-7

    res = translate_formula("=ERFC(0.5)")
    assert res.ok
    assert abs(exec_result(res, []) - 0.479500123) < 1e-7

    # 9-10. Engineering
    res = translate_formula("=DELTA(5; 5)")
    assert res.ok
    assert exec_result(res, []) == 1.0

    res = translate_formula("=GESTEP(10; 5)")
    assert res.ok
    assert exec_result(res, []) == 1.0

    # 11. SQRTPI
    res = translate_formula("=SQRTPI(1)")
    assert res.ok
    assert abs(exec_result(res, []) - math.sqrt(math.pi)) < 1e-9

    # 12-16. Bitwise
    res = translate_formula("=BITAND(6; 3)") # 110 & 011 = 010 (2)
    assert res.ok
    assert exec_result(res, []) == 2.0

    res = translate_formula("=BITOR(6; 3)") # 110 | 011 = 111 (7)
    assert res.ok
    assert exec_result(res, []) == 7.0

    res = translate_formula("=BITXOR(6; 3)") # 110 ^ 011 = 101 (5)
    assert res.ok
    assert exec_result(res, []) == 5.0

    res = translate_formula("=BITLSHIFT(1; 3)") # 1 << 3 = 8
    assert res.ok
    assert exec_result(res, []) == 8.0

    res = translate_formula("=BITRSHIFT(8; 3)") # 8 >> 3 = 1
    assert res.ok
    assert exec_result(res, []) == 1.0


def test_translate_group_d_functions():
    def exec_result(res, *data_args):
        ns: dict = {"np": np, "math": math, "dt": dt, "xl": xl}
        code = str(res.code)
        # replace data[i] with data_args[i]
        for i in range(len(data_args)):
            code = code.replace(f"data[{i}]", f"data_args[{i}]")
        ns["data_args"] = data_args
        ns["data"] = data_args
        exec(f"result = {code}", ns)
        return ns["result"]

    res = translate_formula("=XIRR(A1:A3, B1:B3)")
    assert res.ok
    assert "xl.xirr" in res.code
    assert abs(exec_result(res, [-10000, 2750, 4250], [733042, 733102, 733345])) > 0

    res = translate_formula("=XNPV(0.1, A1:A3, B1:B3)")
    assert res.ok
    assert "xl.xnpv" in res.code
    assert abs(exec_result(res, [-10000, 2750, 4250], [733042, 733102, 733345])) > 0

    res = translate_formula("=YIELD(A1, B1, C1, D1, E1, F1)")
    assert res.ok
    assert "xl.yield_calc" in res.code

    res = translate_formula("=ISFORMULA(A1)")
    assert res.ok
    assert "xl.isformula" in res.code
    assert exec_result(res, 42) is False

    res = translate_formula("=ISREF(A1)")
    assert res.ok
    assert "xl.isref" in res.code
    assert exec_result(res, 42) is False

    res = translate_formula("=NA()")
    assert res.ok
    assert "xl.na" in res.code
    assert math.isnan(exec_result(res))

    res = translate_formula("=AGGREGATE(9, 4, A1:A3)")
    assert res.ok
    assert "xl.aggregate" in res.code
    assert exec_result(res, [1, 2, 3]) == 6

    res = translate_formula("=BASE(10, 2)")
    assert res.ok
    assert "xl.base" in res.code
    assert exec_result(res) == "1010"

    res = translate_formula("=DECIMAL(\"1010\", 2)")
    assert res.ok
    assert "xl.decimal" in res.code
    assert exec_result(res) == 10

    res = translate_formula("=MULTINOMIAL(2, 3)")
    assert res.ok
    assert "xl.multinomial" in res.code
    assert exec_result(res) == 10

    res = translate_formula("=SERIESSUM(1, 0, 1, A1:A3)")
    assert res.ok
    assert "xl.seriessum" in res.code
    assert exec_result(res, [1, 2, 3]) == 6

    res = translate_formula("=FREQUENCY(A1:A5, B1:B2)")
    assert res.ok
    assert "xl.frequency" in res.code
    assert exec_result(res, [1, 2, 3, 4, 5], [2, 4]) == [2, 2, 1]

    res = translate_formula("=GROWTH(A1:A3, B1:B3, C1:C2)")
    assert res.ok
    assert "xl.growth" in res.code
    assert len(exec_result(res, [10, 20, 40], [1, 2, 3], [4, 5])) == 2


def test_translate_complex_functions():
    # 1. COMPLEX
    res = translate_formula('=COMPLEX(3; 4; "j")')
    assert res.ok
    assert exec_result(res, []) == "3.0+4.0j"

    # 2-3. IMABS / IMAGINARY
    res = translate_formula('=IMABS("3+4i")')
    assert res.ok
    assert exec_result(res, []) == 5.0

    res = translate_formula('=IMAGINARY("3+4i")')
    assert res.ok
    assert exec_result(res, []) == 4.0

    # 4-5. IMARGUMENT / IMCONJUGATE
    res = translate_formula('=IMARGUMENT("0+1i")')
    assert res.ok
    assert abs(exec_result(res, []) - math.pi/2) < 1e-9

    res = translate_formula('=IMCONJUGATE("3+4i")')
    assert res.ok
    assert exec_result(res, []) == "3.0-4.0i"

    # 6-7. IMCOS / IMDIV
    res = translate_formula('=IMCOS("1+1i")')
    assert res.ok
    # cos(1+i) is approx 0.83373 - 0.98889i
    val = exec_result(res, [])
    assert "0.83373" in val

    res = translate_formula('=IMDIV("10+10i"; "2")')
    assert res.ok
    assert exec_result(res, []) == "5.0+5.0i"

    # 8-10. IMEXP / IMLN / IMLOG10
    res = translate_formula('=IMEXP("1i")')
    assert res.ok
    # exp(i) = cos(1) + i sin(1) approx 0.5403 + 0.84147i
    assert "0.5403" in exec_result(res, [])

    res = translate_formula('=IMLN("e")') # Note: "e" string depends on cmath/math
    # Better: IMLN("2.718281828459045")
    res = translate_formula('=IMLN("2.718281828459045")')
    assert res.ok
    assert abs(float(exec_result(res, [])) - 1.0) < 1e-9

    res = translate_formula('=IMLOG10("100")')
    assert res.ok
    assert exec_result(res, []) == "2.0"

    # 11-12. IMLOG2 / IMPOWER
    res = translate_formula('=IMLOG2("8")')
    assert res.ok
    assert exec_result(res, []) == "3.0"

    res = translate_formula('=IMPOWER("2"; 3)')
    assert res.ok
    assert exec_result(res, []) == "8.0"

    # 13-15. IMPRODUCT / IMREAL / IMSIN
    res = translate_formula('=IMPRODUCT("2+2i"; "2-2i")') # (2+2i)(2-2i) = 4 - 4i^2 = 8
    assert res.ok
    assert exec_result(res, []) == "8.0"

    res = translate_formula('=IMREAL("3+4i")')
    assert res.ok
    assert exec_result(res, []) == 3.0

    res = translate_formula('=IMSIN("1i")')
    assert res.ok
    # sin(i) = i sinh(1) approx 1.1752i
    assert "1.1752" in exec_result(res, [])

def test_translate_financial_group_c():
    """Verify Group C translation logic (Financial functions)."""

    # 1. ODDLYIELD
    res = translate_formula('=ODDLYIELD("2008-04-20"; "2008-06-15"; "2008-03-12"; 0.05; 99.875; 100; 2; 0)')
    assert res.ok
    assert "xl.oddlyield" in res.code

    # 2. PDURATION
    res = translate_formula('=PDURATION(0.025; 10000; 12000)')
    assert res.ok
    assert "xl.pduration" in res.code

    # 3. PPMT
    res = translate_formula('=PPMT(0.01; 1; 24; 2000)')
    assert res.ok
    assert "xl.ppmt" in res.code

    # 4. PRICE
    res = translate_formula('=PRICE("2008-02-15"; "2017-11-15"; 0.0575; 0.065; 100; 2; 0)')
    assert res.ok
    assert "xl.price" in res.code

    # 5. PRICEDISC
    res = translate_formula('=PRICEDISC("2008-02-15"; "2008-03-01"; 0.0525; 100; 2)')
    assert res.ok
    assert "xl.pricedisc" in res.code

    # 6. PRICEMAT
    res = translate_formula('=PRICEMAT("2008-02-15"; "2008-04-13"; "2007-11-11"; 0.061; 0.061; 2)')
    assert res.ok
    assert "xl.pricemat" in res.code

    # 7. RATE
    res = translate_formula('=RATE(48; -200; 8000)')
    assert res.ok
    assert "xl.rate" in res.code

    # 8. RECEIVED
    res = translate_formula('=RECEIVED("2008-02-15"; "2008-05-15"; 1000000; 0.0575; 2)')
    assert res.ok
    assert "xl.received" in res.code

    # 9. RRI
    res = translate_formula('=RRI(48; 10000; 12000)')
    assert res.ok
    assert "xl.rri" in res.code

    # 10. SLN
    res = translate_formula('=SLN(30000; 7500; 10)')
    assert res.ok
    assert "xl.sln" in res.code

    # 11. SYD
    res = translate_formula('=SYD(30000; 7500; 10; 1)')
    assert res.ok
    assert "xl.syd" in res.code

    # 12. TBILLEQ
    res = translate_formula('=TBILLEQ("2008-03-31"; "2008-06-01"; 0.0914)')
    assert res.ok
    assert "xl.tbilleq" in res.code

    # 13. TBILLPRICE
    res = translate_formula('=TBILLPRICE("2008-03-31"; "2008-06-01"; 0.09)')
    assert res.ok
    assert "xl.tbillprice" in res.code

    # 14. TBILLYIELD
    res = translate_formula('=TBILLYIELD("2008-03-31"; "2008-06-01"; 98.45)')
    assert res.ok
    assert "xl.tbillyield" in res.code

    # 15. VDB
    res = translate_formula('=VDB(2400; 300; 10; 0; 0.875; 1.5)')
    assert res.ok
    assert "xl.vdb" in res.code


def test_translate_financial_group_a():
    res = translate_formula("=ACCRINT(A1, B1, C1, 0.05, 100, 2)")
    assert res.ok
    assert "xl.accrint(data[0], data[1], data[2], 0.05, 100, 2)" in res.code

    res = translate_formula("=DB(10000, 1000, 5, 1)")
    assert res.ok
    assert "xl.db(10000, 1000, 5, 1)" in res.code
def test_translate_group_e_functions():
    # 1. MDETERM
    res = translate_formula("=MDETERM(A1:B2)")
    assert res.ok
    assert abs(exec_result(res, [[[1, 2], [3, 4]]]) - (-2.0)) < 1e-9

    # 2. MINVERSE
    res = translate_formula("=MINVERSE(A1:B2)")
    assert res.ok
    inv = exec_result(res, [[[1, 2], [3, 4]]])
    assert abs(inv[0][0] - (-2.0)) < 1e-9

    # 3. MMULT
    res = translate_formula("=MMULT(A1:B2; C1:D2)")
    assert res.ok
    mm = exec_result(res, [[[1, 2], [3, 4]], [[2, 0], [1, 2]]])
    assert mm[0][0] == 4.0

    # 4. MTRANS
    res = translate_formula("=MTRANS(A1:B2)")
    assert res.ok
    t = exec_result(res, [[[1, 2], [3, 4]]])
    assert t[0][1] == 3.0

    # 5. MUNIT
    res = translate_formula("=MUNIT(2)")
    assert res.ok
    m = exec_result(res, [])
    assert m[0][0] == 1.0
    assert m[0][1] == 0.0

    # 6. BETADIST
    res = translate_formula("=BETADIST(0.5; 2; 3)")
    assert res.ok
    # SciPy needed for execution, but we can verify AST emit
    assert "xl.betadist(0.5, 2, 3)" in res.code or "xl.betadist(float(0.5), float(2), float(3))" in res.code

    # 7. BINOMDIST
    res = translate_formula("=BINOMDIST(2; 10; 0.5; 0)")
    assert res.ok

    # 8. CONFIDENCE
    res = translate_formula("=CONFIDENCE(0.05; 2.5; 50)")
    assert res.ok

    # 9. LINEST
    res = translate_formula("=LINEST(A1:A3)")
    assert res.ok

    # 10. LOGEST
    res = translate_formula("=LOGEST(A1:A3)")
    assert res.ok

    # 11. TREND
    res = translate_formula("=TREND(A1:A3)")
    assert res.ok

    # 12. BETAINV
    res = translate_formula("=BETAINV(0.5; 2; 3)")
    assert res.ok
    assert "xl.betainv(0.5, 2, 3)" in res.code or "xl.betainv(float(0.5), float(2), float(3))" in res.code

    # 13. CHIDIST
    res = translate_formula("=CHIDIST(3; 2)")
    assert res.ok

    # 14. CHIINV
    res = translate_formula("=CHIINV(0.5; 2)")
    assert res.ok

    # 15. CRITBINOM
    res = translate_formula("=CRITBINOM(10; 0.5; 0.5)")
    assert res.ok

def test_translate_group_f_functions():
    # 1. EXPONDIST
    res = translate_formula("=EXPONDIST(1.5; 2; 1)")
    assert res.ok
    assert abs(exec_result(res, []) - 0.9502129316) < 1e-6

    res = translate_formula("=EXPONDIST(1.5; 2; 0)")
    assert res.ok
    assert abs(exec_result(res, []) - 0.0995741367) < 1e-6

    # 2. FDIST
    res = translate_formula("=FDIST(1.5; 2; 3)")
    assert res.ok
    assert abs(exec_result(res, []) - 0.3535533906) < 1e-6

    # 3. FINV
    res = translate_formula("=FINV(0.3535533906; 2; 3)")
    assert res.ok
    assert abs(exec_result(res, []) - 1.5) < 1e-6

    # 4. FISHER
    res = translate_formula("=FISHER(0.5)")
    assert res.ok
    assert abs(exec_result(res, []) - 0.5493061443) < 1e-6

    # 5. FISHERINV
    res = translate_formula("=FISHERINV(0.5493061443)")
    assert res.ok
    assert abs(exec_result(res, []) - 0.5) < 1e-6

    # 6. GAMMA
    res = translate_formula("=GAMMA(1.5)")
    assert res.ok
    assert abs(exec_result(res, []) - 0.8862269255) < 1e-6

    # 7. GAMMADIST
    res = translate_formula("=GAMMADIST(1.5; 2; 3; 1)")
    assert res.ok
    assert abs(exec_result(res, []) - 0.0902040104) < 1e-6

    # 8. GAMMAINV
    res = translate_formula("=GAMMAINV(0.0902040104; 2; 3)")
    assert res.ok
    assert abs(exec_result(res, []) - 1.5) < 1e-6

    # 9. GAMMALN
    res = translate_formula("=GAMMALN(1.5)")
    assert res.ok
    assert abs(exec_result(res, []) - (-0.1207822376)) < 1e-6

    # 10. GAUSS
    res = translate_formula("=GAUSS(1.5)")
    assert res.ok
    assert abs(exec_result(res, []) - 0.4331927987) < 1e-6

    # 11. HYPGEOMDIST
    res = translate_formula("=HYPGEOMDIST(1; 3; 4; 10)")
    assert res.ok
    assert abs(exec_result(res, []) - 0.5) < 1e-6

    # 12. LOGINV
    res = translate_formula("=LOGINV(0.2975321419; 2; 3)")
    assert res.ok
    assert abs(exec_result(res, []) - 1.5) < 1e-6

    # 13. LOGNORMDIST
    res = translate_formula("=LOGNORMDIST(1.5; 2; 3; 1)")
    assert res.ok
    assert abs(exec_result(res, []) - 0.2975321419) < 1e-6

    # 14. NEGBINOMDIST
    res = translate_formula("=NEGBINOMDIST(5; 10; 0.5)")
    assert res.ok
    assert abs(exec_result(res, []) - 0.0610961914) < 1e-6

    # 15. NORMDIST
    res = translate_formula("=NORMDIST(1.5; 2; 3; 1)")
    assert res.ok
    assert abs(exec_result(res, []) - 0.4338161674) < 1e-6


def test_translate_norminv():
    res = translate_formula("=NORMINV(0.5; 0; 1)")
    assert res.ok
    assert res.code == "xl.norminv(0.5, 0, 1)"

def test_translate_normsdist():
    res = translate_formula("=NORMSDIST(1)")
    assert res.ok
    assert res.code == "xl.normsdist(1)"

def test_translate_normsinv():
    res = translate_formula("=NORMSINV(0.5)")
    assert res.ok
    assert res.code == "xl.normsinv(0.5)"

def test_translate_pearson():
    res = translate_formula("=PEARSON(A1:A10; B1:B10)")
    assert res.ok
    assert "xl.pearson(data[0], data[1])" in res.code

def test_translate_percentrank():
    res = translate_formula("=PERCENTRANK(A1:A10; 5)")
    assert res.ok
    assert "xl.percentrank(data, 5)" in res.code

def test_translate_permut():
    res = translate_formula("=PERMUT(5; 2)")
    assert res.ok
    assert res.code == "xl.permut(5, 2)"

def test_translate_poisson():
    res = translate_formula("=POISSON(2; 2; 0)")
    assert res.ok
    assert res.code == "xl.poisson(2, 2, 0)"

def test_translate_prob():
    res = translate_formula("=PROB(A1:A10; B1:B10; 2; 5)")
    assert res.ok
    assert "xl.prob(data[0], data[1], 2, 5)" in res.code

def test_translate_standardize():
    res = translate_formula("=STANDARDIZE(42; 40; 1.5)")
    assert res.ok
    assert res.code == "xl.standardize(42, 40, 1.5)"

def test_translate_tdist():
    res = translate_formula("=TDIST(1.96; 60; 2)")
    assert res.ok
    assert res.code == "xl.tdist(1.96, 60, 2)"

def test_translate_tinv():
    res = translate_formula("=TINV(0.05; 60)")
    assert res.ok
    assert res.code == "xl.tinv(0.05, 60)"

def test_translate_ttest():
    res = translate_formula("=TTEST(A1:A10; B1:B10; 2; 1)")
    assert res.ok
    assert "xl.ttest(data[0], data[1], 2, 1)" in res.code

def test_translate_weibull():
    res = translate_formula("=WEIBULL(105; 20; 100; 1)")
    assert res.ok
    assert res.code == "xl.weibull(105, 20, 100, 1)"

def test_translate_ztest():
    res = translate_formula("=ZTEST(A1:A10; 4)")
    assert res.ok
    assert "xl.ztest(data, 4)" in res.code

def test_translate_asc():
    res = translate_formula("=ASC(A1)")
    assert res.ok
    assert "xl.asc(data)" in res.code

def test_translate_group_h():
    assert translate_formula("=BAHTTEXT(A1)").code == 'xl.bahttext(data)'
    assert translate_formula("=CLEAN(A1)").code == 'xl.clean(data)'
    assert translate_formula("=DOLLAR(A1; 2)").code == 'xl.dollar(data, 2)'
    assert translate_formula("=ENCODEURL(A1)").code == 'xl.encodeurl(data)'
    assert translate_formula("=FIXED(A1; 2; TRUE)").code == 'xl.fixed(data, 2, True)'
    assert translate_formula("=JIS(A1)").code == 'xl.jis(data)'
    assert translate_formula("=NUMBERVALUE(A1; \".\"; \",\")").code == "xl.numbervalue(data, '.', ',')"
    assert translate_formula("=T(A1)").code == 'xl.t(data)'
    assert translate_formula("=TEXTAFTER(A1; \"-\")").code == "xl.textafter(data, '-')"
    assert translate_formula("=TEXTBEFORE(A1; \"-\")").code == "xl.textbefore(data, '-')"
    assert translate_formula("=TEXTSPLIT(A1; \"-\")").code == "xl.textsplit(data, '-')"
    assert translate_formula("=UNICHAR(A1)").code == 'xl.unichar(data)'
    assert translate_formula("=UNICODE(A1)").code == 'xl.unicode(data)'
    assert translate_formula("=BESSELI(A1; B1)").code == 'xl.besseli(data[0], data[1])'
    assert translate_formula("=BESSELJ(A1; B1)").code == 'xl.besselj(data[0], data[1])'

def test_translate_group_b_functions():
    # 1. DOLLARDE
    res = translate_formula("=DOLLARDE(1.02; 16)")
    assert res.ok
    assert abs(exec_result(res, []) - 1.125) < 1e-9

    # 2. DOLLARFR
    res = translate_formula("=DOLLARFR(1.125; 16)")
    assert res.ok
    assert abs(exec_result(res, []) - 1.02) < 1e-9

    # 3. DURATION
    res = translate_formula("=DURATION(45292; 47118; 0.08; 0.09; 2; 1)")
    assert res.ok
    assert abs(exec_result(res, []) - 4.1984139) < 1e-4

    # 4. EFFECT
    res = translate_formula("=EFFECT(0.0525; 4)")
    assert res.ok
    assert abs(exec_result(res, []) - 0.053542667) < 1e-5

    # 5. FVSCHEDULE
    res = translate_formula("=FVSCHEDULE(10000; A1:A3)")
    assert res.ok
    assert abs(exec_result(res, [0.09, 0.11, 0.1]) - 13308.9) < 1e-5

    # 6. INTRATE
    res = translate_formula("=INTRATE(45292; 47118; 100000; 105000; 2)")
    assert res.ok
    assert abs(exec_result(res, []) - 0.0098576) < 1e-5

    # 7. IPMT
    res = translate_formula("=IPMT(0.08/12; 1; 36; 8000)")
    assert res.ok
    assert abs(exec_result(res, []) - (-53.33333333)) < 1e-5

    # 8. ISPMT
    res = translate_formula("=ISPMT(0.08/12; 1; 36; 8000)")
    assert res.ok
    assert abs(exec_result(res, []) - (-51.8518518)) < 1e-5

    # 9. MDURATION
    res = translate_formula("=MDURATION(45292; 47118; 0.08; 0.09; 2; 1)")
    assert res.ok
    assert abs(exec_result(res, []) - 4.0176209) < 1e-4

    # 10. MIRR
    res = translate_formula("=MIRR(A1:A4; 0.1; 0.12)")
    assert res.ok
    assert abs(exec_result(res, [-120000, 39000, 30000, 21000]) - (-0.048044655249)) < 1e-3

    # 11. NOMINAL
    res = translate_formula("=NOMINAL(0.053543; 4)")
    assert res.ok
    assert abs(exec_result(res, []) - 0.0525) < 1e-4

    # 12. NPER
    res = translate_formula("=NPER(0.12/12; -100; -1000; 10000)")
    assert res.ok
    assert abs(exec_result(res, []) - 60.0821229) < 1e-5

    # 13. ODDFPRICE
    res = translate_formula("=ODDFPRICE(45292; 47118; 45000; 45500; 0.08; 0.09; 100; 2; 1)")
    assert res.ok
    assert abs(exec_result(res, []) - 93.471810) < 1e-4

    # 14. ODDFYIELD
    res = translate_formula("=ODDFYIELD(45292; 47118; 45000; 45500; 0.08; 95; 100; 2; 1)")
    assert res.ok
    assert abs(exec_result(res, []) - 0.092309) < 1e-4

    # 15. ODDLPRICE
    res = translate_formula("=ODDLPRICE(45292; 47118; 45000; 0.08; 0.09; 100; 2; 1)")
    assert res.ok
    assert abs(exec_result(res, []) - 87.874726) < 1e-4


def test_translate_group_i():
    assert translate_formula("=BESSELK(A1; B1)").code == 'xl.besselk(data[0], data[1])'
    assert translate_formula("=BESSELY(A1; B1)").code == 'xl.bessely(data[0], data[1])'
    assert translate_formula("=EUROCONVERT(A1; \"ATS\"; \"EUR\")").code == "xl.euroconvert(data, 'ATS', 'EUR')"
    assert translate_formula("=IMCOSH(A1)").code == 'xl.imcosh(data)'
    assert translate_formula("=IMCOT(A1)").code == 'xl.imcot(data)'
    assert translate_formula("=IMCSC(A1)").code == 'xl.imcsc(data)'
    assert translate_formula("=IMCSCH(A1)").code == 'xl.imcsch(data)'
    assert translate_formula("=IMSEC(A1)").code == 'xl.imsec(data)'
    assert translate_formula("=IMSECH(A1)").code == 'xl.imsech(data)'
    assert translate_formula("=IMSINH(A1)").code == 'xl.imsinh(data)'
    assert translate_formula("=IMSQRT(A1)").code == 'xl.imsqrt(data)'
    assert translate_formula("=IMSUB(A1; B1)").code == 'xl.imsub(data[0], data[1])'
    assert translate_formula("=IMSUM(A1; B1)").code == 'xl.imsum(data[0], data[1])'
    assert translate_formula("=IMTAN(A1)").code == 'xl.imtan(data)'
    assert translate_formula("=IMTANH(A1)").code == 'xl.imtanh(data)'


def test_translate_multiple_arguments_aggregates():
    res = translate_formula("=SUM(C7; C12)")
    assert not res.ok
    assert res.reason == "UNSUPPORTED_FUNCTION"

    res = translate_formula("=AVERAGE(A1:A3; B1:B3)")
    assert res.ok
    assert "np.concatenate" in res.code

    res = translate_formula("=PRODUCT(A1; B1)")
    assert res.ok
    assert "np.prod" in res.code

    res = translate_formula("=COUNT(A1; B1)")
    assert res.ok
    assert "sum(np.sum" in res.code


