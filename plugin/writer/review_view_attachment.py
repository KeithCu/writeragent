# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""One registry for Writer review UI hooks per open document (toolbar, context menu, click popup)."""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Any

import uno
from plugin.doc.document_helpers import UNKNOWN_DOC_KEY, _compute_doc_key
from plugin.framework.errors import is_disposal
from plugin.framework.uno_context import get_runtime_uid, uno_same
from plugin.framework.uno_listeners import BaseModifyListener, controller_alive, install_view_doc_listener
from plugin.writer import change_context_menu, review_click_popup

log = logging.getLogger(__name__)

TOOLBAR_RESOURCE_URL = "private:resource/toolbar/addon_org.extension.writeragent.toolbar"

_DOCK_NEW_ROW_Y = 30000

_lock = threading.RLock()
_doc_listener: Any = None
_pending_refresh: set[str] = set()
_pending_lock = threading.Lock()


def attachment_key(model: Any) -> str:
    """Stable per-document key (RuntimeUID / URL), else ``id:<object id>``."""
    key = _compute_doc_key(model)
    if key != UNKNOWN_DOC_KEY:
        return key
    return "id:%d" % id(model)


def is_writer(model: Any) -> bool:
    try:
        return bool(model is not None and model.supportsService("com.sun.star.text.TextDocument"))
    except Exception as exc:
        if is_disposal(exc):
            raise
        return False


def layout_manager(model: Any) -> Any | None:
    try:
        controller = model.getCurrentController()
        if controller is None:
            return None
        frame = controller.getFrame()
        if frame is None:
            return None
        lm = getattr(frame, "LayoutManager", None)
        return lm
    except Exception:
        log.debug("review_view_attachment: layout manager lookup failed", exc_info=True)
        return None


@dataclass
class ReviewViewAttachment:
    """Per-document review hooks: modify listener, toolbar dock state, per-view controllers."""

    key: str
    model: Any
    modify_listener: Any | None = None
    toolbar_docked: bool = False
    controllers: list[tuple[Any, Any, Any]] = field(default_factory=list)  # ctrl, interceptor, click handler


_attachments: dict[str, ReviewViewAttachment] = {}


class ReviewModifyListener(BaseModifyListener):
    """Toolbar visibility recount when native UI resolves agent changes."""

    _key: str

    def __init__(self, key: str) -> None:
        super().__init__()
        self._key = key

    def modified(self, aEvent: Any) -> None:  # noqa: N802, N803 -- UNO signature
        try:
            model = aEvent.Source
            lm = layout_manager(model)
            if lm is not None and lm.isElementVisible(TOOLBAR_RESOURCE_URL):
                schedule_toolbar_refresh(model, self._key)
        except Exception:
            log.debug("review_view_attachment: modify handler failed", exc_info=True)

    def disposing(self, Source: Any) -> None:  # noqa: N802, N803 -- UNO signature
        attachment = _attachments.get(self._key)
        if attachment is not None and attachment.modify_listener is self:
            _drop_attachment(self._key, remove_modify_listener=False)


def _dock_top(lm: Any) -> bool:
    try:
        from com.sun.star.awt import Point

        area = uno.Enum("com.sun.star.ui.DockingArea", "DOCKINGAREA_TOP")
        pos = Point()
        pos.X = 0
        pos.Y = _DOCK_NEW_ROW_Y
        lm.dockWindow(TOOLBAR_RESOURCE_URL, area, pos)
        return True
    except Exception:
        log.debug("review_toolbar: dock-to-top failed", exc_info=True)
        return False


def refresh_review_toolbar(model: Any) -> None:
    """Show the review fast-travel toolbar only while the document has pending agent changes.

    The toolbar is declared statically in Addons.xcu and named/hidden-by-default in
    WriterWindowState.xcu. Here we flip its visibility at runtime: shown when
    ``agent_change_presence(model, stop_at_first=True)`` finds changes, hidden when it drops back to zero. All LayoutManager
    calls must run on the UNO main thread -- every caller below already does (the agent edit path is
    marshalled via main.call; resolve/navigate handlers and document events fire on the
    main thread).
    """
    if model is None:
        return
    try:
        from plugin.writer.review_scan import agent_change_presence

        lm = layout_manager(model)
        if lm is None:
            return
        count, reliable = agent_change_presence(model, stop_at_first=True)
        if not reliable:
            return
        if count > 0:
            lm.showElement(TOOLBAR_RESOURCE_URL)
            uid = get_runtime_uid(model) or None
            key = attachment_key(model)
            attachment = _attachments.get(key)
            docked = attachment.toolbar_docked if attachment is not None else False
            if uid is None or not docked:
                if _dock_top(lm):
                    mark_toolbar_docked(model)
        else:
            lm.hideElement(TOOLBAR_RESOURCE_URL)
    except Exception:
        log.debug("review_toolbar: refresh failed", exc_info=True)


def schedule_toolbar_refresh(model: Any, key: str) -> None:
    """Queue one toolbar recount on a later main-thread turn."""
    with _pending_lock:
        if key in _pending_refresh:
            return
        _pending_refresh.add(key)
    from plugin.framework.queue_executor import main

    try:
        main.post(_run_deferred_refresh, model, key)
    except Exception:
        with _pending_lock:
            _pending_refresh.discard(key)
        log.debug("review_view_attachment: could not queue toolbar recount", exc_info=True)


def _run_deferred_refresh(model: Any, key: str) -> None:
    with _pending_lock:
        if key not in _pending_refresh:
            return
        _pending_refresh.discard(key)
    try:
        lm = layout_manager(model)
        if lm is not None and lm.isElementVisible(TOOLBAR_RESOURCE_URL):
            refresh_review_toolbar(model)
    except Exception:
        log.debug("review_view_attachment: deferred refresh failed", exc_info=True)


def _clear_pending(key: str) -> None:
    attachment = _attachments.get(key)
    if attachment is not None:
        attachment.toolbar_docked = False
    with _pending_lock:
        _pending_refresh.discard(key)


def _get_or_create(model: Any) -> ReviewViewAttachment:
    key = attachment_key(model)
    with _lock:
        attachment = _attachments.get(key)
        if attachment is None:
            attachment = ReviewViewAttachment(key=key, model=model)
            _attachments[key] = attachment
        else:
            attachment.model = model
        return attachment


def ensure_modify_listener(model: Any) -> None:
    """Attach one modify listener per document (idempotent)."""
    attachment = _get_or_create(model)
    with _lock:
        if attachment.modify_listener is not None:
            return
        listener = ReviewModifyListener(attachment.key)
    try:
        model.addModifyListener(listener)
    except Exception:
        log.debug("review_view_attachment: modify listener registration failed", exc_info=True)
        return
    with _lock:
        attachment.modify_listener = listener


def _drop_attachment(key: str, *, remove_modify_listener: bool) -> None:
    attachment = _attachments.pop(key, None)
    _clear_pending(key)
    if attachment is None:
        return
    if remove_modify_listener and attachment.modify_listener is not None:
        try:
            attachment.model.removeModifyListener(attachment.modify_listener)
        except Exception:
            log.debug("review_view_attachment: modify listener removal failed", exc_info=True)
    for controller, interceptor, click_handler in list(attachment.controllers):
        try:
            controller.releaseContextMenuInterceptor(interceptor)
        except Exception:
            log.debug("review_view_attachment: releaseContextMenuInterceptor failed", exc_info=True)
        try:
            if controller_alive(controller):
                controller.removeMouseClickHandler(click_handler)
        except Exception:
            log.debug("review_view_attachment: removeMouseClickHandler failed", exc_info=True)
    attachment.controllers.clear()


def release_document(model: Any) -> None:
    """Tear down all review hooks for one document (OnUnload / dispose)."""
    if model is None:
        return
    key = attachment_key(model)
    with _lock:
        attachment = _attachments.get(key)
        if attachment is None:
            _clear_pending(key)
            return
        if uno_same(attachment.model, model):
            _drop_attachment(key, remove_modify_listener=True)
            return
    _clear_pending(key)


def register_controller(controller: Any) -> None:
    """Attach context-menu interceptor and click-review handler on one Writer view."""
    if controller is None:
        return
    cleanup_disposed_controllers()
    try:
        from plugin.framework.uno_context import uno_same

        model = controller.getModel()
        if not is_writer(model):
            return
        if not hasattr(controller, "registerContextMenuInterceptor"):
            return
        attachment = _get_or_create(model)
        with _lock:
            if any(uno_same(controller, ctrl) for ctrl, _i, _h in attachment.controllers):
                return
        interceptor = change_context_menu._make_interceptor(model)
        click_handler = review_click_popup._make_handler(model, detach_click_handler)
        controller.registerContextMenuInterceptor(interceptor)
        if hasattr(controller, "addMouseClickHandler"):
            controller.addMouseClickHandler(click_handler)
        with _lock:
            attachment.controllers.append((controller, interceptor, click_handler))
        ensure_modify_listener(model)
        refresh_review_toolbar(model)
        log.debug("review_view_attachment: registered on Writer controller")
    except Exception as exc:
        from plugin.framework.errors import is_disposal

        if is_disposal(exc):
            raise
        log.warning("review_view_attachment: register failed", exc_info=True)


def unregister_controllers_for_model(model: Any) -> None:
    """Drop controller hooks whose interceptor model matches *model* (one window closed)."""
    if model is None:
        return
    from plugin.framework.uno_context import uno_same as same

    key = attachment_key(model)
    with _lock:
        attachment = _attachments.get(key)
        if attachment is None:
            return
        keep: list[tuple[Any, Any, Any]] = []
        removed: list[tuple[Any, Any, Any]] = []
        for triple in attachment.controllers:
            interceptor = triple[1]
            interceptor_model = getattr(interceptor, "_model", None)
            if same(interceptor_model, model):
                removed.append(triple)
            else:
                keep.append(triple)
        attachment.controllers[:] = keep
    for controller, interceptor, click_handler in removed:
        try:
            controller.releaseContextMenuInterceptor(interceptor)
        except Exception:
            log.debug("review_view_attachment: releaseContextMenuInterceptor failed", exc_info=True)
        try:
            controller.removeMouseClickHandler(click_handler)
        except Exception:
            log.debug("review_view_attachment: removeMouseClickHandler failed", exc_info=True)


def cleanup_disposed_controllers() -> None:
    """Remove disposed controllers from every attachment."""
    with _lock:
        for attachment in _attachments.values():
            alive: list[tuple[Any, Any, Any]] = []
            for ctrl, interceptor, handler in attachment.controllers:
                if controller_alive(ctrl):
                    alive.append((ctrl, interceptor, handler))
            attachment.controllers[:] = alive


def registered_controllers() -> list[tuple[Any, Any]]:
    """(controller, interceptor) pairs for tests."""
    with _lock:
        out: list[tuple[Any, Any]] = []
        for attachment in _attachments.values():
            for ctrl, interceptor, _handler in attachment.controllers:
                out.append((ctrl, interceptor))
        return out


def registered_click_handlers() -> list[tuple[Any, Any]]:
    """(controller, click_handler) pairs for tests."""
    with _lock:
        return [(c, h) for att in _attachments.values() for c, _i, h in att.controllers]


def detach_click_handler(handler: Any) -> Any | None:
    """Remove *handler* from whichever attachment holds it; return its controller or None."""
    with _lock:
        for attachment in _attachments.values():
            for idx, (c, _i, h) in enumerate(attachment.controllers):
                if h is handler:
                    attachment.controllers.pop(idx)
                    return c
    return None


def modify_listener_map() -> dict[str, Any]:
    """doc attachment key -> modify listener (tests)."""
    with _lock:
        return {
            key: att.modify_listener
            for key, att in _attachments.items()
            if att.modify_listener is not None
        }


def toolbar_docked_keys() -> set[str]:
    with _lock:
        return {key for key, att in _attachments.items() if att.toolbar_docked}


def mark_toolbar_docked(model: Any) -> None:
    attachment = _get_or_create(model)
    with _lock:
        attachment.toolbar_docked = True


def is_toolbar_docked(model: Any) -> bool:
    key = attachment_key(model)
    with _lock:
        attachment = _attachments.get(key)
        return bool(attachment and attachment.toolbar_docked)


def _on_view_event(controller: Any) -> None:
    register_controller(controller)


def _on_document_unload(model: Any) -> None:
    if not is_writer(model):
        return
    unregister_controllers_for_model(model)
    release_document(model)


def _sweep_open_frames(ctx: Any) -> None:
    from plugin.framework.uno_context import get_desktop

    desktop = get_desktop(ctx)
    if desktop is None:
        return
    frames = desktop.getFrames()
    if frames is not None:
        for i in range(frames.getCount()):
            try:
                frame = frames.getByIndex(i)
                controller = frame.getController() if frame is not None else None
                model = controller.getModel() if controller is not None else None
                if is_writer(model):
                    register_controller(controller)
            except Exception:
                log.debug("review_view_attachment: frame %s skipped", i, exc_info=True)
    try:
        current = desktop.getCurrentFrame()
        if current is not None:
            controller = current.getController()
            model = controller.getModel() if controller is not None else None
            if is_writer(model):
                register_controller(controller)
    except Exception:
        log.debug("review_view_attachment: current frame skipped", exc_info=True)


def install(ctx: Any) -> None:
    """Register review hooks on open Writer views and on views opened later."""
    global _doc_listener
    try:
        _sweep_open_frames(ctx)
    except Exception:
        log.warning("review_view_attachment: initial sweep failed", exc_info=True)
    with _lock:
        if _doc_listener is not None:
            return
    listener = install_view_doc_listener(
        ctx,
        _on_view_event,
        "review_view_attachment",
        on_unload=_on_document_unload,
    )
    if listener is not None:
        with _lock:
            _doc_listener = listener


def reset_all() -> None:
    """Clear all attachments (unit tests)."""
    global _doc_listener
    with _lock:
        for key in list(_attachments):
            _drop_attachment(key, remove_modify_listener=False)
        _doc_listener = None
    with _pending_lock:
        _pending_refresh.clear()
