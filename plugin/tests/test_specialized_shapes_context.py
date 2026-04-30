# WriterAgent - AI Writing Assistant for LibreOffice
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for shapes specialized sub-agent canvas context."""

from unittest.mock import MagicMock

from plugin.tests.testing_utils import setup_uno_mocks

setup_uno_mocks()

from plugin.framework.specialized_shapes_context import format_shapes_canvas_context


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


def test_format_shapes_canvas_context_calc_active_sheet():
    doc = MagicMock()

    def supports(svc: str) -> bool:
        return svc == "com.sun.star.sheet.SpreadsheetDocument"

    doc.supportsService = supports

    draw_page = MagicMock()
    draw_page.Width = 30_000
    draw_page.Height = 20_000

    sheet = MagicMock()
    sheet.getDrawPage.return_value = draw_page
    sheet.Name = "Sheet1"

    sheets = MagicMock()
    sheets.getCount.return_value = 2
    sheets.getByIndex = MagicMock(side_effect=lambda i: sheet if i == 1 else MagicMock())

    doc.getSheets.return_value = sheets
    ctrl = MagicMock()
    ctrl.getActiveSheet.return_value = sheet
    doc.getCurrentController.return_value = ctrl

    s = format_shapes_canvas_context(doc)
    assert "Calc" in s
    assert "Sheet1" in s
    assert "300.0" in s
    assert "200.0" in s
    assert "draw-page" in s
    assert "index 1" in s
    assert "1/100 mm" in s


def test_format_shapes_canvas_context_none_doc():
    assert format_shapes_canvas_context(None) == ""
