# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""deal / Hypothesis verification for config_schema coerce helpers."""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from plugin.framework.config_schema import as_bool, parse_float_robust, parse_int_robust


@given(
    value=st.one_of(
        st.booleans(),
        st.sampled_from(["1", "true", "YES", "on", "0", "false", "off", "", "  true  "]),
        st.integers(),
        st.floats(allow_nan=False, allow_infinity=False, width=32),
        st.none(),
        st.lists(st.integers(), max_size=2),
    )
)
@settings(max_examples=100)
def test_hypothesis_as_bool_returns_bool(value) -> None:
    assert isinstance(as_bool(value), bool)


def test_as_bool_truth_table() -> None:
    assert as_bool(True) is True
    assert as_bool(False) is False
    assert as_bool("true") is True
    assert as_bool("ON") is True
    assert as_bool("no") is False
    assert as_bool(1) is True
    assert as_bool(0) is False
    assert as_bool(None) is False


@given(n=st.integers(min_value=-10_000, max_value=10_000))
@settings(max_examples=80)
def test_hypothesis_parse_int_round_trip_str(n: int) -> None:
    assert parse_int_robust(str(n)) == n
    assert parse_int_robust(n) == n


@given(s=st.sampled_from(["8765,0", "8765,5", "12.9", " 42 "]))
@settings(max_examples=20)
def test_hypothesis_parse_int_locale_samples(s: str) -> None:
    assert isinstance(parse_int_robust(s), int)


def test_parse_int_robust_raises() -> None:
    with pytest.raises(ValueError):
        parse_int_robust(None)
    with pytest.raises(ValueError):
        parse_int_robust("")
    with pytest.raises(ValueError):
        parse_int_robust("not-a-number")
    with pytest.raises(ValueError):
        parse_int_robust(float("inf"))
    with pytest.raises(ValueError):
        parse_int_robust(float("-inf"))
    with pytest.raises(ValueError):
        parse_int_robust(float("nan"))
    with pytest.raises(ValueError):
        parse_int_robust("inf")


@given(n=st.floats(allow_nan=False, allow_infinity=False, width=32, min_value=-1e6, max_value=1e6))
@settings(max_examples=80)
def test_hypothesis_parse_float_round_trip_str(n: float) -> None:
    assert parse_float_robust(str(n)) == pytest.approx(n)
    assert parse_float_robust(n) == pytest.approx(n)


@given(s=st.sampled_from(["1,5", "8765,0", "12.9", " 3.14 ", "0"]))
@settings(max_examples=20)
def test_hypothesis_parse_float_locale_samples(s: str) -> None:
    assert isinstance(parse_float_robust(s), float)


def test_parse_float_robust_locale_and_int() -> None:
    assert parse_float_robust("1,5") == pytest.approx(1.5)
    assert parse_float_robust(3) == 3.0
    assert parse_float_robust(2.5) == 2.5


def test_parse_float_robust_raises() -> None:
    with pytest.raises(ValueError):
        parse_float_robust(None)
    with pytest.raises(ValueError):
        parse_float_robust("")
    with pytest.raises(ValueError):
        parse_float_robust("not-a-number")
