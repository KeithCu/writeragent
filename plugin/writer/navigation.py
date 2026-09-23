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
"""Navigation tools: nav_heading, nav_surroundings."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .specialized_base import ToolWriterStructuralBase

if TYPE_CHECKING:
    from plugin.framework.tool import ToolContext


class NavHeading(ToolWriterStructuralBase):
    name = "nav_heading"
    intent = "navigate"
    is_mutation = False
    description = "Navigate from a locator to a related heading. Directions: next, previous, parent, first_child, next_sibling, previous_sibling. Returns the target heading with bookmark for stable addressing."
    parameters = {
        "type": "object",
        "properties": {
            "locator": {"type": "string", "description": ("Starting position (e.g. 'bookmark:_mcp_xxx', 'paragraph:42', 'heading_text:Introduction')")},
            "direction": {"type": "string", "enum": ["next", "previous", "parent", "first_child", "next_sibling", "previous_sibling"], "description": "Navigation direction"},
        },
        "required": ["locator", "direction"],
    }
    uno_services = ["com.sun.star.text.TextDocument"]

    def execute(self, ctx: ToolContext, **kwargs: Any):
        prox_svc = ctx.services.writer_proximity
        try:
            result = prox_svc.navigate_heading(ctx.doc, kwargs["locator"], kwargs["direction"])
            if "error" in result:
                return self._tool_error(result["error"])
            return {"status": "ok", **result}
        except ValueError as e:
            return self._tool_error(str(e))


class NavSurroundings(ToolWriterStructuralBase):
    name = "nav_surroundings"
    intent = "navigate"
    description = "Discover objects within a radius of paragraphs around a locator. Returns nearby paragraphs, heading chain, images, tables, frames, and comments."
    parameters = {
        "type": "object",
        "properties": {
            "locator": {"type": "string", "description": "Center position (e.g. 'bookmark:_mcp_xxx', 'paragraph:42')"},
            "radius": {"type": "integer", "description": "Number of paragraphs in each direction (default: 10, max: 50)"},
            "include": {"type": "array", "items": {"type": "string"}, "description": ("Object types to include: paragraphs, images, tables, frames, comments, headings (default: all)")},
        },
        "required": ["locator"],
    }
    uno_services = ["com.sun.star.text.TextDocument"]

    def execute(self, ctx: ToolContext, **kwargs: Any):
        prox_svc = ctx.services.writer_proximity
        try:
            result = prox_svc.get_surroundings(ctx.doc, kwargs["locator"], radius=kwargs.get("radius", 10), include=kwargs.get("include"))
            return {"status": "ok", **result}
        except ValueError as e:
            return self._tool_error(str(e))


class NavHeadingChildren(ToolWriterStructuralBase):
    name = "nav_heading_children"
    intent = "navigate"
    description = "Drill into a heading's children — body paragraphs and sub-headings. Identify the heading by locator (e.g. 'bookmark:_mcp_xxx', 'heading:1.2'), para_index, or bookmark. para_index values are INTERNAL — never cite paragraph numbers to the user; refer to a place by quoting the first words of its text."
    parameters = {
        "type": "object",
        "properties": {
            "locator": {"type": "string", "description": "Locator string (e.g. 'bookmark:_mcp_xxx', 'heading:1.2')"},
            "para_index": {"type": "integer", "description": "Paragraph index of the heading"},
            "bookmark": {"type": "string", "description": "Bookmark name of the heading"},
            "strategy": {"type": "string", "enum": ["heading_only", "first_lines", "full"], "description": "Content strategy (default: first_lines)"},
            "depth": {"type": "integer", "description": "Max sub-heading depth (default: 1)"},
        },
        "required": [],
    }
    uno_services = ["com.sun.star.text.TextDocument"]

    def execute(self, ctx: ToolContext, **kwargs: Any):
        tree_svc = ctx.services.writer_tree
        para_index = kwargs.get("para_index")
        bookmark = kwargs.get("bookmark")
        strategy = kwargs.get("strategy", "first_lines")
        try:
            result = tree_svc.get_heading_children(ctx.doc, heading_para_index=para_index, heading_bookmark=bookmark, locator=kwargs.get("locator"), content_strategy=strategy, depth=kwargs.get("depth", 1))
            return {"status": "ok", **result}
        except ValueError as e:
            return self._tool_error(str(e))
