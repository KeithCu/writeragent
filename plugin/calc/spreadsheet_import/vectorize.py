# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Column vectorization logic for Calc Spreadsheet → Python Import."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from plugin.calc.address_utils import column_to_index, index_to_column, parse_address

if TYPE_CHECKING:
    from plugin.calc.spreadsheet_import.models import SheetModel

_A1_REF_RE = re.compile(r"(?<![A-Z0-9_])(\$?)([A-Z]+)(\$?)([0-9]+)(?!\()", re.IGNORECASE)


def to_r1c1(formula: str, cell_addr: str) -> str:
    """Convert a formula string from A1 notation to relative R1C1 notation."""
    if not formula:
        return formula

    try:
        cell_col, cell_row = parse_address(cell_addr)
    except ValueError:
        return formula

    def replace_ref(match: re.Match) -> str:
        col_abs = bool(match.group(1))
        col_str = match.group(2).upper()
        row_abs = bool(match.group(3))
        row_str = match.group(4)

        try:
            target_col = column_to_index(col_str)
            target_row = int(row_str) - 1
        except ValueError:
            return match.group(0)

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


def r1c1_to_a1(r1c1_formula: str, cell_addr: str) -> str:
    """Convert R1C1 formula string back to A1 notation relative to cell_addr."""
    if not r1c1_formula:
        return r1c1_formula

    try:
        cell_col, cell_row = parse_address(cell_addr)
    except ValueError:
        return r1c1_formula

    def replace_r1c1(match: re.Match) -> str:
        r_abs = match.group(1)
        r_rel = match.group(2)
        c_abs = match.group(3)
        c_rel = match.group(4)

        # Target Row
        if r_abs is not None:
            target_row = int(r_abs) - 1
            row_prefix = "$"
        elif r_rel is not None:
            offset = int(r_rel) if r_rel else 0
            target_row = cell_row + offset
            row_prefix = ""
        else:
            target_row = cell_row
            row_prefix = ""

        # Target Col
        if c_abs is not None:
            target_col = int(c_abs) - 1
            col_prefix = "$"
        elif c_rel is not None:
            offset = int(c_rel) if c_rel else 0
            target_col = cell_col + offset
            col_prefix = ""
        else:
            target_col = cell_col
            col_prefix = ""

        if target_row < 0 or target_col < 0:
            return match.group(0)

        col_str = index_to_column(target_col)
        row_str = str(target_row + 1)
        return f"{col_prefix}{col_str}{row_prefix}{row_str}"

    pattern = r"R(?:(\d+)|\[(-?\d+)\])?C(?:(\d+)|\[(-?\d+)\])?"
    return re.sub(pattern, replace_r1c1, r1c1_formula)


def detect_vectorized_columns(model: SheetModel) -> dict[str, list[str]]:
    """Detect homogeneous relative formulas down columns.

    Returns a dict mapping the first cell address of a vectorized sequence
    to a list of cell addresses in that sequence (including the first one).
    """
    # Group cells by column
    col_cells: dict[int, list[str]] = {}
    for addr in model.cells:
        record = model.cells[addr]
        if record.type in ("formula", "error") and record.formula:
            try:
                col, row = parse_address(addr)
                col_cells.setdefault(col, []).append(addr)
            except ValueError:
                continue

    vector_groups: dict[str, list[str]] = {}

    for col, addrs in col_cells.items():
        # Sort addresses by row
        def get_row(a: str) -> int:
            _, r = parse_address(a)
            return r

        sorted_addrs = sorted(addrs, key=get_row)
        if not sorted_addrs:
            continue

        # Find contiguous ranges with identical R1C1 representation
        current_group: list[str] = []
        current_r1c1: str | None = None
        last_row: int | None = None

        for addr in sorted_addrs:
            record = model.cells[addr]
            if not record.formula:
                continue
            r1c1 = to_r1c1(record.formula, addr)
            _, row = parse_address(addr)

            if current_r1c1 is None:
                current_group = [addr]
                current_r1c1 = r1c1
                last_row = row
            elif r1c1 == current_r1c1 and last_row is not None and row == last_row + 1:
                current_group.append(addr)
                last_row = row
            else:
                if len(current_group) >= 2:
                    vector_groups[current_group[0]] = current_group
                current_group = [addr]
                current_r1c1 = r1c1
                last_row = row

        if len(current_group) >= 2:
            vector_groups[current_group[0]] = current_group

    return vector_groups


def vectorize_range(r1c1_ref: str, start_addr: str, end_addr: str) -> str:
    """Vectorize a range reference over the row span from start_addr to end_addr."""
    if ":" in r1c1_ref:
        start_part, end_part = r1c1_ref.split(":", 1)
        start_a1 = r1c1_to_a1(start_part, start_addr)
        end_a1 = r1c1_to_a1(end_part, end_addr)
        return f"{start_a1}:{end_a1}"
    else:
        start_a1 = r1c1_to_a1(r1c1_ref, start_addr)
        end_a1 = r1c1_to_a1(r1c1_ref, end_addr)
        if start_a1 == end_a1:
            return start_a1
        return f"{start_a1}:{end_a1}"

