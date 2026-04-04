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

import logging

from plugin.framework.errors import WriterAgentException
from plugin.framework.tool_base import ToolBase

log = logging.getLogger(__name__)

# LibreOffice interprets CustomShapeGeometry as EnhancedCustomShapeGeometry when this engine is set.
_ENHANCED_CUSTOM_SHAPE_ENGINE = "com.sun.star.drawing.EnhancedCustomShapeEngine"


class DrawError(WriterAgentException):
    """Draw-specific errors."""

    def __init__(self, message, code="DRAW_ERROR", context=None, details=None):
        super().__init__(message, code=code, context=context, details=details)


def _parse_color(color_str):
    if not color_str:
        return None
    color_str = color_str.strip().lower()
    names = {
        "red": 0xFF0000,
        "green": 0x00FF00,
        "blue": 0x0000FF,
        "yellow": 0xFFFF00,
        "white": 0xFFFFFF,
        "black": 0x000000,
        "orange": 0xFF8C00,
        "purple": 0x800080,
        "gray": 0x808080,
    }
    if color_str in names:
        return names[color_str]
    if color_str.startswith("#"):
        try:
            return int(color_str[1:], 16)
        except ValueError:
            return None
    return None


def _try_writer_anchor_shape_before_add(doc, shape) -> None:
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

        has_anchor = False
        try:
            ps = shape.getPropertySetInfo()
            has_anchor = ps is not None and ps.hasPropertyByName("AnchorType")
        except Exception as ex:
            log.debug("create_shape writer_anchor: PropertySetInfo failed: %s", ex)

        shape.setPropertyValue("AnchorType", AT_PAGE)
        log.debug(
            "create_shape writer_anchor: set AnchorType=AT_PAGE ok has_anchor_prop=%s",
            has_anchor,
        )
        try:
            cur = shape.getPropertyValue("AnchorType")
            log.debug("create_shape writer_anchor: readback AnchorType=%r", cur)
        except Exception as ex:
            log.debug("create_shape writer_anchor: readback AnchorType failed: %s", ex)
    except Exception as ex:
        log.warning(
            "create_shape writer_anchor: set AnchorType failed: %s: %s",
            type(ex).__name__,
            ex,
        )


def _log_shape_uno_snapshot(phase: str, shape) -> None:
    """Best-effort UNO snapshot for debugging visibility/geometry (Writer vs Draw)."""
    try:
        parts: list[str] = []
        try:
            parts.append(f"uno_type={shape.getShapeType()!r}")
        except Exception as ex:
            parts.append(f"getShapeType_err={ex!r}")
        try:
            pos = shape.getPosition()
            parts.append(f"pos=({pos.X},{pos.Y})")
        except Exception as ex:
            parts.append(f"pos_err={ex!r}")
        try:
            sz = shape.getSize()
            parts.append(f"size=({sz.Width}x{sz.Height})")
        except Exception as ex:
            parts.append(f"size_err={ex!r}")
        for name in (
            "AnchorType",
            "AnchorPageNo",
            "HoriOrient",
            "VertOrient",
            "HoriOrientPosition",
            "VertOrientPosition",
            "Visible",
            "Opaque",
            "FillStyle",
            "FillColor",
            "LineColor",
            "LineWidth",
            "CustomShapeEngine",
            "ZOrder",
            "TextWrap",
            "SurroundContour",
            "IsFollowingTextFlow",
        ):
            try:
                v = shape.getPropertyValue(name)
                parts.append(f"{name}={v!r}")
            except Exception:
                pass
        log.debug("create_shape snapshot [%s]: %s", phase, " ".join(parts))
    except Exception as ex:
        log.debug("create_shape snapshot [%s]: failed: %s", phase, ex)


def _log_custom_shape_geometry_dump(shape, phase: str) -> None:
    """Detailed ``CustomShapeGeometry`` dump (compare rectangle vs octagon)."""
    try:
        g = shape.getPropertyValue("CustomShapeGeometry")
        
        details = []
        if g is not None:
            # `g` is typically a tuple of PropertyValue
            for p in g:
                val = p.Value
                if isinstance(val, tuple):
                    # might be nested property values like in Path
                    nested = []
                    for np in val:
                        if hasattr(np, "Name") and hasattr(np, "Value"):
                            nested.append(f"{np.Name}:{np.Value}")
                        else:
                            nested.append(repr(np))
                    val = "{" + ", ".join(nested) + "}"
                details.append(f"{p.Name}={val}")
        
        log.debug("create_shape geometry_dump [%s] FULL: %s", phase, " | ".join(details))
    except Exception as ex:
        log.debug("create_shape geometry_dump [%s]: %s", phase, ex)


def _log_shape_property_names_sample(shape, phase: str, limit: int = 60) -> None:
    """Sorted sample of ``PropertySetInfo`` names — diff rectangle vs CustomShape in Writer."""
    try:
        ps = shape.getPropertySetInfo()
        if ps is None:
            log.debug("create_shape prop_names [%s]: no PropertySetInfo", phase)
            return
        names: list[str] = []
        for p in ps.getProperties():
            try:
                names.append(p.Name)
            except Exception:
                pass
        names.sort()
        log.debug(
            "create_shape prop_names [%s]: total=%s first_%s=%s",
            phase,
            len(names),
            limit,
            names[:limit],
        )
    except Exception as ex:
        log.debug("create_shape prop_names [%s]: failed: %s", phase, ex)


def _log_writer_document_shape_context(doc) -> None:
    """Writer-only: body enumeration count and URL (helps compare empty doc runs)."""
    try:
        if doc is None or not doc.supportsService("com.sun.star.text.TextDocument"):
            return
        url = ""
        try:
            if hasattr(doc, "getURL"):
                url = doc.getURL() or ""
        except Exception:
            pass
        n_para = -1
        try:
            text = doc.getText()
            enum = text.createEnumeration()
            n_para = 0
            while enum.hasMoreElements():
                enum.nextElement()
                n_para += 1
        except Exception as ex:
            log.debug("create_shape writer_doc_ctx: enum failed: %s", ex)
        log.debug(
            "create_shape writer_doc_ctx: body_elements_enumerated=%s url=%r",
            n_para,
            url[:120] if url else "",
        )
    except Exception as ex:
        log.debug("create_shape writer_doc_ctx: %s", ex)


def _try_writer_at_page_shape_finalize(doc, bridge, page, shape) -> None:
    """Writer: ``AT_PAGE`` shapes must set ``AnchorPageNo`` (1-based) and absolute orient or they may not paint.

    See ``com.sun.star.text.Shape`` — ``AnchorPageNo`` is only valid for ``AT_PAGE``.
    """
    try:
        if doc is None or not doc.supportsService("com.sun.star.text.TextDocument"):
            return
        idx = _page_index_for(bridge, page)
        anchor_no = idx + 1
        # IDL: com.sun.star.text.HoriOrientation/VertOrientation NONE = 0 (absolute position)
        shape.setPropertyValue("AnchorPageNo", anchor_no)
        shape.setPropertyValue("HoriOrient", 0)
        shape.setPropertyValue("VertOrient", 0)
        shape.setPropertyValue("HoriOrientRelation", 8)  # PAGE_PRINT_AREA
        shape.setPropertyValue("VertOrientRelation", 8)  # PAGE_PRINT_AREA
        log.debug(
            "create_shape writer_at_page_finalize: AnchorPageNo=%s draw_page_index=%s HoriOrient=0 VertOrient=0",
            anchor_no,
            idx,
        )
        try:
            log.debug(
                "create_shape writer_at_page_finalize: readback AnchorPageNo=%r HoriOrient=%r VertOrient=%r",
                shape.getPropertyValue("AnchorPageNo"),
                shape.getPropertyValue("HoriOrient"),
                shape.getPropertyValue("VertOrient"),
            )
        except Exception as ex:
            log.debug("create_shape writer_at_page_finalize: readback failed: %s", ex)
    except Exception as ex:
        log.warning(
            "create_shape writer_at_page_finalize: failed %s: %s",
            type(ex).__name__,
            ex,
        )


def _try_writer_reapply_position_after_anchor(doc, shape, position, size) -> None:
    """Writer: setting ``AnchorPageNo`` / orient can reset placement; re-apply ``Position``/``Size``."""
    try:
        if doc is None or not doc.supportsService("com.sun.star.text.TextDocument"):
            return
        shape.setPosition(position)
        shape.setSize(size)
        log.debug(
            "create_shape writer_reapply_pos: pos=(%s,%s) size=(%sx%s)",
            position.X,
            position.Y,
            size.Width,
            size.Height,
        )
    except Exception as ex:
        log.warning("create_shape writer_reapply_pos: %s: %s", type(ex).__name__, ex)


def _try_writer_invalidate_and_pump(doc) -> None:
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
        tk = win.getToolkit()
        if tk is not None:
            tk.processEventsToIdle()
        log.debug("create_shape writer_invalidate: invalidate(0) + processEventsToIdle")
    except Exception as ex:
        log.debug("create_shape writer_invalidate: %s", ex)


def _try_writer_select_created_shape(doc, shape) -> None:
    """Select the new shape so the view shows handles and scrolls to it if needed."""
    try:
        if doc is None or not doc.supportsService("com.sun.star.text.TextDocument"):
            return
        ctrl = doc.getCurrentController()
        if ctrl is None:
            return
        import uno

        sel = None
        try:
            t = uno.getTypeByName("com.sun.star.view.XSelectionSupplier")
            sel = ctrl.queryInterface(t)
        except Exception:
            pass
        if sel is None:
            try:
                from com.sun.star.view import XSelectionSupplier

                sel = ctrl.queryInterface(XSelectionSupplier)
            except Exception:
                sel = None
        if sel is None:
            log.debug("create_shape writer_select: no XSelectionSupplier")
            return
        sel.select(shape)
        log.debug("create_shape writer_select: controller.select(shape) ok")
    except Exception as ex:
        log.debug("create_shape writer_select: %s: %s", type(ex).__name__, ex)


def _log_create_shape_page_context(doc, bridge, page) -> None:
    """How the target draw page was chosen (Writer vs Draw / controller vs first page)."""
    try:
        is_writer = bool(doc and doc.supportsService("com.sun.star.text.TextDocument"))
        pages = bridge.get_pages()
        n_draw_pages = pages.getCount() if pages is not None else -1
        n_on_page = page.getCount() if page is not None else -1
        log.debug(
            "create_shape page_context: is_writer=%s draw_pages=%s shapes_on_target_page=%s",
            is_writer,
            n_draw_pages,
            n_on_page,
        )
        ctrl = doc.getCurrentController() if doc else None
        if ctrl is None:
            log.debug("create_shape page_context: controller=None")
            return
        has_cur = hasattr(ctrl, "getCurrentPage")
        log.debug("create_shape page_context: controller has getCurrentPage=%s", has_cur)
        if not has_cur:
            return
        try:
            cur = ctrl.getCurrentPage()
        except Exception as ex:
            log.debug("create_shape page_context: getCurrentPage raised %s: %s", type(ex).__name__, ex)
            return
        log.debug(
            "create_shape page_context: getCurrentPage is_none=%s same_ref_as_target=%s",
            cur is None,
            (cur is page) if cur is not None and page is not None else None,
        )
        try:
            if cur is not None and page is not None and cur is not page:
                eq = cur == page
                log.debug("create_shape page_context: target_page == controller_page (==) %s", eq)
        except Exception as ex:
            log.debug("create_shape page_context: page equality check failed: %s", ex)
    except Exception as ex:
        log.debug("create_shape page_context: failed: %s", ex)


def _page_index_for(bridge, page):
    """Index of ``page`` in the document's draw pages collection.

    ``uno.isSame`` is not available in all LibreOffice Python-UNO builds; fall back to
    identity and ``==`` (many UNO bindings implement equality for the same underlying object).
    """
    pages = bridge.get_pages()
    is_same = None
    try:
        import uno

        is_same = getattr(uno, "isSame", None)
    except ImportError:
        pass

    for i in range(pages.getCount()):
        p = pages.getByIndex(i)
        if p is page:
            return i
        try:
            if p == page:
                return i
        except Exception:
            pass
        if callable(is_same):
            try:
                if is_same(p, page):
                    return i
            except Exception:
                pass
    return 0


def _apply_enhanced_custom_shape_type(shape, custom_shape_type: str) -> tuple[bool, str | None]:
    """Set EnhancedCustomShape engine and geometry ``Type`` so names like ``octagon`` render."""
    from com.sun.star.beans import PropertyValue
    import uno
    prop = PropertyValue()
    prop.Name = "Type"
    prop.Value = custom_shape_type
    try:
        shape.setPropertyValue("CustomShapeEngine", _ENHANCED_CUSTOM_SHAPE_ENGINE)
        prop_seq = uno.Any("[]com.sun.star.beans.PropertyValue", (prop,))  # type: ignore
        uno.invoke(shape, "setPropertyValue", ("CustomShapeGeometry", prop_seq))
        log.debug(
            "create_shape enhanced_geometry: ok type=%r engine=%r",
            custom_shape_type,
            _ENHANCED_CUSTOM_SHAPE_ENGINE,
        )
        return True, None
    except Exception as ex:
        log.debug(
            "create_shape enhanced_geometry: failed type=%r err=%s: %s",
            custom_shape_type,
            type(ex).__name__,
            ex,
        )
        return False, f"{type(ex).__name__}: {ex}"


class ListPages(ToolBase):
    name = "list_pages"
    description = "Lists all pages (slides) in the document."
    parameters = {"type": "object", "properties": {}, "required": []}
    uno_services = ["com.sun.star.drawing.DrawingDocument"]
    doc_types = ["draw", "impress"]
    tier = "core"

    def execute(self, ctx, **kwargs):
        from plugin.modules.draw.bridge import DrawBridge
        bridge = DrawBridge(ctx.doc)
        pages = bridge.get_pages()
        return {
            "status": "ok",
            "pages": [f"Page {i}" for i in range(pages.getCount())],
            "count": pages.getCount(),
        }


class GetDrawSummary(ToolBase):
    name = "get_draw_summary"
    intent = "edit"
    description = "Returns a summary of shapes on the active or specified page."
    parameters = {
        "type": "object",
        "properties": {
            "page_index": {
                "type": "integer",
                "description": "0-based page index (active page if omitted)",
            }
        },
        "required": [],
    }
    uno_services = ["com.sun.star.drawing.DrawingDocument"]
    doc_types = ["draw", "impress"]

    def execute(self, ctx, **kwargs):
        from plugin.modules.draw.bridge import DrawBridge
        bridge = DrawBridge(ctx.doc)
        idx = kwargs.get("page_index")
        page = (
            bridge.get_pages().getByIndex(idx)
            if idx is not None
            else bridge.get_active_page()
        )
        if page is None:
            return self._tool_error("No draw page available or invalid page index.")
        shapes = []
        for i in range(page.getCount()):
            s = page.getByIndex(i)
            info = {
                "index": i,
                "type": s.getShapeType(),
                "x": s.getPosition().X,
                "y": s.getPosition().Y,
                "width": s.getSize().Width,
                "height": s.getSize().Height,
            }
            if hasattr(s, "getString"):
                info["text"] = s.getString()
            shapes.append(info)
        return {"status": "ok", "page_index": idx, "shapes": shapes}


class DrawShapes:
    def _is_valid_position(self, position):
        if not hasattr(position, "X") or not hasattr(position, "Y"):
            return False
        return True

    def _is_valid_size(self, size):
        if not hasattr(size, "Width") or not hasattr(size, "Height"):
            return False
        if size.Width <= 0 or size.Height <= 0:
            return False
        return True

    def safe_create_shape(
        self,
        doc,
        page,
        shape_type,
        position,
        size,
        custom_shape_type: str | None = None,
    ):
        """Safely create shape with error handling.

        Shapes are created via the document's factory (``doc.createInstance``);
        ``XDrawPage`` is not a reliable ``createInstance`` source in UNO.

        For Writer, ``CustomShape`` + ``EnhancedCustomShapeGeometry`` must be applied **before**
        ``page.add`` (after position/size). Applying geometry only after add breaks display while
        ``RectangleShape`` etc. are unaffected.
        """
        try:
            if doc is None:
                raise DrawError(
                    "Document is None",
                    code="DRAW_DOC_NULL",
                    details={"operation": "create_shape", "shape_type": shape_type}
                )
            # UNO XDrawPage can be falsy when it has zero shapes — use `is None`.
            if page is None:
                raise DrawError(
                    "Page is None",
                    code="DRAW_PAGE_NULL",
                    details={"operation": "create_shape", "shape_type": shape_type}
                )

            if not self._is_valid_position(position):
                raise DrawError(
                    f"Invalid position: {position}",
                    code="DRAW_INVALID_POSITION",
                    details={"position": position}
                )

            if not self._is_valid_size(size):
                raise DrawError(
                    f"Invalid size: {size}",
                    code="DRAW_INVALID_SIZE",
                    details={"size": size}
                )

            # Create shape (document MSF — same as DrawBridge.create_shape)
            if shape_type.startswith("com.sun.star."):
                full_type = shape_type
            else:
                full_type = f"com.sun.star.drawing.{shape_type}"

            shape = doc.createInstance(full_type)
            if shape is None:
                raise DrawError(
                    f"Failed to create shape of type: {shape_type}",
                    code="DRAW_SHAPE_CREATION_FAILED",
                    details={"shape_type": shape_type}
                )

            shape.setPosition(position)
            shape.setSize(size)

            geometry_applied: bool | None = None
            geometry_error: str | None = None
            if custom_shape_type:
                geometry_applied, geometry_error = _apply_enhanced_custom_shape_type(
                    shape, custom_shape_type
                )
                if not geometry_applied:
                    log.warning(
                        "create_shape safe_create: enhanced geometry failed before add type=%s err=%s",
                        custom_shape_type,
                        geometry_error,
                    )

            _try_writer_anchor_shape_before_add(doc, shape)

            # Add to page
            page.add(shape)

            try:
                n_after = page.getCount()
            except Exception:
                n_after = -1
            log.debug(
                "create_shape safe_create: added uno=%s page_shape_count=%s",
                full_type,
                n_after,
            )
            _log_shape_uno_snapshot("after_page_add", shape)

            return shape, geometry_applied, geometry_error

        except DrawError:
            # Re-raise our draw errors
            raise
        except Exception as e:
            # Wrap other exceptions
            raise DrawError(
                f"Failed to create shape: {str(e)}",
                code="DRAW_SHAPE_CREATION_ERROR",
                details={
                    "shape_type": shape_type,
                    "position": position,
                    "size": size,
                    "original_error": str(e),
                    "error_type": type(e).__name__
                }
            ) from e


def _apply_shape_properties(shape, kwargs):
    """Helper to apply rich formatting properties to a shape."""
    if kwargs.get("text") and hasattr(shape, "setString"):
        shape.setString(kwargs["text"])

    # Background/Fill Color
    if kwargs.get("bg_color") or kwargs.get("fill_color"):
        color_str = kwargs.get("fill_color") or kwargs.get("bg_color")
        color_str_l = (color_str or "").strip().lower()
        if color_str_l in ("none", "transparent") and hasattr(shape, "FillStyle"):
            try:
                from com.sun.star.drawing import FillStyle
                shape.setPropertyValue("FillStyle", FillStyle.NONE)
            except Exception:
                pass
        else:
            color = _parse_color(color_str)
            if color is not None:
                # LineShape doesn't have FillColor, typically LineColor is used instead
                prop = "LineColor" if "LineShape" in shape.getShapeType() else "FillColor"
                try:
                    shape.setPropertyValue(prop, color)
                except Exception:
                    pass

    # Fill Style (solid, transparent, etc)
    if kwargs.get("fill_style") and hasattr(shape, "FillStyle"):
        try:
            import sys
            fill_enum = sys.modules.get("com.sun.star.drawing.FillStyle")
            if not fill_enum:
                import uno
                from com.sun.star.drawing import FillStyle
                fill_enum = FillStyle
            style_str = kwargs["fill_style"].lower()
            if style_str == "none" or style_str == "transparent":
                shape.setPropertyValue("FillStyle", fill_enum.NONE)
            elif style_str == "solid":
                shape.setPropertyValue("FillStyle", fill_enum.SOLID)
        except Exception:
            pass

    # Line Color
    if kwargs.get("line_color") and hasattr(shape, "LineColor"):
        color = _parse_color(kwargs["line_color"])
        if color is not None:
            try:
                shape.setPropertyValue("LineColor", color)
            except Exception:
                pass

    # Line Width
    if kwargs.get("line_width") is not None and hasattr(shape, "LineWidth"):
        try:
            shape.setPropertyValue("LineWidth", int(kwargs["line_width"]))
        except Exception:
            pass

    # Line Style (ensure border is visible when colored or sized)
    if (kwargs.get("line_color") or kwargs.get("line_width") is not None) and hasattr(shape, "LineStyle"):
        try:
            import sys
            line_enum = sys.modules.get("com.sun.star.drawing.LineStyle")
            if not line_enum:
                import uno
                from com.sun.star.drawing import LineStyle as line_enum
            shape.setPropertyValue("LineStyle", line_enum.SOLID)
        except Exception:
            pass

    # Text Properties (Font Size, Name, Color)
    if kwargs.get("text_color") and hasattr(shape, "CharColor"):
        color = _parse_color(kwargs["text_color"])
        if color is not None:
            try:
                shape.setPropertyValue("CharColor", color)
            except Exception:
                pass

    if kwargs.get("font_size") and hasattr(shape, "CharHeight"):
        try:
            shape.setPropertyValue("CharHeight", float(kwargs["font_size"]))
        except Exception:
            pass

    if kwargs.get("font_name") and hasattr(shape, "CharFontName"):
        try:
            shape.setPropertyValue("CharFontName", kwargs["font_name"])
        except Exception:
            pass

    # Rotation
    if kwargs.get("rotation_angle") is not None and hasattr(shape, "RotateAngle"):
        try:
            # Angle is in 100ths of a degree
            shape.setPropertyValue("RotateAngle", int(kwargs["rotation_angle"] * 100))
        except Exception:
            pass


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
    "polygons and symbols (octagon, hexagon, diamond, trapezoid, smiley, heart, sun, moon); "
    "stars (star4, star5, star8, star24); "
    "arrows and callouts (names with -arrow, -arrow-callout, line-callout-1, etc.); "
    "braces and brackets (brace-pair, bracket-pair, left-brace, left-bracket, etc.); "
    "other shapes (cube, ring, cloud-callout, lightning, etc.). "
)


class CreateShape(ToolBase):
    name = "create_shape"
    description = "Creates a new shape on the active page."
    parameters = {
        "type": "object",
        "properties": {
            "shape_type": {
                "type": "string",
                "description": _CREATE_SHAPE_SHAPE_TYPE_DESC,
            },
            "x": {"type": "integer", "description": "X position (100ths of mm)"},
            "y": {"type": "integer", "description": "Y position (100ths of mm)"},
            "width": {"type": "integer", "description": "Width (100ths of mm)"},
            "height": {"type": "integer", "description": "Height (100ths of mm)"},
            "text": {"type": "string", "description": "Initial text"},
            "bg_color": {"type": "string", "description": "Alias for fill_color. Hex (#FF0000) or name (red)"},
            "fill_color": {"type": "string", "description": "Fill color. Hex (#FF0000) or name (red)"},
            "fill_style": {"type": "string", "enum": ["solid", "transparent", "none"], "description": "Fill style"},
            "line_color": {"type": "string", "description": "Line border color"},
            "line_width": {"type": "integer", "description": "Line width (100ths of mm)"},
            "text_color": {"type": "string", "description": "Text character color"},
            "font_size": {"type": "number", "description": "Font size in points"},
            "font_name": {"type": "string", "description": "Font family name"},
            "rotation_angle": {"type": "number", "description": "Rotation angle in degrees"},
        },
        "required": ["shape_type", "x", "y", "width", "height"],
    }
    uno_services = ["com.sun.star.drawing.DrawingDocument"]
    doc_types = ["draw", "impress"]
    tier = "core"
    is_mutation = True

    def execute(self, ctx, **kwargs):
        from plugin.modules.draw.bridge import DrawBridge
        from com.sun.star.awt import Point, Size

        bridge = DrawBridge(ctx.doc)
        type_map = {
            "rectangle": "RectangleShape",
            "ellipse": "EllipseShape",
            "text": "TextShape",
            "line": "LineShape",
            "connector": "ConnectorShape",
        }
        shape_type_raw = kwargs["shape_type"]

        page = bridge.get_active_page()
        if page is None:
            return self._tool_error("No draw page available.")
        _log_create_shape_page_context(ctx.doc, bridge, page)
        _log_writer_document_shape_context(ctx.doc)

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

        log.debug(
            "create_shape branch: raw=%r resolved_uno=%r is_custom_catalog=%s catalog_name=%r "
            "(rectangle path uses RectangleShape + no enhanced geometry; octagon uses CustomShape + geometry before add)",
            shape_type_raw,
            uno_type,
            is_custom_shape,
            custom_shape_type if is_custom_shape else None,
        )

        draw_shapes = DrawShapes()

        try:
            shape, geometry_applied, geometry_error = draw_shapes.safe_create_shape(
                ctx.doc,
                page,
                uno_type,
                position,
                size,
                custom_shape_type=None,  # Do not apply geometry before anchoring to avoid Writer clearing bugs
            )
            if is_custom_shape and geometry_applied:
                _log_shape_uno_snapshot("after_custom_geometry", shape)
        except DrawError as e:
            return self._tool_error(e.message)

        _try_writer_at_page_shape_finalize(ctx.doc, bridge, page, shape)
        _try_writer_reapply_position_after_anchor(ctx.doc, shape, position, size)

        # Apply geometry securely AFTER anchoring
        if is_custom_shape and custom_shape_type:
            geometry_applied, geometry_error = _apply_enhanced_custom_shape_type(shape, custom_shape_type)
            if not geometry_applied:
                log.warning("create_shape: Failed to apply EnhancedCustomShapeGeometry post-anchor")

        _apply_shape_properties(shape, kwargs)
        _try_writer_invalidate_and_pump(ctx.doc)
        _try_writer_select_created_shape(ctx.doc, shape)
        _log_shape_uno_snapshot("after_formatting", shape)
        if is_custom_shape:
            _log_custom_shape_geometry_dump(shape, "after_formatting")
        _log_shape_property_names_sample(shape, "after_formatting")

        page_index = _page_index_for(bridge, page)
        shape_count_after = page.getCount()
        shape_index = shape_count_after - 1

        log.debug(
            "create_shape: page_index=%s shape_index=%s shape_type=%s is_custom=%s geometry_applied=%s",
            page_index,
            shape_index,
            shape_type_raw,
            is_custom_shape,
            geometry_applied,
        )

        result: dict = {
            "status": "ok",
            "message": f"Created {shape_type_raw}",
            "shape_index": shape_index,
            "page_index": page_index,
            "shape_count_after": shape_count_after,
        }
        if is_custom_shape:
            result["custom_shape_engine"] = _ENHANCED_CUSTOM_SHAPE_ENGINE
            result["geometry_applied"] = bool(geometry_applied)
            if geometry_error:
                result["geometry_error"] = geometry_error
                result["warning"] = f"Custom shape geometry failed: {geometry_error}"

        return result


class EditShape(ToolBase):
    name = "edit_shape"
    intent = "edit"
    description = "Modifies properties of an existing shape."
    parameters = {
        "type": "object",
        "properties": {
            "shape_index": {"type": "integer", "description": "Index of the shape"},
            "page_index": {"type": "integer", "description": "Page index"},
            "x": {"type": "integer", "description": "X position (100ths of mm)"},
            "y": {"type": "integer", "description": "Y position (100ths of mm)"},
            "width": {"type": "integer", "description": "Width (100ths of mm)"},
            "height": {"type": "integer", "description": "Height (100ths of mm)"},
            "text": {"type": "string", "description": "Text content"},
            "bg_color": {"type": "string", "description": "Alias for fill_color. Hex (#FF0000) or name (red)"},
            "fill_color": {"type": "string", "description": "Fill color. Hex (#FF0000) or name (red)"},
            "fill_style": {"type": "string", "enum": ["solid", "transparent", "none"], "description": "Fill style"},
            "line_color": {"type": "string", "description": "Line border color"},
            "line_width": {"type": "integer", "description": "Line width (100ths of mm)"},
            "text_color": {"type": "string", "description": "Text character color"},
            "font_size": {"type": "number", "description": "Font size in points"},
            "font_name": {"type": "string", "description": "Font family name"},
            "rotation_angle": {"type": "number", "description": "Rotation angle in degrees"},
        },
        "required": ["shape_index"],
    }
    uno_services = ["com.sun.star.drawing.DrawingDocument"]
    is_mutation = True

    def execute(self, ctx, **kwargs):
        from plugin.modules.draw.bridge import DrawBridge
        bridge = DrawBridge(ctx.doc)
        idx = kwargs.get("page_index")
        page = (
            bridge.get_pages().getByIndex(idx)
            if idx is not None
            else bridge.get_active_page()
        )
        if page is None:
            return self._tool_error("No draw page available or invalid page index.")
        shape = page.getByIndex(kwargs["shape_index"])
        if "x" in kwargs or "y" in kwargs:
            from com.sun.star.awt import Point
            pos = shape.getPosition()
            shape.setPosition(Point(kwargs.get("x", pos.X), kwargs.get("y", pos.Y)))
        if "width" in kwargs or "height" in kwargs:
            from com.sun.star.awt import Size
            size = shape.getSize()
            shape.setSize(
                Size(kwargs.get("width", size.Width), kwargs.get("height", size.Height))
            )

        _apply_shape_properties(shape, kwargs)

        return {"status": "ok", "message": "Shape updated"}


class ConnectShapes(ToolBase):
    """Connect two shapes with a connector."""
    name = "shapes_connect"
    intent = "edit"
    description = "Connect two shapes on the same page with a connector."
    parameters = {
        "type": "object",
        "properties": {
            "start_shape_index": {
                "type": "integer",
                "description": "Index of the starting shape.",
            },
            "end_shape_index": {
                "type": "integer",
                "description": "Index of the ending shape.",
            },
            "page_index": {
                "type": "integer",
                "description": "Page index containing the shapes"
            },
            "line_color": {"type": "string", "description": "Color of the connector line"},
            "line_width": {"type": "integer", "description": "Line width (100ths of mm)"},
        },
        "required": ["start_shape_index", "end_shape_index"],
    }
    uno_services = ["com.sun.star.drawing.DrawingDocument"]
    doc_types = ["draw", "impress"]
    is_mutation = True

    def execute(self, ctx, **kwargs):
        from plugin.modules.draw.bridge import DrawBridge
        from com.sun.star.awt import Point, Size

        bridge = DrawBridge(ctx.doc)
        idx = kwargs.get("page_index")
        page = (
            bridge.get_pages().getByIndex(idx)
            if idx is not None
            else bridge.get_active_page()
        )
        if page is None:
            return self._tool_error("No draw page available or invalid page index.")

        start_idx = kwargs["start_shape_index"]
        end_idx = kwargs["end_shape_index"]

        try:
            start_shape = page.getByIndex(start_idx)
            end_shape = page.getByIndex(end_idx)
        except Exception as e:
            return self._tool_error(f"Failed to find shapes at given indices: {str(e)}")

        draw_shapes = DrawShapes()
        try:
            # position and size are technically ignored since it's a connector, but safe_create_shape expects them
            shape, _, _ = draw_shapes.safe_create_shape(
                ctx.doc, page, "com.sun.star.drawing.ConnectorShape", Point(0, 0), Size(100, 100)
            )
        except DrawError as e:
            return self._tool_error(e.message)

        # Connect the shapes
        try:
            shape.setPropertyValue("StartShape", start_shape)
            shape.setPropertyValue("EndShape", end_shape)

            # Additional properties
            _apply_shape_properties(shape, kwargs)

        except Exception as e:
            return self._tool_error(f"Failed to set connector properties: {str(e)}")

        return {"status": "ok", "message": f"Connected shape {start_idx} to {end_idx}", "shape_index": page.getCount() - 1}


class GroupShapes(ToolBase):
    """Group multiple shapes together."""
    name = "shapes_group"
    intent = "edit"
    description = "Groups multiple shapes together on the same page."
    parameters = {
        "type": "object",
        "properties": {
            "shape_indices": {
                "type": "array",
                "items": {"type": "integer"},
                "description": "List of shape indices to group.",
            },
            "page_index": {
                "type": "integer",
                "description": "Page index containing the shapes",
            },
        },
        "required": ["shape_indices"],
    }
    uno_services = ["com.sun.star.drawing.DrawingDocument"]
    doc_types = ["draw", "impress"]
    is_mutation = True

    def execute(self, ctx, **kwargs):
        from plugin.modules.draw.bridge import DrawBridge

        bridge = DrawBridge(ctx.doc)
        idx = kwargs.get("page_index")
        page = (
            bridge.get_pages().getByIndex(idx)
            if idx is not None
            else bridge.get_active_page()
        )
        if page is None:
            return self._tool_error("No draw page available or invalid page index.")

        indices = kwargs["shape_indices"]
        if not indices or len(indices) < 2:
            return self._tool_error("At least two shape indices are required to group.")

        try:
            # Create a shape collection
            shape_collection = ctx.doc.createInstance("com.sun.star.drawing.ShapeCollection")
            for i in indices:
                shape = page.getByIndex(i)
                shape_collection.add(shape)

            # Group the shapes
            group_shape = page.group(shape_collection)
        except Exception as e:
            return self._tool_error(f"Failed to group shapes: {str(e)}")

        return {
            "status": "ok",
            "message": f"Grouped {len(indices)} shapes.",
            "group_shape_index": page.getCount() - 1 # Note: Grouping usually replaces the individual shapes with the group shape
        }


class DeleteShape(ToolBase):
    name = "delete_shape"
    intent = "edit"
    description = "Deletes a shape by index."
    parameters = {
        "type": "object",
        "properties": {
            "shape_index": {"type": "integer"},
            "page_index": {"type": "integer"},
        },
        "required": ["shape_index"],
    }
    uno_services = ["com.sun.star.drawing.DrawingDocument"]
    is_mutation = True

    def execute(self, ctx, **kwargs):
        from plugin.modules.draw.bridge import DrawBridge
        bridge = DrawBridge(ctx.doc)
        idx = kwargs.get("page_index")
        page = (
            bridge.get_pages().getByIndex(idx)
            if idx is not None
            else bridge.get_active_page()
        )
        if page is None:
            return self._tool_error("No draw page available or invalid page index.")
        shape = page.getByIndex(kwargs["shape_index"])
        page.remove(shape)
        return {"status": "ok", "message": "Shape deleted"}
