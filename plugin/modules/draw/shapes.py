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
    doc_types = ["draw"]
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
    doc_types = ["draw"]

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

    def safe_create_shape(self, page, shape_type, position, size):
        """Safely create shape with error handling."""
        try:
            # Validate inputs
            if not page:
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

            # Create shape
            shape = page.createInstance(f"com.sun.star.drawing.{shape_type}")
            if not shape:
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


class CreateShape(ToolBase):
    name = "create_shape"
    description = "Creates a new shape on the active page."
    parameters = {
        "type": "object",
        "properties": {
            "shape_type": {
                "type": "string",
                "enum": ["rectangle", "ellipse", "text", "line"],
                "description": "Type of shape",
            },
            "x": {"type": "integer", "description": "X position (100ths of mm)"},
            "y": {"type": "integer", "description": "Y position (100ths of mm)"},
            "width": {"type": "integer", "description": "Width (100ths of mm)"},
            "height": {"type": "integer", "description": "Height (100ths of mm)"},
            "text": {"type": "string", "description": "Initial text"},
            "bg_color": {
                "type": "string",
                "description": "Hex (#FF0000) or name (red)",
            },
        },
        "required": ["shape_type", "x", "y", "width", "height"],
    }
    doc_types = ["draw"]
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
        }
        uno_type = type_map.get(kwargs["shape_type"])
        if not uno_type:
            return self._tool_error(f"Unsupported shape type: {kwargs['shape_type']}")

        page = bridge.get_active_page()
        position = Point(kwargs["x"], kwargs["y"])
        size = Size(kwargs["width"], kwargs["height"])

        draw_shapes = DrawShapes()

        try:
            shape = draw_shapes.safe_create_shape(
                page, uno_type, position, size
            )
        except DrawError as e:
            return self._tool_error(e.message)

        if kwargs.get("text") and hasattr(shape, "setString"):
            shape.setString(kwargs["text"])
        if kwargs.get("bg_color"):
            color = _parse_color(kwargs["bg_color"])
            if color is not None:
                prop = (
                    "LineColor"
                    if "LineShape" in shape.getShapeType()
                    else "FillColor"
                )
                try:
                    shape.setPropertyValue(prop, color)
                except Exception:
                    pass

        return {
            "status": "ok",
            "message": f"Created {kwargs['shape_type']}",
            "shape_index": page.getCount() - 1,
        }


class EditShape(ToolBase):
    name = "edit_shape"
    intent = "edit"
    description = "Modifies properties of an existing shape."
    parameters = {
        "type": "object",
        "properties": {
            "shape_index": {
                "type": "integer",
                "description": "Index of the shape",
            },
            "page_index": {"type": "integer", "description": "Page index"},
            "x": {"type": "integer"},
            "y": {"type": "integer"},
            "width": {"type": "integer"},
            "height": {"type": "integer"},
            "text": {"type": "string"},
            "bg_color": {"type": "string"},
        },
        "required": ["shape_index"],
    }
    doc_types = ["draw"]
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
        if "text" in kwargs and hasattr(shape, "setString"):
            shape.setString(kwargs["text"])
        if "bg_color" in kwargs:
            color = _parse_color(kwargs["bg_color"])
            if color is not None:
                prop = (
                    "LineColor"
                    if "LineShape" in shape.getShapeType()
                    else "FillColor"
                )
                try:
                    shape.setPropertyValue(prop, color)
                except Exception:
                    pass
        return {"status": "ok", "message": "Shape updated"}


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
    doc_types = ["draw"]
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
