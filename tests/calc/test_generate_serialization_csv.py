# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from tests.calc.serialization_cases import all_serialization_cases  # noqa: E402

_GEN_PATH = REPO_ROOT / "scripts" / "generate_serialization_test_csv.py"
_spec = importlib.util.spec_from_file_location("generate_serialization_test_csv", _GEN_PATH)
assert _spec and _spec.loader
gen = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = gen
_spec.loader.exec_module(gen)


def _case_by_id(case_id: str):
    return next(c for c in all_serialization_cases() if c.id == case_id)


def test_max_input_cols_covers_row_10_and_grid_2x5():
    cases = gen.ordered_cases()
    max_cols = gen.max_input_cols_for_cases(cases)
    assert max_cols >= 10
    _, ncols_2x5 = gen.grid_dimensions(_case_by_id("grid_2x5_sum").input_grid)
    _, ncols_row10 = gen.grid_dimensions(_case_by_id("row_10_sum").input_grid)
    assert ncols_2x5 == 5
    assert ncols_row10 == 10
    assert max_cols >= ncols_2x5
    assert max_cols >= ncols_row10


def test_generated_blocks_write_all_input_cells():
    cases = gen.ordered_cases()
    layout = gen.CsvLayout(max_input_cols=gen.max_input_cols_for_cases(cases))
    row = 2
    for case in cases:
        block = gen.block_rows(layout, case, row)
        nrows, ncols = gen.grid_dimensions(case.input_grid)
        if nrows == 0:
            row += len(block)
            continue
        for dr in range(nrows):
            data_row = block[1 + dr]
            for c in range(ncols):
                expected = case.input_grid[dr][c]
                cell = data_row[gen.COL_INPUT_START + c]
                if expected is None:
                    assert cell == "", f"{case.id} row {dr + 1} col {c + 1} should be empty"
                else:
                    assert cell != "", f"{case.id} row {dr + 1} col {c + 1} missing in CSV"
        data_top = row + 1 if nrows else row
        expected_range = gen.data_range_a1(data_top, nrows, ncols)
        header = block[0]
        py_formula = header[layout.col_python_formula]
        if case.mode != "error" and nrows:
            assert expected_range in py_formula, f"{case.id}: formula missing range {expected_range!r}"
        row += len(block)


def test_bool_cells_use_numeric_one_zero():
    cases = gen.ordered_cases()
    layout = gen.CsvLayout(max_input_cols=gen.max_input_cols_for_cases(cases))
    for case_id in ("bool_true", "bool_false", "bool_col_11_sum"):
        case = _case_by_id(case_id)
        block = gen.block_rows(layout, case, 2)
        for line in block[1:]:
            if not line[gen.COL_TEST_ID].startswith("input_row"):
                continue
            val = line[gen.COL_INPUT_START]
            if val:
                assert val in ("0", "1"), f"{case_id}: expected 0/1, got {val!r}"


def test_wide_case_formula_range_column_count():
    layout = gen.CsvLayout(max_input_cols=gen.max_input_cols_for_cases(gen.ordered_cases()))
    case = _case_by_id("row_10_sum")
    block = gen.block_rows(layout, case, 2)
    py_formula = block[0][layout.col_python_formula]
    m = re.search(r";([A-Z]+\d+(?::[A-Z]+\d+)?)\)", py_formula)
    assert m, py_formula
    a1 = m.group(1)
    assert "M" in a1 or a1.count(":") == 0
