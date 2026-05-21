# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""Tests for payload_codec (host stdlib / child NumPy wire format)."""

from __future__ import annotations

import ast
import math
from pathlib import Path

import pytest

from plugin.scripting import payload_codec
from plugin.scripting.payload_codec import (
    describe_wire_value,
    BINARY_MIN_CELLS,
    PAYLOAD_F64_BLOB,
    child_pack_result,
    child_unpack_data,
    host_pack_data,
    host_unpack_data,
    is_numeric_grid,
    should_use_binary_envelope,
    wire_cell_count,
)
from plugin.tests.testing_utils import setup_uno_mocks

setup_uno_mocks()


def test_host_module_does_not_import_numpy_at_module_level():
    """Host path must stay NumPy-free at import time (ABI / LO embedded Python)."""
    src = Path(payload_codec.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.startswith("numpy"), alias.name
        elif isinstance(node, ast.ImportFrom) and node.module:
            assert not node.module.startswith("numpy"), node.module


def test_should_use_binary_threshold_min_cells():
    assert should_use_binary_envelope((3, 3), force="auto") is False  # 9 cells
    assert should_use_binary_envelope((4, 3), force="auto") is True  # 12 cells
    assert should_use_binary_envelope((4, 4), force="auto") is True  # 16 cells
    assert should_use_binary_envelope((9,), force="auto") is False  # 9 cells
    assert should_use_binary_envelope((10,), force="auto") is True  # 10 cells
    assert should_use_binary_envelope((4, 4), force="never") is False
    assert should_use_binary_envelope((3, 3), force="always") is True


def test_host_pack_auto_uses_blob_for_4x3():
    grid = [[1.0, 4.0, 5.0], [23.0, 4.0, 4.0], [5.0, 4.0, 4.0], [4.0, 5.0, 4.0]]
    wire = host_pack_data(grid, force="auto")
    assert isinstance(wire, dict)
    assert wire["__wa_payload__"] == PAYLOAD_F64_BLOB
    assert wire["shape"] == [4, 3]


def test_host_pack_auto_uses_list_for_3x3():
    grid = [[1.0, 2.0, 3.0], [4.0, 5.0, 6.0], [7.0, 8.0, 9.0]]
    wire = host_pack_data(grid, force="auto")
    assert isinstance(wire, list)
    assert wire[0][0] == 1.0


def test_host_pack_auto_uses_blob_for_4x4():
    grid = [[float(i)] * 4 for i in range(4)]
    wire = host_pack_data(grid, force="auto")
    assert isinstance(wire, dict)
    assert wire["__wa_payload__"] == PAYLOAD_F64_BLOB
    assert wire["shape"] == [4, 4]


def test_round_trip_host_blob_child_ndarray():
    np = pytest.importorskip("numpy")
    grid = [[1.0, 2.0], [3.0, 4.0], [5.0, 6.0], [7.0, 8.0]]
    wire = host_pack_data(grid, force="always")
    arr = child_unpack_data(wire)
    assert isinstance(arr, np.ndarray)
    assert arr.shape == (4, 2)
    assert arr[0, 0] == pytest.approx(1.0)
    assert arr[3, 1] == pytest.approx(8.0)


def test_round_trip_child_blob_host_list():
    np = pytest.importorskip("numpy")
    arr = np.arange(12, dtype=np.float64).reshape(3, 4)
    wire = child_pack_result(arr, force="always")
    back = host_unpack_data(wire, as_nested_list=True)
    assert len(back) == 3
    assert len(back[0]) == 4
    assert back[0][0] == pytest.approx(0.0)
    assert back[2][3] == pytest.approx(11.0)


def test_none_becomes_nan_in_blob():
    np = pytest.importorskip("numpy")
    wire = host_pack_data([1.0, None, 3.0], force="always")
    arr = child_unpack_data(wire)
    assert math.isnan(float(arr[1]))


def test_scalar_egress_stays_json():
    wire = child_pack_result(42.5, force="auto")
    assert wire == 42.5


def test_is_numeric_grid_rejects_text():
    assert is_numeric_grid([1.0, "hello"]) is False
    assert is_numeric_grid([[1.0, 2.0], [3.0, 4.0]]) is True


def test_describe_wire_value_f64_blob():
    wire = host_pack_data([[1.0] * 4 for _ in range(4)], force="always")
    desc = describe_wire_value(wire)
    assert "f64_blob" in desc
    assert "shape=[4, 4]" in desc


def test_wire_cell_count_blob():
    wire = host_pack_data([[1.0] * 4 for _ in range(4)], force="always")
    assert wire_cell_count(wire) == 16


def test_child_list_path_array():
    np = pytest.importorskip("numpy")
    wire = host_pack_data([1.0, 2.0, 3.0], force="never")
    arr = child_unpack_data(wire)
    assert list(arr) == pytest.approx([1.0, 2.0, 3.0])


def test_host_pack_column_grid_mixed():
    """Verify that a 2D mixed grid is packed column-wise with both binary and JSON columns."""
    grid = [
        [1.0, "apple", 10.0],
        [2.0, "banana", 20.0],
        [3.0, "cherry", 30.0],
        [4.0, "date", 40.0]
    ]
    # Use force="always" to trigger it regardless of threshold
    wire = host_pack_data(grid, force="always")
    assert isinstance(wire, dict)
    assert wire["__wa_payload__"] == payload_codec.PAYLOAD_COLUMN_GRID
    assert wire["shape"] == [4, 3]
    assert len(wire["columns"]) == 3
    
    # Col 0 (numeric) should be f64_blob
    assert wire["columns"][0]["type"] == PAYLOAD_F64_BLOB
    # Col 1 (text) should be json_list
    assert wire["columns"][1]["type"] == "json_list"
    assert wire["columns"][1]["data"] == ["apple", "banana", "cherry", "date"]
    # Col 2 (numeric) should be f64_blob
    assert wire["columns"][2]["type"] == PAYLOAD_F64_BLOB


def test_round_trip_column_grid():
    """Verify that column_grid payload round-trips correctly and reconstructs exact values."""
    pytest.importorskip("numpy")
    grid = [
        [1.5, "apple", 10.1],
        [2.5, "banana", 20.2],
        [3.5, "cherry", None],
        [4.5, "", 40.4]
    ]
    wire = host_pack_data(grid, force="always")
    reconstructed = child_unpack_data(wire)
    
    assert isinstance(reconstructed, list)
    assert len(reconstructed) == 4
    assert reconstructed[0] == [1.5, "apple", 10.1]
    assert reconstructed[1] == [2.5, "banana", 20.2]
    # None/empty cells should round-trip correctly
    assert reconstructed[2] == [3.5, "cherry", None]
    assert reconstructed[3] == [4.5, "", 40.4]


def test_column_grid_non_2d_fallback():
    """Verify that non-2D mixed grids or small mixed grids fallback correctly to standard lists."""
    # 1D mixed grid
    grid_1d = [1.0, "apple", 3.0]
    wire_1d = host_pack_data(grid_1d, force="always")
    assert isinstance(wire_1d, list)
    assert wire_1d == [1.0, "apple", 3.0]
    
    # 2D mixed grid but with force="never"
    grid_2d = [
        [1.0, "apple"],
        [2.0, "banana"]
    ]
    wire_2d = host_pack_data(grid_2d, force="never")
    assert isinstance(wire_2d, list)
    assert wire_2d == [[1.0, "apple"], [2.0, "banana"]]
