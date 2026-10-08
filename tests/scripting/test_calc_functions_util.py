# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for plugin.scripting.venv.calc_functions_util."""

from __future__ import annotations

from decimal import Decimal
import datetime as dt
import math
import numpy as np
import pytest

from plugin.scripting.venv.calc_functions_util import (
    _bessel_iv_jv,
    _bessel_kn_yn,
    _build_holiday_set,
    _collect_a_values,
    _criteria_numbers,
    _dollar_fraction_terms,
    _extract_numeric_array,
    _find_match_index,
    _find_text_cut,
    _fractional_dollar_digits,
    _int_bitwise,
    _int_shift,
    _npf_result,
    _parse_weekend,
    _serial_to_date,
    _simple_accrual,
    _to_float_a,
    _wildcard_fullmatch,
    match_criteria,
)



def test_npf_result_valid_and_invalid():
    # PMT(0.05/12, 60, 10000) approx -188.71
    res = _npf_result("pmt", 0.05 / 12, 60, 10000, 0, 0)
    assert abs(res - (-188.712336)) < 1e-2

    # Invalid input should return NaN, not raise exception
    assert math.isnan(_npf_result("pmt", "invalid", 60, 10000, 0, 0))
    assert math.isnan(_npf_result("unknown_func", 1, 2, 3))


def test_wildcard_fullmatch():
    assert _wildcard_fullmatch("apple", "apple") is True
    assert _wildcard_fullmatch("app*", "apple") is True
    assert _wildcard_fullmatch("*ple", "apple") is True
    assert _wildcard_fullmatch("a??le", "apple") is True
    assert _wildcard_fullmatch("a?le", "apple") is False
    assert _wildcard_fullmatch("*banana*", "green banana fruit") is True
    assert _wildcard_fullmatch("*banana*", "apple") is False


def test_match_criteria_comparisons():
    # Numeric comparisons
    assert match_criteria(10, ">5") is True
    assert match_criteria(5, ">5") is False
    assert match_criteria(5, ">=5") is True
    assert match_criteria(4, "<=5") is True
    assert match_criteria(5, "<>5") is False
    assert match_criteria(6, "<>5") is True
    assert match_criteria(5, "=5") is True
    assert match_criteria(5, "5") is True

    # Numeric criterion vs text cell: Excel treats text as not matching numbers
    assert match_criteria("abc", ">5") is False
    assert match_criteria("abc", "<=5") is False
    assert match_criteria("abc", "<>5") is True

    # String comparison
    assert match_criteria("apple", "apple") is True
    assert match_criteria("apple", "=apple") is True
    assert match_criteria("orange", "<>apple") is True

    # Huge ints are not floats. They follow the text-cell rule instead of raising.
    huge = 10**400
    assert match_criteria(huge, ">5") is False
    assert match_criteria(huge, "<>5") is True
    assert match_criteria(huge, huge) is True
    # float("1" + 400 zeros) is inf, not OverflowError, so this is 5 > inf.
    assert match_criteria(5, ">" + str(huge)) is False


def test_find_match_index():
    arr = ["apple", "banana", "cherry", "date"]

    # Exact match
    assert _find_match_index("cherry", arr, match_mode=0) == 2
    assert _find_match_index("pear", arr, match_mode=0) is None

    # Search mode -1 (last to first)
    arr_dup = ["a", "b", "c", "b", "d"]
    assert _find_match_index("b", arr_dup, match_mode=0, search_mode=1) == 1
    assert _find_match_index("b", arr_dup, match_mode=0, search_mode=-1) == 3

    # Numeric next smaller / larger
    num_arr = [10, 20, 30, 40]
    assert _find_match_index(25, num_arr, match_mode=-1) == 1  # 20
    assert _find_match_index(25, num_arr, match_mode=1) == 2   # 30

    # Wildcard
    assert _find_match_index("c*", arr, match_mode=2) == 2     # cherry
    assert _find_match_index("?ate", arr, match_mode=2) == 3   # date


def test_extract_numeric_array():
    data = [1.0, "text", True, False, 2.5, None, float("nan"), 3.0]

    # Default ignores text and bool, keeps finite and nan
    arr = _extract_numeric_array(data)
    assert len(arr) == 4
    assert np.allclose(arr[:2], [1.0, 2.5])
    assert math.isnan(arr[2])
    assert arr[3] == 3.0

    # Ignore NaN
    arr_no_nan = _extract_numeric_array(data, propagate_nan=False)
    assert np.allclose(arr_no_nan, [1.0, 2.5, 3.0])

    # Allow booleans
    arr_with_bool = _extract_numeric_array([10.0, True, False], ignore_bool=False)
    assert np.allclose(arr_with_bool, [10.0, 1.0, 0.0])


def test_to_float_a():
    from plugin.scripting.venv.calc_functions_a_c import _to_float_a as _to_float_a_ac

    # Ensure re-export from a_c matches util
    assert _to_float_a is _to_float_a_ac

    assert _to_float_a(42) == 42.0
    assert _to_float_a(3.14) == 3.14
    assert _to_float_a(True) == 1.0
    assert _to_float_a(False) == 0.0
    assert _to_float_a(np.bool_(True)) == 1.0
    assert _to_float_a(np.bool_(False)) == 0.0
    assert _to_float_a(None) == 0.0
    assert _to_float_a("") == 0.0
    assert _to_float_a("invalid") == 0.0
    assert _to_float_a("#VALUE!") == 0.0
    # int past the float range used to raise OverflowError out of the helper.
    assert _to_float_a(10**400) == 0.0
    assert _to_float_a(-(10**400)) == 0.0


def test_collect_a_values():
    # None is skipped (blank cell), while "" and "text" evaluate to 0.0
    res = _collect_a_values([1.0, 2.0], True, False, "text", "", None, [3.0, np.bool_(True)])
    assert isinstance(res, np.ndarray)
    assert np.allclose(res, [1.0, 2.0, 1.0, 0.0, 0.0, 0.0, 3.0, 1.0])
    empty_res = _collect_a_values()
    assert len(empty_res) == 0


def test_serial_to_date():
    # 46181 corresponds to 2026-06-08 (46181 + 693594 = 739775)
    assert _serial_to_date(46181) == dt.date(2026, 6, 8)
    assert _serial_to_date("46181") == dt.date(2026, 6, 8)
    assert _serial_to_date(46181.75) == dt.date(2026, 6, 8)

    # Invalid values should return None without raising
    assert _serial_to_date("invalid") is None
    assert _serial_to_date(None) is None
    assert _serial_to_date(float("nan")) is None
    assert _serial_to_date(float("inf")) is None
    assert _serial_to_date(100_000_000) is None  # Exceeds max ordinal


def test_build_holiday_set():
    assert _build_holiday_set(None) == set()
    assert _build_holiday_set([]) == set()

    # Scalar and list of serials
    h1 = _build_holiday_set(46181)
    assert h1 == {dt.date(2026, 6, 8)}

    h2 = _build_holiday_set([46181, "46182", "", None, "invalid", 46181])
    assert h2 == {dt.date(2026, 6, 8), dt.date(2026, 6, 9)}


def test_parse_weekend():

    # Standard numeric weekend codes
    assert _parse_weekend(1) == {5, 6}   # Sat, Sun
    assert _parse_weekend(2) == {6, 0}   # Sun, Mon
    assert _parse_weekend(11) == {6}     # Sun only
    assert _parse_weekend(17) == {5}     # Sat only

    # String masks (7 chars: 1 = non-working, 0 = working)
    assert _parse_weekend("0000011") == {5, 6}
    assert _parse_weekend("1000000") == {0}
    assert _parse_weekend("0000000") == set()

    # Invalid numeric codes must return NaN
    assert math.isnan(_parse_weekend(0))
    assert math.isnan(_parse_weekend(8))
    assert math.isnan(_parse_weekend(18))

    # Invalid non-numeric input returns NaN
    assert math.isnan(_parse_weekend(None))


def test_find_text_cut():
    import pytest

    # Positive instance, before
    s, delim, inst, idx, at_end = _find_text_cut("a-b-c", "-", 1, after=False)
    assert (s, delim, inst, idx, at_end) == ("a-b-c", "-", 1, 1, False)

    # Positive instance, after
    s, delim, inst, idx, at_end = _find_text_cut("a-b-c", "-", 1, after=True)
    assert (s, delim, inst, idx, at_end) == ("a-b-c", "-", 1, 2, False)

    # Second instance, before and after
    s, delim, inst, idx, at_end = _find_text_cut("a-b-c", "-", 2, after=False)
    assert idx == 3
    s, delim, inst, idx, at_end = _find_text_cut("a-b-c", "-", 2, after=True)
    assert idx == 4

    # Negative instance
    s, delim, inst, idx, at_end = _find_text_cut("a-b-c", "-", -1, after=False)
    assert (idx, at_end) == (3, False)
    s, delim, inst, idx, at_end = _find_text_cut("a-b-c", "-", -1, after=True)
    assert (idx, at_end) == (3, False)

    # Match mode 1 (case insensitive)
    s, delim, inst, idx, at_end = _find_text_cut("FooBARbaz", "bar", 1, match_mode=1, after=False)
    assert idx == 3
    s, delim, inst, idx, at_end = _find_text_cut("FooBARbaz", "bar", 1, match_mode=1, after=True)
    assert idx == 6

    # Match end miss (instance lands on boundary)
    s, delim, inst, idx, at_end = _find_text_cut("a-b-c", "-", 3)
    assert idx is None and at_end is True

    # Real miss (beyond boundary)
    s, delim, inst, idx, at_end = _find_text_cut("a-b-c", "-", 4)
    assert idx is None and at_end is False

    # Instance 0 raises ValueError
    with pytest.raises(ValueError):
        _find_text_cut("a-b-c", "-", 0)

    # Non-numeric instance raises ValueError
    with pytest.raises(ValueError):
        _find_text_cut("a-b-c", "-", "invalid")


def test_int_bitwise():
    import operator

    assert _int_bitwise(operator.and_, 5, 3) == 1.0
    assert _int_bitwise(operator.or_, 5, 3) == 7.0
    assert _int_bitwise(operator.xor, 5, 3) == 6.0
    assert _int_bitwise(operator.and_, "5", "3") == 1.0

    # Errors return NaN
    assert math.isnan(_int_bitwise(operator.and_, "invalid", 1))
    assert math.isnan(_int_bitwise(operator.and_, None, 1))
    assert math.isnan(_int_bitwise(operator.and_, float("inf"), 1))


def test_int_shift():
    assert _int_shift(5, 2, left=True) == 20.0
    assert _int_shift(20, 2, left=False) == 5.0
    assert _int_shift(10, 0, left=True) == 10.0
    assert _int_shift(10, 0, left=False) == 10.0

    # Negative shift swaps direction (Calc / ODFF semantics)
    assert _int_shift(8, -1, left=True) == 4.0
    assert _int_shift(8, -1, left=False) == 16.0

    # Invalid / inf inputs return NaN
    assert math.isnan(_int_shift("invalid", 1, left=True))
    assert math.isnan(_int_shift(1, float("inf"), left=True))
    assert math.isnan(_int_shift(float("inf"), 1, left=False))


def test_bessel_iv_jv():
    def dummy_fn(v, z):
        return v * 10.0 + z

    assert _bessel_iv_jv(dummy_fn, 2.5, 3) == 32.5
    # n < 0 returns NaN
    assert math.isnan(_bessel_iv_jv(dummy_fn, 2.5, -1))
    # Errors return NaN
    assert math.isnan(_bessel_iv_jv(dummy_fn, "invalid", 1))


def test_bessel_kn_yn():
    def dummy_fn(v, z):
        return v * 10.0 + z

    assert _bessel_kn_yn(dummy_fn, 2.5, 3) == 32.5
    # x <= 0 returns NaN
    assert math.isnan(_bessel_kn_yn(dummy_fn, 0.0, 1))
    assert math.isnan(_bessel_kn_yn(dummy_fn, -2.5, 1))
    # Errors return NaN
    assert math.isnan(_bessel_kn_yn(dummy_fn, "invalid", 1))


def test_simple_accrual():
    # 43831 = 2020-01-01, 43891 = 2020-03-01
    res = _simple_accrual(43831, 43891, 0.05, 1000)
    assert not math.isnan(res)
    assert res > 0.0

    # Invalid values return NaN
    assert math.isnan(_simple_accrual("invalid", 43891, 0.05, 1000))
    assert math.isnan(_simple_accrual(43831, 43891, "invalid", 1000))


def test_criteria_numbers():
    # Same range for criteria and values
    assert _criteria_numbers([10, 20, 30, 40], ">20") == [30.0, 40.0]

    # Separate value range with text and NaN filtering
    cond = ["yes", "no", "yes", "yes"]
    vals = [10.0, 20.0, "bad", 30.0]
    assert _criteria_numbers(cond, "yes", vals) == [10.0, 30.0]

    # No matches returns empty list
    assert _criteria_numbers([1, 2, 3], ">10") == []


def test_dollar_fraction_terms():
    assert _fractional_dollar_digits(1) == 0
    assert _fractional_dollar_digits(4) == 1
    assert _fractional_dollar_digits(16) == 2
    assert _fractional_dollar_digits(32) == 2

    # dollarde terms
    terms = _dollar_fraction_terms(1.02, 4)
    assert terms is not None
    sign, i_part, f_part, f, scale = terms
    assert sign == 1.0
    assert i_part == 1.0
    assert abs(f_part - 0.02) < 1e-9
    assert f == 4
    assert scale == 10

    # Negative amount
    neg_terms = _dollar_fraction_terms(-1.02, 4)
    assert neg_terms is not None
    assert neg_terms[0] == -1.0
    assert neg_terms[1] == 1.0

    # Non-positive or invalid fraction returns None
    assert _dollar_fraction_terms(1.02, 0) is None
    assert _dollar_fraction_terms(1.02, -4) is None
    assert _dollar_fraction_terms("invalid", 4) is None
    assert _dollar_fraction_terms(1.02, "invalid") is None

    # inf used to crash in math.floor / int(float(inf)) via OverflowError.
    assert _dollar_fraction_terms(1.02, float("inf")) is None
    assert _dollar_fraction_terms(1.02, float("-inf")) is None
    assert _dollar_fraction_terms(float("inf"), 4) is None
    assert _dollar_fraction_terms(float("-inf"), 4) is None
    assert _dollar_fraction_terms(float("nan"), 4) is None
    assert _dollar_fraction_terms(10**400, 4) is None


def test_match_criteria_wildcards_and_case():
    # Case insensitivity
    assert match_criteria("Apple", "apple") is True
    assert match_criteria("apple", "Apple") is True
    assert match_criteria("Banana", "<>banana") is False

    # Wildcards: *, ?, and ~ escape
    assert match_criteria("apple", "*pp*") is True
    assert match_criteria("apple", "a??le") is True
    assert match_criteria("apple", "*x*") is False
    assert match_criteria("a*b", "a~*b") is True
    assert match_criteria("ab", "a~*b") is False
    assert match_criteria("a?b", "a~?b") is True
    assert match_criteria("acb", "a~?b") is False

    # Blank matching: "=" matches empty/None, "<>" matches non-blank
    assert match_criteria("", "=") is True
    assert match_criteria(None, "=") is True
    assert match_criteria("hello", "=") is False
    assert match_criteria("", "<>") is False
    assert match_criteria(None, "<>") is False
    assert match_criteria("hello", "<>") is True

    # Operator allowlist: invalid operators like "><" do not match
    assert match_criteria(5, "><5") is False
    assert match_criteria(5, "=>5") is False


def test_is_calc_error_helper():
    from plugin.scripting.venv.calc_functions_util import _is_calc_error

    assert _is_calc_error(float("nan")) is True
    assert _is_calc_error(np.nan) is True
    assert _is_calc_error("#VALUE!") is True
    assert _is_calc_error("#N/A") is True
    assert _is_calc_error("#REF!") is True
    assert _is_calc_error("#hashtag") is False
    assert _is_calc_error("#1") is False
    assert _is_calc_error("hello") is False
    assert _is_calc_error(123) is False
    assert _is_calc_error(True) is False


def test_calc_sort_key():
    from plugin.scripting.venv.calc_functions_util import _calc_sort_key

    # Order: numbers < text (case-insensitive) < bools < blanks/errors
    k_num = _calc_sort_key(10)
    k_str = _calc_sort_key("apple")
    k_str_upper = _calc_sort_key("APPLE")
    k_bool = _calc_sort_key(True)
    k_blank = _calc_sort_key(None)
    k_err = _calc_sort_key("#VALUE!")
    k_nan = _calc_sort_key(float("nan"))

    assert k_num[0] == 0
    assert k_str[0] == 1
    assert k_bool[0] == 2
    assert k_blank[0] == 3
    assert k_err[0] == 3
    assert k_nan[0] == 3

    assert k_num < k_str < k_bool < k_blank
    assert k_str == k_str_upper


def test_clean_paired_arrays():
    from plugin.scripting.venv.calc_functions_util import _clean_paired_arrays

    # Clean paired extraction
    res = _clean_paired_arrays([1, 2, np.nan, 4], [10, 20, 30, 40])
    assert res is not None
    y, x = res
    assert np.array_equal(y, [1.0, 2.0, 4.0])
    assert np.array_equal(x, [10.0, 20.0, 40.0])

    # Length mismatch returns None
    assert _clean_paired_arrays([1, 2], [1, 2, 3]) is None

    # Text cells return None
    assert _clean_paired_arrays(["a", "b"], [1, 2]) is None


def test_int_shift_and_bitwise_limits():
    import operator
    from plugin.scripting.venv.calc_functions_util import _int_bitwise, _int_shift

    # Shift cap |shift| <= 53
    assert not math.isnan(_int_shift(1, 53, left=True))
    assert math.isnan(_int_shift(1, 54, left=True))
    assert math.isnan(_int_shift(1, -54, left=True))

    # Bitwise range 0 <= n < 2^48
    max_val = (1 << 48) - 1
    assert not math.isnan(_int_bitwise(operator.and_, max_val, 1))
    assert math.isnan(_int_bitwise(operator.and_, max_val + 1, 1))
    assert math.isnan(_int_bitwise(operator.and_, -1, 1))


def test_round_half_up():
    from plugin.scripting.venv.calc_functions_util import _round_half_up

    assert _round_half_up(2.5, 0) == Decimal("3")
    assert _round_half_up(-2.5, 0) == Decimal("-3")
    assert _round_half_up(1.25, 1) == Decimal("1.3")
    assert _round_half_up(-1.25, 1) == Decimal("-1.3")

    with pytest.raises(ValueError):
        _round_half_up(float("nan"), 0)
    with pytest.raises(ValueError):
        _round_half_up(float("inf"), 0)


def test_eval_d_criteria():
    from plugin.scripting.venv.calc_functions_util import _eval_d_criteria

    db = [["Tree", "Height"], ["Apple", 10], ["Pear", 15]]
    # Match Pear
    crit = [["Tree"], ["Pear"]]
    vals = _eval_d_criteria(db, "Height", crit)
    assert vals == [15.0]

    # No match returns empty list []
    crit_none = [["Tree"], ["Orange"]]
    vals_none = _eval_d_criteria(db, "Height", crit_none)
    assert vals_none == []

    # Invalid db or field returns None
    assert _eval_d_criteria("invalid_db", "Height", crit) is None
    assert _eval_d_criteria(db, 99, crit) is None
    assert _eval_d_criteria(db, "Height", "invalid_crit") is None


def test_multi_criteria_mask():
    from plugin.scripting.venv.calc_functions_util import _multi_criteria_mask

    # Matching lengths
    pairs = [([1, 2, 3], ">1"), (["a", "b", "c"], "<>a")]
    mask = _multi_criteria_mask(pairs, base_len=3)
    assert mask is not None
    assert list(mask) == [False, True, True]

    # Mismatched lengths returns None
    pairs_bad = [([1, 2], ">0"), ([1, 2, 3], ">0")]
    assert _multi_criteria_mask(pairs_bad) is None
    assert _multi_criteria_mask(pairs, base_len=2) is None


def test_get_coupon_dates_calendar_stepping():
    from plugin.scripting.venv.calc_functions_util import _get_coupon_dates

    # 2020-01-01 to 2020-06-01, freq=2, basis=0
    p_ser, c_ser, days_in_per, k = _get_coupon_dates(43831, 43983, 2, 0)
    assert p_ser == 43800.0  # 2019-12-01
    assert c_ser == 43983.0  # 2020-06-01
    assert days_in_per == 180.0
    assert k == 1.0

    # Invalid frequency or basis raises ValueError
    with pytest.raises(ValueError):
        _get_coupon_dates(43831, 43983, 3, 0)
    with pytest.raises(ValueError):
        _get_coupon_dates(43831, 43983, 2, 5)

    # Settlement >= maturity raises ValueError
    with pytest.raises(ValueError):
        _get_coupon_dates(43983, 43831, 2, 0)


def test_nan_on_error():
    from plugin.scripting.venv.calc_functions_util import nan_on_error

    @nan_on_error(ValueError, ZeroDivisionError)
    def fail(x):
        if x == 0:
            raise ZeroDivisionError
        if x < 0:
            raise ValueError
        return 10.0 / x

    assert fail(2) == 5.0
    assert math.isnan(fail(0))
    assert math.isnan(fail(-1))
    with pytest.raises(TypeError):
        fail("bad")




