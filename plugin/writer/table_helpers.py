# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared cell text writer and table manipulation helpers for Writer and Draw text tables.

Public surface:
- ``_set_cell_string``: Set string on a cell (via getText() or setString).
- ``fill_table_cells``: Validate and write a 2D grid into a table by column and row coordinates.
- ``table_dims``: Return (rows, cols) dimensions of a table.
- ``edit_table_band``: Insert or remove rows/columns on a table band.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "_set_cell_string",
    "edit_table_band",
    "fill_table_cells",
    "table_dims",
]


def _set_cell_string(cell: Any, text: str) -> None:
    """Set text on a Writer or Draw cell."""
    # Write errors and disposal must propagate instead of being swallowed by a probe,
    # and availability is determined by hasattr rather than trial write.
    if hasattr(cell, "getText"):
        cell.getText().setString(text)
        return
    cell.setString(text)


def fill_table_cells(table: Any, data: Any) -> int:
    """Write a 2D string grid into ``table.getCellByPosition(col, row)``. Returns cells written."""
    # Validate entire grid before mutating so malformed rows are not silently dropped
    # and oversized grids do not leave partially-filled tables.
    rows, cols = table_dims(table)
    if len(data) > rows:
        raise ValueError("data has %d rows but table only has %d." % (len(data), rows))
    for r_idx, row in enumerate(data):
        if not isinstance(row, (list, tuple)):
            raise ValueError("data row %d is not a list of cells." % r_idx)
        if len(row) > cols:
            raise ValueError("data row %d has %d columns but table only has %d." % (r_idx, len(row), cols))

    written = 0
    for r_idx, row in enumerate(data):
        for c_idx, val in enumerate(row):
            cell = table.getCellByPosition(c_idx, r_idx)
            _set_cell_string(cell, "" if val is None else str(val))
            written += 1
    return written


def table_dims(table: Any) -> tuple[int, int]:
    """Return (rows, cols) integer count for a Writer or Draw table.

    Writer: ``getColumns().getCount()`` is the first row's box count, so after a
    cell merge ``cols`` can be smaller than the real grid (A1:D1 on a 5x4 table
    reports 1). Cell addresses must come from ``getCellNames()``, not ``range(cols)``.
    Draw: ``cols`` is the real column count.
    """
    return int(table.getRows().getCount()), int(table.getColumns().getCount())


def edit_table_band(band: Any, action: str, idx: int, axis_label: str) -> None:
    """Insert or remove one row/column from a table's XTableRows or XTableColumns band."""
    axis = "rows" if axis_label in ("row", "rows") else "columns"
    count = int(band.getCount())
    if action == "insert":
        if idx > count:
            raise ValueError("index %d out of range (table has %d %s; use 0..%d)." % (idx, count, axis, count))
        band.insertByIndex(idx, 1)
    else:
        if idx >= count:
            raise ValueError("index %d out of range (table has %d %s; use 0..%d)." % (idx, count, axis, count - 1))
        if count <= 1:
            raise ValueError("Cannot remove the last %s of a table." % axis[:-1])
        band.removeByIndex(idx, 1)
