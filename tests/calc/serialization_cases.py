# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""Calc-realistic =PYTHON() serialization cases (shared by CSV generator and UNO tests).

Numeric checks use ``=SUM`` (primary — touches every cell) and ``=MAX`` (one easy
spot-check on the 4×4 split_grid block). Text/bool use first-cell pickup; grids
use ``INDEX`` identity or ×2.

Worker code is a **single expression** (no ``result =``); venv_sandbox uses the
last value when ``result`` is not assigned.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

SheetName = Literal["normal", "mixed", "grid", "nan", "errors"]
CaseMode = Literal["scalar", "matrix_index", "matrix_session", "ingress_only", "error"]

# No float() wrapper: inline =PYTHON("float(...)") breaks Calc's formula parser (#NAME?)
# on XLSX import; np.sum/max return values coerce via to_calc_compatible on egress.
_SUM_CODE = "np.sum(data)"
_MAX_CODE = "np.max(data)"
_NANSUM_CODE = "np.nansum(data)"
_GRID_DOUBLE_CODE = "np.array(data) * 2"


@dataclass(frozen=True)
class SerializationCase:
    id: str
    sheet: SheetName
    description: str
    input_grid: list[list[Any]]
    code: str
    mode: CaseMode = "scalar"
    calc_oracle: str | None = None
    expected: str | float | int | bool | None = None
    expected_error_substr: str | None = None
    matrix_rows: int = 0
    matrix_cols: int = 0
    tags: tuple[str, ...] = field(default_factory=tuple)
    notes: str = ""


def _grid_4x4() -> list[list[float]]:
    return [[float(r * 4 + c + 1) for c in range(4)] for r in range(4)]


def _grid_3x3() -> list[list[float]]:
    return [[float(r * 3 + c + 1) for c in range(3)] for r in range(3)]


def _grid_4x3_int() -> list[list[int]]:
    """12 integers — split_grid path, easy SUM mental check: 6×13 = 78."""
    return [[r * 3 + c + 1 for c in range(3)] for r in range(4)]


def all_serialization_cases() -> list[SerializationCase]:
    g4 = _grid_4x4()
    g3 = _grid_3x3()
    g4x3_int = _grid_4x3_int()
    flat_1x4 = [[1.0, 2.0, 3.0, 4.0]]
    col_4x1 = [[1.0], [2.0], [3.0], [4.0]]
    int_float_row = [[100, 2.5, 3, 4.5]]

    return [
        # --- normal: SUM / MAX vs Calc ---
        SerializationCase(
            id="scalar_single_cell",
            sheet="normal",
            description="Single cell constant (no data range)",
            input_grid=[],
            code="42",
            expected=42,
            tags=("scalar",),
        ),
        SerializationCase(
            id="scalar_row_sum",
            sheet="normal",
            description="Row vector SUM — flat data, floats (1+2+3+4=10)",
            input_grid=flat_1x4,
            code=_SUM_CODE,
            calc_oracle="SUM",
            expected=10.0,
            tags=("flat", "below_threshold", "float"),
        ),
        SerializationCase(
            id="scalar_col_sum",
            sheet="normal",
            description="Column vector SUM — flat data (1+2+3+4=10)",
            input_grid=col_4x1,
            code=_SUM_CODE,
            calc_oracle="SUM",
            expected=10.0,
            tags=("flat", "float"),
        ),
        SerializationCase(
            id="row_int_float_sum",
            sheet="normal",
            description="Row with ints + floats — SUM (100+2.5+3+4.5=110)",
            input_grid=int_float_row,
            code=_SUM_CODE,
            calc_oracle="SUM",
            expected=110.0,
            tags=("flat", "int", "float", "below_threshold"),
        ),
        SerializationCase(
            id="grid_3x3_sum",
            sheet="normal",
            description="3×3 SUM — nested list wire (<10 cells)",
            input_grid=g3,
            code=_SUM_CODE,
            calc_oracle="SUM",
            expected=45.0,
            tags=("below_threshold",),
            notes="9 cells: nested-list wire, not split_grid. SUM 1..9 = 45.",
        ),
        SerializationCase(
            id="grid_2x5_sum",
            sheet="normal",
            description="2×5 SUM — split_grid at exactly 10 cells (boundary)",
            input_grid=[[float(r * 5 + c + 1) for c in range(5)] for r in range(2)],
            code=_SUM_CODE,
            calc_oracle="SUM",
            expected=55.0,
            tags=("split_grid", "boundary"),
            notes="10 cells: first size that uses split_grid (BINARY_MIN_CELLS). SUM 1..10 = 55.",
        ),
        SerializationCase(
            id="row_10_sum",
            sheet="normal",
            description="1×10 row SUM — split_grid flat 1D shape",
            input_grid=[[float(i + 1) for i in range(10)]],
            code=_SUM_CODE,
            calc_oracle="SUM",
            expected=55.0,
            tags=("split_grid", "flat"),
            notes="10-cell row: split_grid with 1D shape on wire. SUM 1..10 = 55.",
        ),
        SerializationCase(
            id="grid_4x4_sum",
            sheet="normal",
            description="4×4 SUM — split_grid ingress (≥10 cells)",
            input_grid=g4,
            code=_SUM_CODE,
            calc_oracle="SUM",
            expected=136.0,
            tags=("split_grid",),
            notes="16 cells: split_grid wire. SUM 1..16 = 136.",
        ),
        SerializationCase(
            id="grid_4x3_int_sum",
            sheet="normal",
            description="4×3 integer SUM — split_grid, all whole numbers",
            input_grid=g4x3_int,
            code=_SUM_CODE,
            calc_oracle="SUM",
            expected=78.0,
            tags=("split_grid", "int"),
            notes="12 cells, values 1..12. SUM = 78.",
        ),
        SerializationCase(
            id="grid_4x4_max",
            sheet="normal",
            description="4×4 MAX spot-check — split_grid (answer is 16)",
            input_grid=g4,
            code=_MAX_CODE,
            calc_oracle="MAX",
            expected=16.0,
            tags=("split_grid",),
            notes="Easy eyeball check: max of 1..16 is 16.",
        ),
        SerializationCase(
            id="bool_true",
            sheet="normal",
            description="Calc logical TRUE — SUM treats as 1",
            input_grid=[[True]],
            code=_SUM_CODE,
            calc_oracle="SUM",
            expected=1.0,
            tags=("bool", "below_threshold"),
            notes="Manual CSV uses 1/0 (Calc logical SUM semantics; CSV import does not run =TRUE()).",
        ),
        SerializationCase(
            id="bool_false",
            sheet="normal",
            description="Calc logical FALSE — SUM treats as 0",
            input_grid=[[False]],
            code=_SUM_CODE,
            calc_oracle="SUM",
            expected=0.0,
            tags=("bool", "below_threshold"),
            notes="Manual CSV uses 1/0 (Calc logical SUM semantics; CSV import does not run =FALSE()).",
        ),
        SerializationCase(
            id="bool_col_11_sum",
            sheet="normal",
            description="11 logical values — split_grid SUM (7 TRUE + 4 FALSE = 7)",
            input_grid=[[v] for v in (True, True, True, False, True, False, True, False, True, True, False)],
            code=_SUM_CODE,
            calc_oracle="SUM",
            expected=7.0,
            tags=("split_grid", "bool"),
            notes="11 cells forces split_grid; CSV uses 1/0 per cell.",
        ),
        # --- mixed ---
        SerializationCase(
            id="mixed_zip_first",
            sheet="mixed",
            description="First cell zip text — INDEX must stay 02138",
            input_grid=[["02138", 1.0, 2.0]],
            code="data[0]",
            calc_oracle="INDEX_FIRST",
            expected="02138",
            tags=("zip_code", "mixed", "below_threshold"),
            notes="calc_oracle =INDEX picks first cell; must remain text.",
        ),
        SerializationCase(
            id="mixed_cols_sum",
            sheet="mixed",
            description="Mixed grid SUM — Calc ignores text (1+10+2+20+…=110)",
            input_grid=[
                [1.0, "label", 10.0],
                [2.0, "x", 20.0],
                [3.0, "y", 30.0],
                [4.0, "z", 40.0],
            ],
            code="sum(v for row in data for v in row if isinstance(v, (int, float)))",
            calc_oracle="SUM",
            expected=110.0,
            tags=("mixed", "split_grid"),
            notes="Same SUM oracle as numeric tests; Calc skips label cells.",
        ),
        SerializationCase(
            id="mixed_unicode_label",
            sheet="mixed",
            description="Unicode first cell — INDEX must stay São Paulo",
            input_grid=[["São Paulo", 5.0], ["北京", 7.0], [None, 9.0], ["x", 11.0]],
            code="data[0]",
            calc_oracle="INDEX_FIRST",
            expected="São Paulo",
            tags=("mixed", "unicode", "split_grid"),
        ),
        # --- grid returns (matrix index) ---
        SerializationCase(
            id="grid_return_double",
            sheet="grid",
            description="Return data×2 — INDEX oracle per cell",
            input_grid=g4,
            code=_GRID_DOUBLE_CODE,
            mode="matrix_index",
            matrix_rows=4,
            matrix_cols=4,
            calc_oracle="MULT2",
            tags=("split_grid", "egress_grid"),
            notes="Matrix formula; each cell should match INDEX×2.",
        ),
        SerializationCase(
            id="grid_return_identity",
            sheet="grid",
            description="Echo input grid — INDEX identity round-trip",
            input_grid=g4,
            code="data",
            mode="matrix_index",
            matrix_rows=4,
            matrix_cols=4,
            calc_oracle="IDENTITY",
            tags=("split_grid", "egress_grid"),
            notes="Matrix block should reproduce the 4×4 input.",
        ),
        # --- nan / empty ---
        SerializationCase(
            id="nan_holes_nansum",
            sheet="nan",
            description="4×4 with empty cells — SUM / nansum (104)",
            input_grid=[
                [1.0, None, 3.0, 4.0],
                [5.0, 6.0, None, 8.0],
                [9.0, 10.0, 11.0, 12.0],
                [None, 14.0, 15.0, 16.0],
            ],
            code=_NANSUM_CODE,
            calc_oracle="SUM",
            expected=104.0,
            tags=("split_grid", "empty", "nan"),
            notes="Empty cells → NaN on wire; Calc SUM skips blanks as 0.",
        ),
        # --- errors ---
        SerializationCase(
            id="error_syntax",
            sheet="errors",
            description="Python syntax error → Error: in cell",
            input_grid=[[1.0]],
            code="1 +",
            mode="error",
            expected_error_substr="Error:",
            tags=("error",),
        ),
        SerializationCase(
            id="error_bad_import",
            sheet="errors",
            description="Blocked import → Error: in cell",
            input_grid=[[1.0]],
            code="import os",
            mode="error",
            expected_error_substr="Error:",
            tags=("error",),
        ),
    ]


def cases_by_sheet(sheet: SheetName) -> list[SerializationCase]:
    return [c for c in all_serialization_cases() if c.sheet == sheet]


SHEET_ORDER: tuple[SheetName, ...] = ("normal", "mixed", "grid", "nan", "errors")
