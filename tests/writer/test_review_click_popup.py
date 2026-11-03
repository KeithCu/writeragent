# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
from __future__ import annotations

import time
from typing import Any
from unittest.mock import MagicMock
import pytest

from plugin.writer import review_click_popup, review_view_attachment as rva


@pytest.fixture(autouse=True)
def clean_attached() -> Any:
    rva.reset_all()
    yield
    rva.reset_all()


def test_same_click_point() -> None:
    # None pressed -> returns True
    assert review_click_popup._same_click_point(None, 100, 200) is True

    # Same point -> returns True
    assert review_click_popup._same_click_point((100, 200), 100, 200) is True

    # Within slop (<= 4px) -> returns True
    assert review_click_popup._same_click_point((100, 200), 104, 196) is True

    # Moved 10px -> returns False
    assert review_click_popup._same_click_point((100, 200), 110, 200) is False
    assert review_click_popup._same_click_point((100, 200), 100, 210) is False


def test_show_popup_uncollapsed_cursor_does_not_enumerate_or_create_popup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = MagicMock()
    monkeypatch.setattr("plugin.framework.uno_context.get_ctx", lambda: ctx)

    vc = MagicMock()
    vc.isCollapsed.return_value = False
    controller = MagicMock()
    controller.getViewCursor.return_value = vc

    redlines = MagicMock()
    model = MagicMock()
    model.getCurrentController.return_value = controller
    model.getRedlines.return_value = redlines

    review_click_popup._show_popup_and_resolve(model, object(), 10, 20)

    redlines.createEnumeration.assert_not_called()
    redlines.getCount.assert_not_called()
    ctx.getServiceManager.assert_not_called()


def test_show_popup_zero_redlines_does_not_enumerate_or_create_popup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = MagicMock()
    monkeypatch.setattr("plugin.framework.uno_context.get_ctx", lambda: ctx)

    vc = MagicMock()
    vc.isCollapsed.return_value = True
    controller = MagicMock()
    controller.getViewCursor.return_value = vc

    redlines = MagicMock()
    redlines.getCount.return_value = 0
    redlines.createEnumeration.side_effect = AssertionError("should not enumerate when count is 0")

    model = MagicMock()
    model.getCurrentController.return_value = controller
    model.getRedlines.return_value = redlines

    review_click_popup._show_popup_and_resolve(model, object(), 10, 20)

    redlines.getCount.assert_called_once()
    redlines.createEnumeration.assert_not_called()
    ctx.getServiceManager.assert_not_called()


def test_show_popup_plain_click_creates_popup_and_does_not_select_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = MagicMock()
    smgr = MagicMock()
    popup = MagicMock()
    popup.execute.return_value = 1  # Accept
    smgr.createInstanceWithContext.return_value = popup
    ctx.getServiceManager.return_value = smgr
    monkeypatch.setattr("plugin.framework.uno_context.get_ctx", lambda: ctx)

    vc = MagicMock()
    vc.isCollapsed.return_value = True
    controller = MagicMock()
    controller.getViewCursor.return_value = vc

    redline_enum = MagicMock()
    redline_enum.hasMoreElements.side_effect = [True, False]
    fake_rl = MagicMock()
    fake_rl.getPropertyValue.side_effect = lambda prop: {
        "RedlineComment": "wa-review:s:1",
        "RedlineType": "Insert",
    }.get(prop)
    redline_enum.nextElement.return_value = fake_rl

    redlines = MagicMock()
    redlines.getCount.return_value = 1
    redlines.createEnumeration.return_value = redline_enum

    model = MagicMock()
    model.getCurrentController.return_value = controller
    model.getRedlines.return_value = redlines

    resolved = []

    def _fake_resolve(mdl: Any, c: Any, accept: bool, token: str | None = None, view: Any = None) -> tuple[bool, str]:
        resolved.append((accept, token))
        return True, "ok"

    monkeypatch.setattr("plugin.writer.inline_review.resolve_change_at_cursor", _fake_resolve)
    monkeypatch.setattr("plugin.writer.inline_review.cursor_in_agent_change", lambda mdl, view=None: "wa-review:s:1")

    source_win = object()
    review_click_popup._show_popup_and_resolve(model, source_win, 10, 20)

    # Asserts
    vc.gotoRange.assert_not_called()
    assert redlines.createEnumeration.call_count == 1
    popup.execute.assert_called_once()
    assert resolved == [(True, "wa-review:s:1")]


def test_show_popup_dismissal_does_not_resolve(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = MagicMock()
    smgr = MagicMock()
    popup = MagicMock()
    popup.execute.return_value = 0  # Dismissed
    smgr.createInstanceWithContext.return_value = popup
    ctx.getServiceManager.return_value = smgr
    monkeypatch.setattr("plugin.framework.uno_context.get_ctx", lambda: ctx)

    vc = MagicMock()
    vc.isCollapsed.return_value = True
    controller = MagicMock()
    controller.getViewCursor.return_value = vc

    redlines = MagicMock()
    redlines.getCount.return_value = 1

    model = MagicMock()
    model.getCurrentController.return_value = controller
    model.getRedlines.return_value = redlines

    resolve_mock = MagicMock()
    monkeypatch.setattr("plugin.writer.inline_review.resolve_change_at_cursor", resolve_mock)
    monkeypatch.setattr("plugin.writer.inline_review.cursor_in_agent_change", lambda mdl, view=None: "wa-review:s:1")
    monkeypatch.setattr("plugin.writer.inline_review.scan_redline_view", lambda mdl: MagicMock())

    review_click_popup._show_popup_and_resolve(model, object(), 10, 20)

    popup.execute.assert_called_once()
    resolve_mock.assert_not_called()


class FakeController:
    def __init__(self, model: Any = object()) -> None:
        self.model = model
        self.add_count = 0
        self.remove_count = 0

    def addMouseClickHandler(self, handler: Any) -> None:  # noqa: N802 -- UNO API
        self.add_count += 1

    def removeMouseClickHandler(self, handler: Any) -> None:  # noqa: N802 -- UNO API
        self.remove_count += 1

    def getModel(self) -> Any:  # noqa: N802 -- UNO API
        return self.model

    def registerContextMenuInterceptor(self, interceptor: Any) -> None:  # noqa: N802 -- UNO API
        pass

    def releaseContextMenuInterceptor(self, interceptor: Any) -> None:  # noqa: N802 -- UNO API
        pass


class FakeHandler:
    def __init__(self, model: Any) -> None:
        self._model = model

    def disposing(self, source: Any) -> None:  # noqa: N802, N803 -- UNO API
        ctrl = rva.detach_click_handler(self)
        if ctrl is None:
            return
        try:
            if ctrl.getModel() is not None:
                ctrl.removeMouseClickHandler(self)
        except Exception:
            pass


def test_registration_dedup_and_detach(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(review_click_popup, "_make_handler", lambda mdl: FakeHandler(mdl))
    monkeypatch.setattr(
        "plugin.writer.change_context_menu._make_interceptor",
        lambda m: MagicMock(_model=m),
    )
    model = MagicMock()
    model.supportsService.return_value = True
    ctrl = FakeController(model=model)
    review_click_popup.register_click_review(ctrl)
    assert len(rva.registered_click_handlers()) == 1
    assert ctrl.add_count == 1

    # Registering again is a no-op
    review_click_popup.register_click_review(ctrl)
    assert len(rva.registered_click_handlers()) == 1
    assert ctrl.add_count == 1

    # detach_click_handler
    handler = rva.registered_click_handlers()[0][1]
    detached_ctrl = rva.detach_click_handler(handler)
    assert detached_ctrl is ctrl
    assert len(rva.registered_click_handlers()) == 0

    # detach_click_handler when not present
    assert rva.detach_click_handler(handler) is None


def test_handler_disposing_calls_remove_mouse_click_handler(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(review_click_popup, "_make_handler", lambda mdl: FakeHandler(mdl))
    monkeypatch.setattr(
        "plugin.writer.change_context_menu._make_interceptor",
        lambda m: MagicMock(_model=m),
    )
    model = MagicMock()
    model.supportsService.return_value = True
    ctrl = FakeController(model=model)
    review_click_popup.register_click_review(ctrl)
    assert len(rva.registered_click_handlers()) == 1

    handler = rva.registered_click_handlers()[0][1]
    handler.disposing(ctrl)

    assert ctrl.remove_count == 1
    assert len(rva.registered_click_handlers()) == 0


def test_cleanup_disposed_controllers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(review_click_popup, "_make_handler", lambda mdl: FakeHandler(mdl))
    monkeypatch.setattr(
        "plugin.writer.change_context_menu._make_interceptor",
        lambda m: MagicMock(_model=m),
    )
    live_model = MagicMock()
    live_model.supportsService.return_value = True
    live_ctrl = FakeController(model=live_model)
    dead_ctrl = FakeController(model=live_model)

    def _dead_get_model() -> Any:
        raise RuntimeError("disposed")

    dead_ctrl.getModel = _dead_get_model  # type: ignore[method-assign]

    review_click_popup.register_click_review(live_ctrl)
    key = rva.attachment_key(live_model)
    rva._attachments[key].controllers.append((dead_ctrl, MagicMock(), FakeHandler(live_model)))
    assert len(rva.registered_click_handlers()) == 2

    review_click_popup.cleanup_disposed_controllers()
    assert len(rva.registered_click_handlers()) == 1
    assert rva.registered_click_handlers()[0][0] is live_ctrl


def _click(x: int, y: int, count: int) -> Any:
    return MagicMock(X=x, Y=y, ClickCount=count, Buttons=1, PopupTrigger=False)


def test_double_click_cancels_popup_and_never_consumes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Double-click: the first release must not open the modal popup (it swallowed the 2nd click)."""
    import sys
    import types

    awt = types.ModuleType("com.sun.star.awt")
    awt.XMouseClickHandler = type("XMouseClickHandler", (), {})  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "com.sun.star.awt", awt)
    monkeypatch.setattr(review_click_popup, "_handler_cls", None)
    monkeypatch.setattr(review_click_popup, "_DOUBLE_CLICK_SEC", 0.05)
    shown: list[tuple[int, int]] = []
    monkeypatch.setattr(review_click_popup, "_show_popup_and_resolve", lambda _m, _s, x, y: shown.append((x, y)))
    monkeypatch.setattr("plugin.framework.queue_executor.main.post", lambda fn, *a: fn(*a))
    handler = review_click_popup._make_handler(MagicMock())

    # Double-click: press/release/press/release inside the window -> no popup, nothing consumed.
    assert handler.mousePressed(_click(10, 10, 1)) is False
    assert handler.mouseReleased(_click(10, 10, 1)) is False
    assert handler.mousePressed(_click(10, 10, 2)) is False
    assert handler.mouseReleased(_click(10, 10, 2)) is False
    time.sleep(0.2)
    assert shown == []

    # Plain single click still pops after the window.
    handler.mousePressed(_click(20, 30, 1))
    handler.mouseReleased(_click(20, 30, 1))
    time.sleep(0.2)
    assert shown == [(20, 30)]
