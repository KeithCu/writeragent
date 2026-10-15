# WriterAgent — unit tests for core slide lifecycle tools
# SPDX-License-Identifier: GPL-3.0-or-later

from unittest.mock import MagicMock, patch

from plugin.draw.bridge import DrawBridge
from plugin.draw.pages import AddSlide, DeleteSlide, DuplicateSlide, ListPages, MoveSlide, RenameSlide, SetActivePage


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


def _add_slide_bridge(page, active_idx=1, page_count=1, landed=None):
    bridge = MagicMock()
    # create_slide returns the page and the slot it actually occupies.
    if landed is None:
        landed = page_count
    bridge.create_slide.return_value = (page, landed)
    bridge.get_active_page_index.return_value = active_idx
    bridge.get_pages.return_value.getCount.return_value = page_count
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
    assert page.Layout == 20

    page.Layout = 20
    with patch("plugin.draw.bridge.DrawBridge") as bridge_cls:
        bridge_cls.return_value = _add_slide_bridge(page)
        out = AddSlide().execute(ctx, layout="blank")
    assert out["layout"] == "blank"
    assert page.Layout == 20


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


def test_add_slide_reports_inserted_index_not_stale_active():
    """Impress get_active_page_index can stay 0; report the created page."""
    ctx = _impress_ctx()
    page = MagicMock()
    page.Layout = 20
    with patch("plugin.draw.bridge.DrawBridge") as bridge_cls:
        bridge_cls.return_value = _add_slide_bridge(page, active_idx=0, page_count=1)
        out = AddSlide().execute(ctx)
    assert out["active_page_index"] == 1
    assert out["layout"] == "text"


def test_add_slide_reports_landed_index_for_page_zero():
    """page=0 must report the slot create_slide actually filled, not a stale active page."""
    ctx = _impress_ctx()
    page = MagicMock()
    page.Layout = 20
    with (
        patch("plugin.draw.bridge.DrawBridge") as bridge_cls,
        patch("plugin.draw.designs.inherit_master_from_neighbor", return_value="Designed") as inherit,
    ):
        bridge_cls.return_value = _add_slide_bridge(page, active_idx=2, page_count=3, landed=0)
        out = AddSlide().execute(ctx, page=0)
    assert out["active_page_index"] == 0
    assert out["master"] == "Designed"
    assert inherit.call_args[0][2] == 0
    assert inherit.call_args[0][1] is page


def test_delete_slide_refuses_only_page():
    ctx = _ctx()
    with patch("plugin.draw.bridge.DrawBridge") as bridge_cls:
        bridge = bridge_cls.return_value
        bridge.get_pages.return_value.getCount.return_value = 1
        out = DeleteSlide().execute(ctx, page=0)
    assert out["status"] == "error"
    assert out["message"] == "Cannot delete the only slide"
    bridge.delete_slide.assert_not_called()


def test_delete_slide_removes_page_when_another_remains():
    ctx = _ctx()
    with patch("plugin.draw.bridge.DrawBridge") as bridge_cls:
        bridge = bridge_cls.return_value
        bridge.get_pages.return_value.getCount.return_value = 2
        bridge.get_active_page_index.return_value = 0
        out = DeleteSlide().execute(ctx, page=1)
    assert out["status"] == "ok"
    bridge.delete_slide.assert_called_once_with(1)
    assert out["active_page_index"] == 0


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


def test_duplicate_slide_rejects_none_copy():
    import pytest

    source = object()
    pages = MagicMock()
    pages.getCount.return_value = 2
    pages.getByIndex.return_value = source
    doc = MagicMock()
    doc.getDrawPages.return_value = pages
    doc.duplicate.return_value = None
    controller = MagicMock()
    doc.getCurrentController.return_value = controller
    bridge = DrawBridge(doc)
    with pytest.raises(RuntimeError, match="returned no page"):
        bridge.duplicate_slide(0, switch=True)
    controller.setCurrentPage.assert_not_called()


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


def test_move_slide_reports_rollback_message():
    from plugin.draw.bridge import PageExchangeRollbackError

    ctx = _ctx()
    with patch("plugin.draw.bridge.DrawBridge") as bridge_cls:
        bridge_cls.return_value.move_slide.side_effect = PageExchangeRollbackError("shapes are on the last slide")
        out = MoveSlide().execute(ctx, from_page=0, to_page=1)
    assert out["status"] == "error"
    assert "shapes are on the last slide" in out["message"]
    assert "Could not move slide" not in out["message"]


def test_add_slide_cleanup_reraises_dispose():
    import pytest

    class DisposedException(Exception):
        pass

    ctx = _impress_ctx()
    page = MagicMock()
    with (
        patch("plugin.draw.bridge.DrawBridge") as bridge_cls,
        patch("plugin.draw.designs.inherit_master_from_neighbor", side_effect=RuntimeError("no master")),
    ):
        bridge = _add_slide_bridge(page)
        bridge.get_pages.return_value.remove.side_effect = DisposedException("doc disposed")
        bridge_cls.return_value = bridge
        with pytest.raises(DisposedException):
            AddSlide().execute(ctx)


def test_add_slide_cleanup_still_returns_layout_error_when_remove_fails():
    ctx = _impress_ctx()
    page = MagicMock()
    with (
        patch("plugin.draw.bridge.DrawBridge") as bridge_cls,
        patch("plugin.draw.designs.inherit_master_from_neighbor", side_effect=RuntimeError("no master")),
    ):
        bridge = _add_slide_bridge(page)
        bridge.get_pages.return_value.remove.side_effect = RuntimeError("remove failed")
        bridge_cls.return_value = bridge
        out = AddSlide().execute(ctx)
    assert out["status"] == "error"
    assert "Failed to set layout or master" in out["message"]
    assert "no master" in out["message"]


def test_set_active_page_delegates_and_is_not_a_mutation():
    tool = SetActivePage()
    assert tool.is_mutation is False
    assert tool.detects_mutation() is False

    ctx = _ctx()
    with patch("plugin.draw.bridge.DrawBridge") as bridge_cls:
        bridge = bridge_cls.return_value
        bridge.get_pages.return_value.getCount.return_value = 3
        bridge.set_current_page_index.return_value = True
        out = tool.execute(ctx, page=1)
    assert out["status"] == "ok"
    assert out["active_page_index"] == 1
    bridge.set_current_page_index.assert_called_once_with(1)
    ctx.doc.getCurrentController.assert_not_called()


def test_set_active_page_reraises_dispose():
    import pytest

    class DisposedException(Exception):
        pass

    ctx = _ctx()
    with patch("plugin.draw.bridge.DrawBridge") as bridge_cls:
        bridge = bridge_cls.return_value
        bridge.get_pages.return_value.getCount.return_value = 3
        bridge.set_current_page_index.side_effect = DisposedException("controller disposed")
        with pytest.raises(DisposedException):
            SetActivePage().execute(ctx, page=1)


def test_set_active_page_false_is_tool_error():
    ctx = _ctx()
    with patch("plugin.draw.bridge.DrawBridge") as bridge_cls:
        bridge = bridge_cls.return_value
        bridge.get_pages.return_value.getCount.return_value = 3
        bridge.set_current_page_index.return_value = False
        out = SetActivePage().execute(ctx, page=1)
    assert out["status"] == "error"
    assert out["message"] == "Failed to set active page."
    assert "controller does not support" not in out["message"]


def test_set_active_page_out_of_range():
    ctx = _ctx()
    with patch("plugin.draw.bridge.DrawBridge") as bridge_cls:
        bridge = bridge_cls.return_value
        bridge.get_pages.return_value.getCount.return_value = 1
        out = SetActivePage().execute(ctx, page=5)
    assert out["status"] == "error"
    bridge.set_current_page_index.assert_not_called()


def test_add_slide_switches_only_after_layout():
    ctx = _impress_ctx()
    page = MagicMock()
    page.Layout = 20
    with patch("plugin.draw.bridge.DrawBridge") as bridge_cls:
        bridge = bridge_cls.return_value
        bridge.create_slide.return_value = (page, 4)
        bridge.get_pages.return_value.getCount.return_value = 4
        out = AddSlide().execute(ctx)
    assert out["status"] == "ok"
    assert out["active_page_index"] == 4
    bridge.create_slide.assert_called_once()
    assert bridge.create_slide.call_args.kwargs.get("switch") is False
    bridge.set_current_page_index.assert_called_once_with(4)
    method_names = [call[0] for call in bridge.method_calls]
    assert method_names.index("create_slide") < method_names.index("set_current_page_index")


def test_add_slide_skips_switch_when_post_insert_fails():
    ctx = _impress_ctx()
    page = MagicMock()
    with (
        patch("plugin.draw.bridge.DrawBridge") as bridge_cls,
        patch("plugin.draw.designs.inherit_master_from_neighbor", side_effect=RuntimeError("no master")),
    ):
        bridge = _add_slide_bridge(page)
        bridge_cls.return_value = bridge
        out = AddSlide().execute(ctx)
    assert out["status"] == "error"
    assert "Failed to set layout or master" in out["message"]
    assert "no master" in out["message"]
    assert bridge.create_slide.call_args.kwargs.get("switch") is False
    bridge.set_current_page_index.assert_not_called()


def test_add_slide_activate_false_does_not_switch():
    ctx = _impress_ctx()
    page = MagicMock()
    page.Layout = 20
    with patch("plugin.draw.bridge.DrawBridge") as bridge_cls:
        bridge = _add_slide_bridge(page, active_idx=2)
        bridge_cls.return_value = bridge
        out = AddSlide().execute(ctx, activate=False)
    assert out["status"] == "ok"
    assert out["active_page_index"] == 2
    bridge.set_current_page_index.assert_not_called()


def test_list_pages_uses_page_name():
    ctx = _ctx()
    p0 = MagicMock()
    p0.Name = "Agenda"
    p1 = MagicMock()
    p1.Name = ""
    p2 = MagicMock()
    p2.Name = "   "
    pages_mock = MagicMock()
    pages_mock.getCount.return_value = 3
    pages_mock.getByIndex.side_effect = [p0, p1, p2]

    with patch("plugin.draw.bridge.DrawBridge") as bridge_cls:
        bridge = bridge_cls.return_value
        bridge.get_pages.return_value = pages_mock
        bridge.get_active_page_index.return_value = 0
        out = ListPages().execute(ctx)
    assert out["status"] == "ok"
    assert out["pages"] == ["Agenda", "Page 1", "Page 2"]


def test_list_pages_reraises_dispose_from_name():
    import pytest

    class DisposedException(Exception):
        pass

    class _DisposedPage:
        @property
        def Name(self) -> str:
            raise DisposedException("page disposed")

    ctx = _ctx()
    pages_mock = MagicMock()
    pages_mock.getCount.return_value = 1
    pages_mock.getByIndex.return_value = _DisposedPage()

    with patch("plugin.draw.bridge.DrawBridge") as bridge_cls:
        bridge = bridge_cls.return_value
        bridge.get_pages.return_value = pages_mock
        with pytest.raises(DisposedException):
            ListPages().execute(ctx)
