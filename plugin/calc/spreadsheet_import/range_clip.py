# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Clip whole-column/row PY data refs to a workbook sheet's used area.

Bounds are 0-based, end-inclusive. Clipping freezes the data extent so whole-axis
references do not materialize full spreadsheet dimensions.
"""

from __future__ import annotations

from plugin.calc.address_utils import index_to_column, qualify_a1, split_axis, split_sheet_prefix


def clip_workbook_data_ranges(ranges: list[str], *, sheet_bounds: dict[str, tuple[int, int]] | None, current_sheet: str | None = None) -> list[str]:
    """Clip ``SHEET.F:F`` style refs to ``SHEET.F1:F{used_row}`` when bounds are known."""
    bounds = {str(key).upper(): value for key, value in (sheet_bounds or {}).items()}
    return [_clip_one_range(r, bounds, current_sheet=current_sheet) for r in ranges]


def _clip_one_range(range_ref: str, sheet_bounds: dict[str, tuple[int, int]], *, current_sheet: str | None = None) -> str:
    text = str(range_ref).strip()
    sheet, address = split_sheet_prefix(text)
    axis_info = split_axis(address)
    if axis_info is None:
        return range_ref

    kind, start, end = axis_info
    resolved_sheet = (sheet or current_sheet or "").upper()
    if not resolved_sheet or resolved_sheet not in sheet_bounds:
        return range_ref

    end_col, end_row = sheet_bounds[resolved_sheet]

    clipped = f"{start}1:{end}{end_row + 1}" if kind == "col" else f"A{start}:{index_to_column(end_col)}{end}"
    # PyFormula.compose re-quotes the sheet by its own rule when it writes the =PY() data argument,
    # so this only has to hand it a reference split_sheet_prefix can read back.
    return qualify_a1(sheet, clipped) if sheet else clipped
