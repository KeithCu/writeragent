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
import time
from typing import TYPE_CHECKING, Any

from plugin.framework.i18n import _
from plugin.framework.queue_executor import main
from plugin.framework.uno_context import get_ctx
from plugin.framework.uno_listeners import BaseMouseClickHandler
from plugin.framework.worker_pool import run_in_background
from plugin.writer.inline_review import (
    cursor_in_agent_change,
    resolve_change_at_cursor,
    scan_redline_view,
    show_review_message,
)

if TYPE_CHECKING:
    from collections.abc import Callable

log = logging.getLogger(__name__)

_CLICK_SLOP = 4
_DOUBLE_CLICK_SEC = 0.5


def _same_click_point(pressed: tuple[int, int] | None, x: int, y: int) -> bool:
    if pressed is None:
        return True
    return abs(x - pressed[0]) <= _CLICK_SLOP and abs(y - pressed[1]) <= _CLICK_SLOP


def _show_popup_and_resolve(model: Any, source_window: Any, x: int, y: int) -> None:
    try:
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

        # com.sun.star names resolve through PyUNO's import hook (pyuno/source/module/uno.py _uno_import); importing here keeps a missing type inside this try.
        from com.sun.star.awt import Rectangle

        smgr = ctx.getServiceManager()
        popup = smgr.createInstanceWithContext("com.sun.star.awt.PopupMenu", ctx)
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


class ClickReviewHandler(BaseMouseClickHandler):
    """Per-view click handler: a plain left click on an agent change opens the Accept/Reject popup."""

    _model: Any
    _detach: Callable[[Any], Any | None]
    _press: tuple[int, int] | None
    _click_gen: int

    def __init__(self, model: Any, detach: Callable[[Any], Any | None]) -> None:
        super().__init__()
        self._model = model
        self._detach = detach
        self._press = None
        self._click_gen = 0

    def _deferred(self, gen: int, source: Any, x: int, y: int) -> None:
        time.sleep(_DOUBLE_CLICK_SEC)

        def _fire() -> None:
            if gen != self._click_gen:
                return
            _show_popup_and_resolve(self._model, source, x, y)

        main.post(_fire)

    def on_mouse_pressed(self, e: Any) -> bool:
        # Reset coordinates on unreadable event attributes so release does not match a stale press.
        try:
            self._press = (e.X, e.Y)
            self._click_gen += 1
        except AttributeError:
            self._press = None
        return False

    def on_mouse_released(self, e: Any) -> bool:
        if e.ClickCount == 1 and e.Buttons == 1 and not e.PopupTrigger:
            if not _same_click_point(self._press, e.X, e.Y):
                return False

            run_in_background(
                self._deferred,
                self._click_gen,
                e.Source,
                e.X,
                e.Y,
                name="review-click-popup",
            )
        return False

    def on_disposing(self, Source: Any) -> None:  # noqa: N803 -- UNO API
        controller = self._detach(self)
        if controller is None:
            return
        try:
            if controller.getModel() is not None:
                controller.removeMouseClickHandler(self)
        except Exception:  # noqa: S110 - best-effort cleanup on dispose
            pass
