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

import logging

logger = logging.getLogger(__name__)


class DrawBridge:
    def __init__(self, doc):
        self.doc = doc
        if not hasattr(doc, "getDrawPages"):
            raise RuntimeError("Provided document is not a Draw/Impress document.")

    def get_pages(self):
        return self.doc.getDrawPages()

    def get_active_page(self):
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

    def create_shape(self, shape_type, x, y, width, height, page=None):
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

    def get_shapes(self, page=None):
        if page is None:
            page = self.get_active_page()
        if page is None:
            raise RuntimeError("No draw page available to list shapes.")
        shapes = []
        for i in range(page.getCount()):
            shapes.append(page.getByIndex(i))
        return shapes

    def create_slide(self, index=None, switch=True):
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
                    logger.debug("setCurrentPage after insert failed: %s", exc)
        return new_page

    def delete_slide(self, index):
        """Deletes the slide at the specified index."""
        pages = self.get_pages()
        page = pages.getByIndex(index)
        pages.remove(page)

    def duplicate_slide(self, index, switch=True):
        """Duplicate the slide at index (new page after source with copied text geometry)."""
        return self._duplicate_slide_fallback(index, switch)

    def _duplicate_slide_fallback(self, index, switch=True):
        """Fallback duplicate: new page + copy shape strings (does not move originals)."""
        old_page = self.get_pages().getByIndex(index)
        new_page = self.create_slide(index + 1, switch=False)
        for i in range(old_page.getCount()):
            old_shape = old_page.getByIndex(i)
            try:
                new_shape = self.doc.createInstance(old_shape.getShapeType())
                new_page.add(new_shape)
                if hasattr(old_shape, "getSize") and hasattr(new_shape, "setSize"):
                    new_shape.setSize(old_shape.getSize())
                if hasattr(old_shape, "getPosition") and hasattr(new_shape, "setPosition"):
                    new_shape.setPosition(old_shape.getPosition())
                if hasattr(old_shape, "getString") and hasattr(new_shape, "setString"):
                    new_shape.setString(old_shape.getString())
            except Exception as exc:
                logger.debug("duplicate_slide fallback shape %s: %s", i, exc)
        if switch:
            self.set_current_page_index(index + 1)
        return new_page

    def insert_slide_from_master(self, master_index=None, master_name=None, after_index=None, switch=True):
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
                logger.debug("insert_slide_from_master MasterPage: %s", exc)
        if switch:
            self.set_current_page_index(insert_at)
        return new_page, insert_at

    def _resolve_master(self, master_index=None, master_name=None):
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

    def move_slide(self, from_index, to_index):
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
                logger.debug("move_slide insert failed: %s", exc)
                return False
        return True

    def rename_slide(self, index, name):
        page = self.get_pages().getByIndex(index)
        if hasattr(page, "Name"):
            page.Name = name
            return True
        return False

    def set_current_page_index(self, index):
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
                logger.debug("set_current_page_index failed: %s", exc)
        return False

    def get_active_page_index(self):
        try:
            page = self.get_active_page()
            if page:
                # In Draw, getNumber() - 1 is often the index.
                if hasattr(page, "getNumber"):
                    try:
                        return page.getNumber() - 1
                    except Exception:
                        pass
                
                # Fallback: compare pages by identity or index
                import uno
                pages = self.get_pages()
                count = pages.getCount()
                for i in range(count):
                    p = pages.getByIndex(i)
                    if p == page or (hasattr(uno, "areSame") and getattr(uno, "areSame")(p, page)):
                        return i
        except Exception:
            logger.debug("get_active_page_index failed", exc_info=True)
        return 0
