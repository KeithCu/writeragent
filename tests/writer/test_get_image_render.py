# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Page-render control flow for image_get — Writer jumpToPage + Draw GraphicExportFilter.

The old XRenderable/DIB math was removed with that route (its getRendererCount reports 1 page on
real multi-page docs; see BUG-5). What is testable without LibreOffice is the control flow of
_render_page_png: Writer no-view / page-not-found (jumpToPage clamps; the message must report the
real total) and view-cursor restore; Draw/Impress page-not-found from getDrawPages().getCount()
(no silent empty image). The happy path (storeToURL / GraphicExportFilter + PNG bytes) is
validated live."""
import base64
import copy
import json
from unittest.mock import MagicMock, patch

import pytest


class _DisposedException(Exception):
    pass


from plugin.framework.tool import ToolContext, prepare_tool_call, to_openai_schema
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

    def getText(self):
        return FakeText()

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

    def supportsService(self, service):
        return service == "com.sun.star.text.TextDocument"


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


@pytest.mark.parametrize("bad_page", [999, 21])
def test_page_not_found_reports_real_total(bad_page):
    doc = FakeDoc(page_count=20)
    png, reason = _render_page_png(object(), doc, bad_page)
    assert png is None
    assert "page not found" in reason
    assert "20 page(s)" in reason


def test_page_not_found_restores_view_cursor():
    doc = FakeDoc(page_count=20)
    _render_page_png(object(), doc, 999)
    assert doc._vc.restored_to == ("saved", doc._vc)


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
    from plugin.framework.tool import ToolRegistry

    registry = ToolRegistry(services={})
    registry.register(GetImage())
    for svc in (
        "com.sun.star.text.TextDocument",
        "com.sun.star.drawing.DrawingDocument",
        "com.sun.star.presentation.PresentationDocument",
        "com.sun.star.sheet.SpreadsheetDocument",
    ):
        doc = MagicMock()
        doc.supportsService = lambda candidate, target=svc: candidate == target
        assert "image_get" in {t.name for t in registry.get_tools(doc=doc)}


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


@pytest.mark.parametrize(
    ("doc_type", "expected_source"),
    [("draw", "shape_get_tree"), ("writer", "get_content"), ("calc", "image_list")],
)
def test_execute_named_image_missing_is_clear_error(doc_type, expected_source):
    with patch("plugin.doc.visual_helpers.get_graphic_object_by_name", return_value=None):
        res = GetImage().execute(_tctx(FakeDrawDoc(), doc_type=doc_type), image="Missing")
    assert res["status"] == "error"
    assert "Missing" in res["message"]
    assert expected_source in res["message"]
    if doc_type == "draw":
        assert "image_list" not in res["message"]


def test_execute_selection_none_is_clear_error():
    with patch("plugin.doc.visual_helpers.selected_graphic_object", return_value=None):
        res = GetImage().execute(_tctx(FakeDrawDoc()), selection=True)
    assert res["status"] == "error"
    assert "No image selected" in res["message"]
    assert "image_list" not in res["message"]

    fake_obj = object()
    with (
        patch("plugin.doc.visual_helpers.selected_graphic_object", return_value=fake_obj),
        patch("plugin.writer.get_image.export_graphic_object_to_bytes", return_value=None),
    ):
        res = GetImage().execute(_tctx(FakeDrawDoc()), selection=True)
    assert res["status"] == "error"
    assert "No image selected" in res["message"]
    assert "image_list" not in res["message"]


@pytest.mark.parametrize("doc_type", ["writer", "draw", "calc"])
def test_selection_false_error_offers_page_only_where_accepted(doc_type):
    res = GetImage().execute(_tctx(FakeDrawDoc(), doc_type=doc_type), selection=False)
    assert res["status"] == "error"
    assert ("page=" in res["message"]) == ("page" in GetImage().get_parameters(doc_type)["properties"])


def test_writer_page_store_disposal_propagates():
    from plugin.framework.errors import DocumentDisposedError

    doc = FakeDoc()
    doc.storeToURL = MagicMock(side_effect=_DisposedException("closed"))
    with pytest.raises(DocumentDisposedError):
        _render_page_png(object(), doc, 0)


def test_draw_page_filter_disposal_propagates():
    from plugin.framework.errors import DocumentDisposedError

    filt = FakeGraphicExportFilter()
    filt.filter = MagicMock(side_effect=_DisposedException("closed"))
    with pytest.raises(DocumentDisposedError):
        _render_page_png(FakeExportCtx(filt), FakeDrawDoc(page_count=2), 0)


@pytest.mark.parametrize(
    "args",
    [
        {"page": "two"},
        {"page": True},
        {"image": True},
        {"selection": "maybe"},
    ],
)
def test_wrong_argument_types_are_rejected_before_execute(args):
    # The registry and the smolagents adapter both run prepare_tool_call before execute,
    # so a wrong type never reaches GetImage.execute.
    prep = prepare_tool_call(GetImage(), _tctx(FakeDrawDoc()), "image_get", args)
    assert prep.error is not None
    assert prep.error["code"] == "VALIDATION_ERROR"


def test_calc_sheet_index_wrong_type_rejected():
    prep = prepare_tool_call(
        GetImage(),
        _tctx(FakeDrawDoc(), doc_type="calc"),
        "image_get",
        {"image": "Logo", "sheet_index": "one"},
    )
    assert prep.error is not None
    assert prep.error["code"] == "VALIDATION_ERROR"


def test_sheet_index_only_reaches_calc():
    tool = GetImage()
    writer_prep = prepare_tool_call(
        tool,
        _tctx(FakeDrawDoc(), doc_type="writer"),
        "image_get",
        {"image": "Logo", "sheet_index": 1},
    )
    assert writer_prep.error is None
    assert writer_prep.ignored == ["sheet_index"]
    assert writer_prep.kwargs == {"image": "Logo"}

    calc_prep = prepare_tool_call(
        tool,
        _tctx(FakeDrawDoc(), doc_type="calc"),
        "image_get",
        {"image": "Logo", "sheet_index": 1},
    )
    assert calc_prep.error is None
    assert calc_prep.ignored == []
    assert calc_prep.kwargs == {"image": "Logo", "sheet_index": 1}

    assert "sheet_index" in (GetImage.parameters or {})["properties"]


def test_whole_float_page_is_coerced_to_int_before_execute():
    prep = prepare_tool_call(GetImage(), _tctx(FakeDrawDoc()), "image_get", {"page": 2.0})
    assert prep.error is None
    assert prep.kwargs == {"page": 2}
    assert type(prep.kwargs["page"]) is int


def test_execute_rejects_multiple_exclusive_modes():
    res1 = GetImage().execute(_tctx(FakeDrawDoc()), page=1, image="x")
    assert res1["status"] == "error"
    assert "at most one" in res1["message"]

    res2 = GetImage().execute(_tctx(FakeDrawDoc()), page=0, selection=True)
    assert res2["status"] == "error"
    assert "at most one" in res2["message"]

    res3 = GetImage().execute(_tctx(FakeDrawDoc()), image="Logo", selection=True)
    assert res3["status"] == "error"
    assert "at most one" in res3["message"]


@pytest.mark.parametrize("bad_image", ["", "   "])
def test_execute_rejects_empty_image(bad_image):
    res = GetImage().execute(_tctx(FakeDrawDoc()), image=bad_image)
    assert res["status"] == "error"
    assert "non-empty string" in res["message"]


def test_execute_rejects_selection_false():
    res = GetImage().execute(_tctx(FakeDrawDoc()), selection=False)
    assert res["status"] == "error"
    assert "selection=false" in res["message"]


def test_execute_empty_args_defaults_to_selection_path():
    with patch("plugin.doc.visual_helpers.selected_graphic_object", return_value=None):
        res = GetImage().execute(_tctx(FakeDrawDoc()))
    assert res["status"] == "error"
    assert "No image selected" in res["message"]


def test_execute_named_image_with_selection_false_uses_named_path():
    doc = FakeDrawDoc()
    raw = b"\x89PNG\r\n\x1a\nnamed"
    with (
        patch("plugin.doc.visual_helpers.get_graphic_object_by_name", return_value=object()),
        patch("plugin.writer.get_image.export_graphic_object_to_bytes", return_value=raw),
    ):
        res = GetImage().execute(_tctx(doc), image="Logo", selection=False)
    assert res["status"] == "ok"
    assert res["source"] == "Logo"


def test_page_only_reaches_off_calc():
    tool = GetImage()
    calc_prep = prepare_tool_call(
        tool,
        _tctx(FakeDrawDoc(), doc_type="calc"),
        "image_get",
        {"page": 0},
    )
    assert calc_prep.error is None
    assert calc_prep.ignored == ["page"]
    assert calc_prep.kwargs == {}

    draw_prep = prepare_tool_call(
        tool,
        _tctx(FakeDrawDoc(), doc_type="draw"),
        "image_get",
        {"page": 0},
    )
    assert draw_prep.error is None
    assert draw_prep.ignored == []
    assert draw_prep.kwargs == {"page": 0}

    calc_props = (tool.get_parameters("calc") or {})["properties"]
    assert "page" not in calc_props
    assert "sheet_index" in calc_props
    assert "page" in to_openai_schema(tool, doc_type="writer")["function"]["parameters"]["properties"]


def test_execute_ambiguous_graphic_name_returns_code():
    from plugin.doc.visual_helpers import AmbiguousGraphicNameError

    doc = FakeDrawDoc()
    err = AmbiguousGraphicNameError("dup", sheet_indices=[0, 1])
    with patch("plugin.doc.visual_helpers.get_graphic_object_by_name", side_effect=err):
        res = GetImage().execute(_tctx(doc), image="Logo")
    assert res["status"] == "error"
    assert res["code"] == "AMBIGUOUS_NAME"


def test_get_image_supports_calc_document():
    from plugin.framework.doc_type import CALC_DOCUMENT_SERVICE
    from plugin.framework.tool import tool_supports_document

    assert tool_supports_document(
        GetImage(),
        doc_type="calc",
        uno_services_supported=frozenset({CALC_DOCUMENT_SERVICE}),
    ) is True


def test_draw_image_get_schema_names_only_draw_reads():
    tool = GetImage()
    before = copy.deepcopy(GetImage.parameters)
    text = tool.get_description("draw") + json.dumps(tool.get_parameters("draw"))
    for forbidden in ("get_content", "get_sheet_summary", "image_list", "Writer", "Calc", "slide", "sheet_index"):
        assert forbidden not in text
    assert "shape_get_tree" in text
    assert GetImage.parameters == before


def test_writer_image_get_schema_names_no_draw_or_calc_read():
    tool = GetImage()
    text = tool.get_description("writer") + json.dumps(tool.get_parameters("writer"))
    for forbidden in ("shape_get_tree", "get_sheet_summary", "Draw", "Calc", "slide", "sheet_index"):
        assert forbidden not in text


def test_calc_image_get_schema_excludes_page_and_non_calc_reads():
    tool = GetImage()
    before = copy.deepcopy(GetImage.parameters)
    text = tool.get_description("calc") + json.dumps(tool.get_parameters("calc"))
    for forbidden in ("page", "shape_get_tree", "get_content", "get_sheet_summary", "Draw", "Writer"):
        assert forbidden not in text
    assert "image_list" in text
    assert "sheet_index" in text
    assert GetImage.parameters == before


def test_calc_execute_rejects_image_and_selection_without_mentioning_page():
    res = GetImage().execute(_tctx(FakeDrawDoc(), doc_type="calc"), image="Logo", selection=True)
    assert res["status"] == "error"
    assert "at most one" in res["message"]
    assert "page" not in res["message"]
