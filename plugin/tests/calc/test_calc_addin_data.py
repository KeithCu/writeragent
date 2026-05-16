# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

import pytest

from plugin.calc.calc_addin_data import (
    MAX_PYTHON_DATA_CELLS,
    calc_addin_data_to_python,
    check_python_data_size,
    count_cells,
    values_from_inspector_range,
)


def test_none_returns_none():
    assert calc_addin_data_to_python(None) is None
    assert count_cells(None) == 0


def test_scalar_wraps_2d():
    assert calc_addin_data_to_python(42) == [[42]]
    assert calc_addin_data_to_python(3.14) == [[3.14]]
    assert calc_addin_data_to_python("hi") == [["hi"]]


def test_empty_string_becomes_none_in_cell():
    assert calc_addin_data_to_python("") == [[None]]


def test_2d_tuple_range():
    raw = ((1.0, 2.0), (3.0, None))
    assert calc_addin_data_to_python(raw) == [[1.0, 2.0], [3.0, None]]


def test_1d_row_sequence():
    assert calc_addin_data_to_python((10, 20, 30)) == [[10, 20, 30]]


def test_column_vector_folded_for_sum_data0():
    """Column ranges from Calc: ((1.0,), (5.0,), ...) → one row for sum(data[0])."""
    raw = ((1.0,), (5.0,), (7.0,), (4.0,))
    assert calc_addin_data_to_python(raw) == [[1.0, 5.0, 7.0, 4.0]]
    assert sum(calc_addin_data_to_python(raw)[0]) == 17.0


def test_count_cells_2d():
    assert count_cells([[1, 2], [3]]) == 3


def test_check_python_data_size_rejects_large():
    big = [[0] * 1000 for _ in range(300)]
    assert count_cells(big) == 300_000
    err = check_python_data_size(big, max_cells=MAX_PYTHON_DATA_CELLS)
    assert err is not None
    assert "300000" in err or "300,000" in err.replace(",", "")


def test_check_python_data_size_ok():
    assert check_python_data_size([[1, 2]]) is None


def test_values_from_inspector_range():
    raw = [[{"address": "A1", "value": 1, "formula": None, "type": "value"}]]
    assert values_from_inspector_range(raw) == [[1]]


def test_values_from_inspector_range_column_folded():
    raw = [
        [{"value": 1}],
        [{"value": 5}],
        [{"value": 7}],
    ]
    assert values_from_inspector_range(raw) == [[1, 5, 7]]
