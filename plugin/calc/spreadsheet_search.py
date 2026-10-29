# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""Shared Calc cell search helpers for search tools and document_research grep."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from plugin.calc.address_utils import format_address
from plugin.calc.bridge import filter_agent_sheet_names
from plugin.calc.calc_utils import resolve_sheet

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

# Cap search hit text at the same 200-character snippet budget as
# document_research_grep (not a user config setting).
_HIT_TEXT_MAX = 200


def iter_target_sheets(doc: Any, *, all_sheets: bool, sheet_name: str | None = None) -> Iterator[tuple[Any, str]]:
    """Yield ``(sheet, name)`` pairs respecting the agent-visible sheet filter."""
    if all_sheets:
        sheets_obj = doc.getSheets()
        for n in filter_agent_sheet_names(sheets_obj.getElementNames()):
            yield sheets_obj.getByName(n), n
    else:
        sheet = resolve_sheet(doc, sheet_name)
        yield sheet, sheet.getName()


def configure_search_descriptor(desc: Any, text: str, *, regex: bool = False, case_sensitive: bool = False) -> None:
    """Set standard search parameters on a UNO SearchDescriptor or ReplaceDescriptor."""
    desc.SearchString = text
    desc.SearchRegularExpression = bool(regex)
    desc.SearchCaseSensitive = bool(case_sensitive)


def search_spreadsheet_cells(
    doc: Any,
    pattern: str,
    *,
    regex: bool = False,
    case_sensitive: bool = False,
    max_results: int = 50,
    all_sheets: bool = True,
    sheet_name: str | None = None,
    stop_checker: Callable[[], bool] | None = None,
) -> list[dict[str, Any]]:
    """Search a Calc document for *pattern*; return match dicts with sheet, cell, value. Long values are truncated to 200 characters with a trailing "…"."""
    if not pattern:
        return []

    matches: list[dict[str, Any]] = []

    for sheet, sname in iter_target_sheets(doc, all_sheets=all_sheets, sheet_name=sheet_name):
        if stop_checker and stop_checker():
            break
        sd = sheet.createSearchDescriptor()
        configure_search_descriptor(sd, pattern, regex=regex, case_sensitive=case_sensitive)

        # findAll() joins adjacent hits into a range object that has no
        # getCellAddress()/getString(); findFirst/findNext return one cell
        # per hit. max_results still bounds the loop if findNext repeats.
        cell = sheet.findFirst(sd)
        while cell is not None:
            if (stop_checker and stop_checker()) or len(matches) >= max_results:
                return matches
            value = cell.getString()
            if len(value) > _HIT_TEXT_MAX:
                value = value[:_HIT_TEXT_MAX] + "…"
            addr = cell.getCellAddress()
            matches.append({"sheet": sname, "cell": format_address(addr.Column, addr.Row), "value": value})
            cell = sheet.findNext(cell, sd)

    return matches
