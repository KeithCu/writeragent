# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Column vectorization logic for Calc Spreadsheet → Python Import."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from plugin.calc.address_utils import column_to_index, parse_address

if TYPE_CHECKING:
    from plugin.calc.spreadsheet_import.models import SheetModel

_A1_REF_RE = re.compile(r"(?<![A-Z0-9_])(\$?)([A-Z]+)(\$?)([0-9]+)(?!\()", re.IGNORECASE)


def to_r1c1(formula: str, cell_addr: str) -> str:
    """Convert a formula string from A1 notation to relative R1C1 notation."""
    try:
        cell_col, cell_row = parse_address(cell_addr)
    except ValueError:
        return formula

    def replace_ref(match: re.Match[str]) -> str:
        col_abs = bool(match.group(1))
        col_str = match.group(2).upper()
        row_abs = bool(match.group(3))
        row_str = match.group(4)
        target_col = column_to_index(col_str)
        target_row = int(row_str) - 1

        # Row relative/absolute representation
        if row_abs:
            r_part = f"R{target_row + 1}"
        else:
            offset = target_row - cell_row
            r_part = f"R[{offset}]"

        # Col relative/absolute representation
        if col_abs:
            c_part = f"C{target_col + 1}"
        else:
            offset = target_col - cell_col
            c_part = f"C[{offset}]"

        return f"{r_part}{c_part}"

    return _A1_REF_RE.sub(replace_ref, formula)


def translation_has_cross_sheet_ranges(ranges: list[str]) -> bool:
    """True when any range refers to another sheet (not a same-sheet A1 range)."""
    for range_addr in ranges:
        addr = str(range_addr).strip()
        if "!" in addr:
            return True
        if "." not in addr:
            continue
        sheet, _unused, rest = addr.partition(".")
        if sheet and rest and not re.match(r"^\$?[A-Z]+\$?\d", sheet, re.IGNORECASE):
            return True
    return False


def detect_vectorized_columns(model: SheetModel) -> dict[str, list[str]]:
    """Detect homogeneous relative formulas down columns.

    Returns a dict mapping the first cell address of a vectorized sequence
    to a list of cell addresses in that sequence (including the first one).
    """
    col_cells: dict[int, list[tuple[int, str, str]]] = {}
    for addr, record in model.cells.items():
        if record.type in ("formula", "error") and record.formula:
            col, row = parse_address(addr)
            col_cells.setdefault(col, []).append((row, addr, record.formula))

    vector_groups: dict[str, list[str]] = {}

    for col, cells in col_cells.items():
        sorted_cells = sorted(cells, key=lambda item: item[0])

        runs: list[list[str]] = []
        current_r1c1 = ""
        last_row = -1
        for row, addr, formula in sorted_cells:
            r1c1 = to_r1c1(formula, addr)
            if runs and r1c1 == current_r1c1 and row == last_row + 1:
                runs[-1].append(addr)
            else:
                runs.append([addr])
                current_r1c1 = r1c1
            last_row = row
        for run in runs:
            if len(run) >= 2:
                vector_groups[run[0]] = run

    return vector_groups


def _shift_a1_rows(addr: str, row_delta: int) -> str:
    def repl(match: re.Match[str]) -> str:
        col_prefix, col, row_prefix, row_s = match.groups()
        if row_prefix:
            return f"{col_prefix}{col.upper()}{row_prefix}{row_s}"
        row = int(row_s) + row_delta
        if row < 1:
            return match.group(0)
        return f"{col_prefix}{col.upper()}{row}"
    return _A1_REF_RE.sub(repl, addr)


def vectorize_range(a1_range: str, row_delta: int) -> str:
    """Expand an A1 ref or range down by *row_delta* rows."""
    if ":" in a1_range:
        start, end = a1_range.split(":", 1)
        return f"{_shift_a1_rows(start, 0)}:{_shift_a1_rows(end, row_delta)}"
    start = _shift_a1_rows(a1_range, 0)
    end = _shift_a1_rows(a1_range, row_delta)
    if start == end:
        return start
    return f"{start}:{end}"
