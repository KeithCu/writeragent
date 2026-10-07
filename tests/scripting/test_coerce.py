# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for grid numeric-string coercion."""

from __future__ import annotations

import pytest

from plugin.scripting.venv.coerce import _parse_numeric_string


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("-5", -5.0),
        ("-1,234.50", -1234.5),
        ("$-123", -123.0),
        ("-$123", -123.0),
        ("-10%", -0.1),
        ("+7", 7.0),
        ("$1,234", 1234.0),
        ("(5)", None),
        ("+-5", None),
    ],
)
def test_parse_numeric_string_keeps_sign(text: str, expected: float | None):
    assert _parse_numeric_string(text) == expected


def test_numeric_headers_and_mixed_columns_keep_their_text():
    """Whole-number headers stay \"2024\", and a mixed column does not become NaN."""
    pytest.importorskip("pandas")
    from plugin.scripting.venv.coerce import grid_to_dataframe

    years = grid_to_dataframe([[2024.0, 2025.5], [1.0, 2.0]])
    assert list(years.df.columns) == ["2024", "2025.5"]

    labels = grid_to_dataframe([["2024", "00123"], [1, "00456"]], parse_strings=True)
    assert list(labels.df.columns) == ["2024", "00123"]
    assert labels.df.iloc[0, 1] == pytest.approx(456.0)

    mixed = grid_to_dataframe([["mix"], [10], ["x"], [30], [40], [50]], parse_strings=True)
    assert mixed.df.loc[1, "mix"] == "x"
    assert float(mixed.df.loc[0, "mix"]) == 10.0
    assert str(mixed.df["mix"].dtype) == "object"


def test_is_missing_value_covers_pandas_and_numpy():
    """is_missing_value recognizes None, strings/errors, float NaN, numpy NaN, pd.NA and pd.NaT."""
    from plugin.scripting.venv.coerce import is_missing_value
    import math

    assert is_missing_value(None) is True
    assert is_missing_value("") is True
    assert is_missing_value("   ") is True
    assert is_missing_value("#N/A") is True
    assert is_missing_value("#DIV/0!") is True
    assert is_missing_value("#VALUE!") is True
    assert is_missing_value(math.nan) is True
    assert is_missing_value(float("nan")) is True

    # Real values
    assert is_missing_value(0) is False
    assert is_missing_value(0.0) is False
    assert is_missing_value(False) is False
    assert is_missing_value(True) is False
    assert is_missing_value("hello") is False

    np = pytest.importorskip("numpy")
    assert is_missing_value(np.nan) is True
    assert is_missing_value(np.float32(float("nan"))) is True
    assert is_missing_value(np.float64(float("nan"))) is True
    assert is_missing_value(np.float32(1.0)) is False

    pd = pytest.importorskip("pandas")
    assert is_missing_value(pd.NA) is True
    assert is_missing_value(pd.NaT) is True


def test_dedupe_column_names_in_coerce():
    """_dedupe_column_names produces unique labels and avoids colliding with pre-existing numbered suffixes."""
    from plugin.scripting.venv.coerce import _dedupe_column_names

    assert _dedupe_column_names(["a", "a", "a_1"]) == ["a", "a_1", "a_1_1"]
    assert _dedupe_column_names(["x", "x", "x"]) == ["x", "x_1", "x_2"]
    assert _dedupe_column_names(["", ""]) == ["column", "column_1"]

