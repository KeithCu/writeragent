# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock
import pytest

from plugin.writer import review_click_popup


@pytest.fixture(autouse=True)
def clean_attached() -> Any:
    review_click_popup._attached.clear()
    yield
    review_click_popup._attached.clear()


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


class FakeHandler:
    def __init__(self, model: Any, controller: Any) -> None:
        self._model = model
        self._controller = controller

    def disposing(self, source: Any) -> None:  # noqa: N802, N803 -- UNO API
        ctrl = review_click_popup._detach(self)
        if ctrl is None:
            return
        try:
            if ctrl.getModel() is not None:
                ctrl.removeMouseClickHandler(self)
        except Exception:
            pass


def test_registration_dedup_and_detach(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(review_click_popup, "_make_handler", lambda mdl, ctrl: FakeHandler(mdl, ctrl))

    ctrl = FakeController()
    review_click_popup.register_click_review(ctrl)
    assert len(review_click_popup._attached) == 1
    assert ctrl.add_count == 1

    # Registering again is a no-op
    review_click_popup.register_click_review(ctrl)
    assert len(review_click_popup._attached) == 1
    assert ctrl.add_count == 1

    # _detach
    handler = review_click_popup._attached[0][1]
    detached_ctrl = review_click_popup._detach(handler)
    assert detached_ctrl is ctrl
    assert len(review_click_popup._attached) == 0

    # _detach when not present
    assert review_click_popup._detach(handler) is None


def test_handler_disposing_calls_remove_mouse_click_handler(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(review_click_popup, "_make_handler", lambda mdl, ctrl: FakeHandler(mdl, ctrl))

    ctrl = FakeController()
    review_click_popup.register_click_review(ctrl)
    assert len(review_click_popup._attached) == 1

    handler = review_click_popup._attached[0][1]
    handler.disposing(ctrl)

    assert ctrl.remove_count == 1
    assert len(review_click_popup._attached) == 0


def test_cleanup_disposed_controllers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(review_click_popup, "_make_handler", lambda mdl, ctrl: FakeHandler(mdl, ctrl))

    live_ctrl = FakeController(model=object())
    dead_ctrl = FakeController(model=None)

    review_click_popup.register_click_review(live_ctrl)
    review_click_popup.register_click_review(dead_ctrl)
    assert len(review_click_popup._attached) == 2

    review_click_popup.cleanup_disposed_controllers()
    assert len(review_click_popup._attached) == 1
    assert review_click_popup._attached[0][0] is live_ctrl
