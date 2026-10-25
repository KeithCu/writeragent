# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Fill-down / fill-across for a single Calc formula into a 1-D or 2-D range.

``write_formula_range`` used to pin the same formula text on every cell
(``[formula] * n``). Ordinary formulas now shift relative A1 refs per cell
(``$`` stays absolute). ``=PY`` / PYTHON / PYTHONFUNCTION formulas use a
direction-aware span policy: only refs whose span does not cross the fill
axis are adjusted; other refs outside the quoted Python string stay
verbatim so a block like ``A1:H1517`` is not walked down the column.
"""

from __future__ import annotations

from dataclasses import dataclass
import enum

from plugin.calc.address_utils import MAX_COL, MAX_ROW, column_to_index, index_to_column
from plugin.calc.array_formula import skip_double_quoted, skip_single_quoted
from plugin.calc.python.formula_edit import py_call_open_end


class _RefKind(enum.Enum):
    CELL = "cell"
    RANGE = "range"
    WHOLE_COL = "whole_col"
    WHOLE_ROW = "whole_row"


class _SpanKind(enum.Enum):
    SINGLE_CELL = "single_cell"
    SAME_ROW = "same_row"
    MULTI_ROW = "multi_row"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True)
class _Part:
    """One end of an A1 token. ``col``/``row`` are 0-based; None = omitted."""

    col: int | None
    row: int | None
    col_abs: bool
    row_abs: bool

    @property
    def is_cell(self) -> bool:
        return self.col is not None and self.row is not None

    @property
    def is_col_only(self) -> bool:
        return self.col is not None and self.row is None

    @property
    def is_row_only(self) -> bool:
        return self.col is None and self.row is not None


@dataclass(frozen=True)
class _Ref:
    """A sheet ref matched in formula text (optional sheet on each end)."""

    sheet1: str
    sheet2: str
    kind: _RefKind
    start: _Part
    end: _Part | None

    def row_span_kind(self) -> _SpanKind:
        """``SINGLE_CELL``, ``SAME_ROW``, ``MULTI_ROW``, or ``AMBIGUOUS``."""
        if self.kind == _RefKind.WHOLE_COL:
            return _SpanKind.MULTI_ROW
        left = self.start.row
        right = self.end.row if self.end is not None else left
        if left is None or right is None:
            return _SpanKind.AMBIGUOUS
        if left != right:
            return _SpanKind.MULTI_ROW
        if self.end is None or (self.start.col is not None and self.end.col is not None and self.start.col == self.end.col):
            return _SpanKind.SINGLE_CELL
        return _SpanKind.SAME_ROW


def expand_single_formula(formula: str, num_rows: int, num_cols: int) -> list[str]:
    """Expand one formula into a row-major 1-D or 2-D rectangle of formulas.

    Raises:
        ValueError: a dimension is less than 1.
    """
    if num_rows < 1 or num_cols < 1:
        raise ValueError("num_rows and num_cols must be >= 1")
    if num_rows > 1 and num_cols > 1:
        axis = "both" if _looks_like_python_formula(formula) else None
        return [
            adjust_a1_formula(formula, c, r, axis=axis)
            for r in range(num_rows)
            for c in range(num_cols)
        ]
    if num_rows > 1:
        return [adjust_a1_formula(formula, 0, r, axis="rows") for r in range(num_rows)]
    return [adjust_a1_formula(formula, c, 0, axis="cols") for c in range(num_cols)]


def _ref_crosses_axis(ref: _Ref, axis: str) -> bool:
    """True if ref's span crosses the fill axis."""
    if axis == "rows":
        if ref.kind == _RefKind.WHOLE_COL:
            return True
        if ref.end is not None and ref.start.row is not None and ref.end.row is not None and ref.start.row != ref.end.row:
            return True
        return False
    if axis == "cols":
        if ref.kind == _RefKind.WHOLE_ROW:
            return True
        if ref.end is not None and ref.start.col is not None and ref.end.col is not None and ref.start.col != ref.end.col:
            return True
        return False
    if axis == "both":
        if ref.kind != _RefKind.CELL:
            return True
        return False
    return False


def adjust_a1_formula(formula: str, dcol: int, drow: int, *, axis: str | None = None) -> str:
    """Shift relative A1 refs by *dcol*/*drow*. ``$`` stays put.

    Double-quoted strings (Calc ``""`` escape) are left untouched, so
    ``A1:H10`` inside ``=PY("…")`` is not a sheet ref. Common
    ``Sheet.A1`` / ``'Sheet'!A1`` prefixes are preserved.
    """
    if not formula or (dcol == 0 and drow == 0):
        return formula

    is_py = _looks_like_python_formula(formula)
    if is_py and axis is None:
        if dcol != 0 and drow == 0:
            axis = "cols"
        elif drow != 0 and dcol == 0:
            axis = "rows"
        elif dcol != 0 and drow != 0:
            axis = "both"

    out: list[str] = []
    i = 0
    n = len(formula)
    while i < n:
        if formula[i] == '"':
            j = skip_double_quoted(formula, i)
            out.append(formula[i:j])
            i = j
            continue
        matched = _try_match_ref(formula, i)
        if matched is not None:
            end, ref = matched
            if is_py and axis and _ref_crosses_axis(ref, axis):
                out.append(formula[i:end])
            else:
                out.append(_format_shifted_ref(ref, dcol, drow))
            i = end
            continue
        if formula[i] == "'":
            j = skip_single_quoted(formula, i)
            out.append(formula[i:j])
            i = j
            continue
        if formula[i] == "[":
            depth = 0
            j = i
            while j < n:
                if formula[j] == "[":
                    depth += 1
                elif formula[j] == "]":
                    depth -= 1
                    if depth == 0:
                        j += 1
                        break
                j += 1
            out.append(formula[i:j])
            i = j
            continue
        out.append(formula[i])
        i += 1
    return "".join(out)


def _classify_sheet_ref_span(token: str) -> _SpanKind:
    """Classify one DataRange-style token for the =PY span-peek."""
    s = (token or "").strip()
    if not s:
        return _SpanKind.AMBIGUOUS
    matched = _try_match_ref(s, 0)
    if matched is None:
        return _SpanKind.AMBIGUOUS
    end, ref = matched
    if end != len(s):
        return _SpanKind.AMBIGUOUS
    return ref.row_span_kind()


def _looks_like_python_formula(formula: str) -> bool:
    return py_call_open_end(formula, require_equals=True) is not None


def _is_letter(ch: str) -> bool:
    return "A" <= ch <= "Z" or "a" <= ch <= "z"


def _is_digit(ch: str) -> bool:
    return "0" <= ch <= "9"


def _is_ident_char(ch: str) -> bool:
    return _is_letter(ch) or _is_digit(ch) or ch == "_"


def _skip_ws(s: str, i: int) -> int:
    n = len(s)
    while i < n and s[i].isspace():
        i += 1
    return i


def _read_dollar(s: str, i: int) -> tuple[int, bool]:
    if i < len(s) and s[i] == "$":
        return i + 1, True
    return i, False


def _read_col(s: str, i: int) -> tuple[int, int] | None:
    start = i
    n = len(s)
    while i < n and _is_letter(s[i]):
        i += 1
    if i == start:
        return None
    letters = s[start:i]
    if len(letters) > 3:
        return None
    col_idx = column_to_index(letters)
    if col_idx > MAX_COL:
        return None
    return i, col_idx


def _read_row(s: str, i: int) -> tuple[int, int] | None:
    start = i
    n = len(s)
    while i < n and _is_digit(s[i]):
        i += 1
    if i == start:
        return None
    row_num = int(s[start:i])
    if row_num < 1:
        return None
    return i, row_num - 1


def _looks_like_a1_start(s: str, i: int) -> bool:
    if i >= len(s):
        return False
    ch = s[i]
    return ch == "$" or _is_letter(ch) or _is_digit(ch)


def _try_sheet_prefix(s: str, i: int) -> tuple[str, int] | None:
    """``Sheet.`` / ``'My Sheet'!`` including the separator, or None."""
    n = len(s)
    if i >= n:
        return None
    if s[i] == "'":
        j = i + 1
        while j < n and s[j] != "'":
            j += 1
        if j >= n:
            return None
        j += 1
        j = _skip_ws(s, j)
        if j >= n or s[j] not in ".!":
            return None
        j += 1
        j = _skip_ws(s, j)
        if not _looks_like_a1_start(s, j):
            return None
        return s[i:j], j

    j = i
    if s[j] == "$":
        j += 1
    start = j
    while j < n and s[j] not in ".!'" and not s[j].isspace() and s[j] not in "(),;+-*/&=<>^%:":
        j += 1
    if j == start:
        return None
    k = _skip_ws(s, j)
    if k >= n or s[k] not in ".!":
        return None
    k += 1
    k = _skip_ws(s, k)
    if not _looks_like_a1_start(s, k):
        return None
    return s[i:k], k


def _try_part(s: str, i: int) -> tuple[_Part, int] | None:
    """Cell, column-only, or row-only token starting at *i*."""
    j, col_abs = _read_dollar(s, i)
    col = _read_col(s, j)
    if col is not None:
        j, col_idx = col
        k, row_abs = _read_dollar(s, j)
        row = _read_row(s, k)
        if row is not None:
            return _Part(col_idx, row[1], col_abs, row_abs), row[0]
        return _Part(col_idx, None, col_abs, False), j
    j, row_abs = _read_dollar(s, i)
    row = _read_row(s, j)
    if row is not None:
        return _Part(None, row[1], False, row_abs), row[0]
    return None


def _next_is_open_paren(s: str, i: int) -> bool:
    i = _skip_ws(s, i)
    return i < len(s) and s[i] == "("


def _try_match_ref(s: str, i: int) -> tuple[int, _Ref] | None:
    """Match a cell / range / whole-col / whole-row (optional sheet) at *i*."""
    if i > 0 and _is_ident_char(s[i - 1]):
        return None
    sheet1 = ""
    j = i
    prefixed = _try_sheet_prefix(s, i)
    if prefixed is not None:
        sheet1, j = prefixed
    part1 = _try_part(s, j)
    if part1 is None:
        return None
    part, j = part1
    if j < len(s) and s[j] == ":":
        k = j + 1
        sheet2 = ""
        prefixed2 = _try_sheet_prefix(s, k)
        if prefixed2 is not None:
            sheet2, k = prefixed2
        part2 = _try_part(s, k)
        if part2 is not None:
            other, end = part2
            if _next_is_open_paren(s, end):
                return None
            if part.is_cell and other.is_cell:
                return end, _Ref(sheet1, sheet2, _RefKind.RANGE, part, other)
            if part.is_col_only and other.is_col_only:
                return end, _Ref(sheet1, sheet2, _RefKind.WHOLE_COL, part, other)
            if part.is_row_only and other.is_row_only:
                return end, _Ref(sheet1, sheet2, _RefKind.WHOLE_ROW, part, other)
    if part.is_cell:
        if _next_is_open_paren(s, j):
            return None
        return j, _Ref(sheet1, "", _RefKind.CELL, part, None)
    return None


def _shift_part(part: _Part, dcol: int, drow: int) -> _Part | None:
    col, row = part.col, part.row
    if col is not None and not part.col_abs:
        col += dcol
    if row is not None and not part.row_abs:
        row += drow
    if col is not None and not 0 <= col <= MAX_COL:
        return None
    if row is not None and not 0 <= row <= MAX_ROW:
        return None
    return _Part(col, row, part.col_abs, part.row_abs)


def _format_part(part: _Part) -> str:
    out = ""
    if part.col is not None:
        letters = index_to_column(part.col)
        out += ("$" if part.col_abs else "") + letters
    if part.row is not None:
        out += ("$" if part.row_abs else "") + str(part.row + 1)
    return out


def _format_shifted_ref(ref: _Ref, dcol: int, drow: int) -> str:
    left = _shift_part(ref.start, dcol, drow)
    left_str = _format_part(left) if left is not None else "#REF!"
    if ref.end is None:
        return f"{ref.sheet1}{left_str}"
    right = _shift_part(ref.end, dcol, drow)
    right_str = _format_part(right) if right is not None else "#REF!"
    return f"{ref.sheet1}{left_str}:{ref.sheet2}{right_str}"
