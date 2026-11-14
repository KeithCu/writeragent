# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock
import pytest

from plugin.writer import review_click_popup


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
    monkeypatch.setattr("plugin.writer.review_click_popup.get_ctx", lambda: ctx)

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
    monkeypatch.setattr("plugin.writer.review_click_popup.get_ctx", lambda: ctx)

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
    monkeypatch.setattr("plugin.writer.review_click_popup.get_ctx", lambda: ctx)

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

    monkeypatch.setattr("plugin.writer.review_click_popup.resolve_change_at_cursor", _fake_resolve)
    monkeypatch.setattr("plugin.writer.review_click_popup.cursor_in_agent_change", lambda mdl, view=None: "wa-review:s:1")

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
    monkeypatch.setattr("plugin.writer.review_click_popup.get_ctx", lambda: ctx)

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
    monkeypatch.setattr("plugin.writer.review_click_popup.resolve_change_at_cursor", resolve_mock)
    monkeypatch.setattr("plugin.writer.review_click_popup.cursor_in_agent_change", lambda mdl, view=None: "wa-review:s:1")
    monkeypatch.setattr("plugin.writer.review_click_popup.scan_redline_view", lambda mdl: MagicMock())

    review_click_popup._show_popup_and_resolve(model, object(), 10, 20)

    popup.execute.assert_called_once()
    resolve_mock.assert_not_called()


def _click(x: int, y: int, count: int) -> Any:
    return MagicMock(X=x, Y=y, ClickCount=count, Buttons=1, PopupTrigger=False)


def test_double_click_cancels_popup_and_never_consumes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Double-click: the first release must not open the modal popup (it swallowed the 2nd click)."""
    monkeypatch.setattr(review_click_popup, "_DOUBLE_CLICK_SEC", 0.0)
    shown: list[tuple[int, int]] = []
    deferred_calls: list[tuple[Any, tuple[Any, ...]]] = []

    monkeypatch.setattr(
        review_click_popup,
        "run_in_background",
        lambda fn, *args, **kwargs: deferred_calls.append((fn, args)),
    )
    monkeypatch.setattr(review_click_popup, "_show_popup_and_resolve", lambda _m, _s, x, y: shown.append((x, y)))
    monkeypatch.setattr("plugin.framework.queue_executor.main.post", lambda fn, *a: fn(*a))
    handler = review_click_popup.ClickReviewHandler(MagicMock(), detach=lambda handler: None)

    # Double-click: press/release/press/release inside the window -> no popup, nothing consumed.
    assert handler.mousePressed(_click(10, 10, 1)) is False
    assert handler.mouseReleased(_click(10, 10, 1)) is False
    assert handler.mousePressed(_click(10, 10, 2)) is False
    assert handler.mouseReleased(_click(10, 10, 2)) is False
    assert len(deferred_calls) == 1
    fn, args = deferred_calls.pop()
    fn(*args)
    assert shown == []

    # Plain single click still pops after the window.
    handler.mousePressed(_click(20, 30, 1))
    handler.mouseReleased(_click(20, 30, 1))
    assert len(deferred_calls) == 1
    fn, args = deferred_calls.pop()
    fn(*args)
    assert shown == [(20, 30)]
