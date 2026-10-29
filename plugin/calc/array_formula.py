# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Which formulas return an array, and where their result goes.

LibreOffice does not spill: ``=SORT(FILTER(...))`` written into one cell with
``setFormula`` shows a single value; the rest of the result is silently gone.
It has to be entered as an array formula on a range of the right size.

Detection is pure Python (no UNO). The write path measures size and calls
``setArrayFormula``. Adapted from nelson-mcp ``afc8cbd8`` / #2631.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, NamedTuple

if TYPE_CHECKING:
    from collections.abc import Iterator

from plugin.calc.address_utils import MAX_COL, MAX_ROW
from plugin.framework.errors import CalcError


class CellRect(NamedTuple):
    left: int
    top: int
    right: int
    bottom: int


# Functions whose result is an array when they are what the formula returns.
# Matching uses .upper(); names are the canonical English uppercase forms.
ARRAY_FUNCTIONS = frozenset({
    # Dynamic array functions
    "FILTER",
    "SORT",
    "SORTBY",
    "UNIQUE",
    "SEQUENCE",
    "RANDARRAY",
    # Matrix functions
    "TRANSPOSE",
    "MMULT",
    "MINVERSE",
    "FREQUENCY",
    "LINEST",
    "LOGEST",
    "TREND",
    "GROWTH",
    "MUNIT",
    # Shape / manipulation functions
    "CHOOSECOLS",
    "CHOOSEROWS",
    "HSTACK",
    "VSTACK",
    "TAKE",
    "DROP",
    "EXPAND",
    "WRAPROWS",
    "WRAPCOLS",
    "TOCOL",
    "TOROW",
    "TEXTSPLIT",
})

_ARRAY_PASSTHROUGH = frozenset({"IF", "IFS", "IFERROR", "IFNA", "LET", "CHOOSE", "SWITCH"})

# Cap on probe/result size to prevent out-of-memory on runaway arrays.
MAX_ARRAY_CELLS = 100_000

_CALL = re.compile(r"([A-Za-z_][A-Za-z0-9_.]*)\s*\(")


def skip_quoted(s: str, i: int) -> int:
    """Index after the closing quote; *i* points at the opening quote.

    The quote character is ``s[i]`` (``"`` or ``'``). A doubled quote is an
    escape (Calc ``""``, sheet-name ``''``), not the end. Unclosed runs to
    ``len(s)``.
    """
    q = s[i]
    j = i + 1
    n = len(s)
    while j < n:
        if s[j] == q:
            if j + 1 < n and s[j + 1] == q:
                j += 2
                continue
            return j + 1
        j += 1
    return n


def _scan_calls(formula: str) -> Iterator[tuple[str, int]]:
    text = formula[1:] if formula.startswith("=") else formula
    depth = 0
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == '"':
            i = skip_quoted(text, i)
            continue
        if ch == "'":
            i = skip_quoted(text, i)
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        else:
            match = _CALL.match(text, i)
            if match and (i == 0 or not (text[i - 1].isalnum() or text[i - 1] in "_.")):
                fn = match.group(1).upper()
                for prefix in ("_XLFN.", "_XLWS."):
                    if fn.startswith(prefix):
                        fn = fn[len(prefix):]
                yield fn, depth
                # Land on '(' so this '(' counts on the next iteration.
                # =SUM(FILTER(...)) then yields FILTER at depth 1.
                i = match.end() - 1
                continue
        i += 1


def top_level_calls(formula: str) -> list[str]:
    """Names of the functions called at the outermost level of *formula*.

    ``=SUM(FILTER(A1:A9;B1:B9>0))`` → ``["SUM"]``: FILTER's array is
    consumed by SUM, so the formula as a whole returns one value.
    Double-quoted strings (Calc ``""`` escape) and single-quoted sheet names are not calls.
    """
    return [name for name, depth in _scan_calls(formula) if depth == 0]


def returns_array(formula: str, forced: bool | None = None) -> bool:
    """True when *formula* should be entered as an array formula.

    *forced* (the tool's ``array`` argument) wins when given. Otherwise a
    formula is an array formula when a function it calls at the top level
    returns an array, or when a passthrough function at the top level
    contains an array function call at any depth.
    """
    if forced is not None:
        return bool(forced)
    if not isinstance(formula, str) or not formula.startswith("=") or len(formula) <= 1:
        return False
    top_array = False
    top_passthrough = False
    any_array = False
    for name, depth in _scan_calls(formula):
        if name in ARRAY_FUNCTIONS:
            any_array = True
            if depth == 0:
                top_array = True
        if depth == 0 and name in _ARRAY_PASSTHROUGH:
            top_passthrough = True
    return top_array or (top_passthrough and any_array)


def result_range(col: int, row: int, rows: int, cols: int) -> CellRect:
    """``(first_col, first_row, last_col, last_row)`` of a result at (*col*, *row*)."""
    end_col = col + cols - 1
    end_row = row + rows - 1
    if end_col > MAX_COL or end_row > MAX_ROW:
        raise CalcError(f"Result range passes sheet bounds: column {end_col} > {MAX_COL} or row {end_row} > {MAX_ROW}.")
    return CellRect(col, row, end_col, end_row)


def plan_target(c1: int, r1: int, c2: int, r2: int, rows: int, cols: int) -> tuple[CellRect, list[str]]:
    """Determine the destination rectangle and any truncation/padding notes."""
    explicit = (c1, r1) != (c2, r2)
    notes: list[str] = []
    if explicit:
        height, width = r2 - r1 + 1, c2 - c1 + 1
        if height * width > MAX_ARRAY_CELLS:
            raise CalcError(f"The target range is {height} x {width} cells, over the {MAX_ARRAY_CELLS}-cell limit.")
        if rows > height or cols > width:
            notes.append(f"The result is {rows} x {cols} but the range is {height} x {width}: the rest is cut (result_does_not_fit).")
        elif rows < height or cols < width:
            notes.append(f"The result is {rows} x {cols}, smaller than the range: the extra cells show #N/A.")
        target = CellRect(c1, r1, c2, r2)
    else:
        if rows * cols > MAX_ARRAY_CELLS:
            raise CalcError(f"The result is {rows} x {cols} cells, over the {MAX_ARRAY_CELLS}-cell limit.")
        target = result_range(c1, r1, rows, cols)
    return target, notes
