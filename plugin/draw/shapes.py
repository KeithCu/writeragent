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
from typing import TYPE_CHECKING, Any, Literal

from plugin.doc.visual_helpers import SHAPE_TOOL_UNO_SERVICES, apply_character_properties, parse_color_to_uno_int
from plugin.framework.errors import WriterAgentException, is_disposed_exception, is_real_disposal
from . import layout
from .base import ToolDrawShapeBase

if TYPE_CHECKING:
    from plugin.framework.tool import ToolBase, ToolContext

log = logging.getLogger(__name__)

_DRAW_SHAPE_DOCS = list(SHAPE_TOOL_UNO_SERVICES)


class _AnyDocumentShapeTool(ToolDrawShapeBase):
    """Writer and Calc as well as Draw and Impress.

    ToolDrawShapeBase only lists Drawing and Presentation. Align, distribute,
    and diagram stay on that base.
    """

    uno_services: list[str] | None = _DRAW_SHAPE_DOCS
    doc_types: list[str] | None = ["writer", "calc", "draw", "impress"]


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
        if is_real_disposal(ex):
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
            if is_real_disposal(ex):
                raise
            log.debug("create_shape writer_at_page_finalize: readback failed: %s", ex)
    except Exception as ex:
        if is_real_disposal(ex):
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
        if is_real_disposal(ex):
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
        if is_real_disposal(ex):
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
        if is_real_disposal(ex):
            raise
        log.debug("create_shape enhanced_geometry: failed type=%r err=%s: %s", custom_shape_type, type(ex).__name__, ex)
        return False, f"{type(ex).__name__}: {ex}"


class GetDrawSummary(_AnyDocumentShapeTool):
    name: str | None = "shape_summary"
    intent: str | None = "read"
    description: str = (
        "Returns a summary of shapes on the active or specified page. "
        'Child shapes in groups use a dotted index (e.g. "2.0.1") which edit and delete accept.'
    )
    parameters: dict[str, Any] | None = {"type": "object", "properties": {"page": {"type": "integer", "description": "0-based page index (active page if omitted)"}}, "required": []}

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        from plugin.draw.bridge import resolve_shape_page
        from plugin.draw.tree import iter_shape_addresses

        page, actual_idx, err = resolve_shape_page(ctx, kwargs)
        if err or page is None:
            return self._tool_error(err or "No draw page available.")

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


class DrawShapes:
    def _is_valid_position(self, position: Any) -> bool:
        if not hasattr(position, "X") or not hasattr(position, "Y"):
            return False
        return True

    def _is_valid_size(self, size: Any, shape_type: str | None = None) -> bool:
        """Reject a size LibreOffice will not use as a shape box.

        ``Width <= 0`` or ``Height <= 0`` must not raise
        ``DRAW_INVALID_SIZE`` for ``shape_type`` ``line``. A horizontal
        or vertical line is a valid shape. Treating every shape like a
        rectangle rejects it: a ``LineShape`` uses its bounding box, and
        an axis-aligned line has a zero width (vertical) or a zero
        height (horizontal). ``LineShape`` may have one zero side.
        Other shapes still need both sides positive. A negative side,
        or both sides zero, is still invalid.
        """
        if not hasattr(size, "Width") or not hasattr(size, "Height"):
            return False
        width = size.Width
        height = size.Height
        if width < 0 or height < 0:
            return False
        if width > 0 and height > 0:
            return True
        if width == 0 and height == 0:
            return False
        return _is_line_shape_type(shape_type)

    def safe_create_shape(self, doc: Any, page: Any, shape_type: str, position: Any, size: Any, custom_shape_type: str | None = None) -> tuple[Any, bool | None, str | None]:
        """Safely create shape with error handling.

        Shapes are created via the document's factory (``doc.createInstance``);
        ``XDrawPage`` is not a reliable ``createInstance`` source in UNO.

        For CustomShape, ``EnhancedCustomShapeGeometry`` must be applied **before**
        ``page.add`` to prevent an immediate abort on some LibreOffice versions. Writer
        and Calc will require re-applying it after add to paint correctly.
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
            if is_disposed_exception(e):
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
            if is_real_disposal(ex):
                raise
            log.debug("setPropertyValue %s failed", prop, exc_info=True)
    if not apply_autofit:
        return
    try:
        import sys

        fit_enum: Any = sys.modules.get("com.sun.star.drawing.TextFitToSizeType")
        if not fit_enum:
            from com.sun.star.drawing import TextFitToSizeType as fit_enum

        shape.setPropertyValue("TextFitToSize", fit_enum.AUTOFIT)
    except Exception as ex:
        if is_real_disposal(ex):
            raise
        log.debug("set TextFitToSize AUTOFIT failed", exc_info=True)


def _failed_prop(failed: list[str], name: str, exc: BaseException) -> None:
    """Record a property that did not apply; a closed document still propagates."""
    if is_real_disposal(exc):
        raise
    failed.append(name)


def _apply_shape_properties(shape: Any, kwargs: dict[str, Any]) -> list[str]:
    """Helper to apply rich formatting properties to a shape. Returns list of failed property names."""
    failed_props: list[str] = []

    # "text" in kwargs (not truthy) so paper-form fills can write "" or keep a Name-only edit.
    if "text" in kwargs and hasattr(shape, "setString"):
        try:
            # Schema already exposes font_size/font_name; explicit font_size opts out of AUTOFIT.
            if kwargs.get("action") == "edit":
                apply_autofit = ("font_size" not in kwargs) and ("width" in kwargs or "height" in kwargs)
            else:
                apply_autofit = "font_size" not in kwargs
            _clamp_shape_text_autogrow(shape, apply_autofit=apply_autofit)
            shape.setString("" if kwargs["text"] is None else str(kwargs["text"]))
        except Exception as exc:
            _failed_prop(failed_props, "text", exc)

    if kwargs.get("name") and hasattr(shape, "Name"):
        try:
            shape.Name = str(kwargs["name"])
        except Exception as exc:
            _failed_prop(failed_props, "name", exc)

    # Background/Fill Color
    if kwargs.get("fill_color"):
        color_str = kwargs.get("fill_color")
        color_str_l = (color_str or "").strip().lower()
        if color_str_l in ("none", "transparent") and hasattr(shape, "FillStyle"):
            try:
                from com.sun.star.drawing import FillStyle

                shape.setPropertyValue("FillStyle", FillStyle.NONE)
            except Exception as exc:
                _failed_prop(failed_props, "fill_color", exc)
        else:
            color = parse_color_to_uno_int(color_str)
            if color is None:
                failed_props.append("fill_color")
            else:
                try:
                    prop = "LineColor" if "LineShape" in shape.getShapeType() else "FillColor"
                    shape.setPropertyValue(prop, color)
                except Exception as exc:
                    _failed_prop(failed_props, "fill_color", exc)

    # Fill Style (solid, transparent, etc)
    if kwargs.get("fill_style") and hasattr(shape, "FillStyle"):
        try:
            import sys

            fill_enum = sys.modules.get("com.sun.star.drawing.FillStyle")
            if not fill_enum:
                from com.sun.star.drawing import FillStyle

                fill_enum = FillStyle
            style_str = kwargs["fill_style"].lower()
            if style_str == "none" or style_str == "transparent":
                shape.setPropertyValue("FillStyle", fill_enum.NONE)
            elif style_str == "solid":
                shape.setPropertyValue("FillStyle", fill_enum.SOLID)
        except Exception as exc:
            _failed_prop(failed_props, "fill_style", exc)

    # Line Color
    if kwargs.get("line_color"):
        color = parse_color_to_uno_int(kwargs["line_color"])
        if color is None:
            failed_props.append("line_color")
        else:
            try:
                shape.setPropertyValue("LineColor", color)
            except Exception as exc:
                _failed_prop(failed_props, "line_color", exc)

    # Line Width
    if kwargs.get("line_width") is not None and hasattr(shape, "LineWidth"):
        try:
            shape.setPropertyValue("LineWidth", int(kwargs["line_width"]))
        except Exception as exc:
            _failed_prop(failed_props, "line_width", exc)

    # Line Style (ensure border is visible when colored or sized)
    if (kwargs.get("line_color") or kwargs.get("line_width") is not None) and hasattr(shape, "LineStyle"):
        try:
            import sys

            line_enum: Any = sys.modules.get("com.sun.star.drawing.LineStyle")
            if not line_enum:
                from com.sun.star.drawing import LineStyle as line_enum
            shape.setPropertyValue("LineStyle", line_enum.SOLID)
        except Exception as exc:
            _failed_prop(failed_props, "line_style", exc)

    # Text Properties (Font Size, Name, Color)
    if kwargs.get("text_color") or kwargs.get("font_size") or kwargs.get("font_name"):
        try:
            char_res = apply_character_properties(shape, font_name=kwargs.get("font_name"), font_size_pt=kwargs.get("font_size"), color=kwargs.get("text_color"))
            for prop_name, ok in char_res.items():
                if not ok:
                    failed_props.append(prop_name)
        except Exception as exc:
            _failed_prop(failed_props, "text_properties", exc)

    # Rotation
    if kwargs.get("rotation_angle") is not None and hasattr(shape, "RotateAngle"):
        try:
            # Angle is in 100ths of a degree
            shape.setPropertyValue("RotateAngle", int(kwargs["rotation_angle"] * 100))
        except Exception as exc:
            _failed_prop(failed_props, "rotation_angle", exc)

    return failed_props


# CustomShape flowchart-* type strings (valid at runtime; omitted from create_shape schema description for brevity):
# flowchart-alternate-process, flowchart-card, flowchart-collate, flowchart-connector, flowchart-data,
# flowchart-decision, flowchart-delay, flowchart-direct-access-storage, flowchart-display, flowchart-document,
# flowchart-extract, flowchart-internal-storage, flowchart-magnetic-disk, flowchart-manual-input,
# flowchart-manual-operation, flowchart-merge, flowchart-multidocument, flowchart-off-page-connector,
# flowchart-or, flowchart-predefined-process, flowchart-preparation, flowchart-process, flowchart-punched-tape,
# flowchart-sequential-access, flowchart-sort, flowchart-stored-data, flowchart-summing-junction,
# flowchart-terminator
#    "flowchart-* (e.g. flowchart-process, flowchart-decision; full set omitted for brevity); "
#   "fontwork-*; scrollbars (horizontal-scroll, vertical-scroll); "

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
    w, h = getattr(page, "Width", None), getattr(page, "Height", None)
    if not isinstance(w, int) or not isinstance(h, int) or w <= 0 or h <= 0:
        return False  # Tip: Calc sheet draw pages have no size or report 0; skip.
    pos, size = shape.getPosition(), shape.getSize()
    x, y = getattr(pos, "X", None), getattr(pos, "Y", None)
    sw, sh = getattr(size, "Width", None), getattr(size, "Height", None)
    if not isinstance(x, int) or not isinstance(y, int) or not isinstance(sw, int) or not isinstance(sh, int):
        return False
    return x < 0 or y < 0 or x + sw > w or y + sh > h


class UpsertShape(_AnyDocumentShapeTool):
    name: str | None = "shape_upsert"
    description: str = "Create or edit a shape on a page. When filling a paper-form blank, edit by shape Name from get_draw_tree — draw-page index shifts when other shapes sit between fields."
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["create", "edit"], "description": "Action to perform: 'create' a new shape, or 'edit' an existing one."},
            "index": {
                "type": ["integer", "string"],
                "description": '0-based index of the shape on the page, or a dotted group path such as "2.0.1" (edit: pass index or name)',
            },
            "name": {"type": "string", "description": "Shape Name. Edit: look up by Name when index is omitted. Create: set the new shape's Name so later fills stay stable."},
            "page": {"type": "integer", "description": "0-based page index (active page if omitted)"},
            "shape_type": {"type": "string", "description": _CREATE_SHAPE_SHAPE_TYPE_DESC + " (required only for action='create')"},
            "x": {"type": "integer", "description": "X position (100ths of mm) (required only for action='create')"},
            "y": {"type": "integer", "description": "Y position (100ths of mm) (required only for action='create')"},
            "width": {"type": "integer", "description": "Width (100ths of mm) (required only for action='create')"},
            "height": {"type": "integer", "description": "Height (100ths of mm) (required only for action='create')"},
            "text": {"type": "string", "description": "Text content"},
            "fill_color": {"type": "string", "description": "Fill color. Hex (#FF0000) or name (red)"},
            "fill_style": {"type": "string", "enum": ["solid", "transparent", "none"], "description": "Fill style"},
            "line_color": {"type": "string", "description": "Line border color"},
            "line_width": {"type": "integer", "description": "Line width (100ths of mm)"},
            "text_color": {"type": "string", "description": "Text character color"},
            "font_size": {"type": "number", "description": "Optional. Omit for TextFitToSize AUTOFIT (auto-size to the shape box); that is usually correct. Only set a point size when you need a specific fixed size."},
            "font_name": {"type": "string", "description": "Font family name"},
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
        if not action:
            return False, "Missing required parameter: 'action' must be 'create' or 'edit'"

        if action == "create":
            required = ["shape_type", "x", "y", "width", "height"]
            for r in required:
                if r not in kwargs:
                    return False, f"Parameter '{r}' is required when action is 'create'"
        elif action == "edit":
            if "index" not in kwargs and not str(kwargs.get("name") or "").strip():
                return False, "Parameter 'index' or 'name' is required when action is 'edit'"
        else:
            return False, f"Unknown action: '{action}'. Must be 'create' or 'edit'"

        return True, None

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        return _execute_upsert_shape(self, ctx, **kwargs)


def _execute_upsert_shape(tool: ToolBase, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
    from plugin.draw.bridge import DrawBridge, resolve_shape_page
    from com.sun.star.awt import Point, Size

    action = kwargs["action"]
    bridge = DrawBridge(ctx.doc)
    page, actual_idx, err = resolve_shape_page(ctx, kwargs, bridge=bridge)
    if err or page is None:
        return tool._tool_error(err or "No draw page available.")

    if action == "create":
        type_map = {"rectangle": "RectangleShape", "ellipse": "EllipseShape", "text": "TextShape", "line": "LineShape", "connector": "ConnectorShape"}
        shape_type_raw = kwargs["shape_type"]

        position = Point(kwargs["x"], kwargs["y"])
        size = Size(kwargs["width"], kwargs["height"])

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

        log.debug("shape_upsert (create) branch: raw=%r resolved_uno=%r is_custom_catalog=%s catalog_name=%r", shape_type_raw, uno_type, is_custom_shape, custom_shape_type if is_custom_shape else None)

        draw_shapes = DrawShapes()

        try:
            # Apply EnhancedCustomShapeGeometry *inside* safe_create_shape (before
            # page.add). Setting Type=octagon after add replaces the live SdrRectObj
            # and can abort in SfxItemPool::unregisterNameOrIndex (soffice SIGABRT).
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

            # Re-apply EnhancedCustomShapeGeometry after add. While Draw works with the pre-add
            # geometry, Writer (after AT_PAGE anchor) and Calc require re-applying it so
            # CustomShapes actually paint correctly on the sheet/page.
            if is_custom_shape and custom_shape_type and ctx.doc is not None and (ctx.doc.supportsService("com.sun.star.text.TextDocument") or ctx.doc.supportsService("com.sun.star.sheet.SpreadsheetDocument")):
                geometry_applied, geometry_error = _apply_enhanced_custom_shape_type(shape, custom_shape_type)

            failed_props = _apply_shape_properties(shape, kwargs)
            # setString can still resize Writer AT_PAGE custom shapes (Arch: 4001x4001 → 2249x489).
            _try_writer_reapply_position_after_anchor(ctx.doc, shape, position, size)
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

        result: dict[str, Any] = {"status": "ok", "message": f"Created {shape_type_raw}", "index": shape_index, "page": page_index, "shape_count_after": shape_count_after}
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

        # Index is fragile for peer "fill field X"; Name from get_draw_tree stays stable
        # when non-fields sit between boxes. Index wins when both are passed.
        lookup_name = kwargs.get("name") if "index" not in kwargs else None
        shape_idx, shape, _parent, find_err = find_shape_on_page(page, index=kwargs.get("index"), name=lookup_name)
        if find_err or shape is None:
            return tool._tool_error(find_err or "Shape not found.")

        if "x" in kwargs or "y" in kwargs:
            pos = shape.getPosition()
            shape.setPosition(Point(kwargs.get("x", pos.X), kwargs.get("y", pos.Y)))
        if "width" in kwargs or "height" in kwargs:
            size = shape.getSize()
            shape.setSize(Size(kwargs.get("width", size.Width), kwargs.get("height", size.Height)))

        failed_props = _apply_shape_properties(shape, kwargs)
        if "text" in kwargs and ("width" in kwargs or "height" in kwargs):
            size = shape.getSize()
            shape.setSize(Size(kwargs.get("width", size.Width), kwargs.get("height", size.Height)))

        result = {"status": "ok", "message": "Shape updated", "page": actual_idx, "index": shape_idx, "name": getattr(shape, "Name", "") or ""}
        if failed_props:
            result["warnings"] = [f"Failed to apply properties: {', '.join(failed_props)}"]
        if _outside_page(page, shape):
            result["warnings"] = result.get("warnings", []) + [_OUTSIDE_PAGE_WARNING]

        return result

    # validate() rejects other actions; keep execute total so the ToolBase
    # override is dict[str, Any] (Calc/Writer inherit this class).
    return tool._tool_error("Unknown action: '%s'. Must be 'create' or 'edit'" % action)


def _resolve_mutation_page(tool: ToolBase, ctx: ToolContext, kwargs: dict[str, Any]) -> tuple[Any, dict[str, Any] | None]:
    from plugin.draw.bridge import mutation_page
    from plugin.framework.errors import ToolExecutionError

    try:
        return mutation_page(ctx, kwargs.get("page")), None
    except ToolExecutionError as exc:
        return None, tool._tool_error(str(exc))


class ConnectShapes(_AnyDocumentShapeTool):
    """Connect two shapes with a connector."""

    name: str | None = "shape_connect"
    intent: str | None = "edit"
    description: str = "Connect two shapes on the same page with a connector."
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "start": {"type": "integer", "description": "Index of the starting shape."},
            "end": {"type": "integer", "description": "Index of the ending shape."},
            "page": {"type": "integer", "description": "Page index containing the shapes"},
            "line_color": {"type": "string", "description": "Color of the connector line"},
            "line_width": {"type": "integer", "description": "Line width (100ths of mm)"},
        },
        "required": ["start", "end"],
    }
    is_mutation: bool | None = True

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        return _execute_connect_shapes(self, ctx, **kwargs)


def _execute_connect_shapes(tool: ToolBase, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
    from com.sun.star.awt import Point, Size

    page, err = _resolve_mutation_page(tool, ctx, kwargs)
    if err is not None:
        return err

    # Schema requires start and end as integers. validate() rejects a
    # missing or non-integer value before execute.
    start_idx = kwargs["start"]
    end_idx = kwargs["end"]

    try:
        start_shape = page.getByIndex(start_idx)
        end_shape = page.getByIndex(end_idx)
    except Exception as e:
        if is_disposed_exception(e):
            raise
        return tool._tool_error(f"Failed to find shapes at given indices: {str(e)}")

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

    except Exception:
        # The connector is already on the page. Remove it so a failed
        # connect does not leave an unbound shape.
        try:
            page.remove(shape)
        except Exception as remove_exc:
            if is_disposed_exception(remove_exc):
                raise
        raise

    res: dict[str, Any] = {"status": "ok", "message": f"Connected shape {start_idx} to {end_idx}", "index": page.getCount() - 1}
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
    intent: str | None = "edit"
    description: str = (
        "Group related shapes into one composite object so the user can select and move them together. "
        "After you place pieces that belong to the same thing (a flag, logo, diagram, icon), "
        "ALWAYS call this before finishing — leaving loose parts is wrong. "
        "Pass every related shape index. "
        "The result is one group shape you can move with shape_upsert (action edit)."
    )
    parameters: dict[str, Any] | None = {"type": "object", "properties": {"indices": {"type": "array", "items": {"type": "integer"}, "description": "List of shape indices to group."}, "page": {"type": "integer", "description": "Page index containing the shapes"}}, "required": ["indices"]}
    is_mutation: bool | None = True

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        page, err = _resolve_mutation_page(self, ctx, kwargs)
        if err is not None:
            return err

        indices = kwargs["indices"]
        assert isinstance(indices, list)
        if not indices or len(indices) < 2:
            return self._tool_error("At least two shape indices are required to group.")

        # page.group is the first page mutation. A failure propagates to
        # execute_safe (TOOL_EXECUTION_ERROR and a log). The old catch
        # repeated "Failed to group shapes" and dropped the traceback.
        shape_collection = _create_shape_collection(ctx.ctx)
        for i in indices:
            shape = page.getByIndex(i)
            shape_collection.add(shape)
        page.group(shape_collection)

        return {
            "status": "ok",
            "message": f"Grouped {len(indices)} shapes.",
            "index": page.getCount() - 1,  # Grouping replaces the individuals with this group shape
        }


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


class AlignShapes(ToolDrawShapeBase):
    name: str | None = "align_shapes"
    intent: str | None = "edit"
    description: str = "Align multiple shapes on a page to a shared edge or center axis. Coordinates are 1/100 mm. Needs at least two indices."
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "page": {"type": "integer", "description": "0-based page index (active page if omitted)"},
            "indices": {"type": "array", "items": {"type": "integer"}, "description": "Shape indices to align"},
            "alignment": {"type": "string", "enum": list(layout.ALIGNMENTS), "description": "Alignment axis"},
        },
        "required": ["indices", "alignment"],
    }
    is_mutation: bool | None = True

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        from plugin.draw.bridge import resolve_shape_page
        from plugin.draw.layout import align_boxes

        indices = kwargs["indices"]
        assert isinstance(indices, list)
        # Schema requires alignment and rejects values outside the enum, including "".
        alignment = kwargs["alignment"]
        if len(indices) < 2:
            return self._tool_error("At least two shape indices are required to align.")
        page, actual_idx, err = resolve_shape_page(ctx, kwargs)
        if err or page is None:
            return self._tool_error(err or "No draw page available.")
        # A bad index and align_boxes ValueError propagate. The old catch
        # returned str(exc) and reported a closed document as a normal tool error.
        shapes = [page.getByIndex(i) for i in indices]
        boxes = [_shape_box(s) for s in shapes]
        new_boxes = align_boxes(boxes, alignment)
        for shape, box in zip(shapes, new_boxes):
            _apply_box(shape, box)
        res: dict[str, Any] = {"status": "ok", "message": "Shapes aligned", "page": actual_idx, "indices": list(indices)}
        if any(_outside_page(page, s) for s in shapes):
            res["warnings"] = [_OUTSIDE_PAGE_WARNING]
        return res


class DistributeShapes(ToolDrawShapeBase):
    name: str | None = "distribute_shapes"
    intent: str | None = "edit"
    description: str = "Evenly distribute three or more shapes between the first and last along an axis. Coordinates are 1/100 mm."
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "page": {"type": "integer", "description": "0-based page index (active page if omitted)"},
            "indices": {"type": "array", "items": {"type": "integer"}, "description": "Shape indices to distribute"},
            "axis": {"type": "string", "enum": list(layout.DISTRIBUTE_AXES), "description": "Distribution axis"},
        },
        "required": ["indices", "axis"],
    }
    is_mutation: bool | None = True

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        from plugin.draw.bridge import resolve_shape_page
        from plugin.draw.layout import distribute_boxes

        indices = kwargs["indices"]
        assert isinstance(indices, list)
        # Schema requires axis and rejects values outside the enum, including "".
        axis = kwargs["axis"]
        if len(indices) < 3:
            return self._tool_error("At least three shape indices are required to distribute.")
        page, actual_idx, err = resolve_shape_page(ctx, kwargs)
        if err or page is None:
            return self._tool_error(err or "No draw page available.")
        # Same as align: do not catch. A closed document must reach execute_safe.
        shapes = [page.getByIndex(i) for i in indices]
        boxes = [_shape_box(s) for s in shapes]
        new_boxes = distribute_boxes(boxes, axis)
        for shape, box in zip(shapes, new_boxes):
            _apply_box(shape, box)
        res = {"status": "ok", "message": "Shapes distributed", "page": actual_idx, "indices": list(indices)}
        if any(_outside_page(page, s) for s in shapes):
            res["warnings"] = [_OUTSIDE_PAGE_WARNING]
        return res


class CreateDiagram(ToolDrawShapeBase):
    name: str | None = "create_diagram"
    intent: str | None = "edit"
    description: str = "Create a flowchart/diagram of multiple nodes and connectors in one turn. Node positions are 1/100 mm. Auto layouts: horizontal_flow, vertical_flow, grid; custom requires x/y on each node."
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "page": {"type": "integer", "description": "0-based page index (active page if omitted)"},
            "layout": {"type": "string", "enum": list(layout.DIAGRAM_LAYOUTS), "description": "How to place nodes (default: horizontal_flow)"},
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
        from plugin.draw.bridge import resolve_shape_page
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

        page, actual_idx, err = resolve_shape_page(ctx, kwargs)
        if err or page is None:
            return self._tool_error(err or "No draw page available.")
        connections = kwargs.get("connections") or []
        known_ids = set(ids)
        for conn in connections:
            if str(conn.get("from")) not in known_ids or str(conn.get("to")) not in known_ids:
                return self._tool_error("Unknown connection endpoint: %s -> %s" % (conn.get("from"), conn.get("to")))

        layout_name = kwargs.get("layout") or "horizontal_flow"
        page_w = int(getattr(page, "Width", layout.DEFAULT_PAGE_WIDTH) or layout.DEFAULT_PAGE_WIDTH)
        page_h = int(getattr(page, "Height", layout.DEFAULT_PAGE_HEIGHT) or layout.DEFAULT_PAGE_HEIGHT)
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
                    if is_real_disposal(exc):
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
            created.append({"id": node["id"], "index": idx, "x": x, "y": y, "width": w, "height": h})

        connected = []
        for conn in connections:
            src = id_to_index.get(str(conn.get("from")))
            dst = id_to_index.get(str(conn.get("to")))
            if src is None or dst is None:
                cleanup_shapes()
                return self._tool_error("Unknown connection endpoint: %s -> %s" % (conn.get("from"), conn.get("to")))
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
    intent: str | None = "edit"
    description: str = "Deletes a shape by page index or dotted path."
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "index": {
                "type": ["integer", "string"],
                "description": '0-based index of the shape on the page, or a dotted group path such as "2.0.1" (edit: pass index or name)',
            },
            "page": {"type": "integer", "description": "0-based page index (active page if omitted)"},
        },
        "required": ["index"],
    }
    is_mutation: bool | None = True

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        from plugin.draw.tree import find_shape_on_page

        page, err = _resolve_mutation_page(self, ctx, kwargs)
        if err is not None:
            return err
        shape_idx = kwargs["index"]
        _addr, shape, parent, find_err = find_shape_on_page(page, index=shape_idx)
        if find_err or shape is None:
            return self._tool_error(find_err or "Shape not found.")
        assert parent is not None
        try:
            parent.remove(shape)
        except Exception as exc:
            if is_disposed_exception(exc):
                raise
            return self._tool_error("Failed to delete shape at index %s: %s" % (shape_idx, exc))
        return {"status": "ok", "message": "Shape deleted"}
