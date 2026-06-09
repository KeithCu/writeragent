# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for spreadsheet import P1 formula translation."""

from __future__ import annotations

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


def test_translate_and_exec_new_functions():
    import datetime
    import math

    import numpy as np

    base_locs = {"np": np, "xl": xl, "math": math, "datetime": datetime}

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
    import datetime
    import math

    import numpy as np

    locs = {"data": data, "np": np, "xl": xl, "math": math, "datetime": datetime}
    code = f"result = {res.code}"
    exec(code, locs)
    return locs["result"]


