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
"""Writer outline / heading navigation tools.

For a simple document outline (headings hierarchy only), use get_document_tree
with content_strategy=\"heading_only\". For content under a heading by path
(e.g. \"1.2\"), use get_heading_children with locator=\"heading:1.2\".
"""

import logging

from plugin.framework.tool_base import ToolBase

from plugin.modules.writer.base import ToolWriterStructuralBase

log = logging.getLogger("writeragent.writer")


class GetDocumentTree(ToolBase):
    """Document heading tree with bookmarks and optional content. Use for outline or full tree."""

    name = "get_document_tree"
    intent = "navigate"
    tier = "core"
    description = (
        "Get the document heading tree with bookmarks and content previews. "
        "Use content_strategy=\"heading_only\" for a simple outline (headings hierarchy). "
        "Creates _mcp_ bookmarks on headings for stable addressing. "
        "Strategies: heading_only, first_lines (default), ai_summary_first, full. "
        "depth=0 for unlimited, depth=1 (default) for top-level only."
    )
    parameters = {
        "type": "object",
        "properties": {
            "content_strategy": {
                "type": "string",
                "enum": ["heading_only", "first_lines", "ai_summary_first", "full"],
                "description": "Content to include with headings (default: first_lines)",
            },
            "depth": {
                "type": "integer",
                "description": "Max tree depth (0=unlimited, default: 1)",
            },
        },
        "required": [],
    }
    uno_services = ["com.sun.star.text.TextDocument"]

    def execute(self, ctx, **kwargs):
        tree_svc = ctx.services.writer_tree
        result = tree_svc.get_document_tree(
            ctx.doc,
            content_strategy=kwargs.get("content_strategy", "first_lines"),
            depth=kwargs.get("depth", 1),
        )
        return {"status": "ok", **result}


class GetHeadingChildren(ToolWriterStructuralBase):
    name = "get_heading_children"
    intent = "navigate"
    description = (
        "Drill into a heading's children — body paragraphs and sub-headings. "
        "Identify the heading by locator (e.g. 'bookmark:_mcp_xxx', "
        "'heading_text:Title'), heading_para_index, or heading_bookmark."
    )
    parameters = {
        "type": "object",
        "properties": {
            "locator": {
                "type": "string",
                "description": "Locator string (e.g. 'bookmark:_mcp_xxx', 'heading:1.2')",
            },
            "heading_para_index": {
                "type": "integer",
                "description": "Paragraph index of the heading",
            },
            "heading_bookmark": {
                "type": "string",
                "description": "Bookmark name of the heading",
            },
            "content_strategy": {
                "type": "string",
                "enum": ["heading_only", "first_lines", "ai_summary_first", "full"],
                "description": "Content strategy (default: first_lines)",
            },
            "depth": {
                "type": "integer",
                "description": "Max sub-heading depth (default: 1)",
            },
        },
        "required": [],
    }
    uno_services = ["com.sun.star.text.TextDocument"]

    def execute(self, ctx, **kwargs):
        tree_svc = ctx.services.writer_tree
        try:
            result = tree_svc.get_heading_children(
                ctx.doc,
                heading_para_index=kwargs.get("heading_para_index"),
                heading_bookmark=kwargs.get("heading_bookmark"),
                locator=kwargs.get("locator"),
                content_strategy=kwargs.get("content_strategy", "first_lines"),
                depth=kwargs.get("depth", 1),
            )
            return {"status": "ok", **result}
        except ValueError as e:
            return self._tool_error(str(e))


