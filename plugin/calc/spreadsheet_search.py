# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""Shared Calc cell search helpers for search tools and document_research grep."""

from __future__ import annotations

from typing import Any


def _cell_address_str(cell: Any) -> str:
    from plugin.calc.address_utils import format_address

    addr = cell.getCellAddress()
    return format_address(addr.Column, addr.Row)


def search_spreadsheet_cells(doc: Any, pattern: str, *, regex: bool = False, case_sensitive: bool = False, max_results: int = 50, all_sheets: bool = True, sheet_name: str | None = None, stop_checker: Any = None) -> list[dict[str, Any]]:
    """Search a Calc document for *pattern*; return match dicts with sheet, cell, value."""
    from plugin.calc.calc_utils import resolve_sheet
    from plugin.calc.bridge import is_agent_visible_sheet

    if not pattern:
        return []

    matches: list[dict[str, Any]] = []

    if all_sheets:
        sheets_obj = doc.getSheets()
        targets = [(sheets_obj.getByName(n), n) for n in sheets_obj.getElementNames() if is_agent_visible_sheet(n)]
    else:
        sheet = resolve_sheet(doc, sheet_name)
        targets = [(sheet, sheet.getName())]

    for sheet, sname in targets:
        if stop_checker and stop_checker():
            break
        sd = sheet.createSearchDescriptor()
        sd.SearchString = pattern
        sd.SearchRegularExpression = bool(regex)
        sd.SearchCaseSensitive = bool(case_sensitive)

        found = sheet.findAll(sd)
        if found is None:
            continue

        for i in range(found.getCount()):
            if stop_checker and stop_checker():
                break
            if len(matches) >= max_results:
                return matches
            cell = found.getByIndex(i)
            matches.append({"sheet": sname, "cell": _cell_address_str(cell), "value": cell.getString()})

    return matches
