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


def test_grid_to_dataframe_header_row_none():
    result = grid_to_dataframe([[1, 2], [3, 4]], header_row=None)
    assert list(result.df.columns) == ["col_0", "col_1"]
    assert len(result.df) == 2
