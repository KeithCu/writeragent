# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Column vectorization logic for Calc Spreadsheet → Python Import.

Groups consecutive filled-down cells in a column that share one element-wise
translation over a single same-sheet numeric cell reference.
"""

from __future__ import annotations

import ast
import re
from typing import TYPE_CHECKING

from plugin.calc.address_utils import format_range, parse_address, split_sheet_prefix
from plugin.calc.spreadsheet_import.models import SheetModel, TranslationResult, VectorRun

if TYPE_CHECKING:
    from collections.abc import Mapping

_ELEMENTWISE_NP = frozenset({
    "abs",
    "sqrt",
    "sign",
    "floor",
    "ceil",
    "trunc",
    "exp",
    "log",
    "log10",
    "sin",
    "cos",
    "tan",
    "arcsin",
    "arccos",
    "arctan",
    "arctan2",
    "arcsinh",
    "arccosh",
    "arctanh",
    "sinh",
    "cosh",
    "tanh",
    "degrees",
    "radians",
    "asarray",
    "int64",
})

_ELEMENTWISE_OPS = (
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
    ast.FloorDiv,
    ast.Mod,
    ast.Pow,
    ast.USub,
    ast.UAdd,
    ast.Eq,
    ast.NotEq,
    ast.Lt,
    ast.LtE,
    ast.Gt,
    ast.GtE,
)


def is_elementwise_code(code: str) -> bool:
    """True when ``code`` evaluates element-wise on a 1D column ``data``.

    The shared ``=PY`` gives the whole column as ``data`` and picks each cell's row
    from the result. Text helpers print the array, aggregates fold it, and
    ``x if c else y`` cannot test an array.
    """
    try:
        tree = ast.parse(code, mode="eval")
    except SyntaxError:
        return False

    for node in ast.walk(tree):
        if isinstance(node, (ast.Expression, ast.BinOp, ast.UnaryOp, ast.Load) + _ELEMENTWISE_OPS):
            continue
        if isinstance(node, ast.Compare):
            if len(node.ops) != 1 or not isinstance(node.ops[0], _ELEMENTWISE_OPS):
                return False
            continue
        if isinstance(node, ast.Constant):
            if type(node.value) not in (int, float):
                return False
            continue
        if isinstance(node, ast.Name):
            if node.id not in ("data", "np"):
                return False
            continue
        if isinstance(node, ast.Attribute):
            if not (isinstance(node.value, ast.Name) and node.value.id == "np" and node.attr in _ELEMENTWISE_NP):
                return False
            continue
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Attribute):
                return False
            continue
        if isinstance(node, ast.keyword):
            if node.arg != "dtype":
                return False
            continue
        return False
    return True


def _single_numeric_ref(model: SheetModel, translation: TranslationResult | None) -> tuple[int, int, str] | None:
    if translation is None or not (translation.ok and translation.code and translation.data_ranges and len(translation.data_ranges) == 1):
        return None
    ref = translation.data_ranges[0]
    # A range or window shifts by a different amount on each row, and another sheet's
    # values are not in the model so they cannot be checked as numbers.
    if ":" in ref or split_sheet_prefix(ref)[0] is not None:
        return None
    # Check model.cells before parse_address so a named range (e.g. PRICES)
    # returns None instead of hitting parse_address's column-letter contract.
    # One text or blank cell would make numpy fail for the whole shared =PY column.
    record = model.cells.get(ref)
    if record is None or isinstance(record.value, bool) or not isinstance(record.value, (int, float)):
        return None
    if not is_elementwise_code(translation.code):
        return None
    ref_col, ref_row = parse_address(ref)
    return ref_col, ref_row, translation.code


def detect_vectorized_columns(
    model: SheetModel,
    translations: Mapping[str, TranslationResult | None],
) -> list[VectorRun]:
    """Detect consecutive filled-down columns that one shared ``=PY()`` can compute."""
    runs: list[VectorRun] = []
    current_cells: list[str] = []
    current_key: tuple[int, str, int, int] | None = None
    first_ref_row = 0
    last_row = -1

    def _flush() -> None:
        if len(current_cells) >= 2 and current_key is not None:
            _col, code, ref_col, _row_offset = current_key
            # CalcRange has __array__ but no arithmetic operators (calc_range.py:242).
            # Multi-arg =PY() passes all arguments in data[], so index data[0] (the column range)
            # by int(data[1]) (the 0-based row offset). The element-wise gate has already rejected
            # string constants, so this only touches the name `data`.
            vec_code = re.sub(r"(?<!np\.asarray\()\bdata\b", "np.asarray(data[0]).ravel()", code)
            vec_code = f"({vec_code})[int(data[1])]"
            col_range = format_range(ref_col, first_ref_row, ref_col, first_ref_row + len(current_cells) - 1)
            runs.append(VectorRun(cells=list(current_cells), code=vec_code, column=col_range))

    for addr in sorted(translations, key=parse_address):
        ref_info = _single_numeric_ref(model, translations[addr])
        col, row = parse_address(addr)
        # Skip cells that cannot share a vector =PY, including same-column recurrences
        # (e.g. =ROUND(B1*1.05;2) in B2:B4) where reading the column range creates a circular ref.
        if ref_info is None or ref_info[0] == col:
            _flush()
            current_cells = []
            current_key = None
            continue
        ref_col, ref_row, code = ref_info
        key = (col, code, ref_col, ref_row - row)
        if current_cells and key == current_key and row == last_row + 1:
            current_cells.append(addr)
        else:
            _flush()
            current_cells = [addr]
            current_key = key
            first_ref_row = ref_row
        last_row = row

    _flush()
    return runs
