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
"""Structural tools: section_list, nav_goto_page, get_page_objects, section_read, bookmark_resolve.

(Index refresh, field refresh, and bookmark list/cleanup live in specialized domains.)"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from plugin.doc.text_helpers import clone_text_range
from plugin.framework.prompts import PARAGRAPH_INDEX_DIRECTIVE
from plugin.framework.tool import ToolBase, ToolBaseDummy

from .specialized_base import ToolWriterStructuralBase

if TYPE_CHECKING:
    from plugin.framework.tool import ToolContext


def _at_page_anchor_page(obj: Any):
    """Physical page if *obj* is AT_PAGE with AnchorPageNo; else None.

    AnchorPageNo is only valid for AT_PAGE (text.Shape / TextFrame /
    TextGraphicObject). AT_PARAGRAPH objects report 0 — that is not a page
    index. Tables have no page property. Any other range is view-cursor
    getPage() only (no UNO page-of-range API).
    """
    try:
        from com.sun.star.text.TextContentAnchorType import AT_PAGE

        if obj.getPropertyValue("AnchorType") != AT_PAGE:
            return None
        return obj.getPropertyValue("AnchorPageNo")
    except Exception:
        return None


def _with_left_body_locked(doc: Any, vc: Any, scan_fn: Any):
    """Leave nested XText, lock for the scan, unlock before restore.

    Why leave first: lockControllers while the view cursor sits in a table
    cell makes gotoRange/getPage fail silently (Cneg: tables=[]). jumpToPage
    is a no-op on the same page (the cell). Unlocked hop to
    doc.getText().getStart() first.

    Why lock after leave: headed visarea — never-lock C hits Y=37017 on
    page-2 hops; leave+lock A does not. Flicker needs lock during the
    multi-object scan.

    Why unlock before restore: gotoRange into a nested cell fails while
    locked. Save/restore uses clone_text_range (vc.getText()), not body
    XText. If the leave hop fails, scan unlocked — empty page is valid.
    """
    saved = None
    try:
        # Nested XText (table cell / frame): body getText() cannot clone this range.
        saved = clone_text_range(vc)
    except Exception:
        pass
    in_body = False
    try:
        vc.gotoRange(doc.getText().getStart(), False)
        in_body = True
    except Exception:
        pass
    if in_body:
        doc.lockControllers()
    try:
        return scan_fn()
    finally:
        if in_body:
            doc.unlockControllers()
        if saved is not None:
            try:
                vc.gotoRange(saved, False)
            except Exception:
                pass


class SectionList(ToolWriterStructuralBase):
    name: str | None = "section_list"
    intent: str | None = "navigate"
    description: str = "List all named sections in the document."
    parameters: dict[str, Any] | None = {"type": "object", "properties": {}, "required": []}
    uno_services: list[str] | None = ["com.sun.star.text.TextDocument"]

    def execute(self, ctx: ToolContext, **kwargs: Any):
        doc = ctx.doc
        if not hasattr(doc, "getTextSections"):
            return {"status": "ok", "sections": [], "count": 0}
        supplier = doc.getTextSections()
        names = supplier.getElementNames()
        sections = []
        for name in names:
            section = supplier.getByName(name)
            sections.append({"name": name, "is_visible": getattr(section, "IsVisible", True), "is_protected": getattr(section, "IsProtected", False)})
        return {"status": "ok", "sections": sections, "count": len(sections)}


class NavGotoPage(ToolWriterStructuralBase):
    name: str | None = "nav_goto_page"
    intent: str | None = "navigate"
    is_mutation: bool | None = False
    description: str = "Navigate the view cursor to a specific page."
    parameters: dict[str, Any] | None = {"type": "object", "properties": {"page": {"type": "integer", "description": "Page number to navigate to"}}, "required": ["page"]}
    uno_services: list[str] | None = ["com.sun.star.text.TextDocument"]

    def execute(self, ctx: ToolContext, **kwargs: Any):
        controller = ctx.doc.getCurrentController()
        vc = controller.getViewCursor()
        vc.jumpToPage(kwargs["page"])
        return {"status": "ok", "page": vc.getPage()}


class GetPageObjects(ToolBase):
    name: str | None = "get_page_objects"
    intent: str | None = "read"
    description: str = (
        "Get images, tables, frames, and Draw shapes visible on a specific physical page. Provide "
        "page number, locator, or paragraph. " + PARAGRAPH_INDEX_DIRECTIVE
    )
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {"page": {"type": "integer", "description": "1-based page number to analyze"}, "locator": {"type": "string", "description": "Locator to determine page"}, "paragraph": {"type": "integer", "description": "Paragraph index to determine page"}},
        "required": [],
    }
    uno_services: list[str] | None = ["com.sun.star.text.TextDocument"]
    tier: str = "core"

    def execute(self, ctx: ToolContext, **kwargs: Any):
        doc = ctx.doc
        doc_svc = ctx.services.document
        page = kwargs.get("page")

        if page is None:
            locator = kwargs.get("locator")
            para_idx = kwargs.get("paragraph")
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
        objects = _with_left_body_locked(doc, vc, lambda: self._scan_page(ctx, doc, vc, page))
        return {"status": "ok", "page": page, **objects}

    def _page_at_range(self, doc: Any, vc: Any, rng: Any, retry_if_zero: bool = False):
        """View-cursor page of *rng*. Pages are 1-based.

        After leave-then-lock, getPage() is fine at body text. A locked
        gotoRange to a table/frame (and some graphic) getAnchor() leaves
        getPage() at 0 — stale layout, not an empty page (the cursor does
        not enter the table; TextTable stays empty). Unlock, hop again,
        relock when retry_if_zero is True.

        Ordinary paragraph/body ranges: getPage()==0 means not on a page;
        skip without unlock churn.
        """
        vc.gotoRange(rng, False)
        page_no = vc.getPage()
        if page_no != 0 or not retry_if_zero:
            return page_no
        has_locked = getattr(doc, "hasControllersLocked", None)
        if has_locked is None or not has_locked():
            return page_no
        doc.unlockControllers()
        try:
            vc.gotoRange(rng, False)
            return vc.getPage()
        finally:
            doc.lockControllers()

    def _content_page(self, doc: Any, vc: Any, obj: Any, retry_if_zero: bool):
        """Page of a graphic/frame/table: AT_PAGE via AnchorPageNo, else view hop."""
        page_no = _at_page_anchor_page(obj)
        if page_no is not None:
            return page_no
        return self._page_at_range(doc, vc, obj.getAnchor(), retry_if_zero=retry_if_zero)

    def _scan_page(self, ctx: ToolContext, doc: Any, vc: Any, page: Any):
        images = []
        if hasattr(doc, "getGraphicObjects"):
            for name in doc.getGraphicObjects().getElementNames():
                try:
                    g = doc.getGraphicObjects().getByName(name)
                    if self._content_page(doc, vc, g, retry_if_zero=True) == page:
                        size = g.getPropertyValue("Size")
                        images.append({"name": name, "width_mm": size.Width // 100, "height_mm": size.Height // 100, "title": g.getPropertyValue("Title")})
                except Exception:
                    pass

        tables = []
        if hasattr(doc, "getTextTables"):
            for name in doc.getTextTables().getElementNames():
                try:
                    t = doc.getTextTables().getByName(name)
                    if self._content_page(doc, vc, t, retry_if_zero=True) == page:
                        tables.append({"name": name, "rows": t.getRows().getCount(), "cols": t.getColumns().getCount()})
                except Exception:
                    pass

        frames = []
        if hasattr(doc, "getTextFrames"):
            for fname in doc.getTextFrames().getElementNames():
                try:
                    fr = doc.getTextFrames().getByName(fname)
                    if self._content_page(doc, vc, fr, retry_if_zero=True) == page:
                        size = fr.getPropertyValue("Size")
                        frames.append({"name": fname, "width_mm": size.Width // 100, "height_mm": size.Height // 100})
                except Exception:
                    pass

        # Do not jumpToEndOfPage + body createTextCursorByRange: end-of-page often sits in a
        # table/frame, and the body XText then raises RuntimeException ("End of content node
        # doesn't have the proper start node"). Same view-cursor page check as tables/images.
        # AT_PAGE shapes use AnchorPageNo — never _page_at_range (no view hop).
        shapes = []
        if hasattr(doc, "getDrawPage"):
            draw_page = doc.getDrawPage()
            from com.sun.star.text.TextContentAnchorType import AT_PARAGRAPH, AT_CHARACTER, AS_CHARACTER

            for i in range(draw_page.getCount()):
                shape = draw_page.getByIndex(i)
                include_shape = False
                try:
                    page_no = _at_page_anchor_page(shape)
                    if page_no is not None:
                        include_shape = page_no == page
                    else:
                        anchor_type = shape.getPropertyValue("AnchorType")
                        if anchor_type in (AT_PARAGRAPH, AT_CHARACTER, AS_CHARACTER):
                            anchor = shape.getAnchor()
                            if anchor and self._page_at_range(doc, vc, anchor, retry_if_zero=False) == page:
                                include_shape = True
                except Exception:
                    pass

                if include_shape:
                    shape_type = shape.getShapeType().replace("com.sun.star.drawing.", "")
                    shape_info = {"type": shape_type, "name": getattr(shape, "Name", ""), "text": shape.getString().strip() if hasattr(shape, "getString") else ""}
                    try:
                        pos = shape.getPosition()
                        size = shape.getSize()
                        shape_info["geometry"] = {"x": pos.X, "y": pos.Y, "width": size.Width, "height": size.Height}
                    except Exception:
                        pass
                    shapes.append(shape_info)

        return {"images": images, "tables": tables, "frames": frames, "shapes": shapes}


class SectionRead(ToolWriterStructuralBase):
    """Read the content of a named text section."""

    name: str | None = "section_read"
    intent: str | None = "navigate"
    description: str = "Read the text content of a named section. Returns the full text within the section boundaries."
    parameters: dict[str, Any] | None = {"type": "object", "properties": {"section": {"type": "string", "description": "Name of the section to read."}}, "required": ["section"]}
    uno_services: list[str] | None = ["com.sun.star.text.TextDocument"]

    def execute(self, ctx: ToolContext, **kwargs: Any):
        section_name = kwargs.get("section", "")
        if not section_name:
            return self._tool_error("section is required.")

        doc = ctx.doc
        if not hasattr(doc, "getTextSections"):
            return self._tool_error("Document does not support sections.")

        sections = doc.getTextSections()
        if not sections.hasByName(section_name):
            available = list(sections.getElementNames())
            return self._tool_error("Section '%s' not found." % section_name, available=available)

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
        return {"status": "ok", "section": section_name, "section_name": section_name, "paragraphs": paragraphs, "content": content, "length": len(content)}


def _resolve_para_index(ctx: ToolContext, kwargs: dict[str, Any]):
    """Resolve locator or paragraph_index from tool kwargs.

    Returns an integer paragraph index, or None if neither is provided.
    """
    locator = kwargs.get("locator")
    para_index = kwargs.get("paragraph_index")

    if locator is not None and para_index is None:
        doc_svc = ctx.services.document
        resolved = doc_svc.resolve_locator(ctx.doc, locator)
        para_index = resolved.get("para_index")

    return para_index


class CloneHeadingBlock(ToolBaseDummy):
    """Clone an entire heading block (heading + all sub-headings + body)."""

    name: str | None = "clone_heading_block"
    intent: str | None = "edit"
    description: str = "Clone an entire heading block (heading + all sub-headings + body). The clone is inserted right after the original block."
    parameters: dict[str, Any] | None = {"type": "object", "properties": {"locator": {"type": "string", "description": ("Locator of the heading to clone (e.g. 'bookmark:_mcp_abc123', 'heading_text:Introduction').")}, "paragraph_index": {"type": "integer", "description": "Paragraph index of the heading (0-based)."}}}
    uno_services: list[str] | None = ["com.sun.star.text.TextDocument"]
    is_mutation: bool | None = True

    def execute(self, ctx: ToolContext, **kwargs: Any):
        from com.sun.star.text.ControlCharacter import PARAGRAPH_BREAK  # type: ignore

        para_index = _resolve_para_index(ctx, kwargs)
        if para_index is None:
            return self._tool_error("Provide locator or paragraph_index.")

        # Use writer_tree service to find the heading node and block size
        tree_svc = ctx.services.get("writer_tree")
        if tree_svc is None:
            return self._tool_error("writer_nav module not loaded; cannot resolve heading block.")

        tree = tree_svc.build_heading_tree(ctx.doc)
        node = tree_svc._find_node_by_para_index(tree, para_index)
        if node is None:
            return self._tool_error("No heading found at paragraph %d." % para_index)

        # Total paragraphs in the block: heading + body + all children
        total = 1 + tree_svc._count_all_children(node)

        # Collect elements for the block
        doc_text = ctx.doc.getText()
        enum = doc_text.createEnumeration()
        elements = []
        idx = 0
        while enum.hasMoreElements():
            el = enum.nextElement()
            if para_index <= idx < para_index + total:
                elements.append(el)
            if idx >= para_index + total - 1:
                break
            idx += 1

        if not elements:
            return self._tool_error("Could not collect heading block paragraphs.")

        # Insert duplicates after the last element of the block
        last = elements[-1]
        cursor = doc_text.createTextCursorByRange(last)
        cursor.gotoEndOfParagraph(False)

        for el in elements:
            txt = el.getString()
            sty = el.getPropertyValue("ParaStyleName")
            doc_text.insertControlCharacter(cursor, PARAGRAPH_BREAK, False)
            doc_text.insertString(cursor, txt, False)
            cursor.gotoStartOfParagraph(False)
            cursor.gotoEndOfParagraph(True)
            cursor.setPropertyValue("ParaStyleName", sty)
            cursor.gotoEndOfParagraph(False)

        return {"status": "ok", "message": "Cloned heading block '%s' (%d paragraphs)." % (node.get("text", ""), total), "heading_text": node.get("text", ""), "block_size": total}

