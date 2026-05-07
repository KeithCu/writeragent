# Copyright (c) David Berlioz
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Calc search tools: search_in_spreadsheet, replace_in_spreadsheet."""

from plugin.framework.errors import UnoObjectError
import logging

from plugin.framework.tool import ToolBase

log = logging.getLogger("nelson.calc")


def _resolve_sheet(doc, sheet_name=None):
    """Return the target sheet (by name or active)."""
    if sheet_name:
        sheets = doc.getSheets()
        if not sheets.hasByName(sheet_name):
            raise UnoObjectError("Sheet not found: %s" % sheet_name)
        return sheets.getByName(sheet_name)
    controller = doc.getCurrentController()
    if hasattr(controller, "getActiveSheet"):
        return controller.getActiveSheet()
    return doc.getSheets().getByIndex(0)


def _cell_address_str(cell):
    """Return 'A1'-style address from a cell."""
    from plugin.modules.calc.address_utils import index_to_column

    col = cell.getCellAddress().Column
    row = cell.getCellAddress().Row
    return "%s%d" % (index_to_column(col), row + 1)


class SearchInSpreadsheet(ToolBase):
    """Search for text in the spreadsheet."""

    name = "search_in_spreadsheet"
    description = "Search for text or values in a Calc spreadsheet. Returns matching cells with their addresses and values."
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Search string or regex pattern."},
            "regex": {"type": "boolean", "description": "Use regular expression (default: false)."},
            "case_sensitive": {"type": "boolean", "description": "Case-sensitive search (default: false)."},
            "max_results": {"type": "integer", "description": "Maximum results to return (default: 50)."},
            "sheet_name": {"type": "string", "description": "Sheet to search (active sheet if omitted)."},
            "all_sheets": {"type": "boolean", "description": "Search all sheets (default: false)."},
        },
        "required": ["pattern"],
    }
    uno_services = ["com.sun.star.sheet.SpreadsheetDocument"]
    tier = "core"

    def execute(self, ctx, **kwargs):
        pattern = kwargs.get("pattern", "")
        if not pattern:
            return self._tool_error("pattern is required.")

        use_regex = kwargs.get("regex", False)
        case_sensitive = kwargs.get("case_sensitive", False)
        max_results = kwargs.get("max_results", 50)
        all_sheets = kwargs.get("all_sheets", False)

        doc = ctx.doc
        matches = []

        if all_sheets:
            sheets_obj = doc.getSheets()
            targets = [(sheets_obj.getByName(n), n) for n in sheets_obj.getElementNames()]
        else:
            sheet = _resolve_sheet(doc, kwargs.get("sheet_name"))
            targets = [(sheet, sheet.getName())]

        for sheet, sname in targets:
            sd = sheet.createSearchDescriptor()
            sd.SearchString = pattern
            sd.SearchRegularExpression = bool(use_regex)
            sd.SearchCaseSensitive = bool(case_sensitive)

            found = sheet.findAll(sd)
            if found is None:
                continue

            for i in range(found.getCount()):
                if len(matches) >= max_results:
                    break
                cell = found.getByIndex(i)
                matches.append({"sheet": sname, "cell": _cell_address_str(cell), "value": cell.getString()})
            if len(matches) >= max_results:
                break

        return {"status": "ok", "matches": matches, "count": len(matches)}


class ReplaceInSpreadsheet(ToolBase):
    """Find and replace in the spreadsheet."""

    name = "replace_in_spreadsheet"
    description = "Find and replace text or values in a Calc spreadsheet. Returns count of replacements made."
    parameters = {
        "type": "object",
        "properties": {
            "search": {"type": "string", "description": "Text or regex pattern to find."},
            "replace": {"type": "string", "description": "Replacement text."},
            "regex": {"type": "boolean", "description": "Use regular expression (default: false)."},
            "case_sensitive": {"type": "boolean", "description": "Case-sensitive matching (default: false)."},
            "sheet_name": {"type": "string", "description": "Sheet to operate on (active sheet if omitted)."},
            "all_sheets": {"type": "boolean", "description": "Replace across all sheets (default: false)."},
        },
        "required": ["search", "replace"],
    }
    uno_services = ["com.sun.star.sheet.SpreadsheetDocument"]
    tier = "core"
    is_mutation = True

    def execute(self, ctx, **kwargs):
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
            targets = [_resolve_sheet(doc, kwargs.get("sheet_name"))]

        for sheet in targets:
            rd = sheet.createReplaceDescriptor()
            rd.SearchString = search
            rd.ReplaceString = replace
            rd.SearchRegularExpression = bool(use_regex)
            rd.SearchCaseSensitive = bool(case_sensitive)
            total += sheet.replaceAll(rd)

        return {"status": "ok", "replacements": total, "search": search, "replace": replace}
