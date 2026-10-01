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

# Functions whose result is an array when they are what the formula returns.
ARRAY_FUNCTIONS = frozenset({"FILTER", "SORT", "SORTBY", "UNIQUE", "SEQUENCE", "RANDARRAY", "TRANSPOSE", "MMULT", "MINVERSE", "FREQUENCY", "CHOOSECOLS", "CHOOSEROWS", "HSTACK", "VSTACK", "TAKE", "DROP", "EXPAND", "WRAPROWS", "WRAPCOLS", "TOCOL", "TOROW"})

MAX_ARRAY_CELLS = 100_000

_CALL = re.compile(r"([A-Za-z_][A-Za-z0-9_.]*)\s*\(")


def top_level_calls(formula: str) -> list[str]:
    """Names of the functions called at the outermost level of *formula*.

    ``=SUM(FILTER(A1:A9;B1:B9>0))`` → ``["SUM"]``: FILTER's array is
    consumed by SUM, so the formula as a whole returns one value.
    Double-quoted strings (Calc ``""`` escape) are not calls.
    """
    text = formula[1:] if formula.startswith("=") else formula
    names: list[str] = []
    depth = 0
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == '"':
            i = _skip_double_quoted(text, i)
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif depth == 0:
            match = _CALL.match(text, i)
            if match and (i == 0 or not (text[i - 1].isalnum() or text[i - 1] in "_.")):
                names.append(match.group(1).upper())
                # Land on '(' so the next iteration raises depth; nested
                # FILTER in =SUM(FILTER(...)) stays off the top-level list.
                i = match.end() - 1
                continue
        i += 1
    return names


def returns_array(formula: str, forced: bool | None = None) -> bool:
    """True when *formula* should be entered as an array formula.

    *forced* (the tool's ``array`` argument) wins when given. Otherwise a
    formula is an array formula when a function it calls at the top level
    returns an array. LET, XLOOKUP and plain range arithmetic can go either
    way: callers pass ``array=true`` for those.
    """
    if forced is not None:
        return bool(forced)
    if not isinstance(formula, str) or not formula.startswith("="):
        return False
    return any(name in ARRAY_FUNCTIONS for name in top_level_calls(formula))


def result_range(col: int, row: int, rows: int, cols: int) -> tuple[int, int, int, int]:
    """``(first_col, first_row, last_col, last_row)`` of a result at (*col*, *row*)."""
    return col, row, col + cols - 1, row + rows - 1


def _skip_double_quoted(s: str, i: int) -> int:
    """Index after the closing ``"``; *i* points at the opening quote."""
    j = i + 1
    n = len(s)
    while j < n:
        if s[j] == '"':
            if j + 1 < n and s[j + 1] == '"':
                j += 2
                continue
            return j + 1
        j += 1
    return n
