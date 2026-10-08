# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""Tests for payload_codec (host stdlib / child NumPy wire format).

Sections: policy threshold, host pack/unpack, child pack/unpack, round-trips, NaN/missing,
realistic Calc-shaped grids only (rectangular 2D; uneven row lengths are rejected at pack).
"""

from __future__ import annotations

import array
import ast
import math
import sys
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import pytest

from plugin.scripting import payload_codec
from plugin.scripting.payload_codec import (
    BINARY_MIN_CELLS,
    PAYLOAD_CALC_RANGE,
    PAYLOAD_DATAFRAME,
    PAYLOAD_MULTI_DATA,
    PAYLOAD_SPLIT_GRID,
    binary_envelope_skip_reason,
    child_pack_result,
    child_unpack_data,
    child_unpack_split_grid,
    describe_wire_value,
    host_pack_data,
    host_pack_multi_data,
    host_unpack_data,
    host_unpack_split_grid,
    is_dataframe_payload,
    is_multi_data,
    is_numeric_coercible,
    is_numeric_grid,
    is_split_grid,
    should_use_binary_envelope,
    wire_cell_count,
)
from tests.scripting.payload_codec_test_support import (
    MIXED_LABEL_GRID,
    MIXED_WITH_ZIP,
    NUMERIC_4X4,
    NUMERIC_AT_THRESHOLD,
    NUMERIC_BELOW_THRESHOLD,
    pickle5_roundtrip,
    rect_shape_for_cell_count,
)
from tests.scripting.serialization_ab_support import cython_accelerator_context


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


def test_flatten_grid_return_parameterizes_array() -> None:
    """Bare array.array leftovers trip reportMissingTypeArgument."""
    from plugin.scripting.payload_codec import _flatten_grid_to_components

    assert _flatten_grid_to_components.__annotations__["return"] == "tuple[array.array[float], dict[int, str], list[str], list[int]]"


@pytest.mark.parametrize(
    ("ncells", "force", "expected"),
    [
        (BINARY_MIN_CELLS - 1, "auto", False),
        (BINARY_MIN_CELLS, "auto", True),
        (BINARY_MIN_CELLS - 1, "never", False),
        (BINARY_MIN_CELLS - 1, "always", True),
    ],
)
def test_should_use_binary_envelope_boundary(ncells: int, force: str, expected: bool) -> None:
    """Default policy: below BINARY_MIN_CELLS uses nested lists; at/above uses split_grid when force=auto."""
    rows, cols = rect_shape_for_cell_count(ncells)
    shape = (rows, cols)
    assert should_use_binary_envelope(shape, force=force) is expected


def test_should_use_binary_envelope_1d_boundary() -> None:
    assert should_use_binary_envelope((BINARY_MIN_CELLS - 1,), force="auto") is False
    assert should_use_binary_envelope((BINARY_MIN_CELLS,), force="auto") is True


def test_binary_envelope_skip_reason_below_threshold() -> None:
    """Policy helper explains why a grid below BINARY_MIN_CELLS skips split_grid."""
    n = BINARY_MIN_CELLS - 1
    rows, cols = rect_shape_for_cell_count(n)
    reason = binary_envelope_skip_reason((rows, cols), force="auto")
    assert reason is not None
    assert str(BINARY_MIN_CELLS) in reason


def test_host_pack_auto_uses_split_grid_for_large_rect():
    """Auto policy uses split_grid when cell count >= BINARY_MIN_CELLS."""
    grid = NUMERIC_AT_THRESHOLD
    wire = host_pack_data(grid, force="auto")
    assert isinstance(wire, dict)
    assert wire["__wa_payload__"] == PAYLOAD_SPLIT_GRID
    rows, cols = rect_shape_for_cell_count(BINARY_MIN_CELLS)
    assert wire["shape"] == [rows, cols]


def test_host_pack_auto_uses_list_for_3x3():
    grid = [[1.0, 2.0, 3.0], [4.0, 5.0, 6.0], [7.0, 8.0, 9.0]]
    wire = host_pack_data(grid, force="auto")
    assert isinstance(wire, list)
    assert wire[0][0] == 1.0


def test_round_trip_host_split_grid_child_ndarray():
    np = pytest.importorskip("numpy")
    grid = [[1.0, 2.0], [3.0, 4.0], [5.0, 6.0], [7.0, 8.0]]
    wire = host_pack_data(grid, force="always")
    assert wire["__wa_payload__"] == PAYLOAD_SPLIT_GRID
    arr = child_unpack_data(wire)
    assert isinstance(arr, np.ndarray)
    assert not isinstance(arr, list)  # perf sentinel: pure-numeric split_grid must not regress to list materialization
    assert arr.shape == (4, 2)
    assert arr[0, 0] == pytest.approx(1.0)
    assert arr[3, 1] == pytest.approx(8.0)


def test_round_trip_child_split_grid_host_list():
    np = pytest.importorskip("numpy")
    arr = np.arange(12, dtype=np.float64).reshape(3, 4)
    wire = child_pack_result(arr, force="always")
    assert wire["__wa_payload__"] == PAYLOAD_SPLIT_GRID
    back = host_unpack_data(wire, as_nested_list=True)
    assert len(back) == 3
    assert len(back[0]) == 4
    assert back[0][0] == pytest.approx(0.0)
    assert back[2][3] == pytest.approx(11.0)


def test_column_kinds_from_cell_types():
    from plugin.scripting.payload_codec import column_kinds_for_grid

    assert column_kinds_for_grid([[100, 541], [101, 547]]) == ["int", "int"]
    assert column_kinds_for_grid([[100, 1.5], [101, 2.5]]) == ["int", "float"]
    assert column_kinds_for_grid([[1.5, 2.0]]) == ["float", "float"]
    assert column_kinds_for_grid([[1, None]]) == ["int", "float"]
    assert column_kinds_for_grid([[1, "x"]]) == ["int", "float"]
    assert column_kinds_for_grid([["x"], ["y"]]) == ["float"]


def test_uniform_unpack_uses_full_column_kinds_on_wire():
    """Fast decode path must not require a shortened wire tag; column_kinds stays per-column."""
    from plugin.scripting.payload_codec import envelope_uniform_column_kind

    grid = [[100, 541], [101, 547], [102, 557], [103, 563], [104, 569], [105, 571], [106, 577]]
    wire = host_pack_data(grid, force="always")
    assert wire["column_kinds"] == ["int", "int"]
    assert "uniform_column_kind" not in wire
    assert envelope_uniform_column_kind(wire, ncols=2) == "int"


def test_host_unpack_restores_integer_grid():
    grid = [[100, 541], [101, 547], [102, 557], [103, 563], [104, 569], [105, 571], [106, 577]]
    wire = host_pack_data(grid, force="always")
    assert wire["dtype"] == "float64"
    assert wire["column_kinds"] == ["int", "int"]
    back = host_unpack_data(wire, as_nested_list=True)
    assert back == grid
    assert all(isinstance(cell, int) for row in back for cell in row)


def test_host_unpack_mixed_int_float_columns():
    grid = [[100, 1.5], [101, 2.5], [102, 3.5], [103, 4.5], [104, 5.5]]
    wire = host_pack_data(grid, force="always")
    assert wire["column_kinds"] == ["int", "float"]
    back = host_unpack_data(wire, as_nested_list=True)
    assert back[0] == [100, 1.5]
    assert isinstance(back[0][0], int)
    assert isinstance(back[0][1], float)
    assert back[1][0] == 101


def test_child_pack_integer_ndarray_sets_column_kinds():
    np = pytest.importorskip("numpy")
    from plugin.scripting.payload_codec import child_pack_result

    wire = child_pack_result(np.arange(12, dtype=np.int64).reshape(3, 4), force="always")
    assert wire["dtype"] == "float64"
    assert wire["column_kinds"] == ["int", "int", "int", "int"]
    back = host_unpack_data(wire, as_nested_list=True)
    assert back[0][0] == 0
    assert isinstance(back[0][0], int)


def test_child_pack_bool_ndarray_sets_column_kinds():
    np = pytest.importorskip("numpy")
    from plugin.scripting.payload_codec import child_pack_split_grid

    wire = child_pack_split_grid(np.array([[True, False], [False, True]]))
    assert wire["column_kinds"] == ["bool", "bool"]
    back = host_unpack_data(wire, as_nested_list=True)
    assert back == [[True, False], [False, True]]


def test_none_becomes_nan_in_split_grid():
    pytest.importorskip("numpy")
    wire = host_pack_data([[1.0, None, 3.0]], force="always")
    arr = child_unpack_data(wire)
    assert arr.shape == (1, 3)
    assert math.isnan(float(arr[0, 1]))


def test_child_pack_plain_python_without_numpy() -> None:
    """Plain Python results serialize when NumPy is unavailable.

    ``sys.modules['numpy'] = None`` is how a later ``import numpy`` fails
    (``ModuleNotFoundError``), without intercepting every other import.
    """
    result = {"changes": [["pays", "paid"]]}
    with patch.dict(sys.modules, {"numpy": None}):
        assert child_pack_result(result, force="auto") == result


def test_child_unpack_plain_python_without_numpy() -> None:
    """Inbound lists and dicts materialize when NumPy is unavailable.

    A numeric list stays a list: there is no ndarray to build. ``split_grid``
    still needs NumPy and is not covered here.
    """
    text = [["pays", "paid"]]
    numbers = [1.0, 2.0]
    with patch.dict(sys.modules, {"numpy": None}):
        assert child_unpack_data(text) == text
        assert child_unpack_data({"changes": text}) == {"changes": text}
        assert child_unpack_data(numbers) == numbers


def test_scalar_egress_stays_json():
    wire = child_pack_result(42.5, force="auto")
    assert wire == 42.5


def test_is_numeric_grid_rejects_text():
    assert is_numeric_grid([1.0, "hello"]) is False
    assert is_numeric_grid([[1.0, 2.0], [3.0, 4.0]]) is True


def test_describe_wire_value_split_grid():
    wire = host_pack_data([[1.0] * 4 for _ in range(4)], force="always")
    desc = describe_wire_value(wire)
    assert "split_grid" in desc
    assert "shape=[4, 4]" in desc


def test_wire_cell_count_split_grid():
    wire = host_pack_data([[1.0] * 4 for _ in range(4)], force="always")
    assert wire_cell_count(wire) == 16


def test_child_list_path_array():
    pytest.importorskip("numpy")
    wire = host_pack_data([1.0, 2.0, 3.0], force="never")
    arr = child_unpack_data(wire)
    assert list(arr) == pytest.approx([1.0, 2.0, 3.0])


def test_host_pack_split_grid_mixed():
    """Verify that a 2D mixed grid is packed using Split-Grid serialization."""
    grid = [
        [1.0, "apple", 10.0],
        [2.0, "banana", 20.0],
        [3.0, "cherry", 30.0],
        [4.0, "date", 40.0]
    ]
    # Use force="always" to trigger it regardless of threshold
    wire = host_pack_data(grid, force="always")
    assert isinstance(wire, dict)
    assert wire["__wa_payload__"] == payload_codec.PAYLOAD_SPLIT_GRID
    assert wire["shape"] == [4, 3]
    assert "strings" in wire
    assert wire["strings"] == {
        1: "apple",
        4: "banana",
        7: "cherry",
        10: "date",
    }


def test_round_trip_split_grid():
    """Verify that split_grid payload round-trips correctly and reconstructs exact values."""
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


def test_split_grid_non_2d_fallback():
    """Verify that grids/lists fallback correctly when force="never"."""
    # 1D mixed grid fallback
    grid_1d = [1.0, "apple", 3.0]
    wire_1d = host_pack_data(grid_1d, force="never")
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


def test_round_trip_split_grid_1d():
    """Verify that both numeric and mixed 1D flat lists round-trip flawlessly under split_grid."""
    np = pytest.importorskip("numpy")
    
    # Numeric 1D flat list
    grid_num_1d = [1.5, 2.5, 3.5, 4.5]
    wire_num = host_pack_data(grid_num_1d, force="always")
    assert isinstance(wire_num, dict)
    assert wire_num["__wa_payload__"] == PAYLOAD_SPLIT_GRID
    assert wire_num["shape"] == [4]
    
    # Unpack in child -> should be purely numeric ndarray
    child_unpacked_num = child_unpack_data(wire_num)
    assert isinstance(child_unpacked_num, np.ndarray)
    assert child_unpacked_num.shape == (4,)
    assert list(child_unpacked_num) == pytest.approx(grid_num_1d)
    
    # Pack result in child -> should pack 1D array as split_grid
    wire_child_num = child_pack_result(child_unpacked_num, force="always")
    assert wire_child_num["__wa_payload__"] == PAYLOAD_SPLIT_GRID
    assert wire_child_num["shape"] == [4]
    
    # Unpack on host -> should return a flat list
    host_unpacked_num = host_unpack_data(wire_child_num, as_nested_list=True)
    assert isinstance(host_unpacked_num, list)
    assert host_unpacked_num == pytest.approx(grid_num_1d)
    
    # Mixed 1D flat list
    grid_mixed_1d = [1.5, "banana", None, 4.5]
    wire_mixed = host_pack_data(grid_mixed_1d, force="always")
    assert isinstance(wire_mixed, dict)
    assert wire_mixed["__wa_payload__"] == PAYLOAD_SPLIT_GRID
    assert wire_mixed["shape"] == [4]
    assert wire_mixed["strings"] == {1: "banana"}
    
    # Unpack in child -> reconstructed mixed list
    child_unpacked_mixed = child_unpack_data(wire_mixed)
    assert isinstance(child_unpacked_mixed, list)
    assert child_unpacked_mixed == [1.5, "banana", None, 4.5]
    
    # Pack result in child -> pack 1D mixed list
    wire_child_mixed = child_pack_result(child_unpacked_mixed, force="always")
    assert wire_child_mixed["__wa_payload__"] == PAYLOAD_SPLIT_GRID
    assert wire_child_mixed["shape"] == [4]
    assert wire_child_mixed["strings"] == {1: "banana"}
    
    # Unpack on host -> flat list.
    # A Python None in the list (hole) was packed via split_grid (nan in buffer, no strings entry).
    # With the egress policy, host unpack now preserves it as nan (Calc will show error for that slot).
    import math
    host_unpacked_mixed = host_unpack_data(wire_child_mixed, as_nested_list=True)
    assert host_unpacked_mixed[0] == 1.5
    assert host_unpacked_mixed[1] == "banana"
    assert math.isnan(host_unpacked_mixed[2])
    assert host_unpacked_mixed[3] == 4.5


def test_child_unpack_single_entry_auto_scalar_and_integer_coercion():
    """Single-cell floats stay floats. A longer float64 column already did; 1×1 used to become int."""
    np = pytest.importorskip("numpy")

    # 1. 1-element numeric list representing a whole-number float
    wire_int_float = [100000.0]
    unpacked_int_float = child_unpack_data(wire_int_float)
    assert isinstance(unpacked_int_float, float)
    assert unpacked_int_float == 100000.0

    # 2. 1-element numeric list representing a real float
    wire_real_float = [3.14]
    unpacked_real_float = child_unpack_data(wire_real_float)
    assert isinstance(unpacked_real_float, float)
    assert unpacked_real_float == pytest.approx(3.14)

    # 3. 1-element string list
    wire_str = ["hello"]
    unpacked_str = child_unpack_data(wire_str)
    assert isinstance(unpacked_str, str)
    assert unpacked_str == "hello"

    # 4. 1-element boolean list
    wire_bool = [True]
    unpacked_bool = child_unpack_data(wire_bool)
    assert isinstance(unpacked_bool, bool)
    assert unpacked_bool is True

    # 5. 1-element numpy array representing a whole-number float
    arr_int_float = np.array([100000.0])
    unpacked_arr_int_float = child_unpack_data(arr_int_float)
    assert isinstance(unpacked_arr_int_float, float)
    assert unpacked_arr_int_float == 100000.0

    # 6. Multi-element list or 2D list should NOT be unpacked to scalar
    assert isinstance(child_unpack_data([100000.0, 200000.0]), np.ndarray)
    assert child_unpack_data([[100000.0]]) == [[100000.0]]  # 2D list preserved


def test_iter_split_grid_cells_row_major_order() -> None:
    """Row-major (col_idx, flat_idx, val) order for 2D and 1D split-grid flatten iterators."""
    from plugin.scripting.payload_codec import _iter_split_grid_cells

    grid_2d = [[10, 11, 12], [20, 21, 22]]
    assert list(_iter_split_grid_cells(grid_2d, is_2d=True)) == [
        (0, 0, 10),
        (1, 1, 11),
        (2, 2, 12),
        (0, 3, 20),
        (1, 4, 21),
        (2, 5, 22),
    ]

    grid_1d = [10, 11, 12]
    assert list(_iter_split_grid_cells(grid_1d, is_2d=False)) == [
        (0, 0, 10),
        (0, 1, 11),
        (0, 2, 12),
    ]


def test_uneven_row_lengths_rejected_on_host_pack() -> None:
    """Uneven nested-list rows are unsupported; Calc ranges are always rectangular."""
    with pytest.raises(ValueError, match="Uneven row lengths"):
        host_pack_data([[1, 2], [3]], force="always")


def test_non_sequence_row_raises_valueerror() -> None:
    """A scalar or string row is ValueError, not TypeError from len().

    Deal's pre rejects the grid before the body. The release build strips
    deal, and that path used to raise TypeError.
    """
    from tests.harness.strip_bundle import expect_pre_or_body

    with pytest.raises(ValueError, match="not a list or tuple"):
        payload_codec._split_grid_row_width(3)
    with pytest.raises(ValueError, match="not a list or tuple"):
        payload_codec._validate_rectangular_grid([[1, 2], 3], 2)
    with pytest.raises(ValueError, match="not a list or tuple"):
        payload_codec._validate_rectangular_grid([[1, 2], "ab"], 2)
    for grid in ([[1, 2], 3], [[1, 2], "ab"]):
        expect_pre_or_body(lambda grid=grid: host_pack_data(grid), body_exc=ValueError)
        expect_pre_or_body(
            lambda grid=grid: host_pack_data(grid, force="always"), body_exc=ValueError
        )


def test_column_kinds_for_grid_jagged_raises() -> None:
    """The kinds helper used to return [] and hide the flatten ValueError."""
    with pytest.raises(ValueError, match="Uneven row lengths"):
        payload_codec.column_kinds_for_grid([[1, 2], [3]])


def test_envelope_detectors_reject_bool_shape_dims() -> None:
    """bool is an int subclass. True used to pass as a shape extent."""
    from plugin.scripting.payload_codec import is_calc_range_payload

    assert is_split_grid(
        {
            "__wa_payload__": PAYLOAD_SPLIT_GRID,
            "shape": [True, 1],
            "buffer": b"\x00" * 8,
        }
    ) is False
    assert is_calc_range_payload(
        {
            "__wa_payload__": PAYLOAD_CALC_RANGE,
            "shape": [1, False],
            "data": [[1.0]],
        }
    ) is False
    assert is_split_grid(
        {
            "__wa_payload__": PAYLOAD_SPLIT_GRID,
            "shape": [0, 0],
            "buffer": b"",
        }
    ) is True


# --- NaN, empty cells, and inf (realistic Calc / NumPy paths) ---


def test_none_cell_pack_produces_nan_in_buffer() -> None:
    """Calc empty cell (None) encodes as NaN in the split_grid float64 buffer."""
    grid = [[1.0, None, 3.0, 4.0], [5.0, 6.0, 7.0, 8.0], [9.0, 10.0, 11.0, 12.0]]
    wire = host_pack_data(grid, force="always")
    assert wire["__wa_payload__"] == PAYLOAD_SPLIT_GRID
    import array

    buf = array.array("d")
    buf.frombytes(wire["buffer"])
    assert math.isnan(buf[1])


def test_none_numeric_ingress_child_gets_np_nan() -> None:
    """Numeric-only ingress: empty Calc cells become np.nan in child ndarray, not Python None."""
    np = pytest.importorskip("numpy")
    grid = [[1.0, None, 3.0, 4.0], [5.0, 6.0, None, 8.0], [9.0, 10.0, 11.0, 12.0]]
    arr = child_unpack_data(host_pack_data(grid, force="always"))
    assert isinstance(arr, np.ndarray)
    assert not isinstance(arr, list)  # perf sentinel: pure-numeric split_grid fast path must return ndarray (not list-of-lists from tolist)
    assert np.isnan(arr[0, 1])
    assert arr[0, 0] == pytest.approx(1.0)


def test_none_mixed_ingress_child_gets_python_none() -> None:
    """Mixed grid ingress: empty cells become None in the nested list (not np.nan)."""
    pytest.importorskip("numpy")
    grid = [[1.0, None, "label"], [2.0, 3.0, "x"]] * 2  # 12 cells, rectangular
    out = child_unpack_data(host_pack_data(grid, force="always"))
    assert isinstance(out, list)
    assert out[0][1] is None


def test_nan_egress_child_pack_host_unpack() -> None:
    """NumPy result with np.nan: host unpack preserves NaN (it becomes a Calc error on =PYTHON() egress)."""
    np = pytest.importorskip("numpy")
    import math
    wire = child_pack_result(np.array([1.0, np.nan, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0]), force="always")
    back = host_unpack_data(wire, as_nested_list=True)
    assert back[0] == pytest.approx(1.0)
    assert math.isnan(back[1])


def test_none_host_egress_round_trip() -> None:
    """Rectangular numeric grid with holes: host pack -> child ndarray (nan) -> host list preserves nan (Calc will show error).

    We no longer coerce buffer NaN back to Python None on host unpack. A Calc blank that flows through
    a pure-numeric range becomes nan on egress and surfaces as a Calc error (by design).
    """
    np = pytest.importorskip("numpy")
    import math
    grid = [[1.0, None, 3.0, 4.0], [5.0, 6.0, None, 8.0], [9.0, 10.0, 11.0, 12.0]]
    wire = host_pack_data(grid, force="always")
    arr = child_unpack_data(wire)
    assert isinstance(arr, np.ndarray)
    back = host_unpack_data(wire, as_nested_list=True)
    assert math.isnan(back[0][1])
    assert math.isnan(back[1][2])


def test_inf_egress_from_numpy_result() -> None:
    """np.inf in worker results is not collapsed to None on host unpack."""
    np = pytest.importorskip("numpy")
    vals = [1.0, float("inf"), -float("inf"), 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0]
    wire = child_pack_result(np.array(vals, dtype=np.float64), force="always")
    back = host_unpack_data(wire, as_nested_list=True)
    assert back[1] == float("inf")
    assert back[2] == float("-inf")


def test_pickle5_roundtrip_preserves_nan_buffer() -> None:
    """IPC Pickle5 must preserve raw buffer bytes including NaN slots."""
    grid = [[1.0, None, 3.0, 4.0], [5.0, 6.0, 7.0, 8.0], [9.0, 10.0, 11.0, 12.0]]
    wire = pickle5_roundtrip(host_pack_data(grid, force="always"))
    np = pytest.importorskip("numpy")
    arr = child_unpack_data(wire)
    assert isinstance(arr, np.ndarray)
    assert np.isnan(arr[0, 1])


def test_mixed_grid_preserves_zip_code_strings() -> None:
    """Zip-style text must stay in strings map, not be coerced to float."""
    wire = host_pack_data(MIXED_WITH_ZIP, force="always")
    assert wire["strings"][1] == "02138"
    pytest.importorskip("numpy")
    out = child_unpack_data(wire)
    assert out[0][1] == "02138"


def test_mixed_grid_preserves_non_numeric_string() -> None:
    """Non-coercible text stays a string; numeric-looking text that fails float() is kept."""
    grid = [[1.0, "hello", "3.14z", 4.0]] * 3  # 12 cells
    wire = host_pack_data(grid, force="always")
    assert "hello" in wire["strings"].values()
    pytest.importorskip("numpy")
    out = child_unpack_data(wire)
    assert out[0][1] == "hello"


def test_whitespace_only_cell_does_not_crash_child_unpack() -> None:
    """Pasted '   ' is numeric-coercible for Calc but np.float64 cannot convert it."""
    pytest.importorskip("numpy")
    grid = [[1.0, "   ", 3.0, 4.0], [5.0, 6.0, 7.0, 8.0], [9.0, 10.0, 11.0, 12.0]]
    out = child_unpack_data(host_pack_data(grid, force="always"))
    assert isinstance(out, list)
    assert out[0][1] == "   "


def test_empty_string_mixed_split_grid_does_not_crash_child_unpack() -> None:
    """Bare '' on mixed split_grid must not raise (Calc usually maps '' to None first)."""
    pytest.importorskip("numpy")
    grid = [[1.0, "", 3.0, 4.0], [5.0, 6.0, 7.0, 8.0], [9.0, 10.0, 11.0, 12.0]]
    out = child_unpack_data(host_pack_data(grid, force="always"))
    assert isinstance(out, list)
    assert out[0][1] == ""


def test_mixed_grid_real_nan_becomes_none_on_child() -> None:
    """Documented: mixed-grid ingress has no blank-vs-NaN wire bit."""
    pytest.importorskip("numpy")
    grid = [[1.0, float("nan")], ["label", 4.0]]
    out = child_unpack_data(host_pack_data(grid, force="always"))
    assert out[0][1] is None
    assert out[1][0] == "label"


def test_decimal_split_grid_stays_float_not_truncated_int() -> None:
    """stdlib flatten must label Decimal columns float (Cython already did)."""
    pytest.importorskip("numpy")
    grid = [[Decimal("1.5"), Decimal("2.25")], [Decimal("3.0"), Decimal("4.75")]]
    with cython_accelerator_context(enabled=False):
        out = child_unpack_data(host_pack_data(grid, force="always"))
    assert out[0][0] == pytest.approx(1.5)
    assert out[0][1] == pytest.approx(2.25)


def test_decimal_fraction_encoding_ignores_earlier_text() -> None:
    """Decimal and Fraction stay floats after a text cell, same as before one."""
    from fractions import Fraction

    from plugin.scripting.payload_codec import host_pack_split_grid

    def _check() -> None:
        after_text = host_unpack_split_grid(
            host_pack_split_grid([["02138", Decimal("1.25"), Fraction(1, 4)]])
        )
        assert after_text[0][0] == "02138"
        assert after_text[0][1] == pytest.approx(1.25)
        assert after_text[0][2] == pytest.approx(0.25)
        assert type(after_text[0][1]) is float
        assert type(after_text[0][2]) is float

        before_text = host_unpack_split_grid(
            host_pack_split_grid([[Decimal("1.25"), Fraction(1, 4), "02138"]])
        )
        assert before_text[0][0] == pytest.approx(1.25)
        assert before_text[0][1] == pytest.approx(0.25)
        assert before_text[0][2] == "02138"
        assert type(before_text[0][0]) is type(after_text[0][1])
        assert type(before_text[0][1]) is type(after_text[0][2])

        wire = host_pack_split_grid(
            [["label", "x"], [Decimal("1.50"), Fraction(1, 4)]]
        )
        assert "1.50" not in wire["strings"].values()
        assert "1/4" not in wire["strings"].values()
        same_col = host_unpack_split_grid(wire)
        assert same_col[0] == ["label", "x"]
        assert same_col[1][0] == pytest.approx(1.5)
        assert same_col[1][1] == pytest.approx(0.25)
        assert wire["column_kinds"] == ["float", "float"]

    with cython_accelerator_context(enabled=False):
        _check()
    if payload_codec.fast_flatten_grid_2d is not None:
        _check()


class _ObjectKind:
    kind = "O"


class _WeirdFloat:
    """dtype.kind 'O' whose float() succeeds. str() is 'W' so a stringify shows up."""

    dtype = _ObjectKind()

    def __float__(self) -> float:
        return 1.5

    def __str__(self) -> str:
        return "W"


def test_unknown_dtype_kind_promotes_column_like_cython() -> None:
    """kind 'O' that float() accepts is a float column on both packers.

    The pure path used to leave the column bool while Cython set float.
    """
    from plugin.scripting.payload_codec import column_kinds_for_grid

    grid = [[True, 0], [_WeirdFloat(), 0]]
    with cython_accelerator_context(enabled=False):
        assert column_kinds_for_grid(grid) == ["float", "int"]
    if payload_codec.fast_flatten_grid_2d is not None:
        assert column_kinds_for_grid(grid) == ["float", "int"]


def test_unknown_dtype_kind_numeric_regardless_of_string_position() -> None:
    """The same object is a float before or after a text cell.

    The slow path used to str() an unknown dtype kind, so ['x', Weird()]
    became the text 'W' while [Weird(), 'x'] stayed 1.5.
    """
    from plugin.scripting.payload_codec import host_pack_split_grid

    weird = _WeirdFloat()

    def _check() -> None:
        before = host_unpack_split_grid(host_pack_split_grid([[weird, "x"]]))
        assert before[0][0] == pytest.approx(1.5)
        assert type(before[0][0]) is float
        assert before[0][1] == "x"
        after_wire = host_pack_split_grid([["x", weird]])
        assert "W" not in after_wire["strings"].values()
        after = host_unpack_split_grid(after_wire)
        assert after[0][0] == "x"
        assert after[0][1] == pytest.approx(1.5)
        assert type(after[0][1]) is float

    with cython_accelerator_context(enabled=False):
        _check()
    if payload_codec.fast_flatten_grid_2d is not None:
        _check()


def test_numpy_str_stays_text_on_split_grid() -> None:
    """Unicode scalars stay text. float() must not eat a zip-code-like np.str_."""
    np = pytest.importorskip("numpy")
    from plugin.scripting.payload_codec import host_pack_split_grid

    def _check() -> None:
        wire = host_pack_split_grid([[np.str_("02138")]])
        assert "02138" in wire["strings"].values()
        assert host_unpack_split_grid(wire) == [["02138"]]

    with cython_accelerator_context(enabled=False):
        _check()
    if payload_codec.fast_flatten_grid_2d is not None:
        _check()


def test_wide_sheet_above_shape_dim_unpacks() -> None:
    """Pack accepts real sheet widths; unpack must not cap columns at SHAPE_DIM."""
    import deal

    from plugin.framework.deal_shim import DEAL_MAX_COL_INDEX, DEAL_MAX_SHAPE_DIM
    from plugin.scripting.payload_codec import envelope_column_kinds
    from tests.harness.strip_bundle import deal_pre_present

    ncols = DEAL_MAX_SHAPE_DIM + 1
    grid = [[float(i) for i in range(ncols)]]
    wire = host_pack_data(grid, force="auto")
    assert is_split_grid(wire)
    assert wire["shape"] == [1, ncols]
    unpacked = host_unpack_data(wire)
    assert len(unpacked) == 1
    assert len(unpacked[0]) == ncols
    assert unpacked[0][0] == pytest.approx(0.0)
    assert unpacked[0][-1] == pytest.approx(float(ncols - 1))

    np = pytest.importorskip("numpy")
    arr = child_unpack_data(wire)
    assert isinstance(arr, np.ndarray)
    assert arr.shape == (1, ncols)
    assert float(arr[0, -1]) == pytest.approx(float(ncols - 1))

    cap = DEAL_MAX_COL_INDEX + 1
    kinds = envelope_column_kinds({"column_kinds": ["float"] * cap}, ncols=cap)
    assert kinds == ["float"] * cap
    if deal_pre_present(envelope_column_kinds):
        with pytest.raises(deal.PreContractError):
            envelope_column_kinds({}, ncols=cap + 1)


def test_bool_cells_round_trip_in_numeric_grid() -> None:
    """Calc booleans in an all-numeric grid become 0.0/1.0 in child ndarray (float64 lane)."""
    np = pytest.importorskip("numpy")
    grid = [[True, False, 1.0, 2.0], [False, True, 3.0, 4.0], [True, False, 5.0, 6.0]]
    arr = child_unpack_data(host_pack_data(grid, force="always"))
    assert isinstance(arr, np.ndarray)
    assert arr[0, 0] == pytest.approx(1.0)
    assert arr[0, 1] == pytest.approx(0.0)


def test_bool_col_11_split_grid_sums() -> None:
    """11 logical cells use split_grid inside calc_range; bools encode as 0/1."""
    np = pytest.importorskip("numpy")
    from plugin.calc.calc_addin_data import calc_addin_data_to_python, pack_calc_data_for_wire
    from plugin.scripting.calc_range import CalcRange, is_calc_range_payload

    pattern = (True, True, True, False, True, False, True, False, True, True, False)
    # Column range stays N×1 under the shape-preserving contract.
    uno_col = tuple((v,) for v in pattern)
    wire = pack_calc_data_for_wire(calc_addin_data_to_python(uno_col), force="always")
    assert is_calc_range_payload(wire)
    assert is_split_grid(wire["data"])
    assert wire_cell_count(wire) == 11
    assert wire["shape"] == [11, 1]
    rng = child_unpack_data(wire)
    assert isinstance(rng, CalcRange)
    assert rng.shape == (11, 1)
    assert float(np.sum(rng)) == pytest.approx(7.0)


def test_large_string_ndarray_packs_instead_of_raising() -> None:
    """A >=100-cell string or object ndarray must not crash the float64 packer."""
    np = pytest.importorskip("numpy")
    from plugin.scripting.venv.venv_sandbox import serialize_result

    n = BINARY_MIN_CELLS
    texts = np.array(["z"] * (n - 1) + ["02138"])
    wire = child_pack_result(texts)
    assert is_split_grid(wire)
    assert wire["strings"][n - 1] == "02138"
    back = host_unpack_data(wire, as_nested_list=True)
    assert back[0] == "z"
    assert back[-1] == "02138"

    obj = np.empty((10, n // 10), dtype=object)
    obj[:] = "ab"
    obj[-1, -1] = "cd"
    packed = serialize_result(obj)
    assert is_split_grid(packed)
    restored = host_unpack_data(packed, as_nested_list=True)
    assert restored[0][0] == "ab"
    assert restored[-1][-1] == "cd"

    small = np.array(["a", "b"], dtype=object)
    assert child_pack_result(small) == ["a", "b"]


def test_split_grid_boundary_at_binary_min_cells() -> None:
    """BINARY_MIN_CELLS: at threshold uses split_grid; one below stays nested list."""
    wire_at = host_pack_data(NUMERIC_AT_THRESHOLD, force="auto")
    assert is_split_grid(wire_at)
    assert wire_cell_count(wire_at) == BINARY_MIN_CELLS

    wire_below = host_pack_data(NUMERIC_BELOW_THRESHOLD, force="auto")
    assert not is_split_grid(wire_below)
    assert wire_cell_count(wire_below) == BINARY_MIN_CELLS - 1


def test_split_grid_flat_row_10_shape() -> None:
    """1×10 row stays 2D calc_range; inner split_grid is 1×10."""
    np = pytest.importorskip("numpy")
    from plugin.calc.calc_addin_data import calc_addin_data_to_python, pack_calc_data_for_wire
    from plugin.scripting.calc_range import CalcRange, is_calc_range_payload

    wire = pack_calc_data_for_wire(calc_addin_data_to_python((tuple(float(i + 1) for i in range(10)),)), force="always")
    assert is_calc_range_payload(wire)
    assert is_split_grid(wire["data"])
    assert wire["shape"] == [1, 10]
    rng = child_unpack_data(wire)
    assert isinstance(rng, CalcRange)
    assert rng.shape == (1, 10)
    assert float(np.sum(rng)) == pytest.approx(55.0)


def test_child_pack_below_threshold_returns_list() -> None:
    """Small ndarray egress below threshold is a nested list, not an ndarray or split_grid.

    Bugfix: returning the ndarray made a DataFrame under 100 cells spill only its
    header, and a bare multi-cell array become one Calc string.
    """
    np = pytest.importorskip("numpy")
    n = max(1, BINARY_MIN_CELLS - 1)
    rows, cols = rect_shape_for_cell_count(n)
    small = np.arange(n, dtype=np.float64).reshape(rows, cols)
    wire = child_pack_result(small, force="auto")
    assert not is_split_grid(wire)
    assert isinstance(wire, list)
    assert len(wire) == rows
    assert wire[0][0] == pytest.approx(0.0)


def test_split_grid_unpack_rejects_non_dict_strings() -> None:
    """strings must be a dict before .items(); a list used to raise AttributeError."""
    pytest.importorskip("numpy")
    envelope = {
        "__wa_payload__": PAYLOAD_SPLIT_GRID,
        "shape": [1],
        "buffer": array.array("d", [1.0]).tobytes(),
        "strings": ["not", "a", "dict"],
    }
    with pytest.raises(ValueError, match="strings must be a dict"):
        host_unpack_split_grid(envelope)
    with pytest.raises(ValueError, match="strings must be a dict"):
        child_unpack_split_grid(envelope)


@pytest.mark.parametrize("key", [float("inf"), 1.5, True])
def test_split_grid_rejects_non_integer_string_keys(key: object) -> None:
    """Float, inf, and bool keys must not truncate into another cell.

    What was wrong: ``int(1.5)`` stored the string at index 1, and
    ``int(float("inf"))`` raised OverflowError. The child contract only
    declares ValueError, so under deal that became RaisesContractError.
    """
    envelope = {
        "__wa_payload__": PAYLOAD_SPLIT_GRID,
        "shape": [2],
        "buffer": array.array("d", [7.0, 8.0]).tobytes(),
        "strings": {key: "x"},
    }
    with pytest.raises(ValueError, match="not an integer"):
        host_unpack_split_grid(envelope)
    pytest.importorskip("numpy")
    with pytest.raises(ValueError, match="not an integer"):
        child_unpack_split_grid(envelope)


def test_split_grid_digit_string_key_still_maps() -> None:
    """Legacy harnesses sent stringified indexes. ``-1`` is an int, then out of bounds."""
    ok = {
        "__wa_payload__": PAYLOAD_SPLIT_GRID,
        "shape": [2],
        "buffer": array.array("d", [7.0, 8.0]).tobytes(),
        "strings": {"0": "zip"},
    }
    assert host_unpack_split_grid(ok) == ["zip", 8.0]
    neg = {
        "__wa_payload__": PAYLOAD_SPLIT_GRID,
        "shape": [2],
        "buffer": array.array("d", [7.0, 8.0]).tobytes(),
        "strings": {"-1": "x"},
    }
    with pytest.raises(ValueError, match="out of bounds"):
        host_unpack_split_grid(neg)
    pytest.importorskip("numpy")
    assert child_unpack_split_grid(ok) == ["zip", 8.0]


def test_integers_past_float64_mantissa_round_only_on_split_grid() -> None:
    """``2**53 + 1`` survives a nested list and rounds once the float64 buffer is used.

    Locked in docs/calc/py-data-shapes.md (no int64 wire lane). A list-packing
    fallback would change the wire for every oversized integer.
    """
    big = 2**53 + 1
    small = [[big, 1], [2, 3]]
    assert host_unpack_data(host_pack_data(small, force="never")) == small
    assert host_unpack_data(host_pack_data(small, force="always"))[0][0] == 2**53

    grid = [[0] * 10 for unused in range(10)]
    grid[0][0] = big
    packed = host_pack_data(grid)
    assert is_split_grid(packed)
    assert host_unpack_data(packed)[0][0] == 2**53

    np = pytest.importorskip("numpy")
    arr = np.array([[big, 1], [2, 3]], dtype=np.int64)
    assert host_unpack_data(child_pack_result(arr, force="always"))[0][0] == 2**53
    child = child_unpack_data(host_pack_data(grid, force="always"))
    assert int(child[0, 0]) == 2**53


def test_host_unpack_split_grid_rejects_short_buffer() -> None:
    """Declared shape must match the float buffer. A short buffer is not a short grid."""
    buf = array.array("d", [1.0])
    envelope = {
        "__wa_payload__": "split_grid",
        "shape": [2, 2],
        "buffer": buf.tobytes(),
        "strings": {},
    }
    with pytest.raises(ValueError, match="buffer has 1 values"):
        host_unpack_split_grid(envelope)


def test_child_unpack_split_grid_mixed_rejects_size_mismatch() -> None:
    """Mixed-string child unpack must reject a buffer that does not match shape.

    The numeric path already did. 1D mixed grids skip reshape, so a short
    buffer used to come back as a shorter list and a long one kept extra cells.
    """
    pytest.importorskip("numpy")
    short = {
        "__wa_payload__": PAYLOAD_SPLIT_GRID,
        "shape": [4],
        "buffer": array.array("d", [1.0, 2.0]).tobytes(),
        "strings": {1: "x"},
    }
    with pytest.raises(ValueError, match="buffer has 2 values"):
        child_unpack_split_grid(short)
    long = {
        "__wa_payload__": PAYLOAD_SPLIT_GRID,
        "shape": [2, 2],
        "buffer": array.array("d", [1.0, 2.0, 3.0, 4.0, 5.0]).tobytes(),
        "strings": {0: "a"},
    }
    with pytest.raises(ValueError, match="buffer has 5 values"):
        child_unpack_split_grid(long)


def test_child_pack_numpy_scalar_types() -> None:
    """Worker egress normalizes numpy scalar types to plain Python."""
    np = pytest.importorskip("numpy")
    assert child_pack_result(np.int64(7)) == 7
    assert child_pack_result(np.float64(3.5)) == pytest.approx(3.5)
    assert child_pack_result(np.bool_(True)) is True


def test_get_cython_status_info() -> None:
    """Verify get_cython_status_info returns valid status line and location."""
    from plugin.scripting.payload_codec import get_cython_status_info, host_cython_status_line

    is_active, source_loc, status_line = get_cython_status_info()
    assert isinstance(is_active, bool)
    assert status_line.startswith("Cython Accelerator:")
    if is_active:
        assert "Active" in status_line
        assert source_loc is not None
    else:
        assert "Inactive" in status_line
        assert source_loc is None
    assert host_cython_status_line() == status_line


def test_cython_canary_failure_disables_accelerator() -> None:
    """Verify that a failing canary test prevents activating the accelerator."""
    from plugin.scripting.payload_codec import _verify_accelerator

    def bad_fn2d(data, shape):
        return [0.0], {}, None, [False], False

    def bad_fn1d(data):
        return [0.0], {}, None, [False], False

    assert _verify_accelerator(bad_fn2d, bad_fn1d) is False


def test_child_mixed_2d_returns_list_not_ndarray() -> None:
    """Any string column forces nested lists in child, not ndarray."""
    np = pytest.importorskip("numpy")
    out = child_unpack_data(host_pack_data(MIXED_LABEL_GRID, force="always"))
    assert isinstance(out, list)
    assert not isinstance(out, np.ndarray)


def test_is_numeric_coercible_and_is_numeric_grid() -> None:
    """Helpers gate numeric-only fast paths."""
    assert is_numeric_coercible(None) is True
    assert is_numeric_coercible("42") is False
    assert is_numeric_coercible("") is True
    assert is_numeric_coercible("hello") is False
    assert is_numeric_grid([[1, 2], [3, 4]]) is True
    assert is_numeric_grid([1, "x"]) is False
    # A user type whose name starts with "int" is not a NumPy scalar.
    internal = type("internal", (), {})
    assert is_numeric_coercible(internal()) is False
    np = pytest.importorskip("numpy")
    assert is_numeric_coercible(np.int64(3)) is True


def test_pickle5_roundtrip_numeric_4x4() -> None:
    """Production path: split_grid envelope survives Pickle5 unchanged."""
    wire = pickle5_roundtrip(host_pack_data(NUMERIC_4X4, force="always"))
    pytest.importorskip("numpy")
    arr = child_unpack_data(wire)
    assert arr.shape == (4, 4)
    assert arr[0, 0] == pytest.approx(0.0)


def test_1d_numeric_host_to_child_ndarray() -> None:
    """Flat 1D numeric list materializes as 1D ndarray in child."""
    np = pytest.importorskip("numpy")
    grid = [1.5, 2.5, 3.5, 4.5, 5.5, 6.5, 7.5, 8.5, 9.5, 10.5]
    arr = child_unpack_data(host_pack_data(grid, force="always"))
    assert isinstance(arr, np.ndarray)
    assert arr.shape == (10,)


def test_1d_mixed_child_returns_list() -> None:
    """Flat 1D list with a string stays a Python list in child."""
    pytest.importorskip("numpy")
    grid = [1.5, "banana", None, 4.5, 5.5, 6.5, 7.5, 8.5, 9.5, 10.5]
    out = child_unpack_data(host_pack_data(grid, force="always"))
    assert out == [1.5, "banana", None, 4.5, 5.5, 6.5, 7.5, 8.5, 9.5, 10.5]


def test_split_grid_numpy_scalars_in_lists():
    """Verify that lists containing NumPy scalar types are serialized numerically instead of stringified."""
    np = pytest.importorskip("numpy")
    grid = [[np.float64(1.5), np.int64(7)], [np.float64(2.5), np.int64(8)]]
    
    # Pack on host/child using split_grid
    wire = host_pack_data(grid, force="always")
    assert isinstance(wire, dict)
    assert wire["__wa_payload__"] == PAYLOAD_SPLIT_GRID
    assert wire["column_kinds"] == ["float", "int"]
    assert wire["strings"] == {}  # NumPy scalars should NOT be treated as strings!
    
    # Round-trip check
    unpacked = child_unpack_data(wire)
    assert isinstance(unpacked, np.ndarray)
    assert unpacked[0, 0] == pytest.approx(1.5)
    assert unpacked[0, 1] == pytest.approx(7.0)


def test_split_grid_boolean_roundtrip_fidelity():
    """Verify that boolean columns roundtrip perfectly to True/False in mixed grids under the 'bool' ColumnKind."""
    pytest.importorskip("numpy")
    
    # 2D mixed grid containing booleans, strings, and None
    grid = [
        [True, "apple", 10],
        [False, "banana", 20],
        [True, "cherry", None],
        [None, "date", 40]
    ]
    
    # 1. Test column kinds computed correctly
    kinds = payload_codec.column_kinds_for_grid(grid)
    # Text-only column is float, not int. The int column keeps int even with a None
    # because the grid has strings, so the pure-numeric None promotion does not fire.
    assert kinds == ["bool", "float", "int"]
    
    # 2. Test round-trip unpacking in child
    wire = host_pack_data(grid, force="always")
    assert wire["column_kinds"] == ["bool", "float", "int"]
    child_unpacked = child_unpack_data(wire)
    assert isinstance(child_unpacked, list)
    assert child_unpacked[0] == [True, "apple", 10]
    assert child_unpacked[1] == [False, "banana", 20]
    assert child_unpacked[2] == [True, "cherry", None]
    assert child_unpacked[3] == [None, "date", 40]
    
    # 3. Test round-trip unpacking on host.
    # Holes (None) become bare NaN slots (no strings entry). Host unpack preserves nan (Calc error policy).
    import math
    host_unpacked = host_unpack_data(wire, as_nested_list=True)
    assert host_unpacked[0] == [True, "apple", 10]
    assert host_unpacked[1] == [False, "banana", 20]
    assert host_unpacked[2][0] is True and host_unpacked[2][1] == "cherry" and math.isnan(host_unpacked[2][2])
    assert math.isnan(host_unpacked[3][0]) and host_unpacked[3][1] == "date" and host_unpacked[3][2] == 40


def test_split_grid_numpy_bool_scalars():
    """Verify that NumPy bool_ scalars are correctly identified as booleans."""
    np = pytest.importorskip("numpy")
    grid = [[np.bool_(True)], [np.bool_(False)]]
    wire = host_pack_data(grid, force="always")
    assert wire["column_kinds"] == ["bool"]
    unpacked = child_unpack_data(wire)
    assert isinstance(unpacked, np.ndarray)
    assert unpacked.dtype == np.bool_
    assert bool(unpacked[0, 0]) is True
    assert bool(unpacked[1, 0]) is False


def test_split_grid_empty_and_edge_cases():
    """Verify that empty and edge case shapes are handled gracefully without errors."""
    # 1. 2D grid with empty row [[]]
    wire = host_pack_data([[]], force="always")
    assert wire["shape"] == [1, 0]
    assert wire["buffer"] == b""
    assert wire["column_kinds"] == []


def test_split_grid_pure_numeric_fast_path():
    """Verify the purely numeric fast path where strings dictionary is empty."""
    np = pytest.importorskip("numpy")
    grid = [[10.5, 20.5], [30.5, 40.5]]
    
    wire = host_pack_data(grid, force="always")
    assert wire["strings"] == {}
    assert wire["column_kinds"] == ["float", "float"]
    
    unpacked = child_unpack_data(wire)
    assert isinstance(unpacked, np.ndarray)
    assert unpacked.shape == (2, 2)
    assert unpacked[1, 0] == pytest.approx(30.5)


def test_split_grid_logical_coercion_at_calc_ingress():
    """Verify that logical strings like "TRUE" and "FALSE" are coerced to bools during unwrap."""
    from plugin.calc.calc_addin_data import _unwrap_cell, calc_addin_data_to_python
    
    true_strings = {"=TRUE()", "TRUE", "True", "=WAHR()", "WAHR"}
    false_strings = {"=FALSE()", "FALSE", "False", "=FALSCH()", "FALSCH"}
    
    # 1. Test unwrap cell directly
    assert _unwrap_cell("TRUE", true_strings, false_strings) is True
    assert _unwrap_cell("=WAHR()", true_strings, false_strings) is True
    assert _unwrap_cell("FALSCH", true_strings, false_strings) is False
    assert _unwrap_cell("banana", true_strings, false_strings) == "banana"
    
    # 2. Test grid ingestion coercion
    raw_grid = [["TRUE", "FALSCH"], ["banana", 100.0]]
    coerced = calc_addin_data_to_python(raw_grid, true_strings, false_strings)
    assert coerced == [[True, False], ["banana", 100.0]]


def test_split_grid_single_cell_scalar_coercion():
    """Single-cell whole-number floats stay floats, matching a longer float64 column."""
    np = pytest.importorskip("numpy")

    assert child_unpack_data([100.0]) == 100.0
    assert isinstance(child_unpack_data([100.0]), float)

    assert child_unpack_data([3.14]) == pytest.approx(3.14)
    assert isinstance(child_unpack_data([3.14]), float)

    arr = np.array([42.0])
    assert child_unpack_data(arr) == 42.0
    assert isinstance(child_unpack_data(arr), float)


def test_split_grid_lattice_promotion_comprehensive():
    """Verify structural type promotions and kinds behavior for all scenarios."""
    # 1. Boolean-only column keeps bool kind in mixed grid
    grid1 = [[True, "apple"], [False, "banana"], [None, "cherry"]]
    assert payload_codec.column_kinds_for_grid(grid1) == ["bool", "float"]
    
    # 2. Boolean mixed with integers becomes int
    grid2 = [[True], [10], [False]]
    assert payload_codec.column_kinds_for_grid(grid2) == ["int"]
    
    # 3. Integer mixed with float becomes float
    grid3 = [[10], [1.5], [20]]
    assert payload_codec.column_kinds_for_grid(grid3) == ["float"]
    
    # 4. Purely numeric grid (no strings) with None forces float
    grid4 = [[10], [None], [20]]
    wire = host_pack_data(grid4, force="always")
    assert wire["column_kinds"] == ["float"]  # promoted to float because strings is empty and has None


def test_host_pack_multi_data_numeric_columns():
    np = pytest.importorskip("numpy")
    ranges = [[1.0, 2.0, 3.0], [4.0, 5.0, 6.0]]
    wire = host_pack_multi_data(ranges, force="always")
    assert is_multi_data(wire)
    assert wire["__wa_payload__"] == PAYLOAD_MULTI_DATA
    assert wire_cell_count(wire) == 6
    unpacked = child_unpack_data(wire)
    assert len(unpacked) == 2
    assert float(np.sum(unpacked[0])) == pytest.approx(6.0)
    assert float(np.sum(unpacked[1])) == pytest.approx(15.0)


def test_host_unpack_multi_data_mixed_grids():
    ranges = [[[1.0, "a"], [2.0, "b"]], [[3.0, "c"]]]
    wire = host_pack_multi_data(ranges, force="never")
    host_decoded = host_unpack_data(wire)
    assert len(host_decoded) == 2
    assert host_decoded[0] == [[1.0, "a"], [2.0, "b"]]
    assert host_decoded[1] == [[3.0, "c"]]
    child_decoded = child_unpack_data(wire)
    assert isinstance(child_decoded, list)
    assert len(child_decoded) == 2


def test_child_pack_nested_dict_ndarray() -> None:
    """Nested ndarray in dict values gets split_grid envelopes."""
    np = pytest.importorskip("numpy")
    arr = np.arange(12, dtype=np.float64).reshape(3, 4)
    wire = child_pack_result({"mean": arr}, force="always")
    assert is_split_grid(wire["mean"])
    back = host_unpack_data(wire)
    assert len(back["mean"]) == 3
    assert len(back["mean"][0]) == 4


def test_child_pack_list_of_ndarrays() -> None:
    """List of ndarrays packs each element separately."""
    np = pytest.importorskip("numpy")
    a = np.arange(10, dtype=np.float64)
    wire = child_pack_result([a, a], force="always")
    assert len(wire) == 2
    assert is_split_grid(wire[0])
    assert is_split_grid(wire[1])
    back = host_unpack_data(wire)
    assert len(back) == 2
    assert float(np.sum(back[0])) == pytest.approx(45.0)


def test_child_pack_nested_dict_list_ndarray() -> None:
    """Dict containing list containing ndarray is fully marshalled."""
    np = pytest.importorskip("numpy")
    wire = child_pack_result({"a": [np.arange(10, dtype=np.float64)]}, force="always")
    assert is_split_grid(wire["a"][0])
    back = host_unpack_data(wire)
    assert len(back["a"]) == 1
    assert len(back["a"][0]) == 10


def test_child_pack_grid_regression() -> None:
    """Plain 2D nested lists still use single-grid packing, not element-wise."""
    wire = child_pack_result([[1.0, 2.0], [3.0, 4.0]], force="auto")
    assert isinstance(wire, list)
    assert wire == [[1.0, 2.0], [3.0, 4.0]]


def test_unwrap_cell_comprehensive():
    """Verify unwrap_cell correctly normalizes standard types, localized formulas, and mocked UNO Any objects."""
    from plugin.calc.calc_addin_data import _unwrap_cell
    
    true_strings = {"=TRUE()", "TRUE", "True", "WAHR"}
    false_strings = {"=FALSE()", "FALSE", "False", "FALSCH"}
    
    # 1. Fast path exact types
    assert _unwrap_cell(1.0) == 1.0
    assert _unwrap_cell(42) == 42
    assert _unwrap_cell(True) is True
    
    # 2. Localized and formula string conversions
    assert _unwrap_cell("  TRUE  ", true_strings, false_strings) is True
    assert _unwrap_cell("WAHR", true_strings, false_strings) is True
    assert _unwrap_cell("FALSCH", true_strings, false_strings) is False
    
    # 3. UNO Mock Wrap types (e.g. uno.Any type emulation)
    class MockUnoAny:
        def __init__(self, value):
            self.value = value
    
    MockUnoAny.__name__ = "Any"
    assert _unwrap_cell(MockUnoAny(10.5)) == 10.5
    assert _unwrap_cell(MockUnoAny("TRUE"), true_strings, false_strings) is True


def test_child_pack_non_contiguous_slices():
    """Verify that non-contiguous numpy slices pack successfully without zero-copy buffer issues."""
    np = pytest.importorskip("numpy")
    from plugin.scripting.payload_codec import child_pack_result
    
    arr = np.arange(100, dtype=np.float64).reshape(10, 10)
    non_contiguous = arr[::2, ::2]  # Step slice creates non-contiguous array
    
    wire = child_pack_result(non_contiguous, force="always")
    assert wire["__wa_payload__"] == "split_grid"
    assert wire["shape"] == [5, 5]


def test_pure_python_pack_speed_regression():
    """Sanity check: 10k float cells pack well under a CI-noisy budget.

    A single 15ms sample flakes on macOS GHA under pytest-xdist (20.8ms vs 15ms
    on run 35477002275) without a real pack regression. Warm up, then require
    the fastest of a few packs to stay under 25ms — still a 10k-cell sanity
    check, not a no-op.
    """
    import time
    grid = [[float(i + j) for i in range(100)] for j in range(100)]

    # First pack pays import / allocator / cache warmup; do not time it.
    warm = host_pack_data(grid, force="always")
    assert is_split_grid(warm)

    samples_ms: list[float] = []
    last_wire = warm
    for unused in range(5):
        start = time.perf_counter()
        last_wire = host_pack_data(grid, force="always")
        samples_ms.append((time.perf_counter() - start) * 1000)

    assert is_split_grid(last_wire)
    best_ms = min(samples_ms)
    assert best_ms < 25.0, (
        f"Serialization took too long: best={best_ms:.2f}ms samples={samples_ms!r}"
    )


# ---------------------------------------------------------------------------
# Dataframe payload (pandas egress envelope) tests
# ---------------------------------------------------------------------------


def test_is_dataframe_payload_and_describe():
    env = {"__wa_payload__": PAYLOAD_DATAFRAME, "columns": ["A", "B"], "data": [[1, 2], [3, 4]]}
    assert is_dataframe_payload(env) is True
    assert not is_dataframe_payload({"foo": 1})
    desc = describe_wire_value(env)
    assert "dataframe" in desc and "cols=2" in desc


def test_dataframe_envelope_roundtrips_through_host_unpack():
    # Simulate child: a rectangular grid packed, wrapped as df payload.
    grid = [[10, "x"], [20, "y"]]
    inner = child_pack_result(grid, force="always")
    assert is_split_grid(inner) or isinstance(inner, list)
    df_env = {
        "__wa_payload__": PAYLOAD_DATAFRAME,
        "columns": ["num", "label"],
        "data": inner,
    }
    unpacked = host_unpack_data(df_env, as_nested_list=True)
    assert is_dataframe_payload(unpacked)
    assert unpacked["columns"] == ["num", "label"]
    data = unpacked["data"]
    # After unpack, inner should be list-of-lists
    assert isinstance(data, list) and len(data) == 2
    assert data[0] == [10, "x"] or data[0][0] == 10


def test_dataframe_host_unpack_preserves_split_grid_for_numeric():
    np = pytest.importorskip("numpy")
    arr = np.array([[1.0, 2.0], [3.0, 4.0]])
    inner = child_pack_result(arr, force="always")
    df_env = {"__wa_payload__": PAYLOAD_DATAFRAME, "columns": ["c0", "c1"], "data": inner}
    unpacked = host_unpack_data(df_env)
    assert unpacked["columns"] == ["c0", "c1"]
    # numeric path keeps list after host unpack (not ndarray on host)
    assert isinstance(unpacked["data"], list)
    assert unpacked["data"][0][0] == pytest.approx(1.0)


def test_date_and_datetime_serialization_handling():
    """Verify how dates and datetimes are handled when passing through the host/child bridge.
    
    This verifies that:
    1. Python datetime/date objects below threshold (pickle list path) preserve their types.
    2. Python datetime/date objects above threshold (split_grid) are coerced to strings on the wire.
    3. NumPy datetime64 arrays passed straight to child_pack_result raise (no epoch-day floats).
    4. Pandas Timestamps are correctly coerced to strings under split_grid.
    """
    np = pytest.importorskip("numpy")
    pd = pytest.importorskip("pandas")
    import datetime

    # 1. Below threshold (plain list path / pickle)
    d = datetime.date(2026, 6, 25)
    dt = datetime.datetime(2026, 6, 25, 14, 30, 0)
    
    wire_list = host_pack_data([d, dt], force="never")
    child_unpacked_list = child_unpack_data(wire_list)
    assert child_unpacked_list[0] == d
    assert child_unpacked_list[1] == dt

    # 2. Above threshold (split_grid / always binary)
    grid = [[d, dt] * 50]  # 100 cells
    wire_sg = host_pack_data(grid, force="always")
    assert wire_sg["__wa_payload__"] == PAYLOAD_SPLIT_GRID
    
    # Verify they were treated as strings in the strings dict
    assert 0 in wire_sg["strings"]
    assert wire_sg["strings"][0] == "2026-06-25"
    assert wire_sg["strings"][1] == "2026-06-25 14:30:00"

    # Child unpacks them as strings
    child_unpacked_sg = child_unpack_data(wire_sg)
    assert isinstance(child_unpacked_sg, list)
    assert child_unpacked_sg[0][0] == "2026-06-25"
    assert child_unpacked_sg[0][1] == "2026-06-25 14:30:00"

    # 3. NumPy datetime64 must not become epoch-day floats or ns integers.
    # serialize_result converts these first; a direct pack raises.
    arr = np.array([np.datetime64("2026-06-25"), np.datetime64("2026-06-26")])
    with pytest.raises(ValueError, match="datetime64"):
        child_pack_result(arr, force="always")
    with pytest.raises(ValueError, match="datetime64"):
        payload_codec.child_pack_split_grid(arr)
    with pytest.raises(ValueError, match="timedelta64"):
        child_pack_result(np.array([np.timedelta64(1, "D")]), force="never")

    # 4. Pandas Timestamps under split_grid (above threshold)
    ts = pd.Timestamp("2026-06-25 14:30:00")
    grid_ts = [[ts] * 100]
    wire_ts = host_pack_data(grid_ts, force="always")
    assert wire_ts["__wa_payload__"] == PAYLOAD_SPLIT_GRID
    assert wire_ts["strings"][0] == "2026-06-25 14:30:00"
    
    child_unpacked_ts = child_unpack_data(wire_ts)
    assert child_unpacked_ts[0][0] == "2026-06-25 14:30:00"


def test_invalidate_host_cython_accelerator_clears_globals_and_modules() -> None:
    import sys
    import types

    prev_2d = payload_codec.fast_flatten_grid_2d
    prev_1d = payload_codec.fast_flatten_grid_1d
    prev_disabled = payload_codec._CYTHON_ACCELERATOR_DISABLED
    fake = types.ModuleType("writeragent_vec")
    fake.fast_flatten_grid_2d = object()
    sys.modules["writeragent_vec"] = fake
    sys.modules["writeragent_vec.pack"] = types.ModuleType("writeragent_vec.pack")

    try:
        payload_codec.fast_flatten_grid_2d = object()
        payload_codec.fast_flatten_grid_1d = object()
        payload_codec._CYTHON_ACCELERATOR_DISABLED = True

        payload_codec.invalidate_host_cython_accelerator()

        assert payload_codec.fast_flatten_grid_2d is None
        assert payload_codec.fast_flatten_grid_1d is None
        assert payload_codec._CYTHON_ACCELERATOR_DISABLED is False
        assert "writeragent_vec" not in sys.modules
        assert "writeragent_vec.pack" not in sys.modules
    finally:
        payload_codec.fast_flatten_grid_2d = prev_2d
        payload_codec.fast_flatten_grid_1d = prev_1d
        payload_codec._CYTHON_ACCELERATOR_DISABLED = prev_disabled
        sys.modules.pop("writeragent_vec", None)
        sys.modules.pop("writeragent_vec.pack", None)


def test_host_cython_status_line_report_only_by_default() -> None:
    prev_2d = payload_codec.fast_flatten_grid_2d
    prev_loc = payload_codec._CYTHON_ACCELERATOR_LOCATION
    prev_reason = payload_codec._CYTHON_ACCELERATOR_INACTIVE_REASON
    try:
        with patch.object(payload_codec, "reload_host_cython_accelerator") as mock_reload:
            payload_codec.fast_flatten_grid_2d = None
            payload_codec._CYTHON_ACCELERATOR_LOCATION = None
            payload_codec._CYTHON_ACCELERATOR_INACTIVE_REASON = None
            line = payload_codec.host_cython_status_line()
            mock_reload.assert_not_called()
            assert line == "Cython Accelerator: Inactive (Pure Python)"

            payload_codec.fast_flatten_grid_2d = object()
            payload_codec._CYTHON_ACCELERATOR_LOCATION = None
            assert payload_codec.host_cython_status_line(reload=True) == "Cython Accelerator: Active (Optimized)"
            mock_reload.assert_called_once()
    finally:
        payload_codec.fast_flatten_grid_2d = prev_2d
        payload_codec._CYTHON_ACCELERATOR_LOCATION = prev_loc
        payload_codec._CYTHON_ACCELERATOR_INACTIVE_REASON = prev_reason


@pytest.mark.parametrize(
    "value",
    [
        {"__wa_payload__": PAYLOAD_CALC_RANGE, "shape": [1, 1], "data": [[1]]},
        {"__wa_payload__": PAYLOAD_CALC_RANGE, "shape": [0, 0], "data": []},
        {"__wa_payload__": PAYLOAD_CALC_RANGE, "shape": [1], "data": []},
        {"__wa_payload__": PAYLOAD_CALC_RANGE, "shape": [1, 1]},
        {"__wa_payload__": PAYLOAD_CALC_RANGE, "shape": [-1, 1], "data": []},
        {"__wa_payload__": PAYLOAD_SPLIT_GRID, "shape": [1, 1], "buffer": b""},
        {"foo": "bar"},
        None,
        [],
        42,
        "calc_range",
    ],
)
def test_is_calc_range_payload_matches_calc_range_module(value: object) -> None:
    """calc_range re-exports the codec detector (same object, same answers)."""
    from plugin.scripting.calc_range import is_calc_range_payload as calc_range_is

    assert calc_range_is is payload_codec.is_calc_range_payload
    assert payload_codec.is_calc_range_payload(value) is calc_range_is(value)


def test_host_pack_data_tuple_rows() -> None:
    # ensure it doesn't crash cython accelerator due to PyList_GET_ITEM on tuple
    grid = [(1.0, 2.0), (3.0, 4.0)]
    try:
        from plugin.scripting import payload_codec
        orig_2d = payload_codec.fast_flatten_grid_2d

        payload_codec._CYTHON_ACCELERATOR_DISABLED = False
        payload_codec.fast_flatten_grid_2d = None
        payload_codec.load_cython_accelerator()

        # Will crash if bug is present
        wire = host_pack_data(grid, force="always")

        # Verify it packed properly
        assert is_split_grid(wire)
        arr, str_map, types, shape = payload_codec._flatten_grid_to_components(grid)
        assert shape == [2, 2]
    finally:
        payload_codec.fast_flatten_grid_2d = orig_2d

def test_host_pack_data_numpy_str() -> None:
    np = pytest.importorskip("numpy")
    grid = [["0123", np.str_("0123")]]

    from plugin.scripting import payload_codec
    orig_2d = payload_codec.fast_flatten_grid_2d

    try:
        # stdlib test
        payload_codec.fast_flatten_grid_2d = None
        arr, str_map, types, shape = payload_codec._flatten_grid_to_components(grid)
        assert str_map[0] == "0123"
        assert str_map[1] == "0123"
        # np.str_ must not ride the strings map; host pickle has no NumPy.
        assert type(str_map[0]) is str
        assert type(str_map[1]) is str

        # accel test
        payload_codec._CYTHON_ACCELERATOR_DISABLED = False
        payload_codec.fast_flatten_grid_2d = None
        payload_codec.load_cython_accelerator()

        if payload_codec.fast_flatten_grid_2d is not None:
            arr, str_map, types, shape = payload_codec._flatten_grid_to_components(grid)
            assert str_map[0] == "0123"
            assert str_map[1] == "0123"
            assert type(str_map[0]) is str
            assert type(str_map[1]) is str
    finally:
        payload_codec.fast_flatten_grid_2d = orig_2d


def test_child_pack_result_dict_key_collision_raises() -> None:
    """{1: "a", "1": "b"} must not silently drop "a" when keys are stringified."""
    with pytest.raises(ValueError, match="collide"):
        child_pack_result({1: "a", "1": "b"})
    assert child_pack_result({1: "a"}) == {"1": "a"}


def test_split_grid_unpack_rejects_non_str_string_value() -> None:
    envelope = {
        "__wa_payload__": PAYLOAD_SPLIT_GRID,
        "shape": [1],
        "buffer": array.array("d", [1.0]).tobytes(),
        "strings": {0: 5},
    }
    with pytest.raises(ValueError, match="must be str"):
        host_unpack_split_grid(envelope)


def test_host_unpack_int_column_inf_raises() -> None:
    """A corrupt int column containing inf is OverflowError, not a swallowed contract miss."""
    wire = payload_codec.host_pack_split_grid([[1, 2], [3, 4]])
    buf = array.array("d")
    buf.frombytes(wire["buffer"])
    buf[0] = float("inf")
    wire["buffer"] = buf.tobytes()
    with pytest.raises(OverflowError):
        host_unpack_split_grid(wire)


def test_flatten_rejects_jagged_before_accelerator() -> None:
    """Jagged rows raise even if a native flattener would pad them."""
    called: list[bool] = []

    def fake_flatten(_grid: list, _ncols: int):
        called.append(True)
        return array.array("d", [1.0, 2.0, 3.0]), {}, [0, 0], [False, False], False

    orig = payload_codec.fast_flatten_grid_2d
    payload_codec.fast_flatten_grid_2d = fake_flatten
    try:
        with pytest.raises(ValueError, match="Uneven"):
            payload_codec._flatten_grid_to_components([[1, 2], [3]])
    finally:
        payload_codec.fast_flatten_grid_2d = orig
    assert called == []


def test_flatten_falls_back_when_accelerator_length_mismatches() -> None:
    """A short accelerator buffer must not replace the stdlib flatten."""

    def fake_flatten(_grid: list, _ncols: int):
        return array.array("d", [9.0]), {}, [3], [False], False

    orig = payload_codec.fast_flatten_grid_2d
    payload_codec.fast_flatten_grid_2d = fake_flatten
    try:
        buf, _strings, kinds, shape = payload_codec._flatten_grid_to_components([[1, 2]])
    finally:
        payload_codec.fast_flatten_grid_2d = orig
    assert shape == [1, 2]
    assert len(buf) == 2
    assert list(buf) == [1.0, 2.0]
    assert kinds == ["int", "int"]


def test_flatten_1d_falls_back_when_accelerator_length_mismatches() -> None:
    """A short 1D native buffer must not replace the stdlib flatten."""

    def fake_flatten(_grid: list):
        return array.array("d", [9.0]), {}, [3], [False], False

    orig = payload_codec.fast_flatten_grid_1d
    payload_codec.fast_flatten_grid_1d = fake_flatten
    try:
        buf, _strings, kinds, shape = payload_codec._flatten_grid_to_components([1, 2, 3])
    finally:
        payload_codec.fast_flatten_grid_1d = orig
    assert shape == [3]
    assert list(buf) == [1.0, 2.0, 3.0]
    assert kinds == ["int"]


def test_child_pack_rank3_is_list_of_planes() -> None:
    """Rank 3+ is not a split_grid envelope. Each plane packs on its own."""
    np = pytest.importorskip("numpy")
    arr = np.arange(125).reshape(5, 5, 5)
    packed = child_pack_result(arr, force="always")
    assert isinstance(packed, list)
    assert len(packed) == 5
    assert is_split_grid(packed) is False
    assert is_split_grid(packed[0]) is True
    back = host_unpack_data(packed)
    assert back[0][0][0] == 0
    assert back[4][4][4] == 124

    small = child_pack_result(np.arange(8).reshape(2, 2, 2))
    assert small == [[[0, 1], [2, 3]], [[4, 5], [6, 7]]]


def test_child_pack_split_grid_rejects_rank_and_complex() -> None:
    np = pytest.importorskip("numpy")
    with pytest.raises(ValueError, match="rank 1 or 2"):
        payload_codec.child_pack_split_grid(np.zeros((2, 2, 2)))
    with pytest.raises(ValueError, match="i/u/f/b"):
        payload_codec.child_pack_split_grid(np.zeros((2, 2), dtype=complex))
    assert child_pack_result(np.array(3), force="always") == 3


def test_child_pack_complex_keeps_imaginary_part() -> None:
    """astype(float64) used to keep only the real part."""
    np = pytest.importorskip("numpy")
    arr = np.array([[1 + 2j, 3 + 4j], [5 + 6j, 7 + 8j]])
    wire = child_pack_result(arr, force="always")
    assert is_split_grid(wire)
    back = host_unpack_data(wire)
    assert back[0][0] == "(1+2j)"
    assert back[1][1] == "(7+8j)"


def test_host_unpack_nonuniform_bool_two_is_false() -> None:
    """A bool-tagged 2.0 is False on the mixed path, matching the uniform path. NaN stays NaN."""
    two = {
        "__wa_payload__": PAYLOAD_SPLIT_GRID,
        "dtype": "float64",
        "shape": [1, 2],
        "buffer": array.array("d", [2.0, 1.5]).tobytes(),
        "strings": {},
        "column_kinds": ["bool", "float"],
    }
    assert host_unpack_split_grid(two) == [[False, 1.5]]
    nan_env = {
        "__wa_payload__": PAYLOAD_SPLIT_GRID,
        "dtype": "float64",
        "shape": [1, 2],
        "buffer": array.array("d", [float("nan"), 1.5]).tobytes(),
        "strings": {},
        "column_kinds": ["bool", "float"],
    }
    unpacked = host_unpack_split_grid(nan_env)
    assert math.isnan(unpacked[0][0])
    assert unpacked[0][1] == 1.5


def test_flatten_overflow_fraction_is_text_without_accelerator() -> None:
    """A huge Fraction is text on the stdlib path, matching Cython _flatten_cell.

    The fast path catches OverflowError and retries the slow helper. An earlier
    text cell forces the slow path directly. Both used to raise.
    """
    from fractions import Fraction

    huge = Fraction(10**309)
    orig = payload_codec.fast_flatten_grid_2d
    payload_codec.fast_flatten_grid_2d = None
    try:
        alone = payload_codec.host_pack_split_grid([[huge]])
        assert alone["strings"][0] == str(huge)
        mixed = payload_codec.host_pack_split_grid([["02138", huge]])
        assert mixed["strings"][0] == "02138"
        assert mixed["strings"][1] == str(huge)
    finally:
        payload_codec.fast_flatten_grid_2d = orig


def test_split_grid_unpack_rejects_duplicate_stringified_keys() -> None:
    """Keys that int() to the same index (1 and \"1\") must not last-wins."""
    pytest.importorskip("numpy")
    envelope = {
        "__wa_payload__": PAYLOAD_SPLIT_GRID,
        "shape": [2],
        "buffer": array.array("d", [float("nan"), 1.0]).tobytes(),
        "strings": {0: "a", "0": "b"},
        "column_kinds": ["float"],
    }
    with pytest.raises(ValueError, match="collide"):
        host_unpack_split_grid(envelope)
    with pytest.raises(ValueError, match="collide"):
        child_unpack_split_grid(envelope)


def test_child_unpack_plain_dict_unpacks_nested_split_grid() -> None:
    """A split_grid nested in a plain dict unpacks. A nested list stays a list."""
    np = pytest.importorskip("numpy")
    grid = [[float(i)] for i in range(4)]
    out = child_unpack_data({"label": host_pack_data(grid, force="always")})
    assert isinstance(out["label"], np.ndarray)
    assert out["label"].shape == (4, 1)
    assert child_unpack_data({"changes": [[1.0, 2.0]]}) == {"changes": [[1.0, 2.0]]}


def test_host_pack_empty_rows_skip_binary_shortcut() -> None:
    """Zero-width rows are not a split_grid. A non-empty first row still is."""
    empty = [[] for _ in range(BINARY_MIN_CELLS)]
    assert not is_split_grid(host_pack_data(empty, force="auto"))
    filled = [[1.0] for _ in range(BINARY_MIN_CELLS)]
    assert is_split_grid(host_pack_data(filled, force="auto"))


def test_child_pack_namedtuple_of_ndarrays_is_plain_tuple() -> None:
    """Tuple subclasses collapse to tuple and do not call type(obj)(items)."""
    from collections import namedtuple

    np = pytest.importorskip("numpy")
    Point = namedtuple("Point", "x y")
    arr = np.zeros((4, 4))
    out = child_pack_result(Point(arr, arr), force="always")
    assert type(out) is tuple
    assert len(out) == 2
    assert is_split_grid(out[0])
    assert is_split_grid(out[1])
    unpacked = host_unpack_data(Point(1, 2))
    assert type(unpacked) is tuple
    assert unpacked == (1, 2)


def test_host_unpack_depth_cap_raises_before_recursion_error() -> None:
    """The cap stays under the default recursion limit, so a cycle is ValueError."""
    from plugin.scripting.payload_codec import _MAX_UNPACK_DEPTH

    nested: object = 0
    for unused in range(_MAX_UNPACK_DEPTH):
        nested = [nested]
    out = host_unpack_data(nested)
    for unused in range(_MAX_UNPACK_DEPTH):
        assert isinstance(out, list) and len(out) == 1
        out = out[0]
    assert out == 0

    one_past: object = 0
    for unused in range(_MAX_UNPACK_DEPTH + 1):
        one_past = [one_past]
    with pytest.raises(ValueError, match="maximum recursion depth"):
        host_unpack_data(one_past)

    cyclic: list[object] = []
    cyclic.append(cyclic)
    with pytest.raises(ValueError, match="maximum recursion depth"):
        host_unpack_data(cyclic)


def test_find_image_payloads_shared_image_and_wrapper() -> None:
    """A shared image dict counts twice. A shared wrapper is walked once."""
    from plugin.scripting.payload_codec import PAYLOAD_IMAGE, find_image_payloads

    img = {"__wa_payload__": PAYLOAD_IMAGE, "data": b"png", "format": "png"}
    assert find_image_payloads({"a": img, "b": img}) == [img, img]
    box = {"image": img}
    assert find_image_payloads({"a": box, "b": box}) == [img]


def test_write_image_payload_to_temp_unlinks_when_write_fails() -> None:
    """delete=False used to leave the file when data was not bytes."""
    import os
    import tempfile

    from plugin.scripting.payload_codec import write_image_payload_to_temp

    created: list[str] = []
    real = tempfile.NamedTemporaryFile

    def _tracking(*args, **kwargs):
        tmp = real(*args, **kwargs)
        created.append(tmp.name)
        return tmp

    with patch("plugin.scripting.payload_codec.tempfile.NamedTemporaryFile", _tracking):
        with pytest.raises(TypeError):
            write_image_payload_to_temp({"data": object(), "format": "png"})
    assert len(created) == 1
    assert not os.path.exists(created[0])


def test_mixed_int_float_child_stays_float64_host_emits_ints() -> None:
    """Empty-strings mixed kinds: child is one float64 view; host ints the int column.

    normalize_for_oracle turns integral floats into int, so the A/B suite
    does not catch this. A homogeneous ndarray cannot store both dtypes.
    """
    np = pytest.importorskip("numpy")
    envelope = {
        "__wa_payload__": PAYLOAD_SPLIT_GRID,
        "dtype": "float64",
        "shape": [1, 2],
        "buffer": array.array("d", [2.0, 1.5]).tobytes(),
        "strings": {},
        "column_kinds": ["int", "float"],
    }
    child = child_unpack_split_grid(envelope)
    assert isinstance(child, np.ndarray)
    assert child.dtype == np.float64
    assert child.flags.writeable is False
    assert float(child[0, 0]) == 2.0
    assert float(child[0, 1]) == 1.5
    host = host_unpack_split_grid(envelope)
    assert host == [[2, 1.5]]
    assert type(host[0][0]) is int
