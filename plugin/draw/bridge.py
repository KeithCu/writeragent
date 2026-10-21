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
from plugin.framework.constants import truncate_chat_context
from plugin.framework.errors import ToolExecutionError, DocumentDisposedError, UnoObjectError, check_disposed, is_disposed_exception, is_real_disposal, safe_call, uno_probe
from plugin.framework.thread_guard import main_thread_only

from plugin.framework.errors import uno_read

log = logging.getLogger(__name__)


class PageExchangeRollbackError(RuntimeError):
    """A page exchange failed and putting its shapes back failed too."""


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
        # Bugfix: a closed document raises DisposedException from getDrawPage(),
        # and the old except turned that into a missing page / not-found result.
        # Real disposal leaves as DocumentDisposedError (execute_safe reports
        # DOCUMENT_DISPOSED); any other failure stays on the missing-page path.
        page = uno_read(lambda: doc.getDrawPage(), None)
        if page is not None:
            return _SingleDrawPageContainer(page)
    if hasattr(doc, "getSheets"):

        def sheet_page() -> Any:
            from plugin.calc.bridge import CalcBridge

            sheet = CalcBridge(doc).get_active_sheet()
            return sheet.getDrawPage() if sheet is not None and hasattr(sheet, "getDrawPage") else None

        # Same bugfix as above, for the active sheet's draw page.
        page = uno_read(sheet_page, None)
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
        if hasattr(self.doc, "supportsService") and self.doc.supportsService("com.sun.star.sheet.SpreadsheetDocument"):
            try:
                controller = self.doc.getCurrentController()
                sheet = None
                if controller is not None:
                    if hasattr(controller, "ActiveSheet"):
                        sheet = controller.ActiveSheet
                    if sheet is None and hasattr(controller, "getActiveSheet"):
                        sheet = controller.getActiveSheet()
                if sheet is not None:
                    if hasattr(sheet, "getDrawPage"):
                        return sheet.getDrawPage()
                    if hasattr(sheet, "DrawPage"):
                        return sheet.DrawPage
            except Exception as exc:
                # Bugfix: a closed spreadsheet raises DisposedException from the active sheet draw page,
                # and this except turned that into a missing page / not-found result.
                # Re-raise real disposal so execute_safe reports DOCUMENT_DISPOSED.
                # Any other failure stays on the existing missing-page path.
                if is_real_disposal(exc):
                    raise
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

        ``IndexError`` stays a ``ToolExecutionError`` (page out of range). Any
        other failure propagates. ``execute_safe`` maps it to
        ``TOOL_EXECUTION_ERROR`` (``Tool execution failed: ...``), the same code
        as the old wrap, and includes the exception type.
        """
        try:
            return cls.resolve_slide(doc, page_index)
        except IndexError:
            # resolve_slide only raises IndexError when page_index is an int.
            raise ToolExecutionError("Page index %s out of range." % page_index)
        # Other failures propagate. execute_safe maps them to TOOL_EXECUTION_ERROR.

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

    def create_slide(self, index: int | None = None, switch: bool = True) -> tuple[Any, int]:
        """Insert a blank page so it occupies ``index`` (append when omitted).

        Returns ``(page, index)`` for the slot the new page actually occupies.
        Impress ``DrawPage.getNumber()`` is missing, so callers must use this
        index instead of ``get_active_page_index`` after the controller switches.

        ``add_slide(page=N)`` must not pass ``N`` straight to
        ``insertNewByIndex`` and report ``active_page_index=N``. The new
        slide is created one slot later, and the next tool edits the
        previous slide. ``page=0`` never lands at 0, so master
        inheritance treats the new page as its own neighbor.
        ``InsertSdPage`` (``sd/source/ui/unoidl/unomodel.cxx``) inserts
        *after* ``min(count-1, nIndex)``. The new page lands at
        ``min(count-1, n)+1``. Append (``n >= count-1``) happens to land
        at the end, which hides the off-by-one on the default "add at
        end" path. Choose ``n`` so the landing index is the request. A
        request of 0 on a non-empty deck inserts after page 0 (lands at
        1) and exchanges with page 0 — the same reorder ``move_slide``
        uses, because ``InsertSdPage`` cannot create a page at index 0.
        The returned page is the object that occupies the requested
        index.
        """
        pages = self.get_pages()
        count = int(pages.getCount())
        if index is None:
            index = count
        return self._insert_page_at(int(index), switch, count)

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
        # Bugfix: duplicate() returning None was handed back as the new page, and
        # switch=True still called set_current_page_index(index + 1). DuplicateSlide
        # then reported success. move_slide already treats a None clone as failure.
        # Raise before any view change.
        if new_page is None:
            raise RuntimeError("duplicate_slide: document duplicate() returned no page")
        if switch:
            self.set_current_page_index(index + 1)
        return new_page

    def insert_slide_from_master(self, master_index: int | None = None, master_name: str | None = None, after_index: int | None = None, switch: bool = True) -> tuple[Any, int]:
        """Insert a slide after ``after_index`` (default: active) and assign its master.

        The returned index is the slot the new slide occupies. Transform
        ``InsertMasterSlide`` stores that index as the current slide.
        ``insertNewByIndex`` does not place a page at the index it is given;
        ``_insert_page_at`` compensates. See ``create_slide``.
        """
        pages = self.get_pages()
        count = int(pages.getCount())
        if after_index is None:
            after_index = self.get_active_page_index()
        # A bad after_index used to append (requested = count). Let int() raise.
        requested = int(after_index) + 1
        master = self._resolve_master(master_index=master_index, master_name=master_name)
        if master is None and (master_index is not None or master_name is not None):
            raise ValueError(f"Master page not found: index={master_index} name={master_name}")

        new_page, landed = self._insert_page_at(requested, switch=False, count=count)
        if master is not None:
            try:
                new_page.MasterPage = master
            except Exception as exc:
                # Bugfix: a failed assignment was logged at debug and the new page was returned,
                # while AddSlide removes the page when post-insert work fails. The caller then
                # treated the insert as applied. Re-raise disposal without remove; otherwise
                # remove the incomplete page and raise RuntimeError with the failure cause.
                if is_disposed_exception(exc):
                    raise
                log.debug("insert_slide_from_master MasterPage: %s", exc)
                try:
                    pages.remove(new_page)
                except Exception as rm_exc:
                    if is_disposed_exception(rm_exc):
                        raise
                    log.debug("insert_slide_from_master could not remove the new page: %s", rm_exc)
                raise RuntimeError("Failed to assign master page: %s" % exc) from exc
        # View follows the real slot. ``_insert_page_at(..., switch=False)``
        # only restores a front-insert; the jump happens after MasterPage
        # is assigned so the shown slide already has its master.
        if switch:
            self.set_current_page_index(landed)
        return new_page, landed

    def _insert_page_at(self, index: int, switch: bool, count: int | None = None) -> tuple[Any, int]:
        """Insert a blank page at ``index`` and optionally show it."""
        pages = self.get_pages()
        if count is None:
            count = int(pages.getCount())
        if index < 0:
            index = 0
        elif index > count:
            index = count
        exchanging = index == 0 and count > 0
        # Capture the view before the exchange. Afterwards the object that
        # was page 0 holds the new slide, so a controller still pointing at
        # it would show the new slide even when activate is false.
        previous = self._controller_page() if (not switch and exchanging) else None
        new_page, landed = self._insert_blank_page(index, count)
        self._show_inserted_page(landed, switch, previous, exchanged=exchanging and landed == 0)
        return new_page, landed

    def _insert_blank_page(self, index: int, count: int) -> tuple[Any, int]:
        """Place a new blank page at ``index`` (already clamped to ``0..count``)."""
        pages = self.get_pages()
        if count == 0 or index >= count:
            # insertNewByIndex(count) lands at the end (or the only slot).
            new_page = pages.insertNewByIndex(count)
            landed = 0 if count == 0 else count
            if new_page is None:
                new_page = pages.getByIndex(landed)
            return new_page, landed
        if index == 0:
            # Lands at 1. Exchange so the new slide occupies index 0 and the
            # previous first slide shifts right. The object at index 0 is what
            # later layout and master writes must touch.
            pages.insertNewByIndex(0)
            try:
                self._exchange_page_contents(pages.getByIndex(0), pages.getByIndex(1))
            except Exception as exc:
                if is_disposed_exception(exc):
                    raise
                log.exception("create_slide could not place the new page at index 0")
                if isinstance(exc, PageExchangeRollbackError):
                    # Shapes may sit on page 1 or the temp page. Removing a page
                    # here could delete them; report where they are instead.
                    raise
                try:
                    pages.remove(pages.getByIndex(1))
                except Exception as rm_exc:
                    if is_disposed_exception(rm_exc):
                        raise
                    log.debug("create_slide could not remove the new page: %s", rm_exc)
                raise RuntimeError("Failed to place the new slide at index 0: %s" % exc) from exc
            return pages.getByIndex(0), 0
        # 0 < index < count: insert after the page that should precede it.
        # insertNewByIndex(index-1) lands at index.
        new_page = pages.insertNewByIndex(index - 1)
        if new_page is None:
            new_page = pages.getByIndex(index)
        return new_page, index

    def _controller_page(self) -> Any | None:
        """Page the view is showing, or None when the controller has none.

        ``get_active_page`` falls back to slide 0. Using that fallback here
        would look like the user was on slide 0 and the front-insert restore
        would move the view.
        """
        get_controller = getattr(self.doc, "getCurrentController", None)
        if not callable(get_controller):
            return None
        controller = uno_probe(get_controller, None)
        if controller is None:
            return None
        get_page = getattr(controller, "getCurrentPage", None)
        if not callable(get_page):
            return None
        return uno_probe(get_page, None)

    def _show_inserted_page(self, landed: int, switch: bool, previous: Any, exchanged: bool) -> None:
        if switch:
            self.set_current_page_index(landed)
            return
        if not exchanged or previous is None or landed != 0:
            return
        from plugin.framework.uno_context import uno_same

        pages = self.get_pages()
        if pages.getCount() > 1 and uno_same(previous, pages.getByIndex(0)):
            self.set_current_page_index(1)

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

        ``XDrawPages.remove`` disposes the source (``XDrawPages.idl``
        has no way to put that page back). A replacement blank page
        that only receives a shallow shape clone keeps position, size,
        a few properties, and text, and drops ``GroupShape``, graphics,
        connectors, and charts. A move to index 0 that then calls
        ``_exchange_page_contents`` returns False with the deck already
        reordered. ``insertNewByIndex`` only creates a blank page, and
        ``InsertSdPage`` (``sd/source/ui/unoidl/unomodel.cxx``) inserts
        after ``min(count-1, nIndex)``, so it cannot create a page at
        index 0. ``XDrawPageDuplicator.duplicate``
        (``SdXImpressDocument::duplicate``) clones the whole page,
        including shapes and notes, and inserts that clone after the
        source. Removing the source leaves the clone in the source
        slot. Neighboring slots then swap by moving those cloned shapes
        (not by constructing new ones) plus page name, layout, master,
        notes, and transition. A failed swap puts the shapes back, so
        the clone is still in the original slot and the other slides
        are unchanged. If a reverse neighbor swap fails with an ordinary
        error, this raises PageExchangeRollbackError instead of returning
        False. False still means every reverse swap completed.
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
            if not self._take_page_name(source, copy):
                log.warning("move_slide: failed to transfer the page name")
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
                if not self._take_page_name(copy, source):
                    log.warning("move_slide undo: failed to restore the page name")
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
            # Bugfix: PageExchangeRollbackError means shapes may sit on the
            # temp page which _exchange_page_contents deliberately did not remove,
            # and returning False made MoveSlide say the move merely failed.
            # Re-raising PageExchangeRollbackError and disposal lets callers report
            # rollback failure or abort on a dead document, while returning False
            # is kept for clean rollback.
            if is_disposed_exception(exc) or isinstance(exc, PageExchangeRollbackError):
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
        except Exception as exc:
            # Bugfix: another exchange after PageExchangeRollbackError moves
            # shapes again on a deck whose rollback already failed, and a disposal
            # during undo was logged and dropped while the original error was re-raised.
            # Re-raise PageExchangeRollbackError and disposal immediately before or during undo.
            if isinstance(exc, PageExchangeRollbackError) or is_disposed_exception(exc):
                raise
            for prev in reversed(swapped):
                try:
                    self._exchange_page_contents(pages.getByIndex(prev), pages.getByIndex(prev + step))
                except Exception as undo_exc:
                    # Bugfix: an ordinary failure from a reverse exchange was logged and the
                    # loop continued, then the original error was re-raised. move_slide
                    # treats that as a clean rollback and returns False, while that swap is
                    # still applied. Stop. Disposal and PageExchangeRollbackError stay those
                    # exceptions (the temporary page may still hold shapes).
                    if isinstance(undo_exc, PageExchangeRollbackError) or is_disposed_exception(undo_exc):
                        raise
                    raise PageExchangeRollbackError(
                        "Slide move could not undo a reorder step. The deck may be partially reordered. "
                        "Original error: %s. Undo error: %s" % (exc, undo_exc)
                    ) from undo_exc
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
        rollback_failed = False
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
        except Exception as exc:
            # Bugfix: rollback used to return False for a disposal, and this except
            # then raised PageExchangeRollbackError from the original error, so
            # execute_safe saw a RuntimeError and not DOCUMENT_DISPOSED. Attempt
            # rollback; if rollback raises disposal or the original error was disposal,
            # re-raise disposal. Otherwise wrap failed rollback in PageExchangeRollbackError.
            # rollback_failed stays set when rollback does not return True, including when
            # rollback raises, so finally does not delete the temp page that still holds shapes.
            try:
                rolled_back = self._rollback_exchange(
                    first, second, temp, first_shapes, second_shapes,
                    moved_first, moved_second, moved_onto_second,
                )
            except Exception as rollback_exc:
                rollback_failed = True
                if is_disposed_exception(rollback_exc):
                    raise
                if is_disposed_exception(exc):
                    raise exc from rollback_exc
                raise PageExchangeRollbackError(
                    "Slide exchange failed and its rollback failed. Some shapes are on the "
                    "last slide (temporary page). Original error: %s" % rollback_exc
                ) from rollback_exc
            if not rolled_back:
                rollback_failed = True
                if is_disposed_exception(exc):
                    raise
                raise PageExchangeRollbackError(
                    "Slide exchange failed and its rollback failed. Some shapes are on the "
                    "last slide (temporary page). Original error: %s" % exc
                ) from exc
            raise
        finally:
            if temp is not None and not rollback_failed:
                self._remove_page_quietly(pages, temp)

    def _rollback_exchange(self, first: Any, second: Any, temp: Any, first_shapes: list[Any], second_shapes: list[Any], moved_first: bool, moved_second: bool, moved_onto_second: bool) -> bool:
        """Put shapes back on *first* and *second*. Metadata undo is inside the swap.
        Returns True if rollback succeeded, False if it failed."""
        if temp is None:
            return True
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
            return True
        except Exception as exc:
            # Bugfix: this except returned False for every failure, including
            # DisposedException, which caused the caller to wrap a closed document
            # as PageExchangeRollbackError instead of propagating disposal.
            # Re-raising disposal allows DOCUMENT_DISPOSED to be recognized.
            if is_disposed_exception(exc):
                raise
            log.exception("move_slide restore exchanged pages failed")
            return False

    def _snapshot_shapes(self, page: Any) -> list[Any]:
        count = uno_probe(lambda: int(page.getCount()), 0)
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
        names = uno_probe(lambda: (str(first.Name or ""), str(second.Name or "")), None)
        if names is None or names[0] == names[1]:
            return
        name_first, name_second = names
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
        def read() -> tuple[bool, Any]:
            # MasterPage is XMasterPageTarget, not an XPropertySet entry;
            # SdGenericDrawPage::getPropertyValue throws UnknownPropertyException
            # (sd/source/ui/unoidl/unopage.cxx), so the getattr fallback below
            # never runs on a live page. PyUNO .MasterPage calls getMasterPage /
            # setMasterPage.
            if prop == "MasterPage":
                return True, getattr(page, prop)
            if hasattr(page, "getPropertyValue"):
                return True, page.getPropertyValue(prop)
            if hasattr(page, prop):
                return True, getattr(page, prop)
            return False, None

        return uno_probe(read, (False, None))

    def _write_page_prop(self, page: Any, prop: str, value: Any) -> None:
        # Assigning Layout instantiates placeholders (apply_slide_layout).
        # The slide's real placeholders already moved with the shapes; drop
        # only the ones this write just inserted.
        before = self._snapshot_shapes(page) if prop == "Layout" else None
        if prop == "MasterPage":
            setattr(page, prop, value)
        elif hasattr(page, "setPropertyValue"):
            page.setPropertyValue(prop, value)
        else:
            setattr(page, prop, value)
        if before is not None:
            self._drop_shapes_added_by_prop(page, before)

    def _drop_shapes_added_by_prop(self, page: Any, before: list[Any]) -> None:
        from plugin.framework.uno_context import uno_same

        guard = 0
        # A failed count reads as 0, so the loop below never starts.
        count = uno_probe(lambda: int(page.getCount()), 0)
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
        # Bugfix: None means this page has no notes shape (find_notes_shape / getNotesPage),
        # which notes.py already treats as a real state. Returning when only one side was
        # None left that text on the page object after shapes moved to the neighbor. Notes
        # are not shapes, so shape rollback never moved them. Raising before any write lets
        # _swap_page_meta undo name and props and lets _exchange_page_contents restore shapes.
        # Empty text is not content to lose, so empty against None returns safely.
        text_first = self._notes_text(first)
        text_second = self._notes_text(second)
        if text_first == text_second:
            return
        if text_first is None or text_second is None:
            present = text_first if text_second is None else text_second
            if not present:
                return
            raise RuntimeError(
                "move_slide cannot move speaker notes: the neighboring slide has no notes shape"
            )
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
        shape = find_notes_shape(uno_probe(lambda: page.getNotesPage(), None))
        if shape is None:
            return None
        return uno_probe(lambda: str(shape.getString() or ""), None)

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

    def _take_page_name(self, source: Any, dest: Any) -> bool:
        try:
            name = str(source.Name or "")
        except Exception as exc:
            if is_disposed_exception(exc):
                raise
            log.warning("move_slide: failed to read source page name: %s", exc)
            return False
        if not name:
            return True
        # Two pages cannot share a name. Park the source name, then give it
        # to the copy. Put it back if the destination rejects it.
        parked = name + "\u200b"
        try:
            source.Name = parked
        except Exception as exc:
            if is_disposed_exception(exc):
                raise
            log.warning("move_slide: failed to park source page name: %s", exc)
            return False
        try:
            dest.Name = name
        except Exception as exc:
            if is_disposed_exception(exc):
                raise
            log.warning("move_slide: failed to assign name to destination page: %s", exc)
            try:
                source.Name = name
            except Exception as restore_exc:
                if is_disposed_exception(restore_exc):
                    raise
                log.warning("move_slide: failed to restore original name: %s", restore_exc)
            return False
        return True

    def rename_slide(self, index: int, name: str) -> bool:
        try:
            pages = self.get_pages()
            if index < 0 or index >= pages.getCount():
                return False
            page = pages.getByIndex(index)
            if hasattr(page, "Name"):
                page.Name = name
                return True
        except Exception as exc:
            if is_disposed_exception(exc):
                raise
            log.debug("rename_slide failed: %s", exc)
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
                # Bugfix: setCurrentPage disposal was logged and turned into False,
                # so duplicate_slide and insert continued as if only the view switch had failed.
                # Re-raising disposal propagates document disposal to callers.
                if is_disposed_exception(exc):
                    raise
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
        except Exception as exc:
            # Bugfix: a closed document raises DisposedException from get_active_page(),
            # and this except turned that into a missing page / not-found result.
            # Re-raise real disposal so execute_safe reports DOCUMENT_DISPOSED.
            # Any other failure stays on the existing missing-page path.
            if is_real_disposal(exc):
                raise
            log.debug("get_active_page_index failed", exc_info=True)
        # Callers outside this package require an int. A total miss still
        # reports 0; an empty page and a missing getNumber() no longer do.
        return 0


_NOTES_SHAPE_TYPE = "com.sun.star.presentation.NotesShape"


def find_notes_shape(notes_page: Any) -> Any | None:
    """The NotesShape on an Impress notes page, not whatever sits at index 1.

    A notes-master header, footer, date, or slide number can be inserted
    ahead of the notes body, so getByIndex(1) reads or overwrites the wrong
    shape. Chat context, read_slide_text, the notes tools, and ppt-master
    notes import and enhance share this.
    """
    if notes_page is None:
        return None
    for i in range(uno_probe(lambda: notes_page.getCount(), 0)):
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
    """Get a Draw/Impress chat summary. max_context is a prefix cap; when the summary is longer, the prefix is returned with a blank line and "[... document truncated ...]" appended. ctx is unused and kept so document_helpers can pass the chat signature."""
    try:
        check_disposed(model, "Document Model")
        bridge = DrawBridge(model)
        pages = bridge.get_pages()
        active_page = bridge.get_active_page()

        is_impress = safe_call(model.supportsService, "Check supportsService", "com.sun.star.presentation.PresentationDocument")
        doc_type = "Impress Presentation" if is_impress else "Draw Document"

        ctx_str = "%s: %s\n" % (doc_type, safe_call(model.getURL, "Get document URL") or "Untitled")
        ctx_str += "Total %s: %d\n" % ("Slides" if is_impress else "Pages", safe_call(pages.getCount, "Get page count"))

        # Active Slide Index is -1 when getCurrentPage() and getByIndex()
        # return different Python wrappers for one page and the loop uses
        # ``==``. PyUNO wrappers often fail ``==`` / ``is`` for one UNO
        # object. get_active_page_index already walks with uno_same (is,
        # then ==, then uno.isSame). Use that same identity ladder so the
        # chat index matches the controller's current page.
        from plugin.framework.uno_context import uno_same

        active_page_idx = -1
        for i in range(safe_call(pages.getCount, "Get page count")):
            candidate = safe_call(pages.getByIndex, "Get page by index", i)
            if uno_same(candidate, active_page):
                active_page_idx = i
                break

        ctx_str += "Active %s Index: %d\n" % ("Slide" if is_impress else "Page", active_page_idx)

        # Tip: page size lets the model keep shapes on the slide.
        page_w = getattr(active_page, "Width", None)
        page_h = getattr(active_page, "Height", None)
        if isinstance(page_w, int) and isinstance(page_h, int):
            ctx_str += "%s Size: width=%d, height=%d (1/100 mm)\n" % (
                "Slide" if is_impress else "Page", page_w, page_h)

        # Summarize shapes on the active page.
        # A blank slide still has speaker notes. An empty XDrawPage is
        # falsy when it has zero shapes, so `if active_page` skips the
        # whole block and drops the notes (and the shapes section) from
        # chat context. Same trap as get_active_page_index, which already
        # uses `is not None`. None means there is no page; a blank page
        # is still a page, so notes on add_slide blank/none are included.
        if active_page is not None:
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

        # Bugfix: the parameter was accepted and document_helpers passed
        # max_chars / max_context, but the body returned the whole active-page
        # summary, so a shape-heavy slide or long speaker notes exceeded
        # CHAT_DOCUMENT_CONTEXT_MAX_CHARS on every prompt. The prefix matches
        # Writer's full-text cap. truncate_chat_context forces negative limit to 0
        # so the slice cannot keep the tail.
        return truncate_chat_context(ctx_str, max_context)
    except DocumentDisposedError:
        raise
    except Exception:
        # UnoObjectError is an Exception; both handlers returned the same fallback.
        log.exception("get_draw_context_for_chat error")
        return "[Unable to read Draw/Impress context. The document may be locked or initializing.]"
