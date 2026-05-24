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


def test_all_sheets_have_cases():
    for sheet in SHEET_ORDER:
        assert len(cases_by_sheet(sheet)) >= 1, sheet


def test_case_ids_unique():
    ids = [c.id for c in all_serialization_cases()]
    assert len(ids) == len(set(ids))


def test_grid_4x4_sum_expected():
    case = next(c for c in all_serialization_cases() if c.id == "grid_4x4_sum")
    assert case.expected == 136.0
    assert "split_grid" in case.tags


def test_below_threshold_3x3():
    case = next(c for c in all_serialization_cases() if c.id == "grid_3x3_sum")
    assert case.expected == 45.0
    assert "below_threshold" in case.tags


def test_no_average_or_min_cases():
    ids = {c.id for c in all_serialization_cases()}
    assert "grid_4x4_mean" not in ids
    assert "grid_4x4_min" not in ids
    assert "nan_count_nonempty" not in ids


def test_int_float_sum_case():
    case = next(c for c in all_serialization_cases() if c.id == "row_int_float_sum")
    assert case.calc_oracle == "SUM"
    assert case.expected == 110.0


def test_mixed_sum_uses_calc_oracle():
    case = next(c for c in all_serialization_cases() if c.id == "mixed_cols_sum")
    assert case.calc_oracle == "SUM"
    assert case.expected == 110.0


def test_error_cases_marked():
    errors = cases_by_sheet("errors")
    assert all(c.mode == "error" for c in errors)
    assert all(c.expected_error_substr for c in errors)


def test_bool_col_11_split_grid_case():
    case = next(c for c in all_serialization_cases() if c.id == "bool_col_11_sum")
    assert case.expected == 7.0
    assert "split_grid" in case.tags
    assert len(case.input_grid) == 11


def test_split_grid_boundary_case():
    case = next(c for c in all_serialization_cases() if c.id == "grid_2x5_sum")
    assert case.expected == 55.0
    assert "split_grid" in case.tags
    assert "boundary" in case.tags


def test_multi_sheet_has_cases():
    multi = cases_by_sheet("multi")
    assert len(multi) >= 4
    assert all(c.input_grid_b is not None for c in multi)
