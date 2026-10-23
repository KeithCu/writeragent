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
with strategy=\"heading_only\". heading:1.2 is the sibling-ordinal
path (1st H1 → 2nd child), not Writer's chapter label. When Tools → Chapter
Numbering is on, heading nodes include chapter_number and
locator=\"chapter_number:3.1\" matches that paint label.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from plugin.framework.errors import best_effort, uno_read
from plugin.framework.tool import ToolBase
from plugin.writer.specialized_base import enum_param

if TYPE_CHECKING:
    from plugin.framework.tool import ToolContext

from plugin.doc.text_helpers import build_heading_tree, get_string_without_tracked_deletions, iter_heading_nodes


class GetDocumentTree(ToolBase):
    """Document heading tree with bookmarks, optional content, and document statistics."""

    # FIXME: Consider renaming (e.g. get_document_overview) — tool returns tree + stats, not tree alone.

    name: str | None = "get_document_tree"
    intent: str | None = "navigate"
    tier: str = "core"
    description: str = (
        "Get the document heading tree with bookmarks and content previews, plus document statistics. "
        "The stats object includes character_count, word_count, paragraph_count, page_count, and heading_count. "
        'Use strategy="heading_only" for a simple outline (headings hierarchy). '
        "Creates session-only _mcp_ bookmarks on headings for stable addressing "
        "(omitted from saved files). "
        "When Tools → Chapter Numbering is on, each heading includes chapter_number "
        "(the paint label, e.g. '3.1'); the key is omitted when numbering is off. "
        "Use that field — never invent numbers from outline depth, sibling order, "
        "or literal titles like 'DOCUMENT 7'. heading:1.2 is sibling-ordinal path, "
        "not the chapter label; use chapter_number:3.1 when the field is present. "
        "Strategies: heading_only, first_lines (default), full. "
        "depth=0 for unlimited, depth=1 (default) for top-level only. "
        "IMPORTANT: para_index is an INTERNAL addressing index — NEVER cite paragraph numbers to "
        "the user (they don't see them and they shift as the document changes). When pointing the "
        "user to a location or an edit, quote the first few words of its text instead "
        "(e.g. \"the sentence starting 'The Amazon…'\")."
    )
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {"strategy": enum_param(["heading_only", "first_lines", "full"], "Content to include with headings (default: first_lines)"), "depth": {"type": "integer", "description": "Max tree depth (0=unlimited, default: 1)"}},
        "required": [],
    }
    uno_services: list[str] | None = ["com.sun.star.text.TextDocument"]

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        tree_svc = ctx.services.writer_tree
        strategy = kwargs.get("strategy", "first_lines")
        result = tree_svc.get_document_tree(ctx.doc, content_strategy=strategy, depth=kwargs.get("depth", 1))
        heading_tree = tree_svc.build_heading_tree(ctx.doc)
        stats = collect_document_stats(
            ctx.doc,
            ctx.services.document,
            heading_tree=heading_tree,
            paragraph_count=result.get("total_paragraphs"),
            page_count=result.get("page_count"),
        )
        return {**result, "stats": stats}


def _count_headings(tree: Any) -> int:
    """Recursively count heading nodes in a tree."""
    return sum(1 for _unused in iter_heading_nodes(tree))


def collect_document_stats(
    doc: Any,
    doc_svc: Any,
    *,
    heading_tree: Any = None,
    paragraph_count: int | None = None,
    page_count: int | None = None,
) -> dict[str, Any]:
    """Character/word/paragraph/page/heading counts for a Writer document.

    Each count degrades on its own. Headings use uno_read so a closed
    document raises instead of looking like zero headings.
    """

    def _visible_counts() -> tuple[int, int]:
        text_obj = doc.getText()
        cursor = text_obj.createTextCursor()
        cursor.gotoStart(False)
        cursor.gotoEnd(True)
        full_text = get_string_without_tracked_deletions(cursor)
        return len(full_text), len(full_text.split())

    # None means the read failed; (0, 0) is a real empty document.
    counted = best_effort(_visible_counts, "character and word counts")
    if counted is None:
        char_count = doc_svc.get_document_length(doc)
        word_count = 0
    else:
        char_count, word_count = counted

    if paragraph_count is not None:
        para_count = paragraph_count
    else:
        para_count = best_effort(
            lambda: len(doc_svc.get_paragraph_ranges(doc)),
            "paragraph count",
            0,
        )

    if heading_tree is not None:
        heading_count = _count_headings(heading_tree)
    else:
        heading_count = uno_read(
            lambda: _count_headings(build_heading_tree(doc)),
            0,
            object_type="Document",
        )

    if page_count is not None:
        final_page_count = page_count
    else:
        final_page_count = best_effort(lambda: doc_svc.get_page_count(doc), "page count", 0)

    return {
        "character_count": char_count,
        "word_count": word_count,
        "paragraph_count": para_count,
        "page_count": final_page_count,
        "heading_count": heading_count,
    }
