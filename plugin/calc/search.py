# Copyright (c) David Berlioz
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Calc search tools: search_cells, search_replace."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from plugin.calc.base import ToolCalcSearchBase
from plugin.calc.spreadsheet_search import (
    configure_search_descriptor,
    iter_target_sheets,
    search_spreadsheet_cells,
)
from plugin.framework.errors import ToolExecutionError
from plugin.writer.search import (
    CASE_SENSITIVE_DESCRIPTION,
    invalid_regex_tool_message,
    resolve_case_sensitive,
    validate_icu_regex,
)

if TYPE_CHECKING:
    from plugin.framework.tool import ToolContext


def _validate_regex(ctx: ToolContext, pattern: str) -> None:
    err = validate_icu_regex(ctx.ctx, pattern)
    if err:
        raise ToolExecutionError(invalid_regex_tool_message(err), code="INVALID_REGEX")


class SearchInSpreadsheet(ToolCalcSearchBase):
    """Search for text in the spreadsheet."""

    name: str | None = "search_cells"
    description: str = "Search for text or values in a Calc spreadsheet. Returns matching cells with their addresses and values."
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Search string or regex pattern."},
            "regex": {"type": "boolean", "description": "Use regular expression (ICU syntax, default: false)."},
            "case_sensitive": {"type": "boolean", "description": CASE_SENSITIVE_DESCRIPTION},
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
            return self._tool_error("pattern is required.", code="INVALID_PARAM")

        max_results = kwargs.get("max_results", 50)
        # Type is settled by the schema; minimum is advertised but not enforced, so the bound is checked here.
        if max_results < 1:
            return self._tool_error("max_results must be a positive integer.", code="INVALID_PARAM")

        use_regex = kwargs.get("regex", False)
        if use_regex:
            _validate_regex(ctx, pattern)

        case_sensitive = resolve_case_sensitive(kwargs.get("case_sensitive"))
        scope = ctx.send_cancellation
        matches = search_spreadsheet_cells(
            ctx.doc,
            pattern,
            regex=use_regex,
            case_sensitive=case_sensitive,
            max_results=max_results,
            all_sheets=kwargs.get("all_sheets", False),
            sheet_name=kwargs.get("sheet"),
            cancel=scope,
        )

        return {"status": "ok", "matches": matches, "count": len(matches), "matched_case_sensitive": case_sensitive}


class ReplaceInSpreadsheet(ToolCalcSearchBase):
    """Find and replace in the spreadsheet."""

    name: str | None = "search_replace"
    description: str = "Find and replace text or values in a Calc spreadsheet. Returns count of replacements made."
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "search": {"type": "string", "description": "Text or regex pattern to find."},
            "replace": {"type": "string", "description": "Replacement text."},
            "regex": {"type": "boolean", "description": "Use regular expression (ICU syntax, default: false)."},
            "case_sensitive": {"type": "boolean", "description": CASE_SENSITIVE_DESCRIPTION},
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
            return self._tool_error("search is required.", code="INVALID_PARAM")

        use_regex = kwargs.get("regex", False)
        if use_regex:
            _validate_regex(ctx, search)

        case_sensitive = resolve_case_sensitive(kwargs.get("case_sensitive"))
        total = 0
        for sheet, _name in iter_target_sheets(ctx.doc, all_sheets=kwargs.get("all_sheets", False), sheet_name=kwargs.get("sheet")):
            rd = sheet.createReplaceDescriptor()
            configure_search_descriptor(rd, search, regex=use_regex, case_sensitive=case_sensitive)
            rd.ReplaceString = replace
            total += sheet.replaceAll(rd)

        return {
            "status": "ok",
            "replacements": total,
            "search": search,
            "replace": replace,
            "matched_case_sensitive": case_sensitive,
        }
