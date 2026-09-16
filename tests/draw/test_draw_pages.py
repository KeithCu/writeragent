# WriterAgent — unit tests for core slide lifecycle tools
# SPDX-License-Identifier: GPL-3.0-or-later

from unittest.mock import MagicMock, patch

from plugin.draw.bridge import DrawBridge
from plugin.draw.pages import AddSlide, DuplicateSlide, MoveSlide, RenameSlide


def _ctx():
    return MagicMock()


def _impress_ctx():
    ctx = MagicMock()
    ctx.doc.supportsService.return_value = True
    return ctx


def _draw_ctx():
    ctx = MagicMock()
    ctx.doc.supportsService.return_value = False
    return ctx


def _add_slide_bridge(page, active_idx=1):
    bridge = MagicMock()
    bridge.create_slide.return_value = page
    bridge.get_active_page_index.return_value = active_idx
    return bridge


def test_add_slide_impress_defaults_to_text_layout():
    ctx = _impress_ctx()
    page = MagicMock()
    page.Layout = 20
    with patch("plugin.draw.bridge.DrawBridge") as bridge_cls:
        bridge_cls.return_value = _add_slide_bridge(page, active_idx=1)
        out = AddSlide().execute(ctx)
    assert out["status"] == "ok"
    assert out["layout"] == "text"
    assert out["active_page_index"] == 1
    assert "list_placeholders" in out["placeholders_hint"]
    assert page.Layout == 1


def test_add_slide_blank_and_none_escape():
    ctx = _impress_ctx()
    page = MagicMock()
    page.Layout = 20
    with patch("plugin.draw.bridge.DrawBridge") as bridge_cls:
        bridge_cls.return_value = _add_slide_bridge(page)
        out = AddSlide().execute(ctx, layout="none")
    assert out["status"] == "ok"
    assert out["layout"] == "blank"
    assert page.Layout == 11

    page.Layout = 20
    with patch("plugin.draw.bridge.DrawBridge") as bridge_cls:
        bridge_cls.return_value = _add_slide_bridge(page)
        out = AddSlide().execute(ctx, layout="blank")
    assert out["layout"] == "blank"
    assert page.Layout == 11


def test_add_slide_draw_ignores_layout():
    ctx = _draw_ctx()
    page = MagicMock()
    page.Layout = 99
    with patch("plugin.draw.bridge.DrawBridge") as bridge_cls:
        bridge_cls.return_value = _add_slide_bridge(page, active_idx=0)
        out = AddSlide().execute(ctx, layout="text")
    assert out["status"] == "ok"
    assert "layout" not in out
    assert "placeholders_hint" not in out
    assert page.Layout == 99


def test_add_slide_unknown_layout_errors_before_create():
    ctx = _impress_ctx()
    with patch("plugin.draw.bridge.DrawBridge") as bridge_cls:
        out = AddSlide().execute(ctx, layout="not_a_layout")
    assert out["status"] == "error"
    bridge_cls.return_value.create_slide.assert_not_called()


def test_bridge_duplicate_calls_doc_duplicate():
    source = object()
    copy = object()
    pages = MagicMock()
    pages.getCount.return_value = 2
    pages.getByIndex.return_value = source
    doc = MagicMock()
    doc.getDrawPages.return_value = pages
    doc.duplicate.return_value = copy
    doc.getCurrentController.return_value = None
    bridge = DrawBridge(doc)
    out = bridge.duplicate_slide(0, switch=True)
    doc.duplicate.assert_called_once_with(source)
    assert out is copy


def test_duplicate_slide_ok():
    ctx = _ctx()
    with patch("plugin.draw.bridge.DrawBridge") as bridge_cls:
        bridge = bridge_cls.return_value
        bridge.get_pages.return_value.getCount.return_value = 2
        bridge.get_active_page_index.return_value = 1
        out = DuplicateSlide().execute(ctx, page=0)
    assert out["status"] == "ok"
    bridge.duplicate_slide.assert_called_once_with(0, switch=True)
    assert out["active_page_index"] == 1


def test_duplicate_slide_out_of_range():
    ctx = _ctx()
    with patch("plugin.draw.bridge.DrawBridge") as bridge_cls:
        bridge_cls.return_value.get_pages.return_value.getCount.return_value = 1
        out = DuplicateSlide().execute(ctx, page=5)
    assert out["status"] == "error"


def test_move_slide_failure():
    ctx = _ctx()
    with patch("plugin.draw.bridge.DrawBridge") as bridge_cls:
        bridge_cls.return_value.move_slide.return_value = False
        out = MoveSlide().execute(ctx, from_page=0, to_page=2)
    assert out["status"] == "error"


def test_move_slide_ok():
    ctx = _ctx()
    with patch("plugin.draw.bridge.DrawBridge") as bridge_cls:
        bridge = bridge_cls.return_value
        bridge.move_slide.return_value = True
        bridge.get_active_page_index.return_value = 1
        out = MoveSlide().execute(ctx, from_page=0, to_page=1)
    assert out["status"] == "ok"
    bridge.move_slide.assert_called_once_with(0, 1)


def test_rename_slide_ok():
    ctx = _ctx()
    with patch("plugin.draw.bridge.DrawBridge") as bridge_cls:
        bridge = bridge_cls.return_value
        bridge.get_pages.return_value.getCount.return_value = 3
        bridge.rename_slide.return_value = True
        bridge.get_active_page_index.return_value = 0
        out = RenameSlide().execute(ctx, page=2, name="Agenda")
    assert out["status"] == "ok"
    assert out["name"] == "Agenda"
    bridge.rename_slide.assert_called_once_with(2, "Agenda")


def test_rename_slide_missing_name():
    out = RenameSlide().execute(_ctx(), page=0, name="")
    assert out["status"] == "error"
