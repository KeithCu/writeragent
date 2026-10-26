# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Click-to-review popup: left-click an agent change, get Accept/Reject right at the pointer.

The closest a LibreOffice extension gets to Google-Docs-style inline review buttons (true
floating hover widgets over the text canvas would require a core fork): an XMouseClickHandler
on the Writer view watches single left-clicks; when the caret lands inside an AGENT change
(identified by its wa-review session token -- the user's own redlines don't trigger it), a
small popup menu opens at the click position with Accept / Reject for that whole change.

The click itself is never consumed (the caret still moves normally); a popup opens only for a
plain click that leaves a collapsed caret (not a drag, Shift-click or word-select), and the click
never changes the selection. The check runs as a posted main-thread follow-up so LibreOffice
finishes its own click handling (caret placement) first. Registration mirrors change_context_menu:
per controller, UNO-identity dedup.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

import unohelper

log = logging.getLogger(__name__)

_CLICK_SLOP = 4
# Wait out the double-click window before opening the modal popup: opening it on the
# first release swallowed the second click, so double-click no longer selected a word.
_DOUBLE_CLICK_SEC = 0.5

_lock = threading.RLock()
_attached: list[tuple[Any, Any]] = []  # (controller, handler); strong refs until the view is disposed
_handler_cls: Any = None


def _same_click_point(pressed: tuple[int, int] | None, x: int, y: int) -> bool:
    if pressed is None:
        return True
    return abs(x - pressed[0]) <= _CLICK_SLOP and abs(y - pressed[1]) <= _CLICK_SLOP


def _detach(handler: Any) -> Any | None:
    with _lock:
        for idx, (c, h) in enumerate(_attached):
            if h is handler:
                _attached.pop(idx)
                return c
    return None


def _show_popup_and_resolve(model: Any, source_window: Any, x: int, y: int) -> None:
    """Runs on the main thread AFTER the click was processed by the view."""
    try:
        from plugin.framework.i18n import _
        from plugin.framework.uno_context import get_ctx
        from plugin.writer.inline_review import (
            cursor_in_agent_change,
            resolve_change_at_cursor,
            scan_redline_view,
            show_review_message,
        )

        ctx = get_ctx()
        if ctx is None:
            return

        try:
            cursor = model.getCurrentController().getViewCursor()
            if not cursor.isCollapsed():
                return
        except Exception:
            return

        try:
            if int(model.getRedlines().getCount()) == 0:
                return
        except Exception:  # noqa: S110 - fail open: unreadable count keeps full-scan path
            pass

        view = scan_redline_view(model)
        token = cursor_in_agent_change(model, view=view)
        if token is None:
            return

        from com.sun.star.awt import Rectangle

        smgr = getattr(ctx, "getServiceManager", lambda: None)()
        if smgr is None:
            return
        popup = smgr.createInstanceWithContext("com.sun.star.awt.PopupMenu", ctx)
        # Per-change only: Accept / Reject the change under the pointer. Bulk accept/reject ALL
        # intentionally lives on the review toolbar, not in this per-click popup.
        popup.insertItem(1, "✓ " + _("Accept this change"), 0, 0)
        popup.insertItem(2, "✗ " + _("Reject this change"), 0, 1)
        rect = Rectangle()
        rect.X = int(x)
        rect.Y = int(y)
        rect.Width = 0
        rect.Height = 0
        choice = popup.execute(source_window, rect, 0)
        accept = {1: True, 2: False}.get(choice)
        if accept is not None:
            ok, msg = resolve_change_at_cursor(model, ctx, accept, token=token, view=view)
            if not ok:
                show_review_message(ctx, msg)
    except Exception:
        log.warning("review_click_popup: popup failed", exc_info=True)


def _post_after_double_click_window(fire: Any, *args: Any) -> None:
    """Background: sleep out the double-click window, then hand *fire* to the main thread."""
    from plugin.framework.queue_executor import post_to_main_thread

    time.sleep(_DOUBLE_CLICK_SEC)
    post_to_main_thread(fire, *args)


def _make_handler(model: Any, controller: Any) -> Any:
    global _handler_cls
    if _handler_cls is None:
        from com.sun.star.awt import XMouseClickHandler

        class _ClickReviewHandler(unohelper.Base, XMouseClickHandler):  # type: ignore[misc, valid-type]
            _model: Any
            _controller: Any
            _press: tuple[int, int] | None
            _click_gen: int

            def __init__(self, model: Any, controller: Any) -> None:
                super().__init__()
                self._model = model
                self._controller = controller
                self._press = None
                self._click_gen = 0

            def _fire_if_single(self, gen: int, source: Any, x: int, y: int) -> None:
                """Main thread, after the double-click window: pop only if no later press."""
                if gen != self._click_gen:
                    return
                _show_popup_and_resolve(self._model, source, x, y)

            def mousePressed(self, e: Any) -> bool:  # noqa: N802 -- UNO API
                try:
                    self._press = (e.X, e.Y)
                    self._click_gen += 1  # a new press cancels any pending popup
                except Exception:
                    self._press = None
                return False  # never consume

            def mouseReleased(self, e: Any) -> bool:  # noqa: N802 -- UNO API
                try:
                    if e.ClickCount == 1 and e.Buttons == 1 and not e.PopupTrigger:  # plain left click
                        if not _same_click_point(self._press, e.X, e.Y):
                            return False
                        from plugin.framework.worker_pool import run_in_background

                        # Defer past the double-click window (a second press bumps _click_gen
                        # and cancels), so the view finishes caret placement / word select first.
                        run_in_background(_post_after_double_click_window, self._fire_if_single, self._click_gen, e.Source, e.X, e.Y, name="review-click-popup")
                except Exception:
                    log.debug("review_click_popup: mouseReleased failed", exc_info=True)
                return False  # never consume

            def disposing(self, Source: Any) -> None:  # noqa: N802, N803 -- UNO API
                controller = _detach(self)
                if controller is None:
                    return
                try:
                    if controller.getModel() is not None:
                        controller.removeMouseClickHandler(self)
                except Exception:  # noqa: S110 - best-effort cleanup on dispose
                    pass

        _handler_cls = _ClickReviewHandler
    return _handler_cls(model, controller)


def cleanup_disposed_controllers() -> None:
    """Passively remove any disposed controllers/handlers from tracking lists."""
    try:
        with _lock:
            alive: list[tuple[Any, Any]] = []
            for ctrl, handler in _attached:
                try:
                    if ctrl.getModel() is not None:
                        alive.append((ctrl, handler))
                except Exception:  # noqa: S110 - disposed controller; getModel() failing is the liveness check
                    pass
            _attached[:] = alive
    except Exception:
        log.debug("review_click_popup: cleanup failed", exc_info=True)


def register_click_review(controller: Any) -> None:
    """Attach the click handler to one Writer view controller (idempotent per controller)."""
    if controller is None:
        return
    cleanup_disposed_controllers()
    try:
        with _lock:
            if any(c == controller for c, _h in _attached):
                return
        if not hasattr(controller, "addMouseClickHandler"):
            return
        model = controller.getModel()
        handler = _make_handler(model, controller)
        with _lock:
            if any(c == controller for c, _h in _attached):
                return
            controller.addMouseClickHandler(handler)
            _attached.append((controller, handler))
        log.debug("review_click_popup: registered on Writer controller")
    except Exception:
        log.warning("review_click_popup: register failed", exc_info=True)
