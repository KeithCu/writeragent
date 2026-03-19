# Copyright (c) David Berlioz
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Calc navigation tools: named ranges, data regions, sheet overview."""

import logging

from plugin.framework.tool_base import ToolBase
from plugin.modules.calc.address_utils import index_to_column

log = logging.getLogger("nelson.calc")


def _range_address_str(ra):
    """Convert a RangeAddress to 'Sheet.A1:D10' style."""
    return "%s%d:%s%d" % (
        index_to_column(ra.StartColumn), ra.StartRow + 1,
        index_to_column(ra.EndColumn), ra.EndRow + 1,
    )


class ListNamedRanges(ToolBase):
    """List all named ranges in the spreadsheet."""

    name = "list_named_ranges"
    intent = "navigate"
    description = (
        "List all named ranges defined in the Calc spreadsheet. "
        "Returns name, formula/content, and range address."
    )
    parameters = {
        "type": "object",
        "properties": {},
        "required": [],
    }
    doc_types = ["calc"]

    def execute(self, ctx, **kwargs):
        doc = ctx.doc
        try:
            named_ranges = doc.NamedRanges
            result = []
            for name in named_ranges.getElementNames():
                nr = named_ranges.getByName(name)
                entry = {"name": name}
                try:
                    entry["content"] = nr.getContent()
                except Exception as e:
                    logger.debug("list_named_ranges getContent error for %s: %s", entry["name"], e)
                try:
                    ra = nr.getReferredCells().getRangeAddress()
                    entry["range"] = _range_address_str(ra)
                except Exception as e:
                    logger.debug("list_named_ranges getRangeAddress error for %s: %s", entry["name"], e)
                result.append(entry)
            return {
                "status": "ok",
                "named_ranges": result,
                "count": len(result),
            }
        except Exception as e:
            return {"status": "error", "error": str(e)}


class GetSheetOverview(ToolBase):
    """Get an overview of a sheet's data regions and structure."""

    name = "get_sheet_overview"
    intent = "navigate"
    description = (
        "Get an overview of a Calc sheet: used area, data regions, "
        "charts, merged cells, and annotations count."
    )
    parameters = {
        "type": "object",
        "properties": {
            "sheet_name": {
                "type": "string",
                "description": "Sheet name (active sheet if omitted).",
            },
        },
        "required": [],
    }
    doc_types = ["calc"]

    def execute(self, ctx, **kwargs):
        doc = ctx.doc
        try:
            sheet_name = kwargs.get("sheet_name")
            if sheet_name:
                sheets = doc.getSheets()
                if not sheets.hasByName(sheet_name):
                    return {"status": "error",
                            "message": "Sheet not found: %s" % sheet_name}
                sheet = sheets.getByName(sheet_name)
            else:
                controller = doc.getCurrentController()
                sheet = controller.getActiveSheet()

            result = {"status": "ok", "sheet": sheet.getName()}

            # Used area via cursor
            try:
                cursor = sheet.createCursor()
                cursor.gotoStartOfUsedArea(False)
                cursor.gotoEndOfUsedArea(True)
                ra = cursor.getRangeAddress()
                result["used_area"] = _range_address_str(ra)
                result["used_rows"] = ra.EndRow - ra.StartRow + 1
                result["used_cols"] = ra.EndColumn - ra.StartColumn + 1
            except Exception as e:
                logger.debug("get_sheet_info used_area error: %s", e)
                result["used_area"] = None

            # Charts
            try:
                charts = sheet.getCharts()
                result["chart_count"] = charts.getCount()
                result["charts"] = list(charts.getElementNames())
            except Exception as e:
                logger.debug("get_sheet_info charts error: %s", e)
                result["chart_count"] = 0

            # Annotations
            try:
                result["annotation_count"] = sheet.getAnnotations().getCount()
            except Exception as e:
                logger.debug("get_sheet_info annotations error: %s", e)
                result["annotation_count"] = 0

            # Merged cells - count via querying
            try:
                merge_count = 0
                if hasattr(cursor, "getMergedArea"):
                    # Iterate used area to find merges
                    pass  # expensive, skip for overview
                result["has_merges"] = sheet.getPropertyValue("HasMergedCells") if hasattr(sheet, "getPropertyValue") else None
            except Exception as e:
                logger.debug("get_sheet_info merged cells error: %s", e)

            # Draw page (shapes on sheet)
            try:
                dp = sheet.DrawPage
                result["shape_count"] = dp.getCount()
            except Exception as e:
                logger.debug("get_sheet_info shape_count error: %s", e)
                result["shape_count"] = 0

            return result
        except Exception as e:
            return {"status": "error", "error": str(e)}
