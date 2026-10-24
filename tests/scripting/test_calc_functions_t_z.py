# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Bad-input tables for public calc helpers whose names start with T–Z.

Each row is an input that was not already locked by an existing test. The
expected value is whatever that helper returns today: NaN, or a spreadsheet
error token. Text, blank, zero, or out-of-range inputs that return a normal
value are omitted — those are not errors.

Skipped because the four kinds are already tested, or they do not produce an
error: ``t``, ``text``, ``textjoin``, ``type``, ``unicode``, ``unique``, ``xor``.
"""

from __future__ import annotations

import datetime as dt
import math

import numpy as np
import pytest

import plugin.scripting.calc_functions as calc

_INF = float("inf")

# (function, kind, args, "nan" or an error token)
_BAD_INPUTS: list[tuple[str, str, tuple, str]] = [
    # tdist: zero x is a real probability. Zero df, zero tails, and a tail
    # count outside 1 or 2 are #NUM! (NaN here).
    ("tdist", "text", ("text", 10, 2), "nan"),
    ("tdist", "blank", (None, 10, 2), "nan"),
    ("tdist", "blank-empty", ("", 10, 2), "nan"),
    ("tdist", "zero-df", (1.96, 0, 2), "nan"),
    ("tdist", "zero-tails", (1.96, 10, 0), "nan"),
    ("tdist", "out-of-range-x", (-1, 10, 2), "nan"),
    ("tdist", "out-of-range-tails", (1, 10, 3), "nan"),
    ("tdist", "out-of-range-inf-tails", (1, 10, _INF), "nan"),
    # textafter / textbefore: instance 0 and a text instance are already tested.
    ("textafter", "blank", ("", "-", 1), "nan"),
    ("textafter", "blank-delimiter", ("a-b", "", 1), "nan"),
    ("textafter", "blank-instance", ("a-b", "-", None), "nan"),
    ("textafter", "out-of-range-instance", ("a-b", "-", 99), "nan"),
    ("textafter", "out-of-range-inf-instance", ("a-b", "-", _INF), "nan"),
    ("textbefore", "blank", ("", "-", 1), "nan"),
    ("textbefore", "blank-delimiter", ("a-b", "", 1), "nan"),
    ("textbefore", "blank-instance", ("a-b", "-", None), "nan"),
    ("textbefore", "out-of-range-instance", ("a-b", "-", 99), "nan"),
    # textsplit: an empty delimiter and empty text are NaN, and a non-empty
    # text with a wild match_mode still splits.
    ("textsplit", "blank-delimiter", ("a-b", ""), "nan"),
    ("textsplit", "blank-text", ("", ","), "nan"),
    # time: text hour and a negative remainder are already tested. Zero is a
    # real time (0).
    ("time", "blank", (None, 0, 0), "nan"),
    ("time", "blank-empty", ("", 0, 0), "nan"),
    ("time", "out-of-range-inf", (_INF, 0, 0), "nan"),
    ("timevalue", "text", ("abc",), "nan"),
    ("timevalue", "blank", ("",), "nan"),
    ("timevalue", "blank-none", (None,), "nan"),
    ("timevalue", "zero", (0,), "nan"),
    ("timevalue", "out-of-range", ("25:00:00",), "nan"),
    ("tinv", "text", ("nope", 10), "nan"),
    ("tinv", "blank", (None, 10), "nan"),
    ("tinv", "blank-empty", ("", 10), "nan"),
    ("tinv", "zero-prob", (0, 10), "nan"),
    ("tinv", "zero-df", (0.05, 0), "nan"),
    ("tinv", "out-of-range-prob", (1.5, 10), "nan"),
    ("tinv", "out-of-range-df", (0.05, 0.5), "nan"),
    # trend: an empty known_y is already #VALUE!. A flat zero series fits.
    ("trend", "text", (["a", "b"],), "#VALUE!"),
    ("trend", "blank", ([None, None],), "#VALUE!"),
    ("trend", "blank-none", (None,), "#VALUE!"),
    ("trend", "out-of-range-shape", ([1.0, 2.0, 3.0], [1.0, 2.0]), "#VALUE!"),
    # trimmean: text cells, a blank cell, and a text percent are already NaN.
    # Percent 0 is the untrimmed mean.
    ("trimmean", "blank-empty", ([], 0.2), "nan"),
    ("trimmean", "out-of-range-percent", ([1.0, 2.0, 3.0, 4.0], 1), "nan"),
    ("trimmean", "out-of-range-negative", ([1.0, 2.0, 3.0, 4.0], -0.1), "nan"),
    ("ttest", "text", (["a", "b"], [1, 2, 3], 2, 2), "nan"),
    ("ttest", "blank", ([None, None], [1, 2, 3], 2, 2), "nan"),
    ("ttest", "zero-tails", ([1, 2, 3], [2, 3, 4], 0, 1), "nan"),
    ("ttest", "zero-type", ([1, 2, 3], [2, 3, 4], 2, 0), "nan"),
    ("ttest", "out-of-range-tails", ([1, 2, 3], [2, 3, 4], 3, 1), "nan"),
    ("ttest", "out-of-range-type", ([1, 2, 3], [2, 3, 4], 2, 4), "nan"),
    ("ttest", "out-of-range-inf-tails", ([1, 2, 3], [2, 3, 4], _INF, 1), "nan"),
    # unichar: a negative code point is already NaN.
    ("unichar", "text", ("A",), "nan"),
    ("unichar", "blank", (None,), "nan"),
    ("unichar", "blank-empty", ("",), "nan"),
    ("unichar", "zero", (0,), "nan"),
    ("unichar", "out-of-range", (0x110000,), "nan"),
    ("unichar", "out-of-range-inf", (_INF,), "nan"),
    # vara / varpa: text, blank, and zero are coerced to numbers (variance 0
    # when there are enough). Fewer than two samples is the error for VARA;
    # VARPA of one number is 0, and only the empty call is NaN.
    ("vara", "blank-empty", (), "nan"),
    ("vara", "out-of-range-count", (1,), "nan"),
    ("varpa", "blank-empty", (), "nan"),
    # weekday / weeknum: serial 0 is a real date. Return type 4 / 3 is already NaN.
    ("weekday", "text", ("nope",), "nan"),
    ("weekday", "blank", (None,), "nan"),
    ("weekday", "blank-empty", ("",), "nan"),
    ("weekday", "out-of-range-serial", (10**12,), "nan"),
    ("weekday", "out-of-range-negative", (-10**9,), "nan"),
    ("weekday", "out-of-range-inf", (_INF,), "nan"),
    ("weekday", "out-of-range-type", (44927, 0), "nan"),
    ("weekday", "text-type", (44927, "nope"), "nan"),
    ("weeknum", "text", ("nope",), "nan"),
    ("weeknum", "blank", (None,), "nan"),
    ("weeknum", "blank-empty", ("",), "nan"),
    ("weeknum", "out-of-range-serial", (10**12,), "nan"),
    ("weeknum", "out-of-range-negative", (-10**9,), "nan"),
    ("weeknum", "out-of-range-inf", (_INF,), "nan"),
    ("weeknum", "out-of-range-type", (44927, 0), "nan"),
    ("weeknum", "text-type", (44927, "x"), "nan"),
    # weibull: x of 0 is a real cdf. Zero or negative shape/scale is #NUM!.
    ("weibull", "text", ("x", 1, 1), "nan"),
    ("weibull", "blank", (None, 1, 1), "nan"),
    ("weibull", "blank-empty", ("", 1, 1), "nan"),
    ("weibull", "zero-alpha", (1, 0, 1), "nan"),
    ("weibull", "zero-beta", (1, 1, 0), "nan"),
    ("weibull", "out-of-range-x", (-1, 1, 1), "nan"),
    ("weibull", "out-of-range-alpha", (1, -1, 1), "nan"),
    # workday: a text day count is already NaN. Zero days and a negative
    # count are real dates. A holiday that does not parse is ignored.
    ("workday", "text-start", ("nope", 1), "nan"),
    ("workday", "blank-days", (46181, None), "nan"),
    ("workday", "blank-start", (None, 1), "nan"),
    ("workday", "blank-empty", ("", 1), "nan"),
    ("workday", "out-of-range-inf", (46181, _INF), "nan"),
    # workday_intl: text days, a blank weekend, and weekend codes 0/8/18
    # are already NaN.
    ("workday_intl", "text-start", ("nope", 1, 1), "nan"),
    ("workday_intl", "blank-days", (46181, None, 1), "nan"),
    ("workday_intl", "blank-start", (None, 1, 1), "nan"),
    ("workday_intl", "out-of-range-inf", (46181, _INF, 1), "nan"),
    # xirr: a zero guess still finds the root. The tiny-derivative case is
    # already NaN.
    ("xirr", "text", (["a", "b"], [1, 2]), "nan"),
    ("xirr", "blank", ([None, None], [1, 2]), "nan"),
    ("xirr", "blank-empty", ([], []), "nan"),
    ("xirr", "blank-none", (None, None), "nan"),
    ("xirr", "out-of-range-length", ([-100, 110], [0]), "nan"),
    ("xirr", "out-of-range-guess", ([-100, 110], [0, 365], -2), "nan"),
    ("xirr", "text-guess", ([-100, 110], [0, 365], "g"), "nan"),
    # xlookup: a return array shorter than the match used to raise.
    ("xlookup", "out-of-range-return", ("b", ["a", "b"], [1]), "#VALUE!"),
    ("xlookup", "out-of-range-rows", ("c", ["a", "b", "c"], [["x"], ["y"]]), "#VALUE!"),
    ("xlookup", "out-of-range-column", ("b", [["a"], ["b"]], [[10]]), "#VALUE!"),
    # xmatch: a blank that occurs in the lookup vector is a hit, not an error.
    ("xmatch", "text", ("z", [1, 2, 3]), "nan"),
    ("xmatch", "zero", (0, [1, 2, 3]), "nan"),
    ("xmatch", "blank-empty", ("a", []), "nan"),
    ("xmatch", "out-of-range-mode", (1, [1, 2], 9), "nan"),
    # xnpv: rate <= -1 is already NaN. Rate 0 is a real present value.
    ("xnpv", "text", ("rate", [-100, 110], [0, 365]), "nan"),
    ("xnpv", "blank", (None, [-100, 110], [0, 365]), "nan"),
    ("xnpv", "blank-empty-rate", ("", [-100, 110], [0, 365]), "nan"),
    ("xnpv", "blank-values", (0.1, [], []), "nan"),
    ("xnpv", "text-values", (0.1, ["a", "b"], [0, 365]), "nan"),
    ("xnpv", "out-of-range-length", (0.1, [-100, 110], [0]), "nan"),
    # yearfrac: basis 0–4 on real dates is already tested. Equal serial 0 is 0.
    ("yearfrac", "text", ("nope", 45292), "nan"),
    ("yearfrac", "blank", (None, 45292), "nan"),
    ("yearfrac", "blank-empty", ("", 45292), "nan"),
    ("yearfrac", "out-of-range-basis", (44927, 45292, 5), "nan"),
    ("yearfrac", "out-of-range-negative-basis", (44927, 45292, -1), "nan"),
    ("yearfrac", "text-basis", (44927, 45292, "b"), "nan"),
    ("yearfrac", "out-of-range-inf", (_INF, 45292, 1), "nan"),
    # The YIELD* helpers are stubs: every input, including these, is NaN.
    ("yield_calc", "text", ("a", "b", "c", "d", "e", "f"), "nan"),
    ("yield_calc", "blank", (None, None, None, None, None, None), "nan"),
    ("yield_calc", "zero", (0, 0, 0, 0, 0, 0), "nan"),
    ("yield_calc", "out-of-range", (-1, -2, -3, 0, 0, 0), "nan"),
    ("yielddisc", "text", ("a", "b", "c", "d"), "nan"),
    ("yielddisc", "blank", (None, None, None, None), "nan"),
    ("yielddisc", "zero", (0, 0, 0, 0), "nan"),
    ("yielddisc", "out-of-range", (-1, -2, 0, 0), "nan"),
    ("yieldmat", "text", ("a", "b", "c", "d", "e"), "nan"),
    ("yieldmat", "blank", (None, None, None, None, None), "nan"),
    ("yieldmat", "zero", (0, 0, 0, 0, 0), "nan"),
    ("yieldmat", "out-of-range", (-1, -2, -3, 0, 0), "nan"),
    # ztest: a negative sigma is a real scale. Zero sigma and zero variance
    # are #DIV/0! (NaN here).
    ("ztest", "text", (["a", "b"], 1), "nan"),
    ("ztest", "blank", ([None, None], 1), "nan"),
    ("ztest", "blank-empty", ([], 1), "nan"),
    ("ztest", "blank-none", (None, 1), "nan"),
    ("ztest", "zero-sigma", ([1, 2, 3], 2, 0), "nan"),
    ("ztest", "zero-variance", ([5, 5, 5], 5), "nan"),
    ("ztest", "text-x", ([1, 2, 3], "x"), "nan"),
    ("ztest", "blank-x", ([1, 2, 3], None), "nan"),
    ("ztest", "text-sigma", ([1, 2, 3], 2, "s"), "nan"),
]


def _case_id(case: tuple[str, str, tuple, str]) -> str:
    name, kind, _unused_args, _unused_expected = case
    return f"{name}-{kind}"


@pytest.mark.parametrize(
    ("name", "kind", "args", "expected"),
    _BAD_INPUTS,
    ids=[_case_id(case) for case in _BAD_INPUTS],
)
def test_t_z_bad_input(name: str, kind: str, args: tuple, expected: str) -> None:
    """Text, blank, zero, and out-of-range inputs return the helper's error."""
    result = getattr(calc, name)(*args)
    if expected == "nan":
        assert isinstance(result, float) and math.isnan(result), (name, kind, result)
    else:
        assert result == expected, (name, kind, result)


def test_xmatch_bad_mode_raises():
    # Bad match_mode now raises instead of being caught by _find_match_index;
    # worker maps ValueError -> #VALUE! and OverflowError -> #NUM!.
    with pytest.raises(ValueError):
        calc.xmatch(1, [1], "bad")
    with pytest.raises(OverflowError):
        calc.xmatch(1, [1], _INF)


def test_yearfrac_reversed_dates():
    # Regression R1: yearfrac swaps start and end dates if start > end
    fwd = calc.yearfrac(44000, 45000, 1)
    rev = calc.yearfrac(45000, 44000, 1)
    assert fwd > 0.0
    assert abs(fwd - rev) < 1e-9


def test_trimmean_skips_blanks_and_text():
    # Regression R2: trimmean skips blanks, text, and bools per Excel spec
    assert calc.trimmean([1, 2, None, 4, 5], 0.2) == 3.0
    assert calc.trimmean([1, 2, "text", 4, 5], 0.2) == 3.0
    assert calc.trimmean([1, 2, True, 4, 5], 0.2) == 3.0


def test_text_extended_formats():
    # Extended formats for text()
    assert calc.text(1234.5, "0.0") == "1234.5"
    assert calc.text(1234.567, "0.000") == "1234.567"
    assert calc.text(1234.5, "#,##0.00") == "1,234.50"
    assert calc.text(0.125, "0%") == "13%"
    assert calc.text(0.125, "0.0%") == "12.5%"
    assert calc.text(45000, "yyyy-mm-dd") == "2023-03-15"
    assert calc.text(45000, "MMMM") == "March"
    assert calc.text(45000, "MMM") == "Mar"


def test_textsplit_preserves_case():
    # Bug Z10: textsplit with match_mode=1 preserves casing of original text
    res = calc.textsplit("Hello World", "world", match_mode=1)
    assert res == [["Hello ", ""]]


def test_type_numpy_bool_and_calc_errors():
    # Bug Z7: type recognizes np.bool_ as 4.0 and calc errors as 16.0
    assert calc.type(np.bool_(True)) == 4.0
    assert calc.type(True) == 4.0
    assert calc.type(10) == 1.0
    assert calc.type(np.int64(10)) == 1.0
    assert calc.type("#VALUE!") == 16.0
    assert calc.type(float("nan")) == 16.0


def test_unique_preserves_mixed_types():
    # Bug Z2: unique does not stringify numbers (1 and '1' stay distinct)
    assert calc.unique([1, "1", 2]) == [1, "1", 2]


def test_yield_helpers():
    # yielddisc and yieldmat calculations; yield_calc not implemented
    yd = calc.yielddisc(45000, 45365, 95, 100, 3)
    assert not math.isnan(yd)
    assert yd > 0.0

    ym = calc.yieldmat(45000, 46000, 44000, 0.05, 98, 0)
    assert not math.isnan(ym)
    assert ym > 0.0

    assert math.isnan(calc.yield_calc(45000, 46000, 0.05, 98, 100, 2))


def test_trend_const_and_multivariate():
    # Bug Z12: trend const=False and multivariate input
    y = [2, 4, 6, 8]
    x = [1, 2, 3, 4]
    pred = calc.trend(y, x, [5, 6])
    assert np.allclose(pred, [10.0, 12.0], atol=1e-6)


def _serial(year: int, month: int, day: int) -> int:
    return dt.date(year, month, day).toordinal() - 693594


def test_text_percent_scales_in_decimal():
    # 0.285 * 100 is 28.499999999999996, which rounded to 28%.
    assert calc.text(0.285, "0%") == "29%"
    assert calc.text(0.145, "0%") == "15%"
    assert calc.text(0.125, "0%") == "13%"
    assert calc.text(0.125, "0.0%") == "12.5%"
    assert calc.text(float("inf"), "0%") == "inf"


def test_yearfrac_basis1_is_actual_actual():
    # GetYearFrac basis 1. A flat /365.25 made a full 2023 about 0.99932.
    assert calc.yearfrac(_serial(2023, 1, 1), _serial(2024, 1, 1), 1) == 1.0
    assert calc.yearfrac(_serial(2024, 1, 1), _serial(2023, 1, 1), 1) == 1.0
    assert calc.yearfrac(_serial(2024, 1, 1), _serial(2025, 1, 1), 1) == 1.0
    # Same leap year uses 366 even when the slice misses 29 February.
    assert calc.yearfrac(_serial(2024, 3, 1), _serial(2024, 4, 1), 1) == 31 / 366
    assert calc.yearfrac(_serial(2020, 1, 1), _serial(2022, 1, 1), 1) == 731 / (1096 / 3)
    assert calc.yearfrac(_serial(2022, 1, 1), _serial(2020, 1, 1), 1) == 731 / (1096 / 3)
    assert calc.yearfrac(_serial(2023, 1, 1), _serial(2024, 6, 1), 1) == 517 / (731 / 2)


def test_yield_rejects_reversed_dates():
    assert math.isnan(calc.yielddisc(45365, 45000, 95, 100))
    assert math.isnan(calc.yieldmat(45000, 46000, 45100, 0.05, 98, 0))
    # Issue equal to settlement is allowed.
    same_issue = calc.yieldmat(45000, 46000, 45000, 0.05, 98, 0)
    assert isinstance(same_issue, float) and math.isfinite(same_issue)


def test_xlookup_default_miss_is_not_available():
    assert calc.xlookup("z", ["a"], [1]) == "#N/A"
    assert calc.xlookup("z", ["a"], [1], None) is None
    assert calc.xlookup("z", ["a"], [1], "missing") == "missing"


def test_textjoin_whole_numbers_drop_decimal():
    assert calc.textjoin(",", True, [1.0, 2.0, 3.0]) == "1,2,3"
    assert calc.textjoin(",", True, [1.5, 2.0]) == "1.5,2"
    assert calc.textjoin(",", True, [True, 1.0]) == "True,1"


def test_unique_casefold_bool_and_nan():
    assert calc.unique(["A", "a"]) == ["A"]
    assert calc.unique(["A", "a"], unique_only=True) == []
    assert calc.unique([True, 1, 1.0]) == [True, 1]
    assert calc.unique([1, "1", 2]) == [1, "1", 2]
    assert calc.unique([["A"], ["a"]]) == [["A"]]
    got = calc.unique([float("nan"), float("nan"), 1.0])
    assert len(got) == 2
    assert isinstance(got[0], float) and math.isnan(got[0])
    assert got[1] == 1.0


def test_tinv_weibull_ztest_overflow():
    # float(10**400) raises OverflowError inside the helper. Evaluating it
    # here would raise before the call.
    huge = 10**400
    assert math.isnan(calc.tinv(huge, 10))
    assert math.isnan(calc.weibull(huge, 1, 1))
    assert math.isnan(calc.ztest([1, 2, 3], 2, float("inf")))


def test_xirr_rejects_rate_at_or_below_minus_one():
    assert math.isnan(calc.xirr([-100, 110], [0, 365], -1))
    rate = calc.xirr([-10000, 2750, 4250, 3250, 2750], [44197, 44562, 44927, 45292, 45658])
    assert isinstance(rate, float) and math.isfinite(rate)


def test_weeknum_december_54_is_week_1():
    # 2012-12-31 with a Monday week start is week 54 before the clamp.
    # Date::GetWeekOfYear sets 54 to 1. Return type 2 is that Monday start.
    assert calc.weeknum(_serial(2012, 12, 31), 2) == 1.0


def test_textsplit_non_one_match_mode_stays_sensitive():
    assert math.isnan(calc.textsplit("a-b", ""))
    assert calc.textsplit("Hello World", "world", match_mode=2) == [["Hello World"]]


def test_xirr_stop_is_scale_invariant():
    # abs(NPV) < 1e-7 returned the 0.1 guess: NPV at 0.1 is about 9e-8.
    # getXirr's absolute 1e-10 NPV stop cannot pin a 1e-6 cash flow to
    # 1e-6 relative. It must still land near 0.2, not on the guess.
    small = calc.xirr([-1e-6, 1.2e-6], [0, 365])
    large = calc.xirr([-1e10, 1.2e10], [0, 365])
    assert math.isclose(small, 0.2, abs_tol=1e-3)
    assert abs(small - 0.1) > 0.05
    assert math.isclose(large, 0.2, rel_tol=1e-6)
    classic = calc.xirr([-10000, 2750, 4250, 3250, 2750], [44197, 44562, 44927, 45292, 45658])
    assert math.isclose(classic, 0.1153874812592906, rel_tol=1e-6)
    assert math.isnan(calc.xirr([-1, 1e-20], [0, 365], 0.1))
    assert math.isnan(calc.xirr([-100, 110], [0, 365], -1))


def test_ztest_skips_blanks_and_propagates_errors():
    # ScZTest skips blanks and text, counts TRUE as 1, and errors the call
    # when a cell is an error or fewer than two numbers remain.
    pair = calc.ztest([1, 3], 2)
    assert math.isclose(calc.ztest([1, None, 3], 2), pair)
    assert math.isclose(calc.ztest([1, "x", 3], 2), pair)
    assert not math.isclose(calc.ztest([1, True, 3], 2), pair)
    assert math.isnan(calc.ztest([1, float("nan"), 3], 2))
    assert math.isnan(calc.ztest([1, "#VALUE!", 3], 2))
    assert math.isnan(calc.ztest([1], 0, 1))
    assert math.isnan(calc.ztest([1, 2, 3], 2, float("inf")))


def test_tdist_tinv_floor_degrees_of_freedom():
    assert math.isclose(calc.tdist(1, 2.7, 1), calc.tdist(1, 2, 1))
    assert math.isclose(calc.tinv(0.05, 2.7), calc.tinv(0.05, 2))
    assert math.isfinite(calc.tdist(1, 2.7, 1))
    assert math.isfinite(calc.tinv(0.05, 2.7))


def test_trend_false_string_drops_intercept():
    y = [3, 5, 7]
    x = [1, 2, 3]
    forced = calc.trend(y, x, [4], False)
    assert np.allclose(calc.trend(y, x, [4], "FALSE"), forced)
    assert not np.allclose(calc.trend(y, x, [4]), forced)
    assert calc.trend([]) == "#VALUE!"


def test_xnpv_bad_value_is_nan():
    assert math.isnan(calc.xnpv(0.1, ["a", 1], [0, 365]))
    assert math.isclose(calc.xnpv(0.1, [-100, 110], [0, 365]), 0.0, abs_tol=1e-9)


def test_text_large_integer_is_not_scientific():
    assert calc.text(1e30, "0") == "1" + "0" * 30
    assert calc.text(0.285, "0%") == "29%"


def test_xlookup_one_d_returns_python_int():
    found = calc.xlookup(20, [10, 20], [100, 200])
    assert isinstance(found, int)
    assert not isinstance(found, np.generic)
    assert found == 200
    assert calc.xlookup("z", ["a"], [1]) == "#N/A"


def test_xlookup_mixed_return_keeps_numbers():
    # asarray without dtype turned 5 into the string "5".
    found = calc.xlookup(5, ["x", 5], ["x", 5])
    assert isinstance(found, int)
    assert found == 5
    row = calc.xlookup("b", ["a", "b"], [["a", 1], ["b", 2]])
    assert row == ["b", 2]
    assert isinstance(row[1], int)


def test_tinv_tiny_probability_stays_finite():
    # ppf(1 - p/2) rounds to 1 and returns inf once p is about 1e-16.
    tiny = calc.tinv(1e-16, 10)
    assert math.isfinite(tiny)
    assert math.isclose(tiny, 109.38147004761092, rel_tol=1e-6)
    assert math.isclose(calc.tinv(0.05, 10), 2.2281388519649385, rel_tol=1e-9)
    assert math.isclose(calc.tinv(1, 10), 0.0, abs_tol=1e-9)


def test_weeknum_year_9999_does_not_raise():
    # date(10000, 1, 1) used to escape weeknum's try.
    serial = _serial(9999, 12, 31)
    assert calc.weeknum(serial, 1) == 1.0
    assert calc.weeknum(serial, 16) == 53.0


def test_textbefore_negative_match_end_is_empty():
    # The virtual delimiter for a negative instance sits at offset 0.
    assert calc.textbefore("a-b-c", "-", -3, match_end=1) == ""
    assert calc.textbefore("a-b-c", "-", 3, match_end=1) == "a-b-c"
    # lower() of İ changed the length, so the cut included the delimiter.
    assert calc.textbefore("aİb", "b", match_mode=1) == "aİ"
    # rfind counted an overlapping second "aa".
    assert calc.textbefore("aaa", "aa", -1) == ""


def test_trimmean_huge_percent_is_nan():
    assert math.isnan(calc.trimmean([1, 2, 3, 4], 10**400))


def test_flag_strings_follow_getdouble():
    # "0" is false. "FALSE" is not a boolean on these functions.
    assert calc.textjoin(",", "0", ["a", "", "b"]) == "a,,b"
    assert calc.textjoin(",", "FALSE", ["a", ""]) == "#VALUE!"
    assert calc.textjoin(",", False, ["a", "", "b"]) == "a,,b"
    assert calc.unique([[1, 2], [3, 4]], "0") == [[1, 2], [3, 4]]
    assert calc.unique([[1, 2]], "FALSE") == "#VALUE!"
    assert math.isclose(calc.weibull(1, 1, 1, "0"), calc.weibull(1, 1, 1, False))
    assert math.isnan(calc.weibull(1, 1, 1, "FALSE"))
    assert calc.textsplit("a,,b", ",", ignore_empty="0") == [["a", "", "b"]]
    assert math.isnan(calc.textsplit("a-b", "-", ignore_empty="FALSE"))


def test_trend_scalar_is_one_by_one():
    # A scalar new_x is 0-d. shape[0] raised IndexError.
    y = [2, 4, 6, 8]
    x = [1, 2, 3, 4]
    pred = calc.trend(y, x, 11)
    assert np.allclose(pred, [22.0], atol=1e-6)
    assert np.allclose(pred, calc.trend(y, x, [11]), atol=1e-6)
    assert calc.trend(y, 11, [4, 5]) == "#VALUE!"
    assert np.allclose(calc.trend(6, 2, 4, False), [12.0], atol=1e-6)


def test_match_end_zero_string_is_false():
    # GetBool of "0" is false. "FALSE" is not a number.
    assert math.isnan(calc.textbefore("a-b-c", "-", 3, match_end="0"))
    assert calc.textbefore("a-b-c", "-", 3, match_end="1") == "a-b-c"
    assert math.isnan(calc.textbefore("a-b-c", "-", 3, match_end="FALSE"))
    assert math.isnan(calc.textafter("a-b-c", "-", -3, match_end="0"))
    assert calc.textafter("a-b-c", "-", -3, match_end="1") == "a-b-c"
    assert math.isnan(calc.textafter("a-b-c", "-", -3, match_end="FALSE"))


def test_xirr_and_xnpv_truncate_dates():
    # sal_Int32 drops the fraction. 365.9 is the same year as 365.
    whole = calc.xnpv(0.1, [-100, 110], [0, 365])
    frac = calc.xnpv(0.1, [-100, 110], [0.9, 365.9])
    assert math.isclose(frac, whole, abs_tol=1e-9)
    dates = [44197, 44562, 44927, 45292, 45658]
    flows = [-10000, 2750, 4250, 3250, 2750]
    assert math.isclose(
        calc.xirr(flows, [d + 0.9 for d in dates]),
        calc.xirr(flows, dates),
        rel_tol=1e-9,
    )
    # getXirr does not reject a date earlier than the first.
    early = calc.xnpv(0.1, [-100, 110], [10, 0])
    assert isinstance(early, float) and math.isfinite(early)


def test_text_unknown_format_is_nan():
    assert math.isnan(calc.text(1234.5, "$#,##0.00"))
    assert math.isnan(calc.text(1234.5, "0.00E+00"))
    assert math.isnan(calc.text(1234.5, "yyyy-mm-dd hh:mm"))
    assert calc.text(1234.5, "") == "1234.5"
    assert calc.text("abc", "0") == "abc"
    assert calc.text(1234.5, "0.00") == "1234.50"


def test_textjoin_propagates_errors_and_flattens_ranges():
    assert calc.textjoin(",", True, ["a", "#VALUE!", "b"]) == "#VALUE!"
    assert calc.textjoin(",", False, ["a", "#VALUE!"]) == "#VALUE!"
    assert calc.textjoin(",", True, ["a", "#DIV/0!", "b"]) == "#DIV/0!"
    assert math.isnan(calc.textjoin(",", True, ["a", float("nan"), "b"]))
    assert calc.textjoin(",", True, ["a", None, "b"]) == "a,b"
    assert calc.textjoin(",", False, ["a", None, "b"]) == "a,,b"
    assert calc.textjoin(",", True, ["a", "  ", "b"]) == "a,b"
    assert calc.textjoin(",", True, [[1, 2], [3]]) == "1,2,3"
    assert calc.textjoin(",", True, [[1.0, 2.0], [3.0, 4.0]]) == "1,2,3,4"
    assert calc.textjoin(",", True, "ab") == "ab"
    assert calc.textjoin(",", True, [True, 1.0]) == "True,1"


def test_textsplit_empty_text_is_nan_and_pad_is_nan():
    assert math.isnan(calc.textsplit("", ","))
    assert math.isnan(calc.textsplit("", ",", ignore_empty=True))
    padded = calc.textsplit("a,b;c", ",", ";")
    assert padded[0] == ["a", "b"]
    assert padded[1][0] == "c"
    assert math.isnan(padded[1][1])
    assert calc.textsplit("a,b;c", ",", ";", pad_with="x") == [["a", "b"], ["c", "x"]]

