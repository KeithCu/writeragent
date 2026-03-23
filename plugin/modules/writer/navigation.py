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
"""Navigation tools: navigate_heading, get_surroundings."""

from plugin.framework.tool_base import ToolBaseDummy


class NavigateHeading(ToolBaseDummy):
    name = "navigate_heading"
    intent = "navigate"
    description = (
        "Navigate from a locator to a related heading. "
        "Directions: next, previous, parent, first_child, "
        "next_sibling, previous_sibling. "
        "Returns the target heading with bookmark for stable addressing."
    )
    parameters = {
        "type": "object",
        "properties": {
            "locator": {
                "type": "string",
                "description": (
                    "Starting position (e.g. 'bookmark:_mcp_xxx', "
                    "'paragraph:42', 'heading_text:Introduction')"
                ),
            },
            "direction": {
                "type": "string",
                "enum": ["next", "previous", "parent", "first_child",
                         "next_sibling", "previous_sibling"],
                "description": "Navigation direction",
            },
        },
        "required": ["locator", "direction"],
    }
    uno_services = ["com.sun.star.text.TextDocument"]

    def execute(self, ctx, **kwargs):
        prox_svc = ctx.services.writer_proximity
        try:
            result = prox_svc.navigate_heading(
                ctx.doc, kwargs["locator"], kwargs["direction"])
            if "error" in result:
                return self._tool_error(result["error"])
            return {"status": "ok", **result}
        except ValueError as e:
            return self._tool_error(str(e))


class GetSurroundings(ToolBaseDummy):
    name = "get_surroundings"
    intent = "navigate"
    description = (
        "Discover objects within a radius of paragraphs around a locator. "
        "Returns nearby paragraphs, heading chain, images, tables, "
        "frames, and comments."
    )
    parameters = {
        "type": "object",
        "properties": {
            "locator": {
                "type": "string",
                "description": "Center position (e.g. 'bookmark:_mcp_xxx', 'paragraph:42')",
            },
            "radius": {
                "type": "integer",
                "description": "Number of paragraphs in each direction (default: 10, max: 50)",
            },
            "include": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Object types to include: paragraphs, images, tables, "
                    "frames, comments, headings (default: all)"
                ),
            },
        },
        "required": ["locator"],
    }
    uno_services = ["com.sun.star.text.TextDocument"]

    def execute(self, ctx, **kwargs):
        prox_svc = ctx.services.writer_proximity
        try:
            result = prox_svc.get_surroundings(
                ctx.doc, kwargs["locator"],
                radius=kwargs.get("radius", 10),
                include=kwargs.get("include"))
            return {"status": "ok", **result}
        except ValueError as e:
            return self._tool_error(str(e))
