# WriterAgent - AI Writing Assistant for LibreOffice
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for shapes specialized sub-agent canvas context."""

import sys
from unittest.mock import MagicMock, patch

import pytest

from plugin.doc.specialized_catalog import InnerToolCall
from plugin.doc.specialized_shapes_context import format_shapes_canvas_context, summarize_shape_calls
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
    assert "21000 x 29700" in s
    assert "2000/2000/2500/2500" in s


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


@pytest.mark.parametrize(
    ("service", "width", "height", "page_num", "page_count", "expected_snippets"),
    [
        pytest.param(
            "com.sun.star.drawing.DrawingDocument",
            28_000,
            20_000,
            2,  # 1-based slide number -> index 1
            5,
            ["Document canvas (Draw):", "28000 x 20000", "index 1", "user's page 2", "5 page"],
            id="draw",
        ),
        pytest.param(
            "com.sun.star.presentation.PresentationDocument",
            25_400,
            19_050,
            1,
            3,
            ["Document canvas (Impress):", "25400 x 19050", "user's page 1"],
            id="impress",
        ),
    ],
)
def test_format_shapes_canvas_context_active_slide(
    service: str,
    width: int,
    height: int,
    page_num: int,
    page_count: int,
    expected_snippets: list[str],
):
    doc = MagicMock()
    doc.supportsService = lambda svc: svc == service

    page = MagicMock()
    page.Width = width
    page.Height = height
    page.getNumber = lambda: page_num

    pages = MagicMock()
    pages.getCount.return_value = page_count
    pages.getByIndex = MagicMock(return_value=page)
    doc.getDrawPages.return_value = pages

    ctrl = MagicMock()
    ctrl.getCurrentPage.return_value = page
    doc.getCurrentController.return_value = ctrl

    s = format_shapes_canvas_context(doc)
    for snippet in expected_snippets:
        assert snippet in s


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


def _issame(left: object, right: object) -> bool:
    return (
        isinstance(left, _DistinctSheet)
        and isinstance(right, _DistinctSheet)
        and left._ident == right._ident
    )


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

    with patch.object(sys.modules["uno"], "isSame", _issame):
        s = format_shapes_canvas_context(doc)
    assert "Calc" in s
    assert "Sheet2" in s
    assert "30000 x 20000" in s
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

    with patch.object(sys.modules["uno"], "isSame", _issame):
        s = format_shapes_canvas_context(doc)
    assert s != ""
    assert "no fixed page size" in s
    assert "index 1 (0-based)" in s
    assert "Sheet2" in s
    assert "0 x 0" not in s


def test_draw_canvas_reports_page_size_in_shape_units():
    doc = MagicMock()
    doc.supportsService = lambda svc: svc == "com.sun.star.drawing.DrawingDocument"

    page = MagicMock()
    page.Width = 15875
    page.Height = 10000
    page.getNumber = lambda: 1

    pages = MagicMock()
    pages.getCount.return_value = 1
    pages.getByIndex = MagicMock(return_value=page)
    doc.getDrawPages.return_value = pages

    ctrl = MagicMock()
    ctrl.getCurrentPage.return_value = page
    doc.getCurrentController.return_value = ctrl

    s = format_shapes_canvas_context(doc)
    assert "15875 x 10000" in s


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


def test_summarize_shape_calls_counts_creates_connectors_and_diagram():
    outside_warning = "Shape extends outside page bounds."
    calls = [
        InnerToolCall(
            "shape_upsert",
            {
                "status": "ok",
                "action": "create",
                "name": "Start",
                "page": 0,
                "index": 0,
                "warnings": [outside_warning],
            },
            True,
        ),
        InnerToolCall(
            "shape_upsert",
            {"status": "ok", "action": "create", "name": "", "page": 0, "index": 1},
            True,
        ),
        InnerToolCall(
            "shape_connect",
            {"status": "ok", "page": 0, "index": 2},
            True,
        ),
        InnerToolCall(
            "shape_create_diagram",
            {
                "status": "ok",
                "page": 1,
                "nodes": [{"id": "a", "index": 0}, {"id": "b", "index": 1}],
                "connections": [{"from": "a", "to": "b", "index": 2}],
            },
            True,
        ),
        InnerToolCall(
            "shape_get_summary",
            {"status": "ok", "page": 3, "shapes": []},
            False,
        ),
    ]

    summary = summarize_shape_calls(calls)
    assert summary == {
        "shapes_created": 4,
        "connectors_created": 2,
        "shapes_changed": 0,
        "shapes_deleted": 0,
        "pages": [0, 1],
        "created_names": ["Start"],
        "warnings": [outside_warning],
    }


def test_summarize_shape_calls_error_results_count_nothing():
    calls = [
        InnerToolCall(
            "shape_upsert",
            {"status": "error", "message": "Invalid size", "page": 0},
            True,
        ),
    ]
    summary = summarize_shape_calls(calls)
    assert summary == {
        "shapes_created": 0,
        "connectors_created": 0,
        "shapes_changed": 0,
        "shapes_deleted": 0,
    }


def test_summarize_shape_calls_edits_fill_and_delete():
    calls = [
        InnerToolCall(
            "shape_upsert",
            {"status": "ok", "action": "edit", "name": "Title", "page": 0, "index": 0},
            True,
        ),
        InnerToolCall(
            "shape_fill_fields",
            {
                "status": "partial",
                "page": 0,
                "ok_count": 2,
                "fail_count": 1,
                "results": [
                    {"ok": True, "name": "FieldA"},
                    {"ok": True, "name": "FieldB"},
                    {"ok": False, "name": "MissingField"},
                ],
            },
            True,
        ),
        InnerToolCall(
            "shape_align",
            {"status": "ok", "page": 0, "indices": [0, 1, 2]},
            True,
        ),
        InnerToolCall(
            "shape_group",
            {"status": "ok", "page": 0, "index": 3},
            True,
        ),
        InnerToolCall(
            "shape_delete",
            {"status": "ok", "page": 0},
            True,
        ),
    ]

    summary = summarize_shape_calls(calls)
    assert summary["shapes_created"] == 0
    assert summary["connectors_created"] == 0
    assert summary["shapes_changed"] == 7
    assert summary["shapes_deleted"] == 1
    assert summary["changed_names"] == ["Title", "FieldA", "FieldB"]
    assert summary["pages"] == [0]
    assert len(summary["warnings"]) == 1
    assert "MissingField" in summary["warnings"][0]


def test_summarize_shape_calls_collects_singular_geometry_warning_and_caps_names():
    calls = [
        InnerToolCall(
            "shape_upsert",
            {
                "status": "ok",
                "action": "create",
                "name": "First",
                "page": 0,
                "warning": "Custom shape geometry failed: x",
            },
            True,
        ),
    ]
    for i in range(50):
        calls.append(
            InnerToolCall(
                "shape_upsert",
                {
                    "status": "ok",
                    "action": "create",
                    "name": f"Box{i}",
                    "page": 0,
                    "warning": "Custom shape geometry failed: x",
                },
                True,
            )
        )

    summary = summarize_shape_calls(calls)
    assert summary["shapes_created"] == 51
    assert len(summary["created_names"]) == 40
    assert summary["warnings"] == ["Custom shape geometry failed: x"]


def test_summarize_shape_calls_fill_failure_at_index_zero_and_unlabeled():
    calls = [
        InnerToolCall(
            "shape_fill_fields",
            {
                "status": "partial",
                "page": 0,
                "ok_count": 0,
                "fail_count": 2,
                "results": [
                    {"ok": False, "name": "", "index": 0},
                    {"ok": False},
                ],
            },
            True,
        ),
    ]

    summary = summarize_shape_calls(calls)
    assert len(summary["warnings"]) == 1
    warning = summary["warnings"][0]
    assert "?" not in warning
    assert "0" in warning
    assert "field 1" in warning
