# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for range inspection, broadcasting, and mapping in map_range.py."""

from __future__ import annotations

from typing import Any

import pytest

from plugin.scripting.calc_range import CalcRange
from plugin.scripting.venv.map_range import (
    broadcast_args,
    inspect_input,
    map_over_range,
    rewrap_output,
)


def test_inspect_scalar():
    insp = inspect_input(10)
    assert insp.is_scalar is True
    assert insp.is_python_1d is False
    assert insp.flat_items == [10]
    assert insp.nrows == 1
    assert insp.ncols == 1


def test_inspect_1x1_calc_range_as_scalar():
    # 1x1 CalcRange and [[v]] must be treated as scalar
    cr = CalcRange([[42]])
    insp = inspect_input(cr)
    assert insp.is_scalar is True
    assert insp.flat_items == [42]

    insp2 = inspect_input([[42]])
    assert insp2.is_scalar is True
    assert insp2.flat_items == [42]


def test_inspect_1d_python_list():
    insp = inspect_input([10, 20, 30])
    assert insp.is_scalar is False
    assert insp.is_python_1d is True
    assert insp.flat_items == [10, 20, 30]
    assert insp.length == 3

    # Python list of length 1 stays 1D list
    insp_single = inspect_input([10])
    assert insp_single.is_scalar is False
    assert insp_single.is_python_1d is True
    assert insp_single.flat_items == [10]


def test_inspect_column_vector():
    cr = CalcRange([[10], [20], [30]])
    insp = inspect_input(cr)
    assert insp.is_scalar is False
    assert insp.is_python_1d is False
    assert insp.nrows == 3
    assert insp.ncols == 1
    assert insp.flat_items == [10, 20, 30]

    # Raw 2D list
    insp_raw = inspect_input([[10], [20]])
    assert insp_raw.is_scalar is False
    assert insp_raw.nrows == 2
    assert insp_raw.ncols == 1
    assert insp_raw.flat_items == [10, 20]


def test_inspect_row_vector():
    cr = CalcRange([[10, 20, 30]])
    insp = inspect_input(cr)
    assert insp.is_scalar is False
    assert insp.is_python_1d is False
    assert insp.nrows == 1
    assert insp.ncols == 3
    assert insp.flat_items == [10, 20, 30]


def test_inspect_2d_grid():
    cr = CalcRange([[1, 2], [3, 4]])
    insp = inspect_input(cr)
    assert insp.is_scalar is False
    assert insp.nrows == 2
    assert insp.ncols == 2
    assert insp.flat_items == [1, 2, 3, 4]


def test_rewrap_output():
    # Scalar
    assert rewrap_output([100], inspect_input(10)) == 100
    assert rewrap_output([100], inspect_input(CalcRange([[10]]))) == 100

    # 1D list
    assert rewrap_output([100, 200], inspect_input([10, 20])) == [100, 200]

    # Column vector N x 1
    assert rewrap_output([100, 200], inspect_input(CalcRange([[10], [20]]))) == [[100], [200]]

    # Row vector 1 x N
    assert rewrap_output([100, 200], inspect_input(CalcRange([[10, 20]]))) == [[100, 200]]

    # 2D Grid
    assert rewrap_output([1, 2, 3, 4], inspect_input(CalcRange([[10, 20], [30, 40]]))) == [[1, 2], [3, 4]]


def test_broadcast_scalar_with_vector():
    primary, b_args, b_kwargs = broadcast_args(
        CalcRange([[10], [20], [30]]),
        "m/s",
        target_unit="km/h",
    )
    assert primary.nrows == 3
    assert primary.ncols == 1
    assert b_args[0] == [10, 20, 30]
    assert b_args[1] == ["m/s", "m/s", "m/s"]
    assert b_kwargs["target_unit"] == ["km/h", "km/h", "km/h"]


def test_broadcast_paired_1d_vectors():
    primary, b_args, b_kwargs = broadcast_args(
        [10, 20],
        ["m/s", "km/h"],
        to=["ft/s", "mph"],
    )
    assert primary.length == 2
    assert b_args[0] == [10, 20]
    assert b_args[1] == ["m/s", "km/h"]
    assert b_kwargs["to"] == ["ft/s", "mph"]


def test_broadcast_length_mismatch_raises():
    with pytest.raises(ValueError, match="Vector length mismatch"):
        broadcast_args([10, 20], ["m/s", "km/h", "mph"])


def test_broadcast_grid_with_vector_raises():
    grid = CalcRange([[1, 2], [3, 4]])
    vec = [10, 20, 30, 40]
    with pytest.raises(ValueError, match="Cannot pair an M×N grid"):
        broadcast_args(grid, vec)


def test_map_over_range_scalar():
    def double(x: int) -> int:
        return x * 2

    assert map_over_range(double, 5) == 10
    assert map_over_range(double, CalcRange([[5]])) == 10


def test_map_over_range_column_vector():
    def double(x: int) -> int:
        return x * 2

    res = map_over_range(double, CalcRange([[1], [2], [3]]))
    assert res == [[2], [4], [6]]


def test_map_over_range_with_kwargs():
    def multiply(x: int, *, factor: int = 1) -> int:
        return x * factor

    res = map_over_range(multiply, CalcRange([[1], [2]]), factor=10)
    assert res == [[10], [20]]


def test_map_over_range_handles_blanks():
    def double(x: Any) -> int:
        return int(x) * 2

    # None, "", and "#N/A" must be skipped and produce ""
    res = map_over_range(double, [1, None, 3, "", "#N/A", 6])
    assert res == [2, "", 6, "", "", 12]


def test_map_over_range_handles_per_element_errors():
    def parse_int(x: Any) -> int:
        return int(x)

    res = map_over_range(parse_int, ["10", "bad_int", "30"])
    assert res == [10, "#VALUE!", 30]


def test_map_over_range_numpy_pandas_input():
    np = pytest.importorskip("numpy")
    pd = pytest.importorskip("pandas")

    def square(x: int) -> int:
        return x * x

    # 1D numpy array
    arr = np.array([1, 2, 3])
    assert map_over_range(square, arr) == [1, 4, 9]

    # Pandas Series
    s = pd.Series([2, 3, 4])
    assert map_over_range(square, s) == [4, 9, 16]


def test_map_over_range_vector_not_first_arg_handles_blanks():
    """Missing cell in non-first vector arg produces blank output, not #VALUE!."""
    def scale(factor: int, x: Any) -> int:
        return factor * int(x)

    res = map_over_range(scale, 2, CalcRange([[1], [None], [3]]))
    assert res == [[2], [""], [6]]


def test_map_over_range_blank_scalar_first_arg_does_not_blank_all():
    """A scalar None first argument must not blank all output rows when mapping a vector."""
    def concat(prefix: Any, val: Any) -> str:
        return f"{prefix}:{val}"

    res = map_over_range(concat, None, CalcRange([[1], [2], [3]]))
    assert res == [["None:1"], ["None:2"], ["None:3"]]


def test_map_over_range_pandas_dataframe_input():
    """Pandas DataFrame input is unwrapped into nested lists and mapped elementwise."""
    pd = pytest.importorskip("pandas")

    def square(x: int) -> int:
        return x * x

    df = pd.DataFrame({"a": [1, 2], "b": [3, 4]})
    res = map_over_range(square, df)
    # DataFrame columns 'a'=[1,2], 'b'=[3,4] form rows [1,3], [2,4]
    assert res == [[1, 9], [4, 16]]


def test_broadcast_args_equal_shape_grids():
    """Two M×N grids with identical shape can be broadcast elementwise."""
    grid1 = CalcRange([[1, 2], [3, 4]])
    grid2 = CalcRange([[10, 20], [30, 40]])
    primary, b_args, _ = broadcast_args(grid1, grid2)
    assert primary.nrows == 2
    assert primary.ncols == 2
    assert b_args[0] == [1, 2, 3, 4]
    assert b_args[1] == [10, 20, 30, 40]

    res = map_over_range(lambda a, b: a + b, grid1, grid2)
    assert res == [[11, 22], [33, 44]]


def test_broadcast_args_orientation_mismatch_raises():
    """Row vector (1×N) and column vector (N×1) must raise an orientation mismatch error."""
    row_vec = CalcRange([[1, 2, 3]])
    col_vec = CalcRange([[1], [2], [3]])
    with pytest.raises(ValueError, match="orientation mismatch"):
        broadcast_args(row_vec, col_vec)


def test_map_over_range_zero_division_error():
    """ZeroDivisionError produces #DIV/0! when return_errors=True, and raises when False."""
    def div(a: int, b: int) -> float:
        return a / b

    res = map_over_range(div, [10, 20], [2, 0])
    assert res == [5.0, "#DIV/0!"]

    with pytest.raises(ZeroDivisionError):
        map_over_range(div, 10, 0, return_errors=False)


def test_broadcast_args_records_vectors():
    """broadcast_args tracks vector argument indices and keyword keys."""
    res = broadcast_args(5, [1, 2], step=10, factor=[2, 3])
    assert res.vector_arg_indices == [1]
    assert res.vector_kwarg_keys == ["factor"]

