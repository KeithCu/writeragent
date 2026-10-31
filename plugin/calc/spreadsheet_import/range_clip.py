# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Clip whole-column/row PY data refs to a workbook sheet's used area.

Bounds are 0-based, end-inclusive. Clipping freezes the data extent so whole-axis
references do not materialize full spreadsheet dimensions.
"""

from __future__ import annotations

from plugin.calc.address_utils import index_to_column, split_sheet_prefix


def _split_axis(address: str) -> tuple[str, str, str] | None:
    address = address.replace("$", "")
    start, has_sep, end = address.partition(":")
    if not has_sep or not start or not end:
        return None
    if start.isascii() and end.isascii() and start.isalpha() and end.isalpha():
        return "col", start.upper(), end.upper()
    if start.isascii() and end.isascii() and start.isdigit() and end.isdigit():
        return "row", start, end
    return None


def _quote_sheet_reference(name: str, was_quoted: bool) -> str:
    if was_quoted or not name.isidentifier():
        return "'" + name.replace("'", "''") + "'"
    return name


def clip_workbook_data_ranges(ranges: list[str], *, sheet_bounds: dict[str, tuple[int, int]] | None, current_sheet: str | None = None) -> list[str]:
    """Clip ``SHEET.F:F`` style refs to ``SHEET.F1:F{used_row}`` when bounds are known."""
    bounds = {str(key).upper(): value for key, value in (sheet_bounds or {}).items()}
    return [_clip_one_range(r, bounds, current_sheet=current_sheet) for r in ranges]


def _clip_one_range(range_ref: str, sheet_bounds: dict[str, tuple[int, int]], *, current_sheet: str | None = None) -> str:
    text = str(range_ref).strip()
    sheet, address = split_sheet_prefix(text)
    axis_info = _split_axis(address)
    if axis_info is None:
        return range_ref

    kind, start, end = axis_info
    resolved_sheet = (sheet or current_sheet or "").upper()
    if not resolved_sheet or resolved_sheet not in sheet_bounds:
        return range_ref

    end_col, end_row = sheet_bounds[resolved_sheet]

    if sheet:
        was_quoted = text.lstrip().startswith("'") or text.lstrip().startswith("$'")
        quoted_name = _quote_sheet_reference(sheet, was_quoted)
        sep = "." if text[:text.rfind(address)].rstrip().endswith(".") else "!"
        prefix = f"{quoted_name}{sep}"
    else:
        prefix = ""

    if kind == "col":
        return f"{prefix}{start}1:{end}{end_row + 1}"
    return f"{prefix}A{start}:{index_to_column(end_col)}{end}"
