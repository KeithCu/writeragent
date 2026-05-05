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
"""Writer shape drawing tools, bridging Draw's implementations."""

import logging
from plugin.modules.writer.base import ToolWriterShapeBase
from plugin.modules.draw.shapes import CreateShape as DrawCreateShape
from plugin.modules.draw.shapes import EditShape as DrawEditShape
from plugin.modules.draw.shapes import DeleteShape as DrawDeleteShape
from plugin.modules.draw.shapes import GetDrawSummary as DrawGetDrawSummary
from plugin.modules.draw.shapes import ConnectShapes as DrawConnectShapes
from plugin.modules.draw.shapes import GroupShapes as DrawGroupShapes

log = logging.getLogger("writeragent.writer")


# 1. Inherit from the Draw tool implementation.
# 2. Inherit from the specialized ToolWriterShapeBase to enforce Writer scoping.
# 3. Union services: same names as Draw tools; include Draw/Impress so registration
# order does not drop support for non-Writer documents.
_WRITER_DRAW_SHAPE_DOCS = ["com.sun.star.text.TextDocument", "com.sun.star.drawing.DrawingDocument", "com.sun.star.presentation.PresentationDocument"]


class CreateShape(DrawCreateShape, ToolWriterShapeBase):
    name = "create_shape"
    uno_services = _WRITER_DRAW_SHAPE_DOCS
    # Specialized for all document types (use delegate_to_specialized_*_toolset(domain=shapes)).
    tier = "specialized"


class EditShape(DrawEditShape, ToolWriterShapeBase):
    name = "edit_shape"
    uno_services = _WRITER_DRAW_SHAPE_DOCS


class DeleteShape(DrawDeleteShape, ToolWriterShapeBase):
    name = "delete_shape"
    uno_services = _WRITER_DRAW_SHAPE_DOCS


class GetDrawSummary(DrawGetDrawSummary, ToolWriterShapeBase):
    name = "get_draw_summary"
    uno_services = _WRITER_DRAW_SHAPE_DOCS


class ListWriterImages(ToolWriterShapeBase):
    """List graphic objects anchored in the Writer document (text layer)."""

    name = "list_writer_images"
    intent = "media"
    description = "List images and graphic objects in the Writer document (names, sizes, titles). Uses the document graphic-object collection (not the full Draw page API)."
    parameters = {"type": "object", "properties": {}, "required": []}
    uno_services = ["com.sun.star.text.TextDocument"]

    def execute(self, ctx, **kwargs):
        doc = ctx.doc
        if not hasattr(doc, "getGraphicObjects"):
            return {"status": "ok", "images": [], "count": 0}
        images = []
        gos = doc.getGraphicObjects()
        for name in gos.getElementNames():
            try:
                g = gos.getByName(name)
                size = g.getPropertyValue("Size")
                title = ""
                try:
                    title = g.getPropertyValue("Title")
                except Exception:
                    pass
                images.append({"name": name, "width_mm": size.Width / 100.0, "height_mm": size.Height / 100.0, "title": title})
            except Exception as e:
                log.debug("list_writer_images skip %s: %s", name, e)
        return {"status": "ok", "images": images, "count": len(images)}


class ConnectShapes(DrawConnectShapes, ToolWriterShapeBase):
    name = "shapes_connect"
    uno_services = _WRITER_DRAW_SHAPE_DOCS


class GroupShapes(DrawGroupShapes, ToolWriterShapeBase):
    name = "shapes_group"
    uno_services = _WRITER_DRAW_SHAPE_DOCS
