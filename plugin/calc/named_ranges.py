# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
"""Calc named range management tools."""

import logging

from plugin.framework.errors import ToolExecutionError, UnoObjectError
from plugin.calc.base import ToolCalcRangeBase
from plugin.calc.bridge import CalcBridge

logger = logging.getLogger("writeragent.calc")


class ListNamedRanges(ToolCalcRangeBase):
    """List all named ranges in the workbook."""

    name = "list_named_ranges"
    description = "Lists all named ranges in the workbook and their reference contents."
    parameters = {"type": "object", "properties": {}}
    is_mutation = False

    def execute(self, ctx, **kwargs):
        bridge = CalcBridge(ctx.doc)
        try:
            doc = bridge.get_active_document()
            named_ranges = doc.NamedRanges
            names = named_ranges.getElementNames()
            result = []
            for name in names:
                nr = named_ranges.getByName(name)
                result.append({
                    "name": name,
                    "content": nr.getContent()
                })
            logger.info("Named ranges listed: %s", [r["name"] for r in result])
            return {"status": "ok", "result": result}
        except Exception as e:
            logger.error("List named ranges error: %s", str(e))
            raise ToolExecutionError(str(e)) from e


class AddNamedRange(ToolCalcRangeBase):
    """Add a new named range to the workbook."""

    name = "add_named_range"
    intent = "edit"
    description = "Defines a new named range in the workbook."
    parameters = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "The name of the range (e.g. 'TaxRate', 'SalesData'). Must start with a letter or underscore and have no spaces."},
            "content": {"type": "string", "description": "The formula or cell range address it points to (e.g. '$Sheet1.$A$1:$B$5')."}
        },
        "required": ["name", "content"]
    }
    is_mutation = True

    def execute(self, ctx, **kwargs):
        from com.sun.star.table import CellAddress
        bridge = CalcBridge(ctx.doc)
        name = kwargs["name"]
        content = kwargs["content"]

        try:
            doc = bridge.get_active_document()
            named_ranges = doc.NamedRanges
            if named_ranges.hasByName(name):
                raise UnoObjectError(f"A named range with the name '{name}' already exists.")

            pos = CellAddress(Sheet=0, Column=0, Row=0)
            named_ranges.addNewByName(name, content, pos, 0)
            logger.info("Named range added: %s -> %s", name, content)
            return {"status": "ok", "message": f"Named range '{name}' added successfully pointing to '{content}'."}
        except Exception as e:
            logger.error("Add named range error (%s): %s", name, str(e))
            raise ToolExecutionError(str(e)) from e


class DeleteNamedRange(ToolCalcRangeBase):
    """Delete an existing named range from the workbook."""

    name = "delete_named_range"
    intent = "edit"
    description = "Deletes an existing named range by name."
    parameters = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Name of the range to delete."}
        },
        "required": ["name"]
    }
    is_mutation = True

    def execute(self, ctx, **kwargs):
        bridge = CalcBridge(ctx.doc)
        name = kwargs["name"]

        try:
            doc = bridge.get_active_document()
            named_ranges = doc.NamedRanges
            if not named_ranges.hasByName(name):
                raise UnoObjectError(f"No named range found with the name '{name}'.")
            named_ranges.removeByName(name)
            logger.info("Named range deleted: %s", name)
            return {"status": "ok", "message": f"Named range '{name}' deleted successfully."}
        except Exception as e:
            logger.error("Delete named range error (%s): %s", name, str(e))
            raise ToolExecutionError(str(e)) from e
