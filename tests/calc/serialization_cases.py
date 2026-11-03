# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""Calc-realistic =PYTHON() serialization cases (shared by spreadsheet generator and UNO tests).

Numeric checks use ``=SUM`` (primary — touches every cell) and ``=MAX`` (one easy
spot-check on the 4×4 split_grid block). Text/bool use first-cell pickup; grids
use ``INDEX`` identity or ×2.

Worker code is a **single expression** (no ``result =``); venv_sandbox uses the
last value when ``result`` is not assigned.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from plugin.scripting.payload_codec import BINARY_MIN_CELLS

SheetName = Literal["normal", "multi", "mixed", "grid", "nan", "errors"]
CaseMode = Literal["scalar", "matrix_index", "matrix_session", "ingress_only", "error"]

# No float() wrapper: inline =PYTHON("float(...)") breaks Calc's formula parser (#NAME?)
# on XLSX import; np.sum/max return values coerce via to_calc_compatible on egress.
_SUM_CODE = "np.sum(data)"
_MAX_CODE = "np.max(data)"
_NANSUM_CODE = "np.nansum(data)"
_GRID_DOUBLE_CODE = "np.array(data) * 2"
_MULTI_SUM_CODE = "sum(np.sum(d) for d in data)"


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
    input_grid_b: list[list[Any]] | None = None


def case_input_grids(case: SerializationCase) -> tuple[list[list[Any]], ...]:
    """Input grids for a case: group 1 (``input_grid``), optional group 2 (``input_grid_b``)."""
    if case.input_grid_b is not None:
        return (case.input_grid, case.input_grid_b)
    if case.input_grid:
        return (case.input_grid,)
    return ()


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
            description="3×3 SUM — nested list wire (below BINARY_MIN_CELLS)",
            input_grid=g3,
            code=_SUM_CODE,
            calc_oracle="SUM",
            expected=45.0,
            tags=("below_threshold",),
            notes=f"9 cells (< {BINARY_MIN_CELLS}): nested-list wire. SUM 1..9 = 45.",
        ),
        SerializationCase(
            id="grid_2x5_sum",
            sheet="normal",
            description="2×5 SUM — 10-cell spreadsheet fixture (fixed 5×5 layout per range)",
            input_grid=[[float(r * 5 + c + 1) for c in range(5)] for r in range(2)],
            code=_SUM_CODE,
            calc_oracle="SUM",
            expected=55.0,
            tags=("boundary",),
            notes=(
                f"Fixed 2×5 for manual spreadsheet (generator max 5×5 per range). "
                f"Production split_grid threshold is BINARY_MIN_CELLS={BINARY_MIN_CELLS} (pytest uses separate grids)."
            ),
        ),
        SerializationCase(
            id="grid_4x4_sum",
            sheet="normal",
            description="4×4 SUM — below split_grid threshold (nested list wire)",
            input_grid=g4,
            code=_SUM_CODE,
            calc_oracle="SUM",
            expected=136.0,
            tags=("below_threshold",),
            notes=f"16 cells (< {BINARY_MIN_CELLS}): nested-list wire. SUM 1..16 = 136.",
        ),
        SerializationCase(
            id="grid_4x3_int_sum",
            sheet="normal",
            description="4×3 integer SUM — below threshold, all whole numbers",
            input_grid=g4x3_int,
            code=_SUM_CODE,
            calc_oracle="SUM",
            expected=78.0,
            tags=("below_threshold", "int"),
            notes=f"12 cells (< {BINARY_MIN_CELLS}), values 1..12. SUM = 78.",
        ),
        SerializationCase(
            id="grid_4x4_max",
            sheet="normal",
            description="4×4 MAX spot-check — below threshold (answer is 16)",
            input_grid=g4,
            code=_MAX_CODE,
            calc_oracle="MAX",
            expected=16.0,
            tags=("below_threshold",),
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
            notes="Manual sheet uses 1/0 (Calc logical SUM semantics; import does not run =TRUE()).",
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
            notes="Manual sheet uses 1/0 (Calc logical SUM semantics; import does not run =FALSE()).",
        ),
        SerializationCase(
            id="bool_col_11_sum",
            sheet="normal",
            description="11 logical values — below threshold SUM (7 TRUE + 4 FALSE = 7)",
            input_grid=[[v] for v in (True, True, True, False, True, False, True, False, True, True, False)],
            code=_SUM_CODE,
            calc_oracle="SUM",
            expected=7.0,
            tags=("below_threshold", "bool"),
            notes=f"11 cells (< {BINARY_MIN_CELLS}); sheet uses 1/0 per cell.",
        ),
        # --- multi: varargs (two ranges, small grids) ---
        SerializationCase(
            id="multi_two_2x2_sum",
            sheet="multi",
            description="Two 2×2 ranges — combined SUM (1+2+3+4+5+6+7+8=36)",
            input_grid=[[1.0, 2.0], [3.0, 4.0]],
            input_grid_b=[[5.0, 6.0], [7.0, 8.0]],
            code=_MULTI_SUM_CODE,
            calc_oracle="SUM",
            expected=36.0,
            tags=("multi_range", "varargs", "below_threshold"),
            notes="data[0] and data[1] are 2×2; oracle =SUM(r1,r2).",
        ),
        SerializationCase(
            id="multi_2x1_plus_1x2",
            sheet="multi",
            description="2×1 column + 1×2 row — shape mismatch (1+2+3+4=10)",
            input_grid=[[1.0], [2.0]],
            input_grid_b=[[3.0, 4.0]],
            code=_MULTI_SUM_CODE,
            calc_oracle="SUM",
            expected=10.0,
            tags=("multi_range", "varargs", "below_threshold"),
            notes="Exercises list-of-grids wire (not one 2D block).",
        ),
        SerializationCase(
            id="multi_two_rows_sum",
            sheet="multi",
            description="Two 1×3 rows — flat per-range lists (1+2+3+4+5+6=21)",
            input_grid=[[1.0, 2.0, 3.0]],
            input_grid_b=[[4.0, 5.0, 6.0]],
            code=_MULTI_SUM_CODE,
            calc_oracle="SUM",
            expected=21.0,
            tags=("multi_range", "varargs", "flat", "below_threshold"),
        ),
        SerializationCase(
            id="multi_scalar_plus_row",
            sheet="multi",
            description="Single cell + 1×3 row — scalar range + flat list (5+1+2+3=11)",
            input_grid=[[5.0]],
            input_grid_b=[[1.0, 2.0, 3.0]],
            code=_MULTI_SUM_CODE,
            calc_oracle="SUM",
            expected=11.0,
            tags=("multi_range", "varargs", "below_threshold"),
        ),
        SerializationCase(
            id="multi_mixed_small",
            sheet="multi",
            description="Mixed 2×2 + numeric 1×1 — numeric-only sum (1+2+3=6)",
            input_grid=[[1.0, "a"], [2.0, "b"]],
            input_grid_b=[[3.0]],
            code="sum(v for g in data for row in g for v in row if isinstance(v, (int, float)))",
            calc_oracle="SUM",
            expected=6.0,
            tags=("multi_range", "varargs", "mixed", "below_threshold"),
            notes="Strings in range 1; Calc SUM skips text in oracle cells.",
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
            tags=("mixed", "below_threshold"),
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
            tags=("mixed", "unicode", "below_threshold"),
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
            tags=("below_threshold", "egress_grid"),
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
            tags=("below_threshold", "egress_grid"),
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
            tags=("below_threshold", "empty", "nan"),
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


SHEET_ORDER: tuple[SheetName, ...] = ("normal", "multi", "mixed", "grid", "nan", "errors")
