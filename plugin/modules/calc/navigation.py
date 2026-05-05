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
    return "%s%d:%s%d" % (index_to_column(ra.StartColumn), ra.StartRow + 1, index_to_column(ra.EndColumn), ra.EndRow + 1)


class ListNamedRanges(ToolBase):
    """List all named ranges in the spreadsheet."""

    name = "list_named_ranges"
    intent = "navigate"
    description = "List all named ranges defined in the Calc spreadsheet. Returns name, formula/content, and range address."
    parameters = {"type": "object", "properties": {}, "required": []}
    uno_services = ["com.sun.star.sheet.SpreadsheetDocument"]

    def execute(self, ctx, **kwargs):
        doc = ctx.doc
        named_ranges = doc.NamedRanges
        result = []
        for name in named_ranges.getElementNames():
            nr = named_ranges.getByName(name)
            entry = {"name": name}
            try:
                entry["content"] = nr.getContent()
            except Exception as e:
                log.debug("list_named_ranges getContent error for %s: %s", entry["name"], e)
            try:
                ra = nr.getReferredCells().getRangeAddress()
                entry["range"] = _range_address_str(ra)
            except Exception as e:
                log.debug("list_named_ranges getRangeAddress error for %s: %s", entry["name"], e)
            result.append(entry)
        return {"status": "ok", "named_ranges": result, "count": len(result)}
