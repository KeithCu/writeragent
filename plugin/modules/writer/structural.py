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
"""Structural tools: list_sections, goto_page, get_page_objects, read_section, resolve_bookmark.

(Index refresh, field refresh, and bookmark list/cleanup live in specialized domains.)"""

from plugin.framework.tool_base import ToolBase, ToolBaseDummy


class ListSections(ToolBaseDummy):
    name = "list_sections"
    intent = "navigate"
    description = "List all named sections in the document."
    parameters = {"type": "object", "properties": {}, "required": []}
    uno_services = ["com.sun.star.text.TextDocument"]

    def execute(self, ctx, **kwargs):
        doc = ctx.doc
        if not hasattr(doc, "getTextSections"):
            return {"status": "ok", "sections": [], "count": 0}
        supplier = doc.getTextSections()
        names = supplier.getElementNames()
        sections = []
        for name in names:
            section = supplier.getByName(name)
            sections.append({
                "name": name,
                "is_visible": getattr(section, "IsVisible", True),
                "is_protected": getattr(section, "IsProtected", False),
            })
        return {"status": "ok", "sections": sections, "count": len(sections)}
class GotoPage(ToolBaseDummy):
    name = "goto_page"
    intent = "navigate"
    description = "Navigate the view cursor to a specific page."
    parameters = {
        "type": "object",
        "properties": {
            "page": {"type": "integer", "description": "Page number to navigate to"},
        },
        "required": ["page"],
    }
    uno_services = ["com.sun.star.text.TextDocument"]

    def execute(self, ctx, **kwargs):
        controller = ctx.doc.getCurrentController()
        vc = controller.getViewCursor()
        vc.jumpToPage(kwargs["page"])
        return {"status": "ok", "page": vc.getPage()}
class GetPageObjects(ToolBaseDummy):
    name = "get_page_objects"
    intent = "navigate"
    description = (
        "Get images, tables, and frames on a specific page. "
        "Provide page number, locator, or paragraph_index."
    )
    parameters = {
        "type": "object",
        "properties": {
            "page": {"type": "integer", "description": "Page number"},
            "locator": {"type": "string", "description": "Locator to determine page"},
            "paragraph_index": {"type": "integer", "description": "Paragraph index to determine page"},
        },
        "required": [],
    }
    uno_services = ["com.sun.star.text.TextDocument"]

    def execute(self, ctx, **kwargs):
        doc = ctx.doc
        doc_svc = ctx.services.document
        page = kwargs.get("page")

        if page is None:
            locator = kwargs.get("locator")
            para_idx = kwargs.get("paragraph_index")
            if locator:
                try:
                    resolved = doc_svc.resolve_locator(doc, locator)
                    para_idx = resolved.get("para_index", 0)
                except ValueError as e:
                    return self._tool_error(str(e))
            if para_idx is not None:
                page = doc_svc.get_page_for_paragraph(doc, para_idx)
            else:
                try:
                    page = doc.getCurrentController().getViewCursor().getPage()
                except Exception:
                    page = 1

        controller = doc.getCurrentController()
        vc = controller.getViewCursor()
        saved = doc.getText().createTextCursorByRange(vc.getStart())
        doc.lockControllers()
        try:
            objects = self._scan_page(doc, vc, page)
        finally:
            vc.gotoRange(saved, False)
            doc.unlockControllers()
        return {"status": "ok", "page": page, **objects}
    def _scan_page(self, doc, vc, page):
        images = []
        if hasattr(doc, "getGraphicObjects"):
            for name in doc.getGraphicObjects().getElementNames():
                try:
                    g = doc.getGraphicObjects().getByName(name)
                    vc.gotoRange(g.getAnchor(), False)
                    if vc.getPage() == page:
                        size = g.getPropertyValue("Size")
                        images.append({
                            "name": name,
                            "width_mm": size.Width // 100,
                            "height_mm": size.Height // 100,
                            "title": g.getPropertyValue("Title"),
                        })
                except Exception:
                    pass

        tables = []
        if hasattr(doc, "getTextTables"):
            for name in doc.getTextTables().getElementNames():
                try:
                    t = doc.getTextTables().getByName(name)
                    vc.gotoRange(t.getAnchor(), False)
                    if vc.getPage() == page:
                        tables.append({
                            "name": name,
                            "rows": t.getRows().getCount(),
                            "cols": t.getColumns().getCount(),
                        })
                except Exception:
                    pass

        frames = []
        if hasattr(doc, "getTextFrames"):
            for fname in doc.getTextFrames().getElementNames():
                try:
                    fr = doc.getTextFrames().getByName(fname)
                    vc.gotoRange(fr.getAnchor(), False)
                    if vc.getPage() == page:
                        size = fr.getPropertyValue("Size")
                        frames.append({
                            "name": fname,
                            "width_mm": size.Width // 100,
                            "height_mm": size.Height // 100,
                        })
                except Exception:
                    pass

        return {"images": images, "tables": tables, "frames": frames}


class ReadSection(ToolBaseDummy):
    """Read the content of a named text section."""

    name = "read_section"
    intent = "navigate"
    description = (
        "Read the text content of a named section. "
        "Returns the full text within the section boundaries."
    )
    parameters = {
        "type": "object",
        "properties": {
            "section_name": {
                "type": "string",
                "description": "Name of the section to read.",
            },
        },
        "required": ["section_name"],
    }
    uno_services = ["com.sun.star.text.TextDocument"]

    def execute(self, ctx, **kwargs):
        section_name = kwargs.get("section_name", "")
        if not section_name:
            return self._tool_error("section_name is required.")

        doc = ctx.doc
        if not hasattr(doc, "getTextSections"):
            return self._tool_error("Document does not support sections.")

        sections = doc.getTextSections()
        if not sections.hasByName(section_name):
            available = list(sections.getElementNames())
            return self._tool_error("Section '%s' not found." % section_name,
                available=available)

        section = sections.getByName(section_name)
        anchor = section.getAnchor()

        # Extract paragraphs within the section
        enum = anchor.createEnumeration()
        paragraphs = []
        while enum.hasMoreElements():
            para = enum.nextElement()
            if para.supportsService("com.sun.star.text.Paragraph"):
                paragraphs.append(para.getString())
            else:
                paragraphs.append("[Table]")

        content = "\n".join(paragraphs)
        return {
            "status": "ok",
            "section_name": section_name,
            "paragraphs": paragraphs,
            "content": content,
            "length": len(content),
        }
class ResolveBookmark(ToolBaseDummy):
    """Resolve a bookmark to its paragraph index and heading text."""

    name = "resolve_bookmark"
    intent = "navigate"
    description = (
        "Resolve a bookmark to its current paragraph index and text. "
        "Most tools accept 'bookmark:NAME' as locator directly -- use "
        "resolve_bookmark only when you need the raw paragraph index."
    )
    parameters = {
        "type": "object",
        "properties": {
            "bookmark_name": {
                "type": "string",
                "description": "Bookmark name (e.g. _mcp_a1b2c3d4).",
            },
        },
        "required": ["bookmark_name"],
    }
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
                hint += (
                    " It may have been deleted or the document changed. "
                    "Use heading_text:<text> locator for resilient "
                    "heading addressing, or call get_document_tree "
                    "to refresh bookmarks."
                )
                existing = [
                    n for n in bookmarks.getElementNames()
                    if n.startswith("_mcp_")
                ]
                if existing:
                    hint += " Existing bookmarks: %s" % ", ".join(existing[:10])
            return self._tool_error(hint)

        bm = bookmarks.getByName(bookmark_name)
        anchor = bm.getAnchor()

        # Find paragraph index
        doc_svc = ctx.services.document
        para_ranges = doc_svc.get_paragraph_ranges(doc)
        text_obj = doc.getText()
        para_idx = doc_svc.find_paragraph_for_range(
            anchor, para_ranges, text_obj
        )

        result = {
            "status": "ok",
            "bookmark": bookmark_name,
            "paragraph_index": para_idx,
        }

        # Get heading text if available
        if 0 <= para_idx < len(para_ranges):
            element = para_ranges[para_idx]
            if element.supportsService("com.sun.star.text.Paragraph"):
                try:
                    result["text"] = element.getString()
                    result["outline_level"] = element.getPropertyValue(
                        "OutlineLevel"
                    )
                except Exception:
                    pass

        return result
