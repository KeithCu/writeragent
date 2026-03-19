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

from plugin.framework.tool_base import ToolBase


class AddSlide(ToolBase):
    name = "add_slide"
    intent = "edit"
    description = "Inserts a new slide (page) at the specified index."
    parameters = {
        "type": "object",
        "properties": {
            "index": {
                "type": "integer",
                "description": "Index where to insert (end if omitted)",
            }
        },
        "required": [],
    }
    doc_types = ["draw"]
    is_mutation = True

    def execute(self, ctx, **kwargs):
        from plugin.modules.draw.bridge import DrawBridge
        bridge = DrawBridge(ctx.doc)
        bridge.create_slide(kwargs.get("index"))
        return {"status": "ok", "message": "Slide added"}


class DeleteSlide(ToolBase):
    name = "delete_slide"
    intent = "edit"
    description = "Deletes the slide (page) at the specified index."
    parameters = {
        "type": "object",
        "properties": {
            "index": {
                "type": "integer",
                "description": "Index of slide to delete",
            }
        },
        "required": ["index"],
    }
    doc_types = ["draw"]
    is_mutation = True

    def execute(self, ctx, **kwargs):
        from plugin.modules.draw.bridge import DrawBridge
        bridge = DrawBridge(ctx.doc)
        bridge.delete_slide(kwargs["index"])
        return {"status": "ok", "message": "Slide deleted"}


class ReadSlideText(ToolBase):
    """Read all text content from a slide plus speaker notes."""

    name = "read_slide_text"
    description = (
        "Read all text content from a slide (shapes text) and "
        "speaker notes. Returns structured text per shape."
    )
    parameters = {
        "type": "object",
        "properties": {
            "page_index": {
                "type": "integer",
                "description": "0-based slide index (default: active slide).",
            },
        },
        "required": [],
    }
    doc_types = ["draw"]
    tier = "core"

    def execute(self, ctx, **kwargs):
        from plugin.modules.draw.bridge import DrawBridge
        bridge = DrawBridge(ctx.doc)
        page_index = kwargs.get("page_index")

        if page_index is not None:
            pages = bridge.get_pages()
            if page_index < 0 or page_index >= pages.getCount():
                return self._tool_error("Page index %d out of range." % page_index)
            page = pages.getByIndex(page_index)
        else:
            page = bridge.get_active_page()
            if page is None:
                return self._tool_error("No active page.")

        texts = []
        for i in range(page.getCount()):
            shape = page.getByIndex(i)
            try:
                txt = shape.getString()
                if txt and txt.strip():
                    entry = {"shape_index": i, "text": txt}
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

        return {
            "status": "ok",
            "page_index": page_index,
            "texts": texts,
            "notes": notes_text,
        }


class GetPresentationInfo(ToolBase):
    """Get presentation metadata."""

    name = "get_presentation_info"
    description = (
        "Get presentation metadata: slide count, dimensions, "
        "master slide names, and whether it is an Impress document."
    )
    parameters = {"type": "object", "properties": {}, "required": []}
    doc_types = ["draw"]
    tier = "core"

    def execute(self, ctx, **kwargs):
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

        is_impress = hasattr(doc, "getPresentation")

        return {
            "status": "ok",
            "slide_count": count,
            "width_mm": width_mm,
            "height_mm": height_mm,
            "master_slides": masters,
            "is_impress": is_impress,
        }
