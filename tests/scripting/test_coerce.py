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
    assert is_missing_value("#n/a") is True
    assert is_missing_value("#DIV/0!") is True
    assert is_missing_value("#div/0!") is True
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
    assert is_missing_value(np.datetime64("NaT")) is True
    assert is_missing_value(np.timedelta64("NaT")) is True
    assert is_missing_value(np.datetime64("2020-01-01")) is False

    pd = pytest.importorskip("pandas")
    assert is_missing_value(pd.NA) is True
    assert is_missing_value(pd.NaT) is True


def test_dedupe_column_names_in_coerce():
    """_dedupe_column_names produces unique labels and avoids colliding with pre-existing numbered suffixes."""
    from plugin.scripting.venv.coerce import _dedupe_column_names

    assert _dedupe_column_names(["a", "a", "a_1"]) == ["a", "a_1", "a_1_1"]
    assert _dedupe_column_names(["x", "x", "x"]) == ["x", "x_1", "x_2"]
    assert _dedupe_column_names(["", ""]) == ["column", "column_1"]


def test_parse_date_column_serial_year_iso_and_mixed():
    import pandas as pd
    from plugin.scripting.venv.coerce import parse_date_column

    # serial 45292 -> 2024-01-01
    s_serial = parse_date_column(pd.Series([45292]))
    assert s_serial.iloc[0] == pd.Timestamp("2024-01-01")

    # serial 45292.5 -> 2024-01-01 12:00
    s_half = parse_date_column(pd.Series([45292.5]))
    assert s_half.iloc[0] == pd.Timestamp("2024-01-01 12:00:00")

    # years 2019, 2020 -> 1 January
    s_years = parse_date_column(pd.Series([2019, 2020]))
    assert s_years.iloc[0] == pd.Timestamp("2019-01-01")
    assert s_years.iloc[1] == pd.Timestamp("2020-01-01")

    # ISO "2024-01-15" stays
    s_iso = parse_date_column(pd.Series(["2024-01-15"]))
    assert s_iso.iloc[0] == pd.Timestamp("2024-01-15")

    # mixed [45292.0, "2024-06-01"] is both in 2024 and neither in 1970
    s_mixed = parse_date_column(pd.Series([45292.0, "2024-06-01"]))
    assert s_mixed.iloc[0] == pd.Timestamp("2024-01-01")
    assert s_mixed.iloc[1] == pd.Timestamp("2024-06-01")

    # 0 and -1 are NaT
    s_neg = parse_date_column(pd.Series([0, -1]))
    assert pd.isna(s_neg.iloc[0])
    assert pd.isna(s_neg.iloc[1])

    s_mixed_formats = parse_date_column(pd.Series(["2024-01-01", "01/02/2024", "not-a-date"]))
    assert s_mixed_formats.iloc[0] == pd.Timestamp("2024-01-01")
    assert s_mixed_formats.iloc[1] == pd.Timestamp("2024-01-02")
    assert pd.isna(s_mixed_formats.iloc[2])

    s_date_time = parse_date_column(pd.Series(["2024-01-01", "2024-01-02 15:30:00"]))
    assert s_date_time.iloc[0] == pd.Timestamp("2024-01-01")
    assert s_date_time.iloc[1] == pd.Timestamp("2024-01-02 15:30:00")

    s_year_strs = parse_date_column(pd.Series(["2024", "2025"]))
    assert s_year_strs.iloc[0] == pd.Timestamp("2024-01-01")
    assert s_year_strs.iloc[1] == pd.Timestamp("2025-01-01")

    s_utc = parse_date_column(pd.Series(["2024-01-01T00:00:00Z", "2024-06-01T00:00:00Z"]))
    assert s_utc.iloc[0] == pd.Timestamp("2024-01-01")
    assert s_utc.iloc[1] == pd.Timestamp("2024-06-01")
    assert s_utc.dt.tz is None

    s_mixed_tz = parse_date_column(pd.Series(["2024-01-01T00:00:00Z", "01/02/2024"]))
    assert s_mixed_tz.iloc[0] == pd.Timestamp("2024-01-01")
    assert pd.isna(s_mixed_tz.iloc[1])
    assert s_mixed_tz.dt.tz is None


def test_table_from_df_nan_is_none():
    import json
    import numpy as np
    import pandas as pd
    from plugin.scripting.venv.coerce import table_from_df

    df = pd.DataFrame({"a": [1.0, np.nan, 3.0]})
    table = table_from_df(df, name="test")
    assert table["rows"] == [[1.0], [None], [3.0]]
    # json.dumps allow_nan=False must succeed
    dumped = json.dumps(table["rows"], allow_nan=False)
    assert dumped == "[[1.0], [null], [3.0]]"


def test_records_from_df_nan_is_none():
    import json
    import numpy as np
    import pandas as pd
    from plugin.scripting.venv.coerce import records_from_df

    df = pd.DataFrame({"a": [1.0, np.nan, 3.0]})
    records = records_from_df(df)
    assert records == [{"a": 1.0}, {"a": None}, {"a": 3.0}]
    dumped = json.dumps(records, allow_nan=False)
    assert dumped == '[{"a": 1.0}, {"a": null}, {"a": 3.0}]'


def test_numeric_columns_string_is_one_name():
    import pandas as pd
    from plugin.scripting.venv.coerce import numeric_columns

    df = pd.DataFrame({"AAPL": [1.0, 2.0], "MSFT": [3.0, 4.0]})
    cols = numeric_columns(df, "AAPL")
    assert cols == ["AAPL"]


def test_date_like_names_are_tokens():
    from plugin.scripting.venv.coerce import _is_date_like_column_name

    false_friends = [
        "candidate",
        "validate",
        "update",
        "consolidate",
        "mandate",
        "runtime",
        "lifetime",
        "overtime",
        "downtime",
        "Update",
        "Consolidated",
        "Runtime",
        "amount",
        "Monday",
        "payday",
        "ORDERDATE",
    ]
    for name in false_friends:
        assert not _is_date_like_column_name(name), f"{name} should not be date-like"

    date_names = [
        "Date",
        "OrderDate",
        "datetime",
        "DateTime",
        "updated",
        "created_at",
        "timestamp",
        "dob",
        "Start Date",
        "fiscal_year",
        "my_date",
        "time",
        "start",
        "end",
        "year",
        "start_price",
        "end_balance",
        "day_of_week",
    ]
    for name in date_names:
        assert _is_date_like_column_name(name), f"{name} should be date-like"


def test_date_cols_true_keeps_false_friend_columns():
    import pandas as pd
    from plugin.scripting.venv.coerce import grid_to_dataframe

    grid = [
        ["Consolidated", "Runtime", "Update", "Amount", "Date", "OrderDate"],
        [10.0, 20.0, 30.0, 45292.0, 45292.0, "2024-01-01"],
    ]
    res = grid_to_dataframe(grid, date_cols=True)
    df = res.df
    assert pd.api.types.is_datetime64_any_dtype(df["Date"])
    assert pd.api.types.is_datetime64_any_dtype(df["OrderDate"])
    assert not pd.api.types.is_datetime64_any_dtype(df["Consolidated"])
    assert not pd.api.types.is_datetime64_any_dtype(df["Runtime"])
    assert not pd.api.types.is_datetime64_any_dtype(df["Update"])
    assert not pd.api.types.is_datetime64_any_dtype(df["Amount"])
    assert pd.api.types.is_numeric_dtype(df["Consolidated"])
    assert pd.api.types.is_numeric_dtype(df["Runtime"])
    assert pd.api.types.is_numeric_dtype(df["Update"])
    assert pd.api.types.is_numeric_dtype(df["Amount"])


def test_float64_serial_uses_date_origin():
    import pandas as pd
    from plugin.scripting.venv.coerce import _coerce_column_types

    df = pd.DataFrame({"Date": pd.Series([45292, 45293], dtype="Float64")})
    out = _coerce_column_types(df, parse_strings=False, date_cols=True, date_origin="1899-12-30")
    assert out["Date"].iloc[0] == pd.Timestamp("2024-01-01")


def test_resolve_df_integer_columns_are_not_both_kinds():
    import pandas as pd
    from plugin.scripting.venv.coerce import resolve_df

    df = pd.DataFrame({1: [1.0, 2.0], 2: ["a", "b"]})
    res = resolve_df(df)
    assert res.metadata["numeric_cols"] == ["1"]
    assert res.metadata["categorical_cols"] == ["2"]
    assert res.metadata["datetime_cols"] == []


def test_ndarray_and_series_are_grids():
    import numpy as np
    import pandas as pd
    from plugin.scripting.venv.coerce import grid_to_dataframe

    # A 2x2 numpy array becomes a 2x2 frame of those values, not a 1x1 cell
    arr_2d = np.array([[1, 2], [3, 4]])
    res_2d = grid_to_dataframe(arr_2d, header_row=None)
    assert res_2d.df.shape == (2, 2)
    assert res_2d.df.values.tolist() == [[1, 2], [3, 4]]

    # A 1-d array [1, 2, 3] is one row of three columns
    arr_1d = np.array([1, 2, 3])
    res_1d = grid_to_dataframe(arr_1d, header_row=None)
    assert res_1d.df.shape == (1, 3)
    assert res_1d.df.values.tolist() == [[1, 2, 3]]

    # A 0-d array numpy.array(7) is a 1x1 frame whose cell is 7 (a Python int, not an array)
    arr_0d = np.array(7)
    res_0d = grid_to_dataframe(arr_0d, header_row=None)
    assert res_0d.df.shape == (1, 1)
    val = res_0d.df.iloc[0, 0]
    assert val == 7
    assert isinstance(val, (int, np.integer))

    # A pandas.Series([10, 20]) is one row, two columns
    s = pd.Series([10, 20])
    res_s = grid_to_dataframe(s, header_row=None)
    assert res_s.df.shape == (1, 2)
    assert res_s.df.values.tolist() == [[10, 20]]

    # A 3-d array stays a 1x1 cell
    arr_3d = np.zeros((2, 2, 2))
    res_3d = grid_to_dataframe(arr_3d, header_row=None)
    assert res_3d.df.shape == (1, 1)
    assert res_3d.df.iloc[0, 0] is arr_3d


def test_grid_dataframe_round_trip():
    # Item 28: round-trip tests: grid_to_dataframe → dataframe_to_labeled_grid
    # keeps values and header for int, float, text, blank and dates.
    from plugin.scripting.calc_range import dataframe_to_labeled_grid
    from plugin.scripting.venv.coerce import grid_to_dataframe

    input_grid = [
        ["int_col", "float_col", "text_col", "blank_col", "date_col"],
        [10, 1.25, "hello", "", "2024-01-01"],
        [20, 2.5, "world", None, "2024-01-02"],
    ]
    res = grid_to_dataframe(input_grid, parse_strings=False, date_cols=["date_col"])
    labeled = dataframe_to_labeled_grid([str(c) for c in res.df.columns], res.df.values.tolist())
    assert labeled[0] == ["int_col", "float_col", "text_col", "blank_col", "date_col"]
    assert labeled[1][0] == 10
    assert labeled[1][1] == 1.25
    assert labeled[1][2] == "hello"
    assert labeled[1][3] in ("", None)
    assert str(labeled[1][4]).startswith("2024-01-01")
    assert labeled[2][0] == 20
    assert labeled[2][1] == 2.5
    assert labeled[2][2] == "world"
    assert labeled[2][3] in ("", None)
    assert str(labeled[2][4]).startswith("2024-01-02")


def test_numeric_columns_empty_list_is_empty():
    import pandas as pd
    from plugin.scripting.venv.coerce import numeric_columns

    df = pd.DataFrame({"AAPL": [1.0, 2.0], "MSFT": [3.0, 4.0]})
    assert numeric_columns(df, []) == []
    assert numeric_columns(df, None) == ["AAPL", "MSFT"]


def test_coerce_str_list_skips_none():
    from plugin.scripting.venv.coerce import coerce_str_list

    assert coerce_str_list(None) is None
    assert coerce_str_list([None]) == []
    assert coerce_str_list([None, "A"]) == ["A"]
    assert coerce_str_list([1]) == ["1"]


def test_resolve_df_calc_range_uses_address_hint():
    from plugin.scripting.calc_range import CalcRange
    from plugin.scripting.venv.coerce import resolve_df

    res = resolve_df(CalcRange([["a", "b"], [1, 2]], address="Sheet1.A1"))
    assert res.metadata["sheet_hint"] == "Sheet1.A1"
    assert list(res.df.columns) == ["a", "b"]


def test_date_cols_integer_years_are_january_first():
    import pandas as pd
    from plugin.scripting.venv.coerce import grid_to_dataframe

    res = grid_to_dataframe([["HireYear", "Name"], [2019, "a"], [2020, "b"]], date_cols=True)
    df = res.df
    assert df["HireYear"].iloc[0] == pd.Timestamp("2019-01-01")
    assert df["HireYear"].iloc[1] == pd.Timestamp("2020-01-01")
    assert list(df["Name"]) == ["a", "b"]


def test_date_cols_mixed_year_and_serial_stays_serial():
    import pandas as pd
    from plugin.scripting.venv.coerce import grid_to_dataframe

    res = grid_to_dataframe([["Date"], [2019], [45292]], date_cols=True)
    df = res.df
    assert df["Date"].iloc[0] == pd.Timestamp("1905-07-11")
    assert df["Date"].iloc[1] == pd.Timestamp("2024-01-01")


def test_date_cols_zero_is_first_column():
    import pandas as pd
    from plugin.scripting.venv.coerce import grid_to_dataframe

    res = grid_to_dataframe([["Code", "N"], [45292, 1]], date_cols=0, parse_strings=False)
    df = res.df
    assert df["Code"].iloc[0] == pd.Timestamp("2024-01-01")
    assert df["N"].iloc[0] == 1


def test_grid_to_dataframe_copies_a_dataframe():
    import pandas as pd
    from plugin.scripting.venv.coerce import coerce_to_dataframe, grid_to_dataframe

    orig = pd.DataFrame({"a": [1, 2]})
    res = grid_to_dataframe(orig, header_row=5, parse_strings=False)
    assert res.df.shape == (2, 1)
    assert list(res.df.columns) == ["a"]
    assert res.df.values.tolist() == [[1], [2]]
    res.df.iloc[0, 0] = 9
    assert orig.iloc[0, 0] == 1

    res_coerce = coerce_to_dataframe(orig)
    assert list(res_coerce.df.columns) == ["a"]
    assert res_coerce.df.values.tolist() == [[1], [2]]


def test_resolve_df_duplicate_columns_have_dtypes():
    import pandas as pd
    from plugin.scripting.venv.coerce import resolve_df

    df = pd.DataFrame([[1, 2], [3, 4]], columns=["a", "a"])
    res = resolve_df(df)
    assert res.metadata["n_cols"] == 2
    assert res.metadata["numeric_cols"] == ["a", "a"]
    assert res.metadata["categorical_cols"] == []
    assert res.metadata["datetime_cols"] == []


def test_coerce_bool_numpy_bool():
    import numpy as np
    from plugin.scripting.venv.coerce import coerce_bool

    assert coerce_bool(np.bool_(True)) is True
    assert coerce_bool(np.bool_(False)) is False
    assert coerce_bool(np.int64(1)) is True
    with pytest.raises(ValueError):
        coerce_bool(np.int64(2))


def test_header_row_and_index_col_out_of_range():
    from plugin.scripting.venv.coerce import grid_to_dataframe

    grid = [["h"], [1]]
    with pytest.raises(ValueError, match=r"header_row 5"):
        grid_to_dataframe(grid, header_row=5)
    with pytest.raises(ValueError, match=r"header_row -1"):
        grid_to_dataframe(grid, header_row=-1)

    res_valid = grid_to_dataframe(grid, header_row=1)
    assert list(res_valid.df.columns) == ["1"]
    assert len(res_valid.df) == 0

    with pytest.raises(ValueError, match=r"index_col 3"):
        grid_to_dataframe(grid, index_col=3)
    with pytest.raises(ValueError, match=r"index_col -1"):
        grid_to_dataframe(grid, index_col=-1)

    empty_res = grid_to_dataframe([], header_row=5, index_col=0)
    assert len(empty_res.df) == 0



