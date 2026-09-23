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
(e.g. \"1.2\"), use nav_heading_children with locator=\"heading:1.2\".
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from plugin.framework.tool import ToolBase

if TYPE_CHECKING:
    from plugin.framework.tool import ToolContext

from plugin.doc.text_helpers import get_string_without_tracked_deletions

log = logging.getLogger("writeragent.writer")


class GetDocumentTree(ToolBase):
    """Document heading tree with bookmarks, optional content, and document statistics."""

    # FIXME: Consider renaming (e.g. get_document_overview) — tool returns tree + stats, not tree alone.

    name = "get_document_tree"
    intent = "navigate"
    tier = "core"
    description = (
        "Get the document heading tree with bookmarks and content previews, plus document statistics. "
        "The stats object includes character_count, word_count, paragraph_count, page_count, and heading_count. "
        'Use strategy="heading_only" for a simple outline (headings hierarchy). '
        "Creates session-only _mcp_ bookmarks on headings for stable addressing "
        "(omitted from saved files). "
        "Strategies: heading_only, first_lines (default), full. "
        "depth=0 for unlimited, depth=1 (default) for top-level only. "
        "IMPORTANT: para_index is an INTERNAL addressing index — NEVER cite paragraph numbers to "
        "the user (they don't see them and they shift as the document changes). When pointing the "
        "user to a location or an edit, quote the first few words of its text instead "
        "(e.g. \"the sentence starting 'The Amazon…'\")."
    )
    parameters = {
        "type": "object",
        "properties": {"strategy": {"type": "string", "enum": ["heading_only", "first_lines", "full"], "description": "Content to include with headings (default: first_lines)"}, "depth": {"type": "integer", "description": "Max tree depth (0=unlimited, default: 1)"}},
        "required": [],
    }
    uno_services = ["com.sun.star.text.TextDocument"]

    def execute(self, ctx: ToolContext, **kwargs: Any):
        tree_svc = ctx.services.writer_tree
        strategy = kwargs.get("strategy", "first_lines")
        result = tree_svc.get_document_tree(ctx.doc, content_strategy=strategy, depth=kwargs.get("depth", 1))
        stats = collect_document_stats(ctx.doc, ctx.services.document)
        return {**result, "stats": stats}


# NavHeadingChildren is defined in .navigation alongside other Nav* tools; re-exported here for compatibility
from .navigation import NavHeadingChildren as NavHeadingChildren  # noqa: F401



def _count_headings(nodes: Any) -> int:
    """Recursively count heading nodes in a nested list."""
    count = 0
    for node in nodes:
        count += 1
        count += _count_headings(node.get("children", []))
    return count


def collect_document_stats(doc: Any, doc_svc: Any):
    """Character/word/paragraph/page/heading counts for a Writer document."""
    from plugin.doc.text_helpers import build_heading_tree

    try:
        text_obj = doc.getText()
        cursor = text_obj.createTextCursor()
        cursor.gotoStart(False)
        cursor.gotoEnd(True)
        full_text = get_string_without_tracked_deletions(cursor)
        char_count = len(full_text)
        word_count = len(full_text.split())
    except Exception:
        char_count = doc_svc.get_document_length(doc)
        word_count = 0

    try:
        para_ranges = doc_svc.get_paragraph_ranges(doc)
        para_count = len(para_ranges)
    except Exception:
        para_count = 0

    try:
        tree = build_heading_tree(doc)
        heading_count = _count_headings(tree.get("children", []))
    except Exception:
        heading_count = 0

    page_count = 0
    try:
        page_count = doc_svc.get_page_count(doc)
    except Exception:
        try:
            vc = doc.getCurrentController().getViewCursor()
            vc.jumpToLastPage()
            page_count = vc.getPage()
        except Exception:
            pass

    return {
        "character_count": char_count,
        "word_count": word_count,
        "paragraph_count": para_count,
        "page_count": page_count,
        "heading_count": heading_count,
    }
