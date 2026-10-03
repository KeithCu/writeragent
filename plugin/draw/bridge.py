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

    def insert_slide_from_master(self, master_index: int | None = None, master_name: str | None = None, after_index: int | None = None, switch: bool = True) -> tuple[Any, int]:
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
        """Move the page at from_index so it occupies to_index.

        What was wrong: after ``XDrawPages.remove`` disposed the source
        (``XDrawPages.idl`` has no way to put that page back), the replacement
        blank page only received a shallow shape clone — position, size, a
        few properties, and text. ``GroupShape``, graphics, connectors, and
        charts were empty or skipped (no ``ShapeType`` returned without
        copying). A move to index 0 then called ``_exchange_page_contents``
        after the source was already gone, so a failed exchange returned
        False with the deck already reordered.
        How it happened: ``insertNewByIndex`` only creates a blank page, and
        ``InsertSdPage`` (``sd/source/ui/unoidl/unomodel.cxx``) inserts after
        ``min(count-1, nIndex)``, so it cannot create a page at index 0.
        Why this fixes it: ``XDrawPageDuplicator.duplicate``
        (``SdXImpressDocument::duplicate``) clones the whole page, including
        shapes and notes, and inserts that clone after the source. Removing
        the source leaves the clone in the source slot. Neighboring slots
        then swap by moving those cloned shapes (not by constructing new
        ones) plus page name, layout, master, notes, and transition. A failed
        swap puts the shapes back, so the clone is still in the original
        slot and the other slides are unchanged.
        """
        pages = self.get_pages()
        count = pages.getCount()
        if from_index < 0 or from_index >= count or to_index < 0 or to_index >= count:
            return False
        if from_index == to_index:
            return True
        source = pages.getByIndex(from_index)
        try:
            copy = self.doc.duplicate(source)
        except Exception as exc:
            if is_disposed_exception(exc):
                raise
            log.debug("move_slide duplicate failed: %s", exc)
            return False
        if copy is None:
            return False
        try:
            self._take_page_name(source, copy)
        except Exception as exc:
            if is_disposed_exception(exc):
                raise
            log.debug("move_slide name transfer failed: %s", exc)
            self._remove_page_quietly(pages, copy)
            return False
        try:
            pages.remove(source)
        except Exception as exc:
            if is_disposed_exception(exc):
                raise
            log.debug("move_slide remove source failed: %s", exc)
            try:
                self._take_page_name(copy, source)
            except Exception as restore_exc:
                if is_disposed_exception(restore_exc):
                    raise
                log.debug("move_slide restore source name failed: %s", restore_exc)
            self._remove_page_quietly(pages, copy)
            return False
        # The clone now occupies from_index. Bubble it to to_index.
        try:
            self._bubble_page(pages, from_index, to_index)
        except Exception as exc:
            if is_disposed_exception(exc):
                raise
            log.debug("move_slide reorder failed: %s", exc)
            return False
        return True

    def _bubble_page(self, pages: Any, start: int, dest: int) -> None:
        """Swap *start* with its neighbor until that slot's content is at *dest*.

        A failed step reverses the swaps already finished, so the clone stays
        where the source page was.
        """
        step = 1 if dest > start else -1
        index = start
        swapped: list[int] = []
        try:
            while index != dest:
                neighbor = index + step
                self._exchange_page_contents(pages.getByIndex(index), pages.getByIndex(neighbor))
                swapped.append(index)
                index = neighbor
        except Exception:
            for prev in reversed(swapped):
                try:
                    self._exchange_page_contents(pages.getByIndex(prev), pages.getByIndex(prev + step))
                except Exception:
                    log.exception("move_slide undo reorder failed")
            raise

    def _exchange_page_contents(self, first: Any, second: Any) -> None:
        """Swap two pages by moving their shapes and page metadata.

        The temporary page is only a holding area. On Impress,
        ``insertNewByIndex`` gives that page its own placeholders
        (``apply_slide_layout``); those must stay on it and die with it.
        Moving every shape off the temp page would drag them into the deck.
        """
        pages = self.get_pages()
        first_shapes = self._snapshot_shapes(first)
        second_shapes = self._snapshot_shapes(second)
        temp = None
        moved_first = False
        moved_second = False
        moved_onto_second = False
        try:
            temp = pages.insertNewByIndex(pages.getCount() - 1)
            if temp is None:
                raise RuntimeError("move_slide could not hold shapes during reorder")
            self._move_shapes(first_shapes, first, temp)
            moved_first = True
            self._move_shapes(second_shapes, second, first)
            moved_second = True
            self._move_shapes(first_shapes, temp, second)
            moved_onto_second = True
            self._swap_page_meta(first, second)
        except Exception:
            self._rollback_exchange(first, second, temp, first_shapes, second_shapes, moved_first, moved_second, moved_onto_second)
            raise
        finally:
            if temp is not None:
                self._remove_page_quietly(pages, temp)

    def _rollback_exchange(self, first: Any, second: Any, temp: Any, first_shapes: list[Any], second_shapes: list[Any], moved_first: bool, moved_second: bool, moved_onto_second: bool) -> None:
        """Put shapes back on *first* and *second*. Metadata undo is inside the swap."""
        if temp is None:
            return
        try:
            if moved_onto_second:
                self._move_shapes(first_shapes, second, temp)
                self._move_shapes(second_shapes, first, second)
                self._move_shapes(first_shapes, temp, first)
            elif moved_second:
                self._move_shapes(second_shapes, first, second)
                self._move_shapes(first_shapes, temp, first)
            elif moved_first:
                self._move_shapes(first_shapes, temp, first)
        except Exception:
            log.exception("move_slide restore exchanged pages failed")

    def _snapshot_shapes(self, page: Any) -> list[Any]:
        try:
            count = int(page.getCount())
        except Exception as exc:
            if is_disposed_exception(exc):
                raise
            return []
        return [page.getByIndex(i) for i in range(count)]

    def _move_shapes(self, shapes: list[Any], src: Any, dest: Any) -> None:
        """Move *shapes* from *src* to *dest* in order. ``add`` of an inserted shape does not move it."""
        moved: list[Any] = []
        try:
            for shape in shapes:
                src.remove(shape)
                try:
                    dest.add(shape)
                except Exception as exc:
                    if is_disposed_exception(exc):
                        raise
                    try:
                        src.add(shape)
                    except Exception as restore_exc:
                        if is_disposed_exception(restore_exc):
                            raise
                        log.debug("move_slide return shape to source failed", exc_info=True)
                    raise
                moved.append(shape)
        except Exception:
            for shape in moved:
                try:
                    dest.remove(shape)
                    src.add(shape)
                except Exception as restore_exc:
                    if is_disposed_exception(restore_exc):
                        raise
                    log.debug("move_slide undo shape move failed", exc_info=True)
            raise

    def _swap_page_meta(self, first: Any, second: Any) -> None:
        """Swap name, layout, master, notes, and transition. Skip props the page does not have."""
        self._swap_page_names(first, second)
        try:
            self._swap_page_props(first, second)
        except Exception:
            self._restore_swapped_names(first, second)
            raise
        try:
            self._swap_notes(first, second)
        except Exception:
            try:
                self._swap_page_props(first, second)
            except Exception:
                log.exception("move_slide undo page props failed")
            self._restore_swapped_names(first, second)
            raise

    def _restore_swapped_names(self, first: Any, second: Any) -> None:
        try:
            self._swap_page_names(first, second)
        except Exception:
            log.exception("move_slide restore page names failed")

    def _swap_page_names(self, first: Any, second: Any) -> None:
        try:
            name_first = str(first.Name or "")
            name_second = str(second.Name or "")
        except Exception as exc:
            if is_disposed_exception(exc):
                raise
            return
        if name_first == name_second:
            return
        # Two pages cannot share a name. Park both, then assign.
        parked_first = name_first + "\u200b"
        parked_second = name_second + "\u200b\u200b"
        try:
            first.Name = parked_first
            second.Name = parked_second
            first.Name = name_second
            second.Name = name_first
        except Exception as exc:
            if is_disposed_exception(exc):
                raise
            try:
                first.Name = name_first
                second.Name = name_second
            except Exception as restore_exc:
                if is_disposed_exception(restore_exc):
                    raise
                log.debug("move_slide restore names failed", exc_info=True)
            raise

    def _swap_page_props(self, first: Any, second: Any) -> None:
        # Background is an SfxItem from the source pool; copying it aborts
        # soffice. Layout and MasterPage are references this document owns.
        # Effect/Speed/Duration/Change/TransitionDuration are the transition
        # fields SetSlideTransition writes; the duplicate already has them.
        props = ("Layout", "MasterPage", "Effect", "Speed", "Duration", "Change", "TransitionDuration")
        applied: list[str] = []
        try:
            for prop in props:
                if self._swap_one_prop(first, second, prop):
                    applied.append(prop)
        except Exception:
            for prop in reversed(applied):
                try:
                    self._swap_one_prop(first, second, prop)
                except Exception:
                    log.exception("move_slide undo page prop %s failed", prop)
            raise

    def _swap_one_prop(self, first: Any, second: Any, prop: str) -> bool:
        """Swap *prop*. Return False when either page does not have it."""
        ok_first, value_first = self._read_page_prop(first, prop)
        ok_second, value_second = self._read_page_prop(second, prop)
        if not ok_first or not ok_second:
            return False
        try:
            unchanged = value_first == value_second
        except Exception:
            unchanged = False
        if unchanged:
            return False
        self._write_page_prop(first, prop, value_second)
        try:
            self._write_page_prop(second, prop, value_first)
        except Exception:
            try:
                self._write_page_prop(first, prop, value_first)
            except Exception as restore_exc:
                if is_disposed_exception(restore_exc):
                    raise
                log.debug("move_slide restore page prop %s failed", prop, exc_info=True)
            raise
        return True

    def _read_page_prop(self, page: Any, prop: str) -> tuple[bool, Any]:
        try:
            if hasattr(page, "getPropertyValue"):
                return True, page.getPropertyValue(prop)
            if hasattr(page, prop):
                return True, getattr(page, prop)
        except Exception as exc:
            if is_disposed_exception(exc):
                raise
            return False, None
        return False, None

    def _write_page_prop(self, page: Any, prop: str, value: Any) -> None:
        # Assigning Layout instantiates placeholders (apply_slide_layout).
        # The slide's real placeholders already moved with the shapes; drop
        # only the ones this write just inserted.
        before = self._snapshot_shapes(page) if prop == "Layout" else None
        if hasattr(page, "setPropertyValue"):
            page.setPropertyValue(prop, value)
        else:
            setattr(page, prop, value)
        if before is not None:
            self._drop_shapes_added_by_prop(page, before)

    def _drop_shapes_added_by_prop(self, page: Any, before: list[Any]) -> None:
        from plugin.framework.uno_context import uno_same

        guard = 0
        try:
            count = int(page.getCount())
        except Exception as exc:
            if is_disposed_exception(exc):
                raise
            return
        while count > len(before) and guard < count + 5:
            guard += 1
            removed = False
            for index in range(count - 1, -1, -1):
                shape = page.getByIndex(index)
                if any(uno_same(shape, old) for old in before):
                    continue
                try:
                    page.remove(shape)
                except Exception as exc:
                    if is_disposed_exception(exc):
                        raise
                    return
                removed = True
                break
            if not removed:
                return
            try:
                count = int(page.getCount())
            except Exception as exc:
                if is_disposed_exception(exc):
                    raise
                return

    def _swap_notes(self, first: Any, second: Any) -> None:
        text_first = self._notes_text(first)
        text_second = self._notes_text(second)
        if text_first is None or text_second is None or text_first == text_second:
            return
        self._set_notes_text(first, text_second)
        try:
            self._set_notes_text(second, text_first)
        except Exception:
            try:
                self._set_notes_text(first, text_first)
            except Exception as restore_exc:
                if is_disposed_exception(restore_exc):
                    raise
                log.debug("move_slide restore notes failed", exc_info=True)
            raise

    def _notes_text(self, page: Any) -> str | None:
        try:
            notes = page.getNotesPage()
        except Exception as exc:
            if is_disposed_exception(exc):
                raise
            return None
        shape = find_notes_shape(notes)
        if shape is None:
            return None
        try:
            return str(shape.getString() or "")
        except Exception as exc:
            if is_disposed_exception(exc):
                raise
            return None

    def _set_notes_text(self, page: Any, text: str) -> None:
        shape = find_notes_shape(page.getNotesPage())
        if shape is None:
            raise RuntimeError("move_slide notes shape missing")
        shape.setString(text)

    def _remove_page_quietly(self, pages: Any, page: Any) -> None:
        try:
            pages.remove(page)
        except Exception as exc:
            if is_disposed_exception(exc):
                raise
            log.debug("move_slide rollback remove failed: %s", exc)

    def _take_page_name(self, source: Any, dest: Any) -> None:
        try:
            name = str(source.Name or "")
        except Exception as exc:
            if is_disposed_exception(exc):
                raise
            return
        if not name:
            return
        # Two pages cannot share a name. Park the source name, then give it
        # to the copy. Put it back if the destination rejects it.
        parked = name + "\u200b"
        try:
            source.Name = parked
        except Exception as exc:
            if is_disposed_exception(exc):
                raise
        try:
            dest.Name = name
        except Exception as exc:
            if is_disposed_exception(exc):
                raise
            try:
                source.Name = name
            except Exception as restore_exc:
                if is_disposed_exception(restore_exc):
                    raise

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
            # An empty XDrawPage is falsy. `if page` then fell through to 0,
            # so a blank Impress slide was reported as slide 0.
            if page is not None:
                pages = self.get_pages()
                count = pages.getCount()
                # getNumber() is missing or None on Impress after insert.
                # Only an in-range int may win over identity.
                if hasattr(page, "getNumber"):
                    try:
                        number = page.getNumber()
                        if isinstance(number, int) and not isinstance(number, bool):
                            idx = number - 1
                            if 0 <= idx < count:
                                return idx
                    except Exception:
                        pass

                from plugin.framework.uno_context import uno_same

                for i in range(count):
                    if uno_same(pages.getByIndex(i), page):
                        return i
        except Exception:
            log.debug("get_active_page_index failed", exc_info=True)
        # Callers outside this package require an int. A total miss still
        # reports 0; an empty page and a missing getNumber() no longer do.
        return 0


_NOTES_SHAPE_TYPE = "com.sun.star.presentation.NotesShape"


def find_notes_shape(notes_page: Any) -> Any | None:
    """The NotesShape on an Impress notes page, not whatever sits at index 1.

    A notes-master header, footer, date, or slide number can be inserted
    ahead of the notes body, so getByIndex(1) reads or overwrites the wrong
    shape. Chat context, read_slide_text, and the notes tools share this.
    """
    if notes_page is None:
        return None
    try:
        count = notes_page.getCount()
    except Exception as exc:
        if is_disposed_exception(exc):
            raise
        return None
    for i in range(count):
        try:
            shape = notes_page.getByIndex(i)
            shape_type = shape.getShapeType() if hasattr(shape, "getShapeType") else getattr(shape, "ShapeType", "")
            if shape_type == _NOTES_SHAPE_TYPE:
                return shape
        except Exception as exc:
            if is_disposed_exception(exc):
                raise
            continue
    return None


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
                    notes_shape = find_notes_shape(notes_page)
                    notes_text = ""
                    if notes_shape is not None:
                        notes_text = safe_call(notes_shape.getString, "Get notes shape string")
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
