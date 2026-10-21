# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared cell text writer and table manipulation helpers for Writer and Draw text tables.

Public surface:
- ``_set_cell_string``: Set string on a cell (via getText() or setString).
- ``fill_table_cells``: Write a 2D grid into a table by column and row coordinates.
- ``table_dims``: Return (rows, cols) dimensions of a table.
- ``edit_table_band``: Insert or remove rows/columns on a table band.
"""

from __future__ import annotations

from typing import Any

from plugin.framework.errors import uno_probe

__all__ = [
    "_set_cell_string",
    "edit_table_band",
    "fill_table_cells",
    "table_dims",
]


def _set_cell_string(cell: Any, text: str) -> None:
    """Set text on a Writer or Draw cell."""

    def via_text() -> bool:
        cell.getText().setString(text)
        return True

    if hasattr(cell, "getText") and uno_probe(via_text, False):
        return
    if hasattr(cell, "setString"):
        cell.setString(text)


def fill_table_cells(table: Any, data: Any) -> int:
    """Write a 2D string grid into ``table.getCellByPosition(col, row)``. Returns cells written."""
    written = 0
    for r_idx, row in enumerate(data):
        if not isinstance(row, (list, tuple)):
            continue
        for c_idx, val in enumerate(row):
            cell = table.getCellByPosition(c_idx, r_idx)
            text = "" if val is None else str(val)
            _set_cell_string(cell, text)
            written += 1
    return written


def table_dims(table: Any) -> tuple[int, int]:
    """Return (rows, cols) integer count for a Writer or Draw table."""
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
