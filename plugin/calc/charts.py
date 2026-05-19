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
from typing import Any

"""Calc chart management tools: list, info, create, edit, delete.
Enhanced to support Writer and Draw documents, 3D, stacking, and rich properties.
"""

import logging

from plugin.framework.errors import ToolExecutionError
from plugin.framework.tool import ToolBaseDummy
from plugin.calc.base import ToolCalcChartBase
from plugin.calc.bridge import CalcBridge
import uno


def supportsService(obj, service_name: str) -> bool:
    """Helper to check if a UNO object supports a service."""
    if obj is None or not hasattr(obj, "supportsService"):
        return False
    try:
        return obj.supportsService(service_name)
    except Exception:
        return False


logger = logging.getLogger("writeragent.calc")

# Chart CLSID: classic OLE chart (Calc sheet charts, Writer TextEmbeddedObject)
# Uppercase is often more compatible with older OLE registries
CHART_CLSID = "12DCAE36-07DA-43C1-9C17-56A938C64445"
# Draw/Impress OLE2Shape: add shape to page first, then set CLSID (OOo wiki sample)
CHART_CLSID_DRAW_OLE = "12DCAE26-281F-416F-A234-C3086127382E"


def _normalize_clsid_value(clsid: Any) -> str:
    """Coerce UNO CLSID (string, ByteSequence, etc.) to a comparable string."""
    if clsid is None:
        return ""
    if isinstance(clsid, str):
        return clsid
    if isinstance(clsid, (bytes, bytearray)):
        try:
            return clsid.decode("ascii", errors="replace")
        except Exception:
            return ""
    try:
        return str(clsid)
    except Exception:
        return ""


def _is_chart_clsid(clsid: Any) -> bool:
    s = _normalize_clsid_value(clsid).strip()
    if not s:
        return False
    c = s.lower().strip("{}")
    return c in (CHART_CLSID.lower(), CHART_CLSID_DRAW_OLE.lower())


def _writer_embed_is_chart(host: Any) -> bool:
    """True for Writer TextEmbeddedObject charts: CLSID match or embedded chart model."""
    try:
        raw = getattr(host, "CLSID", None)
        if _is_chart_clsid(raw):
            return True
    except Exception:
        pass
    try:
        chart_doc = _chart_document_from_host(host)
        if chart_doc is not None and hasattr(chart_doc, "getDiagram"):
            return chart_doc.getDiagram() is not None
    except Exception:
        pass
    return False


def _chart_document_from_host(host: Any):
    """Chart model from a sheet chart, Writer embed, or Draw/Impress OLE2 shape.
    Handles the extra layer of com.sun.star.embed.XEmbeddedObject for Writer.
    """
    if host is None:
        return None
    
    # 1. Try getEmbeddedObject (Standard for Writer TextEmbeddedObject)
    try:
        if hasattr(host, "getEmbeddedObject"):
            ed = host.getEmbeddedObject()
            logger.debug("Host.getEmbeddedObject() -> %s", ed)
            if ed:
                if hasattr(ed, "Component"):
                    comp = ed.Component
                    if comp is not None:
                        return comp
                return ed
    except Exception as e:
        logger.debug("_chart_document_from_host getEmbeddedObject failed: %s", e)


    # 2. Try Model/Component properties (Standard for Shapes)
    try:
        m = getattr(host, "Model", None)
        if m is not None:
            return m
        c = getattr(host, "Component", None)
        if c is not None:
            return c
    except Exception:
        pass

    # 3. Writer Fallback: If we have a Name, try to find it in the DrawPage
    # In Writer, TextEmbeddedObjects are also exposed as Shapes on the DrawPage
    try:
        name = getattr(host, "Name", None)
        if name and hasattr(host, "getAnchor"): # Likely a Writer object
            # We'll just assume the caller handles the doc-level search if this fails.
            pass
    except Exception:
        pass

    return None


CHART_SERVICE_MAP = {
    "bar": "com.sun.star.chart.BarDiagram",
    "column": "com.sun.star.chart.BarDiagram",
    "line": "com.sun.star.chart.LineDiagram",
    "pie": "com.sun.star.chart.PieDiagram",
    "scatter": "com.sun.star.chart.XYDiagram",
    "area": "com.sun.star.chart.AreaDiagram",
    "donut": "com.sun.star.chart.DonutDiagram",
    "net": "com.sun.star.chart.NetDiagram",
    "stock": "com.sun.star.chart.StockDiagram",
    "bubble": "com.sun.star.chart.BubbleDiagram",
}


def _axis_title_shape_string(shape, value: str | None) -> str | None:
    """Read or write axis title text on a diagram title shape (ChartAxis*Supplier)."""
    if shape is None:
        return None
    if value is not None:
        if hasattr(shape, "String"):
            shape.String = value
        return value
    if hasattr(shape, "String"):
        return shape.String
    return None


def _process_events(ctx=None):
    """Give LO a moment to process UI events and update object names/states."""
    try:
        from plugin.framework.uno_context import get_toolkit, get_ctx
        uctx = ctx or get_ctx()
        if not uctx:
            return
        tk = get_toolkit(uctx)
        if tk and hasattr(tk, "processEventsToIdle"):
            tk.processEventsToIdle()
    except Exception:
        # Avoid letting UI event processing crash the tool
        pass


# Shared parameters for Create and Edit
CHART_PROPERTIES = {
    "data_range": {"type": "string", "description": "Cell range for chart data (Calc only, e.g. 'A1:B10')."},
    "headers": {
        "type": "array",
        "items": {"type": "string"},
        "description": "Category/series column headers (Writer/Draw only, e.g. ['Month', 'Sales', 'Expenses'])."
    },
    "rows": {
        "type": "array",
        "items": {
            "type": "array",
            "description": "Row containing category label as first element, followed by numeric values."
        },
        "description": "2D array of category labels and values (Writer/Draw only, e.g. [['Jan', 100, 80], ['Feb', 150, 110]])."
    },
    "chart_type": {"type": "string", "enum": list(CHART_SERVICE_MAP.keys()), "description": "Type of chart to create."},
    "title": {"type": "string", "description": "Chart title."},
    "is_3d": {"type": "boolean", "description": "Enable 3D mode."},
    "stacked": {"type": "boolean", "description": "Stacked data series."},
    "percent": {"type": "boolean", "description": "Percentage stacked."},
    "x_axis_title": {"type": "string"},
    "y_axis_title": {"type": "string"},
    "legend_position": {"type": "string", "enum": ["none", "top", "bottom", "left", "right"]},
    "has_legend": {"type": "boolean"},
    "subtitle": {"type": "string"},
    "position": {"type": "string", "description": "Cell address (Calc) or anchoring position (Writer/Draw)."},
    "bg_color": {"type": "string", "description": "Chart area background color (hex: #FF0000 or name: green)."},
    "colors": {"type": "array", "items": {"type": "string"}, "description": "List of hex/named colors to apply to each data series."},
}


def _parse_color(color_str):
    if not color_str:
        return None

    # Strip, lowercase, and remove internal spaces/underscores/hyphens for flexible naming
    color_str = color_str.strip().lower()

    # Standard CSS / X11 color map
    color_names = {
        "black": 0x000000, "silver": 0xC0C0C0, "gray": 0x808080, "white": 0xFFFFFF,
        "maroon": 0x800000, "red": 0xFF0000, "purple": 0x800080, "fuchsia": 0xFF00FF,
        "green": 0x008000, "lime": 0x00FF00, "olive": 0x808000, "yellow": 0xFFFF00,
        "navy": 0x000080, "blue": 0x0000FF, "teal": 0x008080, "aqua": 0x00FFFF,
        "cyan": 0x00FFFF, "magenta": 0xFF00FF, "orange": 0xFFA500, "pink": 0xFFC0CB,
        "gold": 0xFFD700, "brown": 0xA52A2A, "violet": 0xEE82EE, "indigo": 0x4B0082,
        "turquoise": 0x40E0D0, "lavender": 0xE6E6FA, "beige": 0xF5F5DC, "salmon": 0xFA8072,
        "olive drab": 0x6B8E23, "olivedrab": 0x6B8E23, "dark green": 0x006400, "darkgreen": 0x006400,
        "dark red": 0x8B0000, "darkred": 0x8B0000, "dark blue": 0x00008B, "darkblue": 0x00008B,
        "light blue": 0xADD8E6, "lightblue": 0xADD8E6, "light green": 0x90EE90, "lightgreen": 0x90EE90,
    }

    # Check map
    norm_name = color_str.replace(" ", "").replace("_", "").replace("-", "")
    for name, val in color_names.items():
        if name.replace(" ", "") == norm_name:
            return val

    # Check RGB(a) format
    import re
    rgb_match = re.match(r"^rgba?\s*\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*(?:,\s*[\d\.]+\s*)?\)$", color_str)
    if rgb_match:
        try:
            r = int(rgb_match.group(1))
            g = int(rgb_match.group(2))
            b = int(rgb_match.group(3))
            if 0 <= r <= 255 and 0 <= g <= 255 and 0 <= b <= 255:
                return (r << 16) | (g << 8) | b
        except ValueError:
            pass

    # Check hex format
    hex_str = color_str.lstrip("#")

    # Handle shorthand hex like "f00" or "#f00"
    if len(hex_str) == 3:
        hex_str = "".join(c * 2 for c in hex_str)

    if len(hex_str) == 6:
        try:
            return int(hex_str, 16)
        except ValueError:
            return None

    return None



def _apply_chart_styling(chart_doc, **kwargs):
    """Apply enhanced styling properties to a chart document."""
    logger.info("Applying chart styling with kwargs: %s", {k: v for k, v in kwargs.items() if k not in ["data_range"]})
    diagram = chart_doc.getDiagram()
    if not diagram:
        logger.warning("No diagram found on chart document.")
        return

    # 1. 3D Mode
    is_3d = kwargs.get("is_3d")
    if is_3d is not None and hasattr(diagram, "Dim3D"):
        diagram.Dim3D = is_3d
        logger.debug("Set diagram 3D mode: %s", is_3d)

    # 2. Stacking
    stacked = kwargs.get("stacked")
    if stacked is not None and hasattr(diagram, "Stacked"):
        diagram.Stacked = stacked
        logger.debug("Set diagram stacked mode: %s", stacked)

    percent = kwargs.get("percent")
    if percent is not None and hasattr(diagram, "Percent"):
        diagram.Percent = percent
        logger.debug("Set diagram percent stacked: %s", percent)

    # 3. Bar/Column Orientation
    chart_type = kwargs.get("chart_type")
    if chart_type in ["bar", "column"] and hasattr(diagram, "Vertical"):
        diagram.Vertical = chart_type == "bar"
        logger.debug("Set diagram orientation: vertical=%s", diagram.Vertical)

    # 4. Titles
    title = kwargs.get("title")
    if title is not None:
        chart_doc.HasMainTitle = True
        chart_doc.getTitle().String = title
        logger.debug("Set chart main title: '%s'", title)

    subtitle = kwargs.get("subtitle")
    if subtitle is not None:
        chart_doc.HasSubTitle = True
        chart_doc.getSubTitle().String = subtitle
        logger.debug("Set chart subtitle: '%s'", subtitle)

    x_axis_title = kwargs.get("x_axis_title")
    if x_axis_title is not None and hasattr(diagram, "HasXAxisTitle"):
        diagram.HasXAxisTitle = True
        try:
            _axis_title_shape_string(diagram.getXAxisTitle(), x_axis_title)
            logger.debug("Set X axis title: '%s'", x_axis_title)
        except Exception:
            logger.exception("Setting X axis title failed")

    y_axis_title = kwargs.get("y_axis_title")
    if y_axis_title is not None and hasattr(diagram, "HasYAxisTitle"):
        diagram.HasYAxisTitle = True
        try:
            _axis_title_shape_string(diagram.getYAxisTitle(), y_axis_title)
            logger.debug("Set Y axis title: '%s'", y_axis_title)
        except Exception:
            logger.exception("Setting Y axis title failed")

    # 5. Legend
    has_legend = kwargs.get("has_legend")
    if has_legend is not None:
        chart_doc.HasLegend = has_legend
        logger.debug("Set chart legend visibility: %s", has_legend)

    legend_pos = kwargs.get("legend_position")
    if legend_pos and chart_doc.HasLegend:
        try:
            pos_map = {
                "none": None,
                "top": uno.Enum("com.sun.star.chart.ChartLegendAlignment", "TOP"),
                "bottom": uno.Enum("com.sun.star.chart.ChartLegendAlignment", "BOTTOM"),
                "left": uno.Enum("com.sun.star.chart.ChartLegendAlignment", "LEFT"),
                "right": uno.Enum("com.sun.star.chart.ChartLegendAlignment", "RIGHT"),
            }
            if legend_pos in pos_map:
                if legend_pos == "none":
                    chart_doc.HasLegend = False
                else:
                    chart_doc.getLegend().Alignment = pos_map[legend_pos]
                logger.debug("Set legend position: %s", legend_pos)
        except (ImportError, AttributeError):
            logger.exception("ChartLegendAlignment enum not available")

    # 6. Background Color
    bg_color = kwargs.get("bg_color")
    if bg_color:
        parsed_bg = _parse_color(bg_color)
        if parsed_bg is not None:
            try:
                bg = chart_doc.getPageBackground()
                bg.setPropertyValue("FillStyle", uno.Enum("com.sun.star.drawing.FillStyle", "SOLID"))
                bg.setPropertyValue("FillColor", parsed_bg)
                logger.info("Set chart background color: %s (RGB %d)", bg_color, parsed_bg)
            except Exception:
                logger.exception("Failed to set chart background color")
        else:
            logger.warning("Invalid bg_color ignored: '%s'", bg_color)

    # 7. Series Colors (one color per series/bar in the chart)
    colors = kwargs.get("colors")
    if colors:
        parsed_colors = []
        for c in colors:
            parsed = _parse_color(c)
            if parsed is not None:
                parsed_colors.append(parsed)

        if parsed_colors:
            try:
                diag = chart_doc.getFirstDiagram()
                if diag:
                    coords = diag.getCoordinateSystems()
                    series_count = 0
                    for coord in coords:
                        ctypes = coord.getChartTypes()
                        for ctype in ctypes:
                            series_list = ctype.getDataSeries()
                            for idx, s in enumerate(series_list):
                                color_val = parsed_colors[idx % len(parsed_colors)]
                                for prop in ["Color", "FillColor", "LineColor"]:
                                    if s.getPropertySetInfo().hasPropertyByName(prop):
                                        s.setPropertyValue(prop, color_val)
                                logger.info("Set data series %d color to RGB %d", idx, color_val)
                                series_count += 1
                    logger.info("Successfully styled %d chart data series with colors %s", series_count, colors)
                else:
                    logger.warning("Could not retrieve first diagram using getFirstDiagram")
            except Exception:
                logger.exception("Failed to set data series colors")

    # 8. Programmatic Data Arrays (Writer/Draw)
    headers = kwargs.get("headers")
    rows = kwargs.get("rows")
    if headers and rows:
        _apply_chart_data_arrays(chart_doc, headers, rows)


def _apply_chart_data_arrays(chart_doc, headers, rows):
    """Set chart data programmatically via XChartDataArray for Writer/Draw."""
    if not headers or not rows:
        return

    try:
        chart_data = chart_doc.getData()
        if not chart_data:
            logger.warning("No chart data object found on chart document.")
            return

        # 1. Process rows to extract categories (row descriptions) and numeric matrix values
        row_desc = []
        data_values = []
        for r in rows:
            if not r:
                continue
            row_desc.append(str(r[0]))
            # Convert values to float; fallback to 0.0 if not numeric
            vals = []
            for v in r[1:]:
                try:
                    vals.append(float(v))
                except (ValueError, TypeError):
                    vals.append(0.0)
            data_values.append(tuple(vals))

        # 2. Process headers to extract series names (column descriptions)
        col_desc = tuple(str(h) for h in headers[1:])

        # Ensure all rows have the same number of data points
        expected_len = len(col_desc)
        final_values = []
        for row_vals in data_values:
            if len(row_vals) < expected_len:
                row_vals = row_vals + (0.0,) * (expected_len - len(row_vals))
            elif len(row_vals) > expected_len:
                row_vals = row_vals[:expected_len]
            final_values.append(row_vals)

        # 3. Apply to chart_data
        chart_data.setRowDescriptions(tuple(row_desc))
        chart_data.setColumnDescriptions(col_desc)
        chart_data.setData(tuple(final_values))
        logger.info("Successfully applied chart data arrays: row_desc=%s, col_desc=%s, data=%s", row_desc, col_desc, final_values)

    except Exception as e:
        logger.exception("Failed to apply chart data arrays: %s", e)



def _resolve_chart(doc, chart_name):
    """Resolve a chart object by name across Calc, Writer, or Draw."""
    if supportsService(doc, "com.sun.star.sheet.SpreadsheetDocument"):
        bridge = CalcBridge(doc)
        sheet = bridge.get_active_sheet()
        charts = sheet.getCharts()
        if charts.hasByName(chart_name):
            return charts.getByName(chart_name)
    elif supportsService(doc, "com.sun.star.text.TextDocument"):
        objects = doc.getEmbeddedObjects()
        if objects.hasByName(chart_name):
            return objects.getByName(chart_name)
        try:
            page = doc.getDrawPage()
            for j in range(page.getCount()):
                shape = page.getByIndex(j)
                if shape.getShapeType() == "com.sun.star.drawing.OLE2Shape":
                    if (shape.Name or "") == chart_name:
                        return shape
        except Exception:
            pass
    elif supportsService(doc, "com.sun.star.drawing.DrawingDocument") or supportsService(doc, "com.sun.star.presentation.PresentationDocument"):
        # Iterate all pages and shapes
        for i in range(doc.getDrawPages().getCount()):
            page = doc.getDrawPages().getByIndex(i)
            for j in range(page.getCount()):
                shape = page.getByIndex(j)
                if shape.getShapeType() == "com.sun.star.drawing.OLE2Shape":
                    if shape.Name == chart_name:
                        return shape
    return None


class ListCharts(ToolBaseDummy):
    """List all charts on a sheet, document, or slide."""

    name = "list_charts"
    intent = "navigate"
    description = "List all charts in the current context (active sheet, document, or slide) with name, title, and type."
    parameters = {"type": "object", "properties": {}, "required": []}
    uno_services = ["com.sun.star.sheet.SpreadsheetDocument", "com.sun.star.text.TextDocument", "com.sun.star.drawing.DrawingDocument", "com.sun.star.presentation.PresentationDocument"]

    def execute(self, ctx, **kwargs):
        doc = ctx.doc
        result = []

        if supportsService(doc, "com.sun.star.sheet.SpreadsheetDocument"):
            bridge = CalcBridge(doc)
            sheet = bridge.get_active_sheet()
            charts = sheet.getCharts()
            for name in charts.getElementNames():
                chart_obj = charts.getByName(name)
                result.append(self._get_summary(chart_obj, name))

        elif supportsService(doc, "com.sun.star.text.TextDocument"):
            objects = doc.getEmbeddedObjects()
            for name in objects.getElementNames():
                obj = objects.getByName(name)
                if _writer_embed_is_chart(obj):
                    result.append(self._get_summary(obj, name))
            try:
                page = doc.getDrawPage()
                for j in range(page.getCount()):
                    shape = page.getByIndex(j)
                    if shape.getShapeType() == "com.sun.star.drawing.OLE2Shape":
                        if _is_chart_clsid(getattr(shape, "CLSID", "") or ""):
                            nm = shape.Name or f"Chart_{j}"
                            result.append(self._get_summary(shape, nm))
            except Exception:
                pass

        elif supportsService(doc, "com.sun.star.drawing.DrawingDocument") or supportsService(doc, "com.sun.star.presentation.PresentationDocument"):
            for i in range(doc.getDrawPages().getCount()):
                page = doc.getDrawPages().getByIndex(i)
                for j in range(page.getCount()):
                    shape = page.getByIndex(j)
                    if shape.getShapeType() == "com.sun.star.drawing.OLE2Shape":
                        if _is_chart_clsid(getattr(shape, "CLSID", "") or ""):
                            result.append(self._get_summary(shape, shape.Name or f"Chart_{i}_{j}"))

        return {"status": "ok", "charts": result, "count": len(result)}

    def _get_summary(self, chart_obj, name):
        entry = {"name": name}
        try:
            chart_doc = _chart_document_from_host(chart_obj)
            if chart_doc:
                entry["title"] = chart_doc.getTitle().String if chart_doc.HasMainTitle else ""
                entry["diagram_type"] = chart_doc.getDiagram().getDiagramType()
        except Exception:
            pass
        return entry


class GetChartInfo(ToolBaseDummy):
    """Get detailed info about a chart."""

    name = "get_chart_info"
    intent = "navigate"
    description = "Get detailed info about a chart: type, title, ranges (if Calc), axis titles, and legend properties."
    parameters = {"type": "object", "properties": {"chart_name": {"type": "string", "description": "Chart name (from list_charts)."}}, "required": ["chart_name"]}
    uno_services = ListCharts.uno_services

    def execute(self, ctx, **kwargs):
        doc = ctx.doc
        chart_name = kwargs["chart_name"]
        chart_obj = _resolve_chart(doc, chart_name)

        if not chart_obj:
            return self._tool_error(f"Chart '{chart_name}' not found.")

        info = {"name": chart_name, "status": "ok"}

        # Data ranges (Calc only)
        if hasattr(chart_obj, "getRanges"):
            bridge = CalcBridge(doc)
            try:
                info["data_ranges"] = [bridge._range_to_str(r) for r in chart_obj.getRanges()]
            except Exception:
                info["data_ranges"] = []

        try:
            chart_doc = _chart_document_from_host(chart_obj)
            if chart_doc:
                info["title"] = chart_doc.getTitle().String if chart_doc.HasMainTitle else ""
                info["subtitle"] = chart_doc.getSubTitle().String if chart_doc.HasSubTitle else ""
                info["has_legend"] = chart_doc.HasLegend

                diagram = chart_doc.getDiagram()
                if diagram:
                    info["diagram_type"] = diagram.getDiagramType()
                    info["is_3d"] = getattr(diagram, "Dim3D", None)
                    info["stacked"] = getattr(diagram, "Stacked", None)
                    info["percent"] = getattr(diagram, "Percent", None)

                    if hasattr(diagram, "HasXAxisTitle") and diagram.HasXAxisTitle:
                        try:
                            xs = _axis_title_shape_string(diagram.getXAxisTitle(), None)
                            if xs is not None:
                                info["x_axis_title"] = xs
                        except Exception:
                            pass
                    if hasattr(diagram, "HasYAxisTitle") and diagram.HasYAxisTitle:
                        try:
                            ys = _axis_title_shape_string(diagram.getYAxisTitle(), None)
                            if ys is not None:
                                info["y_axis_title"] = ys
                        except Exception:
                            pass

        except Exception as e:
            logger.debug("get_chart_info error: %s", e)

        return info


class CreateChart(ToolBaseDummy):
    """Create a new chart."""

    name = "create_chart"
    intent = "edit"
    description = "Creates a chart in the current context. In Calc, data_range is required. In Writer/Impress, a chart is inserted at the cursor or on the active slide."
    parameters = {"type": "object", "properties": CHART_PROPERTIES, "required": ["chart_type"]}
    uno_services = ListCharts.uno_services
    is_mutation = True

    def get_parameters(self, doc_type: str | None = None) -> dict | None:
        import copy
        from typing import cast
        params = copy.deepcopy(self.parameters)
        if not params or "properties" not in params:
            return params
        properties = cast("dict[str, Any]", params["properties"])
        if doc_type == "calc":
            properties.pop("headers", None)
            properties.pop("rows", None)
            if "required" in params:
                required = cast("list[Any]", params["required"])
                if "data_range" not in required:
                    required.append("data_range")
        elif doc_type in ("writer", "draw"):
            properties.pop("data_range", None)
            if "required" in params:
                required = cast("list[Any]", params["required"])
                if "headers" not in required:
                    required.append("headers")
                if "rows" not in required:
                    required.append("rows")
        return params

    def execute(self, ctx, **kwargs):
        doc = ctx.doc
        is_calc = supportsService(doc, "com.sun.star.sheet.SpreadsheetDocument")
        
        if is_calc:
            if "headers" in kwargs or "rows" in kwargs:
                return self._tool_error("Data arrays ('headers', 'rows') are not supported in Calc. Please use 'data_range' instead.")
            if not kwargs.get("data_range"):
                return self._tool_error("data_range is required for Calc charts.")
        else:
            if "data_range" in kwargs:
                return self._tool_error("Parameter 'data_range' is only supported in Calc. For Writer/Draw, please use 'headers' and 'rows' to pass chart data.")
            if not kwargs.get("headers") or not kwargs.get("rows"):
                return self._tool_error("Both 'headers' and 'rows' are required to create a chart in Writer or Draw/Impress.")

        chart_type = kwargs["chart_type"]
        chart_service = CHART_SERVICE_MAP.get(chart_type, CHART_SERVICE_MAP["column"])

        rect = uno.createUnoStruct("com.sun.star.awt.Rectangle", X=1000, Y=1000, Width=12000, Height=8000)

        try:
            if supportsService(doc, "com.sun.star.sheet.SpreadsheetDocument"):
                return self._create_calc_chart(ctx, rect, chart_service, **kwargs)
            elif supportsService(doc, "com.sun.star.text.TextDocument"):
                return self._create_writer_chart(ctx, rect, chart_service, **kwargs)
            elif supportsService(doc, "com.sun.star.presentation.PresentationDocument") or supportsService(doc, "com.sun.star.drawing.DrawingDocument"):
                return self._create_draw_chart(ctx, rect, chart_service, **kwargs)

            return self._tool_error("Unsupported document type for chart creation.")
        except Exception as e:
            msg = f"{type(e).__name__}: {str(e)}"
            logger.error("Chart creation error: %s", msg)
            raise ToolExecutionError(f"Tool execution failed: {msg}") from e

    def _create_calc_chart(self, ctx, rect, service, **kwargs):
        bridge = CalcBridge(ctx.doc)
        data_range = kwargs.get("data_range")
        if not data_range:
            return self._tool_error("data_range is required for Calc charts.")

        sheet = bridge.get_active_sheet()
        cell_range = bridge.get_cell_range(sheet, data_range)
        addr = cell_range.getRangeAddress()

        logger.debug("Creating Calc chart: name=Chart_N, rect=(%d,%d,%d,%d), range=(%d,%d,%d,%d)", rect.X, rect.Y, rect.Width, rect.Height, addr.StartColumn, addr.StartRow, addr.EndColumn, addr.EndRow)

        charts = sheet.getCharts()
        name = f"Chart_{len(charts)}"

        # Ensure name is unique
        try:
            while charts.hasByName(name):
                name = f"{name}_new"
        except Exception:
            pass

        charts.addNewByName(name, rect, (addr,), True, True)

        chart_obj = charts.getByName(name)
        chart_doc = _chart_document_from_host(chart_obj)
        if not chart_doc:
            return self._tool_error("Cannot access chart content.")
        chart_doc.setDiagram(chart_doc.createInstance(service))

        _apply_chart_styling(chart_doc, **kwargs)
        #_process_events() causes a hang in tests
        return {"status": "ok", "message": f"Chart '{name}' created in Calc.", "chart_name": name}

    def _create_writer_chart(self, ctx, rect, service, **kwargs):
        """Insert a chart as inline ``TextEmbeddedObject`` (Writer body text).
        Using a retry loop and event pumping to ensure the embedded model is initialized.
        """
        import time
        doc = ctx.doc
        text = doc.getText()
        logger.info("Creating Writer chart. Current text length: %d", len(text.getString()))

        # 1. Resolve cursor position
        try:
            pos = kwargs.get("position", "end")
            if pos == "cursor":
                controller = doc.getCurrentController()
                if hasattr(controller, "getViewCursor"):
                    vc = controller.getViewCursor()
                    cursor = text.createTextCursorByRange(vc.getStart())
                else:
                    cursor = text.createTextCursorByRange(text.getEnd())
            else:
                cursor = text.createTextCursorByRange(text.getEnd())
        except Exception:
            cursor = text.createTextCursor()
            try:
                cursor.gotoEnd(False)
            except Exception:
                pass

        # 2. Create and configure TextEmbeddedObject
        name = f"Chart_{len(doc.getEmbeddedObjects())}"
        try:
            # We use plain createInstance for Writer; createInstanceWithArguments can be flaky for OLE
            chart_obj = doc.createInstance("com.sun.star.text.TextEmbeddedObject")
            if not chart_obj:
                 return self._tool_error("Failed to create TextEmbeddedObject instance.")
            
            try:
                logger.debug("TextEmbeddedObject Implementation: %s", chart_obj.getImplementationName())
            except Exception:
                pass

            # CRITICAL: Match proven working pattern from plugin/writer/math/math_mml_convert.py
            chart_obj.CLSID = CHART_CLSID.upper()
            from com.sun.star.text.TextContentAnchorType import AS_CHARACTER
            chart_obj.AnchorType = AS_CHARACTER
            
            # Try to set name before insertion
            try:
                chart_obj.Name = name
            except Exception:
                pass
            
            logger.info("Created and configured TextEmbeddedObject with CLSID: %s", chart_obj.CLSID)
        except Exception as e:
            logger.debug("Creation/config failed: %s", e)
            return self._tool_error(f"Failed to configure chart object: {e}")

        # 3. Insert into document
        try:
            # Ensure we are at a valid insertion point if the doc is empty
            if text.getString() == "":
                try:
                    PARAGRAPH_BREAK = uno.getConstantByName("com.sun.star.text.ControlCharacter.PARAGRAPH_BREAK")
                    text.insertControlCharacter(cursor, PARAGRAPH_BREAK, False)
                except Exception:
                    pass

            text.insertTextContent(cursor, chart_obj, False)
            logger.info("Successfully inserted chart object into text.")
        except Exception as e:
            # Fallback 1: Try AT_PARAGRAPH
            logger.debug("First insertion attempt failed (%s). Trying AT_PARAGRAPH anchor...", e)
            try:
                from com.sun.star.text.TextContentAnchorType import AT_PARAGRAPH
                chart_obj.AnchorType = AT_PARAGRAPH
                text.insertTextContent(cursor, chart_obj, False)
                logger.info("Successfully inserted chart object with AT_PARAGRAPH.")
            except Exception:
                # Fallback 2: Try CHART_CLSID_DRAW_OLE
                logger.debug("Second insertion attempt failed. Trying DRAW_OLE CLSID...")
                try:
                    chart_obj.CLSID = CHART_CLSID_DRAW_OLE.upper()
                    text.insertTextContent(cursor, chart_obj, False)
                    logger.info("Successfully inserted chart object with DRAW_OLE CLSID.")
                except Exception as e3:
                    logger.error("All insertion attempts failed for chart object: %s", e3)
                    return self._tool_error(f"Failed to insert chart into document: {e3}")

        # 4. Configure properties after insertion
        # Try to set name again if it failed before
        try:
            chart_obj.Name = name
        except Exception:
            pass

        try:
            from com.sun.star.text.TextContentAnchorType import AS_CHARACTER, AT_PARAGRAPH

            # If we didn't already set AT_PARAGRAPH in the catch block, try setting AS_CHARACTER now
            if chart_obj.AnchorType != AT_PARAGRAPH:
                chart_obj.AnchorType = AS_CHARACTER
                logger.debug("Set AnchorType to AS_CHARACTER (post-insertion)")
        except Exception as e:
            logger.debug("Failed to set AnchorType post-insertion: %s", e)

        try:
            chart_obj.setPropertyValue("Width", rect.Width)
            chart_obj.setPropertyValue("Height", rect.Height)
            logger.debug("Set size post-insertion: %dx%d", rect.Width, rect.Height)
        except Exception:
            try:
                chart_obj.Width = rect.Width
                chart_obj.Height = rect.Height
            except Exception:
                pass

        # 5. Wait for model initialization
        chart_doc = None
        for i in range(10):
            chart_doc = _chart_document_from_host(chart_obj)
            if chart_doc:
                logger.info("Obtained chart model on attempt %d", i + 1)
                break
            logger.debug("Model missing on attempt %d, pumping events...", i + 1)
            _process_events(ctx.ctx)
            time.sleep(0.05)

        if not chart_doc:
            # Last ditch effort: find it in the collection
            try:
                objects = doc.getEmbeddedObjects()
                logger.debug("Final attempt: checking EmbeddedObjects collection (count=%d)", objects.getCount())
                if objects.hasByName(name):
                    obj = objects.getByName(name)
                    logger.debug("Found object in collection by name. Type: %s", type(obj))
                    chart_doc = _chart_document_from_host(obj)
            except Exception as e:
                logger.debug("Last ditch effort failed: %s", e)
                pass

        # 5. Configure Diagram
        if chart_doc:
            try:
                diagram = chart_doc.createInstance(service)
                if diagram:
                    chart_doc.setDiagram(diagram)
                    logger.info("Set chart diagram: %s", service)
                else:
                    logger.error("Failed to create diagram instance for service: %s", service)
            except Exception as e:
                logger.error("Failed to set chart diagram: %s", e)

            _apply_chart_styling(chart_doc, **kwargs)
        else:
            logger.error("Could not obtain chart model after retries. Chart might be empty/invisible.")

        _process_events()
        return {"status": "ok", "message": f"Chart '{name}' inserted in Writer.", "chart_name": name}

    def _create_draw_chart(self, ctx, rect, service, **kwargs):
        doc = ctx.doc
        controller = doc.getCurrentController()
        page = None
        if controller is not None and hasattr(controller, "getCurrentPage"):
            try:
                page = controller.getCurrentPage()
            except Exception:
                page = None
        if page is None and doc.getDrawPages().getCount() > 0:
            page = doc.getDrawPages().getByIndex(0)
        if page is None:
            return self._tool_error("No draw page or slide to insert chart.")

        # Draw/Impress: add OLE2 shape first, then CLSID (chart2 OLE GUID); chart lives on .Model
        shape = doc.createInstance("com.sun.star.drawing.OLE2Shape")
        page.add(shape)
        try:
            shape.setSize(uno.createUnoStruct("com.sun.star.awt.Size", Width=rect.Width, Height=rect.Height))
            shape.setPosition(uno.createUnoStruct("com.sun.star.awt.Point", X=rect.X, Y=rect.Y))
        except Exception as e:
            logger.debug("Failed to set Draw shape size/pos: %s", e)
        shape.CLSID = CHART_CLSID_DRAW_OLE

        name = f"Chart_{page.getCount()}"
        shape.Name = name

        chart_doc = _chart_document_from_host(shape)
        if chart_doc:
            chart_doc.setDiagram(chart_doc.createInstance(service))
            _apply_chart_styling(chart_doc, **kwargs)

        _process_events()
        return {"status": "ok", "message": f"Chart '{name}' inserted on slide.", "chart_name": name}


class EditChart(ToolBaseDummy):
    """Modify chart properties."""

    name = "edit_chart"
    intent = "edit"
    description = "Edit a chart's properties: title, 3D mode, stacking, legend, axes, etc."
    parameters = {"type": "object", "properties": {**CHART_PROPERTIES, "chart_name": {"type": "string", "description": "Name of the chart to edit."}}, "required": ["chart_name"]}
    uno_services = ListCharts.uno_services
    is_mutation = True

    def get_parameters(self, doc_type: str | None = None) -> dict | None:
        import copy
        from typing import cast
        params = copy.deepcopy(self.parameters)
        if not params or "properties" not in params:
            return params
        properties = cast("dict[str, Any]", params["properties"])
        if doc_type == "calc":
            properties.pop("headers", None)
            properties.pop("rows", None)
        elif doc_type in ("writer", "draw"):
            properties.pop("data_range", None)
        return params

    def execute(self, ctx, **kwargs):
        doc = ctx.doc
        is_calc = supportsService(doc, "com.sun.star.sheet.SpreadsheetDocument")
        
        if is_calc:
            if "headers" in kwargs or "rows" in kwargs:
                return self._tool_error("Data arrays ('headers', 'rows') are not supported in Calc. Please use 'data_range' instead.")
        else:
            if "data_range" in kwargs:
                return self._tool_error("Parameter 'data_range' is only supported in Calc. For Writer/Draw, please use 'headers' and 'rows' to pass chart data.")

        chart_name = kwargs["chart_name"]
        chart_obj = _resolve_chart(doc, chart_name)

        if not chart_obj:
            return self._tool_error(f"Chart '{chart_name}' not found.")

        chart_doc = _chart_document_from_host(chart_obj)
        if not chart_doc:
            return self._tool_error("Cannot access chart content.")

        # If chart_type is provided, update the diagram first
        chart_type = kwargs.get("chart_type")
        if chart_type:
            service = CHART_SERVICE_MAP.get(chart_type)
            if service:
                chart_doc.setDiagram(chart_doc.createInstance(service))

        _apply_chart_styling(chart_doc, **kwargs)

        return {"status": "ok", "chart_name": chart_name, "message": "Chart updated."}


class DeleteChart(ToolBaseDummy):
    """Delete a chart."""

    name = "delete_chart"
    intent = "edit"
    description = "Delete a chart by name."
    parameters = {"type": "object", "properties": {"chart_name": {"type": "string", "description": "Chart name to delete."}}, "required": ["chart_name"]}
    uno_services = ListCharts.uno_services
    is_mutation = True

    def execute(self, ctx, **kwargs):
        doc = ctx.doc
        chart_name = kwargs["chart_name"]

        if supportsService(doc, "com.sun.star.sheet.SpreadsheetDocument"):
            bridge = CalcBridge(doc)
            sheet = bridge.get_active_sheet()
            charts = sheet.getCharts()
            if not charts.hasByName(chart_name):
                return self._tool_error(f"Chart '{chart_name}' not found.")
            charts.removeByName(chart_name)
        elif supportsService(doc, "com.sun.star.text.TextDocument"):
            objects = doc.getEmbeddedObjects()
            if objects.hasByName(chart_name):
                objects.removeByName(chart_name)
                return {"status": "ok", "deleted": chart_name}
            try:
                page = doc.getDrawPage()
                for j in range(page.getCount()):
                    shape = page.getByIndex(j)
                    if shape.getShapeType() == "com.sun.star.drawing.OLE2Shape" and shape.Name == chart_name:
                        page.remove(shape)
                        return {"status": "ok", "deleted": chart_name}
            except Exception:
                pass
            return self._tool_error(f"Chart '{chart_name}' not found.")
        else:
            # Draw/Impress: find shape and remove from page
            for i in range(doc.getDrawPages().getCount()):
                page = doc.getDrawPages().getByIndex(i)
                for j in range(page.getCount()):
                    shape = page.getByIndex(j)
                    if shape.getShapeType() == "com.sun.star.drawing.OLE2Shape" and shape.Name == chart_name:
                        page.remove(shape)
                        return {"status": "ok", "deleted": chart_name}
            return self._tool_error(f"Chart '{chart_name}' not found.")

        return {"status": "ok", "deleted": chart_name}


class ManageCharts(ToolCalcChartBase):
    """Manage charts: list, get_info, create, edit, or delete in the current context.

    Calc/Writer/Draw each register ``ManageCharts`` with their chart specialized base (like ``upsert_shape`` in
    shapes); ``ToolRegistry`` keeps one instance per name (last module load wins). Writer/Draw set union
    ``uno_services`` so registration order does not drop other document types. Legacy ``list_charts`` etc. stay
    ``ToolBaseDummy`` (disabled in favor of this tool).

    Future (per-app tiers and a growing API, e.g. full 3D):
    - Registry: store ``list[ToolBase]`` per name and resolve via ``supportsService`` instead of last-wins.
    - Or ``get_tier(doc_type)`` on a single class if multi-bind is too heavy.
    - Schema: ``get_parameters(doc_type)`` / ``get_description(doc_type)`` so Calc keeps ``data_range`` while
      Writer/Draw omit it; add a 3D block (view angle, perspective, wall/floor) when UNO paths beyond ``is_3d`` exist.
    - Sub-agent: richer ``required_core_tools`` or a domain preamble when the consolidated schema grows.
    """

    name = "manage_charts"
    intent = "edit"
    description = "Manage charts: list, get_info, create, edit, or delete a chart in the current context (active sheet, document, or slide)."
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["list", "get_info", "create", "edit", "delete"],
                "description": "The action to perform on the charts."
            },
            "chart_name": {
                "type": "string",
                "description": "The name of the chart (required for get_info, edit, delete)."
            },
            "data_range": {
                "type": "string",
                "description": "Cell range for chart data (Calc only, required for create, e.g. 'A1:B10')."
            },
            "headers": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Category/series column headers (Writer/Draw only, required for create, e.g. ['Month', 'Sales', 'Expenses'])."
            },
            "rows": {
                "type": "array",
                "items": {
                    "type": "array",
                    "description": "Row containing category label as first element, followed by numeric values."
                },
                "description": "2D array of category labels and values (Writer/Draw only, required for create, e.g. [['Jan', 100, 80], ['Feb', 150, 110]])."
            },
            "chart_type": {
                "type": "string",
                "enum": ["bar", "pie", "column", "line", "scatter", "area", "donut", "net", "stock", "bubble"],
                "description": "Type of chart to create or update to (required for create)."
            },
            "title": {
                "type": "string",
                "description": "Chart title."
            },
            "subtitle": {
                "type": "string",
                "description": "Chart subtitle."
            },
            "is_3d": {
                "type": "boolean",
                "description": "Enable 3D mode."
            },
            "stacked": {
                "type": "boolean",
                "description": "Stacked data series."
            },
            "percent": {
                "type": "boolean",
                "description": "Percentage stacked."
            },
            "x_axis_title": {
                "type": "string",
                "description": "Title for X axis."
            },
            "y_axis_title": {
                "type": "string",
                "description": "Title for Y axis."
            },
            "legend_position": {
                "type": "string",
                "enum": ["none", "top", "bottom", "left", "right"],
                "description": "Legend position."
            },
            "has_legend": {
                "type": "boolean",
                "description": "Whether the chart has a legend."
            },
            "position": {
                "type": "string",
                "description": "Cell address (Calc) or anchoring position (Writer/Draw)."
            },
            "bg_color": {
                "type": "string",
                "description": "Chart area background color (hex: #FF0000 or name: green)."
            },
            "colors": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of hex/named colors to apply to each data series."
            }
        },
        "required": ["action"]
    }
    uno_services = [
        "com.sun.star.sheet.SpreadsheetDocument",
        "com.sun.star.text.TextDocument",
        "com.sun.star.drawing.DrawingDocument",
        "com.sun.star.presentation.PresentationDocument"
    ]
    is_mutation = True

    def get_parameters(self, doc_type: str | None = None) -> dict | None:
        import copy
        from typing import cast
        params = copy.deepcopy(self.parameters)
        if not params or "properties" not in params:
            return params
        properties = cast("dict[str, Any]", params["properties"])
        if doc_type == "calc":
            properties.pop("headers", None)
            properties.pop("rows", None)
        elif doc_type in ("writer", "draw"):
            properties.pop("data_range", None)
        return params

    def execute(self, ctx, **kwargs):
        action = kwargs.get("action")
        if not action:
            raise ToolExecutionError("Action parameter is required.")

        if action == "list":
            return ListCharts().execute(ctx, **kwargs)
        elif action == "get_info":
            if "chart_name" not in kwargs:
                return self._tool_error("chart_name parameter is required for action='get_info'.")
            return GetChartInfo().execute(ctx, **kwargs)
        elif action == "create":
            if "chart_type" not in kwargs:
                return self._tool_error("chart_type parameter is required for action='create'.")
            return CreateChart().execute(ctx, **kwargs)
        elif action == "edit":
            if "chart_name" not in kwargs:
                return self._tool_error("chart_name parameter is required for action='edit'.")
            return EditChart().execute(ctx, **kwargs)
        elif action == "delete":
            if "chart_name" not in kwargs:
                return self._tool_error("chart_name parameter is required for action='delete'.")
            return DeleteChart().execute(ctx, **kwargs)
        else:
            return self._tool_error(f"Unsupported action: '{action}'")

