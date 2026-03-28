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

from plugin.framework.errors import WriterAgentException
from plugin.framework.tool_base import ToolBase


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

    def safe_create_shape(self, doc, page, shape_type, position, size):
        """Safely create shape with error handling.

        Shapes are created via the document's factory (``doc.createInstance``);
        ``XDrawPage`` is not a reliable ``createInstance`` source in UNO.
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

            # Set properties
            shape.setPosition(position)
            shape.setSize(size)

            # Add to page
            page.add(shape)

            return shape

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


class CreateShape(ToolBase):
    name = "create_shape"
    description = "Creates a new shape on the active page."
    parameters = {
        "type": "object",
        "properties": {
            "shape_type": {
                "type": "string",
                "description": "Type of shape. Use simple names like 'rectangle', 'ellipse', 'text', 'line', 'connector', or CustomShape types like 'octagon', 'star5', 'smiley', 'heart'. Or provide full UNO class names like 'PolyPolygonShape'.",
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

        draw_shapes = DrawShapes()

        try:
            shape = draw_shapes.safe_create_shape(
                ctx.doc, page, uno_type, position, size
            )

            if is_custom_shape:
                from com.sun.star.beans import PropertyValue
                prop = PropertyValue()
                prop.Name = "Type"
                prop.Value = custom_shape_type
                try:
                    shape.setPropertyValue("CustomShapeGeometry", (prop,))
                except Exception as ex:
                    # Ignore if the specific CustomShape Type fails to apply,
                    # LibreOffice will just draw a default shape or nothing.
                    pass
        except DrawError as e:
            return self._tool_error(e.message)

        _apply_shape_properties(shape, kwargs)

        return {
            "status": "ok",
            "message": f"Created {shape_type_raw}",
            "shape_index": page.getCount() - 1,
        }


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
            shape = draw_shapes.safe_create_shape(
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
        shape = page.getByIndex(kwargs["shape_index"])
        page.remove(shape)
        return {"status": "ok", "message": "Shape deleted"}
