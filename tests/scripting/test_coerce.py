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

def test_convert_to_datetime_nanoseconds_regression():
    from plugin.scripting.venv.coerce import convert_to_datetime
    import pandas as pd

    # 46242 is 2026-08-08 under 1899-12-30 default origin.
    # On the old behavior (pd.to_datetime([46242])), this resulted in 1970-01-01 00:00:00.000046242
    result = convert_to_datetime([46242.0])
    ts = result.iloc[0]
    assert ts.year == 2026
    assert ts.month == 8
    assert ts.day == 8
