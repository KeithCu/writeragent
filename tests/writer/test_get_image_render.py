# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""R4/BUG-5: page render via the writer_png_Export filter — pure control-flow tests with fakes.

The old XRenderable/DIB math was removed with that route (its getRendererCount reports 1 page on
real multi-page docs; see BUG-5). What is testable without LibreOffice is the control flow of
_render_page_png: the no-view error, the page-not-found error (jumpToPage clamps; the message must
report the real total), and that the view cursor is restored best-effort on the way out. The happy
path (storeToURL + PNG bytes) is validated live."""
import pytest

from plugin.tests.testing_utils import setup_uno_mocks
setup_uno_mocks()

from plugin.writer.get_image import _render_page_png


class FakeViewCursor:
    def __init__(self, page_count):
        self.page_count = page_count
        self.current = 1
        self.restored_to = None

    def jumpToPage(self, n):
        self.current = min(max(1, n), self.page_count)  # LO clamps out-of-range jumps
        return True

    def jumpToLastPage(self):
        self.current = self.page_count

    def getPage(self):
        return self.current

    def getStart(self):
        return "start-range"

    def gotoRange(self, rng, expand):
        self.restored_to = rng


class FakeText:
    def createTextCursorByRange(self, rng):
        return ("saved", rng)


class FakeController:
    def __init__(self, vc):
        self._vc = vc

    def getViewCursor(self):
        return self._vc


class FakeDoc:
    def __init__(self, page_count=20, has_view=True):
        self._vc = FakeViewCursor(page_count)
        self._has_view = has_view

    def getCurrentController(self):
        if not self._has_view:
            raise RuntimeError("no view")
        return FakeController(self._vc)

    def getText(self):
        return FakeText()


def test_no_view_is_a_clear_error():
    png, reason = _render_page_png(object(), FakeDoc(has_view=False), 1)
    assert png is None
    assert "could not render page 1" in reason
    assert "no document view available" in reason


def test_page_not_found_reports_real_total():
    doc = FakeDoc(page_count=20)
    png, reason = _render_page_png(object(), doc, 999)
    assert png is None
    assert "page not found" in reason
    assert "20 page(s)" in reason


def test_page_not_found_restores_view_cursor():
    doc = FakeDoc(page_count=20)
    _render_page_png(object(), doc, 999)
    assert doc._vc.restored_to == ("saved", "start-range")


@pytest.mark.parametrize("bad_page", [999, 21])
def test_out_of_range_never_renders(bad_page):
    doc = FakeDoc(page_count=20)
    png, reason = _render_page_png(object(), doc, bad_page)
    assert png is None and "page not found" in reason
