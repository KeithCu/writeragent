# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for plugin.scripting.venv.calc_functions_util."""

from __future__ import annotations

import datetime as dt
import math
import numpy as np

from plugin.scripting.venv.calc_functions_util import (
    _build_holiday_set,
    _collect_a_values,
    _extract_numeric_array,
    _find_match_index,
    _npf_result,
    _parse_holidays,
    _parse_weekend,
    _parse_weekend_code,
    _serial_to_date,
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


def test_collect_a_values():
    res = _collect_a_values([1.0, 2.0], True, False, "text", "", None, [3.0, np.bool_(True)])
    assert isinstance(res, np.ndarray)
    assert np.allclose(res, [1.0, 2.0, 1.0, 0.0, 0.0, 0.0, 0.0, 3.0, 1.0])
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
    assert _parse_holidays is _build_holiday_set

    # Scalar and list of serials
    h1 = _build_holiday_set(46181)
    assert h1 == {dt.date(2026, 6, 8)}

    h2 = _build_holiday_set([46181, "46182", "", None, "invalid", 46181])
    assert h2 == {dt.date(2026, 6, 8), dt.date(2026, 6, 9)}


def test_parse_weekend():
    assert _parse_weekend_code is _parse_weekend

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


