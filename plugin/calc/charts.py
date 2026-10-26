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
"""Calc chart management tools: list, info, create, edit, delete.
Enhanced to support Writer and Draw documents, 3D, stacking, and rich properties.
Recognized chart CLSIDs include classic OLE, SO3_SCH_CLASSID_60, LibreOffice OLE
embed IDs, and the runtime Writer draw-page shape id (12d37028-0b55-463d-863a-211463e2c59f,
from native test assertions rather than the factory CLSID).
"""
from __future__ import annotations

import logging
import math
import time
from typing import TYPE_CHECKING, Any, Callable, Iterator, Literal, NamedTuple, cast

from plugin.doc.visual_helpers import parse_color_to_uno_int as _parse_color
from plugin.framework.tool import ToolBaseDummy

if TYPE_CHECKING:
    from plugin.framework.tool import ToolContext

from plugin.calc.address_utils import format_range_address, parse_address, split_sheet_prefix
from plugin.calc.base import ToolCalcChartBase
from plugin.calc.bridge import CalcBridge
from plugin.framework.constants import ALL_DOCUMENT_SERVICES
from plugin.framework.errors import DocumentDisposedError, is_disposed_exception, is_document_disposed, is_real_disposal, is_tool_document_disposed
from plugin.framework.uno_context import uno_same
import uno

_MAX_CHART_DATA_CELLS = 10_000


class ChartRef(NamedTuple):
    host: Any
    parent: Any
    kind: Literal["calc_sheet", "writer_embed", "writer_draw_page", "draw_page"]
    page_index: int | None = None


class ChartLookup(NamedTuple):
    ref: ChartRef | None = None
    error: str | None = None


def supportsService(obj: Any, service_name: str) -> bool:
    """Helper to check if a UNO object supports a service."""
    if obj is None or not hasattr(obj, "supportsService"):
        return False
    try:
        return obj.supportsService(service_name)
    except Exception as exc:
        # Bugfix: a closed document raises DisposedException from supportsService, and this
        # except turned that into a missing chart or an empty list.
        # Re-raise real disposal so execute_safe reports DOCUMENT_DISPOSED.
        # Any other failure stays on the existing error path.
        if is_real_disposal(exc):
            raise
        return False


log = logging.getLogger("writeragent.calc")

# Chart CLSID: classic OLE chart (Calc sheet charts, Writer TextEmbeddedObject)
# Uppercase is often more compatible with older OLE registries
CHART_CLSID = "12DCAE36-07DA-43C1-9C17-56A938C64445"
# Draw/Impress OLE2Shape: add shape to page first, then set CLSID (OOo wiki sample)
CHART_CLSID_DRAW_OLE = "12DCAE26-281F-416F-A234-C3086127382E"
_OLE2_SHAPE = "com.sun.star.drawing.OLE2Shape"

# Recognized chart CLSIDs across Calc, Writer, and Draw.
# 12d37028-... is the runtime Writer shape CLSID asserted in native tests.
# d415cd93-... and 0dd0a57f-... are OLE embed IDs mapped to the chart class in LibreOffice.
RECOGNIZED_CHART_CLSIDS: frozenset[str] = frozenset({
    CHART_CLSID.lower(),
    CHART_CLSID_DRAW_OLE.lower(),
    "12d37028-0b55-463d-863a-211463e2c59f",
    "d415cd93-35c4-4c6f-819d-a664a1c813ae",
    "0dd0a57f-cf3b-4fd2-bda4-9442719b2a73",
})


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
    return c in RECOGNIZED_CHART_CLSIDS


def _is_chart_host(host: Any, *, is_embed: bool = False) -> bool:
    """True when *host* is a valid chart object."""
    if host is None:
        return False
    try:
        raw = getattr(host, "CLSID", None)
    except Exception as exc:
        if is_real_disposal(exc):
            raise
        raw = None

    if _normalize_clsid_value(raw).strip():
        return _is_chart_clsid(raw)

    # Missing CLSID: Writer embeds fall back to getDiagram() check
    if is_embed:
        try:
            chart_doc = _chart_document_from_host(host)
            if chart_doc is not None and hasattr(chart_doc, "getDiagram"):
                return chart_doc.getDiagram() is not None
        except Exception as exc:
            if is_real_disposal(exc):
                raise
            pass
    return False


def _writer_embed_is_chart(host: Any) -> bool:
    """True for Writer TextEmbeddedObject charts: CLSID match or embedded chart model."""
    return _is_chart_host(host, is_embed=True)


def _is_chart_shape(shape: Any, shape_type: str | None | Exception) -> bool:
    if isinstance(shape_type, Exception):
        raise shape_type
    return shape_type == _OLE2_SHAPE and _is_chart_host(shape, is_embed=False)


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
        # Bugfix: a closed document raises DisposedException from the chart host, and this
        # except turned that into a missing chart.
        # Re-raise real disposal so execute_safe reports DOCUMENT_DISPOSED.
        # Any other failure stays on the existing error path.
        if is_real_disposal(e):
            raise
        log.debug("_chart_document_from_host getEmbeddedObject failed: %s", e)

    # 2. Try Model/Component properties (Standard for Shapes)
    try:
        m = getattr(host, "Model", None)
        if m is not None:
            return m
        c = getattr(host, "Component", None)
        if c is not None:
            return c
    except Exception as exc:
        if is_real_disposal(exc):
            raise
        pass

    return None


def _strip_chart_schema_for_doc_type(properties: dict[str, Any], doc_type: str | None) -> None:
    """Drop fields that are invalid for *doc_type* (in-place)."""
    if doc_type == "calc":
        properties.pop("headers", None)
        properties.pop("rows", None)
        properties.pop("page", None)
        if "position" in properties:
            properties["position"] = {
                "type": "string",
                "description": "Cell address for chart placement (Calc only, e.g. 'A1').",
            }
    elif doc_type in ("writer", "draw", "impress"):
        properties.pop("data_range", None)
        properties.pop("sheet", None)
        properties.pop("has_header", None)
        if doc_type == "writer":
            properties.pop("page", None)
            if "position" in properties:
                properties["position"] = {
                    "type": "string",
                    "description": "Insertion position: 'cursor' to insert at view cursor, or 'end' (default) to insert at end of document.",
                }
        else:
            properties.pop("position", None)


def _chart_parameters(base_schema: dict[str, Any] | None, doc_type: str | None) -> dict[str, Any] | None:
    """Return a deep copy of *base_schema* stripped for *doc_type*."""
    if not base_schema or "properties" not in base_schema:
        return base_schema
    import copy

    params = copy.deepcopy(base_schema)
    properties = cast("dict[str, Any]", params["properties"])
    _strip_chart_schema_for_doc_type(properties, doc_type)
    return params


def _unique_chart_name(existing: set[str], stem: str = "Chart_") -> str:
    """Find the lowest unused indexed name."""
    idx = 0
    while f"{stem}{idx}" in existing:
        idx += 1
    return f"{stem}{idx}"


def _listed_chart_name(shape_or_name: Any) -> str:
    # Tip: a made-up Chart_{page}_{index} is not the shape's name, so get/edit/delete cannot find it.
    name = getattr(shape_or_name, "Name", shape_or_name)
    if isinstance(name, str) and name:
        return name
    return ""


def _validate_headers_and_rows(headers: Any, rows: Any) -> tuple[bool, str | None]:
    """Validate headers and rows for programmatic chart creation."""
    if not isinstance(headers, (list, tuple)):
        return False, "Parameter 'headers' must be an array"
    if len(headers) < 2:
        return False, "Parameter 'headers' must contain at least 2 elements"
    for idx, h in enumerate(headers):
        if isinstance(h, bool) or not isinstance(h, (str, int, float)):
            return False, f"Header at index {idx} must be a string or number, not {type(h).__name__}"

    if not isinstance(rows, (list, tuple)):
        return False, "Parameter 'rows' must be an array"
    if not rows:
        return False, "Parameter 'rows' must contain at least one row"

    if len(rows) * len(headers) > _MAX_CHART_DATA_CELLS:
        return False, f"Chart data size ({len(rows)} rows x {len(headers)} columns = {len(rows) * len(headers)} cells) exceeds limit of {_MAX_CHART_DATA_CELLS} cells"

    expected_len = len(headers)
    for r_idx, r in enumerate(rows):
        if not isinstance(r, (list, tuple)):
            return False, f"Row {r_idx} must be an array"
        if len(r) != expected_len:
            return False, f"Row {r_idx} length ({len(r)}) does not match headers length ({expected_len})"
        cat = r[0]
        if cat is None or isinstance(cat, bool) or not isinstance(cat, (str, int, float)):
            return False, f"Category label at row {r_idx} must be a string or number"
        for c_idx, val in enumerate(r[1:], start=1):
            if val is None:
                return False, f"Value at row {r_idx}, column {c_idx} cannot be None"
            if isinstance(val, bool):
                return False, f"Value at row {r_idx}, column {c_idx} cannot be boolean"
            if isinstance(val, str) and not val.strip():
                return False, f"Value at row {r_idx}, column {c_idx} cannot be blank"
            try:
                num = float(val)
                if not math.isfinite(num):
                    return False, f"Value at row {r_idx}, column {c_idx} ({val!r}) must be a finite number"
            except (ValueError, TypeError):
                return False, f"Value at row {r_idx}, column {c_idx} ({val!r}) must be a number"

    return True, None


def _validate_chart_action(kwargs: dict[str, Any], doc_type: str | None = None) -> tuple[Literal[False], str] | tuple[Literal[True], None]:
    """Validate parameters for chart create and edit actions."""
    action = kwargs.get("action")
    if action == "create":
        if not kwargs.get("chart_type"):
            return False, "Parameter 'chart_type' is required when action is 'create'"
        if doc_type == "calc":
            if not kwargs.get("data_range"):
                return False, "Parameter 'data_range' is required when action is 'create' in Calc"
        else:
            headers = kwargs.get("headers")
            rows = kwargs.get("rows")
            if not headers or not rows:
                return False, "Both 'headers' and 'rows' are required to create a chart in Writer or Draw/Impress"
            ok, err = _validate_headers_and_rows(headers, rows)
            if not ok:
                return False, err or "Invalid headers or rows"
    elif action == "edit":
        if "name" not in kwargs:
            return False, "Parameter 'name' is required when action is 'edit'"
        headers = kwargs.get("headers")
        rows = kwargs.get("rows")
        if headers is not None or rows is not None:
            if not headers or not rows:
                return False, "Both 'headers' and 'rows' are required when updating chart data"
            ok, err = _validate_headers_and_rows(headers, rows)
            if not ok:
                return False, err or "Invalid headers or rows"

    if doc_type == "writer" and "position" in kwargs:
        pos = kwargs["position"]
        if pos not in ("cursor", "end"):
            return False, f"Invalid position '{pos}' for Writer chart: must be 'cursor' or 'end'"

    return True, None


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
        log.debug("chart event pump failed", exc_info=True)
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
CHART_PROPERTIES: dict[str, Any] = {
    "sheet": {"type": "string", "description": "Sheet name where the chart should be placed (Calc only, defaults to active sheet)."},
    "page": {"type": "integer", "description": "Zero-based page/slide index (Draw/Impress only; omitted page means the current page, otherwise page 0)."},
    "data_range": {"type": "string", "description": "Cell range for chart data (Calc only, required for create, e.g. 'A1:B10')."},
    "has_header": {"type": "boolean", "description": "Whether the first row/column of data_range contains header and category labels (Calc only, defaults to true)."},
    "headers": {"type": "array", "items": {"type": ["string", "number"]}, "description": "Category/series column headers (Writer/Draw only, required for create, e.g. ['Month', 'Sales', 'Expenses'])."},
    "rows": {
        "type": "array",
        "items": {"type": "array", "description": "Row containing category label as first element, followed by numeric values."},
        "description": "2D array of category labels and values (Writer/Draw only, required for create, e.g. [['Jan', 100, 80], ['Feb', 150, 110]]).",
    },
    "chart_type": {"type": "string", "enum": list(CHART_SERVICE_MAP.keys()), "description": "Type of chart to create or update to (required for create)."},
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
}


def _chart_step(warnings: list[str], label: str, fn: Callable[[], None]) -> None:
    """Run a styling step, recording non-fatal failures as warnings while re-raising disposal."""
    try:
        fn()
    except Exception as exc:
        if is_real_disposal(exc):
            raise
        msg = _format_chart_exception_msg(exc)
        warnings.append(f"Failed to set {label}: {msg}")
        log.warning("Setting chart %s failed: %s", label, exc)


def _set_diagram_flag(diagram: Any, prop: str, value: Any, warnings: list[str], debug: str) -> None:
    if value is None:
        return
    if hasattr(diagram, prop):
        def _apply() -> None:
            setattr(diagram, prop, value)
            log.debug(debug, value)
        _chart_step(warnings, prop, _apply)
    else:
        warnings.append(f"Diagram does not support {prop}")


def _set_axis_title(diagram: Any, axis: str, value: Any, warnings: list[str]) -> None:
    if value is None or not hasattr(diagram, f"Has{axis}AxisTitle"):
        return

    def _set_axis() -> None:
        setattr(diagram, f"Has{axis}AxisTitle", True)
        getter = getattr(diagram, f"get{axis}AxisTitle")
        written = _axis_title_shape_string(getter(), value)
        if written is not None:
            log.debug("Set %s axis title: '%s'", axis, written)

    _chart_step(warnings, f"{axis} axis title", _set_axis)


def _apply_chart_styling(chart_doc: Any, **kwargs: Any) -> list[str]:
    """Apply enhanced styling properties to a chart document."""
    warnings: list[str] = []
    log.info(
        "Applying chart styling: chart_type=%s, has_title=%s, series_colors=%d, rows=%d, cols=%d",
        kwargs.get("chart_type"),
        bool(kwargs.get("title")),
        len(kwargs.get("colors") or []),
        len(kwargs.get("rows") or []),
        len(kwargs.get("headers") or []),
    )
    log.debug("Chart styling kwargs: %s", {k: v for k, v in kwargs.items() if k not in ("data_range", "rows")})
    if kwargs.get("rows"):
        log.debug("Chart rows first item: %r", kwargs["rows"][0] if kwargs["rows"] else None)

    diagram = chart_doc.getDiagram()
    if not diagram:
        warnings.append("No diagram found on chart document.")
        log.warning("No diagram found on chart document.")
        return warnings

    # 1. 3D Mode, Stacking, Percent
    _set_diagram_flag(diagram, "Dim3D", kwargs.get("is_3d"), warnings, "Set diagram 3D mode: %s")
    _set_diagram_flag(diagram, "Stacked", kwargs.get("stacked"), warnings, "Set diagram stacked mode: %s")
    _set_diagram_flag(diagram, "Percent", kwargs.get("percent"), warnings, "Set diagram percent stacked: %s")

    # 2. Bar/Column Orientation
    chart_type = kwargs.get("chart_type")
    if chart_type in ["bar", "column"]:
        if hasattr(diagram, "Vertical"):
            def _set_vertical() -> None:
                diagram.Vertical = chart_type == "bar"
                log.debug("Set diagram orientation: vertical=%s", diagram.Vertical)
            _chart_step(warnings, "Vertical", _set_vertical)
        else:
            warnings.append("Diagram does not support Vertical")

    # 3. Titles
    def _apply_chart_title(prop: str, getter: str, text: str, label: str) -> None:
        def _set() -> None:
            setattr(chart_doc, prop, True)
            getattr(chart_doc, getter)().String = text
            log.debug("Set chart %s: '%s'", label, text)
        _chart_step(warnings, label, _set)

    title = kwargs.get("title")
    if title is not None:
        _apply_chart_title("HasMainTitle", "getTitle", title, "main title")

    subtitle = kwargs.get("subtitle")
    if subtitle is not None:
        _apply_chart_title("HasSubTitle", "getSubTitle", subtitle, "subtitle")

    _set_axis_title(diagram, "X", kwargs.get("x_axis_title"), warnings)
    _set_axis_title(diagram, "Y", kwargs.get("y_axis_title"), warnings)

    # 5. Legend
    # offapi ChartLegend.Alignment is ChartLegendPosition (NONE/LEFT/TOP/RIGHT/BOTTOM).
    # There is no ChartLegendAlignment type.
    has_legend = kwargs.get("has_legend")
    if has_legend is not None:
        def _set_legend_vis() -> None:
            chart_doc.HasLegend = has_legend
            log.debug("Set chart legend visibility: %s", has_legend)
        _chart_step(warnings, "legend visibility", _set_legend_vis)

    legend_pos = kwargs.get("legend_position")
    if legend_pos:
        if legend_pos == "none":
            def _hide_legend() -> None:
                chart_doc.HasLegend = False
                log.debug("Set legend position: none")
            _chart_step(warnings, "legend position", _hide_legend)
        elif legend_pos in _LEGEND_POSITION_MEMBER:
            if has_legend is False:
                msg = f"legend_position '{legend_pos}' ignored because has_legend is false"
                warnings.append(msg)
                log.warning("%s", msg)
            else:
                def _set_legend_pos() -> None:
                    # Setting legend_position enables the legend like title setters enable titles;
                    # previously a False HasLegend silently skipped placement.
                    chart_doc.HasLegend = True
                    member = _LEGEND_POSITION_MEMBER[legend_pos]
                    # Tip: Alignment takes ChartLegendPosition; ChartLegendAlignment is not in the API.
                    chart_doc.getLegend().Alignment = uno.Enum(_CHART_LEGEND_POSITION_ENUM, member)
                    log.debug("Set legend position: %s", legend_pos)
                _chart_step(warnings, "legend position", _set_legend_pos)
        else:
            msg = f"Invalid legend_position: '{legend_pos}'"
            warnings.append(msg)
            log.warning("%s", msg)

    # 6. Background Color
    bg_color = kwargs.get("bg_color")
    if bg_color:
        parsed_bg = _parse_color(bg_color)
        if parsed_bg is not None:
            def _set_bg() -> None:
                bg = chart_doc.getPageBackground()
                bg.setPropertyValue("FillStyle", uno.Enum("com.sun.star.drawing.FillStyle", "SOLID"))
                bg.setPropertyValue("FillColor", parsed_bg)
                log.info("Set chart background color: %s (RGB %d)", bg_color, parsed_bg)
            _chart_step(warnings, "background color", _set_bg)
        else:
            warnings.append(f"Invalid bg_color: '{bg_color}'")
            log.warning("Invalid bg_color ignored: '%s'", bg_color)

    # 7. Series Colors (one color per series/bar in the chart)
    colors = kwargs.get("colors")
    if colors:
        parsed_colors = []
        for c in colors:
            parsed = _parse_color(c)
            if parsed is not None:
                parsed_colors.append(parsed)
            else:
                warnings.append(f"Invalid series color: '{c}'")
                log.warning("Invalid series color ignored: '%s'", c)

        if parsed_colors:
            try:
                diag = chart_doc.getFirstDiagram()
                if diag:
                    coords = diag.getCoordinateSystems()
                    series_count = 0
                    # Counter spans all chart types; the old enumerate restarted per type repeating colors[0].
                    series_idx = 0
                    for coord in coords:
                        ctypes = coord.getChartTypes()
                        for ctype in ctypes:
                            series_list = ctype.getDataSeries()
                            for s in series_list:
                                idx = series_idx
                                series_idx += 1
                                color_val = parsed_colors[idx % len(parsed_colors)]
                                try:
                                    prop_info = s.getPropertySetInfo()
                                    for prop in ["Color", "FillColor", "LineColor"]:
                                        if prop_info.hasPropertyByName(prop):
                                            s.setPropertyValue(prop, color_val)
                                except Exception as exc:
                                    # Skip a disposed series and keep coloring the rest.
                                    if is_disposed_exception(exc):
                                        log.debug("Skipping disposed chart series %d", idx)
                                        continue
                                    raise
                                log.debug("Set data series %d color to RGB %d", idx, color_val)
                                series_count += 1
                    log.info("Successfully styled %d chart data series with %d colors", series_count, len(parsed_colors))
                else:
                    warnings.append("Could not retrieve first diagram to apply series colors")
                    log.warning("Could not retrieve first diagram using getFirstDiagram")
            except Exception as exc:
                if is_real_disposal(exc):
                    raise
                warnings.append(f"Failed to set series colors: {_format_chart_exception_msg(exc)}")
                log.warning("Failed to set data series colors: %s", exc)

    return warnings


def _apply_chart_data_arrays(chart_doc: Any, headers: Any, rows: Any) -> None:
    """Set chart data programmatically via XChartDataArray for Writer/Draw."""
    if not headers or not rows:
        return

    ok, err = _validate_headers_and_rows(headers, rows)
    if not ok:
        raise ValueError(err or "Invalid headers or rows")

    chart_data = chart_doc.getData()
    if not chart_data:
        raise ValueError("No chart data object found on chart document.")

    row_desc = tuple(str(r[0]) for r in rows)
    # headers[0] is the category column title; series names start at index 1
    # 2. Process headers to extract series names (column descriptions)
    col_desc = tuple(str(h) for h in headers[1:])
    final_values = tuple(tuple(float(v) for v in r[1:]) for r in rows)

    chart_data.setRowDescriptions(row_desc)
    chart_data.setColumnDescriptions(col_desc)
    chart_data.setData(final_values)
    log.info("Applied chart data arrays: rows=%d, cols=%d", len(row_desc), len(col_desc))
    log.debug("Chart data row 0: desc=%r, values=%r", row_desc[0] if row_desc else None, final_values[0] if final_values else None)


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


def _remove_writer_chart(doc: Any, chart_obj: Any, name: str) -> None:
    """Remove a Writer chart from the text that anchors it (body, frame, or cell).

    doc.getText() only holds body anchors, so a chart in a frame or table cell
    stayed in the document while delete reported success. The embedded objects
    container is the fallback used by create rollback.
    """
    try:
        chart_obj.getAnchor().getText().removeTextContent(chart_obj)
        return
    except Exception as exc:
        if is_real_disposal(exc):
            raise
        log.debug("chart anchor removal failed for '%s'; using embedded objects", name, exc_info=True)
    objects = doc.getEmbeddedObjects()
    if not objects.hasByName(name):
        raise ValueError(f"Chart '{name}' is not in the embedded objects container.")
    objects.removeByName(name)


def _drop_writer_chart_insert(doc: Any, chart_obj: Any, name: str) -> None:
    """Remove a Writer chart embedded before a later create step failed."""
    _drop_failed_chart_insert(lambda: _remove_writer_chart(doc, chart_obj, name), name)


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


def _chart_list_failure(doc: Any, exc: Exception, errors: list[dict[str, Any]], **where: Any) -> None:
    """Record one chart-list read failure, or re-raise when *doc* is closed.

    Calc used to re-raise only is_disposed_exception and then return status ok
    with the charts appended before the failure. Draw/Writer skipped disposal
    and raised other errors, so one bad shape aborted the list. A closed
    document propagates. A live-document failure is appended to *errors*.
    """
    if is_tool_document_disposed(exc, doc):
        raise exc
    errors.append({**where, "message": _format_chart_exception_msg(exc)})


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


def _iter_page_shapes(container: Any, doc: Any = None, parent: Any = None) -> Iterator[tuple[Any, Any, str | None | Exception]]:
    """Yield (shape, parent, shape_type) for shapes in a draw page or group shape, descending into nested groups."""
    # Tip: a disposed shape on a live page is skipped so later shapes still list; on a closed
    # document re-raise, or ListCharts would return status ok with a partial list.
    try:
        count = container.getCount()
    except Exception as exc:
        if is_disposed_exception(exc):
            if doc is not None and is_document_disposed(doc):
                raise
            return
        raise

    current_parent = parent if parent is not None else container

    for i in range(count):
        try:
            shape = container.getByIndex(i)
        except Exception as exc:
            if is_disposed_exception(exc):
                if doc is not None and is_document_disposed(doc):
                    raise
                continue
            raise

        try:
            shape_type = shape.getShapeType()
        except Exception as exc:
            if is_disposed_exception(exc):
                if doc is not None and is_document_disposed(doc):
                    raise
                continue
            shape_type = exc

        yield shape, current_parent, shape_type

        if shape_type and not isinstance(shape_type, Exception) and "GroupShape" in str(shape_type):
            yield from _iter_page_shapes(shape, doc, parent=shape)


def _named_ole_chart_ref(
    shape: Any,
    parent: Any,
    shape_type: Any,
    name: str,
    kind: Literal["writer_draw_page", "draw_page"],
    page_index: int | None = None,
) -> ChartRef | None:
    if isinstance(shape_type, Exception):
        return None
    try:
        if shape_type != _OLE2_SHAPE:
            return None
        listed_name = _listed_chart_name(shape)
        if listed_name and listed_name == name and _is_chart_host(shape, is_embed=False):
            return ChartRef(shape, parent, kind, page_index)
    except Exception as exc:
        if is_disposed_exception(exc):
            return None
        raise
    return None


def _lookup_chart(
    doc: Any,
    name: str,
    *,
    sheet: str | None = None,
    page: int | None = None,
) -> ChartLookup:
    """Resolve a chart by name across Calc, Writer, or Draw/Impress with direct parent."""
    if supportsService(doc, "com.sun.star.sheet.SpreadsheetDocument"):
        chart_obj, found_sheet = _find_calc_chart_and_sheet(doc, name, sheet)
        if chart_obj is not None:
            return ChartLookup(ref=ChartRef(chart_obj, found_sheet, "calc_sheet", None))
        return ChartLookup()
    elif supportsService(doc, "com.sun.star.text.TextDocument"):
        try:
            objects = doc.getEmbeddedObjects()
            if objects.hasByName(name):
                obj = objects.getByName(name)
                if _writer_embed_is_chart(obj):
                    return ChartLookup(ref=ChartRef(obj, objects, "writer_embed", None))
        except Exception as exc:
            if is_real_disposal(exc):
                raise
        page_obj = doc.getDrawPage()
        for shape, parent, shape_type in _iter_page_shapes(page_obj, doc):
            ref = _named_ole_chart_ref(shape, parent, shape_type, name, "writer_draw_page")
            if ref is not None:
                return ChartLookup(ref=ref)
        return ChartLookup()
    elif supportsService(doc, "com.sun.star.drawing.DrawingDocument") or supportsService(doc, "com.sun.star.presentation.PresentationDocument"):
        draw_pages = doc.getDrawPages()
        count = draw_pages.getCount()
        if page is not None:
            if page < 0 or page >= count:
                return ChartLookup(error=f"Page index {page} is out of range (document has {count} pages).")
            target_page = draw_pages.getByIndex(page)
            for shape, parent, shape_type in _iter_page_shapes(target_page, doc):
                ref = _named_ole_chart_ref(shape, parent, shape_type, name, "draw_page", page)
                if ref is not None:
                    return ChartLookup(ref=ref)
            return ChartLookup()

        matches: list[ChartRef] = []
        for i in range(count):
            pg = draw_pages.getByIndex(i)
            for shape, parent, shape_type in _iter_page_shapes(pg, doc):
                ref = _named_ole_chart_ref(shape, parent, shape_type, name, "draw_page", i)
                if ref is not None:
                    matches.append(ref)
        if len(matches) == 1:
            return ChartLookup(ref=matches[0])
        if len(matches) > 1:
            indexes_str = ", ".join(str(m.page_index) for m in matches if m.page_index is not None)
            return ChartLookup(error=f"Chart '{name}' found on multiple pages ({indexes_str}). Please specify 'page'.")
        return ChartLookup()
    return ChartLookup()


def _require_chart(
    tool: Any,
    doc: Any,
    kwargs: dict[str, Any],
) -> tuple[str, ChartRef] | dict[str, Any]:
    chart_name = kwargs["name"]
    lookup = _lookup_chart(doc, chart_name, sheet=kwargs.get("sheet"), page=kwargs.get("page"))
    if lookup.error:
        return tool._tool_error(lookup.error, code="CHART_AMBIGUOUS")
    if not lookup.ref:
        return tool._tool_error(f"Chart '{chart_name}' not found.")
    return chart_name, lookup.ref


class ListCharts(ToolBaseDummy):
    """List charts; Dummy backend for ``ManageCharts`` action=list."""

    name: str | None = "list_charts"
    intent: str | None = "navigate"
    description: str = "List all charts in the current context (active sheet, document, or slide) with name, title, and type."
    parameters: dict[str, Any] | None = {"type": "object", "properties": {}, "required": []}
    uno_services: list[str] | None = list(ALL_DOCUMENT_SERVICES)

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        doc = ctx.doc
        # supportsService swallows DisposedException, so a closed document
        # matched no branch and list_charts returned status ok with charts=[].
        # Probe first and surface disposal instead of an empty success.
        if is_document_disposed(doc):
            raise DocumentDisposedError("Document was closed or disposed by LibreOffice", object_type="document")
        result = []
        errors: list[dict[str, Any]] = []

        if supportsService(doc, "com.sun.star.sheet.SpreadsheetDocument"):
            try:
                sheets = doc.getSheets()
                sheets_count = sheets.getCount()
            except Exception as exc:
                # Tip: real disposal propagates even when the document probe still answers.
                if is_tool_document_disposed(exc, doc):
                    raise exc
                return self._tool_error("Could not list charts: %s" % _format_chart_exception_msg(exc))
            for i in range(sheets_count):
                sheet_name = None
                try:
                    sheet = sheets.getByIndex(i)
                    sheet_name = sheet.getName()
                    charts = sheet.getCharts()
                    names = charts.getElementNames()
                except Exception as exc:
                    _chart_list_failure(doc, exc, errors, sheet=sheet_name or i)
                    continue
                for name in names:
                    try:
                        chart_obj = charts.getByName(name)
                        result.append(self._get_summary(chart_obj, name, sheet_name=sheet_name))
                    except Exception as exc:
                        _chart_list_failure(doc, exc, errors, sheet=sheet_name, name=name)

        elif supportsService(doc, "com.sun.star.text.TextDocument"):
            embed_names = ()
            seen_names: set[str] = set()
            try:
                objects = doc.getEmbeddedObjects()
                embed_names = objects.getElementNames()
            except Exception as exc:
                _chart_list_failure(doc, exc, errors, source="embedded")
            else:
                for name in embed_names:
                    try:
                        obj = objects.getByName(name)
                        if _writer_embed_is_chart(obj):
                            result.append(self._get_summary(obj, name))
                            seen_names.add(name)
                    except Exception as exc:
                        _chart_list_failure(doc, exc, errors, source="embedded", name=name)
            try:
                page = doc.getDrawPage()
                for j, (shape, _parent, shape_type) in enumerate(_iter_page_shapes(page, doc)):
                    try:
                        if not _is_chart_shape(shape, shape_type):
                            continue
                        nm = _listed_chart_name(shape)
                        if nm and nm in seen_names:
                            continue
                        if nm:
                            seen_names.add(nm)
                        result.append(self._get_summary(shape, nm))
                    except Exception as exc:
                        _chart_list_failure(doc, exc, errors, source="draw_page", index=j)
            except Exception as exc:
                _chart_list_failure(doc, exc, errors, source="draw_page")

        elif supportsService(doc, "com.sun.star.drawing.DrawingDocument") or supportsService(doc, "com.sun.star.presentation.PresentationDocument"):
            try:
                pages = doc.getDrawPages()
                pages_count = pages.getCount()
            except Exception as exc:
                # Tip: real disposal propagates even when the document probe still answers.
                if is_tool_document_disposed(exc, doc):
                    raise exc
                return self._tool_error("Could not list charts: %s" % _format_chart_exception_msg(exc))
            for i in range(pages_count):
                try:
                    page = pages.getByIndex(i)
                except Exception as exc:
                    _chart_list_failure(doc, exc, errors, page=i)
                    continue
                try:
                    for j, (shape, _parent, shape_type) in enumerate(_iter_page_shapes(page, doc)):
                        try:
                            if not _is_chart_shape(shape, shape_type):
                                continue
                            summary = self._get_summary(shape, _listed_chart_name(shape))
                            summary["page"] = i
                            result.append(summary)
                        except Exception as exc:
                            _chart_list_failure(doc, exc, errors, page=i, index=j)
                except Exception as exc:
                    _chart_list_failure(doc, exc, errors, page=i)

        out: dict[str, Any] = {"status": "ok", "charts": result, "count": len(result)}
        if errors:
            out["errors"] = errors
        return out

    def _get_summary(self, chart_obj: Any, name: str, sheet_name: str | None = None) -> dict[str, Any]:
        entry = {"name": name}
        if sheet_name:
            entry["sheet_name"] = sheet_name
        try:
            chart_doc = _chart_document_from_host(chart_obj)
            if chart_doc:
                entry["title"] = chart_doc.getTitle().String if chart_doc.HasMainTitle else ""
                entry["diagram_type"] = chart_doc.getDiagram().getDiagramType()
        except Exception as exc:
            # Title and diagram type are optional. Swallowing DisposedException
            # here still returned the chart and list_charts finished status ok
            # after the document closed. Real disposal propagates; the caller
            # re-raises only when the document itself is disposed.
            if is_real_disposal(exc):
                raise
            log.debug("list_charts summary failed for %s: %s", name, exc)
        return entry


class GetChartInfo(ToolBaseDummy):
    """Chart details; Dummy backend for ``ManageCharts`` action=get_info."""

    name: str | None = "get_chart_info"
    intent: str | None = "navigate"
    description: str = "Get detailed info about a chart: type, title, ranges (if Calc), axis titles, and legend properties."
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Chart name (from list_charts)."},
            "page": {"type": "integer", "description": "Zero-based page/slide index (Draw/Impress only)."},
        },
        "required": ["name"],
    }
    uno_services: list[str] | None = ListCharts.uno_services

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        doc = ctx.doc
        target = _require_chart(self, doc, kwargs)
        if isinstance(target, dict):
            return target
        chart_name, ref = target

        chart_obj = ref.host

        info: dict[str, Any] = {"name": chart_name, "status": "ok"}

        # Data ranges (Calc only)
        if hasattr(chart_obj, "getRanges"):
            try:
                info["data_ranges"] = [format_range_address(r) for r in chart_obj.getRanges()]
            except Exception as exc:
                # Bugfix: a closed document raises DisposedException from the chart info read, and this
                # except turned that into a partial chart.
                # Re-raise real disposal so execute_safe reports DOCUMENT_DISPOSED.
                # Any other failure stays on the existing error path.
                if is_real_disposal(exc):
                    raise
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
                        except Exception as exc:
                            if is_real_disposal(exc):
                                raise
                            pass
                    if hasattr(diagram, "HasYAxisTitle") and diagram.HasYAxisTitle:
                        try:
                            ys = _axis_title_shape_string(diagram.getYAxisTitle(), None)
                            if ys is not None:
                                info["y_axis_title"] = ys
                        except Exception as exc:
                            if is_real_disposal(exc):
                                raise
                            pass

        except Exception as e:
            if is_real_disposal(e):
                raise
            log.debug("get_chart_info error: %s", e)

        return info


def _writer_body_has_no_text(text: Any) -> bool:
    """Return True if the text body has no content (empty or single empty paragraph)."""
    enum = text.createEnumeration()
    if not enum.hasMoreElements():
        return True
    first = enum.nextElement()
    if enum.hasMoreElements():
        return False
    if hasattr(first, "getString"):
        return first.getString() == ""
    return False


class UpsertChart(ToolBaseDummy):
    """Create/edit chart; Dummy backend for ``ManageCharts`` action=create|edit."""

    name: str | None = "upsert_chart"
    intent: str | None = "edit"
    description: str = "Creates a new chart or modifies an existing chart on a sheet, document, or slide."
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["create", "edit"], "description": "Action to perform: 'create' a new chart, or 'edit' an existing one."},
            "name": {"type": "string", "description": "Name of the chart to edit (required for action='edit'); create always uses the next free 'Chart_N' name."},
            **CHART_PROPERTIES,
        },
        "required": ["action"],
    }
    uno_services: list[str] | None = ListCharts.uno_services
    is_mutation: bool | None = True

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
            assert chart_type in CHART_SERVICE_MAP, chart_type
            chart_service = CHART_SERVICE_MAP[chart_type]
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
                # Bugfix: a closed document raises DisposedException during chart create, and this
                # except turned that into CHART_CREATE_ERROR.
                # Re-raise real disposal so execute_safe reports DOCUMENT_DISPOSED.
                # Any other failure stays on the existing error path.
                if is_real_disposal(e):
                    raise
                msg = _format_chart_exception_msg(e)
                log.exception("Chart creation failed")
                return self._tool_error(f"Failed to create chart: {msg}", code="CHART_CREATE_ERROR")

        elif action == "edit":
            target = _require_chart(self, doc, kwargs)
            if isinstance(target, dict):
                return target
            chart_name, ref = target

            chart_obj = ref.host

            chart_doc = _chart_document_from_host(chart_obj)
            if not chart_doc:
                return self._tool_error("Cannot access chart content.")

            warnings: list[str] = []
            if kwargs.get("position"):
                warnings.append("Chart placement is unchanged on edit.")

            if is_calc:
                data_range = kwargs.get("data_range")
                if data_range is not None:
                    if not hasattr(chart_obj, "setRanges"):
                        return self._tool_error("Chart object does not support setRanges.")
                    try:
                        bridge = CalcBridge(doc)
                        target_sheet = ref.parent if ref.parent is not None else bridge.get_active_sheet()
                        cell_range = bridge.get_cell_range(target_sheet, data_range)
                        addr = cell_range.getRangeAddress()
                        chart_obj.setRanges((addr,))
                    except Exception as e:
                        if is_real_disposal(e):
                            raise
                        msg = _format_chart_exception_msg(e)
                        log.exception("Setting chart data range failed")
                        return self._tool_error(f"Failed to set data_range: {msg}", code="CHART_EDIT_ERROR")

                if "has_header" in kwargs:
                    if not (hasattr(chart_obj, "setHasColumnHeaders") and hasattr(chart_obj, "setHasRowHeaders")):
                        return self._tool_error("Chart object does not support header flags.")
                    try:
                        has_hdr = bool(kwargs["has_header"])
                        chart_obj.setHasColumnHeaders(has_hdr)
                        chart_obj.setHasRowHeaders(has_hdr)
                    except Exception as e:
                        if is_real_disposal(e):
                            raise
                        msg = _format_chart_exception_msg(e)
                        log.exception("Setting chart headers failed")
                        return self._tool_error(f"Failed to set has_header: {msg}", code="CHART_EDIT_ERROR")

            try:
                headers = kwargs.get("headers")
                rows = kwargs.get("rows")
                if headers and rows:
                    _apply_chart_data_arrays(chart_doc, headers, rows)

                # If chart_type is provided, update the diagram first
                chart_type = kwargs.get("chart_type")
                if chart_type:
                    assert chart_type in CHART_SERVICE_MAP, chart_type
                    chart_doc.setDiagram(chart_doc.createInstance(CHART_SERVICE_MAP[chart_type]))

                styling_warnings = _apply_chart_styling(chart_doc, **kwargs)
                warnings.extend(styling_warnings)
            except Exception as e:
                if is_real_disposal(e):
                    raise
                msg = _format_chart_exception_msg(e)
                log.exception("Chart edit failed")
                return self._tool_error(f"Failed to edit chart: {msg}", code="CHART_EDIT_ERROR")

            out: dict[str, Any] = {"status": "ok", "name": chart_name, "message": "Chart updated."}
            if warnings:
                out["warnings"] = warnings
            return out

        return self._tool_error(f"Unsupported action: '{action}'")

    def _create_calc_chart(self, ctx: ToolContext, rect: Any, service: str, **kwargs: Any) -> dict[str, Any]:
        bridge = CalcBridge(ctx.doc)
        data_range = cast("str", kwargs.get("data_range"))
        # Missing data_range is rejected by ManageCharts.validate for calc create.

        target_sheet_name = kwargs.get("sheet")
        if target_sheet_name:
            try:
                sheet = bridge.get_sheet(target_sheet_name)
            except Exception as exc:
                # Bugfix: a closed document raises DisposedException from get_sheet, and this
                # except turned that into a missing sheet.
                # Re-raise real disposal so execute_safe reports DOCUMENT_DISPOSED.
                # Any other failure stays on the existing error path.
                if is_real_disposal(exc):
                    raise
                return self._tool_error(f"Sheet '{target_sheet_name}' not found in document.")
        else:
            sheet = bridge.get_active_sheet()

        cell_range = bridge.get_cell_range(sheet, data_range)
        addr = cell_range.getRangeAddress()

        pos_str = kwargs.get("position")
        if pos_str:
            pos_prefix, bare_pos = split_sheet_prefix(pos_str)
            target_name = target_sheet_name or sheet.getName()
            if pos_prefix is not None and pos_prefix != target_name:
                return self._tool_error(f"Position names sheet '{pos_prefix}' but placement sheet is '{target_name}'.")
            try:
                parse_address(bare_pos)
                cell_obj = sheet.getCellRangeByName(bare_pos)
                cell_pos = cell_obj.getPosition()
                cell_size = cell_obj.getSize()
                rect.X = cell_pos.X
                rect.Y = cell_pos.Y
                if cell_size.Width > 0 and cell_size.Height > 0:
                    rect.Width = cell_size.Width
                    rect.Height = cell_size.Height
            except Exception as exc:
                if is_real_disposal(exc):
                    raise
                return self._tool_error(f"Invalid position '{pos_str}': {_format_chart_exception_msg(exc)}")

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
        name = _unique_chart_name(existing_names)

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
            warnings = _apply_chart_styling(chart_doc, **kwargs)
        except Exception:
            # Insert already committed. Drop it so the error return is not an orphan chart.
            _drop_failed_chart_insert(lambda: charts.removeByName(name), name)
            raise
        # _process_events() causes a hang in tests
        out = {"status": "ok", "message": f"Chart '{name}' created on sheet '{sheet.getName()}'.", "name": name, "sheet": sheet.getName()}
        if warnings:
            out["warnings"] = warnings
        return out

    def _create_writer_chart(self, ctx: ToolContext, rect: Any, service: str, **kwargs: Any) -> dict[str, Any]:
        """Insert a chart as inline ``TextEmbeddedObject`` (Writer body text).
        Using a bounded wait and event pumping to ensure the embedded model is initialized.
        """
        doc = ctx.doc
        text = doc.getText()

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
        except Exception as exc:
            # Bugfix: a closed document raises DisposedException while creating a Writer chart, and this
            # except turned that into a chart configure or insert error.
            # Re-raise real disposal so execute_safe reports DOCUMENT_DISPOSED.
            # Any other failure stays on the existing error path.
            if is_real_disposal(exc):
                raise
            cursor = text.createTextCursor()
            try:
                cursor.gotoEnd(False)
            except Exception as exc:
                if is_real_disposal(exc):
                    raise
                pass

        # 2. Collect existing names across embeds and draw page
        existing_names: set[str] = set()
        try:
            embeds = doc.getEmbeddedObjects()
            if hasattr(embeds, "getElementNames"):
                existing_names.update(embeds.getElementNames())
        except Exception as exc:
            if is_real_disposal(exc):
                raise
        page = doc.getDrawPage()
        for s, _p, _st in _iter_page_shapes(page, doc):
            try:
                nm = getattr(s, "Name", None)
                if nm:
                    existing_names.add(nm)
            except Exception as exc:
                if is_disposed_exception(exc):
                    continue
                raise
        name = _unique_chart_name(existing_names)

        # We use plain createInstance for Writer; createInstanceWithArguments can be flaky for OLE
        chart_obj = doc.createInstance("com.sun.star.text.TextEmbeddedObject")
        if not chart_obj:
            return self._tool_error("Failed to create TextEmbeddedObject instance.")

        try:
            log.debug("TextEmbeddedObject Implementation: %s", chart_obj.getImplementationName())
        except Exception:
            log.debug("TextEmbeddedObject getImplementationName failed", exc_info=True)

        # CRITICAL: Match proven working pattern from plugin/writer/math/math_mml_convert.py
        chart_obj.CLSID = CHART_CLSID_DRAW_OLE.upper()
        from com.sun.star.text.TextContentAnchorType import AS_CHARACTER

        chart_obj.AnchorType = AS_CHARACTER

        # Try to set name before insertion
        try:
            chart_obj.Name = name
        except Exception:
            log.debug("set chart Name before insert failed", exc_info=True)

        log.info("Created and configured TextEmbeddedObject with CLSID: %s", chart_obj.CLSID)

        # 3. Insert into document
        try:
            # Ensure we are at a valid insertion point if the doc is empty
            if _writer_body_has_no_text(text):
                try:
                    PARAGRAPH_BREAK = uno.getConstantByName("com.sun.star.text.ControlCharacter.PARAGRAPH_BREAK")
                    text.insertControlCharacter(cursor, PARAGRAPH_BREAK, False)
                except Exception:
                    log.debug("insert PARAGRAPH_BREAK before chart failed", exc_info=True)

            text.insertTextContent(cursor, chart_obj, False)
            log.info("Successfully inserted chart object into text.")
        except Exception as e:
            if is_real_disposal(e):
                raise
            # Fallback: Try AT_PARAGRAPH
            log.debug("First insertion attempt failed (%s). Trying AT_PARAGRAPH anchor...", e)
            from com.sun.star.text.TextContentAnchorType import AT_PARAGRAPH

            chart_obj.AnchorType = AT_PARAGRAPH
            text.insertTextContent(cursor, chart_obj, False)
            log.info("Successfully inserted chart object with AT_PARAGRAPH.")

        # 4. Configure properties after insertion
        # Try to set name again if it failed before
        try:
            chart_obj.Name = name
        except Exception:
            log.debug("set chart Name after insert failed", exc_info=True)

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
                log.debug("chart Width/Height attribute fallback failed", exc_info=True)

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

        warnings: list[str] = []
        # 5. Configure Diagram
        if chart_doc:
            try:
                diagram = chart_doc.createInstance(service)
                if not diagram:
                    raise RuntimeError(f"Failed to create diagram instance for service: {service}")
                chart_doc.setDiagram(diagram)
                log.info("Set chart diagram: %s", service)
                headers = kwargs.get("headers")
                rows = kwargs.get("rows")
                if headers and rows:
                    _apply_chart_data_arrays(chart_doc, headers, rows)
                warnings = _apply_chart_styling(chart_doc, **kwargs)
            except Exception:
                # Diagram installation and styling must succeed or the embed is rolled back.
                _drop_writer_chart_insert(doc, chart_obj, name)
                raise
        else:
            _drop_writer_chart_insert(doc, chart_obj, name)
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
        out: dict[str, Any] = {"status": "ok", "message": f"Chart '{name}' inserted in Writer.", "name": name}
        if warnings:
            out["warnings"] = warnings
        return out

    def _create_draw_chart(self, ctx: ToolContext, rect: Any, service: str, **kwargs: Any) -> dict[str, Any]:
        doc = ctx.doc
        draw_pages = doc.getDrawPages()
        if draw_pages is None:
            return self._tool_error("No draw page or slide to insert chart.")
        count = draw_pages.getCount()
        page_arg = kwargs.get("page")
        page = None
        page_index: int = 0
        if isinstance(page_arg, int) and not isinstance(page_arg, bool):
            if page_arg < 0 or page_arg >= count:
                return self._tool_error(f"Page index {page_arg} is out of range (document has {count} pages).")
            page = draw_pages.getByIndex(page_arg)
            page_index = page_arg
        else:
            controller = doc.getCurrentController()
            if controller is not None and hasattr(controller, "getCurrentPage"):
                try:
                    page = controller.getCurrentPage()
                except Exception:
                    page = None
            if page is None and count > 0:
                page = draw_pages.getByIndex(0)
            if page is None:
                return self._tool_error("No draw page or slide to insert chart.")
            for i in range(count):
                candidate = draw_pages.getByIndex(i)
                if page is candidate or uno_same(page, candidate):
                    page_index = i
                    break

        # Draw/Impress: add OLE2 shape first, then CLSID (chart2 OLE GUID); chart lives on .Model
        shape = doc.createInstance(_OLE2_SHAPE)
        page.add(shape)
        name = ""
        try:
            try:
                shape.setSize(uno.createUnoStruct("com.sun.star.awt.Size", Width=rect.Width, Height=rect.Height))
                shape.setPosition(uno.createUnoStruct("com.sun.star.awt.Point", X=rect.X, Y=rect.Y))
            except Exception as e:
                log.debug("Failed to set Draw shape size/pos: %s", e)
            shape.CLSID = CHART_CLSID_DRAW_OLE

            # Tip: a per-page Chart_0 made the next edit/delete CHART_AMBIGUOUS.
            existing_names: set[str] = set()
            for i in range(count):
                pg = draw_pages.getByIndex(i)
                for s, _p, _st in _iter_page_shapes(pg, doc):
                    if s is not shape:
                        try:
                            nm = getattr(s, "Name", None)
                            if nm:
                                existing_names.add(nm)
                        except Exception as exc:
                            if is_disposed_exception(exc):
                                continue
                            raise
            name = _unique_chart_name(existing_names)
            shape.Name = name

            chart_doc = _chart_document_from_host(shape)
            if not chart_doc:
                _drop_failed_chart_insert(lambda: page.remove(shape), name or "draw-chart")
                return self._tool_error("Cannot access chart content.")
            chart_doc.setDiagram(chart_doc.createInstance(service))
            headers = kwargs.get("headers")
            rows = kwargs.get("rows")
            if headers and rows:
                _apply_chart_data_arrays(chart_doc, headers, rows)
            warnings = _apply_chart_styling(chart_doc, **kwargs)
        except Exception:
            _drop_failed_chart_insert(lambda: page.remove(shape), name or "draw-chart")
            raise

        _process_events(ctx.ctx, deadline=time.monotonic() + _WRITER_CHART_MODEL_WAIT_SEC)
        out: dict[str, Any] = {"status": "ok", "message": f"Chart '{name}' inserted on slide.", "name": name, "page": page_index}
        if warnings:
            out["warnings"] = warnings
        return out


class DeleteChart(ToolBaseDummy):
    """Delete chart; Dummy backend for ``ManageCharts`` action=delete."""

    name: str | None = "delete_chart"
    intent: str | None = "edit"
    description: str = "Delete a chart by name."
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Chart name to delete."},
            "page": {"type": "integer", "description": "Zero-based page/slide index (Draw/Impress only)."},
        },
        "required": ["name"],
    }
    uno_services: list[str] | None = ListCharts.uno_services
    is_mutation: bool | None = True

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        doc = ctx.doc
        target = _require_chart(self, doc, kwargs)
        if isinstance(target, dict):
            return target
        chart_name, ref = target

        try:
            if ref.kind == "calc_sheet":
                ref.parent.getCharts().removeByName(chart_name)
                return {"status": "ok", "deleted": chart_name, "sheet": ref.parent.getName()}
            elif ref.kind == "writer_embed":
                _remove_writer_chart(doc, ref.host, chart_name)
                return {"status": "ok", "deleted": chart_name}
            elif ref.kind in ("writer_draw_page", "draw_page"):
                ref.parent.remove(ref.host)
                return {"status": "ok", "deleted": chart_name}
            else:
                return self._tool_error(f"Unknown chart kind: {ref.kind}")
        except Exception as exc:
            if is_real_disposal(exc):
                raise
            return self._tool_error(f"Failed to delete chart: {_format_chart_exception_msg(exc)}")


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
            **CHART_PROPERTIES,
        },
        "required": ["action"],
    }
    uno_services: list[str] | None = list(ALL_DOCUMENT_SERVICES)
    is_mutation: bool | None = True

    def requires_document_lock(self, arguments: Any = None) -> bool:
        """Read actions must not take the MCP per-document write gate."""
        action = arguments.get("action") if isinstance(arguments, dict) else None
        return action not in ("list", "get_info")

    def get_parameters(self, doc_type: str | None = None) -> dict[str, Any] | None:
        return _chart_parameters(self.parameters, doc_type)

    def validate(self, *, doc_type: str | None = None, **kwargs: Any) -> tuple[Literal[False], str] | tuple[Literal[True], None]:
        ok, err = super().validate(doc_type=doc_type, **kwargs)
        if not ok:
            return False, err or "invalid parameters"
        action = kwargs.get("action")
        if action in ("get_info", "delete") and "name" not in kwargs:
            return False, f"Parameter 'name' is required when action is '{action}'"
        if action in ("create", "edit"):
            return _validate_chart_action(kwargs, doc_type)
        return True, None

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        # action (required + enum), create chart_type, and edit/get_info/delete name are
        # rejected by validate() before execute. Direct execute checks remain below.
        action = kwargs.get("action")

        if action == "list":
            return ListCharts().execute(ctx, **kwargs)
        elif action == "get_info":
            if "name" not in kwargs:
                return self._tool_error("name parameter is required for action='get_info'.")
            return GetChartInfo().execute(ctx, **kwargs)
        elif action in ("create", "edit"):
            return UpsertChart().execute(ctx, **kwargs)
        elif action == "delete":
            if "name" not in kwargs:
                return self._tool_error("name parameter is required for action='delete'.")
            return DeleteChart().execute(ctx, **kwargs)
        return self._tool_error(f"Unsupported action: '{action}'")
