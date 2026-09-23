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
"""In-process UNO bridge for LibreOffice Draw."""

from __future__ import annotations

import logging
from typing import Any

from plugin.doc import text_helpers as _text_helpers
from plugin.framework.errors import ToolExecutionError, UnoObjectError, check_disposed, is_disposed_exception, safe_call
from plugin.framework.thread_guard import main_thread_only

log = logging.getLogger(__name__)


class _SingleDrawPageContainer:
    """Writer/Calc expose one ``XDrawPage``, not ``XDrawPages``. Shape tools still use getCount/getByIndex."""

    _page: Any

    def __init__(self, page: Any) -> None:
        self._page = page

    def getCount(self) -> int:
        return 1

    def getByIndex(self, index: int) -> Any:
        if index != 0:
            raise IndexError("Page index %s out of range." % index)
        return self._page


def _draw_page_container(doc: Any) -> Any | None:
    """Return an ``XDrawPages``-like object, or None if *doc* has no draw page."""
    if hasattr(doc, "getDrawPages"):
        return doc.getDrawPages()
    if hasattr(doc, "getDrawPage"):
        try:
            page = doc.getDrawPage()
        except Exception:
            page = None
        if page is not None:
            return _SingleDrawPageContainer(page)
    if hasattr(doc, "getSheets"):
        try:
            from plugin.calc.bridge import CalcBridge

            sheet = CalcBridge(doc).get_active_sheet()
            page = sheet.getDrawPage() if sheet is not None and hasattr(sheet, "getDrawPage") else None
        except Exception:
            page = None
        if page is not None:
            return _SingleDrawPageContainer(page)
    return None


class DrawBridge:
    doc: Any

    def __init__(self, doc: Any) -> None:
        self.doc = doc
        pages = _draw_page_container(doc)
        if pages is None:
            raise RuntimeError("Provided document has no draw page (Draw/Impress, Writer, or Calc).")
        self._pages: Any = pages

    def get_pages(self) -> Any:
        return self._pages

    def get_active_page(self) -> Any | None:
        controller = self.doc.getCurrentController()
        if controller is not None and hasattr(controller, "getCurrentPage"):
            page = controller.getCurrentPage()
            if page is not None:
                return page
        # Hidden / headless docs often have no current page; use first slide.
        pages = self.get_pages()
        if pages.getCount() > 0:
            return pages.getByIndex(0)
        return None

    @classmethod
    def resolve_slide(cls, doc: Any, page_index: int | None = None) -> Any:
        """Resolve a slide (XDrawPage) by index or active slide."""
        bridge = cls(doc)
        if page_index is not None:
            pages = bridge.get_pages()
            if page_index < 0 or page_index >= pages.getCount():
                raise IndexError(f"Page index {page_index} out of range.")
            return pages.getByIndex(page_index)
        page = bridge.get_active_page()
        if page is None:
            raise RuntimeError("No draw page available.")
        return page

    @classmethod
    def get_slide_for_tool(cls, doc: Any, page_index: int | None = None) -> Any:
        """Resolve a slide for tool ``execute()``.

        Re-raises UNO dispose so ``execute_safe`` maps it to ``DOCUMENT_DISPOSED``.
        The four Draw/Impress tool modules used to catch ``Exception`` and raise
        ``ToolExecutionError(str(e))``, which stripped dispose identity. The native
        test runner then treated a dead URP as a normal tool failure instead of
        aborting the remaining suite.

        ``IndexError`` stays a ``ToolExecutionError`` (page out of range). Other
        failures are wrapped the same way so callers still get a tool error, not
        a raw UNO exception.
        """
        try:
            return cls.resolve_slide(doc, page_index)
        except IndexError:
            # resolve_slide only raises IndexError when page_index is an int.
            raise ToolExecutionError("Page index %s out of range." % page_index)
        except Exception as e:
            if is_disposed_exception(e):
                raise
            raise ToolExecutionError(str(e)) from e

    def create_shape(self, shape_type: str, x: int, y: int, width: int, height: int, page: Any | None = None) -> Any:
        """
        Creates a shape of specified type and adds it to the page.
        shape_type: e.g. "com.sun.star.drawing.RectangleShape"
        """
        if page is None:
            page = self.get_active_page()
        if page is None:
            raise RuntimeError("No draw page available to create shape.")

        shape = self.doc.createInstance(shape_type)
        page.add(shape)

        # Set size and position
        from com.sun.star.awt import Size, Point

        shape.setSize(Size(width, height))
        shape.setPosition(Point(x, y))
        return shape

    def get_shapes(self, page: Any | None = None) -> list[Any]:
        if page is None:
            page = self.get_active_page()
        if page is None:
            raise RuntimeError("No draw page available to list shapes.")
        shapes = []
        for i in range(page.getCount()):
            shapes.append(page.getByIndex(i))
        return shapes

    def create_slide(self, index: int | None = None, switch: bool = True) -> Any:
        """Creates a new slide (page) at the specified index."""
        pages = self.get_pages()
        if index is None:
            index = pages.getCount()
        new_page = pages.insertNewByIndex(index)
        
        if switch:
            controller = self.doc.getCurrentController()
            if controller is not None and hasattr(controller, "setCurrentPage"):
                try:
                    controller.setCurrentPage(new_page)
                except Exception as exc:
                    log.debug("setCurrentPage after insert failed: %s", exc)
        return new_page

    def delete_slide(self, index: int) -> None:
        """Deletes the slide at the specified index."""
        pages = self.get_pages()
        page = pages.getByIndex(index)
        pages.remove(page)

    def duplicate_slide(self, index: int, switch: bool = True) -> Any:
        """Duplicate the slide via UNO ``XDrawPageDuplicator.duplicate`` (full shape copy)."""
        pages = self.get_pages()
        source = pages.getByIndex(index)
        # DrawingDocument / PresentationDocument implement XDrawPageDuplicator.
        new_page = self.doc.duplicate(source)
        if switch:
            self.set_current_page_index(index + 1)
        return new_page

    def insert_slide_from_master(
        self,
        master_index: int | None = None,
        master_name: str | None = None,
        after_index: int | None = None,
        switch: bool = True,
    ) -> tuple[Any, int]:
        """Insert a slide after after_index (default: active), assign master, jump to new slide."""
        pages = self.get_pages()
        if after_index is None:
            after_index = self.get_active_page_index()
        insert_at = min(after_index + 1, pages.getCount())
        new_page = pages.insertNewByIndex(insert_at)
        master = self._resolve_master(master_index=master_index, master_name=master_name)
        if master is not None:
            try:
                new_page.MasterPage = master
            except Exception as exc:
                log.debug("insert_slide_from_master MasterPage: %s", exc)
        if switch:
            self.set_current_page_index(insert_at)
        return new_page, insert_at

    def _resolve_master(self, master_index: int | None = None, master_name: str | None = None) -> Any | None:
        if not hasattr(self.doc, "getMasterPages"):
            return None
        masters = self.doc.getMasterPages()
        if master_name is not None:
            for i in range(masters.getCount()):
                m = masters.getByIndex(i)
                if hasattr(m, "Name") and m.Name == master_name:
                    return m
            return None
        if master_index is not None:
            if 0 <= master_index < masters.getCount():
                return masters.getByIndex(master_index)
        return None

    def move_slide(self, from_index: int, to_index: int) -> bool:
        """Move slide from_index to to_index."""
        if from_index == to_index:
            return True
        pages = self.get_pages()
        count = pages.getCount()
        if from_index < 0 or from_index >= count or to_index < 0 or to_index >= count:
            return False
        page = pages.getByIndex(from_index)
        pages.remove(page)
        try:
            pages.insertByIndex(to_index, page)
        except Exception:
            # Some builds only expose insertNewByIndex; re-append at end as fallback.
            try:
                pages.insertNewByIndex(min(to_index, pages.getCount()))
            except Exception as exc:
                log.debug("move_slide insert failed: %s", exc)
                return False
        return True

    def rename_slide(self, index: int, name: str) -> bool:
        page = self.get_pages().getByIndex(index)
        if hasattr(page, "Name"):
            page.Name = name
            return True
        return False

    def set_current_page_index(self, index: int) -> bool:
        pages = self.get_pages()
        if index < 0 or index >= pages.getCount():
            return False
        page = pages.getByIndex(index)
        controller = self.doc.getCurrentController()
        if controller is not None and hasattr(controller, "setCurrentPage"):
            try:
                controller.setCurrentPage(page)
                return True
            except Exception as exc:
                log.debug("set_current_page_index failed: %s", exc)
        return False

    def get_active_page_index(self) -> int:
        try:
            page = self.get_active_page()
            if page:
                # In Draw, getNumber() - 1 is often the index.
                if hasattr(page, "getNumber"):
                    try:
                        return page.getNumber() - 1
                    except Exception:
                        pass
                
                # Fallback: compare pages by UNO identity (PyUNO wrappers can differ).
                from plugin.framework.uno_context import uno_same

                pages = self.get_pages()
                count = pages.getCount()
                for i in range(count):
                    if uno_same(pages.getByIndex(i), page):
                        return i
        except Exception:
            log.debug("get_active_page_index failed", exc_info=True)
        return 0


@main_thread_only
def get_draw_context_for_chat(model: Any, max_context: int = 8000, ctx: Any | None = None) -> str:
    """Get context summary for a Draw/Impress document. ctx: unused, kept for signature compat."""
    try:
        check_disposed(model, "Document Model")
        bridge = DrawBridge(model)
        pages = bridge.get_pages()
        active_page = bridge.get_active_page()

        is_impress = safe_call(model.supportsService, "Check supportsService", "com.sun.star.presentation.PresentationDocument")
        doc_type = "Impress Presentation" if is_impress else "Draw Document"

        ctx_str = "%s: %s\n" % (doc_type, safe_call(model.getURL, "Get document URL") or "Untitled")
        ctx_str += "Total %s: %d\n" % ("Slides" if is_impress else "Pages", safe_call(pages.getCount, "Get page count"))

        # Get index of active page
        active_page_idx = -1
        for i in range(safe_call(pages.getCount, "Get page count")):
            if safe_call(pages.getByIndex, "Get page by index", i) == active_page:
                active_page_idx = i
                break

        ctx_str += "Active %s Index: %d\n" % ("Slide" if is_impress else "Page", active_page_idx)

        # Summarize shapes on active page
        if active_page:
            shapes = bridge.get_shapes(active_page)
            ctx_str += "\nShapes on %s %d:\n" % ("Slide" if is_impress else "Page", active_page_idx)
            for i, s in enumerate(shapes):
                type_name = safe_call(s.getShapeType, "Get shape type").split(".")[-1]
                pos = safe_call(s.getPosition, "Get position")
                size = safe_call(s.getSize, "Get size")
                ctx_str += "- [%d] %s: pos(%d, %d) size(%dx%d)" % (i, type_name, pos.X, pos.Y, size.Width, size.Height)
                if hasattr(s, "getString"):
                    text = _text_helpers.normalize_linebreaks(safe_call(s.getString, "Get string"))
                    if text:
                        ctx_str += ' text: "%s"' % text[:200]
                ctx_str += "\n"

            # Impress-specific: Speaker Notes
            if is_impress and hasattr(active_page, "getNotesPage"):
                try:
                    notes_page = safe_call(active_page.getNotesPage, "Get notes page")
                    notes_text = ""
                    for i in range(safe_call(notes_page.getCount, "Get notes page count")):
                        shape = safe_call(notes_page.getByIndex, "Get notes shape by index", i)
                        if safe_call(shape.getShapeType, "Get notes shape type") == "com.sun.star.presentation.NotesShape":
                            notes_text += safe_call(shape.getString, "Get notes shape string") + "\n"
                    if notes_text.strip():
                        ctx_str += "\nSpeaker Notes:\n%s\n" % notes_text.strip()
                except UnoObjectError:
                    pass

        return ctx_str
    except UnoObjectError:
        log.exception("get_draw_context_for_chat error")
        return "[Unable to read Draw/Impress context. The document may be locked or initializing.]"
    except Exception:
        log.exception("get_draw_context_for_chat exception")
        return "[Unable to read Draw/Impress context. The document may be locked or initializing.]"
