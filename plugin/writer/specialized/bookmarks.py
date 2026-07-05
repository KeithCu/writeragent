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
"""BookmarkService and tools for Writer documents."""

import logging
import uuid

from plugin.framework.service import ServiceBase
from ..specialized_base import ToolWriterBookmarkBase

log = logging.getLogger("writeragent.writer.nav.bookmarks")


class BookmarkService(ServiceBase):
    """Manage _mcp_ bookmarks on headings for stable addressing."""

    name = "writer_bookmarks"

    def __init__(self, services):
        self._doc_svc = services.document

    def get_mcp_bookmark_map(self, doc):
        """Return {para_index: bookmark_name} for all _mcp_ bookmarks."""
        result = {}
        try:
            if not hasattr(doc, "getBookmarks"):
                return result
            bookmarks = doc.getBookmarks()
            names = bookmarks.getElementNames()
            if not names:
                return result
            para_ranges = self._doc_svc.get_paragraph_ranges(doc)
            text_obj = doc.getText()
            for name in names:
                if not name.startswith("_mcp_"):
                    continue
                bm = bookmarks.getByName(name)
                anchor = bm.getAnchor()
                para_idx = self._doc_svc.find_paragraph_for_range(anchor, para_ranges, text_obj)
                if para_idx >= 0:
                    result[para_idx] = name
        except Exception:
            log.exception("Failed to get MCP bookmark map")

        return result

    def ensure_heading_bookmarks(self, doc):
        """Ensure every heading has an _mcp_ bookmark. Returns map."""
        existing_map = self.get_mcp_bookmark_map(doc)
        text = doc.getText()
        enum = text.createEnumeration()
        para_index = 0
        bookmark_map = {}
        needs_bookmark = []

        while enum.hasMoreElements():
            element = enum.nextElement()
            if element.supportsService("com.sun.star.text.Paragraph"):
                outline_level = 0
                try:
                    outline_level = element.getPropertyValue("OutlineLevel")
                except Exception:
                    pass
                if outline_level > 0:
                    if para_index in existing_map:
                        bookmark_map[para_index] = existing_map[para_index]
                    else:
                        needs_bookmark.append((para_index, element.getStart()))
            para_index += 1
            self._doc_svc.yield_to_gui()

        for para_idx, start_range in needs_bookmark:
            bm_name = "_mcp_%s" % uuid.uuid4().hex[:8]
            bookmark = doc.createInstance("com.sun.star.text.Bookmark")
            bookmark.Name = bm_name
            cursor = text.createTextCursorByRange(start_range)
            text.insertTextContent(cursor, bookmark, False)
            bookmark_map[para_idx] = bm_name

        # Deliberately NO doc.store() here: this runs inside READ tools (get_document_tree,
        # proximity reads), and saving would silently persist the user's unsaved manual edits
        # and pending tracked changes to disk. The bookmarks live in the document model and are
        # persisted whenever the USER next saves; on a discarded session they vanish with the
        # rest of the unsaved state, which is exactly what not-saving means.
        return bookmark_map

    def find_nearest_heading_bookmark(self, para_index, bookmark_map):
        """Find nearest heading bookmark at or before para_index."""
        best_idx = -1
        for idx in bookmark_map:
            if idx <= para_index and idx > best_idx:
                best_idx = idx
        if best_idx >= 0:
            return {"bookmark": bookmark_map[best_idx], "heading_para_index": best_idx}
        return None

    def cleanup_mcp_bookmarks(self, doc):
        """Remove all _mcp_* bookmarks from the document."""
        removed = 0
        try:
            if not hasattr(doc, "getBookmarks"):
                return removed
            bookmarks = doc.getBookmarks()
            names = bookmarks.getElementNames()
            text = doc.getText()
            for name in names:
                if name.startswith("_mcp_"):
                    try:
                        bm = bookmarks.getByName(name)
                        text.removeTextContent(bm)
                        removed += 1
                    except Exception:
                        pass
            # No doc.store() (same reason as ensure_heading_bookmarks: a maintenance path must
            # never persist the user's unsaved work as a side effect).
        except Exception:
            log.exception("Failed to cleanup bookmarks")
        return removed


# ── Bookmark Tools ────────────────────────────────────────────────────


class ListBookmarks(ToolWriterBookmarkBase):
    name = "list_bookmarks"
    description = "List all bookmarks in the document with their anchor text preview. Includes both user bookmarks and _mcp_ heading bookmarks."
    parameters = {"type": "object", "properties": {}, "required": []}

    def execute(self, ctx, **kwargs):
        doc = ctx.doc
        if not hasattr(doc, "getBookmarks"):
            return {"status": "ok", "bookmarks": [], "count": 0}
        try:
            bookmarks = doc.getBookmarks()
            names = bookmarks.getElementNames()
            result = []
            for name in names:
                bm = bookmarks.getByName(name)
                anchor_text = bm.getAnchor().getString()
                result.append({"name": name, "text": anchor_text[:100] if anchor_text else ""})
            return {"status": "ok", "bookmarks": result, "count": len(result)}
        except Exception as e:
            return {"status": "error", "message": str(e)}


class CleanupBookmarks(ToolWriterBookmarkBase):
    name = "cleanup_bookmarks"
    description = "Remove all _mcp_* bookmarks from the document. Use when bookmarks become stale after major edits."
    parameters = {"type": "object", "properties": {}, "required": []}
    is_mutation = True

    def execute(self, ctx, **kwargs):
        bm_svc = ctx.services.writer_bookmarks
        removed = bm_svc.cleanup_mcp_bookmarks(ctx.doc)
        return {"status": "ok", "removed": removed}


class CreateBookmark(ToolWriterBookmarkBase):
    name = "create_bookmark"
    description = "Create a new bookmark at the current cursor or selection in Writer. If text is selected, the bookmark will span the selection."
    parameters = {"type": "object", "properties": {"name": {"type": "string", "description": "The unique name for the new bookmark."}}, "required": ["name"]}
    is_mutation = True

    def execute(self, ctx, **kwargs):
        doc = ctx.doc
        name = kwargs.get("name")
        if not name:
            return self._tool_error("Bookmark name is required.")

        try:
            if not hasattr(doc, "getBookmarks"):
                return self._tool_error("Document does not support bookmarks.")

            bookmarks = doc.getBookmarks()
            if bookmarks.hasByName(name):
                return self._tool_error(f"A bookmark named '{name}' already exists.")

            ctrl = doc.getCurrentController()
            if not ctrl:
                return self._tool_error("No current controller found.")

            view_cursor = ctrl.getViewCursor()
            if not view_cursor:
                return self._tool_error("No view cursor found.")

            text = view_cursor.getText()
            if not text:
                return self._tool_error("Cannot get text from current cursor position.")

            bookmark = doc.createInstance("com.sun.star.text.Bookmark")
            bookmark.Name = name

            # insertTextContent signature: (XTextRange xRange, XTextContent xContent, boolean bAbsorb)
            # If bAbsorb is True, the text content replaces or spans the current selection.
            # If False, it's inserted as a point. We'll use True so if there's a selection, it's spanned.
            text.insertTextContent(view_cursor, bookmark, True)

            return {"status": "ok", "message": f"Bookmark '{name}' created."}
        except Exception as e:
            return self._tool_error(f"Failed to create bookmark: {str(e)}")


class DeleteBookmark(ToolWriterBookmarkBase):
    name = "delete_bookmark"
    description = "Delete an existing bookmark by its name."
    parameters = {"type": "object", "properties": {"name": {"type": "string", "description": "The name of the bookmark to delete."}}, "required": ["name"]}
    is_mutation = True

    def execute(self, ctx, **kwargs):
        doc = ctx.doc
        name = kwargs.get("name")
        if not name:
            return self._tool_error("Bookmark name is required.")

        try:
            if not hasattr(doc, "getBookmarks"):
                return self._tool_error("Document does not support bookmarks.")

            bookmarks = doc.getBookmarks()
            if not bookmarks.hasByName(name):
                return self._tool_error(f"Bookmark '{name}' not found.")

            bm = bookmarks.getByName(name)
            anchor = bm.getAnchor()
            text = anchor.getText()

            text.removeTextContent(bm)

            return {"status": "ok", "message": f"Bookmark '{name}' deleted."}
        except Exception as e:
            return self._tool_error(f"Failed to delete bookmark: {str(e)}")


class RenameBookmark(ToolWriterBookmarkBase):
    name = "rename_bookmark"
    description = "Rename an existing bookmark."
    parameters = {"type": "object", "properties": {"old_name": {"type": "string", "description": "The current name of the bookmark."}, "new_name": {"type": "string", "description": "The new name for the bookmark."}}, "required": ["old_name", "new_name"]}
    is_mutation = True

    def execute(self, ctx, **kwargs):
        doc = ctx.doc
        old_name = kwargs.get("old_name")
        new_name = kwargs.get("new_name")

        if not old_name or not new_name:
            return self._tool_error("Both old_name and new_name are required.")

        try:
            if not hasattr(doc, "getBookmarks"):
                return self._tool_error("Document does not support bookmarks.")

            bookmarks = doc.getBookmarks()
            if not bookmarks.hasByName(old_name):
                return self._tool_error(f"Bookmark '{old_name}' not found.")

            if bookmarks.hasByName(new_name):
                return self._tool_error(f"A bookmark named '{new_name}' already exists.")

            bm = bookmarks.getByName(old_name)
            bm.setName(new_name)

            return {"status": "ok", "message": f"Bookmark renamed from '{old_name}' to '{new_name}'."}
        except Exception as e:
            return self._tool_error(f"Failed to rename bookmark: {str(e)}")


class GetBookmark(ToolWriterBookmarkBase):
    name = "get_bookmark"
    description = "Get details about a specific bookmark, including the text it spans."
    parameters = {"type": "object", "properties": {"name": {"type": "string", "description": "The name of the bookmark."}}, "required": ["name"]}

    def execute(self, ctx, **kwargs):
        doc = ctx.doc
        name = kwargs.get("name")
        if not name:
            return self._tool_error("Bookmark name is required.")

        try:
            if not hasattr(doc, "getBookmarks"):
                return self._tool_error("Document does not support bookmarks.")

            bookmarks = doc.getBookmarks()
            if not bookmarks.hasByName(name):
                return self._tool_error(f"Bookmark '{name}' not found.")

            bm = bookmarks.getByName(name)
            anchor = bm.getAnchor()
            text_content = anchor.getString()

            return {"status": "ok", "bookmark": {"name": name, "text": text_content}}
        except Exception as e:
            return self._tool_error(f"Failed to get bookmark details: {str(e)}")


class ResolveBookmark(ToolWriterBookmarkBase):
    """Resolve a bookmark to its paragraph index and heading text."""

    name = "resolve_bookmark"
    intent = "navigate"
    description = "Resolve a bookmark to its current paragraph index and text. Most tools accept 'bookmark:NAME' as locator directly -- use resolve_bookmark only when you need the raw paragraph index."
    parameters = {"type": "object", "properties": {"bookmark_name": {"type": "string", "description": "Bookmark name (e.g. _mcp_a1b2c3d4)."}}, "required": ["bookmark_name"]}
    uno_services = ["com.sun.star.text.TextDocument"]

    def execute(self, ctx, **kwargs):
        bookmark_name = kwargs.get("bookmark_name", "")
        if not bookmark_name:
            return self._tool_error("bookmark_name is required.")

        doc = ctx.doc
        if not hasattr(doc, "getBookmarks"):
            return self._tool_error("Document does not support bookmarks.")

        bookmarks = doc.getBookmarks()
        if not bookmarks.hasByName(bookmark_name):
            hint = "Bookmark '%s' not found." % bookmark_name
            if bookmark_name.startswith("_mcp_"):
                hint += " It may have been deleted or the document changed. Use heading_text:<text> locator for resilient heading addressing, or call get_document_tree to refresh bookmarks."
                existing = [n for n in bookmarks.getElementNames() if n.startswith("_mcp_")]
                if existing:
                    hint += " Existing bookmarks: %s" % ", ".join(existing[:10])
            return self._tool_error(hint)

        bm = bookmarks.getByName(bookmark_name)
        anchor = bm.getAnchor()

        # Find paragraph index
        doc_svc = ctx.services.document
        para_ranges = doc_svc.get_paragraph_ranges(doc)
        text_obj = doc.getText()
        para_idx = doc_svc.find_paragraph_for_range(anchor, para_ranges, text_obj)

        result = {"status": "ok", "bookmark": bookmark_name, "paragraph_index": para_idx}

        # Get heading text if available
        if 0 <= para_idx < len(para_ranges):
            element = para_ranges[para_idx]
            if element.supportsService("com.sun.star.text.Paragraph"):
                try:
                    result["text"] = element.getString()
                    result["outline_level"] = element.getPropertyValue("OutlineLevel")
                except Exception:
                    pass

        return result
