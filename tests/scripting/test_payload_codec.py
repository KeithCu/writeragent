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

import ast
import math
from pathlib import Path

import pytest

from plugin.scripting import payload_codec
from plugin.scripting.payload_codec import (
    BINARY_MIN_CELLS,
    PAYLOAD_MULTI_DATA,
    PAYLOAD_SPLIT_GRID,
    binary_envelope_skip_reason,
    child_pack_result,
    child_unpack_data,
    describe_wire_value,
    host_pack_data,
    host_pack_multi_data,
    host_unpack_data,
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
    pickle5_roundtrip,
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


@pytest.mark.parametrize(
    ("shape", "force", "expected"),
    [
        ((3, 3), "auto", False),
        ((4, 3), "auto", True),
        ((4, 4), "auto", True),
        ((9,), "auto", False),
        ((10,), "auto", True),
        ((4, 4), "never", False),
        ((3, 3), "always", True),
    ],
)
def test_should_use_binary_envelope_boundary(shape: tuple[int, ...], force: str, expected: bool) -> None:
    """BINARY_MIN_CELLS=10: 9 cells use nested lists; 10+ use split_grid when force=auto."""
    assert should_use_binary_envelope(shape, min_cells=10, force=force) is expected


def test_binary_envelope_skip_reason_below_threshold() -> None:
    """Policy helper explains why a 3x3 grid skips split_grid."""
    reason = binary_envelope_skip_reason((3, 3), min_cells=10, force="auto")
    assert reason is not None
    assert "10" in reason


def test_host_pack_auto_uses_split_grid_for_4x3():
    grid = [[1.0, 4.0, 5.0], [23.0, 4.0, 4.0], [5.0, 4.0, 4.0], [4.0, 5.0, 4.0]]
    wire = host_pack_data(grid, min_cells=10, force="auto")
    assert isinstance(wire, dict)
    assert wire["__wa_payload__"] == PAYLOAD_SPLIT_GRID
    assert wire["shape"] == [4, 3]


def test_host_pack_auto_uses_list_for_3x3():
    grid = [[1.0, 2.0, 3.0], [4.0, 5.0, 6.0], [7.0, 8.0, 9.0]]
    wire = host_pack_data(grid, min_cells=10, force="auto")
    assert isinstance(wire, list)
    assert wire[0][0] == 1.0


def test_host_pack_auto_uses_split_grid_for_4x4():
    grid = [[float(i)] * 4 for i in range(4)]
    wire = host_pack_data(grid, min_cells=10, force="auto")
    assert isinstance(wire, dict)
    assert wire["__wa_payload__"] == PAYLOAD_SPLIT_GRID
    assert wire["shape"] == [4, 4]


def test_round_trip_host_split_grid_child_ndarray():
    np = pytest.importorskip("numpy")
    grid = [[1.0, 2.0], [3.0, 4.0], [5.0, 6.0], [7.0, 8.0]]
    wire = host_pack_data(grid, force="always")
    assert wire["__wa_payload__"] == PAYLOAD_SPLIT_GRID
    arr = child_unpack_data(wire)
    assert isinstance(arr, np.ndarray)
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
    assert column_kinds_for_grid([[1, "x"]]) == ["int", "int"]


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


def test_none_becomes_nan_in_split_grid():
    np = pytest.importorskip("numpy")
    wire = host_pack_data([[1.0, None, 3.0]], force="always")
    arr = child_unpack_data(wire)
    assert arr.shape == (1, 3)
    assert math.isnan(float(arr[0, 1]))


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
    np = pytest.importorskip("numpy")
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
    
    # Unpack on host -> flat list
    host_unpacked_mixed = host_unpack_data(wire_child_mixed, as_nested_list=True)
    assert host_unpacked_mixed == [1.5, "banana", None, 4.5]


def test_child_unpack_single_entry_auto_scalar_and_integer_coercion():
    """Verify that child_unpack_data automatically unpacks single-entry inputs into scalars and coerces float-integers."""
    np = pytest.importorskip("numpy")

    # 1. 1-element numeric list representing an integer float
    wire_int_float = [100000.0]
    unpacked_int_float = child_unpack_data(wire_int_float)
    assert isinstance(unpacked_int_float, int)
    assert unpacked_int_float == 100000

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

    # 5. 1-element numpy array representing an integer float (e.g. from split-grid of shape (1,))
    arr_int_float = np.array([100000.0])
    unpacked_arr_int_float = child_unpack_data(arr_int_float)
    assert isinstance(unpacked_arr_int_float, int)
    assert unpacked_arr_int_float == 100000

    # 6. Multi-element list or 2D list should NOT be unpacked to scalar
    assert isinstance(child_unpack_data([100000.0, 200000.0]), np.ndarray)
    assert child_unpack_data([[100000.0]]) == [[100000.0]]  # 2D list preserved


def test_uneven_row_lengths_rejected_on_host_pack() -> None:
    """Uneven nested-list rows are unsupported; Calc ranges are always rectangular."""
    with pytest.raises(ValueError, match="Uneven row lengths"):
        host_pack_data([[1, 2], [3]], force="always")


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
    """NumPy result with np.nan: host unpack maps buffer NaN to None for Calc/LLM."""
    np = pytest.importorskip("numpy")
    wire = child_pack_result(np.array([1.0, np.nan, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0]), force="always")
    back = host_unpack_data(wire, as_nested_list=True)
    assert back[0] == pytest.approx(1.0)
    assert back[1] is None


def test_none_host_egress_round_trip() -> None:
    """Rectangular grid with holes: host pack -> child ndarray -> host list restores None."""
    np = pytest.importorskip("numpy")
    grid = [[1.0, None, 3.0, 4.0], [5.0, 6.0, None, 8.0], [9.0, 10.0, 11.0, 12.0]]
    wire = host_pack_data(grid, force="always")
    arr = child_unpack_data(wire)
    assert isinstance(arr, np.ndarray)
    back = host_unpack_data(wire, as_nested_list=True)
    assert back[0][1] is None
    assert back[1][2] is None


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


def test_bool_cells_round_trip_in_numeric_grid() -> None:
    """Calc booleans in an all-numeric grid become 0.0/1.0 in child ndarray (float64 lane)."""
    np = pytest.importorskip("numpy")
    grid = [[True, False, 1.0, 2.0], [False, True, 3.0, 4.0], [True, False, 5.0, 6.0]]
    arr = child_unpack_data(host_pack_data(grid, force="always"))
    assert isinstance(arr, np.ndarray)
    assert arr[0, 0] == pytest.approx(1.0)
    assert arr[0, 1] == pytest.approx(0.0)


def test_bool_col_11_split_grid_sums() -> None:
    """11 logical cells use split_grid; bools encode as 0/1 in float64 buffer."""
    np = pytest.importorskip("numpy")
    from plugin.calc.calc_addin_data import calc_addin_data_to_python, pack_calc_data_for_wire

    pattern = (True, True, True, False, True, False, True, False, True, True, False)
    # Column range: calc_addin flattens to 1D before pack (same as =PYTHON(code;D1:D11)).
    uno_col = tuple((v,) for v in pattern)
    wire = pack_calc_data_for_wire(calc_addin_data_to_python(uno_col), force="always")
    assert is_split_grid(wire)
    assert wire_cell_count(wire) == 11
    assert wire["shape"] == [11]
    arr = child_unpack_data(wire)
    assert isinstance(arr, np.ndarray)
    assert arr.shape == (11,)
    assert float(np.sum(arr)) == pytest.approx(7.0)


def test_split_grid_boundary_exactly_10_cells() -> None:
    """BINARY_MIN_CELLS: 10 cells pack as split_grid; 9 stay nested list."""
    grid_2x5 = [[float(r * 5 + c + 1) for c in range(5)] for r in range(2)]
    wire_10 = host_pack_data(grid_2x5, min_cells=10)
    assert is_split_grid(wire_10)
    assert wire_cell_count(wire_10) == 10

    grid_3x3 = [[float(r * 3 + c + 1) for c in range(3)] for r in range(3)]
    wire_9 = host_pack_data(grid_3x3, min_cells=10)
    assert not is_split_grid(wire_9)


def test_split_grid_flat_row_10_shape() -> None:
    """1×10 row flattens to 1D split_grid after calc_addin shaping."""
    np = pytest.importorskip("numpy")
    from plugin.calc.calc_addin_data import calc_addin_data_to_python, pack_calc_data_for_wire

    wire = pack_calc_data_for_wire(calc_addin_data_to_python((tuple(float(i + 1) for i in range(10)),)), force="always")
    assert is_split_grid(wire)
    assert wire["shape"] == [10]
    arr = child_unpack_data(wire)
    assert isinstance(arr, np.ndarray)
    assert arr.shape == (10,)
    assert float(np.sum(arr)) == pytest.approx(55.0)


def test_child_pack_below_threshold_returns_list() -> None:
    """Small ndarray egress uses tolist(), not split_grid envelope."""
    np = pytest.importorskip("numpy")
    small = np.arange(9, dtype=np.float64).reshape(3, 3)
    wire = child_pack_result(small, force="auto")
    assert isinstance(wire, list)
    assert len(wire) == 3


def test_child_pack_numpy_scalar_types() -> None:
    """Worker egress normalizes numpy scalar types to plain Python."""
    np = pytest.importorskip("numpy")
    assert child_pack_result(np.int64(7)) == 7
    assert child_pack_result(np.float64(3.5)) == pytest.approx(3.5)
    assert child_pack_result(np.bool_(True)) is True


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


def test_pickle5_roundtrip_numeric_4x4() -> None:
    """Production path: split_grid envelope survives Pickle5 unchanged."""
    wire = pickle5_roundtrip(host_pack_data(NUMERIC_4X4, force="always"))
    np = pytest.importorskip("numpy")
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
    np = pytest.importorskip("numpy")
    
    # 2D mixed grid containing booleans, strings, and None
    grid = [
        [True, "apple", 10],
        [False, "banana", 20],
        [True, "cherry", None],
        [None, "date", 40]
    ]
    
    # 1. Test column kinds computed correctly
    kinds = payload_codec.column_kinds_for_grid(grid)
    assert kinds == ["bool", "int", "int"]  # column 0 is bool, column 2 has None and ints so remains int
    
    # 2. Test round-trip unpacking in child
    wire = host_pack_data(grid, force="always")
    assert wire["column_kinds"] == ["bool", "int", "int"]
    child_unpacked = child_unpack_data(wire)
    assert isinstance(child_unpacked, list)
    assert child_unpacked[0] == [True, "apple", 10]
    assert child_unpacked[1] == [False, "banana", 20]
    assert child_unpacked[2] == [True, "cherry", None]
    assert child_unpacked[3] == [None, "date", 40]
    
    # 3. Test round-trip unpacking on host
    host_unpacked = host_unpack_data(wire, as_nested_list=True)
    assert host_unpacked == grid


def test_split_grid_numpy_bool_scalars():
    """Verify that NumPy bool_ scalars are correctly identified as booleans."""
    np = pytest.importorskip("numpy")
    grid = [[np.bool_(True)], [np.bool_(False)]]
    wire = host_pack_data(grid, force="always")
    assert wire["column_kinds"] == ["bool"]
    unpacked = child_unpack_data(wire)
    assert isinstance(unpacked, np.ndarray)
    assert unpacked.dtype == np.bool_
    assert unpacked[0, 0] == True
    assert unpacked[1, 0] == False


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
    """Verify automatic scalar extraction and whole float to integer coercion for single cells."""
    np = pytest.importorskip("numpy")
    
    # 1. 1-element list with a whole number float should become python int
    assert child_unpack_data([100.0]) == 100
    assert isinstance(child_unpack_data([100.0]), int)
    
    # 2. 1-element list with real float remains float
    assert child_unpack_data([3.14]) == pytest.approx(3.14)
    assert isinstance(child_unpack_data([3.14]), float)
    
    # 3. 1-element ndarray with integer float
    arr = np.array([42.0])
    assert child_unpack_data(arr) == 42
    assert isinstance(child_unpack_data(arr), int)


def test_split_grid_lattice_promotion_comprehensive():
    """Verify structural type promotions and kinds behavior for all scenarios."""
    # 1. Boolean-only column keeps bool kind in mixed grid
    grid1 = [[True, "apple"], [False, "banana"], [None, "cherry"]]
    assert payload_codec.column_kinds_for_grid(grid1) == ["bool", "int"]
    
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

