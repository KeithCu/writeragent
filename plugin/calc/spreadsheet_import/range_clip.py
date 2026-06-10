# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Clip whole-column/row PY data refs to a workbook sheet's used area."""

from __future__ import annotations

import re

# ACTUAL.F:F, 'Dashboard Finished'.C:C, or local F:F
_QUOTED_COL_ONLY_RE = re.compile(r"^'([^']+)'.([A-Z]+):\2$", re.IGNORECASE)
_SHEET_COL_ONLY_RE = re.compile(r"^([A-Za-z][A-Za-z0-9_ ]*)\.([A-Z]+):\2$", re.IGNORECASE)
_LOCAL_COL_ONLY_RE = re.compile(r"^([A-Z]+):\1$", re.IGNORECASE)


def clip_workbook_data_ranges(
    ranges: list[str],
    *,
    sheet_bounds: dict[str, tuple[int, int]] | None,
    current_sheet: str | None = None,
) -> list[str]:
    """Clip ``SHEET.F:F`` style refs to ``SHEET.F1:F{used_row}`` when bounds are known."""
    if not sheet_bounds:
        return list(ranges)
    return [_clip_one_range(r, sheet_bounds, current_sheet=current_sheet) for r in ranges]


def _clip_one_range(
    range_ref: str,
    sheet_bounds: dict[str, tuple[int, int]],
    *,
    current_sheet: str | None = None,
) -> str:
    normalized = str(range_ref).strip().replace("$", "")
    quoted_sheet: str | None = None
    bare_sheet: str | None = None
    col: str | None = None
    current_sheet_key = ""

    quoted = _QUOTED_COL_ONLY_RE.match(normalized)
    if quoted:
        quoted_sheet, col = quoted.group(1), quoted.group(2)
        current_sheet_key = quoted_sheet.upper()
    else:
        sheet_col = _SHEET_COL_ONLY_RE.match(normalized)
        if sheet_col:
            bare_sheet, col = sheet_col.group(1), sheet_col.group(2)
            current_sheet_key = bare_sheet.upper()
        else:
            local = _LOCAL_COL_ONLY_RE.match(normalized)
            if local:
                col = local.group(1)
                if current_sheet:
                    current_sheet_key = current_sheet.upper()

    if not col:
        return range_ref

    sheet_key = current_sheet_key
    if not sheet_key or sheet_key not in sheet_bounds:
        return range_ref
    _end_col, end_row = sheet_bounds[sheet_key]
    end_row += 1  # 0-based → 1-based A1
    col = col.upper()
    if quoted_sheet:
        return f"'{quoted_sheet}'.{col}1:{col}{end_row}"
    if bare_sheet:
        return f"{bare_sheet}.{col}1:{col}{end_row}"
    return f"{col}1:{col}{end_row}"
