# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

from tests.calc.serialization_cases import (
    SHEET_ORDER,
    all_serialization_cases,
    cases_by_sheet,
)
import pytest


def test_all_sheets_have_cases():
    for sheet in SHEET_ORDER:
        assert len(cases_by_sheet(sheet)) >= 1, sheet


def test_case_ids_unique():
    ids = [c.id for c in all_serialization_cases()]
    assert len(ids) == len(set(ids))


@pytest.mark.parametrize(
    "expected, expected_2",
    [
        pytest.param("grid_4x4_sum", 136.0, id="test_grid_4x4_sum_expected"),
        pytest.param("grid_3x3_sum", 45.0, id="test_below_threshold_3x3"),
    ],
)
def test_grid_4x4_sum_expected(expected, expected_2):
    case = next(c for c in all_serialization_cases() if c.id == expected)
    assert case.expected == expected_2
    assert "below_threshold" in case.tags

def test_no_average_or_min_cases():
    ids = {c.id for c in all_serialization_cases()}
    assert "grid_4x4_mean" not in ids
    assert "grid_4x4_min" not in ids
    assert "nan_count_nonempty" not in ids


@pytest.mark.parametrize(
    "expected",
    [
        pytest.param("row_int_float_sum", id="test_int_float_sum_case"),
        pytest.param("mixed_cols_sum", id="test_mixed_sum_uses_calc_oracle"),
    ],
)
def test_int_float_sum_case(expected):
    case = next(c for c in all_serialization_cases() if c.id == expected)
    assert case.calc_oracle == "SUM"
    assert case.expected == 110.0

def test_error_cases_marked():
    errors = cases_by_sheet("errors")
    assert all(c.mode == "error" for c in errors)
    assert all(c.expected_error_substr for c in errors)


def test_bool_col_11_below_threshold_case():
    case = next(c for c in all_serialization_cases() if c.id == "bool_col_11_sum")
    assert case.expected == 7.0
    assert "below_threshold" in case.tags
    assert len(case.input_grid) == 11


def test_split_grid_boundary_case():
    """Spreadsheet fixture: fixed 2×5 grid (fits 5×5 generator layout; not tied to BINARY_MIN_CELLS)."""
    case = next(c for c in all_serialization_cases() if c.id == "grid_2x5_sum")
    assert case.expected == 55.0
    assert "boundary" in case.tags
    assert len(case.input_grid) == 2
    assert len(case.input_grid[0]) == 5


def test_multi_sheet_has_cases():
    multi = cases_by_sheet("multi")
    assert len(multi) >= 4
    assert all(c.input_grid_b is not None for c in multi)
