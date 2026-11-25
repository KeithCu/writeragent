# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Bad inputs for public Calc helpers whose names start with N–S.

Each row is text, a blank, zero, or out of range, and is not already asserted
in ``test_calc_functions.py``. ``subtotal`` already has that table, so it is
not repeated. ``na`` takes no arguments. PPMT is not a registered helper.

``nan`` is this module's token for ``#NUM!`` / ``#VALUE!`` / ``#DIV/0!``.
A number or string is the value the helper already returns when that input
is not an error (text ignored, or a defined zero).
"""

from __future__ import annotations

import math
import struct

import numpy as np
import pytest

import plugin.scripting.calc_functions as calc
from plugin.scripting.calc_range import CalcRange
from plugin.scripting.venv.calc_functions_util import nan_error_code

_INF = float("inf")

# name, args, expected, id
# expected is "nan", "inf", or the exact current result.
_CASES: list[tuple[str, tuple[object, ...], object, str]] = [
    # N() maps text and blank to 0. +inf stays +inf; it does not raise.
    ("n", ("text",), 0.0, "n-text"),
    ("n", ("",), 0.0, "n-blank"),
    ("n", (0,), 0.0, "n-zero"),
    ("n", (_INF,), "inf", "n-out-of-range"),
    ("negbinomdist", ("text", 5, 0.5), ValueError, "negbinomdist-text"),
    ("negbinomdist", ("", 5, 0.5), ValueError, "negbinomdist-blank"),
    ("negbinomdist", (2, 5, 0), 0.0, "negbinomdist-zero-prob"),
    ("negbinomdist", (_INF, 5, 0.5), OverflowError, "negbinomdist-out-of-range"),
    ("networkdays", ("text", 46185), "nan", "networkdays-text"),
    ("networkdays", ("", 46185), "nan", "networkdays-blank"),
    ("networkdays", (0, 0), 0.0, "networkdays-zero"),
    ("networkdays", (1e20, 1e20), "nan", "networkdays-out-of-range"),
    ("networkdays_intl", ("text", 46185), "nan", "networkdays-intl-text"),
    ("networkdays_intl", ("", 46185), "nan", "networkdays-intl-blank"),
    ("networkdays_intl", (0, 0), 0.0, "networkdays-intl-zero"),
    ("networkdays_intl", (1e20, 1e20), "nan", "networkdays-intl-out-of-range"),
    ("nominal", ("text", 4), ValueError, "nominal-text"),
    ("nominal", ("", 4), ValueError, "nominal-blank"),
    ("nominal", (0.1, _INF), OverflowError, "nominal-out-of-range"),
    ("nominal", (_INF, 4), "nan", "nominal-inf-rate"),
    ("normdist", ("text", 0, 1), ValueError, "normdist-text"),
    ("normdist", ("", 0, 1), ValueError, "normdist-blank"),
    ("normdist", (1, 0, 1, None), 0.24197072451914337, "normdist-blank-flag"),
    ("normdist", (1, 0, 1, ""), 0.24197072451914337, "normdist-empty-flag"),
    ("normdist", (1, 0, 1, "abc"), ValueError, "normdist-text-flag"),
    ("normdist", (0, 0, 0), "nan", "normdist-zero-stdev"),
    ("normdist", (0, 0, -1), "nan", "normdist-out-of-range"),
    ("norminv", ("text", 0, 1), ValueError, "norminv-text"),
    ("norminv", ("", 0, 1), ValueError, "norminv-blank"),
    ("norminv", (0, 0, 1), "nan", "norminv-zero"),
    ("norminv", (1, 0, 1), "nan", "norminv-out-of-range"),
    ("normsdist", ("text",), ValueError, "normsdist-text"),
    ("normsdist", ("",), ValueError, "normsdist-blank"),
    ("normsdist", (0,), 0.5, "normsdist-zero"),
    ("normsdist", (_INF,), 1.0, "normsdist-out-of-range"),
    ("normsinv", ("text",), ValueError, "normsinv-text"),
    ("normsinv", ("",), ValueError, "normsinv-blank"),
    ("normsinv", (0,), "nan", "normsinv-zero"),
    ("normsinv", (1,), "nan", "normsinv-out-of-range"),
    ("nper", ("text", -100, 1000), ValueError, "nper-text"),
    ("nper", ("", -100, 1000), ValueError, "nper-blank"),
    ("npv", ("", 100, 200), ValueError, "npv-blank"),
    ("npv", (None, 100, 200), TypeError, "npv-blank-none"),
    ("numbervalue", ("text",), "nan", "numbervalue-text"),
    ("numbervalue", ("",), "nan", "numbervalue-blank"),
    ("numbervalue", (0,), 0.0, "numbervalue-zero"),
    ("numbervalue", ("1.2.3",), "nan", "numbervalue-out-of-range"),
    ("odd", ("",), ValueError, "odd-blank"),
    ("odd", (0,), 1.0, "odd-zero"),
    ("odd", (_INF,), "nan", "odd-out-of-range"),
    ("oddfprice", ("text", 41000, 39900, 40100, 0.05, 0.06, 100, 2), "nan", "oddfprice-text"),
    ("oddfprice", ("", 41000, 39900, 40100, 0.05, 0.06, 100, 2), "nan", "oddfprice-blank"),
    ("oddfyield", ("text", 41000, 39900, 40100, 0.05, 95, 100, 2), "nan", "oddfyield-text"),
    ("oddfyield", ("", 41000, 39900, 40100, 0.05, 95, 100, 2), "nan", "oddfyield-blank"),
    # price + redemption == 0 returns NaN instead of raising ZeroDivisionError.
    ("oddfyield", (40000, 41000, 39900, 40100, 0.05, -100, 100, 2), "nan", "oddfyield-zero-denom"),
    ("oddfyield", (40000, 40000, 39900, 40100, 0.05, 95, 100, 2), "nan", "oddfyield-out-of-range"),
    ("oddlprice", ("text", 41000, 39000, 0.05, 0.06, 100, 2), "nan", "oddlprice-text"),
    ("oddlprice", ("", 41000, 39000, 0.05, 0.06, 100, 2), "nan", "oddlprice-blank"),
    ("pearson", ("text", [1, 2, 3]), "nan", "pearson-text"),
    ("pearson", ("", [1, 2, 3]), "nan", "pearson-blank"),
    ("pearson", ([0, 0, 0], [1, 2, 3]), "nan", "pearson-zero"),
    ("pearson", ([1, 2], [1, 2, 3]), "nan", "pearson-out-of-range"),
    ("percentrank", ("text", 1), "nan", "percentrank-text"),
    ("percentrank", ("", 1), "nan", "percentrank-blank"),
    # Significance < 1 returns NaN (#NUM! in Excel/Calc).
    ("percentrank", ([1, 2, 3, 4], 3, 0), "nan", "percentrank-zero-significance"),
    ("percentrank", ([1, 2, 3, 4], 3, _INF), "nan", "percentrank-out-of-range"),
    ("permut", ("text", 2), ValueError, "permut-text"),
    ("permut", ("", 2), ValueError, "permut-blank"),
    ("permut", (0, 1), "nan", "permut-zero"),
    ("permut", (_INF, 2), OverflowError, "permut-out-of-range"),
    ("pmt", ("", 12, 1000), ValueError, "pmt-blank"),
    # Zero rate is the closed form -pv/nper, not #DIV/0!.
    ("pmt", (0, 12, 1000), -1000 / 12, "pmt-zero-rate"),
    ("pmt", (-1, 12, 1000), 0.0, "pmt-out-of-range"),
    ("poisson", ("text", 1), ValueError, "poisson-text"),
    ("poisson", ("", 1), ValueError, "poisson-blank"),
    ("poisson", (0, 0), "nan", "poisson-zero"),
    ("poisson", (0, 0, 0), "nan", "poisson-zero-pmf"),
    ("poisson", (_INF, 1), OverflowError, "poisson-out-of-range"),
    ("prob", ("text", [0.2, 0.3, 0.5], 1), "nan", "prob-text"),
    ("prob", ("", [0.2, 0.3, 0.5], 1), "nan", "prob-blank"),
    ("prob", ([1, 2], [0, 0], 1), "nan", "prob-zero"),
    ("prob", ([1, 2], [0.2, 0.2], 1), "nan", "prob-out-of-range"),
    ("pv", ("", 12, -100), ValueError, "pv-blank"),
    ("pv", (0.01, 0, -100), 0.0, "pv-zero-nper"),
    ("pv", (-1, 12, -100), "nan", "pv-out-of-range"),
    ("quartile", ([1.0, 2.0, 3.0, 4.0], ""), "nan", "quartile-blank"),
    ("quartile", ([1.0, 2.0, 3.0, 4.0], None), "nan", "quartile-blank-none"),
    ("rank", ("text", [1, 2, 3]), ValueError, "rank-text"),
    ("rank", ("", [1, 2, 3]), ValueError, "rank-blank"),
    ("rank", (0, [0, 1, 2]), 3.0, "rank-zero"),
    ("rank", (9, [1, 2, 3]), "#N/A", "rank-out-of-range"),
    ("regex", ("abc", ""), "", "regex-blank-pattern"),
    ("rept", ("ab", "text"), ValueError, "rept-text-count"),
    ("rept", ("ab", ""), ValueError, "rept-blank-count"),
    ("rsq", ("", [1.0, 2.0, 3.0]), "nan", "rsq-blank"),
    ("rsq", (None, [1.0, 2.0, 3.0]), "nan", "rsq-blank-none"),
    ("rsq", ([0.0, 0.0, 0.0], [0.0, 0.0, 0.0]), "nan", "rsq-zero"),
    ("rsq", ([1.0, 2.0, 3.0], [1.0, 2.0]), "nan", "rsq-out-of-range"),
    ("sec", ("text",), ValueError, "sec-text"),
    ("sec", ("",), ValueError, "sec-blank"),
    ("sec", (0,), 1.0, "sec-zero"),
    ("sec", (_INF,), "nan", "sec-out-of-range"),
    ("sech", ("text",), ValueError, "sech-text"),
    ("sech", ("",), ValueError, "sech-blank"),
    ("sech", (0,), 1.0, "sech-zero"),
    ("sech", (_INF,), "nan", "sech-out-of-range"),
    ("seriessum", ("text", 1, 1, [1]), "nan", "seriessum-text"),
    ("seriessum", ("", 1, 1, [1]), "nan", "seriessum-blank"),
    ("seriessum", (0, 1, 1, [1, 2]), 0.0, "seriessum-zero"),
    ("seriessum", (2, 1, 1, ["a"]), "nan", "seriessum-out-of-range"),
    ("skew", ("text",), "nan", "skew-text"),
    ("skew", ("",), "nan", "skew-blank"),
    ("slope", ("", [1.0, 2.0, 3.0]), "nan", "slope-blank"),
    ("slope", (None, [1.0, 2.0, 3.0]), "nan", "slope-blank-none"),
    ("slope", ([0.0, 0.0, 0.0], [0.0, 0.0, 0.0]), "nan", "slope-zero"),
    ("slope", ([1.0, 2.0, 3.0], [1.0, 2.0]), "nan", "slope-out-of-range"),
    ("small", ("", 1), "nan", "small-blank"),
    ("small", ([1.0, 2.0, 3.0], 0), "nan", "small-zero-k"),
    ("small", ([1.0, 2.0, 3.0], 9), "nan", "small-out-of-range"),
    ("sort", ("text",), "nan", "sort-text"),
    ("sort", ("",), "nan", "sort-blank"),
    ("sort", (None,), "nan", "sort-blank-none"),
    ("sort", (0,), "nan", "sort-zero"),
    ("sortby", ("text", [1]), "nan", "sortby-text"),
    ("sortby", ("", [1]), "nan", "sortby-blank"),
    ("sortby", (0, [0]), "nan", "sortby-zero"),
    ("sqrtpi", ("text",), ValueError, "sqrtpi-text"),
    ("sqrtpi", ("",), ValueError, "sqrtpi-blank"),
    ("sqrtpi", (0,), 0.0, "sqrtpi-zero"),
    ("sqrtpi", (-1,), "nan", "sqrtpi-out-of-range"),
    ("standardize", ("text", 0, 1), ValueError, "standardize-text"),
    ("standardize", ("", 0, 1), ValueError, "standardize-blank"),
    ("standardize", (1, 0, 0), "nan", "standardize-zero-stdev"),
    ("standardize", (1, 0, -1), "nan", "standardize-out-of-range"),
    ("stdeva", ("text",), "nan", "stdeva-text"),
    ("stdeva", ("",), "nan", "stdeva-blank"),
    ("stdeva", (0,), "nan", "stdeva-zero"),
    ("stdeva", (), "nan", "stdeva-out-of-range"),
    ("stdevpa", ("text",), 0.0, "stdevpa-text"),
    ("stdevpa", ("",), 0.0, "stdevpa-blank"),
    ("stdevpa", (0,), 0.0, "stdevpa-zero"),
    ("stdevpa", (), "nan", "stdevpa-out-of-range"),
    ("steyx", ("", [1.0, 2.0, 3.0]), "nan", "steyx-blank"),
    ("steyx", (None, [1.0, 2.0, 3.0]), "nan", "steyx-blank-none"),
    ("steyx", ([0.0, 0.0, 0.0], [0.0, 0.0, 0.0]), "nan", "steyx-zero"),
    ("steyx", ([1.0, 2.0, 3.0], [1.0, 2.0]), "nan", "steyx-out-of-range"),
    # Text and blanks are ignored, so these sums stay 0 rather than #VALUE!.
    ("sumif", ("text", ">0"), 0.0, "sumif-text"),
    ("sumif", ("", ">0"), 0.0, "sumif-blank"),
    ("sumif", ([0, 0], ">0"), 0.0, "sumif-zero"),
    ("sumif", ([1, 2, 3], "no-such"), 0.0, "sumif-out-of-range"),
    ("sumifs", ("text", [1, 2], ">0"), 0.0, "sumifs-text"),
    ("sumifs", ("", [1, 2], ">0"), 0.0, "sumifs-blank"),
    ("sumifs", ([0, 1, 2], [0, 1, 2], 0), 0.0, "sumifs-zero"),
    ("sumproduct", ("text",), 0.0, "sumproduct-text"),
    ("sumproduct", ("",), 0.0, "sumproduct-blank"),
    ("sumproduct", ([0, 0], [0, 0]), 0.0, "sumproduct-zero"),
    ("sumsq", ("text",), 0.0, "sumsq-text"),
    ("sumsq", ("",), 0.0, "sumsq-blank"),
    ("sumsq", (0,), 0.0, "sumsq-zero"),
    ("sumsq", (), 0.0, "sumsq-empty"),
]


def _assert_calc_result(result: object, expected: object) -> None:
    if expected == "nan":
        assert isinstance(result, float) and math.isnan(result)
        return
    if expected == "inf":
        assert isinstance(result, float) and math.isinf(result) and result > 0
        return
    assert result == expected


@pytest.mark.parametrize(
    ("name", "args", "expected"),
    [case[:3] for case in _CASES],
    ids=[case[3] for case in _CASES],
)
def test_n_s_bad_inputs(name: str, args: tuple[object, ...], expected: object) -> None:
    if isinstance(expected, type) and issubclass(expected, BaseException):
        with pytest.raises(expected):
            getattr(calc, name)(*args)
        return
    result = getattr(calc, name)(*args)
    _assert_calc_result(result, expected)


def test_networkdays_large_span():
    # Weekday counting via week arithmetic; should not hang on large spans
    res = calc.networkdays(1, 70000)
    assert not math.isnan(res)
    assert res > 0.0


def test_npv_skips_non_numeric():
    # Bug Z6: npv skips blank and text cells without consuming a period
    base = calc.npv(0.1, 100, 100)
    with_blank = calc.npv(0.1, 100, None, 100)
    with_text = calc.npv(0.1, 100, "text", 100)
    assert abs(with_blank - base) < 1e-9
    assert abs(with_text - base) < 1e-9


def test_nan_cells_propagate_in_npv_sumsq_subtotal_sumifs():
    # na() and error tokens propagate through NPV, SUMSQ, and SUBTOTAL's
    # numeric functions, and through SUMIFS when the row matches. COUNT skips
    # errors and COUNTA counts them. NPV counts booleans as 1 and 0.
    nan = float("nan")
    assert math.isnan(calc.npv(0.1, 100, nan, 100))
    assert math.isnan(calc.npv(0.1, nan))
    assert calc.npv(0.1, 100, "#N/A", 100) == "#N/A"
    assert math.isclose(calc.npv(0.1, 100, True, 100), 100 / 1.1 + 1 / 1.21 + 100 / 1.331)

    assert math.isnan(calc.sumsq(nan))
    assert math.isnan(calc.sumsq([1.0, nan, 3.0]))
    assert calc.sumsq(["#N/A", 3]) == "#N/A"
    assert calc.sumsq(["5", 3]) == 9.0

    assert math.isnan(calc.subtotal(9, [1.0, nan, 2.0]))
    assert math.isnan(calc.subtotal(1, [1.0, nan, 3.0]))
    assert math.isnan(calc.subtotal(4, [1.0, nan]))
    assert math.isnan(calc.subtotal(6, [2.0, nan]))
    assert math.isnan(calc.subtotal(7, [1.0, 3.0, nan]))
    assert calc.subtotal(2, [1.0, nan, 2.0]) == 2.0
    assert calc.subtotal(2, [nan]) == 0.0
    assert calc.subtotal(3, [1.0, nan]) == 2.0
    assert calc.subtotal(3, [nan]) == 1.0
    assert calc.subtotal(9, []) == 0.0
    # 109 subtracts 100 once and is still SUM, so the NaN still propagates.
    assert math.isnan(calc.subtotal(109, [1.0, nan, 2.0]))

    assert math.isnan(calc.sumifs([1.0, nan, 3.0], [1.0, 1.0, 1.0], ">0"))
    assert calc.sumifs([1.0, nan, 3.0], [1.0, 0.0, 1.0], ">0") == 4.0
    assert calc.sumifs([1.0, "#N/A", 3.0], [1.0, 1.0, 1.0], ">0") == "#N/A"
    assert calc.sumifs([1.0, "#N/A", 3.0], [1.0, 0.0, 1.0], ">0") == 4.0


def test_numbervalue_grp_equals_dec():
    # Bug Z4: numbervalue("1,5", ",") should not drop grp when grp==dec
    assert calc.numbervalue("1,5", ",") == 1.5


def test_percentrank_truncation_and_validation():
    # Bug Z9: sig < 1 returns NaN, and values are truncated rather than rounded
    assert math.isnan(calc.percentrank([1, 2, 3, 4], 3, 0))
    assert math.isnan(calc.percentrank([1, 2, 3, 4], 3, -1))
    # 2.5 in [1, 2, 3, 4] is at percentile 0.5; truncated to 1 digit is 0.5
    assert calc.percentrank([1, 2, 3, 4], 2.5, 1) == 0.5


def test_rank_small_extract_numeric():
    # Bug Z15: rank and small ignore NaN, text, and bools
    assert calc.rank(3, [1, float("nan"), 3]) == 1.0
    assert calc.rank(3, [1, "a", 3]) == 1.0
    assert calc.rank(3, [1, True, 3]) == 1.0

    assert calc.small([1, float("nan"), 3], 1) == 1.0
    assert calc.small([1, "a", 3], 1) == 1.0
    assert calc.small([1, True, 3], 1) == 1.0

    # Missing number is #N/A. A NaN lookup keeps its payload, and an empty numeric list stays #NUM!.
    assert calc.rank(4, [1, 2, 3]) == "#N/A"
    assert calc.rank(0, [1, 2, 3], 1) == "#N/A"
    assert calc.rank(2, [1, 3]) == "#N/A"
    assert math.isnan(calc.rank(float("nan"), [1, 2, 3]))
    na_rank = calc.rank(calc.na(), [1])
    assert isinstance(na_rank, float) and math.isnan(na_rank)
    assert (struct.unpack("Q", struct.pack("d", na_rank))[0] & 0xFFFFFFFF) == 0x7FFF
    assert math.isnan(calc.rank(1, []))
    assert math.isnan(calc.rank(1, ["a", "b"]))
    assert math.isnan(calc.rank(1, [float("nan")]))
    assert calc.rank(20, [10, 20, 20, 30]) == 2.0
    assert calc.rank(20, [10, 20, 20, 30], 1) == 2.0
    assert calc.rank(5, [5, 5, 5, 1]) == 1.0
    assert calc.rank(5, [5, 5, 5, 1], 1) == 2.0


def test_regex_calc_replacement_syntax():
    # Bug Z13: regex replacement supports Calc $1, $&, $$ syntax
    assert calc.regex("abc-123", "([a-z]+)-([0-9]+)", "$2-$1") == "123-abc"
    assert calc.regex("abc", "b", "[$&]") == "a[b]c"
    assert calc.regex("abc", "b", "$$") == "a$c"


def test_sech_overflow():
    # Bug Z8: sech(1000) returns 0.0 on overflow instead of crashing
    assert calc.sech(1000) == 0.0
    assert calc.sech(-1000) == 0.0
    assert math.isnan(calc.sech(float("inf")))
    assert math.isnan(calc.sech(float("-inf")))
    with pytest.raises(ValueError):
        calc.sech("x")


def test_seriessum_complex_returns_nan():
    # Bug Z14: negative base with fractional power returns NaN, not complex
    res = calc.seriessum(-2, 0.5, 1, [1, 1])
    assert isinstance(res, float)
    assert math.isnan(res)


def test_sort_sortby_dtype_object_and_ties():
    # Bug Z2 & Z11: mixed-type ranges stay uncorrupted and descending sort preserves ties
    sorted_mixed = calc.sort([["a", 10], ["b", 9]], 2)
    assert sorted_mixed == [["b", 9], ["a", 10]]
    # Ties preserved in descending sort
    ties = [[1, "first"], [1, "second"]]
    assert calc.sort(ties, 1, -1) == [[1, "first"], [1, "second"]]
    assert calc.sort([[1, 1], [10, 20]], 1, -1, True) == [[1, 1], [10, 20]]

    # sortby preserves mixed types
    res_sortby = calc.sortby([["a", 10], ["b", 9]], [10, 9])
    assert res_sortby == [["b", 9], ["a", 10]]


def test_rsq_slope_steyx_paired_clean():
    # Clean paired arrays mask out NaNs consistently across rsq, slope, steyx
    assert calc.rsq([2, 4, float("nan"), 8], [1, 2, 5, 4]) == 1.0
    assert calc.rsq([1, 2, 3], [3, 2, 1]) == 1.0
    assert calc.slope([2, 4, float("nan"), 8], [1, 2, 5, 4]) == 2.0
    assert calc.steyx([2, 4, float("nan"), 8], [1, 2, 5, 4]) == 0.0


def test_odd_rounds_away_from_zero():
    # ScOdd ceils away from zero, then steps off an even integer.
    assert calc.odd(1.5) == 3.0
    assert calc.odd(-1.5) == -3.0
    assert calc.odd(0) == 1.0
    assert calc.odd(2) == 3.0
    assert calc.odd(-2) == -3.0
    assert calc.odd(-3.2) == -5.0
    assert calc.odd(1.1) == 3.0
    assert calc.odd(-0.5) == -1.0


def test_regex_occurrence_and_whole_match():
    # Search returns the whole match even when an optional group did not participate.
    assert calc.regex("b", "(a)?b") == "b"
    assert calc.regex("abc-123", "([a-z]+)-([0-9]+)") == "abc-123"
    assert calc.regex("ababa", "a", flags=2) == "a"
    assert calc.regex("a1b2", r"[a-z]\d", flags=2) == "b2"
    assert calc.regex("ababa", "a", "X", 2) == "abXba"
    assert calc.regex("abc", "z") == "#N/A"
    # Occurrence 0 is the original text, before the pattern is compiled.
    assert calc.regex("abc", "[", flags=0) == "abc"
    assert calc.regex("aba", "a", flags="g") == "a"
    assert math.isnan(calc.regex("abc", "b", flags="x"))
    assert math.isnan(calc.regex("abc", "b", flags=-1))


def test_rept_length_cap_and_bad_count():
    # Excel REPT allows 32767 characters. The old cap was 2**31 // 8.
    assert len(calc.rept("a", 32767)) == 32767
    assert math.isnan(calc.rept("a", 32768))
    assert len(calc.rept("ab", 16383)) == 32766
    assert math.isnan(calc.rept("ab", 16384))
    assert calc.rept("", 10**9) == ""
    with pytest.raises(ValueError):
        calc.rept("a", "x")
    with pytest.raises(ValueError):
        calc.rept("ab", "text")


def test_subtotal_sumproduct_sumsq_ignore_text_keep_bools():
    # TRUE is 1. The text "5" is not a number. 201 is not a function code.
    assert calc.subtotal(9, [1, True, "5"]) == 2.0
    assert math.isnan(calc.subtotal(201, [1, 2]))
    assert math.isnan(calc.subtotal(211, [1, 2]))
    assert calc.subtotal(3, ["5"]) == 1.0
    assert calc.subtotal(2, [1, True, "5"]) == 2.0
    assert calc.sumproduct([1, "5"], [2, 2]) == 2.0
    assert calc.sumsq(["5", 3]) == 9.0
    huge = 10**10000
    assert math.isnan(calc.sumproduct([huge], [1]))
    assert math.isnan(calc.sumsq(huge))
    assert math.isnan(calc.subtotal(9, [huge]))


def test_numbervalue_percent_spaces_and_exponent():
    assert calc.numbervalue("5%") == 0.05
    assert calc.numbervalue("5%%") == 0.0005
    assert calc.numbervalue(" 5 % ") == 0.05
    assert calc.numbervalue("1 000") == 1000.0
    assert calc.numbervalue("1e5") == 100000.0
    assert math.isnan(calc.numbervalue("1_000"))
    assert math.isnan(calc.numbervalue("inf"))
    assert math.isnan(calc.numbervalue("INF"))
    assert math.isnan(calc.numbervalue("%%"))
    assert math.isnan(calc.numbervalue("   "))
    assert calc.numbervalue("1,234.56") == 1234.56
    assert calc.numbervalue("1.234,56", ",", ".") == 1234.56


def test_oddfprice_negative_base_is_nan():
    # (-2) ** fractional is a Python complex and must not leak out.
    res = calc.oddfprice(40000, 41000, 39900, 40100, 0.05, -3, 100, 1)
    assert isinstance(res, float)
    assert math.isnan(res)


def test_oddlprice_uses_yearfrac():
    # GetOddlprice. Basis 3 is Actual/365; basis 0 is 30/360.
    def expected(basis: int) -> float:
        rate = 0.05
        yld = 0.06
        red = 100.0
        last, settle, mat = 39000, 40000, 41000
        span_last_mat = calc.yearfrac(last, mat, basis)
        span_set_mat = calc.yearfrac(settle, mat, basis)
        span_last_set = calc.yearfrac(last, settle, basis)
        return (red + span_last_mat * 100.0 * rate) / (1.0 + span_set_mat * yld) - span_last_set * 100.0 * rate

    basis0 = calc.oddlprice(40000, 41000, 39000, 0.05, 0.06, 100, 2, 0)
    basis3 = calc.oddlprice(40000, 41000, 39000, 0.05, 0.06, 100, 2, 3)
    assert math.isclose(basis0, expected(0), rel_tol=1e-12)
    assert math.isclose(basis3, expected(3), rel_tol=1e-12)
    assert basis0 != basis3


def test_sort_order_is_only_one_or_minus_one():
    assert math.isnan(calc.sort([3, 1, 2], 1, 0))
    assert math.isnan(calc.sort([3, 1, 2], 1, 2))
    assert calc.sort([3, 1, 2], 1, 1) == [1, 2, 3]
    assert calc.sort([3, 1, 2], 1, -1) == [3, 2, 1]
    data = [[3, 1, 2], [6, 4, 5]]
    # Key shape (1, 3) sorts columns: 1, then 2, then 3.
    assert calc.sortby(data, [[2, 3, 1]]) == [[2, 3, 1], [5, 6, 4]]
    assert math.isnan(calc.sortby([1, 2, 3], [1, 2, 3], 0))
    assert math.isnan(calc.sortby(data, [[2, 3, 1]], 2))
    # A row key and a column key in one call do not share an axis.
    assert math.isnan(calc.sortby(data, [[3], [1]], 1, [[2, 3, 1]], 1))


def test_oddf_frequency_and_basis():
    # isFreqInvalid is {1, 2, 4}; non-member frequencies including 1e7 return NaN immediately.
    args = (40000, 41000, 39900, 40100, 0.05, 0.06, 100)
    for freq in (3, 1.5, 4.9, 1.9, 0, -1, 10_000_000):
        assert math.isnan(calc.oddfprice(*args, freq))
        assert math.isnan(calc.oddfyield(*args[:4], 0.05, 95, 100, freq))
        assert math.isnan(calc.oddlprice(40000, 41000, 39000, 0.05, 0.06, 100, freq))
    assert math.isnan(calc.oddfprice(*args, 2, 9))
    assert math.isnan(calc.oddfprice(*args, 2, -1))
    assert math.isnan(calc.oddfprice(*args, 2, 5))
    assert math.isnan(calc.oddfyield(40000, 41000, 39900, 40100, 0.05, 95, 100, 2, 9))
    assert math.isnan(calc.oddlprice(40000, 41000, 39000, 0.05, 0.06, 100, 2, 9))
    # 4.0 == 4. int(float(4.9)) is basis 4, which stays legal.
    priced = calc.oddfprice(*args, 2)
    assert math.isfinite(priced)
    assert calc.oddfprice(*args, 4.0, 4) == calc.oddfprice(*args, 4, 4.9)
    assert math.isfinite(calc.oddfprice(*args, 4.0, 4))
    # Frequency does not enter the odd-last formula or the odd-first yield.
    last = [
        calc.oddlprice(40000, 41000, 39000, 0.05, 0.06, 100, freq)
        for freq in (1, 2, 4)
    ]
    assert last[0] == last[1] == last[2]
    assert math.isfinite(last[0])
    yld = [
        calc.oddfyield(40000, 41000, 39900, 40100, 0.05, 95, 100, freq)
        for freq in (1, 2, 4)
    ]
    assert yld[0] == yld[1] == yld[2]
    assert math.isfinite(yld[0])


def test_sqrtpi_overflow_is_nan():
    assert math.isnan(calc.sqrtpi(1e308))
    assert math.isnan(calc.sqrtpi(float("inf")))
    assert calc.sqrtpi(0) == 0.0
    assert math.isfinite(calc.sqrtpi(1))


def test_sumproduct_sumsq_float_overflow_is_nan():
    assert math.isnan(calc.sumproduct([1e200], [1e200]))
    assert math.isnan(calc.sumsq(1e200))
    assert math.isnan(calc.sumsq(1e200, -1e200))
    assert calc.sumproduct([1.0, 2.0], [3.0, 4.0]) == 11.0
    assert calc.sumsq(3, 4) == 25.0
    assert math.isnan(calc.sumproduct([float("nan")], [1.0]))


def test_poisson_text_cumulative_flag():
    pmf = calc.poisson(2, 2, False)
    cdf = calc.poisson(2, 2, True)
    assert math.isclose(calc.poisson(2, 2, "0"), pmf)
    assert math.isclose(calc.poisson(2, 2, 0), pmf)
    assert math.isclose(calc.poisson(2, 2, ""), pmf)
    assert math.isclose(calc.poisson(2, 2, None), pmf)
    assert math.isclose(calc.poisson(2, 2, "1"), cdf)
    assert math.isclose(calc.poisson(2, 2, "TRUE"), cdf)
    assert math.isclose(calc.poisson(2, 3), 0.4231900811268435)
    assert math.isclose(calc.poisson(2, 3, None), 0.22404180765538775)
    with pytest.raises(ValueError):
        calc.poisson(2, 2, "abc")


def test_npv_error_propagation_and_text_skip():
    import struct

    assert calc.npv(0.1, "#N/A", "#DIV/0!") == "#N/A"
    assert calc.npv("#N/A", 100) == "#N/A"
    assert calc.npv("#DIV/0!", "#N/A") == "#DIV/0!"
    assert calc.npv("abc", "#N/A") == "#N/A"
    assert calc.npv(0.1, " Err:502 ", 100) == "Err:502"
    assert math.isnan(calc.npv(-1, 5))
    assert calc.npv(-1, "#N/A") == "#N/A"
    assert math.isclose(calc.npv(0.1, "12", 5), 5 / 1.1)
    for na_res in (calc.npv(0.1, 100, calc.na(), 100), calc.npv(calc.na(), 100)):
        assert isinstance(na_res, float) and math.isnan(na_res)
        assert (struct.unpack("Q", struct.pack("d", na_res))[0] & 0xFFFF) == 0x7FFF


def test_poisson_approx_floor():
    assert math.isnan(calc.poisson(-0.5, 1))
    assert math.isnan(calc.poisson(-1e-17, 1))
    assert math.isclose(calc.poisson(2.9999999999999996, 1, 0), calc.poisson(3, 1, 0))
    assert math.isclose(calc.poisson(2.9999999999, 1, 0), calc.poisson(2, 1, 0))


def test_sort_flat_vector_index():
    assert math.isnan(calc.sort([3, 1, 2], 99))
    assert math.isnan(calc.sort([3, 1, 2], 2))
    assert calc.sort([3, 1, 2], 1.9) == [1, 2, 3]
    # by_col keys a row of a one-column vector: original order.
    assert calc.sort([3, 1, 2], 1, 1, True) == [3, 1, 2]
    assert calc.sort([3, 1, 2], 2, 1, True) == [3, 1, 2]
    assert math.isnan(calc.sort([3, 1, 2], 4, 1, True))
    assert calc.sort([], 99) == []


def test_n_text_returns_zero():
    # Bug Z5: n(str) should return 0.0, not convert numeric text
    assert calc.n("5") == 0.0
    assert calc.n("hello") == 0.0
    assert calc.n("") == 0.0
    assert calc.n(5) == 5.0
    assert calc.n(True) == 1.0
    assert calc.n(False) == 0.0
    assert calc.n(np.int64(42)) == 42.0
    assert calc.n(np.float64(3.5)) == 3.5
    with pytest.raises(OverflowError):
        calc.n(10**400)


def test_sumif_error_propagation_and_shapes():
    assert calc.sumif(["a", "a"], "a", [1, "#VALUE!"]) == "#VALUE!"
    assert math.isnan(calc.sumif(["a", "b"], "a", [1, 2, 3]))  # mismatched shapes
    assert calc.sumif(["a", "b"], "a", [1, "#VALUE!"]) == 1.0


def test_oddfprice_basis_and_percentrank_huge_significance():
    # ODDFPRICE with basis 3 vs basis 0 must differ
    p0 = calc.oddfprice(40000, 44000, 39000, 40500, 0.05, 0.06, 100, 2, 0)
    p3 = calc.oddfprice(40000, 44000, 39000, 40500, 0.05, 0.06, 100, 2, 3)
    assert not math.isnan(p0)
    assert not math.isnan(p3)
    assert p0 != p3

    # PERCENTRANK with huge significance returns quickly with NaN
    assert math.isnan(calc.percentrank([1, 2, 3], 2, 10**9))
    assert calc.percentrank([1, 2, 3], 2, 0.9999999999999999) == calc.percentrank([1, 2, 3], 2, 1)
    from plugin.scripting.venv.calc_functions_util import nan_error_code
    bad_sig = calc.percentrank([1, 2, 3], 2, 0.9)
    assert math.isnan(bad_sig) and nan_error_code(bad_sig) == 502


def test_na_returns_err_na_payload():
    val = calc.na()
    assert math.isnan(val)
    assert (struct.unpack("Q", struct.pack("d", val))[0] & 0xFFFFFFFF) == 0x7FFF
    assert calc.isna(val) is True


def test_negbinomdist_and_permut_approx_floor():
    from plugin.scripting.venv.calc_functions_util import nan_error_code

    for bad in (
        calc.negbinomdist(-0.5, 2, 0.5),
        calc.negbinomdist(0, 1, 0.5),
        calc.negbinomdist(-1, 2, 0.5),
    ):
        assert math.isnan(bad) and nan_error_code(bad) == 502
    assert math.isclose(calc.negbinomdist(-0.5, 3, 0.5), 0.125)
    assert calc.negbinomdist(2, 1, 0) == 0.0
    assert calc.negbinomdist(0, 2, 0) == 0.0
    assert calc.negbinomdist(2, 0, 0.5) == 0.0
    assert calc.negbinomdist(5, -1, 0.5) == 0.0
    assert calc.negbinomdist(1e20, 3, 0.5) == 0.0
    zero_neg = calc.negbinomdist(5, -1, 0)
    assert math.isnan(zero_neg) and nan_error_code(zero_neg) == 0
    na_prob = calc.negbinomdist(2, 0, calc.na())
    assert math.isnan(na_prob) and nan_error_code(na_prob) == 0x7FFF
    assert math.isnan(calc.permut(5, -0.5))
    assert calc.permut(0.29 * 100, 1) == 29


def test_prob_compares_dimensions():
    from plugin.scripting.venv.calc_functions_util import nan_error_code

    for bad in (
        calc.prob([[1, 2, 3]], [[0.2], [0.3], [0.5]], 1),
        calc.prob([[1, 2], [3, 4]], [[0.25, 0.25, 0.25, 0.25]], 1),
    ):
        assert math.isnan(bad) and nan_error_code(bad) == 0x7FFF
    assert math.isclose(calc.prob([1, 2, 3], [[0.2], [0.3], [0.5]], 2), 0.3)
    assert calc.prob(1, 1, 1) == 1.0


def test_sumsq_error_propagation():
    assert calc.sumsq("#N/A", "#DIV/0!") == "#DIV/0!"
    assert calc.sumsq(["#N/A", "#DIV/0!"]) == "#N/A"
    assert calc.sumsq("#N/A", 1e200) == "#N/A"
    assert calc.sumsq([1, True]) == 2
    na_sq = calc.sumsq([calc.na()])
    assert isinstance(na_sq, float) and math.isnan(na_sq)
    assert (struct.unpack("Q", struct.pack("d", na_sq))[0] & 0xFFFFFFFF) == 0x7FFF


def test_sumproduct_error_and_text_rules():
    assert calc.sumproduct([1], ["#N/A"]) == "#N/A"
    assert calc.sumproduct(["text"], ["#N/A"]) == 0
    assert calc.sumproduct(["text"], [calc.na()]) == 0
    assert calc.sumproduct(["#N/A"], ["text"]) == 0
    assert calc.sumproduct([None], ["#N/A"]) == "#N/A"
    assert calc.sumproduct(["#N/A"], ["#DIV/0!"]) == "#DIV/0!"
    assert calc.sumproduct(["#N/A"], [1], ["#DIV/0!"]) == "#DIV/0!"
    assert calc.sumproduct(["#N/A", "#DIV/0!"], [1, 1]) == "#N/A"
    assert calc.sumproduct([1], [True]) == 1
    assert calc.sumproduct([1e200], [1e200], [None]) == 0.0


def test_subtotal_error_propagation():
    assert calc.subtotal(9, ["#N/A", 3]) == "#N/A"
    mixed = ["text", "#N/A", 0.3, "#DIV/0!"]
    assert calc.subtotal(2, mixed) == 1
    assert calc.subtotal(3, mixed) == 4


def test_npv_booleans_and_text():
    assert math.isclose(calc.npv(0.1, True), 1.0 / 1.1)
    assert math.isclose(calc.npv(0.1, [1, True]), 1.0 / 1.1 + 1.0 / 1.21)
    assert math.isclose(calc.npv(0.1, [True, False], 10), 1.0 / 1.1 + 10.0 / 1.331)
    assert math.isclose(calc.npv(0.1, ["1"], 10), 10.0 / 1.1)


def test_numbervalue_empty_text_is_value_error():
    res = calc.numbervalue("")
    assert math.isnan(res) and nan_error_code(res) == 519


def test_blank_one_cell_args_in_prob_regex_and_numbervalue():
    blank = CalcRange([[None]])
    assert calc.prob([1, 2, 3], [0.2, 0.5, 0.3], 2, blank) == 0.0
    assert calc.regex("abc", "b", blank) == "ac"
    assert calc.regex("None here", blank) == ""
    assert calc.numbervalue("1e3", ".", blank) == 1000.0

