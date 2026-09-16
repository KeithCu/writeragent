# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Page-render control flow for get_image — Writer jumpToPage + Draw GraphicExportFilter.

The old XRenderable/DIB math was removed with that route (its getRendererCount reports 1 page on
real multi-page docs; see BUG-5). What is testable without LibreOffice is the control flow of
_render_page_png: Writer no-view / page-not-found (jumpToPage clamps; the message must report the
real total) and view-cursor restore; Draw/Impress page-not-found from getDrawPages().getCount()
(no silent empty image). The happy path (storeToURL / GraphicExportFilter + PNG bytes) is
validated live."""
import base64
from unittest.mock import patch

import pytest

from plugin.tests.testing_utils import setup_uno_mocks
setup_uno_mocks()

from plugin.framework.tool import ToolContext
from plugin.writer.get_image import GetImage, _is_draw_family, _render_page_png


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
    png, reason = _render_page_png(object(), FakeDoc(has_view=False), 0)
    assert png is None
    assert "could not render page 0" in reason
    assert "no document view available" in reason


def test_writer_jumps_to_1based_lo_page():
    """Model-facing page is 0-based; jumpToPage is the 1-based Writer API."""
    doc = FakeDoc(page_count=20)
    _render_page_png(object(), doc, 0)
    assert doc._vc.current == 1
    _render_page_png(object(), doc, 2)
    assert doc._vc.current == 3


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


class FakeDrawPages:
    def __init__(self, page_count):
        self.page_count = page_count
        self.pages = [object() for _unused in range(page_count)]
        self.last_index = None

    def getCount(self):
        return self.page_count

    def getByIndex(self, index):
        self.last_index = index
        return self.pages[index]


class FakeGraphicExportFilter:
    def __init__(self):
        self.source = None

    def setSourceDocument(self, src):
        self.source = src

    def filter(self, props):
        return False


class FakeExportServiceManager:
    def __init__(self, filt):
        self._filt = filt

    def createInstanceWithContext(self, name, ctx):
        return self._filt


class FakeExportCtx:
    """Enough of a UNO ctx for GraphicExportFilter create + setSourceDocument."""

    def __init__(self, filt):
        self.ServiceManager = FakeExportServiceManager(filt)


class FakeDrawDoc:
    def __init__(self, page_count=2, services=None, pages_error=None):
        self.page_count = page_count
        self.services = services or ("com.sun.star.drawing.DrawingDocument",)
        self.pages_error = pages_error
        self._pages = FakeDrawPages(page_count)

    def supportsService(self, service):
        return service in self.services

    def getDrawPages(self):
        if self.pages_error is not None:
            raise RuntimeError(self.pages_error)
        return self._pages

    def getGraphicObjects(self):
        raise AssertionError("Draw must not use Writer getGraphicObjects()")


def test_draw_family_detects_impress_before_drawing_service():
    impress = FakeDrawDoc(services=("com.sun.star.drawing.DrawingDocument", "com.sun.star.presentation.PresentationDocument"))
    draw = FakeDrawDoc(services=("com.sun.star.drawing.DrawingDocument",))
    writer = FakeDoc()
    assert _is_draw_family(impress) is True
    assert _is_draw_family(draw) is True
    assert _is_draw_family(writer) is False


def test_draw_page_not_found_reports_real_total():
    png, reason = _render_page_png(object(), FakeDrawDoc(page_count=2), 9)
    assert png is None
    assert "page not found" in reason
    assert "2 page(s)" in reason


def test_draw_no_pages_is_a_clear_error():
    png, reason = _render_page_png(object(), FakeDrawDoc(pages_error="no pages"), 0)
    assert png is None
    assert "could not render page 0" in reason
    assert "no draw pages available" in reason


def test_draw_first_page_is_zero_not_one():
    """Regression: under 1-based, page=1 was in range on a 1-page doc and page=0 was not."""
    png, reason = _render_page_png(object(), FakeDrawDoc(page_count=1), 1)
    assert png is None
    assert "page not found" in reason
    assert "1 page(s)" in reason

    png, reason = _render_page_png(object(), FakeDrawDoc(page_count=1), 0)
    assert "page not found" not in (reason or "")


def test_draw_getbyindex_uses_0based_page():
    filt = FakeGraphicExportFilter()
    doc = FakeDrawDoc(page_count=2)
    _render_page_png(FakeExportCtx(filt), doc, 0)
    assert doc._pages.last_index == 0
    assert filt.source is doc._pages.pages[0]
    _render_page_png(FakeExportCtx(filt), doc, 1)
    assert doc._pages.last_index == 1
    assert filt.source is doc._pages.pages[1]


def test_impress_uses_draw_page_path_not_writer_view_cursor():
    """Impress supports DrawingDocument too; must not fall through to jumpToPage."""
    doc = FakeDrawDoc(
        page_count=1,
        services=("com.sun.star.drawing.DrawingDocument", "com.sun.star.presentation.PresentationDocument"),
    )
    png, reason = _render_page_png(object(), doc, 4)
    assert png is None
    assert "page not found" in reason
    assert "1 page(s)" in reason


def test_get_image_registers_draw_and_impress():
    assert "com.sun.star.text.TextDocument" in GetImage.uno_services
    assert "com.sun.star.drawing.DrawingDocument" in GetImage.uno_services
    assert "com.sun.star.presentation.PresentationDocument" in GetImage.uno_services


def _tctx(doc, doc_type="draw"):
    return ToolContext(doc=doc, ctx=object(), doc_type=doc_type, services={}, caller="test")


def test_execute_draw_page_error_is_tool_error():
    res = GetImage().execute(_tctx(FakeDrawDoc(page_count=2)), page=9)
    assert res["status"] == "error"
    assert "page not found" in res["message"]
    assert "2 page(s)" in res["message"]


def test_execute_draw_page_success_returns_mcp_image():
    png = b"\x89PNG\r\n\x1a\n" + b"draw-page"
    with patch("plugin.writer.get_image._render_page_png", return_value=(png, None)):
        res = GetImage().execute(_tctx(FakeDrawDoc()), page=0)
    assert res["status"] == "ok"
    assert res["source"] == "page 0"
    assert res["_mcp_image"]["mimeType"] == "image/png"
    assert base64.b64decode(res["_mcp_image"]["data"]) == png


def test_execute_rejects_negative_page():
    res = GetImage().execute(_tctx(FakeDrawDoc()), page=-1)
    assert res["status"] == "error"
    assert "0-based" in res["message"]


def test_execute_page_param_is_0based():
    assert "0-based" in GetImage.parameters["properties"]["page"]["description"]
    assert "1-based" not in GetImage.parameters["properties"]["page"]["description"]
    assert "1-based" not in GetImage.description


def test_execute_named_image_uses_visual_helpers_not_graphic_objects():
    doc = FakeDrawDoc()
    raw = b"\x89PNG\r\n\x1a\nimg"
    with (
        patch("plugin.doc.visual_helpers.get_graphic_object_by_name", return_value=object()) as lookup,
        patch("plugin.writer.get_image.export_graphic_object_to_bytes", return_value=raw),
    ):
        res = GetImage().execute(_tctx(doc), image="Logo")
    lookup.assert_called_once_with(doc, "Logo")
    assert res["status"] == "ok"
    assert res["source"] == "Logo"
    assert base64.b64decode(res["_mcp_image"]["data"]) == raw


def test_execute_named_image_missing_is_clear_error():
    with patch("plugin.doc.visual_helpers.get_graphic_object_by_name", return_value=None):
        res = GetImage().execute(_tctx(FakeDrawDoc()), image="Missing")
    assert res["status"] == "error"
    assert "Missing" in res["message"]
    assert "image_list" in res["message"]


def test_execute_selection_none_is_clear_error():
    with patch("plugin.writer.get_image.get_selected_image_base64", return_value=None):
        res = GetImage().execute(_tctx(FakeDrawDoc()), selection=True)
    assert res["status"] == "error"
    assert "No image selected" in res["message"]
