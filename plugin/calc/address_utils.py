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

from plugin.framework.deal_shim import (
    DEAL_MAX_CELL_REF,
    DEAL_MAX_COL_INDEX,
    DEAL_MAX_COL_LETTERS,
    DEAL_MAX_ROW_INDEX,
    ascii_bounded,
    deal,
)


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
    result = []
    index += 1
    while index > 0:
        index, remainder = divmod(index - 1, 26)
        result.append(chr(ord("A") + remainder))
    return "".join(reversed(result))


# Cap length at Excel/Calc max column width (XFD); unbounded alpha strings make CrossHair
# chase the inverse ensure forever under deep check.
@deal.pre(
    lambda col_str: ascii_bounded(col_str, DEAL_MAX_COL_LETTERS, min_len=1)
    and col_str.isalpha()
)
@deal.post(lambda result: isinstance(result, int) and result >= 0)
@deal.ensure(lambda col_str, result: index_to_column(result) == col_str.upper())
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
# quoted name may contain spaces and dots.
_SHEET_PREFIX = re.compile(
    r"""^\s*
        (?:'(?P<quoted>[^']+)'      # 'Sheet One'
          |(?P<bare>[^.!'\s][^.!']*?))   # Sheet1
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
    >>> split_sheet_prefix("A1:C5")
    (None, 'A1:C5')
    """
    if not ref:
        return None, ref
    match = _SHEET_PREFIX.match(ref)
    if not match:
        return None, ref.strip()
    name = match.group("quoted") or match.group("bare")
    return name.strip(), match.group("rest").strip()


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
    # Reject prefixes here so callers that need the sheet go through
    # split_sheet_prefix / CalcBridge.resolve instead of silently dropping it.
    sheet, address = split_sheet_prefix(address)
    if sheet is not None:
        raise ValueError(
            f"Cell address '{sheet}.{address}' names a sheet, but this "
            f"operation resolves the sheet separately. Pass the sheet via "
            f"sheet_name, or drop the prefix."
        )
    address = address.strip().upper()
    match = re.match(r"^([A-Z]+)([0-9]+)$", address)
    if not match:
        raise ValueError(f"Invalid cell address: '{address}'")

    col_str = match.group(1)
    row_num = int(match.group(2))
    if row_num < 1:
        raise ValueError(f"Invalid row number in cell address: {row_num}")

    col_index = column_to_index(col_str)
    row_index = row_num - 1

    return col_index, row_index


@deal.pre(lambda range_str: ascii_bounded(range_str, DEAL_MAX_CELL_REF, min_len=1))
@deal.post(
    lambda result: isinstance(result, tuple)
    and len(result) == 2
    and all(isinstance(p, tuple) and len(p) == 2 and p[0] >= 0 and p[1] >= 0 for p in result)
)
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
    sheet, range_str = split_sheet_prefix(range_str)
    if sheet is not None:
        raise ValueError(
            f"Range '{sheet}.{range_str}' names a sheet, but this operation "
            f"resolves the sheet separately. Pass the sheet via sheet_name, "
            f"or drop the prefix."
        )
    range_str = range_str.strip().upper()

    pattern = r"^([A-Z]+)([0-9]+)(?::([A-Z]+)([0-9]+))?$"
    match = re.match(pattern, range_str)
    if not match:
        raise ValueError(f"Invalid cell range format: '{range_str}'")

    start_col = column_to_index(match.group(1))
    start_row_num = int(match.group(2))
    if start_row_num < 1:
        raise ValueError(f"Invalid row number in start cell address: {start_row_num}")
    start_row = start_row_num - 1

    if match.group(3) is not None:
        end_col = column_to_index(match.group(3))
        end_row_num = int(match.group(4))
        if end_row_num < 1:
            raise ValueError(f"Invalid row number in end cell address: {end_row_num}")
        end_row = end_row_num - 1
    else:
        end_col = start_col
        end_row = start_row

    return (start_col, start_row), (end_col, end_row)


@deal.pre(lambda col, row: isinstance(col, int) and 0 <= col <= DEAL_MAX_COL_INDEX and isinstance(row, int) and 0 <= row <= DEAL_MAX_ROW_INDEX)
@deal.post(lambda result: isinstance(result, str) and bool(re.match(r"^[A-Z]+\d+$", result)))
@deal.ensure(lambda col, row, result: parse_address(result) == (col, row))
def format_address(col: int, row: int) -> str:
    """Create cell address from column and row indices.

    Args:
        col: 0-based column index.
        row: 0-based row index.

    Returns:
        Cell address (e.g. "A1", "AB10").
    """
    return f"{index_to_column(col)}{row + 1}"
