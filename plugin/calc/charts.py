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
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable, Literal

"""Calc chart management tools: list, info, create, edit, delete.
Enhanced to support Writer and Draw documents, 3D, stacking, and rich properties.
"""

import logging
import time

from plugin.doc.visual_helpers import parse_color_to_uno_int as _parse_color
from plugin.framework.tool import ToolBaseDummy

if TYPE_CHECKING:
    from plugin.framework.tool import ToolContext

from plugin.calc.address_utils import split_sheet_prefix
from plugin.calc.base import ToolCalcChartBase
from plugin.calc.bridge import CalcBridge
from plugin.framework.errors import is_disposed_exception
import uno


def supportsService(obj: Any, service_name: str) -> bool:
    """Helper to check if a UNO object supports a service."""
    if obj is None or not hasattr(obj, "supportsService"):
        return False
    try:
        return obj.supportsService(service_name)
    except Exception:
        return False


log = logging.getLogger("writeragent.calc")

# Chart CLSID: classic OLE chart (Calc sheet charts, Writer TextEmbeddedObject)
# Uppercase is often more compatible with older OLE registries
CHART_CLSID = "12DCAE36-07DA-43C1-9C17-56A938C64445"
# Draw/Impress OLE2Shape: add shape to page first, then set CLSID (OOo wiki sample)
CHART_CLSID_DRAW_OLE = "12DCAE26-281F-416F-A234-C3086127382E"


def _byte_sequence_payload(clsid: Any) -> bytes | bytearray | None:
    """Payload of a UNO ByteSequence, or None when *clsid* is not one.

    ``uno.ByteSequence`` is not ``bytes``. Unwrap ``.value`` (some bridges
    use ``.Value``) before the bytes/string checks, or ``str(seq)`` is
    ``<ByteSequence instance ...>`` and chart embeds disappear from
    list/get/delete/resolve.
    """
    for attr in ("Value", "value"):
        payload = getattr(clsid, attr, None)
        if isinstance(payload, (bytes, bytearray)):
            return payload
    return None


def _normalize_clsid_value(clsid: Any) -> str:
    """Coerce UNO CLSID (string, ByteSequence, etc.) to a comparable string."""
    if clsid is None:
        return ""
    payload = _byte_sequence_payload(clsid)
    if payload is not None:
        clsid = payload
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


def _chart_document_from_host(host: Any) -> Any | None:
    """Chart model from a sheet chart, Writer embed, or Draw/Impress OLE2 shape.
    Handles the extra layer of com.sun.star.embed.XEmbeddedObject for Writer.
    """
    if host is None:
        return None

    # 1. Try getEmbeddedObject (Standard for Writer TextEmbeddedObject)
    try:
        if hasattr(host, "getEmbeddedObject"):
            ed = host.getEmbeddedObject()
            log.debug("Host.getEmbeddedObject() -> %s", ed)
            if ed:
                if hasattr(ed, "Component"):
                    comp = ed.Component
                    if comp is not None:
                        return comp
                return ed
    except Exception as e:
        log.debug("_chart_document_from_host getEmbeddedObject failed: %s", e)

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
        if name and hasattr(host, "getAnchor"):  # Likely a Writer object
            # We'll just assume the caller handles the doc-level search if this fails.
            pass
    except Exception:
        pass

    return None


def _strip_chart_schema_for_doc_type(properties: dict[str, Any], doc_type: str | None) -> None:
    """Drop fields that are invalid for *doc_type* (in-place)."""
    if doc_type == "calc":
        properties.pop("headers", None)
        properties.pop("rows", None)
    elif doc_type in ("writer", "draw", "impress"):
        properties.pop("data_range", None)
        properties.pop("sheet", None)
        properties.pop("has_header", None)


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


def _axis_title_shape_string(shape: Any, value: str | None) -> str | None:
    """Read or write axis title text on a diagram title shape (ChartAxis*Supplier).

    Return None unless the ``String`` property exists. ``hasattr`` failing
    and still returning *value* logs a missing setter as a successful write.
    """
    if shape is None or not hasattr(shape, "String"):
        return None
    if value is not None:
        shape.String = value
        return value
    return shape.String


def _process_events(ctx: Any = None, *, deadline: float | None = None) -> bool:
    """Pump UI events once so chart object names and models can settle.

    Returns True only when an idle pump finished. False means idle did not
    arrive (testing, headless, no context, past *deadline*, or the pump did
    not run). Callers must stop instead of spinning on the UI thread.
    """
    import os
    import time

    if deadline is not None and time.monotonic() >= deadline:
        return False
    if os.environ.get("WRITERAGENT_TESTING") == "1":
        return False
    try:
        from plugin.framework.uno_context import get_desktop, get_ctx

        uctx = ctx or get_ctx()
        if not uctx:
            return False
        # Bypass if running in headless mode (no active frame) to avoid event pump hangs during chart rendering
        desktop = get_desktop(uctx)
        if desktop and desktop.getActiveFrame() is None:
            return False

        from plugin.framework.uno_context import process_events_to_idle

        return bool(process_events_to_idle(uctx))
    except Exception:
        # Avoid letting UI event processing crash the tool
        return False


# Total budget for re-pumping while the embedded chart model appears. A pump that
# never reaches idle must not repeat; processEventsToIdle itself has no timeout,
# so this caps further pumps.
_WRITER_CHART_MODEL_WAIT_SEC = 0.5


def _await_writer_chart_document(chart_obj: Any, ctx: Any, *, timeout: float = _WRITER_CHART_MODEL_WAIT_SEC) -> Any | None:
    """Poll for an embedded Writer chart model without spinning the UI thread.

    Stop at a hard timeout, and stop immediately when a pump does not
    report that idle was reached. A fixed retry count with no deadline
    keeps calling ``_process_events``, and ``processEventsToIdle`` does
    not return when idle never arrives, which freezes the UI thread.
    """
    import time

    deadline = time.monotonic() + max(0.0, float(timeout))
    chart_doc = _chart_document_from_host(chart_obj)
    if chart_doc is not None:
        log.info("Obtained chart model on attempt 1")
        return chart_doc
    pumps = 0
    while time.monotonic() < deadline:
        pumps += 1
        log.debug("Model missing on attempt %d, pumping events...", pumps)
        idle_reached = _process_events(getattr(ctx, "ctx", ctx), deadline=deadline)
        chart_doc = _chart_document_from_host(chart_obj)
        if chart_doc is not None:
            log.info("Obtained chart model on attempt %d", pumps + 1)
            return chart_doc
        if not idle_reached:
            log.debug("Writer chart model wait stopped: idle did not arrive")
            return None
        # No sleep between pumps. time.sleep here holds SolarMutex, so
        # worker threads waiting on the mutex stall. The next pump's VCL
        # yield is where they get it, and the deadline above bounds the loop.
    log.debug("Writer chart model wait hit total timeout (%.2fs)", timeout)
    return None


# com.sun.star.chart.ChartLegend.Alignment. Not ChartLegendAlignment.
_CHART_LEGEND_POSITION_ENUM = "com.sun.star.chart.ChartLegendPosition"
_LEGEND_POSITION_MEMBER = {
    "top": "TOP",
    "bottom": "BOTTOM",
    "left": "LEFT",
    "right": "RIGHT",
}


# Shared parameters for Create and Edit
CHART_PROPERTIES = {
    "sheet": {"type": "string", "description": "Sheet name where the chart should be placed (Calc only, defaults to active sheet)."},
    "data_range": {"type": "string", "description": "Cell range for chart data (Calc only, e.g. 'A1:B10')."},
    "has_header": {"type": "boolean", "description": "Whether the first row/column of data_range contains header and category labels (Calc only, defaults to true)."},
    "headers": {"type": "array", "items": {"type": "string"}, "description": "Category/series column headers (Writer/Draw only, e.g. ['Month', 'Sales', 'Expenses'])."},
    "rows": {"type": "array", "items": {"type": "array", "description": "Row containing category label as first element, followed by numeric values."}, "description": "2D array of category labels and values (Writer/Draw only, e.g. [['Jan', 100, 80], ['Feb', 150, 110]])."},
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


def _apply_chart_styling(chart_doc: Any, **kwargs: Any) -> None:
    """Apply enhanced styling properties to a chart document."""
    log.info("Applying chart styling with kwargs: %s", {k: v for k, v in kwargs.items() if k not in ["data_range"]})
    diagram = chart_doc.getDiagram()
    if not diagram:
        log.warning("No diagram found on chart document.")
        return

    # 1. 3D Mode
    is_3d = kwargs.get("is_3d")
    if is_3d is not None and hasattr(diagram, "Dim3D"):
        diagram.Dim3D = is_3d
        log.debug("Set diagram 3D mode: %s", is_3d)

    # 2. Stacking
    stacked = kwargs.get("stacked")
    if stacked is not None and hasattr(diagram, "Stacked"):
        diagram.Stacked = stacked
        log.debug("Set diagram stacked mode: %s", stacked)

    percent = kwargs.get("percent")
    if percent is not None and hasattr(diagram, "Percent"):
        diagram.Percent = percent
        log.debug("Set diagram percent stacked: %s", percent)

    # 3. Bar/Column Orientation
    chart_type = kwargs.get("chart_type")
    if chart_type in ["bar", "column"] and hasattr(diagram, "Vertical"):
        diagram.Vertical = chart_type == "bar"
        log.debug("Set diagram orientation: vertical=%s", diagram.Vertical)

    # 4. Titles
    title = kwargs.get("title")
    if title is not None:
        chart_doc.HasMainTitle = True
        chart_doc.getTitle().String = title
        log.debug("Set chart main title: '%s'", title)

    subtitle = kwargs.get("subtitle")
    if subtitle is not None:
        chart_doc.HasSubTitle = True
        chart_doc.getSubTitle().String = subtitle
        log.debug("Set chart subtitle: '%s'", subtitle)

    x_axis_title = kwargs.get("x_axis_title")
    if x_axis_title is not None and hasattr(diagram, "HasXAxisTitle"):
        diagram.HasXAxisTitle = True
        try:
            written = _axis_title_shape_string(diagram.getXAxisTitle(), x_axis_title)
            if written is not None:
                log.debug("Set X axis title: '%s'", written)
        except Exception:
            log.exception("Setting X axis title failed")

    y_axis_title = kwargs.get("y_axis_title")
    if y_axis_title is not None and hasattr(diagram, "HasYAxisTitle"):
        diagram.HasYAxisTitle = True
        try:
            written = _axis_title_shape_string(diagram.getYAxisTitle(), y_axis_title)
            if written is not None:
                log.debug("Set Y axis title: '%s'", written)
        except Exception:
            log.exception("Setting Y axis title failed")

    # 5. Legend
    # offapi ChartLegend.Alignment is ChartLegendPosition (NONE/LEFT/TOP/RIGHT/BOTTOM).
    # There is no ChartLegendAlignment type.
    has_legend = kwargs.get("has_legend")
    if has_legend is not None:
        chart_doc.HasLegend = has_legend
        log.debug("Set chart legend visibility: %s", has_legend)

    legend_pos = kwargs.get("legend_position")
    if legend_pos and chart_doc.HasLegend:
        if legend_pos == "none":
            chart_doc.HasLegend = False
            log.debug("Set legend position: none")
        elif legend_pos in _LEGEND_POSITION_MEMBER:
            # Alignment is ChartLegendPosition. ChartLegendAlignment is not
            # in the API, so uno.Enum raises RuntimeException. The handler
            # only catches ImportError and AttributeError, which lets that
            # escape after the chart is already inserted. If this still
            # fails, the create path removes the chart it just inserted.
            member = _LEGEND_POSITION_MEMBER[legend_pos]
            chart_doc.getLegend().Alignment = uno.Enum(_CHART_LEGEND_POSITION_ENUM, member)
            log.debug("Set legend position: %s", legend_pos)

    # 6. Background Color
    bg_color = kwargs.get("bg_color")
    if bg_color:
        parsed_bg = _parse_color(bg_color)
        if parsed_bg is not None:
            try:
                bg = chart_doc.getPageBackground()
                bg.setPropertyValue("FillStyle", uno.Enum("com.sun.star.drawing.FillStyle", "SOLID"))
                bg.setPropertyValue("FillColor", parsed_bg)
                log.info("Set chart background color: %s (RGB %d)", bg_color, parsed_bg)
            except Exception:
                log.exception("Failed to set chart background color")
        else:
            log.warning("Invalid bg_color ignored: '%s'", bg_color)

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
                                try:
                                    for prop in ["Color", "FillColor", "LineColor"]:
                                        if s.getPropertySetInfo().hasPropertyByName(prop):
                                            s.setPropertyValue(prop, color_val)
                                except Exception as exc:
                                    # Skip a disposed series and keep coloring
                                    # the rest. One getPropertySetInfo or
                                    # setPropertyValue failure must not abort
                                    # every later series.
                                    if is_disposed_exception(exc):
                                        log.debug("Skipping disposed chart series %d", idx)
                                        continue
                                    raise
                                log.info("Set data series %d color to RGB %d", idx, color_val)
                                series_count += 1
                    log.info("Successfully styled %d chart data series with colors %s", series_count, colors)
                else:
                    log.warning("Could not retrieve first diagram using getFirstDiagram")
            except Exception:
                log.exception("Failed to set data series colors")

    # 8. Programmatic Data Arrays (Writer/Draw)
    headers = kwargs.get("headers")
    rows = kwargs.get("rows")
    if headers and rows:
        _apply_chart_data_arrays(chart_doc, headers, rows)


def _apply_chart_data_arrays(chart_doc: Any, headers: Any, rows: Any) -> None:
    """Set chart data programmatically via XChartDataArray for Writer/Draw."""
    if not headers or not rows:
        return

    try:
        chart_data = chart_doc.getData()
        if not chart_data:
            log.warning("No chart data object found on chart document.")
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
        log.info("Successfully applied chart data arrays: row_desc=%s, col_desc=%s, data=%s", row_desc, col_desc, final_values)

    except Exception as e:
        log.exception("Failed to apply chart data arrays: %s", e)


def _drop_failed_chart_insert(remove: Callable[[], None], name: str) -> None:
    """Remove a chart inserted before a later create step failed.

    Delete this insert before the error is returned. A failed delete is
    logged and does not replace the original error. ``legend_position``
    used ``uno.Enum`` for ``ChartLegendAlignment``, which is not in the
    UNO API, so that call raises ``RuntimeException``. The legend handler
    only caught ``ImportError`` and ``AttributeError``, and the create
    handler left the chart in the sheet, text, or slide while telling the
    caller it did not exist. The same window exists for any property set
    that fails after the insert (diagram, title, legend).
    """
    try:
        remove()
    except Exception:
        log.exception("Failed to remove chart %r after a create error", name)


def _drop_writer_chart_insert(doc: Any, text: Any, chart_obj: Any, name: str) -> None:
    """Remove a Writer chart embedded before a later create step failed."""

    def _remove() -> None:
        try:
            text.removeTextContent(chart_obj)
        except Exception:
            objects = doc.getEmbeddedObjects()
            if objects.hasByName(name):
                objects.removeByName(name)
            else:
                raise

    _drop_failed_chart_insert(_remove, name)


def _format_chart_exception_msg(e: Exception) -> str:
    """Format an exception into a non-empty descriptive string (handles UNO exception Message field)."""
    detail = getattr(e, "Message", None) or str(e)
    if isinstance(detail, str):
        detail = detail.strip()
    else:
        detail = ""
    if detail:
        return f"{type(e).__name__}: {detail}"
    return f"{type(e).__name__}"


def _get_all_calc_chart_names(doc: Any) -> set[str]:
    """Return a set of all chart names existing across all sheets in a Calc document."""
    names: set[str] = set()
    try:
        sheets = doc.getSheets()
        for name in sheets.getElementNames():
            names.update(sheets.getByName(name).getCharts().getElementNames())
    except Exception as exc:
        if is_disposed_exception(exc):
            raise
    return names


def _find_calc_chart_and_sheet(doc: Any, chart_name: str, sheet_name: str | None = None) -> tuple[Any | None, Any | None]:
    """Find a chart object and its parent sheet across all sheets in a Calc document.

    Note: Chart names in Calc are document-wide objects (e.g. Chart_0, Chart_1).
    If sheet_name is omitted or mismatched, this function automatically falls back
    to searching across all sheets in the document to locate and operate on the chart cleanly.
    """
    try:
        sheets = doc.getSheets()
        if sheet_name and sheets.hasByName(sheet_name):
            sheet = sheets.getByName(sheet_name)
            charts = sheet.getCharts()
            if charts.hasByName(chart_name):
                return charts.getByName(chart_name), sheet

        for name in sheets.getElementNames():
            sheet = sheets.getByName(name)
            charts = sheet.getCharts()
            if charts.hasByName(chart_name):
                return charts.getByName(chart_name), sheet
    except Exception as exc:
        if is_disposed_exception(exc):
            raise
    return None, None


def _ole2_shape_at(page: Any, index: int) -> Any | None:
    """OLE2 shape at *index*, or None when it is another type or disposed.

    Skip a disposed shape and keep scanning. ``getShapeType`` with no
    per-shape guard lets one ``DisposedException`` end list/resolve for
    every later shape. Other errors still propagate.
    """
    try:
        shape = page.getByIndex(index)
        if shape.getShapeType() != "com.sun.star.drawing.OLE2Shape":
            return None
        return shape
    except Exception as exc:
        if is_disposed_exception(exc):
            return None
        raise


def _resolve_chart(doc: Any, chart_name: str, sheet_name: str | None = None) -> Any | None:
    """Resolve a chart object by name across Calc, Writer, or Draw."""
    if supportsService(doc, "com.sun.star.sheet.SpreadsheetDocument"):
        chart_obj, _ = _find_calc_chart_and_sheet(doc, chart_name, sheet_name)
        return chart_obj
    elif supportsService(doc, "com.sun.star.text.TextDocument"):
        objects = doc.getEmbeddedObjects()
        if objects.hasByName(chart_name):
            return objects.getByName(chart_name)
        try:
            page = doc.getDrawPage()
            for j in range(page.getCount()):
                shape = _ole2_shape_at(page, j)
                if shape is None:
                    continue
                try:
                    if (shape.Name or "") == chart_name:
                        return shape
                except Exception as exc:
                    if is_disposed_exception(exc):
                        continue
                    raise
        except Exception:
            pass
    elif supportsService(doc, "com.sun.star.drawing.DrawingDocument") or supportsService(doc, "com.sun.star.presentation.PresentationDocument"):
        # Iterate all pages and shapes
        for i in range(doc.getDrawPages().getCount()):
            page = doc.getDrawPages().getByIndex(i)
            for j in range(page.getCount()):
                shape = _ole2_shape_at(page, j)
                if shape is None:
                    continue
                try:
                    if shape.Name == chart_name:
                        return shape
                except Exception as exc:
                    if is_disposed_exception(exc):
                        continue
                    raise
    return None


class ListCharts(ToolBaseDummy):
    """List charts; Dummy backend for ``ManageCharts`` action=list."""

    name: str | None = "list_charts"
    intent: str | None = "navigate"
    description: str = "List all charts in the current context (active sheet, document, or slide) with name, title, and type."
    parameters: dict[str, Any] | None = {"type": "object", "properties": {}, "required": []}
    uno_services: list[str] | None = ["com.sun.star.sheet.SpreadsheetDocument", "com.sun.star.text.TextDocument", "com.sun.star.drawing.DrawingDocument", "com.sun.star.presentation.PresentationDocument"]

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        doc = ctx.doc
        result = []

        if supportsService(doc, "com.sun.star.sheet.SpreadsheetDocument"):
            try:
                sheets = doc.getSheets()
                for i in range(sheets.getCount()):
                    sheet = sheets.getByIndex(i)
                    sheet_name = sheet.getName()
                    charts = sheet.getCharts()
                    for name in charts.getElementNames():
                        chart_obj = charts.getByName(name)
                        result.append(self._get_summary(chart_obj, name, sheet_name=sheet_name))
            except Exception as exc:
                if is_disposed_exception(exc):
                    raise

        elif supportsService(doc, "com.sun.star.text.TextDocument"):
            objects = doc.getEmbeddedObjects()
            for name in objects.getElementNames():
                obj = objects.getByName(name)
                if _writer_embed_is_chart(obj):
                    result.append(self._get_summary(obj, name))
            try:
                page = doc.getDrawPage()
                for j in range(page.getCount()):
                    shape = _ole2_shape_at(page, j)
                    if shape is None:
                        continue
                    try:
                        if _is_chart_clsid(getattr(shape, "CLSID", "") or ""):
                            nm = shape.Name or f"Chart_{j}"
                            result.append(self._get_summary(shape, nm))
                    except Exception as exc:
                        if is_disposed_exception(exc):
                            continue
                        raise
            except Exception:
                pass

        elif supportsService(doc, "com.sun.star.drawing.DrawingDocument") or supportsService(doc, "com.sun.star.presentation.PresentationDocument"):
            for i in range(doc.getDrawPages().getCount()):
                page = doc.getDrawPages().getByIndex(i)
                for j in range(page.getCount()):
                    shape = _ole2_shape_at(page, j)
                    if shape is None:
                        continue
                    try:
                        if _is_chart_clsid(getattr(shape, "CLSID", "") or ""):
                            result.append(self._get_summary(shape, shape.Name or f"Chart_{i}_{j}"))
                    except Exception as exc:
                        if is_disposed_exception(exc):
                            continue
                        raise

        return {"status": "ok", "charts": result, "count": len(result)}

    def _get_summary(self, chart_obj: Any, name: str, sheet_name: str | None = None) -> dict[str, Any]:
        entry = {"name": name}
        if sheet_name:
            entry["sheet_name"] = sheet_name
        try:
            chart_doc = _chart_document_from_host(chart_obj)
            if chart_doc:
                entry["title"] = chart_doc.getTitle().String if chart_doc.HasMainTitle else ""
                entry["diagram_type"] = chart_doc.getDiagram().getDiagramType()
        except Exception:
            pass
        return entry


class GetChartInfo(ToolBaseDummy):
    """Chart details; Dummy backend for ``ManageCharts`` action=get_info."""

    name: str | None = "get_chart_info"
    intent: str | None = "navigate"
    description: str = "Get detailed info about a chart: type, title, ranges (if Calc), axis titles, and legend properties."
    parameters: dict[str, Any] | None = {"type": "object", "properties": {"name": {"type": "string", "description": "Chart name (from list_charts)."}}, "required": ["name"]}
    uno_services: list[str] | None = ListCharts.uno_services

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        doc = ctx.doc
        chart_name = kwargs["name"]
        chart_obj = _resolve_chart(doc, chart_name, kwargs.get("sheet"))

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
            log.debug("get_chart_info error: %s", e)

        return info


class UpsertChart(ToolBaseDummy):
    """Create/edit chart; Dummy backend for ``ManageCharts`` action=create|edit."""

    name: str | None = "upsert_chart"
    intent: str | None = "edit"
    description: str = "Creates a new chart or modifies an existing chart on a sheet, document, or slide."
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["create", "edit"], "description": "Action to perform: 'create' a new chart, or 'edit' an existing one."},
            "name": {"type": "string", "description": "Name of the chart to edit (required for action='edit', optional for action='create')."},
            **CHART_PROPERTIES,
        },
        "required": ["action"],
    }
    uno_services: list[str] | None = ListCharts.uno_services
    is_mutation: bool | None = True

    def get_parameters(self, doc_type: str | None = None) -> dict[str, Any] | None:
        import copy
        from typing import cast

        params = copy.deepcopy(self.parameters)
        if not params or "properties" not in params:
            return params
        properties = cast("dict[str, Any]", params["properties"])
        _strip_chart_schema_for_doc_type(properties, doc_type)
        return params

    def validate(self, *, doc_type: str | None = None, **kwargs: Any) -> tuple[Literal[False], str] | tuple[Literal[True], None]:
        # ToolBaseDummy has no schema validate; mirror ToolBase.validate here.
        schema = self.get_parameters(doc_type) or {}
        required = schema.get("required", [])
        for key in required:
            if key not in kwargs:
                return False, f"Missing required parameter: {key}"
        props = schema.get("properties", {})
        for key in kwargs:
            if props and key not in props:
                return False, f"Unknown parameter: {key}"
        action = kwargs.get("action")
        if action == "create":
            if not kwargs.get("chart_type"):
                return False, "Parameter 'chart_type' is required when action is 'create'"
            if doc_type == "calc":
                if not kwargs.get("data_range"):
                    return False, "Parameter 'data_range' is required when action is 'create' in Calc"
            else:
                if not kwargs.get("headers") or not kwargs.get("rows"):
                    return False, "Both 'headers' and 'rows' are required to create a chart in Writer or Draw/Impress"
        elif action == "edit":
            if "name" not in kwargs:
                return False, "Parameter 'name' is required when action is 'edit'"
        return True, None

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        action = kwargs.get("action")
        doc = ctx.doc
        is_calc = supportsService(doc, "com.sun.star.sheet.SpreadsheetDocument")

        # Doc-type parameter checks (in addition to validate)
        if is_calc:
            if "headers" in kwargs or "rows" in kwargs:
                return self._tool_error("Data arrays ('headers', 'rows') are not supported in Calc. Please use 'data_range' instead.")
        else:
            if "data_range" in kwargs:
                return self._tool_error("Parameter 'data_range' is only supported in Calc. For Writer/Draw, please use 'headers' and 'rows' to pass chart data.")

        if action == "create":
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
                msg = _format_chart_exception_msg(e)
                log.exception("Chart creation failed")
                return self._tool_error(f"Failed to create chart: {msg}", code="CHART_CREATE_ERROR")

        elif action == "edit":
            chart_name = kwargs["name"]
            chart_obj = _resolve_chart(doc, chart_name, kwargs.get("sheet"))

            if not chart_obj:
                return self._tool_error(f"Chart '{chart_name}' not found.")

            chart_doc = _chart_document_from_host(chart_obj)
            if not chart_doc:
                return self._tool_error("Cannot access chart content.")

            try:
                # If chart_type is provided, update the diagram first
                chart_type = kwargs.get("chart_type")
                if chart_type:
                    service = CHART_SERVICE_MAP.get(chart_type)
                    if service:
                        chart_doc.setDiagram(chart_doc.createInstance(service))

                _apply_chart_styling(chart_doc, **kwargs)
            except Exception as e:
                msg = _format_chart_exception_msg(e)
                log.exception("Chart edit failed")
                return self._tool_error(f"Failed to edit chart: {msg}", code="CHART_EDIT_ERROR")

            return {"status": "ok", "name": chart_name, "message": "Chart updated."}

        return self._tool_error(f"Unsupported action: '{action}'")

    def _create_calc_chart(self, ctx: ToolContext, rect: Any, service: str, **kwargs: Any) -> dict[str, Any]:
        bridge = CalcBridge(ctx.doc)
        data_range = kwargs.get("data_range")
        if not data_range:
            return self._tool_error("data_range is required for Calc charts.")

        target_sheet_name = kwargs.get("sheet")
        if target_sheet_name:
            try:
                sheet = bridge.get_sheet(target_sheet_name)
            except Exception:
                return self._tool_error(f"Sheet '{target_sheet_name}' not found in document.")
        else:
            sheet = bridge.get_active_sheet()

        cell_range = bridge.get_cell_range(sheet, data_range)
        addr = cell_range.getRangeAddress()

        pos_str = kwargs.get("position")
        if pos_str:
            try:
                _unused, bare_pos = split_sheet_prefix(pos_str)
                cell_obj = sheet.getCellRangeByName(bare_pos)
                cell_pos = cell_obj.getPosition()
                cell_size = cell_obj.getSize()
                rect.X = cell_pos.X
                rect.Y = cell_pos.Y
                if cell_size.Width > 0 and cell_size.Height > 0:
                    rect.Width = cell_size.Width
                    rect.Height = cell_size.Height
            except Exception:
                pass

        # The log does not place the chart. Missing fields stay None.
        # Reading rect.X before addNewByName aborts create on the headless
        # pytest stub (an empty SimpleNamespace), so a legend failure never
        # reaches removeByName.
        log.debug(
            "Creating Calc chart: sheet=%s, rect=(%s,%s,%s,%s), range=(%s,%s,%s,%s)",
            sheet.getName(),
            getattr(rect, "X", None),
            getattr(rect, "Y", None),
            getattr(rect, "Width", None),
            getattr(rect, "Height", None),
            getattr(addr, "StartColumn", None),
            getattr(addr, "StartRow", None),
            getattr(addr, "EndColumn", None),
            getattr(addr, "EndRow", None),
        )

        existing_names = _get_all_calc_chart_names(ctx.doc)
        idx = len(existing_names)
        name = f"Chart_{idx}"
        while name in existing_names:
            idx += 1
            name = f"Chart_{idx}"

        has_header = bool(kwargs.get("has_header", True))
        charts = sheet.getCharts()
        charts.addNewByName(name, rect, (addr,), has_header, has_header)

        try:
            chart_obj = charts.getByName(name)
            chart_doc = _chart_document_from_host(chart_obj)
            if not chart_doc:
                _drop_failed_chart_insert(lambda: charts.removeByName(name), name)
                return self._tool_error("Cannot access chart content.")
            chart_doc.setDiagram(chart_doc.createInstance(service))
            _apply_chart_styling(chart_doc, **kwargs)
        except Exception:
            # Insert already committed. Drop it so the error return is not an orphan chart.
            _drop_failed_chart_insert(lambda: charts.removeByName(name), name)
            raise
        # _process_events() causes a hang in tests
        return {"status": "ok", "message": f"Chart '{name}' created on sheet '{sheet.getName()}'.", "name": name, "sheet": sheet.getName()}

    def _create_writer_chart(self, ctx: ToolContext, rect: Any, service: str, **kwargs: Any) -> dict[str, Any]:
        """Insert a chart as inline ``TextEmbeddedObject`` (Writer body text).
        Using a bounded wait and event pumping to ensure the embedded model is initialized.
        """
        doc = ctx.doc
        text = doc.getText()
        log.info("Creating Writer chart. Current text length: %d", len(text.getString()))

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
                log.debug("TextEmbeddedObject Implementation: %s", chart_obj.getImplementationName())
            except Exception:
                pass

            # CRITICAL: Match proven working pattern from plugin/writer/math/math_mml_convert.py
            chart_obj.CLSID = CHART_CLSID_DRAW_OLE.upper()
            from com.sun.star.text.TextContentAnchorType import AS_CHARACTER

            chart_obj.AnchorType = AS_CHARACTER

            # Try to set name before insertion
            try:
                chart_obj.Name = name
            except Exception:
                pass

            log.info("Created and configured TextEmbeddedObject with CLSID: %s", chart_obj.CLSID)
        except Exception as e:
            log.debug("Creation/config failed: %s", e)
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
            log.info("Successfully inserted chart object into text.")
        except Exception as e:
            # Fallback 1: Try AT_PARAGRAPH
            log.debug("First insertion attempt failed (%s). Trying AT_PARAGRAPH anchor...", e)
            try:
                from com.sun.star.text.TextContentAnchorType import AT_PARAGRAPH

                chart_obj.AnchorType = AT_PARAGRAPH
                text.insertTextContent(cursor, chart_obj, False)
                log.info("Successfully inserted chart object with AT_PARAGRAPH.")
            except Exception:
                # Fallback 2: Try CHART_CLSID_DRAW_OLE
                log.debug("Second insertion attempt failed. Trying DRAW_OLE CLSID...")
                try:
                    chart_obj.CLSID = CHART_CLSID_DRAW_OLE.upper()
                    text.insertTextContent(cursor, chart_obj, False)
                    log.info("Successfully inserted chart object with DRAW_OLE CLSID.")
                except Exception as e3:
                    log.exception("All insertion attempts failed for chart object")
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
                log.debug("Set AnchorType to AS_CHARACTER (post-insertion)")
        except Exception as e:
            log.debug("Failed to set AnchorType post-insertion: %s", e)

        try:
            chart_obj.setPropertyValue("Width", rect.Width)
            chart_obj.setPropertyValue("Height", rect.Height)
            log.debug("Set size post-insertion: %dx%d", rect.Width, rect.Height)
        except Exception:
            try:
                chart_obj.Width = rect.Width
                chart_obj.Height = rect.Height
            except Exception:
                pass

        # 5. Wait for model initialization. One idle pump that never returns
        # freezes the UI; the helper stops at a hard timeout or when idle does not arrive.
        chart_doc = _await_writer_chart_document(chart_obj, ctx.ctx)

        if not chart_doc:
            # Last ditch effort: find it in the collection
            try:
                objects = doc.getEmbeddedObjects()
                log.debug("Final attempt: checking EmbeddedObjects collection (count=%d)", objects.getCount())
                if objects.hasByName(name):
                    obj = objects.getByName(name)
                    log.debug("Found object in collection by name. Type: %s", type(obj))
                    chart_doc = _chart_document_from_host(obj)
            except Exception as e:
                log.debug("Last ditch effort failed: %s", e)
                pass

        # 5. Configure Diagram
        if chart_doc:
            try:
                diagram = chart_doc.createInstance(service)
                if diagram:
                    chart_doc.setDiagram(diagram)
                    log.info("Set chart diagram: %s", service)
                else:
                    log.error("Failed to create diagram instance for service: %s", service)
            except Exception:
                log.exception("Failed to set chart diagram")

            try:
                _apply_chart_styling(chart_doc, **kwargs)
            except Exception:
                _drop_writer_chart_insert(doc, text, chart_obj, name)
                raise
        else:
            _drop_writer_chart_insert(doc, text, chart_obj, name)
            return self._tool_error("Cannot access chart content.")

        # Force a refresh of the chart model using direct UNO calls on the chart document itself.
        if chart_doc:
            try:
                # 1. Notify views of model changes (com.sun.star.util.XModifiable)
                if hasattr(chart_doc, "setModified"):
                    chart_doc.setModified(True)
                    log.debug("Called chart_doc.setModified(True) to notify view listeners.")

                # 2. Trigger layout/calculations update (com.sun.star.util.XRefreshable)
                if hasattr(chart_doc, "refresh"):
                    chart_doc.refresh()
                    log.debug("Called chart_doc.refresh() to update chart document.")
            except Exception as e:
                log.debug("Failed direct chart_doc model update: %s", e)

        _process_events(ctx.ctx, deadline=time.monotonic() + _WRITER_CHART_MODEL_WAIT_SEC)
        return {"status": "ok", "message": f"Chart '{name}' inserted in Writer.", "name": name}

    def _create_draw_chart(self, ctx: ToolContext, rect: Any, service: str, **kwargs: Any) -> dict[str, Any]:
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
        name = ""
        try:
            try:
                shape.setSize(uno.createUnoStruct("com.sun.star.awt.Size", Width=rect.Width, Height=rect.Height))
                shape.setPosition(uno.createUnoStruct("com.sun.star.awt.Point", X=rect.X, Y=rect.Y))
            except Exception as e:
                log.debug("Failed to set Draw shape size/pos: %s", e)
            shape.CLSID = CHART_CLSID_DRAW_OLE

            name = f"Chart_{page.getCount()}"
            shape.Name = name

            chart_doc = _chart_document_from_host(shape)
            if not chart_doc:
                _drop_failed_chart_insert(lambda: page.remove(shape), name or "draw-chart")
                return self._tool_error("Cannot access chart content.")
            chart_doc.setDiagram(chart_doc.createInstance(service))
            _apply_chart_styling(chart_doc, **kwargs)
        except Exception:
            _drop_failed_chart_insert(lambda: page.remove(shape), name or "draw-chart")
            raise

        _process_events(ctx.ctx, deadline=time.monotonic() + _WRITER_CHART_MODEL_WAIT_SEC)
        return {"status": "ok", "message": f"Chart '{name}' inserted on slide.", "name": name}


class DeleteChart(ToolBaseDummy):
    """Delete chart; Dummy backend for ``ManageCharts`` action=delete."""

    name: str | None = "delete_chart"
    intent: str | None = "edit"
    description: str = "Delete a chart by name."
    parameters: dict[str, Any] | None = {"type": "object", "properties": {"name": {"type": "string", "description": "Chart name to delete."}}, "required": ["name"]}
    uno_services: list[str] | None = ListCharts.uno_services
    is_mutation: bool | None = True

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        doc = ctx.doc
        chart_name = kwargs["name"]

        if supportsService(doc, "com.sun.star.sheet.SpreadsheetDocument"):
            chart_obj, sheet = _find_calc_chart_and_sheet(doc, chart_name, kwargs.get("sheet"))
            if not chart_obj or not sheet:
                return self._tool_error(f"Chart '{chart_name}' not found.")
            sheet.getCharts().removeByName(chart_name)
            return {"status": "ok", "deleted": chart_name, "sheet": sheet.getName()}
        elif supportsService(doc, "com.sun.star.text.TextDocument"):
            objects = doc.getEmbeddedObjects()
            if objects.hasByName(chart_name):
                objects.removeByName(chart_name)
                return {"status": "ok", "deleted": chart_name}
            try:
                page = doc.getDrawPage()
                for j in range(page.getCount()):
                    shape = _ole2_shape_at(page, j)
                    if shape and shape.Name == chart_name:
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
                    shape = _ole2_shape_at(page, j)
                    if shape and shape.Name == chart_name:
                        page.remove(shape)
                        return {"status": "ok", "deleted": chart_name}
            return self._tool_error(f"Chart '{chart_name}' not found.")

        return {"status": "ok", "deleted": chart_name}


class ManageCharts(ToolCalcChartBase):
    """Manage charts: list, get_info, create, edit, or delete in the current context.

    Sole charts-domain tool advertised to LLMs/MCP. Calc/Writer/Draw each register
    ``ManageCharts`` with their chart specialized base (like ``shape_upsert``);
    ``ToolRegistry`` keeps one instance per name (last module load wins). Writer/Draw
    set union ``uno_services`` so registration order does not drop other document types.
    Skinny list/info/upsert/delete classes are Dummy backends for this dispatcher.

    Future (per-app tiers and a growing API, e.g. full 3D):
    - Registry: store ``list[ToolBase]`` per name and resolve via ``supportsService`` instead of last-wins.
    - Or ``get_tier(doc_type)`` on a single class if multi-bind is too heavy.
    - Schema: ``get_parameters(doc_type)`` / ``get_description(doc_type)`` so Calc keeps ``data_range`` while
      Writer/Draw omit it; add a 3D block (view angle, perspective, wall/floor) when UNO paths beyond ``is_3d`` exist.
    - Sub-agent: richer ``required_core_tools`` or a domain preamble when the consolidated schema grows.
    """

    name: str | None = "manage_charts"
    intent: str | None = "edit"
    description: str = "Manage charts: list, get_info, create, edit, or delete a chart in the current context (active sheet, document, or slide)."
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["list", "get_info", "create", "edit", "delete"], "description": "The action to perform on the charts."},
            "name": {"type": "string", "description": "The name of the chart (required for get_info, edit, delete)."},
            "sheet": {"type": "string", "description": "Sheet name where the chart should be placed (Calc only, defaults to active sheet)."},
            "data_range": {"type": "string", "description": "Cell range for chart data (Calc only, required for create, e.g. 'A1:B10')."},
            "has_header": {"type": "boolean", "description": "Whether the first row/column of data_range contains header and category labels (Calc only, defaults to true)."},
            "headers": {"type": "array", "items": {"type": "string"}, "description": "Category/series column headers (Writer/Draw only, required for create, e.g. ['Month', 'Sales', 'Expenses'])."},
            "rows": {
                "type": "array",
                "items": {"type": "array", "description": "Row containing category label as first element, followed by numeric values."},
                "description": "2D array of category labels and values (Writer/Draw only, required for create, e.g. [['Jan', 100, 80], ['Feb', 150, 110]]).",
            },
            "chart_type": {"type": "string", "enum": ["bar", "pie", "column", "line", "scatter", "area", "donut", "net", "stock", "bubble"], "description": "Type of chart to create or update to (required for create)."},
            "title": {"type": "string", "description": "Chart title."},
            "subtitle": {"type": "string", "description": "Chart subtitle."},
            "is_3d": {"type": "boolean", "description": "Enable 3D mode."},
            "stacked": {"type": "boolean", "description": "Stacked data series."},
            "percent": {"type": "boolean", "description": "Percentage stacked."},
            "x_axis_title": {"type": "string", "description": "Title for X axis."},
            "y_axis_title": {"type": "string", "description": "Title for Y axis."},
            "legend_position": {"type": "string", "enum": ["none", "top", "bottom", "left", "right"], "description": "Legend position."},
            "has_legend": {"type": "boolean", "description": "Whether the chart has a legend."},
            "position": {"type": "string", "description": "Cell address (Calc) or anchoring position (Writer/Draw)."},
            "bg_color": {"type": "string", "description": "Chart area background color (hex: #FF0000 or name: green)."},
            "colors": {"type": "array", "items": {"type": "string"}, "description": "List of hex/named colors to apply to each data series."},
        },
        "required": ["action"],
    }
    uno_services: list[str] | None = ["com.sun.star.sheet.SpreadsheetDocument", "com.sun.star.text.TextDocument", "com.sun.star.drawing.DrawingDocument", "com.sun.star.presentation.PresentationDocument"]
    is_mutation: bool | None = True

    def get_parameters(self, doc_type: str | None = None) -> dict[str, Any] | None:
        import copy
        from typing import cast

        params = copy.deepcopy(self.parameters)
        if not params or "properties" not in params:
            return params
        properties = cast("dict[str, Any]", params["properties"])
        _strip_chart_schema_for_doc_type(properties, doc_type)
        return params

    def validate(self, *, doc_type: str | None = None, **kwargs: Any) -> tuple[Literal[False], str] | tuple[Literal[True], None]:
        ok, err = super().validate(doc_type=doc_type, **kwargs)
        if not ok:
            return False, err or "invalid parameters"
        return UpsertChart().validate(doc_type=doc_type, **kwargs)

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        action = kwargs.get("action")
        if not action:
            return self._tool_error("Action parameter is required.", code="MISSING_PARAMETER")

        if action == "list":
            return ListCharts().execute(ctx, **kwargs)
        elif action == "get_info":
            if "name" not in kwargs:
                return self._tool_error("name parameter is required for action='get_info'.")
            return GetChartInfo().execute(ctx, **kwargs)
        elif action == "create":
            if "chart_type" not in kwargs:
                return self._tool_error("chart_type parameter is required for action='create'.")
            return UpsertChart().execute(ctx, **kwargs)
        elif action == "edit":
            if "name" not in kwargs:
                return self._tool_error("name parameter is required for action='edit'.")
            return UpsertChart().execute(ctx, **kwargs)
        elif action == "delete":
            if "name" not in kwargs:
                return self._tool_error("name parameter is required for action='delete'.")
            return DeleteChart().execute(ctx, **kwargs)
        else:
            return self._tool_error(f"Unsupported action: '{action}'")
