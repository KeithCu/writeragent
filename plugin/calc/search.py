# Copyright (c) David Berlioz
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Calc search tools: search_in_spreadsheet, replace_in_spreadsheet."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from plugin.calc.base import ToolCalcSearchBase
from plugin.calc.calc_utils import resolve_sheet
from plugin.calc.spreadsheet_search import search_spreadsheet_cells

if TYPE_CHECKING:
    from plugin.framework.tool import ToolContext

log = logging.getLogger("writeragent.calc")



class SearchInSpreadsheet(ToolCalcSearchBase):
    """Search for text in the spreadsheet."""

    name: str | None = "search_in_spreadsheet"
    description: str = "Search for text or values in a Calc spreadsheet. Returns matching cells with their addresses and values."
    parameters: dict | None = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Search string or regex pattern."},
            "regex": {"type": "boolean", "description": "Use regular expression (default: false)."},
            "case_sensitive": {"type": "boolean", "description": "Case-sensitive search (default: false)."},
            "max_results": {"type": "integer", "description": "Maximum results to return (default: 50)."},
            "sheet": {"type": "string", "description": "Sheet to search (active sheet if omitted)."},
            "all_sheets": {"type": "boolean", "description": "Search all sheets (default: false)."},
        },
        "required": ["pattern"],
    }
    uno_services: list = ["com.sun.star.sheet.SpreadsheetDocument"]

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        pattern = kwargs.get("pattern", "")
        if not pattern:
            return self._tool_error("pattern is required.")

        use_regex = kwargs.get("regex", False)
        case_sensitive = kwargs.get("case_sensitive", False)
        max_results = kwargs.get("max_results", 50)
        all_sheets = kwargs.get("all_sheets", False)

        matches = search_spreadsheet_cells(
            ctx.doc,
            pattern,
            regex=use_regex,
            case_sensitive=case_sensitive,
            max_results=max_results,
            all_sheets=all_sheets,
            sheet_name=kwargs.get("sheet"),
        )

        return {"status": "ok", "matches": matches, "count": len(matches)}


class ReplaceInSpreadsheet(ToolCalcSearchBase):
    """Find and replace in the spreadsheet."""

    name: str | None = "replace_in_spreadsheet"
    description: str = "Find and replace text or values in a Calc spreadsheet. Returns count of replacements made."
    parameters: dict | None = {
        "type": "object",
        "properties": {
            "search": {"type": "string", "description": "Text or regex pattern to find."},
            "replace": {"type": "string", "description": "Replacement text."},
            "regex": {"type": "boolean", "description": "Use regular expression (default: false)."},
            "case_sensitive": {"type": "boolean", "description": "Case-sensitive matching (default: false)."},
            "sheet": {"type": "string", "description": "Sheet to operate on (active sheet if omitted)."},
            "all_sheets": {"type": "boolean", "description": "Replace across all sheets (default: false)."},
        },
        "required": ["search", "replace"],
    }
    uno_services: list = ["com.sun.star.sheet.SpreadsheetDocument"]
    is_mutation: bool | None = True

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        search = kwargs.get("search", "")
        replace = kwargs.get("replace", "")
        if not search:
            return self._tool_error("search is required.")

        use_regex = kwargs.get("regex", False)
        case_sensitive = kwargs.get("case_sensitive", False)
        all_sheets = kwargs.get("all_sheets", False)

        doc = ctx.doc
        total = 0

        if all_sheets:
            sheets_obj = doc.getSheets()
            targets = [sheets_obj.getByName(n) for n in sheets_obj.getElementNames()]
        else:
            targets = [resolve_sheet(doc, kwargs.get("sheet"))]

        for sheet in targets:
            rd = sheet.createReplaceDescriptor()
            rd.SearchString = search
            rd.ReplaceString = replace
            rd.SearchRegularExpression = bool(use_regex)
            rd.SearchCaseSensitive = bool(case_sensitive)
            total += sheet.replaceAll(rd)

        return {"status": "ok", "replacements": total, "search": search, "replace": replace}
