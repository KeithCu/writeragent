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


def test_as_bool_truth_table() -> None:
    assert as_bool(True) is True
    assert as_bool(False) is False
    assert as_bool("true") is True
    assert as_bool("ON") is True
    assert as_bool("off") is False
    assert as_bool("no") is False
    assert as_bool(1) is True
    assert as_bool(0) is False
    assert as_bool(None) is False


@given(n=st.integers(min_value=-10_000, max_value=10_000))
@settings(max_examples=80)
def test_hypothesis_parse_int_round_trip_str(n: int) -> None:
    assert parse_int_robust(str(n)) == n
    assert parse_int_robust(n) == n


def test_parse_int_robust_formats_and_raises() -> None:
    assert parse_int_robust("8765.7") == 8765
    assert parse_int_robust("8765,0") == 8765
    assert parse_int_robust("8765,5") == 8765
    assert parse_int_robust("1,234") == 1234
    assert parse_int_robust("-1,234") == -1234
    for bad in (None, "", "   ", "not-a-number", float("inf"), float("-inf"), float("nan"), "inf"):
        with pytest.raises(ValueError):
            parse_int_robust(bad)


@given(n=st.floats(allow_nan=False, allow_infinity=False, width=32, min_value=-1e6, max_value=1e6))
@settings(max_examples=80)
def test_hypothesis_parse_float_round_trip_str(n: float) -> None:
    assert parse_float_robust(str(n)) == pytest.approx(n)
    assert parse_float_robust(n) == pytest.approx(n)


def test_parse_float_robust_locale_and_int() -> None:
    assert parse_float_robust("1,5") == pytest.approx(1.5)
    assert parse_float_robust("0,25") == pytest.approx(0.25)
    assert parse_float_robust("1,234") == pytest.approx(1234.0)
    assert parse_float_robust(" 7.5 ") == pytest.approx(7.5)
    assert parse_float_robust(3) == 3.0
    assert parse_float_robust(2.5) == 2.5


def test_parse_float_robust_raises() -> None:
    for bad in (None, "", "   ", "not-a-number"):
        with pytest.raises(ValueError):
            parse_float_robust(bad)
