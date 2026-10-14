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

import inspect
import math

import pytest

import plugin.scripting.calc_functions as calc

_INF = float("inf")

# name, args, expected, id
# expected is "nan", "inf", or the exact current result.
_CASES: list[tuple[str, tuple[object, ...], object, str]] = [
    # N() maps text and blank to 0. +inf stays +inf; it does not raise.
    ("n", ("text",), 0.0, "n-text"),
    ("n", ("",), 0.0, "n-blank"),
    ("n", (0,), 0.0, "n-zero"),
    ("n", (_INF,), "inf", "n-out-of-range"),
    ("negbinomdist", ("text", 5, 0.5), "nan", "negbinomdist-text"),
    ("negbinomdist", ("", 5, 0.5), "nan", "negbinomdist-blank"),
    ("negbinomdist", (2, 5, 0), "nan", "negbinomdist-zero-prob"),
    ("negbinomdist", (_INF, 5, 0.5), "nan", "negbinomdist-out-of-range"),
    ("networkdays", ("text", 46185), "nan", "networkdays-text"),
    ("networkdays", ("", 46185), "nan", "networkdays-blank"),
    ("networkdays", (0, 0), 0.0, "networkdays-zero"),
    ("networkdays", (1e20, 1e20), "nan", "networkdays-out-of-range"),
    ("networkdays_intl", ("text", 46185), "nan", "networkdays-intl-text"),
    ("networkdays_intl", ("", 46185), "nan", "networkdays-intl-blank"),
    ("networkdays_intl", (0, 0), 0.0, "networkdays-intl-zero"),
    ("networkdays_intl", (1e20, 1e20), "nan", "networkdays-intl-out-of-range"),
    ("nominal", ("text", 4), "nan", "nominal-text"),
    ("nominal", ("", 4), "nan", "nominal-blank"),
    ("nominal", (0.1, _INF), "nan", "nominal-out-of-range"),
    ("normdist", ("text", 0, 1), "nan", "normdist-text"),
    ("normdist", ("", 0, 1), "nan", "normdist-blank"),
    ("normdist", (0, 0, 0), "nan", "normdist-zero-stdev"),
    ("normdist", (0, 0, -1), "nan", "normdist-out-of-range"),
    ("norminv", ("text", 0, 1), "nan", "norminv-text"),
    ("norminv", ("", 0, 1), "nan", "norminv-blank"),
    ("norminv", (0, 0, 1), "nan", "norminv-zero"),
    ("norminv", (1, 0, 1), "nan", "norminv-out-of-range"),
    ("normsdist", ("text",), "nan", "normsdist-text"),
    ("normsdist", ("",), "nan", "normsdist-blank"),
    ("normsdist", (0,), 0.5, "normsdist-zero"),
    ("normsdist", (_INF,), 1.0, "normsdist-out-of-range"),
    ("normsinv", ("text",), "nan", "normsinv-text"),
    ("normsinv", ("",), "nan", "normsinv-blank"),
    ("normsinv", (0,), "nan", "normsinv-zero"),
    ("normsinv", (1,), "nan", "normsinv-out-of-range"),
    ("nper", ("text", -100, 1000), "nan", "nper-text"),
    ("nper", ("", -100, 1000), "nan", "nper-blank"),
    ("npv", ("", 100, 200), "nan", "npv-blank"),
    ("npv", (None, 100, 200), "nan", "npv-blank-none"),
    ("numbervalue", ("text",), "nan", "numbervalue-text"),
    ("numbervalue", ("",), 0.0, "numbervalue-blank"),
    ("numbervalue", (0,), 0.0, "numbervalue-zero"),
    ("numbervalue", ("1.2.3",), "nan", "numbervalue-out-of-range"),
    ("odd", ("",), "nan", "odd-blank"),
    ("odd", (0,), 1.0, "odd-zero"),
    ("odd", (_INF,), "nan", "odd-out-of-range"),
    ("oddfprice", ("text", 41000, 39900, 40100, 0.05, 0.06, 100, 2), "nan", "oddfprice-text"),
    ("oddfprice", ("", 41000, 39900, 40100, 0.05, 0.06, 100, 2), "nan", "oddfprice-blank"),
    ("oddfyield", ("text", 41000, 39900, 40100, 0.05, 95, 100, 2), "nan", "oddfyield-text"),
    ("oddfyield", ("", 41000, 39900, 40100, 0.05, 95, 100, 2), "nan", "oddfyield-blank"),
    # price + redemption == 0 used to raise ZeroDivisionError.
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
    ("permut", ("text", 2), "nan", "permut-text"),
    ("permut", ("", 2), "nan", "permut-blank"),
    ("permut", (0, 1), "nan", "permut-zero"),
    ("permut", (_INF, 2), "nan", "permut-out-of-range"),
    ("pmt", ("", 12, 1000), "nan", "pmt-blank"),
    # Zero rate is the closed form -pv/nper, not #DIV/0!.
    ("pmt", (0, 12, 1000), -1000 / 12, "pmt-zero-rate"),
    ("pmt", (-1, 12, 1000), 0.0, "pmt-out-of-range"),
    ("poisson", ("text", 1), "nan", "poisson-text"),
    ("poisson", ("", 1), "nan", "poisson-blank"),
    ("poisson", (0, 0), 1.0, "poisson-zero"),
    ("poisson", (_INF, 1), "nan", "poisson-out-of-range"),
    ("prob", ("text", [0.2, 0.3, 0.5], 1), "nan", "prob-text"),
    ("prob", ("", [0.2, 0.3, 0.5], 1), "nan", "prob-blank"),
    ("prob", ([1, 2], [0, 0], 1), "nan", "prob-zero"),
    ("prob", ([1, 2], [0.2, 0.2], 1), "nan", "prob-out-of-range"),
    ("pv", ("", 12, -100), "nan", "pv-blank"),
    ("pv", (0.01, 0, -100), 0.0, "pv-zero-nper"),
    ("pv", (-1, 12, -100), "nan", "pv-out-of-range"),
    ("quartile", ([1.0, 2.0, 3.0, 4.0], ""), "nan", "quartile-blank"),
    ("quartile", ([1.0, 2.0, 3.0, 4.0], None), "nan", "quartile-blank-none"),
    ("rank", ("text", [1, 2, 3]), "nan", "rank-text"),
    ("rank", ("", [1, 2, 3]), "nan", "rank-blank"),
    ("rank", (0, [0, 1, 2]), 3.0, "rank-zero"),
    ("rank", (9, [1, 2, 3]), "nan", "rank-out-of-range"),
    ("regex", ("abc", ""), "", "regex-blank-pattern"),
    ("rept", ("ab", "text"), "nan", "rept-text-count"),
    ("rept", ("ab", ""), "nan", "rept-blank-count"),
    ("rsq", ("", [1.0, 2.0, 3.0]), "nan", "rsq-blank"),
    ("rsq", (None, [1.0, 2.0, 3.0]), "nan", "rsq-blank-none"),
    ("rsq", ([0.0, 0.0, 0.0], [0.0, 0.0, 0.0]), "nan", "rsq-zero"),
    ("rsq", ([1.0, 2.0, 3.0], [1.0, 2.0]), "nan", "rsq-out-of-range"),
    ("sec", ("text",), "nan", "sec-text"),
    ("sec", ("",), "nan", "sec-blank"),
    ("sec", (0,), 1.0, "sec-zero"),
    ("sec", (_INF,), "nan", "sec-out-of-range"),
    ("sech", ("text",), "nan", "sech-text"),
    ("sech", ("",), "nan", "sech-blank"),
    ("sech", (0,), 1.0, "sech-zero"),
    ("sech", (_INF,), 0.0, "sech-out-of-range"),
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
    ("sqrtpi", ("text",), "nan", "sqrtpi-text"),
    ("sqrtpi", ("",), "nan", "sqrtpi-blank"),
    ("sqrtpi", (0,), 0.0, "sqrtpi-zero"),
    ("sqrtpi", (-1,), "nan", "sqrtpi-out-of-range"),
    ("standardize", ("text", 0, 1), "nan", "standardize-text"),
    ("standardize", ("", 0, 1), "nan", "standardize-blank"),
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


def test_regex_calc_replacement_syntax():
    # Bug Z13: regex replacement supports Calc $1, $&, $$ syntax
    assert calc.regex("abc-123", "([a-z]+)-([0-9]+)", "$2-$1") == "123-abc"
    assert calc.regex("abc", "b", "[$&]") == "a[b]c"
    assert calc.regex("abc", "b", "$$") == "a$c"


def test_sech_overflow():
    # Bug Z8: sech(1000) returns 0.0 on overflow instead of crashing
    assert calc.sech(1000) == 0.0
    assert calc.sech(-1000) == 0.0


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

    # sortby preserves mixed types
    res_sortby = calc.sortby([["a", 10], ["b", 9]], [10, 9])
    assert res_sortby == [["b", 9], ["a", 10]]


def test_rsq_slope_steyx_paired_clean():
    # Clean paired arrays mask out NaNs consistently across rsq, slope, steyx
    assert calc.rsq([2, 4, float("nan"), 8], [1, 2, 5, 4]) == 1.0
    assert calc.slope([2, 4, float("nan"), 8], [1, 2, 5, 4]) == 2.0
    assert calc.steyx([2, 4, float("nan"), 8], [1, 2, 5, 4]) == 0.0


def test_n_s_all_matches_public_functions():
    import plugin.scripting.venv.calc_functions_n_s as n_s

    public = {
        name
        for name, obj in inspect.getmembers(n_s, inspect.isfunction)
        if not name.startswith("_") and obj.__module__ == n_s.__name__
    }
    assert public == set(n_s.__all__)


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
    # Search returns the whole match. An optional group that did not
    # participate used to make group(1) None.
    assert calc.regex("b", "(a)?b") == "b"
    assert calc.regex("abc-123", "([a-z]+)-([0-9]+)") == "abc-123"
    assert calc.regex("ababa", "a", None, 2) == "a"
    assert calc.regex("a1b2", r"[a-z]\d", None, 2) == "b2"
    assert calc.regex("ababa", "a", "X", 2) == "abXba"
    assert calc.regex("abc", "z") == "#N/A"
    # Occurrence 0 is the original text, before the pattern is compiled.
    assert calc.regex("abc", "[", None, 0) == "abc"
    assert calc.regex("aba", "a", None, "g") == "a"
    assert math.isnan(calc.regex("abc", "b", None, "x"))
    assert math.isnan(calc.regex("abc", "b", None, -1))


def test_rept_length_cap_and_bad_count():
    assert len(calc.rept("a", 10**6)) == 10**6
    # kScInterpreterMaxStrLen is 2**31 // 8. Do not allocate the product.
    assert math.isnan(calc.rept("a", 10**9))
    assert math.isnan(calc.rept("ab", (2**31 // 8) // 2 + 1))
    assert math.isnan(calc.rept("a", "x"))
    assert math.isnan(calc.rept("ab", "text"))


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
    # isFreqInvalid is {1, 2, 4}. 1e7 used to walk range(1, int(years * f) + 1).
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
    assert math.isnan(calc.poisson(2, 2, "abc"))


def test_sort_flat_vector_index():
    assert math.isnan(calc.sort([3, 1, 2], 99))
    assert math.isnan(calc.sort([3, 1, 2], 2))
    assert calc.sort([3, 1, 2], 1.9) == [1, 2, 3]
    # by_col keys a row of a one-column vector: original order.
    assert calc.sort([3, 1, 2], 1, 1, True) == [3, 1, 2]
    assert calc.sort([3, 1, 2], 2, 1, True) == [3, 1, 2]
    assert math.isnan(calc.sort([3, 1, 2], 4, 1, True))
    assert calc.sort([], 99) == []


