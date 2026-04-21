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
from plugin.framework.tool_base import ToolBase
from plugin.modules.calc.bridge import CalcBridge
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
CHART_CLSID = "12dcae36-07da-43c1-9c17-56a938c64445"
# Draw/Impress OLE2Shape: add shape to page first, then set CLSID (OOo wiki sample)
CHART_CLSID_DRAW_OLE = "12dcae26-281f-416f-a234-c3086127382e"


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
    """Chart model from a sheet chart, Writer embed, or Draw/Impress OLE2 shape."""
    if host is None:
        return None
    try:
        if hasattr(host, "getEmbeddedObject"):
            ed = host.getEmbeddedObject()
            if ed:
                return ed
    except Exception:
        pass
    return getattr(host, "Model", None)

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
        tk = get_toolkit(ctx or get_ctx())
        if tk:
            tk.processEventsToIdle()
    except Exception:
        pass

# Shared parameters for Create and Edit
CHART_PROPERTIES = {
    "data_range": {
        "type": "string",
        "description": "Cell range for chart data (Calc only, e.g. 'A1:B10').",
    },
    "chart_type": {
        "type": "string",
        "enum": list(CHART_SERVICE_MAP.keys()),
        "description": "Type of chart to create.",
    },
    "title": {
        "type": "string",
        "description": "Chart title.",
    },
    "is_3d": {"type": "boolean", "description": "Enable 3D mode."},
    "stacked": {"type": "boolean", "description": "Stacked data series."},
    "percent": {"type": "boolean", "description": "Percentage stacked."},
    "x_axis_title": {"type": "string"},
    "y_axis_title": {"type": "string"},
    "legend_position": {
        "type": "string",
        "enum": ["none", "top", "bottom", "left", "right"],
    },
    "has_legend": {"type": "boolean"},
    "subtitle": {"type": "string"},
    "position": {
        "type": "string",
        "description": "Cell address (Calc) or anchoring position (Writer/Draw).",
    },
}


def _apply_chart_styling(chart_doc, **kwargs):
    """Apply enhanced styling properties to a chart document."""
    diagram = chart_doc.getDiagram()
    if not diagram:
        return

    # 1. 3D Mode
    is_3d = kwargs.get("is_3d")
    if is_3d is not None and hasattr(diagram, "Dim3D"):
        diagram.Dim3D = is_3d

    # 2. Stacking
    stacked = kwargs.get("stacked")
    if stacked is not None and hasattr(diagram, "Stacked"):
        diagram.Stacked = stacked

    percent = kwargs.get("percent")
    if percent is not None and hasattr(diagram, "Percent"):
        diagram.Percent = percent

    # 3. Bar/Column Orientation
    chart_type = kwargs.get("chart_type")
    if chart_type in ["bar", "column"] and hasattr(diagram, "Vertical"):
        diagram.Vertical = (chart_type == "bar")

    # 4. Titles
    title = kwargs.get("title")
    if title is not None:
        chart_doc.HasMainTitle = True
        chart_doc.getTitle().String = title

    subtitle = kwargs.get("subtitle")
    if subtitle is not None:
        chart_doc.HasSubTitle = True
        chart_doc.getSubTitle().String = subtitle

    x_axis_title = kwargs.get("x_axis_title")
    if x_axis_title is not None and hasattr(diagram, "HasXAxisTitle"):
        diagram.HasXAxisTitle = True
        try:
            _axis_title_shape_string(diagram.getXAxisTitle(), x_axis_title)
        except Exception:
            logger.debug("Setting X axis title failed", exc_info=True)

    y_axis_title = kwargs.get("y_axis_title")
    if y_axis_title is not None and hasattr(diagram, "HasYAxisTitle"):
        diagram.HasYAxisTitle = True
        try:
            _axis_title_shape_string(diagram.getYAxisTitle(), y_axis_title)
        except Exception:
            logger.debug("Setting Y axis title failed", exc_info=True)

    # 5. Legend
    has_legend = kwargs.get("has_legend")
    if has_legend is not None:
        chart_doc.HasLegend = has_legend

    legend_pos = kwargs.get("legend_position")
    if legend_pos and chart_doc.HasLegend:
        try:
            pos_map = {
                "top": uno.getConstantByName("com.sun.star.chart.ChartLegendAlignment.TOP"),
                "bottom": uno.getConstantByName("com.sun.star.chart.ChartLegendAlignment.BOTTOM"),
                "left": uno.getConstantByName("com.sun.star.chart.ChartLegendAlignment.LEFT"),
                "right": uno.getConstantByName("com.sun.star.chart.ChartLegendAlignment.RIGHT"),
            }
            if legend_pos in pos_map:
                chart_doc.getLegend().Alignment = pos_map[legend_pos]
        except (ImportError, AttributeError):
            logger.debug("ChartLegendAlignment enum not available")


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
    elif supportsService(doc, "com.sun.star.drawing.DrawingDocument") or \
         supportsService(doc, "com.sun.star.presentation.PresentationDocument"):
        # Iterate all pages and shapes
        for i in range(doc.getDrawPages().getCount()):
            page = doc.getDrawPages().getByIndex(i)
            for j in range(page.getCount()):
                shape = page.getByIndex(j)
                if shape.getShapeType() == "com.sun.star.drawing.OLE2Shape":
                    if shape.Name == chart_name:
                        return shape
    return None


class ListCharts(ToolBase):
    """List all charts on a sheet, document, or slide."""

    name = "list_charts"
    intent = "navigate"
    description = (
        "List all charts in the current context (active sheet, document, or slide) "
        "with name, title, and type."
    )
    parameters = {
        "type": "object",
        "properties": {},
        "required": [],
    }
    uno_services = [
        "com.sun.star.sheet.SpreadsheetDocument",
        "com.sun.star.text.TextDocument",
        "com.sun.star.drawing.DrawingDocument",
        "com.sun.star.presentation.PresentationDocument",
    ]

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

        elif supportsService(doc, "com.sun.star.drawing.DrawingDocument") or \
             supportsService(doc, "com.sun.star.presentation.PresentationDocument"):
            for i in range(doc.getDrawPages().getCount()):
                page = doc.getDrawPages().getByIndex(i)
                for j in range(page.getCount()):
                    shape = page.getByIndex(j)
                    if shape.getShapeType() == "com.sun.star.drawing.OLE2Shape":
                        if _is_chart_clsid(getattr(shape, "CLSID", "") or ""):
                            result.append(self._get_summary(shape, shape.Name or f"Chart_{i}_{j}"))

        return {
            "status": "ok",
            "charts": result,
            "count": len(result),
        }

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


class GetChartInfo(ToolBase):
    """Get detailed info about a chart."""

    name = "get_chart_info"
    intent = "navigate"
    description = (
        "Get detailed info about a chart: type, title, ranges (if Calc), "
        "axis titles, and legend properties."
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


class CreateChart(ToolBase):
    """Create a new chart."""

    name = "create_chart"
    intent = "edit"
    description = (
        "Creates a chart in the current context. In Calc, data_range is required. "
        "In Writer/Impress, a chart is inserted at the cursor or on the active slide."
    )
    parameters = {
        "type": "object",
        "properties": CHART_PROPERTIES,
        "required": ["chart_type"],
    }
    uno_services = ListCharts.uno_services
    is_mutation = True

    def execute(self, ctx, **kwargs):
        doc = ctx.doc
        chart_type = kwargs["chart_type"]
        chart_service = CHART_SERVICE_MAP.get(chart_type, CHART_SERVICE_MAP["column"])

        rect = uno.createUnoStruct("com.sun.star.awt.Rectangle", X=1000, Y=1000, Width=12000, Height=8000)

        try:
            if supportsService(doc, "com.sun.star.sheet.SpreadsheetDocument"):
                return self._create_calc_chart(ctx, rect, chart_service, **kwargs)
            elif supportsService(doc, "com.sun.star.text.TextDocument"):
                return self._create_writer_chart(ctx, rect, chart_service, **kwargs)
            elif supportsService(doc, "com.sun.star.presentation.PresentationDocument") or \
                 supportsService(doc, "com.sun.star.drawing.DrawingDocument"):
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

        logger.debug("Creating Calc chart: name=Chart_N, rect=(%d,%d,%d,%d), range=(%d,%d,%d,%d)", 
                     rect.X, rect.Y, rect.Width, rect.Height,
                     addr.StartColumn, addr.StartRow, addr.EndColumn, addr.EndRow)

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
        _process_events()
        return {"status": "ok", "message": f"Chart '{name}' created in Calc.", "chart_name": name}

    def _create_writer_chart(self, ctx, rect, service, **kwargs):
        """Insert a chart as inline ``TextEmbeddedObject`` (Writer body text).

        Use ``createTextCursorByRange(getEnd())`` — ``getViewCursor()`` fails for hidden docs.
        When the document has a visible window, ``getEmbeddedObjects()`` stays in sync
        with ``list_charts``; headless hidden Writer may omit embeds from the registry.
        """
        doc = ctx.doc
        text = doc.getText()
        try:
            cursor = text.createTextCursorByRange(text.getEnd())
        except Exception:
            cursor = text.createTextCursor()
            try:
                cursor.gotoEnd(False)
            except Exception:
                pass

        name = f"Chart_{len(doc.getEmbeddedObjects())}"
        chart_obj = doc.createInstance("com.sun.star.text.TextEmbeddedObject")
        chart_obj.CLSID = CHART_CLSID
        chart_obj.Name = name
        try:
            chart_obj.setPropertyValue("Width", rect.Width)
            chart_obj.setPropertyValue("Height", rect.Height)
        except Exception:
            try:
                chart_obj.Width = rect.Width
                chart_obj.Height = rect.Height
            except Exception:
                pass

        text.insertTextContent(cursor, chart_obj, False)

        chart_doc = chart_obj.getEmbeddedObject()
        if chart_doc:
            chart_doc.setDiagram(chart_doc.createInstance(service))
            _apply_chart_styling(chart_doc, **kwargs)

        chart_name = name
        try:
            is_same = getattr(uno, "isSame", None)
        except Exception:
            is_same = None
        objects = doc.getEmbeddedObjects()
        for n in objects.getElementNames():
            try:
                o = objects.getByName(n)
                if o is chart_obj:
                    chart_name = n
                    break
                try:
                    if o == chart_obj:
                        chart_name = n
                        break
                except Exception:
                    pass
                if callable(is_same) and is_same(o, chart_obj):
                    chart_name = n
                    break
            except Exception:
                pass
        if chart_name == name:
            by_clsid = []
            for n in objects.getElementNames():
                try:
                    o = objects.getByName(n)
                    if _writer_embed_is_chart(o):
                        by_clsid.append(n)
                except Exception:
                    pass
            if len(by_clsid) == 1:
                chart_name = by_clsid[0]
            elif len(by_clsid) > 1:
                chart_name = by_clsid[-1]
        try:
            enms = list(objects.getElementNames())
            if len(enms) == 1 and chart_name == name:
                chart_name = enms[0]
        except Exception:
            pass

        _process_events()
        return {"status": "ok", "message": f"Chart '{chart_name}' inserted in Writer.", "chart_name": chart_name}

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


class EditChart(ToolBase):
    """Modify chart properties."""

    name = "edit_chart"
    intent = "edit"
    description = (
        "Edit a chart's properties: title, 3D mode, stacking, legend, axes, etc."
    )
    parameters = {
        "type": "object",
        "properties": {
            **CHART_PROPERTIES,
            "chart_name": {"type": "string", "description": "Name of the chart to edit."},
        },
        "required": ["chart_name"],
    }
    uno_services = ListCharts.uno_services
    is_mutation = True

    def execute(self, ctx, **kwargs):
        doc = ctx.doc
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


class DeleteChart(ToolBase):
    """Delete a chart."""

    name = "delete_chart"
    intent = "edit"
    description = "Delete a chart by name."
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
