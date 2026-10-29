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
"""Page/slide management tools for Draw/Impress documents."""

import logging
from typing import Any

from plugin.framework.errors import is_real_disposal
from plugin.framework.tool import ToolBase, ToolContext
from plugin.framework.uno_context import uno_same

log = logging.getLogger("writeragent.draw.pages")


# Impress Title + Content (title + body). Not "title" (id 0 = title+subtitle).
_DEFAULT_IMPRESS_LAYOUT = "text"

_DRAW_UNO = ["com.sun.star.drawing.DrawingDocument", "com.sun.star.presentation.PresentationDocument"]


def _is_impress_doc(doc: Any) -> bool:
    """True for Impress. Draw shares add_slide; PyUNO hasattr is unreliable."""
    return bool(doc.supportsService("com.sun.star.presentation.PresentationDocument"))


def inherit_master_from_neighbor(pages: Any, new_page: Any, insert_at: int) -> str:
    """Copy MasterPage from a slide that is not the new page.

    ``insertNewByIndex`` can leave a factory Default master even when the deck
    already has an assigned design master. Prefer the previous slide, then the
    following one, so add_slide keeps the deck look.

    ``add_slide(page=0)`` can pass ``insert_at=0`` while the new
    page actually sits at index 1. The only candidate is then index
    1, which is the new page, so the copy assigns that page's own
    master. ``InsertSdPage`` cannot create a page at index 0, and a
    neighbor chosen by integer index alone is the new page. Skip
    the new page object (``uno_same``), not the integer
    ``insert_at``. A stale ``insert_at`` of 0 is the neighbor when
    the new page actually sits at 1. With placement fixed, index
    0's neighbor is the previous first slide.
    """
    # pages.getCount() does not fail on a live deck. Returning "" hid a closed document.
    count = int(pages.getCount())
    if count < 2:
        return ""

    order: list[int] = []
    if insert_at > 0:
        order.append(insert_at - 1)
    if insert_at + 1 < count:
        order.append(insert_at + 1)
    for i in range(count):
        if i not in order:
            order.append(i)
    for ref_idx in order:
        if ref_idx < 0 or ref_idx >= count:
            continue
        try:
            ref = pages.getByIndex(ref_idx)
            if uno_same(ref, new_page):
                continue
            master = ref.MasterPage
            new_page.MasterPage = master
            return master.Name if hasattr(master, "Name") else ""
        except Exception as exc:
            # Prefer the previous slide, then the following one. Returning
            # here stopped on the first failure. is_real_disposal only:
            # a live-doc RuntimeException must try the next neighbor.
            # is_disposed_exception treats that name as disposal, and
            # add_slide would then delete the new slide.
            if is_real_disposal(exc):
                raise
            log.debug("inherit_master_from_neighbor failed insert_at=%s ref=%s", insert_at, ref_idx, exc_info=True)
            continue
    return ""


class AddSlide(ToolBase):
    name: str | None = "add_slide"
    intent: str | None = "edit"
    description: str = "Inserts a new slide (page) at the specified index. Impress defaults to the Title + Content ('text') layout. New slides inherit the deck's assigned master. Call list_placeholders before set_placeholder_text."
    parameters: dict[str, Any] | None = {
        "type": "object",
        "properties": {
            "page": {"type": "integer", "description": "0-based index where to insert the new slide (defaults to appending at the end if omitted)"},
            "activate": {"type": "boolean", "description": "Whether to switch the view to the new slide (default: true)"},
            "layout": {"type": "string", "description": ("Impress layout name (default: 'text' = Title + Content). Use 'blank' or 'none' for an empty slide. Ignored on Draw documents.")},
        },
        "required": [],
    }
    uno_services: list[str] | None = _DRAW_UNO
    is_mutation: bool | None = True

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        from plugin.draw.bridge import DrawBridge
        from plugin.draw.transitions import apply_slide_layout, available_layout_names, layout_id

        bridge = DrawBridge(ctx.doc)
        page_idx = kwargs.get("page")
        activate = kwargs.get("activate", True)
        switch_view = bool(activate if activate is not None else True)
        is_impress = _is_impress_doc(ctx.doc)

        layout_name = None
        if is_impress:
            raw = kwargs.get("layout")
            if raw is None or (isinstance(raw, str) and not raw.strip()):
                layout_name = _DEFAULT_IMPRESS_LAYOUT
            else:
                layout_name = str(raw).strip().lower()
            # Validate before insert so a bad name does not leave a stray page.
            if layout_id(layout_name) is None:
                return self._tool_error("Unknown layout: %s" % layout_name, available=available_layout_names())

        # Bugfix: create_slide(switch=True) moved the view onto the new page
        # before master inheritance and layout. A failure then removed that page
        # and left the controller on it. insert_slide_from_master already inserts
        # with switch=False and switches only after MasterPage is assigned.
        # Do not trust get_active_page_index after a requested switch — Impress
        # DrawPage.getNumber() is missing/None, so the helper falls back to 0.
        # The landed index is the slot the new page occupies.
        new_page, landed = bridge.create_slide(page_idx, switch=False)

        result: dict[str, Any] = {"status": "ok", "message": "Slide added"}

        try:
            # insertNewByIndex can attach factory Default even when the deck already
            # has a designed master (M1′). Copy the neighbor slide's MasterPage.
            # Pass the landed index: page=0 used to claim insert_at=0 while the
            # new page sat at 1, so the "neighbor" was the new page itself.
            if is_impress:
                inherited = inherit_master_from_neighbor(bridge.get_pages(), new_page, landed)
                if inherited:
                    result["master"] = inherited
            if is_impress and layout_name is not None:
                result["placeholders_hint"] = "call list_placeholders on this page"
                # blank/none are AUTOLAYOUT_NONE (20). insertNewByIndex already
                # leaves that id with 0 shapes; assigning it keeps the page empty.
                # Skipping used to be required because the PowerPoint blank id (11)
                # is AUTOLAYOUT_OBJ and grew a title plus an OLE placeholder.
                result["layout"] = apply_slide_layout(new_page, layout_name)
        except Exception as exc:
            # What was wrong: is_disposed_exception matched bare RuntimeException by name, skipping cleanup on live documents.
            # How it happened: is_disposed_exception checks _has_runtime_teardown_name which matches RuntimeException.
            # Why this change: use is_real_disposal so only real disposal skips cleanup, while live RuntimeException removes new_page.
            if is_real_disposal(exc):
                raise
            cleanup_failed = False
            try:
                bridge.get_pages().remove(new_page)
            except Exception as rm_exc:
                if is_real_disposal(rm_exc):
                    raise
                cleanup_failed = True
                log.debug("add_slide cleanup remove failed", exc_info=True)
            return self._tool_error(
                f"Failed to set layout or master on new slide: {exc}",
                cleanup_failed=cleanup_failed,
            )

        # What was wrong: failed set_current_page_index return was ignored, reporting landed as active_page_index.
        # How it happened: bridge.set_current_page_index return value was not checked.
        # Why this change: report real active page when switch fails and set view_switch_failed flag.
        switched = bridge.set_current_page_index(landed) if switch_view else False
        if switch_view and not switched:
            result["view_switch_failed"] = True
        result["active_page_index"] = landed if switched else bridge.get_active_page_index()
        return result


class _SlideMutationTool(ToolBase):
    uno_services: list[str] | None = _DRAW_UNO
    is_mutation: bool | None = True


def _check_page_range(tool: ToolBase, pages: Any, page_idx: int) -> dict[str, Any] | None:
    if page_idx < 0 or page_idx >= pages.getCount():
        return tool._tool_error("Page index %s out of range." % page_idx)
    return None


class DeleteSlide(_SlideMutationTool):
    name: str | None = "delete_slide"
    intent: str | None = "edit"
    description: str = "Deletes the slide (page) at the specified index."
    parameters: dict[str, Any] | None = {"type": "object", "properties": {"page": {"type": "integer", "description": "0-based index of slide to delete"}}, "required": ["page"]}

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        from plugin.draw.bridge import DrawBridge

        bridge = DrawBridge(ctx.doc)
        page_idx = kwargs["page"]
        assert page_idx is not None
        pages = bridge.get_pages()
        err = _check_page_range(self, pages, page_idx)
        if err is not None:
            return err
        # A one-page Draw or Impress document keeps its only slide. An
        # in-range index is not enough to call bridge.delete_slide.
        # SlideCommandEngine._delete_slide already refuses that case
        # before it touches the document. Return the same error and skip
        # the removal when one page remains.
        if pages.getCount() <= 1:
            return self._tool_error("Cannot delete the only slide")
        bridge.delete_slide(page_idx)

        return {"status": "ok", "message": "Slide deleted", "active_page_index": bridge.get_active_page_index()}


class ListPages(ToolBase):
    name: str | None = "list_pages"
    description: str = "Lists all pages (slides). Each entry is that page's Name, or \"Page N\" when Name is missing or blank. The list position is the 0-based page index."
    parameters: dict[str, Any] | None = {"type": "object", "properties": {}, "required": []}
    uno_services: list[str] | None = _DRAW_UNO
    doc_types: list[str] | None = ["draw", "impress"]
    tier: str = "core"

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        from plugin.draw.bridge import resolve_page_and_bridge

        bridge, active_idx = resolve_page_and_bridge(ctx, {})
        pages = bridge.get_pages()
        labels: list[str] = []
        count = pages.getCount()
        for i in range(count):
            label = f"Page {i}"
            name = pages.getByIndex(i).Name
            if name is not None and str(name).strip():
                label = str(name).strip()
            labels.append(label)
        return {"status": "ok", "pages": labels, "count": count, "active_page_index": active_idx}


class ReadSlideText(ToolBase):
    """Read all text content from a slide plus speaker notes."""

    name: str | None = "read_slide_text"
    description: str = "Read all text content from a slide (shapes text) and speaker notes. Returns structured text per shape."
    parameters: dict[str, Any] | None = {"type": "object", "properties": {"page": {"type": "integer", "description": "0-based slide index (default: active slide)."}}, "required": []}
    uno_services: list[str] | None = _DRAW_UNO
    tier: str = "core"

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        from plugin.draw.bridge import DrawBridge, find_notes_shape, resolve_page_and_bridge
        from plugin.framework.errors import is_disposed_exception, uno_probe

        _bridge, actual_idx = resolve_page_and_bridge(ctx, kwargs)
        page = DrawBridge.get_slide_for_tool(ctx.doc, actual_idx)

        texts = []
        for i in range(page.getCount()):
            shape = page.getByIndex(i)
            try:
                txt = shape.getString()
                if txt and txt.strip():
                    entry = {"index": i, "text": txt, "shape_name": shape.Name}
                    texts.append(entry)
            except Exception as exc:
                if is_disposed_exception(exc):
                    raise

        notes_shape = find_notes_shape(uno_probe(lambda: page.getNotesPage(), None))
        notes_text = ""
        if notes_shape is not None:
            notes_text = uno_probe(lambda: notes_shape.getString() or "", "")

        return {"status": "ok", "page": actual_idx, "texts": texts, "notes": notes_text}


class GetPresentationInfo(ToolBase):
    """Get presentation metadata."""

    name: str | None = "get_presentation_info"
    description: str = "Get presentation metadata: slide count, dimensions, master slide names, and whether it is an Impress document."
    parameters: dict[str, Any] | None = {"type": "object", "properties": {}, "required": []}
    uno_services: list[str] | None = _DRAW_UNO
    tier: str = "core"

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        doc = ctx.doc
        pages = doc.getDrawPages()
        count = pages.getCount()

        # Dimensions from first page
        width_mm = 0
        height_mm = 0
        if count > 0:
            p = pages.getByIndex(0)
            width_mm = p.Width // 100
            height_mm = p.Height // 100

        # Master pages
        masters = []
        mp = doc.getMasterPages()
        for i in range(mp.getCount()):
            m = mp.getByIndex(i)
            masters.append(m.Name if hasattr(m, "Name") else "Master_%d" % i)

        from plugin.draw.bridge import resolve_page_and_bridge

        _bridge, active_idx = resolve_page_and_bridge(ctx, {})
        is_impress = _is_impress_doc(doc)

        return {"status": "ok", "slide_count": count, "width_mm": width_mm, "height_mm": height_mm, "master_slides": masters, "is_impress": is_impress, "active_page_index": active_idx}


class SetActivePage(ToolBase):
    name: str | None = "set_active_page"
    intent: str | None = "navigate"
    description: str = "Changes the currently active slide (page) in Draw/Impress."
    parameters: dict[str, Any] | None = {"type": "object", "properties": {"page": {"type": "integer", "description": "0-based index of page to activate"}}, "required": ["page"]}
    uno_services: list[str] | None = _DRAW_UNO
    # View change only. None would still detect a mutation: the name
    # set_active_page is not a read prefix. read_only_target and the MCP
    # document lock both follow detects_mutation.
    is_mutation: bool | None = False

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        from plugin.draw.bridge import DrawBridge

        bridge = DrawBridge(ctx.doc)
        pages = bridge.get_pages()
        idx = kwargs["page"]
        assert idx is not None
        if idx < 0 or idx >= pages.getCount():
            return self._tool_error("Page index %d out of range." % idx)

        # Bugfix: this reimplemented setCurrentPage and caught every exception,
        # so a disposed controller became a normal tool error instead of
        # DOCUMENT_DISPOSED. set_current_page_index already re-raises disposal
        # and returns False for a missing controller or a non-disposal failure.
        if not bridge.set_current_page_index(idx):
            return self._tool_error("Failed to set active page.")
        return {"status": "ok", "message": "Active page changed to %d" % idx, "active_page_index": idx}


class DuplicateSlide(_SlideMutationTool):
    name: str | None = "duplicate_slide"
    intent: str | None = "edit"
    description: str = "Duplicates the slide at the given 0-based index. The copy is inserted immediately after the source slide."
    parameters: dict[str, Any] | None = {"type": "object", "properties": {"page": {"type": "integer", "description": "0-based index of the slide to duplicate"}, "activate": {"type": "boolean", "description": "Whether to switch the view to the new slide (default: true)"}}, "required": ["page"]}
    tier: str = "core"

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        from plugin.draw.bridge import DrawBridge

        page_idx = kwargs["page"]
        assert page_idx is not None
        bridge = DrawBridge(ctx.doc)
        pages = bridge.get_pages()
        err = _check_page_range(self, pages, page_idx)
        if err is not None:
            return err
        activate = kwargs.get("activate", True)
        switch_view = bool(activate if activate is not None else True)
        bridge.duplicate_slide(page_idx, switch=switch_view)
        # The copy is inserted immediately after the source. Do not re-read
        # getNumber() — Impress leaves it missing and the helper used to
        # report 0 after a successful switch.
        active_idx = page_idx + 1 if switch_view else bridge.get_active_page_index()
        return {"status": "ok", "message": "Slide duplicated", "source_page": page_idx, "active_page_index": active_idx}


class MoveSlide(_SlideMutationTool):
    name: str | None = "move_slide"
    intent: str | None = "edit"
    description: str = "Moves a slide from from_page to to_page (both 0-based). to_page is the destination index after removal of the source (insert-at that index)."
    parameters: dict[str, Any] | None = {"type": "object", "properties": {"from_page": {"type": "integer", "description": "0-based source slide index"}, "to_page": {"type": "integer", "description": "0-based destination slide index"}}, "required": ["from_page", "to_page"]}
    tier: str = "core"

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        from plugin.draw.bridge import DrawBridge, PageExchangeRollbackError

        from_page = kwargs["from_page"]
        to_page = kwargs["to_page"]
        assert from_page is not None and to_page is not None
        bridge = DrawBridge(ctx.doc)
        try:
            ok = bridge.move_slide(from_page, to_page)
        except PageExchangeRollbackError as exc:
            # Bugfix: move_slide now raises when the temp page still holds shapes;
            # execute_safe would prefix that with "Tool execution failed", and the old
            # False path hid it as "Could not move slide". Return the rollback error directly.
            return self._tool_error(str(exc))
        if not ok:
            return self._tool_error("Could not move slide from %s to %s." % (from_page, to_page))
        return {"status": "ok", "message": "Slide moved", "from_page": from_page, "to_page": to_page, "active_page_index": bridge.get_active_page_index()}


class RenameSlide(_SlideMutationTool):
    name: str | None = "rename_slide"
    intent: str | None = "edit"
    description: str = "Sets the Name property of a slide (0-based page index)."
    parameters: dict[str, Any] | None = {"type": "object", "properties": {"page": {"type": "integer", "description": "0-based slide index"}, "name": {"type": "string", "description": "New slide name"}}, "required": ["page", "name"]}
    tier: str = "core"

    def execute(self, ctx: ToolContext, **kwargs: Any) -> dict[str, Any]:
        from plugin.draw.bridge import DrawBridge

        page_idx = kwargs["page"]
        name = kwargs["name"]
        assert page_idx is not None
        if not name:
            return self._tool_error("name is required.")
        bridge = DrawBridge(ctx.doc)
        pages = bridge.get_pages()
        err = _check_page_range(self, pages, page_idx)
        if err is not None:
            return err
        ok = bridge.rename_slide(page_idx, str(name))
        if not ok:
            return self._tool_error("Slide does not support renaming.")
        return {"status": "ok", "message": "Slide renamed", "page": page_idx, "name": str(name), "active_page_index": bridge.get_active_page_index()}
