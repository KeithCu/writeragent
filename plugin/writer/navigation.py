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
"""Navigation tools: nav_heading, nav_surroundings, nav_heading_children."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable

from plugin.framework.errors import ToolExecutionError
from plugin.framework.prompts import PARAGRAPH_INDEX_DIRECTIVE

from .specialized_base import ToolWriterStructuralBase, enum_param

if TYPE_CHECKING:
    from plugin.framework.tool import ToolContext


def _nav_result(tool: ToolWriterStructuralBase, call: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    """Map one navigation service call to the tool envelope (error dict and exception both become tool errors)."""
    try:
        result = call()
    except (ValueError, ToolExecutionError) as e:
        return tool._tool_error(str(e))
    if "error" in result:
        return tool._tool_error(result["error"])
    return {"status": "ok", **result}


class NavHeading(ToolWriterStructuralBase):
    name: str | None = "nav_heading"
    intent: str | None = "navigate"
    is_mutation: bool | None = False
    description: str = (
        "Navigate from a locator to a related heading. Directions: next, previous, parent, first_child, next_sibling, previous_sibling. "
        "Returns the target heading with bookmark for stable addressing. "
        + PARAGRAPH_INDEX_DIRECTIVE
    )
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "locator": {"type": "string", "description": ("Starting position (e.g. 'bookmark:_mcp_xxx', 'paragraph:42', 'heading_text:Introduction')")},
            "direction": enum_param(["next", "previous", "parent", "first_child", "next_sibling", "previous_sibling"], "Navigation direction"),
        },
        "required": ["locator", "direction"],
    }
    uno_services: list[str] | None = ["com.sun.star.text.TextDocument"]

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        prox_svc = ctx.services.writer_proximity
        return _nav_result(self, lambda: prox_svc.navigate_heading(ctx.doc, kwargs["locator"], kwargs["direction"]))


class NavSurroundings(ToolWriterStructuralBase):
    name: str | None = "nav_surroundings"
    intent: str | None = "navigate"
    is_mutation: bool | None = False
    description: str = (
        "Discover objects within a radius of paragraphs around a locator. Returns nearby paragraphs, heading chain, images, tables, frames, and comments. "
        + PARAGRAPH_INDEX_DIRECTIVE
    )
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "locator": {"type": "string", "description": "Center position (e.g. 'bookmark:_mcp_xxx', 'paragraph:42')"},
            "radius": {"type": "integer", "description": "Number of paragraphs in each direction (default: 10, max: 50)"},
            "include": {"type": "array", "items": {"type": "string"}, "description": ("Object types to include: paragraphs, images, tables, frames, comments, headings (default: all)")},
        },
        "required": ["locator"],
    }
    uno_services: list[str] | None = ["com.sun.star.text.TextDocument"]

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        prox_svc = ctx.services.writer_proximity
        return _nav_result(self, lambda: prox_svc.get_surroundings(ctx.doc, kwargs["locator"], radius=kwargs.get("radius", 10), include=kwargs.get("include")))


class NavHeadingChildren(ToolWriterStructuralBase):
    name: str | None = "nav_heading_children"
    intent: str | None = "navigate"
    is_mutation: bool | None = False
    description: str = (
        "Drill into a heading's children — body paragraphs and sub-headings. Identify the heading by locator (e.g. 'bookmark:_mcp_xxx', sibling-ordinal 'heading:1.2', or chapter-label 'chapter_number:3.1' when present), para_index, or bookmark (exactly one). heading: is ordinal path among siblings, not Writer's chapter number. "
        + PARAGRAPH_INDEX_DIRECTIVE
    )
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "locator": {"type": "string", "description": "Locator string (e.g. 'bookmark:_mcp_xxx', ordinal 'heading:1.2', or 'chapter_number:3.1' when Chapter Numbering is on)"},
            "para_index": {"type": "integer", "description": "Paragraph index of the heading"},
            "bookmark": {"type": "string", "description": "Bookmark name of the heading"},
            "strategy": enum_param(["heading_only", "first_lines", "full"], "Content strategy (default: first_lines)"),
            "depth": {"type": "integer", "description": "Max sub-heading depth (default: 1)"},
        },
        "required": [],
    }
    uno_services: list[str] | None = ["com.sun.star.text.TextDocument"]

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        tree_svc = ctx.services.writer_tree
        para_index = kwargs.get("para_index")
        bookmark = kwargs.get("bookmark")
        strategy = kwargs.get("strategy", "first_lines")
        provided = [name for name, value in (("locator", kwargs.get("locator")), ("para_index", para_index), ("bookmark", bookmark)) if value is not None]
        if len(provided) > 1:
            return self._tool_error("Provide only one of locator, para_index, bookmark (got: %s)" % ", ".join(provided))
        return _nav_result(self, lambda: tree_svc.get_heading_children(ctx.doc, heading_para_index=para_index, heading_bookmark=bookmark, locator=kwargs.get("locator"), content_strategy=strategy, depth=kwargs.get("depth", 1)))
