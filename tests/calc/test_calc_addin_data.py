# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

import pytest

from plugin.calc.calc_addin_data import (
    calc_addin_args_to_python,
    calc_addin_data_to_python,
    check_python_data_size,
    check_python_multi_data_size,
    count_cells,
    finalize_python_data,
    pack_calc_data_for_wire,
    pack_calc_multi_data_for_wire,
    split_python_addin_data_args,
    values_from_inspector_range,
)
from plugin.scripting.config_limits import python_max_data_cells_default
from plugin.scripting.payload_codec import child_unpack_data, is_multi_data, is_split_grid, wire_cell_count


def test_none_returns_none():
    assert calc_addin_data_to_python(None) is None
    assert count_cells(None) == 0


def test_scalar_is_flat_list():
    assert calc_addin_data_to_python(42) == [42]
    assert calc_addin_data_to_python(3.14) == [3.14]
    assert sum(calc_addin_data_to_python(42)) == 42


def test_empty_string_becomes_none_in_cell():
    assert calc_addin_data_to_python("") == [None]


def test_2d_tuple_range_stays_2d():
    raw = ((1.0, 2.0), (3.0, None))
    assert calc_addin_data_to_python(raw) == [[1.0, 2.0], [3.0, None]]


def test_1d_row_sequence_flat():
    assert calc_addin_data_to_python((10, 20, 30)) == [10, 20, 30]
    assert sum(calc_addin_data_to_python((10, 20, 30))) == 60


def test_column_vector_flat_for_sum_data():
    raw = ((1.0,), (5.0,), (7.0,), (4.0,))
    assert calc_addin_data_to_python(raw) == [1.0, 5.0, 7.0, 4.0]
    assert sum(calc_addin_data_to_python(raw)) == 17.0


def test_count_cells_2d():
    assert count_cells([[1, 2], [3]]) == 3


def test_count_cells_flat():
    assert count_cells([1, 2, 3]) == 3


def test_check_python_data_size_rejects_large():
    big = [[0] * 1000 for _ in range(300)]
    assert count_cells(big) == 300_000
    err = check_python_data_size(big, max_cells=python_max_data_cells_default())
    assert err is not None
    assert "300000" in err or "300,000" in err.replace(",", "")


def test_check_python_data_size_ok():
    assert check_python_data_size([1, 2]) is None


def test_values_from_inspector_range():
    raw = [[{"address": "A1", "value": 1, "formula": None, "type": "value"}]]
    assert values_from_inspector_range(raw) == [1]


def test_values_from_inspector_range_column_flat():
    raw = [
        [{"value": 1}],
        [{"value": 5}],
        [{"value": 7}],
    ]
    assert values_from_inspector_range(raw) == [1, 5, 7]
    assert sum(values_from_inspector_range(raw)) == 13


def test_finalize_python_data_nested_row():
    assert finalize_python_data([[1, 2, 3]]) == [1, 2, 3]


def test_finalize_python_data_already_flat():
    assert finalize_python_data([1, 2]) == [1, 2]


def test_pack_calc_data_for_wire_uses_split_grid_at_threshold():
    """Calc-sized numeric range uses split_grid when cell count >= BINARY_MIN_CELLS."""
    from plugin.scripting.payload_codec import BINARY_MIN_CELLS
    from tests.scripting.payload_codec_test_support import NUMERIC_AT_THRESHOLD

    wire = pack_calc_data_for_wire(NUMERIC_AT_THRESHOLD, force="auto")
    assert is_split_grid(wire)
    assert count_cells(wire) == BINARY_MIN_CELLS
    assert wire_cell_count(wire) == BINARY_MIN_CELLS


def test_pack_calc_data_for_wire_uses_list_below_threshold():
    """3x3 Calc range stays nested list on wire (below split_grid threshold)."""
    grid = [[float(i)] * 3 for i in range(3)]
    wire = pack_calc_data_for_wire(grid, force="never")
    assert isinstance(wire, list)
    assert not is_split_grid(wire)


def test_text_true_string_stays_string_when_no_sets_provided():
    """Text fidelity: literal text True is not coerced if no coercion sets are passed."""
    assert calc_addin_data_to_python("True") == ["True"]


def test_logical_coercion_standard_scope():
    """Verify coercion for formulas, plain text, and Python constants."""
    true_s = {"=TRUE()", "TRUE", "True"}
    false_s = {"=FALSE()", "FALSE", "False"}

    # Formulas
    assert calc_addin_data_to_python("=TRUE()", true_s, false_s) == [True]
    assert calc_addin_data_to_python(" =FALSE() ", true_s, false_s) == [False]

    # Plain Text
    assert calc_addin_data_to_python("TRUE", true_s, false_s) == [True]
    assert calc_addin_data_to_python("FALSE", true_s, false_s) == [False]

    # Python constants
    assert calc_addin_data_to_python("True", true_s, false_s) == [True]
    assert calc_addin_data_to_python("False", true_s, false_s) == [False]

    # Mixed 2D grid
    grid = [["=TRUE()", "Normal"], ["False", 10]]
    expected = [[True, "Normal"], [False, 10]]
    assert calc_addin_data_to_python(grid, true_s, false_s) == expected


def test_localized_coercion_simulation():
    """Verify coercion with simulated localized strings (e.g. German WAHR)."""
    true_s = {"=TRUE()", "TRUE", "True", "=WAHR()", "WAHR", "Wahr"}
    false_s = {"=FALSE()", "FALSE", "False", "=FALSCH()", "FALSCH", "Falsch"}

    assert calc_addin_data_to_python("=WAHR()", true_s, false_s) == [True]
    assert calc_addin_data_to_python("Falsch", true_s, false_s) == [False]


def test_logical_coercion_negative_controls():
    """Ensure unrelated strings are not coerced even if they look similar."""
    true_s = {"=TRUE()", "TRUE", "True"}
    false_s = {"=FALSE()", "FALSE", "False"}

    # Not in set
    assert calc_addin_data_to_python("true", true_s, false_s) == ["true"]
    assert calc_addin_data_to_python("True()", true_s, false_s) == ["True()"]
    assert calc_addin_data_to_python("VERDADERO", true_s, false_s) == ["VERDADERO"]


def test_calc_logical_float_sums_through_wire():
    """Calc logical cells arrive as 1.0/0.0; split_grid numeric path supports np.sum."""
    np = pytest.importorskip("numpy")
    from plugin.scripting.payload_codec import child_unpack_data

    wire = pack_calc_data_for_wire([1.0])
    assert not is_split_grid(wire)
    assert child_unpack_data(wire) == pytest.approx(1.0)
    assert float(np.sum(child_unpack_data(wire))) == pytest.approx(1.0)


def test_split_python_addin_data_args_empty():
    assert split_python_addin_data_args(None) == []
    assert split_python_addin_data_args(()) == []


def test_split_python_addin_data_args_scalar():
    assert split_python_addin_data_args(2.0) == [2.0]


def test_split_python_addin_data_args_single_wrapped_range():
    col = ((1.0,), (2.0,), (3.0,))
    assert split_python_addin_data_args((col,)) == [col]


def test_split_python_addin_data_args_legacy_bare_column():
    col = ((1.0,), (2.0,), (3.0,))
    assert split_python_addin_data_args(col) == [col]


def test_split_python_addin_data_args_multi_scalar():
    assert split_python_addin_data_args((1.0, 2.0, 3.0)) == [1.0, 2.0, 3.0]


def test_split_python_addin_data_args_multi_grid():
    col_a = ((1.0,), (2.0,))
    col_b = ((3.0,), (4.0,))
    assert split_python_addin_data_args((col_a, col_b)) == [col_a, col_b]


def test_calc_addin_args_to_python_multi_range():
    col_a = ((1.0,), (2.0,), (3.0,))
    col_b = ((4.0,), (5.0,))
    result = calc_addin_args_to_python((col_a, col_b))
    assert result == [[1.0, 2.0, 3.0], [4.0, 5.0]]


def test_calc_addin_args_from_split_matches_to_python():
    col_a = ((1.0,), (2.0,))
    col_b = ((3.0,), (4.0,))
    args = split_python_addin_data_args((col_a, col_b))
    from plugin.calc.calc_addin_data import calc_addin_args_from_split

    assert calc_addin_args_from_split(args) == calc_addin_args_to_python((col_a, col_b))


def test_check_python_multi_data_size_combined():
    ranges = [[1.0] * 100, [2.0] * 100]
    assert check_python_multi_data_size(ranges, max_cells=150) is not None
    assert check_python_multi_data_size(ranges, max_cells=250) is None


def test_pack_calc_multi_data_for_wire_roundtrip():
    np = pytest.importorskip("numpy")
    ranges = [[1.0, 2.0, 3.0], [4.0, 5.0, 6.0]]
    wire = pack_calc_multi_data_for_wire(ranges, force="always")
    assert is_multi_data(wire)
    assert wire_cell_count(wire) == 6
    unpacked = child_unpack_data(wire)
    assert len(unpacked) == 2
    assert float(np.sum(unpacked[0])) == pytest.approx(6.0)
    assert float(np.sum(unpacked[1])) == pytest.approx(15.0)
