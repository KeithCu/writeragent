# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2024 John Balis
# Copyright (c) 2026 KeithCu (modifications and relicensing)
# Copyright (c) 2026 LibreCalc AI Assistant (Calc integration features, originally MIT)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
"""Cell address processing helper functions.

Pure utility functions with no UNO dependency. Ported from
core/calc_address_utils.py for the plugin framework.
"""

from __future__ import annotations

import re
from typing import Any

from plugin.framework.deal_shim import DEAL_MAX_CELL_REF, DEAL_MAX_COL_INDEX, DEAL_MAX_COL_LETTERS, DEAL_MAX_ROW_INDEX, ascii_bounded, deal, inverse_ensure

# Calc grid limits (0-based: col 0..16383 is A..XFD, row 0..1048575 is 1..1048576).
# These are physical Calc sheet limits, not the reduced bounds in deal_shim.
MAX_COL = 16383
MAX_ROW = 1_048_575

# Scans cell tokens in formulas (optional $). The boundaries skip function names
# like LOG10(, named ranges like SALES2024, and the Sheet1 of Sheet1.A1. Not a validator.
A1_CELL_RE = re.compile(
    r"(?<![A-Z0-9_$])\$?([A-Z]{1,3})\$?([1-9][0-9]{0,6})(?![A-Z0-9_(.!])",
    re.IGNORECASE,
)

_A1_RE = re.compile(r"^\$?([A-Z]+)\$?([0-9]+)$")


# Pre must cap the int: `index >= 0` with no max lets CrossHair feed a giant
# value into `while index > 0: divmod(..., 26)`, and deep check never returns.
@deal.pre(lambda index: isinstance(index, int) and 0 <= index <= DEAL_MAX_COL_INDEX)
@deal.post(lambda result: isinstance(result, str) and 1 <= len(result) <= DEAL_MAX_COL_LETTERS)
def index_to_column(index: int) -> str:
    """Convert 0-based column index to column letter.

    Args:
        index: 0-based column index.

    Returns:
        Column letter (e.g. "A", "AB").
    """
    # crosshair: off
    result = []
    index += 1
    while index > 0:
        index, remainder = divmod(index - 1, 26)
        result.append(chr(ord("A") + remainder))
    return "".join(reversed(result))


# Cap letters so CrossHair cannot chase an unbounded inverse. Nested
# index_to_column is skipped under CrossHair (import-time inverse_ensure
# no-op); cheap @deal.post still runs so the function is analyzed.
@deal.pre(lambda col_str: ascii_bounded(col_str, DEAL_MAX_COL_LETTERS, min_len=1) and col_str.isalpha())
@deal.post(lambda result: isinstance(result, int) and result >= 0)
@inverse_ensure(lambda col_str, result: index_to_column(result) == col_str.upper())
def column_to_index(col_str: str) -> int:
    """Convert column letter to 0-based index.

    Args:
        col_str: Column letter (e.g. "A", "AB"); at most 3 letters (Excel/Calc max).

    Returns:
        0-based column index.
    """
    result = 0
    for char in col_str.upper():
        result = result * 26 + (ord(char) - ord("A") + 1)
    return result - 1


# A reference may name its sheet: Sheet1.A1, 'Sheet One'.A1:C5, Sheet1!A1.
# LibreOffice writes the dot form, Excel the bang; both are accepted, and a
# quoted name may contain spaces and dots. An apostrophe inside the name is
# doubled ('O''Brien'.A1), the same escape Calc and Excel use.
_SHEET_PREFIX = re.compile(
    r"""^\s*
        (?:\$?'(?P<quoted>(?:[^']|'')+)'  # 'Sheet One', $'Sheet One'
          |(?P<bare>[^.!'\s][^.!']*?))  # Sheet1
        \s*[.!]\s*
        (?P<rest>.+)$""",
    re.VERBOSE,
)


def split_sheet_prefix(ref: str) -> tuple[str | None, str]:
    """Split a reference into ``(sheet_name, address)``.

    Returns ``(None, ref)`` when there is no prefix. The sheet name keeps
    its original case — only the address part is normalised later, so a
    sheet called ``Summary`` is not reported back as ``SUMMARY``.

    >>> split_sheet_prefix("Sheet1.A1:C5")
    ('Sheet1', 'A1:C5')
    >>> split_sheet_prefix("'Data Sheet'!B2")
    ('Data Sheet', 'B2')
    >>> split_sheet_prefix("'O''Brien'.A1")
    ("O'Brien", 'A1')
    >>> split_sheet_prefix("A1:C5")
    (None, 'A1:C5')
    """
    # crosshair: off
    if not ref:
        return None, ref
    match = _SHEET_PREFIX.match(ref)
    if not match:
        return None, ref.strip()
    # A quoted sheet name can hold a doubled apostrophe. '' is one
    # apostrophe in Calc/Excel, so unescape after the match. Otherwise
    # 'O''Brien'.C5:D6 does not match and the sheet is dropped.
    quoted = match.group("quoted")
    if quoted is not None:
        name = quoted.replace("''", "'")
    else:
        name = match.group("bare") or ""
        # Bugfix: absolute sheet references like $Sheet1.$A$1 failed parsing.
        # Calc writes an absolute sheet as $Sheet1.$A$1; the $ is a marker,
        # not part of the name. A quoted name that really starts with $ is left alone.
        if name.startswith("$"):
            name = name[1:]
    return name.strip(), match.group("rest").strip()


def quote_sheet_name(name: str) -> str:
    """Bare unless Calc needs quotes (spaces, dots, apostrophes, bangs, or a leading $)."""
    # A leading $ is Calc's absolute-sheet marker, so a literal name must be quoted.
    if name.startswith("$") or any(ch in name for ch in " .!'"):
        return "'" + name.replace("'", "''") + "'"
    return name


def qualify_a1(sheet_name: str, a1: str) -> str:
    """Qualify an A1 address or range with a properly quoted sheet name."""
    return f"{quote_sheet_name(sheet_name)}.{a1}"


def _parse_a1(text: str, label: str) -> tuple[int, int]:
    cleaned = text.strip().upper()
    match = _A1_RE.match(cleaned)
    if not match:
        raise ValueError(f"Invalid {label}: '{cleaned}'")
    row_num = int(match.group(2))
    if row_num < 1:
        raise ValueError(f"Invalid row number in {label}: {row_num}")
    if row_num - 1 > MAX_ROW:
        raise ValueError(f"Row {row_num} in {label} is past Calc's last row ({MAX_ROW + 1})")
    return column_to_index(match.group(1)), row_num - 1


def split_sheet_and_name(ref: str, sheet_name: str | None) -> tuple[str, str | None]:
    """Return ``(address, sheet)``. A prefix on *ref* wins over *sheet_name*."""
    prefix, address = split_sheet_prefix(ref)
    if prefix is not None and sheet_name and prefix.casefold() != sheet_name.casefold():
        raise ValueError(
            "Reference names sheet '%s' but sheet_name says '%s' — pass one or the other."
            % (prefix, sheet_name)
        )
    return address, (prefix or sheet_name)


def _reject_sheet_prefix(ref: str, kind: str) -> str:
    # crosshair: off — _SHEET_PREFIX hangs under deep check (same as the callers).
    # Reject prefixes here so callers that need the sheet go through
    # split_sheet_prefix / CalcBridge.resolve instead of silently dropping it.
    sheet, rest = split_sheet_prefix(ref)
    if sheet is not None:
        raise ValueError(f"{kind} '{sheet}.{rest}' names a sheet, but this operation resolves the sheet separately. Pass the sheet via sheet_name, or drop the prefix.")
    return rest


@deal.pre(lambda address: ascii_bounded(address, DEAL_MAX_CELL_REF, min_len=1))
@deal.post(lambda result: isinstance(result, tuple) and len(result) == 2 and result[0] >= 0 and result[1] >= 0)
@deal.raises(ValueError)
def parse_address(address: str) -> tuple[int, int]:
    """Convert cell address to column and row indices.

    Args:
        address: Cell address (e.g. "A1", "AB10"), without a sheet prefix —
            use :func:`split_sheet_prefix` first when the reference may name one.

    Returns:
        (column_index, row_index) tuple (0-based).

    Raises:
        ValueError: Invalid cell address, or a sheet prefix was left on.
    """
    # Sheet-prefix + A1 regex hang under deep check.
    # crosshair: off
    return _parse_a1(_reject_sheet_prefix(address, "Cell address"), "cell address")


@deal.pre(lambda range_str: ascii_bounded(range_str, DEAL_MAX_CELL_REF, min_len=1))
@deal.post(lambda result: isinstance(result, tuple) and len(result) == 2 and all(isinstance(p, tuple) and len(p) == 2 and p[0] >= 0 and p[1] >= 0 for p in result))
@deal.raises(ValueError)
def parse_range_string(range_str: str) -> tuple[tuple[int, int], tuple[int, int]]:
    """Convert cell range string to column/row indices.

    Args:
        range_str: Range string in "A1:D10" or "A1" format, without a
            sheet prefix — use :func:`split_sheet_prefix` first when the
            reference may name one.

    Returns:
        ((start_col, start_row), (end_col, end_row)) tuple.
        Both tuples are the same for a single cell.

    Raises:
        ValueError: Invalid range format, or a sheet prefix was left on.
    """
    # Sheet-prefix + A1 regex hang under deep check.
    # crosshair: off
    range_str = _reject_sheet_prefix(range_str, "Range")
    cleaned = range_str.strip().upper()
    start_str, has_sep, end_str = cleaned.partition(":")
    if not has_sep:
        pt = _parse_a1(start_str, "cell address")
        return pt, pt

    start_pt = _parse_a1(start_str, "start cell address")
    end_pt = _parse_a1(end_str, "end cell address")
    if start_pt[0] > end_pt[0] or start_pt[1] > end_pt[1]:
        raise ValueError(f"Invalid cell range: start cell must be top-left and end cell bottom-right: '{range_str}'")
    return start_pt, end_pt


# Nested parse_address is skipped under CrossHair (import-time inverse_ensure
# no-op); cheap @deal.post still runs so the function is analyzed.
@deal.pre(lambda col, row: isinstance(col, int) and 0 <= col <= DEAL_MAX_COL_INDEX and isinstance(row, int) and 0 <= row <= DEAL_MAX_ROW_INDEX)
@deal.post(lambda result: isinstance(result, str) and bool(result) and result[0].isalpha() and result.isascii() and result[-1].isdigit())
@inverse_ensure(lambda col, row, result: parse_address(result) == (col, row))
def format_address(col: int, row: int) -> str:
    """Create cell address from column and row indices.

    Args:
        col: 0-based column index.
        row: 0-based row index.

    Returns:
        Cell address (e.g. "A1", "AB10").
    """
    return f"{index_to_column(col)}{row + 1}"


def format_range(start_col: int, start_row: int, end_col: int, end_row: int, *, collapse_single: bool = False) -> str:
    """Create cell range string from column and row indices.

    Args:
        start_col: 0-based start column index.
        start_row: 0-based start row index.
        end_col: 0-based end column index.
        end_row: 0-based end row index.
        collapse_single: When True and start == end, return "A1" instead of "A1:A1".

    Returns:
        Range string (e.g. "A1:B3" or "A1:A1" / "A1").
    """
    start = format_address(start_col, start_row)
    end = format_address(end_col, end_row)
    if collapse_single and start == end:
        return start
    return f"{start}:{end}"


def format_range_address(addr: Any, *, collapse_single: bool = False) -> str:
    """Format a UNO CellRangeAddress into an A1 range string."""
    return format_range(
        addr.StartColumn,
        addr.StartRow,
        addr.EndColumn,
        addr.EndRow,
        collapse_single=collapse_single,
    )

