# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared cell text writer helpers for Writer and Draw text tables.

Public surface:
- ``_set_cell_string``: Set string on a cell (via getText() or setString).
- ``fill_table_cells``: Write a 2D grid into a table by column and row coordinates.
"""

from __future__ import annotations

from typing import Any

from plugin.framework.errors import uno_probe

__all__ = [
    "_set_cell_string",
    "fill_table_cells",
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
