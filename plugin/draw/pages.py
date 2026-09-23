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

from typing import Any

from plugin.framework.tool import ToolBase, ToolContext


# Impress Title + Content (title + body). Not "title" (id 0 = title+subtitle).
_DEFAULT_IMPRESS_LAYOUT = "text"


def _is_impress_doc(doc: Any):
    """True for Impress. Draw shares add_slide; PyUNO hasattr is unreliable."""
    try:
        return bool(doc.supportsService("com.sun.star.presentation.PresentationDocument"))
    except Exception:
        return False


class AddSlide(ToolBase):
    name: str | None = "add_slide"
    intent: str | None = "edit"
    description: str = (
        "Inserts a new slide (page) at the specified index. "
        "Impress defaults to the Title + Content ('text') layout. "
        "New slides inherit the deck's assigned master. "
        "Call list_placeholders before set_placeholder_text."
    )
    parameters: dict | None = {
        "type": "object",
        "properties": {
            "page": {"type": "integer", "description": "0-based index where to insert the new slide (defaults to appending at the end if omitted)"},
            "activate": {"type": "boolean", "description": "Whether to switch the view to the new slide (default: true)"},
            "layout": {
                "type": "string",
                "description": (
                    "Impress layout name (default: 'text' = Title + Content). "
                    "Use 'blank' or 'none' for an empty slide. Ignored on Draw documents."
                ),
            },
        },
        "required": [],
    }
    uno_services: list | None = ["com.sun.star.drawing.DrawingDocument", "com.sun.star.presentation.PresentationDocument"]
    is_mutation: bool | None = True

    def execute(self, ctx: ToolContext, **kwargs: Any):
        from plugin.draw.bridge import DrawBridge
        from plugin.draw.transitions import _LAYOUTS, apply_slide_layout, layout_id

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
                return self._tool_error("Unknown layout: %s" % layout_name, available=sorted(_LAYOUTS.keys()))

        # create_slide uses this same index; do not trust get_active_page_index
        # after insert — Impress DrawPage.getNumber() is missing/None, so the
        # bridge helper falls back to 0 even when the controller switched.
        insert_at = bridge.get_pages().getCount() if page_idx is None else page_idx
        new_page = bridge.create_slide(page_idx, switch=switch_view)
        active_idx = insert_at if switch_view else bridge.get_active_page_index()

        result = {"status": "ok", "message": "Slide added", "active_page_index": active_idx}
        # insertNewByIndex can attach factory Default even when the deck already
        # has a designed master (M1′). Copy the neighbor slide's MasterPage.
        if is_impress:
            from plugin.draw.designs import inherit_master_from_neighbor

            inherited = inherit_master_from_neighbor(bridge.get_pages(), new_page, insert_at)
            if inherited:
                result["master"] = inherited
        if is_impress and layout_name is not None:
            result["placeholders_hint"] = "call list_placeholders on this page"
            # insertNewByIndex is already empty (Layout=20, 0 shapes). _LAYOUTS
            # "blank"=11 is a different autolayout that still grows placeholders.
            # Skip assignment so blank/none keep today's empty-page hatch.
            if layout_name in ("blank", "none"):
                result["layout"] = "blank"
            else:
                result["layout"] = apply_slide_layout(new_page, layout_name)
        return result


class DeleteSlide(ToolBase):
    name: str | None = "delete_slide"
    intent: str | None = "edit"
    description: str = "Deletes the slide (page) at the specified index."
    parameters: dict | None = {"type": "object", "properties": {"page": {"type": "integer", "description": "0-based index of slide to delete"}}, "required": ["page"]}
    uno_services: list | None = ["com.sun.star.drawing.DrawingDocument", "com.sun.star.presentation.PresentationDocument"]
    is_mutation: bool | None = True

    def execute(self, ctx: ToolContext, **kwargs: Any):
        from plugin.draw.bridge import DrawBridge

        bridge = DrawBridge(ctx.doc)
        page_idx = kwargs.get("page")
        if page_idx is None:
            return self._tool_error("page is required.")
        bridge.delete_slide(page_idx)
        
        # Resolve active index
        active_idx = bridge.get_active_page_index()
        
        return {"status": "ok", "message": "Slide deleted", "active_page_index": active_idx}


class ListPages(ToolBase):
    name: str | None = "list_pages"
    description: str = "Lists all pages (slides) in the document."
    parameters: dict | None = {"type": "object", "properties": {}, "required": []}
    uno_services: list | None = ["com.sun.star.drawing.DrawingDocument", "com.sun.star.presentation.PresentationDocument"]
    doc_types: list[str] | None = ["draw", "impress"]
    tier: str = "core"

    def execute(self, ctx: ToolContext, **kwargs: Any):
        from plugin.draw.bridge import DrawBridge

        bridge = DrawBridge(ctx.doc)
        pages = bridge.get_pages()
        active_idx = ctx.active_page_index
        if active_idx is None:
            active_idx = bridge.get_active_page_index()
        return {"status": "ok", "pages": [f"Page {i}" for i in range(pages.getCount())], "count": pages.getCount(), "active_page_index": active_idx}


class ReadSlideText(ToolBase):
    """Read all text content from a slide plus speaker notes."""

    name: str | None = "read_slide_text"
    description: str = "Read all text content from a slide (shapes text) and speaker notes. Returns structured text per shape."
    parameters: dict | None = {"type": "object", "properties": {"page": {"type": "integer", "description": "0-based slide index (default: active slide)."}}, "required": []}
    uno_services: list | None = ["com.sun.star.drawing.DrawingDocument", "com.sun.star.presentation.PresentationDocument"]
    tier: str = "core"

    def execute(self, ctx: ToolContext, **kwargs: Any):
        from plugin.draw.bridge import DrawBridge

        bridge = DrawBridge(ctx.doc)
        idx = kwargs.get("page")
        actual_idx = idx if idx is not None else ctx.active_page_index
        if actual_idx is None:
            actual_idx = bridge.get_active_page_index()

        try:
            page = DrawBridge.resolve_slide(ctx.doc, actual_idx)
        except IndexError:
            return self._tool_error("Invalid page index: %s" % actual_idx)
        except Exception:
            return self._tool_error("No draw page available.")

        texts = []
        for i in range(page.getCount()):
            shape = page.getByIndex(i)
            try:
                txt = shape.getString()
                if txt and txt.strip():
                    entry = {"index": i, "text": txt}
                    try:
                        entry["shape_name"] = shape.Name
                    except Exception:
                        pass
                    texts.append(entry)
            except Exception:
                pass

        # Speaker notes
        notes_text = ""
        try:
            notes_page = page.getNotesPage()
            if notes_page and notes_page.getCount() > 1:
                notes_shape = notes_page.getByIndex(1)
                notes_text = notes_shape.getString()
        except Exception:
            pass

        return {"status": "ok", "page": actual_idx, "texts": texts, "notes": notes_text}


class GetPresentationInfo(ToolBase):
    """Get presentation metadata."""

    name: str | None = "get_presentation_info"
    description: str = "Get presentation metadata: slide count, dimensions, master slide names, and whether it is an Impress document."
    parameters: dict | None = {"type": "object", "properties": {}, "required": []}
    uno_services: list | None = ["com.sun.star.drawing.DrawingDocument", "com.sun.star.presentation.PresentationDocument"]
    tier: str = "core"

    def execute(self, ctx: ToolContext, **kwargs: Any):
        doc = ctx.doc
        pages = doc.getDrawPages()
        count = pages.getCount()

        # Dimensions from first page
        width_mm = 0
        height_mm = 0
        if count > 0:
            p = pages.getByIndex(0)
            try:
                width_mm = p.Width // 100
                height_mm = p.Height // 100
            except Exception:
                pass

        # Master pages
        masters = []
        try:
            mp = doc.getMasterPages()
            for i in range(mp.getCount()):
                m = mp.getByIndex(i)
                masters.append(m.Name if hasattr(m, "Name") else "Master_%d" % i)
        except Exception:
            pass

        from plugin.draw.bridge import DrawBridge
        bridge = DrawBridge(doc)
        active_idx = ctx.active_page_index
        if active_idx is None:
            active_idx = bridge.get_active_page_index()
        is_impress = hasattr(doc, "getPresentation")

        return {"status": "ok", "slide_count": count, "width_mm": width_mm, "height_mm": height_mm, "master_slides": masters, "is_impress": is_impress, "active_page_index": active_idx}

class SetActivePage(ToolBase):
    name: str | None = "set_active_page"
    intent: str | None = "navigate"
    description: str = "Changes the currently active slide (page) in Draw/Impress."
    parameters: dict | None = {"type": "object", "properties": {"page": {"type": "integer", "description": "0-based index of page to activate"}}, "required": ["page"]}
    uno_services: list | None = ["com.sun.star.drawing.DrawingDocument", "com.sun.star.presentation.PresentationDocument"]
    is_mutation: bool | None = True

    def execute(self, ctx: ToolContext, **kwargs: Any):
        from plugin.draw.bridge import DrawBridge

        bridge = DrawBridge(ctx.doc)
        pages = bridge.get_pages()
        idx = kwargs.get("page")
        if idx is None:
            return self._tool_error("page is required.")
        if idx < 0 or idx >= pages.getCount():
            return self._tool_error("Page index %d out of range." % idx)

        page = pages.getByIndex(idx)
        controller = ctx.doc.getCurrentController()
        if controller is not None and hasattr(controller, "setCurrentPage"):
            try:
                controller.setCurrentPage(page)
                return {"status": "ok", "message": "Active page changed to %d" % idx, "active_page_index": idx}
            except Exception as e:
                return self._tool_error("Failed to set active page: %s" % e)
        return self._tool_error("Document controller does not support switching pages.")


_DRAW_UNO = ["com.sun.star.drawing.DrawingDocument", "com.sun.star.presentation.PresentationDocument"]


class DuplicateSlide(ToolBase):
    name: str | None = "duplicate_slide"
    intent: str | None = "edit"
    description: str = (
        "Duplicates the slide at the given 0-based index. The copy is inserted immediately after "
        "the source slide."
    )
    parameters: dict | None = {
        "type": "object",
        "properties": {
            "page": {"type": "integer", "description": "0-based index of the slide to duplicate"},
            "activate": {
                "type": "boolean",
                "description": "Whether to switch the view to the new slide (default: true)",
            },
        },
        "required": ["page"],
    }
    uno_services: list | None = _DRAW_UNO
    is_mutation: bool | None = True
    tier: str = "core"

    def execute(self, ctx: ToolContext, **kwargs: Any):
        from plugin.draw.bridge import DrawBridge

        page_idx = kwargs.get("page")
        if page_idx is None:
            return self._tool_error("page is required.")
        bridge = DrawBridge(ctx.doc)
        pages = bridge.get_pages()
        if page_idx < 0 or page_idx >= pages.getCount():
            return self._tool_error("Page index %s out of range." % page_idx)
        activate = kwargs.get("activate", True)
        switch_view = bool(activate if activate is not None else True)
        bridge.duplicate_slide(page_idx, switch=switch_view)
        return {
            "status": "ok",
            "message": "Slide duplicated",
            "source_page": page_idx,
            "active_page_index": bridge.get_active_page_index(),
        }


class MoveSlide(ToolBase):
    name: str | None = "move_slide"
    intent: str | None = "edit"
    description: str = (
        "Moves a slide from from_page to to_page (both 0-based). to_page is the destination index "
        "after removal of the source (insert-at that index)."
    )
    parameters: dict | None = {
        "type": "object",
        "properties": {
            "from_page": {"type": "integer", "description": "0-based source slide index"},
            "to_page": {"type": "integer", "description": "0-based destination slide index"},
        },
        "required": ["from_page", "to_page"],
    }
    uno_services: list | None = _DRAW_UNO
    is_mutation: bool | None = True
    tier: str = "core"

    def execute(self, ctx: ToolContext, **kwargs: Any):
        from plugin.draw.bridge import DrawBridge

        from_page = kwargs.get("from_page")
        to_page = kwargs.get("to_page")
        if from_page is None or to_page is None:
            return self._tool_error("from_page and to_page are required.")
        bridge = DrawBridge(ctx.doc)
        ok = bridge.move_slide(from_page, to_page)
        if not ok:
            return self._tool_error("Could not move slide from %s to %s." % (from_page, to_page))
        return {
            "status": "ok",
            "message": "Slide moved",
            "from_page": from_page,
            "to_page": to_page,
            "active_page_index": bridge.get_active_page_index(),
        }


class RenameSlide(ToolBase):
    name: str | None = "rename_slide"
    intent: str | None = "edit"
    description: str = "Sets the Name property of a slide (0-based page index)."
    parameters: dict | None = {
        "type": "object",
        "properties": {
            "page": {"type": "integer", "description": "0-based slide index"},
            "name": {"type": "string", "description": "New slide name"},
        },
        "required": ["page", "name"],
    }
    uno_services: list | None = _DRAW_UNO
    is_mutation: bool | None = True
    tier: str = "core"

    def execute(self, ctx: ToolContext, **kwargs: Any):
        from plugin.draw.bridge import DrawBridge

        page_idx = kwargs.get("page")
        name = kwargs.get("name")
        if page_idx is None:
            return self._tool_error("page is required.")
        if not name:
            return self._tool_error("name is required.")
        bridge = DrawBridge(ctx.doc)
        pages = bridge.get_pages()
        if page_idx < 0 or page_idx >= pages.getCount():
            return self._tool_error("Page index %s out of range." % page_idx)
        ok = bridge.rename_slide(page_idx, str(name))
        if not ok:
            return self._tool_error("Slide does not support renaming.")
        return {
            "status": "ok",
            "message": "Slide renamed",
            "page": page_idx,
            "name": str(name),
            "active_page_index": bridge.get_active_page_index(),
        }
