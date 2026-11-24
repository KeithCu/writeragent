# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2024 John Balis
# Copyright (c) 2026 KeithCu (modifications and relicensing)
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
"""Shape tools for Draw/Impress documents."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, ClassVar, Literal

from plugin.calc.base import ToolCalcShapeBase
from plugin.doc.visual_helpers import SHAPE_TOOL_UNO_SERVICES, apply_character_properties, parse_color_to_uno_int
from plugin.framework.errors import WriterAgentException, is_disposal, uno_read, uno_write
from plugin.writer.specialized_base import ToolWriterShapeBase
from plugin.writer.styles import para_adjust_to_uno
from . import layout
from .base import DRAW_PAGE_PROPERTY, ToolDrawShapeBase

if TYPE_CHECKING:
    from plugin.framework.tool import ToolBase, ToolContext

log = logging.getLogger(__name__)


class _AnyDocumentShapeTool(ToolDrawShapeBase):
    """Shared shape-tool base for Writer, Calc, Draw, and Impress (the only class for these names)."""

    uno_services: list[str] | None = SHAPE_TOOL_UNO_SERVICES
    doc_types: list[str] | None = ["writer", "calc", "draw", "impress"]
    # One class serves every app, so it carries each app's reader tools. get_tools drops the
    # readers that do not support the open document (Calc readers never reach a Writer agent).
    required_core_tools: ClassVar[frozenset[str] | None] = (
        (ToolCalcShapeBase.required_core_tools or frozenset())
        | (ToolWriterShapeBase.required_core_tools or frozenset())
    )


def _page_shape_names(page: Any) -> list[str]:
    from plugin.draw.tree import iter_shape_addresses

    seen: set[str] = set()
    names: list[str] = []
    for _path, shape, _parent in iter_shape_addresses(page):
        raw = getattr(shape, "Name", None)
        if raw is None:
            continue
        name = str(raw).strip()
        if name and name not in seen:
            seen.add(name)
            names.append(name)
    return names


def _ensure_shape_name(page: Any, shape: Any) -> str:
    """Return *shape*'s Name, assigning ``Shape<n>`` if it is empty.

    Writer's ``SwXDrawPage::add`` (``sw/source/core/unocore/unodraw.cxx:SwXDrawPage::add``)
    already assigns a unique Name, whereas Draw, Impress, and Calc leave Name
    empty and prompts tell the model to address shapes by Name.
    Uses one more than the highest existing ``Shape<n>`` number on the page so
    deleting a shape never hands its old name to a newly created shape.
    """
    current = str(shape.Name or "").strip()
    if current:
        return current
    highest = 0
    for name in _page_shape_names(page):
        if name.startswith("Shape"):
            suffix = name[5:]
            if suffix.isascii() and suffix.isdigit():
                highest = max(highest, int(suffix))
    candidate = f"Shape{highest + 1}"
    shape.Name = candidate
    return candidate


def _name_taken_error(tool: ToolBase, name: str) -> dict[str, Any]:
    return tool._tool_error(
        "A shape named %r already exists on this page; choose another name or edit that shape." % name,
        code="INVALID_PARAM",
    )


def _lookup_ref(
    tool: ToolBase,
    page: Any,
    ref: Any,
    *,
    by_name: bool = False,
) -> tuple[int | str | None, Any | None, Any | None, dict[str, Any] | None]:
    from plugin.draw.tree import find_shape_on_page, resolve_shape_ref
    from plugin.framework.errors import available_miss_details

    if by_name:
        wanted = str(ref).strip()
        addr, shape, parent, err = find_shape_on_page(page, name=wanted)
    else:
        wanted = str(ref).strip() if isinstance(ref, str) else str(ref)
        addr, shape, parent, err = resolve_shape_ref(page, ref)
    if err or shape is None:
        if err and err.startswith("No shape named"):
            names = _page_shape_names(page)
            capped = names[:40]
            return None, None, None, tool._tool_error(
                err,
                available=capped,
                **available_miss_details(wanted, capped),
            )
        return None, None, None, tool._tool_error(err or "Shape not found.")
    return addr, shape, parent, None


def _reject_duplicate_shapes(tool: ToolBase, shapes: list[Any]) -> dict[str, Any] | None:
    seen: set[int] = set()
    for shape in shapes:
        token = id(shape)
        if token in seen:
            return tool._tool_error("Duplicate shape in indices; pass each shape once.")
        seen.add(token)
    return None


_DIGIT_REF_SCHEMA_NOTE = (
    " All-digit strings are page indices, not Names (only shape_delete accepts name= for digit-only Names)."
)


# LibreOffice interprets CustomShapeGeometry as EnhancedCustomShapeGeometry when this engine is set.
_ENHANCED_CUSTOM_SHAPE_ENGINE = "com.sun.star.drawing.EnhancedCustomShapeEngine"


class DrawError(WriterAgentException):
    """Draw-specific errors."""

    code: str = "DRAW_ERROR"


def _try_writer_anchor_shape_before_add(doc: Any, shape: Any) -> None:
    """Writer: set ``AnchorType`` before ``XDrawPage.add`` or shapes often do not display.

    Uses ``TextContentAnchorType.AT_PAGE`` when the property exists (``text.Shape``).
    """
    try:
        if doc is None:
            log.debug("create_shape writer_anchor: skip (doc is None)")
            return
        if not doc.supportsService("com.sun.star.text.TextDocument"):
            log.debug("create_shape writer_anchor: skip (not TextDocument)")
            return
        from com.sun.star.text.TextContentAnchorType import AT_PAGE

        shape.setPropertyValue("AnchorType", AT_PAGE)
    except Exception as ex:
        if is_disposal(ex):
            raise
        log.warning("create_shape writer_anchor: set AnchorType failed: %s: %s", type(ex).__name__, ex)


def _try_writer_at_page_shape_finalize(doc: Any, shape: Any, page_idx: Any = 0) -> None:
    """Writer: ``AT_PAGE`` shapes must set ``AnchorPageNo`` (1-based) and absolute orient or they may not paint.

    See ``com.sun.star.text.Shape`` — ``AnchorPageNo`` is only valid for ``AT_PAGE``.
    """
    try:
        if doc is None or not doc.supportsService("com.sun.star.text.TextDocument"):
            return
        idx = int(page_idx)
        anchor_no = idx + 1
        # IDL: com.sun.star.text.HoriOrientation/VertOrientation NONE = 0 (absolute position)
        shape.setPropertyValue("AnchorPageNo", anchor_no)
        shape.setPropertyValue("HoriOrient", 0)
        shape.setPropertyValue("VertOrient", 0)
        shape.setPropertyValue("HoriOrientRelation", 8)  # PAGE_PRINT_AREA
        shape.setPropertyValue("VertOrientRelation", 8)  # PAGE_PRINT_AREA
        log.debug("create_shape writer_at_page_finalize: AnchorPageNo=%s draw_page_index=%s HoriOrient=0 VertOrient=0", anchor_no, idx)
        try:
            log.debug("create_shape writer_at_page_finalize: readback AnchorPageNo=%r HoriOrient=%r VertOrient=%r", shape.getPropertyValue("AnchorPageNo"), shape.getPropertyValue("HoriOrient"), shape.getPropertyValue("VertOrient"))
        except Exception as ex:
            if is_disposal(ex):
                raise
            log.debug("create_shape writer_at_page_finalize: readback failed: %s", ex)
    except Exception as ex:
        if is_disposal(ex):
            raise
        log.warning("create_shape writer_at_page_finalize: failed %s: %s", type(ex).__name__, ex)


def _try_writer_reapply_position_after_anchor(doc: Any, shape: Any, position: Any, size: Any) -> None:
    """Writer: setting ``AnchorPageNo`` / orient can reset placement; re-apply ``Position``/``Size``."""
    try:
        if doc is None or not doc.supportsService("com.sun.star.text.TextDocument"):
            return
        shape.setPosition(position)
        shape.setSize(size)
        log.debug("create_shape writer_reapply_pos: pos=(%s,%s) size=(%sx%s)", position.X, position.Y, size.Width, size.Height)
    except Exception as ex:
        if is_disposal(ex):
            raise
        log.warning("create_shape writer_reapply_pos: %s: %s", type(ex).__name__, ex)


def _try_writer_invalidate_and_pump(doc: Any) -> None:
    """Force a repaint after shape changes (Writer sometimes does not redraw the draw layer)."""
    try:
        if doc is None or not doc.supportsService("com.sun.star.text.TextDocument"):
            return
        ctrl = doc.getCurrentController()
        if ctrl is None:
            log.debug("create_shape writer_invalidate: no controller")
            return
        frame = ctrl.getFrame()
        if frame is None:
            return
        win = frame.getContainerWindow()
        if win is None:
            return
        win.invalidate(0)
        log.debug("create_shape writer_invalidate: invalidate(0)")
    except Exception as ex:
        if is_disposal(ex):
            raise
        log.debug("create_shape writer_invalidate: %s", ex)


def _apply_enhanced_custom_shape_type(shape: Any, custom_shape_type: str) -> tuple[bool, str | None]:
    """Set EnhancedCustomShape engine and geometry ``Type`` so names like ``octagon`` render."""
    from com.sun.star.beans import PropertyValue
    import uno

    prop = PropertyValue()
    prop.Name = "Type"
    prop.Value = custom_shape_type
    try:
        import typing

        shape.setPropertyValue("CustomShapeEngine", _ENHANCED_CUSTOM_SHAPE_ENGINE)
        # uno.Any is a pyuno helper; stubs/mypy do not export it as a module attribute.
        uno_any = getattr(uno, "Any")
        prop_seq = uno_any("[]com.sun.star.beans.PropertyValue", (prop,))
        uno.invoke(shape, "setPropertyValue", typing.cast("typing.Any", ("CustomShapeGeometry", prop_seq)))
        log.debug("create_shape enhanced_geometry: ok type=%r engine=%r", custom_shape_type, _ENHANCED_CUSTOM_SHAPE_ENGINE)
        return True, None
    except Exception as ex:
        if is_disposal(ex):
            raise
        log.debug("create_shape enhanced_geometry: failed type=%r err=%s: %s", custom_shape_type, type(ex).__name__, ex)
        return False, f"{type(ex).__name__}: {ex}"


class GetDrawSummary(_AnyDocumentShapeTool):
    name: str | None = "shape_get_summary"
    description: str = (
        "Returns a summary of shapes on the active or specified page. "
        'Child shapes in groups use a dotted index (e.g. "2.0.1") which edit and delete accept.'
    )
    parameters: dict[str, Any] | None = {"type": "object", "properties": {"page": DRAW_PAGE_PROPERTY}, "required": []}

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        from plugin.draw.bridge import DrawBridge
        from plugin.draw.tree import iter_shape_addresses

        page, actual_idx = DrawBridge(ctx.doc).resolve_page(kwargs.get("page"), active_page_index=ctx.active_page_index)

        shapes = []
        for addr, s, _parent in iter_shape_addresses(page):
            row_index: int | str = int(addr) if "." not in addr else addr
            info: dict[str, Any] = {
                "index": row_index,
                "name": getattr(s, "Name", "") or "",
                "type": s.getShapeType(),
                "x": s.getPosition().X,
                "y": s.getPosition().Y,
                "width": s.getSize().Width,
                "height": s.getSize().Height,
            }
            if hasattr(s, "getString"):
                info["text"] = s.getString()
            shapes.append(info)

        return {"status": "ok", "page": actual_idx, "shapes": shapes}


def _is_line_shape_type(shape_type: str | None) -> bool:
    """True for UNO ``LineShape`` (tool alias ``line``), not connectors or polylines."""
    if not shape_type:
        return False
    name = str(shape_type).rsplit(".", 1)[-1]
    return name == "LineShape" or name == "line"


def _normalize_negative_span(x: int, y: int, width: int, height: int) -> tuple[int, int, int, int]:
    """Shift x/y by any negative width/height so position is top-left and size is non-negative."""
    if width < 0:
        x += width
        width = -width
    if height < 0:
        y += height
        height = -height
    return x, y, width, height


def _set_line_endpoints(shape: Any, x: int, y: int, width: int, height: int) -> None:
    """Set a LineShape's start/end points via PolyPolygon, preserving app layer offset.

    Once a LineShape is on the page, setSize with a negative span rescales/flips
    relative to its existing bounding box (and Writer ignores negative direction),
    so endpoints must be written through PolyPolygon instead.
    """
    # Host boundary: uno is only available inside LibreOffice or with UNO stubs.
    import uno
    from com.sun.star.awt import Point

    # Writer's SwXShape::getPosition returns anchor-relative coords while PolyPolygon
    # is in draw-layer page coords; add the current bbox-to-position delta so both
    # Draw/Calc (delta 0) and Writer land at the caller's coordinates.
    dx = dy = 0
    poly = shape.getPropertyValue("PolyPolygon")
    flat = [pt for seq in poly for pt in seq] if poly else []
    if flat:
        pos = shape.getPosition()
        dx = min(pt.X for pt in flat) - pos.X
        dy = min(pt.Y for pt in flat) - pos.Y

    pts = ((Point(x + dx, y + dy), Point(x + width + dx, y + height + dy)),)
    # Bare tuples raise IllegalArgumentException through pyuno's Any-typed setPropertyValue,
    # and PyUNO rejects uno.Any outside uno.invoke; uno_mod: Any avoids stub errors
    # where uno.Any is typed as taking str | Type.
    uno_mod: Any = uno
    uno_mod.invoke(shape, "setPropertyValue", ("PolyPolygon", uno_mod.Any("[][]com.sun.star.awt.Point", pts)))


def _drawing_enum(enum_name: str, member: str) -> Any:
    """Return a ``com.sun.star.drawing`` UNO enum value via ``uno.Enum``."""
    # Host boundary: uno is only available inside LibreOffice or with UNO stubs.
    import uno

    return uno.Enum(f"com.sun.star.drawing.{enum_name}", member)


class DrawShapes:
    def _is_valid_position(self, position: Any) -> bool:
        if not hasattr(position, "X") or not hasattr(position, "Y"):
            return False
        return True

    def _is_valid_size(self, size: Any, shape_type: str | None = None) -> bool:
        """Reject a size LibreOffice will not use as a shape box.

        A ``LineShape`` may have one zero side (horizontal or vertical) and
        may have negative width or height (applied via ``PolyPolygon`` after
        insertion so direction is preserved in all apps). Other shapes need
        both sides positive. Both sides zero is always invalid.
        """
        if not hasattr(size, "Width") or not hasattr(size, "Height"):
            return False
        width = size.Width
        height = size.Height
        if width == 0 and height == 0:
            return False
        if _is_line_shape_type(shape_type):
            return True
        if width <= 0 or height <= 0:
            return False
        return True

    def safe_create_shape(self, doc: Any, page: Any, shape_type: str, position: Any, size: Any, custom_shape_type: str | None = None) -> tuple[Any, bool | None, str | None]:
        """Safely create shape with error handling.

        Shapes are created via the document's factory (``doc.createInstance``);
        ``XDrawPage`` is not a reliable ``createInstance`` source in UNO.

        For CustomShape, ``EnhancedCustomShapeGeometry`` is applied **before**
        ``page.add`` so ``SvxShape::setPropertyValue`` buffers ``Type``
        (``svx/source/unodraw/unoshape.cxx:566``) instead of swapping it on a
        live default ``SdrRectObj`` (which can abort in
        ``SfxItemPool::unregisterNameOrIndex``), and re-applied after add in
        ``UpsertShape`` so ``SvxCustomShape::setPropertyValue`` runs
        ``MergeDefaultAttributes()`` (``svx/source/unodraw/unoshap2.cxx:1733-1749``)
        to expand ``ViewBox`` and ``Path``.
        """
        try:
            if doc is None:
                raise DrawError("Document is None", code="DRAW_DOC_NULL", details={"operation": "create_shape", "shape_type": shape_type})
            # UNO XDrawPage can be falsy when it has zero shapes — use `is None`.
            if page is None:
                raise DrawError("Page is None", code="DRAW_PAGE_NULL", details={"operation": "create_shape", "shape_type": shape_type})

            if not self._is_valid_position(position):
                raise DrawError(f"Invalid position: {position}", code="DRAW_INVALID_POSITION", details={"position": position})

            if not self._is_valid_size(size, shape_type):
                raise DrawError(f"Invalid size: {size}", code="DRAW_INVALID_SIZE", details={"size": size})

            # Create shape (document MSF — same as DrawBridge.create_shape)
            if shape_type.startswith("com.sun.star."):
                full_type = shape_type
            else:
                full_type = f"com.sun.star.drawing.{shape_type}"

            shape = doc.createInstance(full_type)
            if shape is None:
                raise DrawError(f"Failed to create shape of type: {shape_type}", code="DRAW_SHAPE_CREATION_FAILED", details={"shape_type": shape_type})

            shape.setPosition(position)
            shape.setSize(size)

            geometry_applied: bool | None = None
            geometry_error: str | None = None
            if custom_shape_type:
                geometry_applied, geometry_error = _apply_enhanced_custom_shape_type(shape, custom_shape_type)
                if not geometry_applied:
                    log.warning("create_shape safe_create: enhanced geometry failed before add type=%s err=%s", custom_shape_type, geometry_error)

            _try_writer_anchor_shape_before_add(doc, shape)

            # Add to page
            page.add(shape)

            # getCount after a successful add does not fail on a live page.
            n_after = page.getCount()
            log.debug("create_shape safe_create: added uno=%s page_shape_count=%s", full_type, n_after)

            return shape, geometry_applied, geometry_error

        except DrawError:
            # Re-raise our draw errors
            raise
        except Exception as e:
            # A disposed document must reach execute_safe as DisposedException.
            # Wrapping it in DrawError made a closed document look like a
            # normal shape-creation failure.
            if is_disposal(e):
                raise
            raise DrawError(f"Failed to create shape: {str(e)}", code="DRAW_SHAPE_CREATION_ERROR", details={"shape_type": shape_type, "position": position, "size": size, "original_error": str(e), "error_type": type(e).__name__}) from e


def _clamp_shape_text_autogrow(shape: Any, *, apply_autofit: bool = True) -> None:
    """Keep explicit Size after setString; default TextFitToSize=AUTOFIT.

    AutoGrow stays off so Writer AT_PAGE custom shapes do not shrink to text.
    AUTOFIT scales font down to the shape box. Callers that pass font_size (or
    another explicit fit/font override) skip AUTOFIT so CharHeight wins.
    """
    for prop, value in (("TextAutoGrowHeight", False), ("TextAutoGrowWidth", False)):
        try:
            shape.setPropertyValue(prop, value)
        except Exception as ex:
            if is_disposal(ex):
                raise
            log.debug("setPropertyValue %s failed", prop, exc_info=True)
    if not apply_autofit:
        return
    try:
        shape.setPropertyValue("TextFitToSize", _drawing_enum("TextFitToSizeType", "AUTOFIT"))
    except Exception as ex:
        if is_disposal(ex):
            raise
        log.debug("set TextFitToSize AUTOFIT failed", exc_info=True)


_DEFAULT_ARROWHEAD_WIDTH = 300


def _arrow_coords() -> Any:
    """PolyPolygonBezierCoords for LibreOffice's built-in 'Arrow' triangle (svx/source/xoutdev/xtablend.cxx:62-67)."""
    # Host boundary: uno/com.sun.star are only available inside LibreOffice or with UNO stubs.
    import uno
    from com.sun.star.awt import Point

    normal = _drawing_enum("PolygonFlags", "NORMAL")
    return uno.createUnoStruct(
        "com.sun.star.drawing.PolyPolygonBezierCoords",
        Coordinates=((Point(10, 0), Point(0, 30), Point(20, 30), Point(10, 0)),),
        Flags=((normal, normal, normal, normal),),
    )


def _set_arrowhead(shape: Any, prefix: str, marker: str) -> None:
    """Apply or clear a line-end marker ('arrow' or 'none') on a shape."""
    name_prop = f"{prefix}Name"
    if marker.strip().lower() == "arrow":
        shape.setPropertyValue(name_prop, "Arrow")
        # Write the polygon explicitly too: when "Arrow" is missing from the model's XLineEndList,
        # SetFillAttribute (svx/source/unodraw/unoshape.cxx:1345-1369) falls back to storing only the name.
        shape.setPropertyValue(prefix, _arrow_coords())
        # Scale width from the shape's current LineWidth so create and edit match (at least 300 = 3 mm).
        line_w = uno_read(lambda: shape.getPropertyValue("LineWidth"), 0)
        width = max(_DEFAULT_ARROWHEAD_WIDTH, line_w * 3) if isinstance(line_w, int) and not isinstance(line_w, bool) else _DEFAULT_ARROWHEAD_WIDTH
        shape.setPropertyValue(f"{prefix}Width", width)
    else:
        # Empty name puts an empty polygon in svx/source/unodraw/unoshape.cxx:1404-1413.
        shape.setPropertyValue(name_prop, "")


def _try(failed: list[str], name: str, fn: Any) -> None:
    if not uno_write(fn):
        failed.append(name)


def _apply_shape_properties(shape: Any, kwargs: dict[str, Any]) -> list[str]:
    """Helper to apply rich formatting properties to a shape. Returns list of failed property names."""
    failed_props: list[str] = []

    # "text" in kwargs (not truthy) so paper-form fills can write "" or keep a Name-only edit.
    if "text" in kwargs and hasattr(shape, "setString"):

        def _apply_text() -> None:
            was_empty = shape.getString() == ""
            text_str = "" if kwargs["text"] is None else str(kwargs["text"])
            # Schema already exposes font_size/font_name; explicit font_size opts out of AUTOFIT.
            if kwargs.get("action") == "edit":
                apply_autofit = ("font_size" not in kwargs) and ("width" in kwargs or "height" in kwargs)
            else:
                apply_autofit = "font_size" not in kwargs
            _clamp_shape_text_autogrow(shape, apply_autofit=apply_autofit)
            shape.setString(text_str)
            if was_empty and text_str:
                # A fresh paragraph does not inherit the object's ParaAdjust
                # (svx/source/unodraw/unoshtxt.cxx: SvxTextEditSourceImpl::GetBackgroundTextForwarder),
                # and a cursor on an empty shape stores nothing and clears IsEmptyPresObj
                # (svx/source/unodraw/unoshtxt.cxx:759-792, UpdateData). Re-applying the direct
                # object item pushes it into the new paragraph via TextProperties::ItemSetChanged
                # (svx/source/sdr/properties/textproperties.cxx:86).
                state = uno_read(lambda: shape.getPropertyState("ParaAdjust"), None)
                if getattr(state, "value", None) == "DIRECT_VALUE":
                    shape.setPropertyValue("ParaAdjust", shape.getPropertyValue("ParaAdjust"))

        _try(failed_props, "text", _apply_text)

    stripped_name = str(kwargs.get("name") or "").strip()
    if stripped_name and hasattr(shape, "Name"):
        _try(failed_props, "name", lambda: setattr(shape, "Name", stripped_name))

    # Background/Fill Color
    fill_color_set = False
    if kwargs.get("fill_color"):
        color_str = kwargs.get("fill_color")
        color_str_l = (color_str or "").strip().lower()
        if color_str_l in ("none", "transparent") and hasattr(shape, "FillStyle"):

            def _clear_fill_color() -> None:
                shape.setPropertyValue("FillStyle", _drawing_enum("FillStyle", "NONE"))

            _try(failed_props, "fill_color", _clear_fill_color)
        else:
            color = parse_color_to_uno_int(color_str)
            if color is None:
                failed_props.append("fill_color")
            else:
                is_line = "LineShape" in shape.getShapeType()
                if not is_line:
                    fill_color_set = True
                _try(
                    failed_props,
                    "fill_color",
                    lambda: shape.setPropertyValue(
                        "LineColor" if is_line else "FillColor",
                        color,
                    ),
                )

    # Fill Style (solid, transparent, etc; ensure fill is visible when fill_color is set)
    if (kwargs.get("fill_style") or fill_color_set) and hasattr(shape, "FillStyle"):

        def _apply_fill_style() -> None:
            style_str = (kwargs.get("fill_style") or "solid").lower()
            if style_str in ("none", "transparent"):
                shape.setPropertyValue("FillStyle", _drawing_enum("FillStyle", "NONE"))
            elif style_str == "solid":
                shape.setPropertyValue("FillStyle", _drawing_enum("FillStyle", "SOLID"))

        _try(failed_props, "fill_style", _apply_fill_style)

    # Line Color
    if kwargs.get("line_color"):
        color = parse_color_to_uno_int(kwargs["line_color"])
        if color is None:
            failed_props.append("line_color")
        else:
            _try(failed_props, "line_color", lambda: shape.setPropertyValue("LineColor", color))

    # Line Width
    if kwargs.get("line_width") is not None and hasattr(shape, "LineWidth"):
        _try(failed_props, "line_width", lambda: shape.setPropertyValue("LineWidth", int(kwargs["line_width"])))

    # Line Style (ensure border is visible when colored or sized)
    if (kwargs.get("line_color") or kwargs.get("line_width") is not None) and hasattr(shape, "LineStyle"):

        def _apply_line_style() -> None:
            shape.setPropertyValue("LineStyle", _drawing_enum("LineStyle", "SOLID"))

        _try(failed_props, "line_style", _apply_line_style)

    # Line Start / End arrowheads
    for param, prefix in (("line_start", "LineStart"), ("line_end", "LineEnd")):
        marker = kwargs.get(param)
        if marker is not None:
            if not hasattr(shape, f"{prefix}Name"):
                failed_props.append(param)
            else:
                _try(failed_props, param, lambda p=prefix, m=str(marker): _set_arrowhead(shape, p, m))

    # Character properties (font, size, color, bold, italic)
    if (
        kwargs.get("text_color")
        or kwargs.get("font_size")
        or kwargs.get("font_name")
        or kwargs.get("bold") is not None
        or kwargs.get("italic") is not None
    ):

        def _apply_text_properties() -> None:
            char_res = apply_character_properties(
                shape,
                font_name=kwargs.get("font_name"),
                font_size_pt=kwargs.get("font_size"),
                bold=kwargs.get("bold"),
                italic=kwargs.get("italic"),
                color=kwargs.get("text_color"),
            )
            for prop_name, ok in char_res.items():
                if not ok:
                    failed_props.append(prop_name)

        _try(failed_props, "text_properties", _apply_text_properties)

    if kwargs.get("h_align"):

        def _apply_h_align() -> None:
            # BLOCK makes the text area span the shape width so ParaAdjust aligns
            # against the shape edges; RectangleShape and EllipseShape default to
            # CENTER, which centres the text block and ignores left/right
            # (svx/source/svdraw/svdotextdecomposition.cxx: SdrTextObj::impDecomposeBlockTextPrimitive).
            shape.setPropertyValue("TextHorizontalAdjust", _drawing_enum("TextHorizontalAdjust", "BLOCK"))
            shape.setPropertyValue("ParaAdjust", para_adjust_to_uno(kwargs["h_align"]))

        _try(failed_props, "h_align", _apply_h_align)

    if kwargs.get("v_align"):
        _try(
            failed_props,
            "v_align",
            lambda: shape.setPropertyValue("TextVerticalAdjust", _drawing_enum("TextVerticalAdjust", kwargs["v_align"].upper())),
        )

    if kwargs.get("wrap_text") is not None:
        # Only custom shapes read this flag (svx/source/sdr/contact/viewcontactofsdrobjcustomshape.cxx);
        # the rectangle and ellipse primitives pass bWordWrap=false.
        _try(failed_props, "wrap_text", lambda: shape.setPropertyValue("TextWordWrap", bool(kwargs["wrap_text"])))

    if kwargs.get("corner_radius") is not None:
        # CustomShape has no CornerRadius (svx/source/unodraw/unoprov.cxx: ImplGetSvxCustomShapePropertyMap),
        # so the set fails and is reported as a warning.
        _try(failed_props, "corner_radius", lambda: shape.setPropertyValue("CornerRadius", int(kwargs["corner_radius"])))

    # Rotation
    if kwargs.get("rotation_angle") is not None and hasattr(shape, "RotateAngle"):
        # Angle is in 100ths of a degree
        _try(failed_props, "rotation_angle", lambda: shape.setPropertyValue("RotateAngle", int(kwargs["rotation_angle"] * 100)))

    return failed_props


# A label is centred and wrapped unless the caller says otherwise. LibreOffice leaves a
# CustomShape created through UNO top/start aligned, and unwrapped in Writer and Calc
# (svx/source/svdraw/svdmodel.cxx:185).
_CUSTOM_SHAPE_TEXT_DEFAULTS: dict[str, Any] = {"h_align": "center", "v_align": "center", "wrap_text": True}

# CustomShape flowchart-* type strings (valid at runtime; omitted from create_shape schema description for brevity):
# flowchart-alternate-process, flowchart-card, flowchart-collate, flowchart-connector, flowchart-data,
# flowchart-decision, flowchart-delay, flowchart-direct-access-storage, flowchart-display, flowchart-document,
# flowchart-extract, flowchart-internal-storage, flowchart-magnetic-disk, flowchart-manual-input,
# flowchart-manual-operation, flowchart-merge, flowchart-multidocument, flowchart-off-page-connector,
# flowchart-or, flowchart-predefined-process, flowchart-preparation, flowchart-process, flowchart-punched-tape,
# flowchart-sequential-access, flowchart-sort, flowchart-stored-data, flowchart-summing-junction,
# flowchart-terminator

_CREATE_SHAPE_SHAPE_TYPE_DESC = (
    "Type of shape. "
    "Simple aliases (built-in UNO classes): rectangle, round-rectangle, ellipse, text, line, connector. "
    "Polygons and symbols: octagon, hexagon, diamond, trapezoid, smiley, heart, sun, moon; "
    "stars (star4, star5, star8, star24); "
    "arrows and callouts (names with -arrow, -arrow-callout, line-callout-1, etc.); "
    "braces and brackets (brace-pair, bracket-pair, left-brace, left-bracket, etc.); "
    "other shapes (cube, ring, cloud-callout, lightning, etc.). "
)

_OUTSIDE_PAGE_WARNING = "Shape is outside the page bounds."


def _outside_page(page: Any, shape: Any) -> bool:
    """True when the shape's box spills past the page edges."""
    from plugin.draw.bridge import page_size

    size = page_size(page)
    if size is None:
        return False  # Tip: Calc sheet draw pages have no size or report 0; skip.
    w, h = size
    pos, size = shape.getPosition(), shape.getSize()
    x, y = getattr(pos, "X", None), getattr(pos, "Y", None)
    sw, sh = getattr(size, "Width", None), getattr(size, "Height", None)
    if not isinstance(x, int) or not isinstance(y, int) or not isinstance(sw, int) or not isinstance(sh, int):
        return False
    return x < 0 or y < 0 or x + sw > w or y + sh > h


class UpsertShape(_AnyDocumentShapeTool):
    name: str | None = "shape_upsert"
    description: str = "Create or edit a shape on a page. When filling a paper-form blank, edit by shape Name from shape_get_tree — draw-page index shifts when other shapes sit between fields."
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["create", "edit"], "description": "Action to perform: 'create' a new shape, or 'edit' an existing one."},
            "index": {
                "type": ["integer", "string"],
                "description": '0-based index of the shape on the page, or a dotted group path such as "2.0.1" (edit: pass index or name)',
            },
            "name": {
                "type": "string",
                "description": "Shape Name. Edit: look up by Name when index is omitted; with index, renames (must be unused on the page). Create: optional unique Name; when omitted a unique Name is assigned and returned as `name`.",
            },
            "page": DRAW_PAGE_PROPERTY,
            "shape_type": {"type": "string", "description": _CREATE_SHAPE_SHAPE_TYPE_DESC + " (required only for action='create')"},
            "x": {"type": "integer", "description": "X position, 1/100 mm (required only for action='create')"},
            "y": {"type": "integer", "description": "Y position, 1/100 mm (required only for action='create')"},
            "width": {"type": "integer", "description": "Width, 1/100 mm (required only for action='create'; negative values are allowed, and for lines x,y is the start point)"},
            "height": {"type": "integer", "description": "Height, 1/100 mm (required only for action='create'; negative values are allowed, and for lines x,y is the start point)"},
            "text": {"type": "string", "description": "Text content"},
            "fill_color": {"type": "string", "description": "Fill color. Hex (#FF0000) or name (red)"},
            "fill_style": {"type": "string", "enum": ["solid", "transparent", "none"], "description": "Fill style"},
            "line_color": {"type": "string", "description": "Line border color"},
            "line_width": {"type": "integer", "description": "Line width, 1/100 mm"},
            "line_start": {
                "type": "string",
                "enum": ["none", "arrow"],
                "description": "Arrowhead at the start of a line or connector ('none' clears).",
            },
            "line_end": {
                "type": "string",
                "enum": ["none", "arrow"],
                "description": "Arrowhead at the end of a line or connector ('none' clears).",
            },
            "text_color": {"type": "string", "description": "Text character color"},
            "font_size": {"type": "number", "description": "Optional. Omit for TextFitToSize AUTOFIT (auto-size to the shape box); that is usually correct. Only set a point size when you need a specific fixed size."},
            "font_name": {"type": "string", "description": "Font family name"},
            "bold": {"type": "boolean", "description": "Bold text (false removes bold)."},
            "italic": {"type": "boolean", "description": "Italic text (false removes italic)."},
            "h_align": {
                "type": "string",
                "enum": ["left", "center", "right", "justify"],
                "description": "Horizontal text alignment inside the shape. Custom shapes, rectangle and ellipse default to center.",
            },
            "v_align": {
                "type": "string",
                "enum": ["top", "center", "bottom"],
                "description": "Vertical text alignment inside the shape. Custom shapes, rectangle and ellipse default to center.",
            },
            "wrap_text": {
                "type": "boolean",
                "description": (
                    "Wrap text at the shape width. Applies to custom shapes (flowchart-process, round-rectangle, …); "
                    "on by default. Text boxes (text) always wrap. "
                    "rectangle and ellipse never wrap a fixed font_size; use flowchart-process (box) or flowchart-connector (circle) for a wrapped label."
                ),
            },
            "corner_radius": {"type": "integer", "description": "Corner radius, 1/100 mm. Only for rectangle (round-rectangle already has rounded corners)."},
            "rotation_angle": {"type": "number", "description": "Rotation angle in degrees"},
        },
        "required": ["action"],
    }
    is_mutation: bool | None = True

    def validate(self, *, doc_type: str | None = None, **kwargs: Any) -> tuple[Literal[False], str] | tuple[Literal[True], None]:
        # This override replaced ToolBase.validate, so shape_upsert had no
        # type or enum check. x="10" reached Point(), rotation_angle="5"
        # was string-repeated by `* 100`, and a fill_style outside the enum
        # was ignored with status ok.
        ok, err = super().validate(doc_type=doc_type, **kwargs)
        if not ok:
            return False, err or "Invalid arguments"

        action = kwargs.get("action")
        if action == "create":
            required = ["shape_type", "x", "y", "width", "height"]
            for r in required:
                if r not in kwargs:
                    return False, f"Parameter '{r}' is required when action is 'create'"
        elif action == "edit":
            if "index" not in kwargs and not str(kwargs.get("name") or "").strip():
                return False, "Parameter 'index' or 'name' is required when action is 'edit'"

        return True, None

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        return _execute_upsert_shape(self, ctx, **kwargs)


def _execute_upsert_shape(tool: ToolBase, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
    from plugin.draw.bridge import DrawBridge
    from com.sun.star.awt import Point, Size

    action = kwargs["action"]
    bridge = DrawBridge(ctx.doc)
    page, actual_idx = bridge.resolve_page(kwargs.get("page"), active_page_index=ctx.active_page_index)

    if action == "create":
        # Without this check, an explicit duplicate name would undo the
        # page-unique Name guarantee that auto-naming relies on.
        wanted_name = str(kwargs.get("name") or "").strip()
        if wanted_name and wanted_name in _page_shape_names(page):
            return _name_taken_error(tool, wanted_name)

        type_map = {"rectangle": "RectangleShape", "ellipse": "EllipseShape", "text": "TextShape", "line": "LineShape", "connector": "ConnectorShape"}
        shape_type_raw = kwargs["shape_type"]

        is_custom_shape = False
        custom_shape_type = ""

        # Determine UNO type
        if shape_type_raw in type_map:
            uno_type = type_map[shape_type_raw]
        elif "." in shape_type_raw or shape_type_raw.endswith("Shape"):
            uno_type = shape_type_raw
        else:
            # Fallback to CustomShape for things like 'octagon', 'smiley', 'heart'
            uno_type = "CustomShape"
            is_custom_shape = True
            custom_shape_type = shape_type_raw

        x, y, width, height = kwargs["x"], kwargs["y"], kwargs["width"], kwargs["height"]
        # Non-line shapes normalize negative spans to top-left + positive size.
        # LineShape keeps the signed size so LibreOffice preserves start->end direction.
        if not _is_line_shape_type(uno_type):
            x, y, width, height = _normalize_negative_span(x, y, width, height)

        position = Point(x, y)
        size = Size(width, height)

        log.debug("shape_upsert (create) branch: raw=%r resolved_uno=%r is_custom_catalog=%s catalog_name=%r", shape_type_raw, uno_type, is_custom_shape, custom_shape_type if is_custom_shape else None)

        draw_shapes = DrawShapes()

        try:
            # Pre-add write sets Type before page.add so SdrObjCustomShape is created with
            # that Type instead of swapping a live SdrRectObj (avoids SfxItemPool::unregisterNameOrIndex abort).
            shape, geometry_applied, geometry_error = draw_shapes.safe_create_shape(ctx.doc, page, uno_type, position, size, custom_shape_type=custom_shape_type if is_custom_shape else None)
        except DrawError as e:
            # _tool_error(message) defaults to TOOL_EXECUTION_ERROR, so
            # DRAW_INVALID_SIZE and DRAW_SHAPE_CREATION_ERROR were the
            # same code. Do not pass e.details: position and size are
            # UNO structs.
            return tool._tool_error(e.message, code=e.code)

        try:
            _try_writer_at_page_shape_finalize(ctx.doc, shape, actual_idx)
            _try_writer_reapply_position_after_anchor(ctx.doc, shape, position, size)

            # Post-add write triggers SvxCustomShape::setPropertyValue -> MergeDefaultAttributes()
            # (svx/source/unodraw/unoshap2.cxx:1733-1749) so ViewBox and Path are built and the shape paints.
            if is_custom_shape and custom_shape_type:
                geometry_applied, geometry_error = _apply_enhanced_custom_shape_type(shape, custom_shape_type)

            props = {**_CUSTOM_SHAPE_TEXT_DEFAULTS, **kwargs} if is_custom_shape else kwargs
            failed_props = _apply_shape_properties(shape, props)
            shape_name = _ensure_shape_name(page, shape)
            # setString can still resize Writer AT_PAGE custom shapes (Arch: 4001x4001 → 2249x489).
            _try_writer_reapply_position_after_anchor(ctx.doc, shape, position, size)
            if _is_line_shape_type(uno_type) and (width < 0 or height < 0):
                _set_line_endpoints(shape, x, y, width, height)
            _try_writer_invalidate_and_pump(ctx.doc)
        except Exception:
            # Remove the half-built shape; execute_safe reports the error (or disposal).
            try:
                page.remove(shape)
            except Exception:
                log.debug("remove half-built shape failed", exc_info=True)
            raise

        page_index = actual_idx
        shape_count_after = page.getCount()
        shape_index = shape_count_after - 1

        log.debug("shape_upsert (create): page=%s shape=%s shape_type=%s is_custom=%s geometry_applied=%s", page_index, shape_index, shape_type_raw, is_custom_shape, geometry_applied)

        result: dict[str, Any] = {
            "status": "ok",
            "action": "create",
            "message": f"Created {shape_type_raw}",
            "index": shape_index,
            "page": page_index,
            "name": shape_name,
            "shape_count_after": shape_count_after,
        }
        if is_custom_shape:
            result["custom_shape_engine"] = _ENHANCED_CUSTOM_SHAPE_ENGINE
            result["geometry_applied"] = bool(geometry_applied)
            if geometry_error:
                result["geometry_error"] = geometry_error
                result["warning"] = f"Custom shape geometry failed: {geometry_error}"
        if failed_props:
            result["warnings"] = result.get("warnings", []) + [f"Failed to apply properties: {', '.join(failed_props)}"]
        if _outside_page(page, shape):
            result["warnings"] = result.get("warnings", []) + [_OUTSIDE_PAGE_WARNING]

        return result

    elif action == "edit":
        from plugin.draw.tree import find_shape_on_page

        # Index is fragile for peer "fill field X"; Name from shape_get_tree stays stable
        # when non-fields sit between boxes. Index wins when both are passed, and then
        # name is the new Name (a rename), not a lookup key.
        if "index" in kwargs:
            shape_idx, shape, _parent, find_err = find_shape_on_page(page, index=kwargs["index"])
            if find_err or shape is None:
                return tool._tool_error(find_err or "Shape not found.")
        else:
            # _lookup_ref is the shared not-found path (also used by shape_delete): an unknown
            # Name returns details.available (the page's shape Names, capped) plus similar, so
            # the model can retry with a real Name instead of guessing.
            shape_idx, shape, _parent, err_res = _lookup_ref(tool, page, kwargs.get("name") or "", by_name=True)
            if err_res is not None:
                return err_res
            assert shape is not None

        # Without this check, renaming by index to an existing name would
        # create a duplicate on the page.
        new_name = str(kwargs.get("name") or "").strip()
        if "index" in kwargs and new_name and new_name != str(shape.Name or "").strip() and new_name in _page_shape_names(page):
            return _name_taken_error(tool, new_name)

        has_pos = "x" in kwargs or "y" in kwargs
        has_size = "width" in kwargs or "height" in kwargs
        is_line = _is_line_shape_type(shape.getShapeType())

        cur_pos = shape.getPosition() if (has_pos or has_size) else None
        cur_size = shape.getSize() if has_size else None
        target_x = kwargs.get("x", cur_pos.X) if cur_pos is not None else 0
        target_y = kwargs.get("y", cur_pos.Y) if cur_pos is not None else 0
        target_w = kwargs.get("width", cur_size.Width) if cur_size is not None else 0
        target_h = kwargs.get("height", cur_size.Height) if cur_size is not None else 0

        if has_size:
            if is_line:
                _set_line_endpoints(shape, target_x, target_y, target_w, target_h)
            else:
                norm_x, norm_y, target_w, target_h = _normalize_negative_span(target_x, target_y, target_w, target_h)
                if has_pos or norm_x != target_x or norm_y != target_y:
                    shape.setPosition(Point(norm_x, norm_y))
                shape.setSize(Size(target_w, target_h))
        elif has_pos:
            shape.setPosition(Point(target_x, target_y))

        failed_props = _apply_shape_properties(shape, kwargs)
        if "text" in kwargs and has_size:
            if is_line:
                _set_line_endpoints(shape, target_x, target_y, target_w, target_h)
            else:
                shape.setSize(Size(target_w, target_h))

        result = {"status": "ok", "action": "edit", "message": "Shape updated", "page": actual_idx, "index": shape_idx, "name": shape.Name or ""}
        if failed_props:
            result["warnings"] = [f"Failed to apply properties: {', '.join(failed_props)}"]
        if _outside_page(page, shape):
            result["warnings"] = result.get("warnings", []) + [_OUTSIDE_PAGE_WARNING]

        return result

    # validate() rejects other actions; keep execute total so the ToolBase
    # override is dict[str, Any] (Calc/Writer inherit this class).
    return tool._tool_error("Unknown action: '%s'. Must be 'create' or 'edit'" % action)


class ConnectShapes(_AnyDocumentShapeTool):
    """Connect two shapes with a connector."""

    name: str | None = "shape_connect"
    description: str = (
        "Connect two shapes on the same page with a connector."
        + _DIGIT_REF_SCHEMA_NOTE
    )
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "start": {"type": ["integer", "string"], "description": "Index or Name of the starting shape."},
            "end": {"type": ["integer", "string"], "description": "Index or Name of the ending shape."},
            "page": DRAW_PAGE_PROPERTY,
            "line_color": {"type": "string", "description": "Color of the connector line"},
            "line_width": {"type": "integer", "description": "Line width, 1/100 mm"},
        },
        "required": ["start", "end"],
    }
    is_mutation: bool | None = True

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        return _execute_connect_shapes(self, ctx, **kwargs)


def _execute_connect_shapes(tool: ToolBase, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
    from com.sun.star.awt import Point, Size
    from plugin.draw.bridge import DrawBridge

    page, actual_idx = DrawBridge(ctx.doc).resolve_page(kwargs.get("page"), active_page_index=ctx.active_page_index)

    start_addr, start_shape, _sp, start_err = _lookup_ref(tool, page, kwargs["start"])
    if start_err is not None:
        return start_err
    end_addr, end_shape, _ep, end_err = _lookup_ref(tool, page, kwargs["end"])
    if end_err is not None:
        return end_err
    assert start_shape is not None and end_shape is not None

    draw_shapes = DrawShapes()
    try:
        # position and size are technically ignored since it's a connector, but safe_create_shape expects them
        shape, _unused, _unused2 = draw_shapes.safe_create_shape(ctx.doc, page, "com.sun.star.drawing.ConnectorShape", Point(0, 0), Size(100, 100))
    except DrawError as e:
        # _tool_error(message) defaults to TOOL_EXECUTION_ERROR, so
        # DRAW_INVALID_SIZE and DRAW_SHAPE_CREATION_ERROR were the
        # same code. Do not pass e.details: position and size are
        # UNO structs.
        return tool._tool_error(e.message, code=e.code)

    # Connect the shapes
    try:
        shape.setPropertyValue("StartShape", start_shape)
        shape.setPropertyValue("EndShape", end_shape)

        # Additional properties
        failed_props = _apply_shape_properties(shape, kwargs)
        conn_name = _ensure_shape_name(page, shape)

    except Exception:
        # The connector is already on the page. Remove it so a failed
        # connect does not leave an unbound shape.
        try:
            page.remove(shape)
        except Exception as remove_exc:
            if is_disposal(remove_exc):
                raise
        raise

    res: dict[str, Any] = {
        "status": "ok",
        "message": f"Connected shape {start_addr} to {end_addr}",
        "index": page.getCount() - 1,
        "name": conn_name,
        "page": actual_idx,
    }
    if failed_props:
        res["warnings"] = [f"Failed to apply properties: {', '.join(failed_props)}"]
    return res


def _create_shape_collection(uno_ctx: Any) -> Any:
    """Temporary ``XShapes`` bag for ``XDrawPage.group``.

    ``doc.createInstance("com.sun.star.drawing.ShapeCollection")``
    fails. LibreOffice 26 Writer and Draw raise
    ``ServiceNotRegisteredException: unknown service:
    com.sun.star.drawing.ShapeCollection``. Calc's document factory
    returns None. ``ShapeCollection`` is not a document-factory
    service. It is the global implementation
    ``com.sun.star.drawing.SvxShapeCollection`` (LibreOffice
    ``services.rdb``). ``XMultiServiceFactory`` on the document
    only creates services that factory registers. The
    component-context service manager creates that global service,
    and ``XDrawPage.group`` accepts it (Writer ``AT_PAGE`` shapes,
    Draw, Calc).
    """
    from plugin.framework.uno_context import get_service_manager

    smgr = get_service_manager(uno_ctx)
    if smgr is None:
        raise RuntimeError("ServiceManager unavailable for ShapeCollection")
    collection = smgr.createInstanceWithContext("com.sun.star.drawing.ShapeCollection", uno_ctx)
    if collection is None:
        raise RuntimeError("ShapeCollection service returned None")
    return collection


class GroupShapes(_AnyDocumentShapeTool):
    """Group multiple shapes together."""

    name: str | None = "shape_group"
    description: str = (
        "Group related shapes into one composite object so the user can select and move them together. "
        "After you place pieces that belong to the same thing (a flag, logo, diagram, icon), "
        "ALWAYS call this before finishing — leaving loose parts is wrong. "
        "Pass every related shape index. "
        "The result is one group shape you can move with shape_upsert (action edit)."
        + _DIGIT_REF_SCHEMA_NOTE
    )
    parameters: dict[str, Any] | None = {"type": "object", "properties": {"indices": {"type": "array", "items": {"type": ["integer", "string"]}, "description": "List of shape indices or Names to group."}, "page": DRAW_PAGE_PROPERTY}, "required": ["indices"]}
    is_mutation: bool | None = True

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        from plugin.draw.bridge import DrawBridge
        from plugin.framework.uno_context import uno_same

        page, actual_idx = DrawBridge(ctx.doc).resolve_page(kwargs.get("page"), active_page_index=ctx.active_page_index)

        indices = kwargs["indices"]
        assert isinstance(indices, list)
        if not indices or len(indices) < 2:
            return self._tool_error("At least two shape indices are required to group.")

        shape_collection = _create_shape_collection(ctx.ctx)
        resolved_shapes: list[Any] = []
        for ref in indices:
            addr, shape, parent, err_res = _lookup_ref(self, page, ref)
            if err_res is not None:
                return err_res
            assert shape is not None and parent is not None
            if not uno_same(parent, page):
                return self._tool_error(
                    "Cannot group shape %s: it is not a top-level shape on the page." % addr,
                )
            resolved_shapes.append(shape)
        dup_err = _reject_duplicate_shapes(self, resolved_shapes)
        if dup_err is not None:
            return dup_err
        for shape in resolved_shapes:
            shape_collection.add(shape)
        # page.group is the first page mutation; failure propagates to execute_safe.
        group = page.group(shape_collection)
        group_name = ""
        warnings: list[str] = []
        try:
            group_name = _ensure_shape_name(page, group)
        except Exception as exc:
            if is_disposal(exc):
                raise
            log.debug("group name assignment failed", exc_info=True)
            warnings.append("Failed to assign group name")

        res: dict[str, Any] = {
            "status": "ok",
            "message": f"Grouped {len(indices)} shapes.",
            "index": page.getCount() - 1,  # Grouping replaces the individuals with this group shape
            "name": group_name,
            "page": actual_idx,
        }
        if warnings:
            res["warnings"] = warnings
        return res


def _shape_box(shape: Any) -> tuple[int, int, int, int]:
    pos = shape.getPosition()
    size = shape.getSize()
    return (int(pos.X), int(pos.Y), int(size.Width), int(size.Height))


def _apply_box(shape: Any, box: tuple[int, int, int, int]) -> None:
    from com.sun.star.awt import Point

    x, y, _w, _h = box
    pos = shape.getPosition()
    if pos.X != x or pos.Y != y:
        shape.setPosition(Point(x, y))


def _layout_on_page(tool: ToolBase, ctx: ToolContext, indices: list[Any], layout_arg: str, *, min_count: int, too_few: str, layout_fn: Any, ok_message: str, page_kwargs: dict[str, Any]) -> dict[str, Any]:
    """Shared align/distribute tail. Do not catch: disposal and a bad index must reach execute_safe."""
    from plugin.draw.bridge import DrawBridge

    if len(indices) < min_count:
        return tool._tool_error(too_few)
    page, actual_idx = DrawBridge(ctx.doc).resolve_page(page_kwargs.get("page"), active_page_index=ctx.active_page_index)
    shapes: list[Any] = []
    resolved_indices: list[int | str] = []
    for ref in indices:
        addr, shape, _parent, err_res = _lookup_ref(tool, page, ref)
        if err_res is not None:
            return err_res
        assert shape is not None and addr is not None
        shapes.append(shape)
        resolved_indices.append(addr)
    dup_err = _reject_duplicate_shapes(tool, shapes)
    if dup_err is not None:
        return dup_err
    new_boxes = layout_fn([_shape_box(s) for s in shapes], layout_arg)
    for shape, box in zip(shapes, new_boxes):
        _apply_box(shape, box)
    res: dict[str, Any] = {"status": "ok", "message": ok_message, "page": actual_idx, "indices": resolved_indices}
    if any(_outside_page(page, s) for s in shapes):
        res["warnings"] = [_OUTSIDE_PAGE_WARNING]
    return res


class AlignShapes(_AnyDocumentShapeTool):
    name: str | None = "shape_align"
    description: str = (
        "Align multiple shapes on a page to a shared edge or center axis. "
        "Needs at least two indices or Names."
        + _DIGIT_REF_SCHEMA_NOTE
    )
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "page": DRAW_PAGE_PROPERTY,
            "indices": {"type": "array", "items": {"type": ["integer", "string"]}, "description": "Shape indices or Names to align"},
            "alignment": {"type": "string", "enum": list(layout.ALIGNMENTS), "description": "Alignment axis"},
        },
        "required": ["indices", "alignment"],
    }
    is_mutation: bool | None = True

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        indices = kwargs["indices"]
        assert isinstance(indices, list)
        # Schema requires alignment and rejects values outside the enum, including "".
        alignment = kwargs["alignment"]
        return _layout_on_page(self, ctx, indices, alignment, min_count=2, too_few="At least two shape indices are required to align.", layout_fn=layout.align_boxes, ok_message="Shapes aligned", page_kwargs=kwargs)


class DistributeShapes(_AnyDocumentShapeTool):
    name: str | None = "shape_distribute"
    description: str = "Evenly distribute three or more shapes between the first and last along an axis." + _DIGIT_REF_SCHEMA_NOTE
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "page": DRAW_PAGE_PROPERTY,
            "indices": {"type": "array", "items": {"type": ["integer", "string"]}, "description": "Shape indices or Names to distribute"},
            "axis": {"type": "string", "enum": list(layout.DISTRIBUTE_AXES), "description": "Distribution axis"},
        },
        "required": ["indices", "axis"],
    }
    is_mutation: bool | None = True

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        indices = kwargs["indices"]
        assert isinstance(indices, list)
        # Schema requires axis and rejects values outside the enum, including "".
        axis = kwargs["axis"]
        return _layout_on_page(self, ctx, indices, axis, min_count=3, too_few="At least three shape indices are required to distribute.", layout_fn=layout.distribute_boxes, ok_message="Shapes distributed", page_kwargs=kwargs)


class CreateDiagram(_AnyDocumentShapeTool):
    name: str | None = "shape_create_diagram"
    description: str = "Create a flowchart/diagram of multiple nodes and connectors in one turn. Node x/y/width/height: 1/100 mm. Auto layouts: horizontal_flow, vertical_flow, grid; custom requires x/y on each node."
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "page": DRAW_PAGE_PROPERTY,
            "layout": {"type": "string", "enum": list(layout.DIAGRAM_LAYOUTS), "default": "horizontal_flow", "description": "How to place nodes"},
            "nodes": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "description": "Node id used in connections"},
                        "text": {"type": "string"},
                        "shape_type": {"type": "string"},
                        "x": {"type": "integer"},
                        "y": {"type": "integer"},
                        "width": {"type": "integer"},
                        "height": {"type": "integer"},
                        "fill_color": {"type": "string"},
                    },
                    "required": ["id", "text"],
                },
            },
            "connections": {"type": "array", "items": {"type": "object", "properties": {"from": {"type": "string"}, "to": {"type": "string"}, "line_color": {"type": "string"}}, "required": ["from", "to"]}},
        },
        "required": ["nodes"],
    }
    is_mutation: bool | None = True

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        from plugin.draw.bridge import DrawBridge
        from plugin.draw.layout import diagram_node_boxes

        nodes = kwargs["nodes"]
        assert isinstance(nodes, list)
        if not nodes:
            return self._tool_error("nodes is required and must be non-empty.")
        raw_ids = [n.get("id") for n in nodes]
        if any(nid is None or nid == "" for nid in raw_ids):
            return self._tool_error("Each node needs an id.")
        ids = [str(nid) for nid in raw_ids]
        if len(ids) != len(set(ids)):
            return self._tool_error("Node ids must be unique.")

        page, actual_idx = DrawBridge(ctx.doc).resolve_page(kwargs.get("page"), active_page_index=ctx.active_page_index)
        connections = kwargs.get("connections") or []
        known_ids = set(ids)
        for conn in connections:
            if str(conn.get("from")) not in known_ids or str(conn.get("to")) not in known_ids:
                return self._tool_error("Unknown connection endpoint: %s -> %s" % (conn.get("from"), conn.get("to")))

        layout_name = kwargs["layout"]
        from plugin.draw.bridge import page_size

        page_w, page_h = page_size(page) or (layout.DEFAULT_PAGE_WIDTH, layout.DEFAULT_PAGE_HEIGHT)
        try:
            boxes = diagram_node_boxes(nodes, layout_name, page_width=page_w, page_height=page_h)
        except ValueError as exc:
            return self._tool_error(str(exc))

        id_to_index: dict[str, int] = {}
        created: list[dict[str, Any]] = []
        warnings: list[str] = []
        # Objects in create order (nodes, then each connector that succeeded).
        # page.remove(shape) does not use the index from create time. Deleting
        # a node by that index shifts later connectors onto node slots, and
        # connectors were not in `created`, so a failed connector left the
        # earlier connectors on the page.
        tracked: list[Any] = []

        def cleanup_shapes() -> None:
            # No outer try: reversed() and the list itself do not raise.
            # The per-shape try is the cleanup. A closed document must not
            # be swallowed as a failed cleanup, but a live-document
            # RuntimeException must not strand the remaining shapes.
            for shape in reversed(tracked):
                try:
                    page.remove(shape)
                except Exception as exc:
                    if is_disposal(exc):
                        raise

        for node, box in zip(nodes, boxes):
            x, y, w, h = box
            create_kwargs = {"action": "create", "page": actual_idx, "shape_type": node.get("shape_type") or "rectangle", "x": x, "y": y, "width": w, "height": h, "text": node.get("text") or ""}
            if node.get("fill_color"):
                create_kwargs["fill_color"] = node["fill_color"]
            result = _execute_upsert_shape(self, ctx, **create_kwargs)
            if isinstance(result, dict) and result.get("status") != "ok":
                cleanup_shapes()
                return result
            warnings.extend(result.get("warnings", []))
            idx = result.get("index")
            if not isinstance(idx, int):
                cleanup_shapes()
                return self._tool_error("shape_upsert did not return a shape index.")
            tracked.append(page.getByIndex(idx))
            id_to_index[str(node["id"])] = idx
            created.append({"id": node["id"], "index": idx, "name": result.get("name"), "x": x, "y": y, "width": w, "height": h})

        connected = []
        for conn in connections:
            src = id_to_index[str(conn.get("from"))]
            dst = id_to_index[str(conn.get("to"))]
            conn_kwargs = {"start": src, "end": dst, "page": actual_idx}
            if conn.get("line_color"):
                conn_kwargs["line_color"] = conn["line_color"]
            result = _execute_connect_shapes(self, ctx, **conn_kwargs)
            if isinstance(result, dict) and result.get("status") != "ok":
                cleanup_shapes()
                return result
            warnings.extend(result.get("warnings", []))
            conn_index = result.get("index")
            if not isinstance(conn_index, int):
                cleanup_shapes()
                return self._tool_error("shape_connect did not return a connector index.")
            tracked.append(page.getByIndex(conn_index))
            connected.append({"from": conn.get("from"), "to": conn.get("to"), "index": conn_index})

        out: dict[str, Any] = {"status": "ok", "message": "Diagram created", "page": actual_idx, "layout": layout_name, "nodes": created, "id_to_index": id_to_index, "connections": connected}
        if warnings:
            out["warnings"] = list(dict.fromkeys(warnings))
        return out


class DeleteShape(_AnyDocumentShapeTool):
    name: str | None = "shape_delete"
    description: str = "Deletes a shape by page index, dotted path, or Name."
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "index": {
                "type": ["integer", "string"],
                "description": '0-based index or dotted group path such as "2.0.1". When both index and name are passed, index wins.',
            },
            "name": {"type": "string", "description": "Shape Name (use for all-digit names that would be parsed as an index)."},
            "page": DRAW_PAGE_PROPERTY,
        },
        "required": [],
    }
    is_mutation: bool | None = True

    def validate(self, *, doc_type: str | None = None, **kwargs: Any) -> tuple[Literal[False], str] | tuple[Literal[True], None]:
        ok, err = super().validate(doc_type=doc_type, **kwargs)
        if not ok:
            return False, err or "Invalid arguments"
        if "index" not in kwargs and not str(kwargs.get("name") or "").strip():
            return False, "Pass index or name to address the shape."
        return True, None

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        from plugin.draw.bridge import DrawBridge

        page, actual_idx = DrawBridge(ctx.doc).resolve_page(kwargs.get("page"), active_page_index=ctx.active_page_index)
        if "index" in kwargs:
            _addr, shape, parent, err_res = _lookup_ref(self, page, kwargs["index"])
        else:
            _addr, shape, parent, err_res = _lookup_ref(self, page, kwargs["name"], by_name=True)
        if err_res is not None:
            return err_res
        assert parent is not None and shape is not None
        try:
            parent.remove(shape)
        except Exception as exc:
            if is_disposal(exc):
                raise
            label = kwargs.get("index", kwargs.get("name"))
            return self._tool_error("Failed to delete shape at index %s: %s" % (label, exc))
        return {"status": "ok", "message": "Shape deleted", "page": actual_idx}
