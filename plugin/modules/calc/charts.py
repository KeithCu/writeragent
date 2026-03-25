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

from plugin.framework.errors import ToolExecutionError, UnoObjectError
from plugin.framework.tool_base import ToolBase
from plugin.modules.calc.address_utils import parse_address
from plugin.modules.calc.bridge import CalcBridge

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
    uno_services = ["com.sun.star.sheet.SpreadsheetDocument"]

    def execute(self, ctx, **kwargs):
        bridge = CalcBridge(ctx.doc)
        try:
            sheet = bridge.get_active_sheet()
            charts = sheet.getCharts()
            result = []
            for name in charts.getElementNames():
                chart_obj = charts.getByName(name)
                entry = {"name": name}
                try:
                    chart_doc = chart_obj.getEmbeddedObject()
                    if chart_doc:
                        try:
                            entry["has_legend"] = chart_doc.HasLegend
                        except Exception as e:
                            logger.debug("list_charts HasLegend error: %s", e)
                            entry["has_legend"] = False
                        try:
                            entry["title"] = chart_doc.getTitle().String if chart_doc.HasMainTitle else ""
                        except Exception as e:
                            logger.debug("list_charts getTitle error: %s", e)
                            entry["title"] = ""
                except Exception as e:
                    logger.debug("list_charts getEmbeddedObject error: %s", e)
                result.append(entry)

            return {
                "status": "ok",
                "charts": result,
                "count": len(result),
            }
        except Exception as e:
            logger.error("List charts error: %s", str(e))
            raise ToolExecutionError(str(e)) from e
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
    uno_services = ["com.sun.star.sheet.SpreadsheetDocument"]

    def execute(self, ctx, **kwargs):
        bridge = CalcBridge(ctx.doc)
        chart_name = kwargs["chart_name"]

        try:
            sheet = bridge.get_active_sheet()
            charts = sheet.getCharts()
            if not charts.hasByName(chart_name):
                return self._tool_error(f"Chart '{chart_name}' not found.")

            chart_obj = charts.getByName(chart_name)
            info = {"name": chart_name, "sheet": sheet.getName()}

            try:
                ranges = chart_obj.getRanges()
                info["data_ranges"] = [bridge._range_to_str(r) for r in ranges]
            except Exception as e:
                logger.debug("get_chart_info getRanges error: %s", e)
                info["data_ranges"] = []

            chart_doc = chart_obj.getEmbeddedObject()
            if chart_doc:
                try:
                    info["title"] = chart_doc.getTitle().String if chart_doc.HasMainTitle else ""
                except Exception as e:
                    logger.debug("get_chart_info getTitle error: %s", e)
                    info["title"] = ""
                try:
                    info["subtitle"] = chart_doc.getSubTitle().String if chart_doc.HasSubTitle else ""
                except Exception as e:
                    logger.debug("get_chart_info getSubTitle error: %s", e)
                    info["subtitle"] = ""
                try:
                    info["has_legend"] = chart_doc.HasLegend
                except Exception as e:
                    logger.debug("get_chart_info HasLegend error: %s", e)
                    info["has_legend"] = None
                try:
                    diagram = chart_doc.getDiagram()
                    info["diagram_type"] = diagram.getDiagramType()
                except Exception as e:
                    logger.debug("get_chart_info getDiagramType error: %s", e)
                    info["diagram_type"] = ""

            info["status"] = "ok"
            return info
        except Exception as e:
            logger.error("Get chart info error: %s", str(e))
            raise ToolExecutionError(str(e)) from e
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
    uno_services = ["com.sun.star.sheet.SpreadsheetDocument"]
    is_mutation = True

    def execute(self, ctx, **kwargs):
        bridge = CalcBridge(ctx.doc)
        data_range = kwargs["data_range"]
        chart_type = kwargs["chart_type"]
        title = kwargs.get("title")
        position = kwargs.get("position")
        has_header = kwargs.get("has_header", True)

        try:
            sheet = bridge.get_active_sheet()
            cell_range = bridge.get_cell_range(sheet, data_range)
            range_address = cell_range.getRangeAddress()

            if position:
                col, row = parse_address(position)
                pos_cell = bridge.get_cell(sheet, col, row)
                pos_x = pos_cell.Position.X
                pos_y = pos_cell.Position.Y
            else:
                pos_x = 10000
                pos_y = 1000

            from com.sun.star.awt import Rectangle

            rect = Rectangle()
            rect.X = pos_x
            rect.Y = pos_y
            rect.Width = 12000
            rect.Height = 8000

            charts = sheet.getCharts()
            chart_name = f"Chart_{len(charts)}"

            type_map = {
                "bar": "com.sun.star.chart.BarDiagram",
                "column": "com.sun.star.chart.BarDiagram",
                "line": "com.sun.star.chart.LineDiagram",
                "pie": "com.sun.star.chart.PieDiagram",
                "scatter": "com.sun.star.chart.XYDiagram",
            }
            chart_service = type_map.get(chart_type, "com.sun.star.chart.BarDiagram")

            charts.addNewByName(
                chart_name, rect, (range_address,), has_header, has_header,
            )

            chart_obj = charts.getByName(chart_name)
            chart_doc = chart_obj.getEmbeddedObject()
            diagram = chart_doc.createInstance(chart_service)
            chart_doc.setDiagram(diagram)

            if chart_type == "bar" and hasattr(diagram, "Vertical"):
                diagram.Vertical = True
            elif chart_type == "column" and hasattr(diagram, "Vertical"):
                diagram.Vertical = False

            if title:
                chart_doc.setPropertyValue("HasMainTitle", True)
                chart_title = chart_doc.getTitle()
                chart_title.setPropertyValue("String", title)

            logger.info("Chart created: %s (%s)", chart_name, chart_type)
            result = f"{chart_type} type chart created as '{chart_name}'."
            return {"status": "ok", "message": result}
        except Exception as e:
            logger.error("Chart creation error: %s", str(e))
            raise ToolExecutionError(str(e)) from e
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
    uno_services = ["com.sun.star.sheet.SpreadsheetDocument"]
    is_mutation = True

    def execute(self, ctx, **kwargs):
        bridge = CalcBridge(ctx.doc)
        chart_name = kwargs["chart_name"]
        title = kwargs.get("title")
        subtitle = kwargs.get("subtitle")
        has_legend = kwargs.get("has_legend")

        try:
            sheet = bridge.get_active_sheet()
            charts = sheet.getCharts()
            if not charts.hasByName(chart_name):
                raise UnoObjectError(f"Chart '{chart_name}' not found.")

            chart_obj = charts.getByName(chart_name)
            chart_doc = chart_obj.getEmbeddedObject()
            if chart_doc is None:
                raise RuntimeError("Cannot access chart document.")

            updated = []
            if title is not None:
                chart_doc.HasMainTitle = True
                title_obj = chart_doc.getTitle()
                title_obj.String = title
                updated.append("title")

            if subtitle is not None:
                chart_doc.HasSubTitle = True
                sub_obj = chart_doc.getSubTitle()
                sub_obj.String = subtitle
                updated.append("subtitle")

            if has_legend is not None:
                chart_doc.HasLegend = has_legend
                updated.append("has_legend")

            return {"status": "ok", "chart_name": chart_name, "updated": updated}
        except Exception as e:
            logger.error("Edit chart error: %s", str(e))
            raise ToolExecutionError(str(e)) from e
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
    uno_services = ["com.sun.star.sheet.SpreadsheetDocument"]
    is_mutation = True

    def execute(self, ctx, **kwargs):
        bridge = CalcBridge(ctx.doc)
        chart_name = kwargs["chart_name"]

        try:
            sheet = bridge.get_active_sheet()
            charts = sheet.getCharts()
            if not charts.hasByName(chart_name):
                return self._tool_error(f"Chart '{chart_name}' not found.")
            charts.removeByName(chart_name)
            return {"status": "ok", "deleted": chart_name}
        except Exception as e:
            logger.error("Delete chart error: %s", str(e))
            raise ToolExecutionError(str(e)) from e
