# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for CalcRange / labeled-table handoff contract."""

from __future__ import annotations

import pytest

pytest.importorskip("pandas")
pytest.importorskip("numpy")

from plugin.calc.calc_addin_data import pack_calc_data_for_wire
from plugin.calc.python.function import result_to_calc_grid
from plugin.scripting.calc_range import CalcRange, dataframe_to_labeled_grid, materialize_inputs
from plugin.scripting.payload_codec import PAYLOAD_DATAFRAME, child_unpack_data, is_calc_range_payload
from plugin.scripting.venv.coerce import grid_to_dataframe


def test_calc_range_preserves_row_and_column_orientation():
    row = CalcRange([[1, 2, 3]])
    col = CalcRange([[1], [2], [3]])
    assert row.shape == (1, 3)
    assert col.shape == (3, 1)
    assert list(row) == [[1, 2, 3]]
    assert list(col) == [[1], [2], [3]]


def test_to_pandas_header_row_none_and_duplicates():
    grid = [["A", "A", ""], [1, 2, 3]]
    df = CalcRange(grid).to_pandas(header_row=0)
    assert list(df.columns) == ["A", "A_1", "column"]
    collided = [["a", "a", "a_1"], [1, 2, 3]]
    df_collided = CalcRange(collided).to_pandas(header_row=0)
    assert list(df_collided.columns) == ["a", "a_1", "a_1_1"]
    assert len(set(df_collided.columns)) == 3
    df2 = CalcRange(grid).to_pandas(header_row=None)
    assert list(df2.columns) == ["col_0", "col_1", "col_2"]
    assert len(df2) == 2


def test_to_pandas_keeps_text_without_parse_strings():
    grid = [["Zip", "Amt"], ["00123", "$1,200.50"]]
    df = CalcRange(grid).to_pandas(header_row=0, parse_strings=False)
    assert df.loc[0, "Zip"] == "00123"
    assert df.loc[0, "Amt"] == "$1,200.50"
    df_parsed = CalcRange(grid).to_pandas(header_row=0, parse_strings=True)
    assert df_parsed.loc[0, "Amt"] == pytest.approx(1200.50)


def test_to_pandas_index_col():
    grid = [["id", "v"], ["a", 1], ["b", 2]]
    df = CalcRange(grid).to_pandas(header_row=0, index_col=0)
    assert list(df.index) == ["a", "b"]
    assert list(df.columns) == ["v"]


def test_numpy_interop_via_array_protocol():
    import numpy as np

    rng = CalcRange([[1.0], [2.0], [3.0]])
    assert float(np.mean(rng)) == pytest.approx(2.0)
    assert rng.to_numpy().shape == (3, 1)


def test_wire_roundtrip_materialize_inputs():
    wire = pack_calc_data_for_wire([["H1", "H2"], [1, 2]], address="Sheet1.A1:B2")
    assert is_calc_range_payload(wire)
    inputs = materialize_inputs(wire)
    assert len(inputs) == 1
    assert inputs[0].address == "Sheet1.A1:B2"
    assert inputs[0].values == [["H1", "H2"], [1, 2]]
    rng = child_unpack_data(wire)
    assert isinstance(rng, CalcRange)


def test_materialize_json_list_of_grids_as_multi_inputs():
    # Online =PY multi-range JSON without multi_data envelope.
    wire = [[[1, 2]], [[3], [4]]]
    inputs = materialize_inputs(wire)
    assert len(inputs) == 2
    assert inputs[0].values == [[1, 2]]
    assert inputs[1].values == [[3], [4]]
    # Ordinary 2D block stays one range.
    single = materialize_inputs([[1, 2], [3, 4]])
    assert len(single) == 1
    assert single[0].shape == (2, 2)


def test_dataframe_egress_includes_header_row():
    envelope = {
        "__wa_payload__": PAYLOAD_DATAFRAME,
        "columns": ["A", "B"],
        "data": [[1, 2], [3, 4]],
    }
    grid = result_to_calc_grid(envelope)
    assert grid == [["A", "B"], [1, 2], [3, 4]]
    assert dataframe_to_labeled_grid(["X"], [[9]], include_header=True) == [["X"], [9]]
    assert dataframe_to_labeled_grid(["X"], [[9]], include_header=False) == [[9]]


def test_result_to_calc_grid_ndarray_body_is_data():
    """A leftover ndarray body is the grid, not an empty body under the header."""
    np = pytest.importorskip("numpy")
    envelope = {
        "__wa_payload__": PAYLOAD_DATAFRAME,
        "columns": ["a", "b"],
        "data": np.array([[1.0, 2.0], [3.0, 4.0]]),
    }
    assert result_to_calc_grid(envelope) == [["a", "b"], [1.0, 2.0], [3.0, 4.0]]
    bare = result_to_calc_grid(np.arange(3))
    assert bare == [0, 1, 2]


def test_dataframe_to_labeled_grid_zero_row_is_header_only():
    """0-row DataFrame envelope spills the header row only — not an error and not a new payload kind."""
    assert dataframe_to_labeled_grid(["A", "B"], []) == [["A", "B"]]
    assert dataframe_to_labeled_grid(["A", "B"], None) == [["A", "B"]]
    assert dataframe_to_labeled_grid(["A"], [], include_header=False) == []
    envelope = {"__wa_payload__": PAYLOAD_DATAFRAME, "columns": ["A", "B"], "data": []}
    assert result_to_calc_grid(envelope) == [["A", "B"]]


def test_grid_to_dataframe_header_row_none():
    result = grid_to_dataframe([[1, 2], [3, 4]], header_row=None)
    assert list(result.df.columns) == ["col_0", "col_1"]
    assert len(result.df) == 2


def test_to_pandas_date_cols_explicit_and_detected():
    import pandas as pd

    # 46242.0 is 2026-08-08 under 1899-12-30 NullDate
    grid = [["Date", "Amount", "OrderDate"], [46242.0, 100.0, "2026-08-08"], [46243.0, 200.0, "2026-08-09"]]
    rng = CalcRange(grid)

    # 1. date_cols=True auto-detects date-like column names
    df = rng.to_pandas(date_cols=True)
    assert pd.api.types.is_datetime64_any_dtype(df["Date"])
    assert df.loc[0, "Date"] == pd.Timestamp("2026-08-08")
    assert pd.api.types.is_datetime64_any_dtype(df["OrderDate"])
    assert df.loc[0, "OrderDate"] == pd.Timestamp("2026-08-08")
    assert df.loc[0, "Amount"] == 100.0

    # 2. Specific column list or scalar by name or index
    df2 = rng.to_pandas(date_cols="Date")
    assert pd.api.types.is_datetime64_any_dtype(df2["Date"])
    assert df2.loc[0, "Date"] == pd.Timestamp("2026-08-08")
    assert df2.loc[0, "OrderDate"] == "2026-08-08"

    # Index 2 (OrderDate) as scalar integer
    df3 = rng.to_pandas(date_cols=2)
    assert pd.api.types.is_datetime64_any_dtype(df3["OrderDate"])
    assert df3.loc[0, "OrderDate"] == pd.Timestamp("2026-08-08")
    # Date was not specified -> stays float
    assert df3.loc[0, "Date"] == 46242.0

    # 3. Custom date_origin (e.g. 1904-01-01)
    # Under 1904-01-01, day 0 is 1904-01-01, day 1 is 1904-01-02
    grid_1904 = [["Date"], [1.0]]
    df_1904 = CalcRange(grid_1904).to_pandas(date_cols=True, date_origin="1904-01-01")
    assert df_1904.loc[0, "Date"] == pd.Timestamp("1904-01-02")


def test_calc_range_1x1_arithmetic_and_scalar_returns():
    data = CalcRange([[2]])
    assert data + 3 == 5
    assert 3 + data == 5
    assert sum([data]) == 2
    assert data * 4 == 8
    assert 4 * data == 8
    assert data - 1 == 1
    assert 10 - data == 8
    assert data / 2 == 1.0
    assert 10 / data == 5.0
    assert data // 2 == 1
    assert 5 // data == 2
    assert data % 2 == 0
    assert 5 % data == 1
    assert data ** 3 == 8
    assert 3 ** data == 9
    assert -data == -2
    assert +data == 2
    assert abs(data) == 2


def test_calc_range_1x1_scalar_conversions_and_formatting():
    import math

    data = CalcRange([[2.5]])
    assert float(data) == 2.5
    assert int(data) == 2
    assert round(data) == 2
    assert round(CalcRange([[2.567]]), 2) == 2.57
    assert math.trunc(data) == 2
    assert math.floor(data) == 2
    assert math.ceil(data) == 3
    assert f"{data:.2f}" == "2.50"
    assert str(data) == "2.5"


def test_calc_range_1x1_comparisons_and_hash():
    d1 = CalcRange([[2]])
    d2 = CalcRange([[2]])
    d3 = CalcRange([[5]])

    assert d1 == 2
    assert 2 == d1
    assert d1 == d2
    assert d1 != 3
    assert d1 != d3
    assert d1 < 3
    assert d1 < d3
    assert d1 <= 2
    assert d1 <= d2
    assert d3 > 2
    assert d3 > d1
    assert d3 >= 5
    assert d3 >= d1
    assert CalcRange.__hash__ is None


def test_calc_range_1x1_string_and_blank():
    s = CalcRange([["hello"]])
    assert s + " world" == "hello world"
    assert "say " + s == "say hello"
    assert s * 2 == "hellohello"
    assert 2 * s == "hellohello"
    assert str(s) == "hello"

    blank = CalcRange([[None]])
    with pytest.raises(TypeError):
        _ = blank + 3
    with pytest.raises(TypeError):
        _ = float(blank)
    with pytest.raises(TypeError):
        _ = int(blank)


def test_calc_range_bool_protocol():
    assert bool(CalcRange([[1]])) is True
    assert bool(CalcRange([[0]])) is False
    assert bool(CalcRange([[None]])) is False
    assert bool(CalcRange([["hello"]])) is True
    assert bool(CalcRange([[""]])) is False
    assert bool(CalcRange([])) is False

    multi = CalcRange([[1, 2], [3, 4]])
    with pytest.raises(ValueError, match="ambiguous"):
        _ = bool(multi)


def test_calc_range_chaining_two_ranges():
    r1 = CalcRange([[2]])
    r2 = CalcRange([[3]])
    assert r1 + r2 == 5
    assert r1 * r2 == 6
    assert r2 - r1 == 1


def test_calc_range_multi_cell_arithmetic_broadcasting():
    import numpy as np

    multi = CalcRange([[1, 2], [3, 4]])
    res = multi + 10
    assert isinstance(res, np.ndarray)
    assert res.tolist() == [[11, 12], [13, 14]]

    res2 = 10 + multi
    assert isinstance(res2, np.ndarray)
    assert res2.tolist() == [[11, 12], [13, 14]]

    # 1x1 + multi
    single = CalcRange([[5]])
    res3 = single + multi
    assert isinstance(res3, np.ndarray)
    assert res3.tolist() == [[6, 7], [8, 9]]


def test_calc_range_preserves_attributes_on_1x1():
    r = CalcRange([[42]], address="Sheet1.A1")
    assert r.values == [[42]]
    assert r.shape == (1, 1)
    assert r.nrows == 1
    assert r.ncols == 1
    assert r.address == "Sheet1.A1"
    assert r.to_numpy().shape == (1, 1)
    assert r.to_pandas().shape == (0, 1)  # 1 row as header
    assert r.to_pandas(header_row=None).shape == (1, 1)


def test_calc_range_no_numpy_fallback():
    from unittest.mock import patch

    # 1x1 arithmetic is pure stdlib and never calls to_numpy
    single = CalcRange([[7]])
    with patch.object(CalcRange, "to_numpy", side_effect=ImportError("No module named numpy")):
        assert single + 3 == 10
        assert 10 - single == 3
        assert single * 2 == 14
        assert single == 7

    # Multi-cell arithmetic requires NumPy and cleanly raises TypeError when absent
    multi = CalcRange([[1, 2], [3, 4]])
    with patch.object(CalcRange, "to_numpy", side_effect=ImportError("No module named numpy")):
        with pytest.raises(TypeError, match="Multi-cell arithmetic requires NumPy"):
            _ = multi + 10
        with pytest.raises(TypeError, match="Multi-cell arithmetic requires NumPy"):
            _ = -multi
        with pytest.raises(TypeError, match="Multi-cell arithmetic requires NumPy"):
            _ = multi == 10


def test_tall_sheet_materialize_and_arithmetic_do_not_precontract():
    """A column past SHAPE_DIM is a Calc range. Deal must not assert."""
    import deal

    from plugin.framework.deal_shim import DEAL_MAX_SHAPE_DIM
    from plugin.scripting.calc_range import CalcRange, materialize_inputs

    rows = [[i] for i in range(DEAL_MAX_SHAPE_DIM + 1)]
    ranges = materialize_inputs(rows)
    assert len(ranges) == 1
    assert ranges[0].shape == (DEAL_MAX_SHAPE_DIM + 1, 1)
    labeled = dataframe_to_labeled_grid(["n" * 80], rows, include_header=False)
    assert len(labeled) == DEAL_MAX_SHAPE_DIM + 1
    try:
        _ = CalcRange(rows) + 1
    except deal.PreContractError:
        raise
    except TypeError:
        return

