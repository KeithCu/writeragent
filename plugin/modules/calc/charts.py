# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2024 John Balis
# Copyright (c) 2026 KeithCu (modifications and relicensing)
# Copyright (c) 2026 LibreCalc AI Assistant (Calc integration features, originally MIT)
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
"""Calc chart management tools: list, info, create, edit, delete."""

import logging

from plugin.framework.tool_base import ToolBase
from plugin.modules.calc.bridge import CalcBridge
from plugin.modules.calc.manipulator import CellManipulator

logger = logging.getLogger("writeragent.calc")


class ListCharts(ToolBase):
    """List all charts on a Calc sheet."""

    name = "list_charts"
    intent = "navigate"
    description = (
        "List all charts on the active Calc sheet with name, title, and legend status."
    )
    parameters = {
        "type": "object",
        "properties": {},
        "required": [],
    }
    doc_types = ["calc"]

    def execute(self, ctx, **kwargs):
        bridge = CalcBridge(ctx.doc)
        manipulator = CellManipulator(bridge)
        charts = manipulator.list_charts()
        return {
            "status": "ok",
            "charts": charts,
            "count": len(charts),
        }
class GetChartInfo(ToolBase):
    """Get detailed info about a chart."""

    name = "get_chart_info"
    intent = "navigate"
    description = (
        "Get detailed info about a Calc chart: type, title, "
        "data ranges, legend, and diagram properties."
    )
    parameters = {
        "type": "object",
        "properties": {
            "chart_name": {
                "type": "string",
                "description": "Chart name (from list_charts).",
            },
        },
        "required": ["chart_name"],
    }
    doc_types = ["calc"]

    def execute(self, ctx, **kwargs):
        bridge = CalcBridge(ctx.doc)
        manipulator = CellManipulator(bridge)
        chart_name = kwargs["chart_name"]
        info = manipulator.get_chart_info(chart_name)
        if info is None:
            return self._tool_error(f"Chart '{chart_name}' not found.")
        info["status"] = "ok"
        return info
class CreateChart(ToolBase):
    """Create a new chart from a data range."""

    name = "create_chart"
    intent = "edit"
    description = (
        "Creates a chart on the active sheet from the specified data range."
    )
    parameters = {
        "type": "object",
        "properties": {
            "data_range": {
                "type": "string",
                "description": "Cell range for chart data (e.g. 'A1:B10').",
            },
            "chart_type": {
                "type": "string",
                "enum": ["bar", "column", "line", "pie", "scatter"],
                "description": "Type of chart to create.",
            },
            "title": {
                "type": "string",
                "description": "Chart title (optional).",
            },
            "position": {
                "type": "string",
                "description": "Cell address for chart placement (e.g. 'E1').",
            },
            "has_header": {
                "type": "boolean",
                "description": "Whether first row/column is a label (default: true).",
            },
        },
        "required": ["data_range", "chart_type"],
    }
    doc_types = ["calc"]
    is_mutation = True

    def execute(self, ctx, **kwargs):
        bridge = CalcBridge(ctx.doc)
        manipulator = CellManipulator(bridge)
        result = manipulator.create_chart(
            kwargs["data_range"],
            kwargs["chart_type"],
            title=kwargs.get("title"),
            position=kwargs.get("position"),
            has_header=kwargs.get("has_header", True),
        )
        return {"status": "ok", "message": result}
class EditChart(ToolBase):
    """Modify chart properties."""

    name = "edit_chart"
    intent = "edit"
    description = (
        "Edit a Calc chart: update title, subtitle, or legend visibility."
    )
    parameters = {
        "type": "object",
        "properties": {
            "chart_name": {
                "type": "string",
                "description": "Chart name (from list_charts).",
            },
            "title": {
                "type": "string",
                "description": "New chart title.",
            },
            "subtitle": {
                "type": "string",
                "description": "New chart subtitle.",
            },
            "has_legend": {
                "type": "boolean",
                "description": "Show or hide legend.",
            },
        },
        "required": ["chart_name"],
    }
    doc_types = ["calc"]
    is_mutation = True

    def execute(self, ctx, **kwargs):
        bridge = CalcBridge(ctx.doc)
        manipulator = CellManipulator(bridge)
        chart_name = kwargs["chart_name"]
        updated = manipulator.edit_chart(
            chart_name,
            title=kwargs.get("title"),
            subtitle=kwargs.get("subtitle"),
            has_legend=kwargs.get("has_legend"),
        )
        return {"status": "ok", "chart_name": chart_name, "updated": updated}
class DeleteChart(ToolBase):
    """Delete a chart from a Calc sheet."""

    name = "delete_chart"
    intent = "edit"
    description = "Delete a chart from a Calc sheet by name."
    parameters = {
        "type": "object",
        "properties": {
            "chart_name": {
                "type": "string",
                "description": "Chart name to delete.",
            },
        },
        "required": ["chart_name"],
    }
    doc_types = ["calc"]
    is_mutation = True

    def execute(self, ctx, **kwargs):
        bridge = CalcBridge(ctx.doc)
        manipulator = CellManipulator(bridge)
        chart_name = kwargs["chart_name"]
        if manipulator.delete_chart(chart_name):
            return {"status": "ok", "deleted": chart_name}
        else:
            return self._tool_error(f"Chart '{chart_name}' not found.")
