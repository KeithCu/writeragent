# WriterAgent - AI Writing Assistant for LibreOffice
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for shapes specialized sub-agent canvas context."""

import sys
from unittest.mock import MagicMock, patch

import pytest

from plugin.doc.specialized_shapes_context import format_shapes_canvas_context
from plugin.framework.errors import DocumentDisposedError


def test_format_shapes_canvas_context_writer_standard_page():
    doc = MagicMock()

    def supports(svc: str) -> bool:
        return svc == "com.sun.star.text.TextDocument"

    doc.supportsService = supports
    doc.getCurrentController.return_value = None

    style = MagicMock()

    def gv(name: str):
        return {
            "Width": 21_000,
            "Height": 29_700,
            "LeftMargin": 2_000,
            "RightMargin": 2_000,
            "TopMargin": 2_500,
            "BottomMargin": 2_500,
            "IsLandscape": False,
        }[name]

    style.getPropertyValue = gv
    page_styles = MagicMock()
    page_styles.hasByName = lambda n: n == "Standard"
    page_styles.getByName = lambda n: style if n == "Standard" else MagicMock()
    families = MagicMock()
    families.getByName = lambda n: page_styles if n == "PageStyles" else MagicMock()
    doc.getStyleFamilies.return_value = families

    s = format_shapes_canvas_context(doc)
    assert "Writer" in s
    assert "Standard" in s
    assert "210.0" in s
    assert "297.0" in s
    assert "1/100 mm" in s
    assert "printable" in s


def test_writer_page_style_uses_view_cursor_text():
    doc = MagicMock()
    doc.supportsService = lambda svc: svc == "com.sun.star.text.TextDocument"
    doc.getText = MagicMock()

    ctrl = MagicMock()
    vc = MagicMock()
    fake_text = MagicMock()
    cursor = MagicMock()

    psi = MagicMock()
    psi.hasPropertyByName = MagicMock(side_effect=lambda prop: prop == "PageDescName")
    cursor.getPropertySetInfo = MagicMock(return_value=psi)
    cursor.getPropertyValue = MagicMock(side_effect=lambda prop: "Annex" if prop == "PageDescName" else None)
    cursor.gotoStartOfParagraph = MagicMock(return_value=None)

    fake_text.createTextCursorByRange = MagicMock(return_value=cursor)
    vc.getText = MagicMock(return_value=fake_text)
    vc.getStart = MagicMock(return_value=MagicMock())
    ctrl.getViewCursor = MagicMock(return_value=vc)
    doc.getCurrentController = MagicMock(return_value=ctrl)

    style = MagicMock()
    style.getPropertyValue = MagicMock(
        side_effect=lambda name: {
            "Width": 21_000,
            "Height": 29_700,
            "LeftMargin": 2_000,
            "RightMargin": 2_000,
            "TopMargin": 2_500,
            "BottomMargin": 2_500,
            "IsLandscape": False,
        }[name]
    )
    page_styles = MagicMock()
    page_styles.hasByName = MagicMock(side_effect=lambda n: n == "Annex")
    page_styles.getByName = MagicMock(side_effect=lambda n: style if n == "Annex" else None)
    families = MagicMock()
    families.getByName = MagicMock(side_effect=lambda n: page_styles if n == "PageStyles" else None)
    doc.getStyleFamilies = MagicMock(return_value=families)

    s = format_shapes_canvas_context(doc)
    assert "Annex" in s
    doc.getText.assert_not_called()


def test_format_shapes_canvas_context_draw_active_slide():
    doc = MagicMock()

    def supports(svc: str) -> bool:
        return svc == "com.sun.star.drawing.DrawingDocument"

    doc.supportsService = supports

    page = MagicMock()
    page.Width = 28_000
    page.Height = 20_000
    page.getNumber = lambda: 2  # 1-based slide number -> index 1

    pages = MagicMock()
    pages.getCount.return_value = 5
    pages.getByIndex = MagicMock(return_value=page)

    doc.getDrawPages.return_value = pages

    ctrl = MagicMock()
    ctrl.getCurrentPage.return_value = page
    doc.getCurrentController.return_value = ctrl

    s = format_shapes_canvas_context(doc)
    assert "Document canvas (Draw):" in s
    assert "280.0" in s
    assert "200.0" in s
    assert "index 1" in s
    assert "5 page" in s or "5 page(s)" in s


def test_format_shapes_canvas_context_impress_active_slide():
    doc = MagicMock()

    def supports(svc: str) -> bool:
        return svc == "com.sun.star.presentation.PresentationDocument"

    doc.supportsService = supports

    page = MagicMock()
    page.Width = 25_400
    page.Height = 19_050
    page.getNumber = lambda: 1

    pages = MagicMock()
    pages.getCount.return_value = 3
    pages.getByIndex = MagicMock(return_value=page)

    doc.getDrawPages.return_value = pages

    ctrl = MagicMock()
    ctrl.getCurrentPage.return_value = page
    doc.getCurrentController.return_value = ctrl

    s = format_shapes_canvas_context(doc)
    assert "Document canvas (Impress):" in s
    assert "254.0" in s
    assert "190.5" in s


class _DistinctSheet:
    """PyUNO-style wrapper: distinct objects, ``==`` misses, identity is ``_ident``."""

    def __init__(self, name: str, ident: int, draw_page: object | None = None) -> None:
        self.Name = name
        self._ident = ident
        self._draw_page = draw_page

    def getDrawPage(self) -> object | None:
        return self._draw_page

    def __eq__(self, other: object) -> bool:
        return False


def test_format_shapes_canvas_context_calc_active_sheet():
    """Active sheet index follows uno identity, not wrapper ``is`` / ``==``."""
    doc = MagicMock()

    def supports(svc: str) -> bool:
        return svc == "com.sun.star.sheet.SpreadsheetDocument"

    doc.supportsService = supports

    draw_page = MagicMock()
    draw_page.Width = 30_000
    draw_page.Height = 20_000

    # getActiveSheet and getByIndex(1) are different wrappers for sheet 2.
    active = _DistinctSheet("Sheet2", ident=2, draw_page=draw_page)
    indexed = [
        _DistinctSheet("Sheet1", ident=1),
        _DistinctSheet("Sheet2", ident=2),
    ]
    assert active is not indexed[1]
    assert (active == indexed[1]) is False

    sheets = MagicMock()
    sheets.getCount.return_value = 2
    sheets.getByIndex = MagicMock(side_effect=lambda i: indexed[i])

    doc.getSheets.return_value = sheets
    ctrl = MagicMock()
    ctrl.getActiveSheet.return_value = active
    doc.getCurrentController.return_value = ctrl

    def _issame(left: object, right: object) -> bool:
        return (
            isinstance(left, _DistinctSheet)
            and isinstance(right, _DistinctSheet)
            and left._ident == right._ident
        )

    with patch.object(sys.modules["uno"], "isSame", _issame):
        s = format_shapes_canvas_context(doc)
    assert "Calc" in s
    assert "Sheet2" in s
    assert "300.0" in s
    assert "200.0" in s
    assert "draw-page" in s
    assert "index 1 (0-based)" in s
    assert "1/100 mm" in s


def test_format_shapes_canvas_context_calc_zero_size_draw_page():
    doc = MagicMock()
    doc.supportsService = lambda svc: svc == "com.sun.star.sheet.SpreadsheetDocument"

    draw_page = MagicMock()
    draw_page.Width = 0
    draw_page.Height = 0

    active = _DistinctSheet("Sheet2", ident=2, draw_page=draw_page)
    indexed = [
        _DistinctSheet("Sheet1", ident=1),
        _DistinctSheet("Sheet2", ident=2),
    ]

    sheets = MagicMock()
    sheets.getCount.return_value = 2
    sheets.getByIndex = MagicMock(side_effect=lambda i: indexed[i])

    doc.getSheets.return_value = sheets
    ctrl = MagicMock()
    ctrl.getActiveSheet.return_value = active
    doc.getCurrentController.return_value = ctrl

    def _issame(left: object, right: object) -> bool:
        return (
            isinstance(left, _DistinctSheet)
            and isinstance(right, _DistinctSheet)
            and left._ident == right._ident
        )

    with patch.object(sys.modules["uno"], "isSame", _issame):
        s = format_shapes_canvas_context(doc)
    assert s != ""
    assert "no fixed page size" in s
    assert "index 1 (0-based)" in s
    assert "Sheet2" in s
    assert "1/100 mm" in s
    assert "0.0 x 0.0" not in s


def test_format_shapes_canvas_context_none_doc():
    assert format_shapes_canvas_context(None) == ""


def test_format_shapes_canvas_context_reraises_disposed():
    class DisposedException(Exception):
        pass

    doc = MagicMock()
    doc.supportsService.side_effect = lambda svc: svc == "com.sun.star.drawing.DrawingDocument"
    doc.getDrawPages.side_effect = DisposedException("closed")

    with pytest.raises(DocumentDisposedError):
        format_shapes_canvas_context(doc)
    assert format_shapes_canvas_context(None) == ""


def test_format_shapes_canvas_context_ordinary_failure_returns_empty():
    doc = MagicMock()
    doc.supportsService.side_effect = lambda svc: svc == "com.sun.star.text.TextDocument"
    doc.getCurrentController.return_value = None
    doc.getStyleFamilies.side_effect = RuntimeError("UNO bridge broken")

    assert format_shapes_canvas_context(doc) == ""


def test_format_shapes_canvas_context_asserts_main_thread():
    with patch("plugin.doc.specialized_shapes_context.assert_main_thread", side_effect=RuntimeError("off thread")):
        with pytest.raises(RuntimeError, match="off thread"):
            format_shapes_canvas_context(MagicMock())
