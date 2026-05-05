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
"""Tree (LO-DOM) tools for Draw/Impress documents."""

import logging

from plugin.framework.tool_base import ToolBase

log = logging.getLogger(__name__)


class GetDrawTree(ToolBase):
    name = "get_draw_tree"
    intent = "read"
    description = (
        "Returns a semantic tree (DOM) of the shapes on the active or specified draw page. "
        "Use this instead of requesting a screenshot to understand the layout, text, "
        "connections, and hierarchy of objects (like flowcharts or diagrams)."
    )
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
    uno_services = ["com.sun.star.drawing.DrawingDocument", "com.sun.star.presentation.PresentationDocument"]
    doc_types = ["draw", "impress"]
    tier = "core"

    def execute(self, ctx, **kwargs):
        from plugin.modules.draw.bridge import DrawBridge

        bridge = DrawBridge(ctx.doc)
        idx = kwargs.get("page_index")
        page = bridge.get_pages().getByIndex(idx) if idx is not None else bridge.get_active_page()
        if page is None:
            return self._tool_error("No draw page available or invalid page index.")

        # Try to resolve actual page index if active page was used
        actual_idx = idx
        if actual_idx is None:
            try:
                pages = bridge.get_pages()
                for i in range(pages.getCount()):
                    if pages.getByIndex(i) == page:
                        actual_idx = i
                        break
            except Exception:
                pass

        return {"status": "ok", "page_index": actual_idx, "tree": self._build_shape_tree(page)}

    def _build_shape_tree(self, xshapes, base_index=None):
        """Recursively build a semantic tree from an XShapes collection (DrawPage or GroupShape)."""
        tree = []
        try:
            count = xshapes.getCount()
        except Exception:
            return tree

        for i in range(count):
            try:
                shape = xshapes.getByIndex(i)
            except Exception:
                continue

            current_index = str(i) if base_index is None else f"{base_index}.{i}"

            try:
                shape_type = shape.getShapeType()
            except Exception:
                shape_type = "UnknownShape"

            node = {"type": shape_type.replace("com.sun.star.drawing.", "")}

            if base_index is None:
                node["shape_index"] = i
            else:
                node["path_index"] = current_index

            try:
                name = getattr(shape, "Name", "")
                if name:
                    node["name"] = name
            except Exception:
                pass

            try:
                if hasattr(shape, "getString"):
                    text = shape.getString().strip()
                    if text:
                        node["text"] = text
            except Exception:
                pass

            try:
                desc = getattr(shape, "Description", "")
                if desc:
                    node["alt_description"] = desc
                title = getattr(shape, "Title", "")
                if title:
                    node["alt_title"] = title
            except Exception:
                pass

            try:
                pos = shape.getPosition()
                size = shape.getSize()
                node["geometry"] = {"x": pos.X, "y": pos.Y, "width": size.Width, "height": size.Height}
            except Exception:
                pass

            if "ConnectorShape" in shape_type:
                try:
                    start_shape = shape.getPropertyValue("StartShape")
                    if start_shape:
                        s_name = getattr(start_shape, "Name", "")
                        s_text = start_shape.getString().strip() if hasattr(start_shape, "getString") else ""
                        node["connected_start"] = {"name": s_name, "text": s_text}
                except Exception:
                    pass
                try:
                    end_shape = shape.getPropertyValue("EndShape")
                    if end_shape:
                        e_name = getattr(end_shape, "Name", "")
                        e_text = end_shape.getString().strip() if hasattr(end_shape, "getString") else ""
                        node["connected_end"] = {"name": e_name, "text": e_text}
                except Exception:
                    pass

            style = {}
            for prop in ["FillColor", "LineColor", "ZOrder", "RotateAngle", "LineWidth"]:
                try:
                    val = shape.getPropertyValue(prop)
                    if val is not None:
                        if prop in ["FillColor", "LineColor"] and isinstance(val, int) and val != -1:
                            style[prop] = f"#{val:06X}"
                        else:
                            style[prop] = val
                except Exception:
                    pass

            try:
                geom = shape.getPropertyValue("CustomShapeGeometry")
                if geom:
                    for p in geom:
                        if p.Name == "Type":
                            node["custom_shape_type"] = p.Value
            except Exception:
                pass

            if style:
                node["style"] = style

            if "GroupShape" in shape_type:
                node["children"] = self._build_shape_tree(shape, current_index)

            tree.append(node)

        return tree
