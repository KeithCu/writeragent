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
"""BookmarkService — heading bookmarks (stable IDs) for Writer documents.

Ported from mcp-libre services/writer/tree.py (bookmark methods).
"""

import logging
import uuid

from plugin.framework.service_base import ServiceBase

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
                para_idx = self._doc_svc.find_paragraph_for_range(
                    anchor, para_ranges, text_obj)
                if para_idx >= 0:
                    result[para_idx] = name
        except Exception as e:
            log.error("Failed to get MCP bookmark map: %s", e)

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
                        needs_bookmark.append(
                            (para_index, element.getStart()))
            para_index += 1
            self._doc_svc.yield_to_gui()

        for para_idx, start_range in needs_bookmark:
            bm_name = "_mcp_%s" % uuid.uuid4().hex[:8]
            bookmark = doc.createInstance("com.sun.star.text.Bookmark")
            bookmark.Name = bm_name
            cursor = text.createTextCursorByRange(start_range)
            text.insertTextContent(cursor, bookmark, False)
            bookmark_map[para_idx] = bm_name

        if needs_bookmark:
            try:
                if doc.hasLocation():
                    doc.store()
            except Exception:
                pass

        return bookmark_map

    def find_nearest_heading_bookmark(self, para_index, bookmark_map):
        """Find nearest heading bookmark at or before para_index."""
        best_idx = -1
        for idx in bookmark_map:
            if idx <= para_index and idx > best_idx:
                best_idx = idx
        if best_idx >= 0:
            return {"bookmark": bookmark_map[best_idx],
                    "heading_para_index": best_idx}
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
            if removed:
                try:
                    if doc.hasLocation():
                        doc.store()
                except Exception:
                    pass
        except Exception as e:
            log.error("Failed to cleanup bookmarks: %s", e)
        return removed
