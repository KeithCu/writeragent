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
        if hasattr(controller, "getCurrentPage"):
            return controller.getCurrentPage()
        # Fallback to first page
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
        shapes = []
        for i in range(page.getCount()):
            shapes.append(page.getByIndex(i))
        return shapes

    def create_slide(self, index=None):
        """Creates a new slide (page) at the specified index."""
        pages = self.get_pages()
        if index is None:
            index = pages.getCount()
        return pages.insertNewByIndex(index)

    def delete_slide(self, index):
        """Deletes the slide at the specified index."""
        pages = self.get_pages()
        page = pages.getByIndex(index)
        pages.remove(page)

    def duplicate_slide(self, index):
        """Duplicates the slide at the specified index."""
        # Note: LO API for duplicating pages is a bit involved
        # For now, we can create a new page and copy shapes
        old_page = self.get_pages().getByIndex(index)
        new_page = self.create_slide(index + 1)

        # Copy shapes
        for i in range(old_page.getCount()):
            shape = old_page.getByIndex(i)
            # This is a shallow copy, might need more for complex shapes
            # But simple add works for many
            new_page.add(shape)
        return new_page
