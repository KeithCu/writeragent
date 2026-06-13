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

The click itself is never consumed (the caret still moves normally), and the check runs as a
posted main-thread follow-up so LibreOffice finishes its own click handling (caret placement)
first. Registration mirrors change_context_menu: per controller, UNO-identity dedup.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

import unohelper

log = logging.getLogger(__name__)

_lock = threading.RLock()
_registered_controllers: list[Any] = []  # UNO-identity dedup (PyUNO proxies: use ==, not id());
# strong refs kept for the office session -- releasing on view dispose is follow-up work
_handlers: list[Any] = []  # strong refs
_handler_cls: Any = None


def _show_popup_and_resolve(model: Any, source_window: Any, x: int, y: int) -> None:
    """Runs on the main thread AFTER the click was processed by the view."""
    try:
        from plugin.framework.uno_context import get_ctx
        from plugin.framework.i18n import _
        from plugin.writer.inline_review import (
            agent_changes,
            cursor_in_agent_change,
            goto_agent_change,
            resolve_agent_change,
            resolve_all_agent_changes,
        )

        ctx = get_ctx()
        token = cursor_in_agent_change(model)
        if token is None:
            return

        # Select the whole edited region so it's highlighted while the user decides -- mirrors
        # the native Manage Changes dialog, where picking a change selects it in the document.
        goto_agent_change(model, token)

        from com.sun.star.awt import Rectangle

        smgr = ctx.getServiceManager()
        popup = smgr.createInstanceWithContext("com.sun.star.awt.PopupMenu", ctx)
        popup.insertItem(1, "✓ " + _("Accept this change"), 0, 0)
        popup.insertItem(2, "✗ " + _("Reject this change"), 0, 1)
        total = len(agent_changes(model))
        if total > 1:
            popup.insertSeparator(2)
            popup.insertItem(3, "✓ " + _("Accept all {0} changes").format(total), 0, 3)
            popup.insertItem(4, "✗ " + _("Reject all {0} changes").format(total), 0, 4)
        rect = Rectangle()
        rect.X = int(x)
        rect.Y = int(y)
        rect.Width = 0
        rect.Height = 0
        choice = popup.execute(source_window, rect, 0)
        if choice == 1:
            resolve_agent_change(model, ctx, token, True)
        elif choice == 2:
            resolve_agent_change(model, ctx, token, False)
        elif choice == 3:
            resolve_all_agent_changes(model, ctx, True)
        elif choice == 4:
            resolve_all_agent_changes(model, ctx, False)
    except Exception:
        log.warning("review_click_popup: popup failed", exc_info=True)


def _make_handler(model: Any) -> Any:
    global _handler_cls
    if _handler_cls is None:
        from com.sun.star.awt import XMouseClickHandler

        class _ClickReviewHandler(unohelper.Base, XMouseClickHandler):  # type: ignore[misc, valid-type]
            def __init__(self, model: Any) -> None:
                super().__init__()
                self._model = model

            def mousePressed(self, e):  # noqa: N802 -- UNO API
                return False  # never consume

            def mouseReleased(self, e):  # noqa: N802 -- UNO API
                try:
                    if e.ClickCount == 1 and e.Buttons == 1 and not e.PopupTrigger:  # plain left click
                        source = e.Source
                        x, y = e.X, e.Y
                        model = self._model
                        from plugin.framework.queue_executor import post_to_main_thread

                        # Defer: let the view finish moving the caret to the click point first.
                        post_to_main_thread(_show_popup_and_resolve, model, source, x, y)
                except Exception:
                    log.debug("review_click_popup: mouseReleased failed", exc_info=True)
                return False  # never consume

            def disposing(self, Source):  # noqa: N802, N803 -- UNO API
                pass

        _handler_cls = _ClickReviewHandler
    return _handler_cls(model)


def register_click_review(controller: Any) -> None:
    """Attach the click handler to one Writer view controller (idempotent per controller)."""
    if controller is None:
        return
    try:
        with _lock:
            if controller in _registered_controllers:
                return
        if not hasattr(controller, "addMouseClickHandler"):
            return
        model = controller.getModel()
        handler = _make_handler(model)
        with _lock:
            if controller in _registered_controllers:
                return
            controller.addMouseClickHandler(handler)
            _registered_controllers.append(controller)
            _handlers.append(handler)
        log.debug("review_click_popup: registered on Writer controller")
    except Exception:
        log.warning("review_click_popup: register failed", exc_info=True)
