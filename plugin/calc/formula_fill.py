# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Fill-down / fill-across for a single Calc formula into a 1-D range.

``write_formula_range`` used to pin the same formula text on every cell
(``[formula] * n``). Ordinary formulas now shift relative A1 refs per cell
(``$`` stays absolute). ``=PY`` / PYTHON / PYTHONFUNCTION use a span-peek:
only same-row or single-cell sheet refs outside the quoted Python string
are adjusted; a multi-row DataRange (or anything unparseable) stays
verbatim so a block like ``A1:H1517`` is not walked down the column.

Python A1-adjust is intentional for v1 (unit-testable, no soffice). LO
fill/series APIs can be revisited if they prove cheaper and reliable
across builds. 2-D destination + one formula is refused — no
fill-down-and-right.
"""

from __future__ import annotations

from dataclasses import dataclass

from plugin.calc.address_utils import column_to_index, index_to_column
from plugin.calc.python.formula_edit import _py_call_open_end, format_data_binding_display, parse_data_binding_text, parse_python_formula, py_formula_has_unquoted_code_ref
from plugin.framework.errors import CalcError


# Calc / Excel current grid. Out-of-bounds relative shift → #REF!.
_MAX_COL = 16383  # XFD
_MAX_ROW = 1_048_575  # 0-based last row of 1048576

_TWOD_FORMULA_MSG = "A single formula cannot fill a 2-D range ({rows}×{cols}). Use a 1-column or 1-row range for fill-down/across (relative A1 refs adjust per cell), or pass an explicit array with one value per cell."


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
    kind: str  # cell | range | whole_col | whole_row
    start: _Part
    end: _Part | None

    def row_span_kind(self) -> str:
        """``single_cell``, ``same_row``, ``multi_row``, or ``ambiguous``."""
        if self.kind == "whole_col":
            return "multi_row"
        left = self.start.row
        right = self.end.row if self.end is not None else left
        if left is None or right is None:
            return "ambiguous"
        if left != right:
            return "multi_row"
        if self.end is None or (self.start.col is not None and self.end.col is not None and self.start.col == self.end.col):
            return "single_cell"
        return "same_row"


def expand_single_formula(formula: str, num_rows: int, num_cols: int) -> list[str]:
    """Expand one formula into a row-major 1-D rectangle of formulas.

    Raises:
        CalcError: *num_rows* and *num_cols* are both greater than 1.
        ValueError: a dimension is less than 1.
    """
    if num_rows < 1 or num_cols < 1:
        raise ValueError("num_rows and num_cols must be >= 1")
    if num_rows > 1 and num_cols > 1:
        raise CalcError(_TWOD_FORMULA_MSG.format(rows=num_rows, cols=num_cols))
    adjust = should_adjust_formula_fill(formula)
    out: list[str] = []
    for row in range(num_rows):
        for col in range(num_cols):
            if adjust and (col or row):
                out.append(adjust_a1_formula(formula, col, row))
            else:
                out.append(formula)
    return out


def should_adjust_formula_fill(formula: str) -> bool:
    """True when a 1-D fill should shift relative A1 refs.

    Ordinary Calc formulas always adjust. ``=PY`` / PYTHON / PYTHONFUNCTION
    fail closed: unparseable or any multi-row sheet ref outside the quoted
    Python string → no adjust (verbatim pin).
    """
    if _looks_like_python_formula(formula):
        return _python_span_allows_adjust(formula)
    return True


def adjust_a1_formula(formula: str, dcol: int, drow: int) -> str:
    """Shift relative A1 refs by *dcol*/*drow*. ``$`` stays put.

    Double-quoted strings (Calc ``""`` escape) are left untouched, so
    ``A1:H10`` inside ``=PY("…")`` is not a sheet ref. Common
    ``Sheet.A1`` / ``'Sheet'!A1`` prefixes are preserved.
    """
    # crosshair: off
    # Char-scan + sheet/A1 combinatorics; same class as formula_edit scanners.
    if not formula or (dcol == 0 and drow == 0):
        return formula
    out: list[str] = []
    i = 0
    n = len(formula)
    while i < n:
        if formula[i] == '"':
            j = _skip_double_quoted(formula, i)
            out.append(formula[i:j])
            i = j
            continue
        matched = _try_match_ref(formula, i)
        if matched is not None:
            end, ref = matched
            out.append(_format_shifted_ref(ref, dcol, drow))
            i = end
            continue
        out.append(formula[i])
        i += 1
    return "".join(out)


def classify_sheet_ref_span(token: str) -> str:
    """Classify one DataRange-style token for the =PY span-peek."""
    s = (token or "").strip()
    if not s:
        return "ambiguous"
    matched = _try_match_ref(s, 0)
    if matched is None:
        return "ambiguous"
    end, ref = matched
    if end != len(s):
        return "ambiguous"
    return ref.row_span_kind()


def _looks_like_python_formula(formula: str) -> bool:
    return _py_call_open_end(formula, require_equals=True) is not None


def _python_span_allows_adjust(formula: str) -> bool:
    """Peek sheet refs outside the quoted Python string (esp. DataRange)."""
    parts = parse_python_formula(formula)
    if parts is None:
        return False
    tokens = parse_data_binding_text(format_data_binding_display(parts.data_suffix))
    if py_formula_has_unquoted_code_ref(formula):
        tokens = [parts.code, *tokens]
    for tok in tokens:
        kind = classify_sheet_ref_span(tok)
        if kind in ("multi_row", "ambiguous"):
            return False
    return True


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
    try:
        return i, column_to_index(letters)
    except Exception:
        return None


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
                return end, _Ref(sheet1, sheet2, "range", part, other)
            if part.is_col_only and other.is_col_only:
                return end, _Ref(sheet1, sheet2, "whole_col", part, other)
            if part.is_row_only and other.is_row_only:
                return end, _Ref(sheet1, sheet2, "whole_row", part, other)
    if part.is_cell:
        if _next_is_open_paren(s, j):
            return None
        return j, _Ref(sheet1, "", "cell", part, None)
    return None


def _shift_part(part: _Part, dcol: int, drow: int) -> _Part | None:
    col, row = part.col, part.row
    if col is not None and not part.col_abs:
        col += dcol
    if row is not None and not part.row_abs:
        row += drow
    if col is not None and not 0 <= col <= _MAX_COL:
        return None
    if row is not None and not 0 <= row <= _MAX_ROW:
        return None
    return _Part(col, row, part.col_abs, part.row_abs)


def _format_part(part: _Part) -> str:
    out = ""
    if part.col is not None:
        try:
            letters = index_to_column(part.col)
        except Exception:
            return ""
        out += ("$" if part.col_abs else "") + letters
    if part.row is not None:
        out += ("$" if part.row_abs else "") + str(part.row + 1)
    return out


def _format_shifted_ref(ref: _Ref, dcol: int, drow: int) -> str:
    left = _shift_part(ref.start, dcol, drow)
    right = _shift_part(ref.end, dcol, drow) if ref.end is not None else None
    if left is None or (ref.end is not None and right is None):
        return "#REF!"
    left_txt = _format_part(left)
    if not left_txt:
        return "#REF!"
    if ref.end is None or right is None:
        return f"{ref.sheet1}{left_txt}"
    right_txt = _format_part(right)
    if not right_txt:
        return "#REF!"
    return f"{ref.sheet1}{left_txt}:{ref.sheet2}{right_txt}"
