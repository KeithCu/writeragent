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
"""Structural tools: nav_goto_page, page_get_objects, bookmark_resolve.

(Index refresh, field refresh, section list/read, and bookmark list/cleanup live in specialized domains.)"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from plugin.doc.text_helpers import (
    TrackedDeletions,
    clone_text_range,
    get_string_without_tracked_deletions,
    iter_elements,
    jump_view_cursor_to_page,
    page_not_found_message,
    with_view_cursor_left_body_locked as _with_left_body_locked,
)
from plugin.doc.visual_helpers import safe_get_property
from plugin.draw.tree import custom_shape_geometry
from plugin.framework.errors import ToolExecutionError, best_effort, reraise_if_disposed
from plugin.framework.tool import ToolBase, ToolBaseDummy
from plugin.framework.tool_schema import page_index_param
from plugin.writer.target_resolver import update_selection_pin

from .search import shape_text_boxes
from .specialized_base import ToolWriterStructuralBase

if TYPE_CHECKING:
    from plugin.framework.tool import ToolContext

log = logging.getLogger(__name__)


def _at_page_anchor_page(obj: Any) -> Any | None:
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
    except Exception as exc:
        reraise_if_disposed(exc)
        log.warning("Failed to read AnchorPageNo for object %s: %s", getattr(obj, "Name", type(obj).__name__), exc)
        return None


_PAGE_PROPERTY: dict[str, Any] = page_index_param()


class NavGotoPage(ToolWriterStructuralBase):
    name: str | None = "nav_goto_page"
    is_mutation: bool | None = False
    description: str = "Navigate the view cursor to the start of a page."
    parameters: dict[str, Any] | None = {"type": "object", "properties": {"page": _PAGE_PROPERTY}, "required": ["page"]}
    uno_services: list[str] | None = ["com.sun.star.text.TextDocument"]

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        page = kwargs["page"]
        vc = ctx.doc.getCurrentController().getViewCursor()
        # jump_view_cursor_to_page moves to the last page on a miss; restore so a failed call leaves the caret untouched.
        saved = best_effort(lambda: clone_text_range(vc), "save view cursor before nav_goto_page")
        total = jump_view_cursor_to_page(vc, page)
        if total is not None:
            if saved is not None:
                best_effort(lambda: vc.gotoRange(saved, False), "restore view cursor after failed nav_goto_page")
            return self._tool_error(page_not_found_message(page, total))
        update_selection_pin(ctx, vc)
        return {"status": "ok", "page": page}


class GetPageObjects(ToolBase):
    name: str | None = "page_get_objects"
    description: str = (
        "Get images, tables, frames, and Draw shapes visible on a specific physical page. Provide "
        "page, locator, or paragraph. Shape geometry is in 1/100 mm; x/y are relative to the "
        "anchor (position_relative_to='anchor'); for anchor_type AS_CHARACTER, x is always 0 and y is "
        "the object top's offset from the text baseline (negative = above)."
    )
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {"page": _PAGE_PROPERTY, "locator": {"type": "string", "description": "Locator to determine page"}, "paragraph": {"type": "integer", "description": "Paragraph index to determine page"}},
        "required": [],
    }
    uno_services: list[str] | None = ["com.sun.star.text.TextDocument"]
    tier: str = "core"

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        doc = ctx.doc
        doc_svc = ctx.services.document
        page = kwargs.get("page")

        lo_page = 0
        if page is not None:
            if page < 0:
                return self._tool_error("page must be a non-negative integer (0-based, first page = 0).")
            total = doc_svc.get_page_count(doc)
            if total <= 0:
                return self._tool_error("Cannot resolve page count.")
            if page >= total:
                return self._tool_error(page_not_found_message(page, total))
            lo_page = page + 1
        try:
            vc = doc.getCurrentController().getViewCursor()
        except Exception as exc:
            reraise_if_disposed(exc)
            return self._tool_error("Cannot resolve view cursor page: %s" % exc)

        if page is None:
            locator = kwargs.get("locator")
            para_idx = kwargs.get("paragraph")
            if locator:
                try:
                    para_idx = _resolve_para_index(ctx, locator=locator)
                except (ValueError, ToolExecutionError) as e:
                    return self._tool_error(str(e))
            if para_idx is not None:
                # Page lookup failures surface as tool errors instead of defaulting to
                # the first page, so callers and automation do not act on a false ok response.
                try:
                    lo_page = doc_svc.get_page_for_paragraph(doc, para_idx)
                except (ValueError, ToolExecutionError) as e:
                    return self._tool_error(str(e))
            else:
                try:
                    lo_page = vc.getPage()
                except Exception as exc:
                    reraise_if_disposed(exc)
                    return self._tool_error("Cannot resolve view cursor page: %s" % exc)
            if lo_page <= 0:
                return self._tool_error("Cannot resolve page: invalid page %s" % lo_page)

        objects = _with_left_body_locked(doc, vc, lambda: self._scan_page(doc, vc, lo_page))
        return {"status": "ok", "page": lo_page - 1, **objects}

    def _page_at_range(self, doc: Any, vc: Any, rng: Any, retry_if_zero: bool = False) -> int:
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

    def _content_page(self, doc: Any, vc: Any, obj: Any) -> int:
        """Page of a graphic/frame/table: AT_PAGE via AnchorPageNo, else view hop."""
        page_no = _at_page_anchor_page(obj)
        if page_no is not None:
            return page_no
        return self._page_at_range(doc, vc, obj.getAnchor(), retry_if_zero=True)

    def _scan_page(self, doc: Any, vc: Any, lo_page: Any) -> dict[str, Any]:
        """Scan objects on LibreOffice's 1-based physical *lo_page* (same space as getPage() and AnchorPageNo)."""
        def _scan(container: Any, label: str, build: Any) -> list[dict[str, Any]]:
            items = []
            for name in container.getElementNames():
                try:
                    obj = container.getByName(name)
                    if self._content_page(doc, vc, obj) == lo_page:
                        items.append(build(name, obj))
                except Exception as exc:
                    reraise_if_disposed(exc)
                    log.warning("Failed to scan %s '%s': %s", label, name, exc)
            return items

        images = _scan(
            doc.getGraphicObjects(),
            "graphic object",
            lambda name, g: {
                "name": name,
                "width_mm": g.getPropertyValue("Size").Width // 100,
                "height_mm": g.getPropertyValue("Size").Height // 100,
                "title": g.getPropertyValue("Title"),
            },
        )
        tables = _scan(
            doc.getTextTables(),
            "text table",
            lambda name, t: {
                "name": name,
                "rows": t.getRows().getCount(),
                "cols": t.getColumns().getCount(),
            },
        )

        def frame_entry(name: str, frame: Any) -> dict[str, Any]:
            size = frame.getPropertyValue("Size")
            return {"name": name, "width_mm": size.Width // 100, "height_mm": size.Height // 100}

        frames = _scan(doc.getTextFrames(), "text frame", frame_entry)

        # Do not jumpToEndOfPage + body createTextCursorByRange: end-of-page often sits in a
        # table/frame, and the body XText then raises RuntimeException ("End of content node
        # doesn't have the proper start node"). Same view-cursor page check as tables/images.
        # AT_PAGE shapes use AnchorPageNo — never _page_at_range (no view hop).
        shapes = []
        draw_page = doc.getDrawPage()
        from com.sun.star.text.TextContentAnchorType import AT_PARAGRAPH, AT_CHARACTER, AS_CHARACTER

        for i in range(draw_page.getCount()):
            shape = draw_page.getByIndex(i)
            shape_name = getattr(shape, "Name", "") or ("shape_%d" % i)
            include_shape = False
            try:
                page_no = _at_page_anchor_page(shape)
                if page_no is not None:
                    include_shape = page_no == lo_page
                else:
                    anchor_type = shape.getPropertyValue("AnchorType")
                    if anchor_type in (AT_PARAGRAPH, AT_CHARACTER, AS_CHARACTER):
                        anchor = shape.getAnchor()
                        if anchor and self._page_at_range(doc, vc, anchor, retry_if_zero=False) == lo_page:
                            include_shape = True
            except Exception as exc:
                reraise_if_disposed(exc)
                log.warning("Failed to scan draw shape '%s': %s", shape_name, exc)

            if include_shape:
                shape_type = shape.getShapeType().replace("com.sun.star.drawing.", "")
                shape_info = {"type": shape_type, "name": getattr(shape, "Name", ""), "text": get_string_without_tracked_deletions(shape).strip() if hasattr(shape, "getString") else ""}
                preset = custom_shape_geometry(shape).get("Type")
                if preset:
                    shape_info["custom_shape_type"] = preset
                shape_info["geometry"] = None
                try:
                    size = shape.getSize()
                    geometry: dict[str, Any] = {"width": size.Width, "height": size.Height}
                    hori = safe_get_property(shape, "HoriOrientPosition")
                    vert = safe_get_property(shape, "VertOrientPosition")
                    if hori is not None and vert is not None:
                        geometry.update({"x": hori, "y": vert, "position_relative_to": "anchor"})
                        anchor_kind = safe_get_property(shape, "AnchorType")
                        if anchor_kind is not None:
                            geometry["anchor_type"] = getattr(anchor_kind, "value", str(anchor_kind))
                    else:
                        pos = shape.getPosition()
                        geometry.update({"x": pos.X, "y": pos.Y, "position_relative_to": "page"})
                    shape_info["geometry"] = geometry
                except Exception as exc:
                    reraise_if_disposed(exc)
                    log.warning("Failed to read geometry for draw shape '%s': %s", shape_name, exc)
                shapes.append(shape_info)
                # getTextFrames().getElementNames() omits the hidden frame of a Word text box, so it is
                # listed here (including boxes inside groups), on the page of the shape that owns it.
                # A failed read skips the rest of this shape's boxes.
                try:
                    for address, box_shape, box in shape_text_boxes(i, shape):
                        frames.append({**frame_entry(box.getName(), box), "shape_index": address, "shape_name": box_shape.getName()})
                except Exception as exc:
                    reraise_if_disposed(exc)
                    log.warning("Failed to read text box frame of draw shape '%s': %s", shape_name, exc)

        return {"images": images, "tables": tables, "frames": frames, "shapes": shapes}


def _resolve_para_index(
    ctx: ToolContext,
    locator: str | None = None,
    paragraph_index: int | None = None,
) -> int | None:
    """Resolve locator or paragraph_index.

    Returns an integer paragraph index, or None if neither is provided.
    """
    if locator is not None and paragraph_index is None:
        return ctx.services.document.resolve_locator(ctx.doc, locator)["para_index"]

    return paragraph_index


class CloneHeadingBlock(ToolBaseDummy):
    """Clone an entire heading block (heading + all sub-headings + body)."""

    name: str | None = "clone_heading_block"
    description: str = (
        "Clone an entire heading block (heading + all sub-headings + body). Plain paragraph text and "
        "paragraph style are copied; character formatting, lists, fields, hyperlinks, bookmarks, and "
        "inline objects are not copied; tables are skipped. The clone is inserted right after the original block."
    )
    parameters: dict[str, Any] | None = {"type": "object", "properties": {"locator": {"type": "string", "description": ("Locator of the heading to clone (e.g. 'bookmark:_mcp_abc123', 'heading_text:Introduction').")}, "paragraph_index": {"type": "integer", "description": "Paragraph index of the heading (0-based)."}}}
    uno_services: list[str] | None = ["com.sun.star.text.TextDocument"]
    is_mutation: bool | None = True

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        try:
            para_index = _resolve_para_index(
                ctx,
                locator=kwargs.get("locator"),
                paragraph_index=kwargs.get("paragraph_index"),
            )
        except ToolExecutionError as exc:
            return self._tool_error(str(exc))
        if para_index is None:
            return self._tool_error("Provide locator or paragraph_index.")

        # Use writer_tree service to find the heading node and block size.
        tree_svc = getattr(ctx.services, "writer_tree", None)
        if tree_svc is None:
            return self._tool_error("Writer tree service unavailable.", code="SERVICE_UNAVAILABLE")

        from com.sun.star.text.ControlCharacter import PARAGRAPH_BREAK  # type: ignore

        tree = tree_svc.build_heading_tree(ctx.doc)
        node = tree_svc.find_heading_by_para_index(tree, para_index)
        if node is None:
            return self._tool_error("No heading found at paragraph %d." % para_index)

        # Total paragraphs in the block: heading + body + all children
        total = 1 + tree_svc.heading_block_body_count(node)

        # Collect elements for the block
        doc_text = ctx.doc.getText()
        enum = doc_text.createEnumeration()
        elements = []
        idx = 0
        for el in iter_elements(enum):
            if para_index <= idx < para_index + total:
                elements.append(el)
            if idx >= para_index + total - 1:
                break
            idx += 1

        if not elements:
            return self._tool_error("Could not collect heading block paragraphs.")

        def _is_text_table(el: Any) -> bool:
            return hasattr(el, "supportsService") and el.supportsService("com.sun.star.text.TextTable") is True

        # Insert duplicates after the last element of the block.
        # Snapshot text and style first. The cursor is created from the last
        # block paragraph, and insertControlCharacter(PARAGRAPH_BREAK) at that
        # paragraph's end retargets its UNO range onto the new paragraph.
        # Re-reading the live range on the next iteration therefore clones the
        # paragraph just inserted (the heading) instead of the source body, so
        # the last paragraph is "My Heading" and the tracked-deletion-stripped
        # body never gets written. get_string_without_tracked_deletions has to
        # run before that split so the body clone is the visible text.
        clones: list[tuple[str, Any]] = []
        skipped_tables = 0
        state = TrackedDeletions(ctx.doc).state_at(elements[0])
        for el in elements:
            # A table is in this enumeration and has no ParaStyleName
            # (same fact as html_export._copy_window_to_scratch); do not add a table copier.
            if _is_text_table(el):
                skipped_tables += 1
                continue
            clones.append((
                get_string_without_tracked_deletions(el, state),
                el.getPropertyValue("ParaStyleName"),
            ))

        last = elements[-1]
        if _is_text_table(last):
            cursor = doc_text.createTextCursorByRange(last.getAnchor())
            cursor.collapseToEnd()
        else:
            cursor = doc_text.createTextCursorByRange(last)
            cursor.gotoEndOfParagraph(False)

        for txt, sty in clones:
            doc_text.insertControlCharacter(cursor, PARAGRAPH_BREAK, False)
            # This cursor is already in the new paragraph after the break
            # (insertString fills it). gotoNextParagraph would skip that empty
            # paragraph and write into whatever follows the block. html_export's
            # temp-doc cursor stays before the break and must step forward;
            # this one must not. Collapse to the end after styling so the next
            # break is after the clone, not inside it.
            doc_text.insertString(cursor, txt, False)
            cursor.gotoStartOfParagraph(False)
            cursor.gotoEndOfParagraph(True)
            cursor.setPropertyValue("ParaStyleName", sty)
            cursor.gotoEndOfParagraph(False)

        suffix = (", %d tables skipped" % skipped_tables) if skipped_tables else ""
        msg = "Cloned heading block '%s' (%d paragraphs%s)." % (node.get("text", ""), total, suffix)

        return {"status": "ok", "message": msg, "heading_text": node.get("text", ""), "block_size": total}

