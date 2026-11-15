# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Fill-down / fill-across, Excel sheet-ref dotting, and repeated-formula detection.

``write_formula_range`` fills one formula across a range instead of repeating
the same text in every cell. Ordinary formulas shift relative A1 refs per cell
(``$`` stays absolute). ``=PY`` / PYTHON / PYTHONFUNCTION formulas use a
direction-aware span policy: only refs whose span does not cross the fill
axis are adjusted; other refs outside the quoted Python string stay
verbatim so a block like ``A1:H1517`` is not walked down the column.

``dot_sheet_refs`` rewrites Excel-style ``Sheet!A1`` prefixes to Calc's
``Sheet.A1`` dot form, and ``repeated_formula_columns`` detects list-write
columns that repeat a single row-aligned formula so callers can warn.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

from plugin.calc.address_utils import (
    MAX_COL,
    MAX_ROW,
    column_to_index,
    index_to_column,
    quote_sheet_name,
)
from plugin.calc.array_formula import skip_quoted
from plugin.calc.python.formula_edit import PyFormula


@dataclass(frozen=True)
class _Part:
    """One end of an A1 token. ``col``/``row`` are 0-based; None = omitted."""

    col: int | None
    row: int | None
    col_abs: bool
    row_abs: bool


@dataclass(frozen=True)
class _Ref:
    """A sheet ref matched in formula text (optional sheet on each end)."""

    sheet1: str
    sheet2: str
    start: _Part
    end: _Part | None


def expand_single_formula(formula: str, num_rows: int, num_cols: int) -> list[str]:
    """Expand one formula into a row-major 1-D or 2-D rectangle of formulas.

    Raises:
        ValueError: a dimension is less than 1.
    """
    if num_rows < 1 or num_cols < 1:
        raise ValueError("num_rows and num_cols must be >= 1")
    # "both" on every 2-D fill: a Python cell at (c>0, r=0) would infer "cols".
    # Non-Python formulas ignore axis; 1-D offsets infer rows vs cols.
    axis = "both" if num_rows > 1 and num_cols > 1 else None
    return [
        adjust_a1_formula(formula, c, r, axis=axis)
        for r in range(num_rows)
        for c in range(num_cols)
    ]


def _ref_crosses_axis(ref: _Ref, axis: str | None) -> bool:
    """True when *ref* spans the fill axis and must stay verbatim.

    ``_try_match_ref`` only builds a lone cell (no end), a cell range, a
    whole column (row omitted), or a whole row (column omitted).
    ``None != None`` would miss a whole column on a vertical fill.
    """
    if axis == "rows":
        return ref.end is not None and (ref.start.row is None or ref.start.row != ref.end.row)
    if axis == "cols":
        return ref.end is not None and (ref.start.col is None or ref.start.col != ref.end.col)
    if axis == "both":
        return ref.end is not None
    return False


def _map_refs(formula: str, on_ref: Callable[[str, _Ref], str]) -> str:
    """Walk *formula*, skipping strings, quoted names, and structured refs, and map each ref."""
    out: list[str] = []
    i = 0
    n = len(formula)
    while i < n:
        if formula[i] == '"':
            j = skip_quoted(formula, i)
            out.append(formula[i:j])
            i = j
            continue
        matched = _try_match_ref(formula, i)
        if matched is not None:
            end, ref = matched
            out.append(on_ref(formula[i:end], ref))
            i = end
            continue
        if formula[i] == "'":
            j = skip_quoted(formula, i)
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


def adjust_a1_formula(formula: str, dcol: int, drow: int, *, axis: str | None = None) -> str:
    """Shift relative A1 refs by *dcol*/*drow*. ``$`` stays put.

    Double-quoted strings (Calc ``""`` escape) are left untouched, so
    ``A1:H10`` inside ``=PY("…")`` is not a sheet ref. Common
    ``Sheet.A1`` / ``'Sheet'!A1`` prefixes are preserved.
    """
    if not formula or (dcol == 0 and drow == 0):
        return formula

    is_py = PyFormula.open_end(formula, require_equals=True) is not None
    if is_py and axis is None:
        axis = "both" if dcol and drow else "cols" if dcol else "rows"

    def _shift(text: str, ref: _Ref) -> str:
        if is_py and _ref_crosses_axis(ref, axis):
            return text
        return _format_shifted_ref(ref, dcol, drow)

    return _map_refs(formula, _shift)


def _dot_prefix(prefix: str, sheet_for: Callable[[str], str | None]) -> str:
    stripped = prefix.strip()
    if not stripped.endswith("!"):
        return prefix
    body = stripped[:-1].strip()
    absolute = body.startswith("$")
    if absolute:
        body = body[1:].strip()
    if len(body) >= 2 and body.startswith("'") and body.endswith("'"):
        name = body[1:-1].replace("''", "'")
    else:
        name = body
    real = sheet_for(name)
    if real is None:
        return prefix
    return ("$" if absolute else "") + quote_sheet_name(real) + "."


def dot_sheet_refs(formula: str, sheet_for: Callable[[str], str | None]) -> str:
    """Rewrite Excel ``Sheet!A1`` prefixes to Calc's ``Sheet.A1`` dot form.

    In Calc's API grammar (``formula/inc/core_resource.hrc``,
    ``RID_STRLIST_FUNCTION_NAMES_ENGLISH_API``), ``!`` is ``SC_OPCODE_INTERSECT``,
    so an unrewritten ``Orders!A1`` fails with ``#NAME?`` unless ``Orders`` is a
    named range. Only prefixes where ``sheet_for(name)`` returns a sheet name
    are rewritten.
    """
    if "!" not in formula:
        return formula

    def _replace(text: str, ref: _Ref) -> str:
        s1 = _dot_prefix(ref.sheet1, sheet_for) if ref.sheet1 else ""
        s2 = _dot_prefix(ref.sheet2, sheet_for) if ref.sheet2 else ""
        if s1 == ref.sheet1 and s2 == ref.sheet2:
            return text
        return _format_shifted_ref(replace(ref, sheet1=s1, sheet2=s2), 0, 0)

    return _map_refs(formula, _replace)


def _relative_rows(formula: str) -> set[int]:
    """0-based rows of every relative-row ref end in *formula*."""
    rows: set[int] = set()

    def _collect(text: str, ref: _Ref) -> str:
        if ref.start.row is not None and not ref.start.row_abs:
            rows.add(ref.start.row)
        if ref.end is not None and ref.end.row is not None and not ref.end.row_abs:
            rows.add(ref.end.row)
        return text

    _map_refs(formula, _collect)
    return rows


def repeated_formula_columns(
    values: Sequence[Any], first_row: int, num_rows: int, num_cols: int
) -> list[tuple[int, str]]:
    """Find columns repeating one row-aligned formula across every row.

    A model that pastes the same row-2 formula in every row likely meant
    fill-down, but rewriting silently can corrupt intentional pinning, so
    callers warn instead. Returns ``[(0-based col offset, formula), ...]``.
    """
    if num_rows < 2 or num_cols < 1 or len(values) != num_rows * num_cols:
        return []
    repeated: list[tuple[int, str]] = []
    target_rows = {first_row}
    for c in range(num_cols):
        col = values[c::num_cols]
        head = col[0]
        if not isinstance(head, str) or not head.startswith("="):
            continue
        if any(v != head for v in col[1:]):
            continue
        if _relative_rows(head) != target_rows:
            continue
        repeated.append((c, head))
    return repeated


def _is_letter(ch: str) -> bool:
    return "A" <= ch <= "Z" or "a" <= ch <= "z"


def _is_digit(ch: str) -> bool:
    return "0" <= ch <= "9"


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


def _try_sheet_prefix(s: str, i: int) -> tuple[str, int] | None:
    """``Sheet.`` / ``'My Sheet'!`` including the separator, or None."""
    n = len(s)
    if i >= n:
        return None
    if s[i] == "'":
        j = skip_quoted(s, i)
        if s[j - 1] != "'":
            return None
    else:
        j = i
        if s[j] == "$":
            j += 1
        start = j
        while j < n and s[j] not in ".!'" and not s[j].isspace() and s[j] not in "(),;+-*/&=<>^%:":
            j += 1
        if j == start:
            return None
    j = _skip_ws(s, j)
    if j >= n or s[j] not in ".!":
        return None
    j += 1
    j = _skip_ws(s, j)
    if j >= n or not (s[j] == "$" or _is_letter(s[j]) or _is_digit(s[j])):
        return None
    return s[i:j], j


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
    row = _read_row(s, j)
    if row is not None:
        return _Part(None, row[1], False, col_abs), row[0]
    return None


def _next_is_open_paren(s: str, i: int) -> bool:
    i = _skip_ws(s, i)
    return i < len(s) and s[i] == "("


def _try_match_ref(s: str, i: int) -> tuple[int, _Ref] | None:
    """Match a cell / range / whole-col / whole-row (optional sheet) at *i*."""
    if i > 0 and (_is_letter(s[i - 1]) or _is_digit(s[i - 1]) or s[i - 1] == "_"):
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
            if (part.col is None, part.row is None) == (other.col is None, other.row is None):
                return end, _Ref(sheet1, sheet2, part, other)
    if part.col is not None and part.row is not None:
        if _next_is_open_paren(s, j):
            return None
        return j, _Ref(sheet1, "", part, None)
    return None


def _shift_part_str(part: _Part, dcol: int, drow: int) -> str:
    col, row = part.col, part.row
    if col is not None and not part.col_abs:
        col += dcol
    if row is not None and not part.row_abs:
        row += drow
    if (col is not None and not 0 <= col <= MAX_COL) or (row is not None and not 0 <= row <= MAX_ROW):
        return "#REF!"
    out = ""
    if col is not None:
        out += ("$" if part.col_abs else "") + index_to_column(col)
    if row is not None:
        out += ("$" if part.row_abs else "") + str(row + 1)
    return out


def _format_shifted_ref(ref: _Ref, dcol: int, drow: int) -> str:
    left_str = _shift_part_str(ref.start, dcol, drow)
    if ref.end is None:
        return f"{ref.sheet1}{left_str}"
    right_str = _shift_part_str(ref.end, dcol, drow)
    return f"{ref.sheet1}{left_str}:{ref.sheet2}{right_str}"
