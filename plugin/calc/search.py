# Copyright (c) David Berlioz
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Calc search tools: search_in_spreadsheet, replace_in_spreadsheet."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from plugin.calc.base import ToolCalcSearchBase
from plugin.calc.spreadsheet_search import (
    configure_search_descriptor,
    iter_target_sheets,
    search_spreadsheet_cells,
)
from plugin.writer.search import invalid_regex_tool_message, validate_regex_pattern

if TYPE_CHECKING:
    from plugin.framework.tool import ToolContext


def _validate_regex(tool: ToolCalcSearchBase, pattern: str) -> dict[str, Any] | None:
    err = validate_regex_pattern(pattern)
    if err:
        return tool._tool_error(invalid_regex_tool_message(err), code="INVALID_REGEX")
    return None


class SearchInSpreadsheet(ToolCalcSearchBase):
    """Search for text in the spreadsheet."""

    name: str | None = "search_in_spreadsheet"
    description: str = "Search for text or values in a Calc spreadsheet. Returns matching cells with their addresses and values."
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Search string or regex pattern."},
            "regex": {"type": "boolean", "description": "Use regular expression (default: false)."},
            "case_sensitive": {"type": "boolean", "description": "Case-sensitive search (default: false)."},
            "max_results": {"type": "integer", "minimum": 1, "description": "Maximum results to return (default: 50). Must be a positive integer."},
            "sheet": {"type": "string", "description": "Sheet to search (active sheet if omitted)."},
            "all_sheets": {"type": "boolean", "description": "Search all sheets (default: false)."},
        },
        "required": ["pattern"],
    }
    uno_services: list[str] | None = ["com.sun.star.sheet.SpreadsheetDocument"]

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        pattern = kwargs["pattern"]
        if not pattern:
            return self._tool_error("pattern is required.")

        raw_max = kwargs.get("max_results", 50)
        if type(raw_max) is not int or raw_max < 1:
            return self._tool_error("max_results must be a positive integer.", code="INVALID_PARAM")
        max_results = raw_max

        use_regex = kwargs.get("regex", False)
        if use_regex:
            err = _validate_regex(self, pattern)
            if err:
                return err
        case_sensitive = kwargs.get("case_sensitive", False)
        all_sheets = kwargs.get("all_sheets", False)

        matches = search_spreadsheet_cells(
            ctx.doc,
            pattern,
            regex=use_regex,
            case_sensitive=case_sensitive,
            max_results=max_results,
            all_sheets=all_sheets,
            sheet_name=kwargs.get("sheet"),
            stop_checker=ctx.stop_checker,
        )

        return {"status": "ok", "matches": matches, "count": len(matches)}


class ReplaceInSpreadsheet(ToolCalcSearchBase):
    """Find and replace in the spreadsheet."""

    name: str | None = "replace_in_spreadsheet"
    description: str = "Find and replace text or values in a Calc spreadsheet. Returns count of replacements made."
    parameters: dict[str, Any] | None = {
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
    uno_services: list[str] | None = ["com.sun.star.sheet.SpreadsheetDocument"]
    is_mutation: bool | None = True

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        search = kwargs["search"]
        replace = kwargs["replace"]
        if not search:
            return self._tool_error("search is required.")

        use_regex = kwargs.get("regex", False)
        if use_regex:
            err = _validate_regex(self, search)
            if err:
                return err
        case_sensitive = kwargs.get("case_sensitive", False)
        all_sheets = kwargs.get("all_sheets", False)

        total = 0
        for sheet, _name in iter_target_sheets(ctx.doc, all_sheets=all_sheets, sheet_name=kwargs.get("sheet")):
            rd = sheet.createReplaceDescriptor()
            configure_search_descriptor(rd, search, regex=use_regex, case_sensitive=case_sensitive)
            rd.ReplaceString = replace
            total += sheet.replaceAll(rd)

        return {"status": "ok", "replacements": total, "search": search, "replace": replace}
