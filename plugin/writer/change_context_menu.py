# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Writer right-click review: accept/reject the agent's tracked changes, as whole units.

Adds two entries to the Writer text context menu (only while the document holds pending AGENT
changes -- wa-review session tokens): accept/reject the agent change under the cursor as one
unit (a replace's strikethrough + underline resolve together, no Manage dialog). The user's own
tracked changes are never touched. Bulk accept/reject ALL agent changes lives on the review
toolbar (#2), not in this menu. The entries dispatch
``org.extension.writeragent:writer.accept_change`` / ``writer.reject_change``, handled in ``main.py``.

PyUNO pitfalls this file works around (each silently killed the menu when done wrong):
- ``queryInterface(InterfaceClass)`` cannot convert an imported interface class to a UNO type;
  call the interface's methods directly on the proxy object instead.
- An exception escaping ``notifyContextMenuExecute`` makes the core permanently UNREGISTER the
  interceptor (``catch (...) { removeInterface }`` in SfxViewShell::TryContextMenuInterception)
  with no diagnostics -- so the entire body, imports included, lives inside try/except.
- ``registerContextMenuInterceptor`` on a controller that is not (yet/anymore) attached to a
  view shell succeeds and does nothing. Registration therefore prefers ``Event.ViewController``
  from OnViewCreated (broadcast only after the view shell is attached) and re-registers per new
  controller; interceptors do not survive a view recreation (reload, new window).
"""

from __future__ import annotations

import logging
import threading
from typing import Any

import unohelper

log = logging.getLogger(__name__)

_ACCEPT_URL = "org.extension.writeragent:writer.accept_change"
_REJECT_URL = "org.extension.writeragent:writer.reject_change"

_lock = threading.RLock()
# Registered controllers, deduplicated by UNO object identity: PyUNO hands out a NEW proxy
# object per call/event for the same underlying controller, so dedup by Python id() fails
# (OnViewCreated + OnLoad would register the same view twice -> duplicated menu entries).
# PyUNO proxies implement == via UNO identity, so a list + `in` is the reliable test.
# Strong refs are kept for the office session; releasing them when a view is disposed is
# follow-up work (one controller+interceptor pair lingers per closed document).
_registered_controllers: list[Any] = []
_interceptors: list[Any] = []  # keep strong refs so the Python objects outlive the call
_doc_listener: Any = None
_interceptor_cls: Any = None


def _is_writer(model: Any) -> bool:
    try:
        return bool(model is not None and model.supportsService("com.sun.star.text.TextDocument"))
    except Exception:
        return False


def _build_menu(model: Any, event: Any) -> Any:
    """Append the Accept/Reject entries when the doc has tracked changes.

    EVERYTHING (imports included) stays inside the try: an exception escaping this callback
    causes LibreOffice to silently unregister the interceptor forever.
    """
    try:
        from com.sun.star.ui.ContextMenuInterceptorAction import CONTINUE_MODIFIED, IGNORED
        from com.sun.star.ui import ActionTriggerSeparatorType

        from plugin.framework.i18n import _
        from plugin.writer.inline_review import has_agent_changes

        has = has_agent_changes(model)
        log.debug("change_context_menu: notifyContextMenuExecute (Writer), has_agent_changes=%s", has)
        if not has:
            return IGNORED
        container = event.ActionTriggerContainer
        # The container implements XMultiServiceFactory; call it directly on the proxy
        # (queryInterface with an imported interface class does not work in PyUNO).
        if container is None or not hasattr(container, "createInstance"):
            return IGNORED

        separator = container.createInstance("com.sun.star.ui.ActionTriggerSeparator")
        separator.setPropertyValue("SeparatorType", ActionTriggerSeparatorType.LINE)
        # Distinct wording from LibreOffice's native per-mark "Accept Change" / "Reject Change":
        # ours resolves the WHOLE change at the cursor (a replace's delete + insert together).
        accept = container.createInstance("com.sun.star.ui.ActionTrigger")
        accept.setPropertyValue("Text", _("Accept whole change (agent)"))
        accept.setPropertyValue("CommandURL", _ACCEPT_URL)
        reject = container.createInstance("com.sun.star.ui.ActionTrigger")
        reject.setPropertyValue("Text", _("Reject whole change (agent)"))
        reject.setPropertyValue("CommandURL", _REJECT_URL)
        # Bulk accept/reject ALL is intentionally NOT here -- it lives on the review toolbar (#2).
        # The right-click stays focused on the change under the cursor (the punctual action).

        count = container.getCount()
        container.insertByIndex(count, separator)
        container.insertByIndex(count + 1, accept)
        container.insertByIndex(count + 2, reject)
        log.debug("change_context_menu: added review entries at index %d", count)
        return CONTINUE_MODIFIED
    except Exception:
        log.exception("change_context_menu: building menu failed")
        try:
            from com.sun.star.ui.ContextMenuInterceptorAction import IGNORED

            return IGNORED
        except Exception:
            return None


def _make_interceptor(model: Any) -> Any:
    global _interceptor_cls
    if _interceptor_cls is None:
        from com.sun.star.ui import XContextMenuInterceptor

        class _ChangeContextMenuInterceptor(unohelper.Base, XContextMenuInterceptor):  # type: ignore[misc, valid-type]
            def __init__(self, model: Any) -> None:
                super().__init__()
                self._model = model

            def notifyContextMenuExecute(self, aEvent):  # noqa: N802, N803 -- UNO API
                return _build_menu(self._model, aEvent)

        _interceptor_cls = _ChangeContextMenuInterceptor
    return _interceptor_cls(model)


def _cleanup_disposed_controllers() -> None:
    """Passively remove any disposed controllers/interceptors from our tracking lists."""
    try:
        from plugin.writer import review_click_popup
        with _lock:
            alive_controllers = []
            alive_interceptors = []
            for idx, ctrl in enumerate(_registered_controllers):
                try:
                    if ctrl.getModel() is not None:
                        alive_controllers.append(ctrl)
                        if idx < len(_interceptors):
                            alive_interceptors.append(_interceptors[idx])
                except Exception:
                    pass
            _registered_controllers[:] = alive_controllers
            _interceptors[:] = alive_interceptors

        with review_click_popup._lock:
            alive_popup_controllers = []
            alive_popup_handlers = []
            for idx, ctrl in enumerate(review_click_popup._registered_controllers):
                try:
                    if ctrl.getModel() is not None:
                        alive_popup_controllers.append(ctrl)
                        if idx < len(review_click_popup._handlers):
                            alive_popup_handlers.append(review_click_popup._handlers[idx])
                except Exception:
                    pass
            review_click_popup._registered_controllers[:] = alive_popup_controllers
            review_click_popup._handlers[:] = alive_popup_handlers
    except Exception:
        log.debug("change_context_menu: cleanup failed", exc_info=True)


def _register_controller(controller: Any) -> None:
    """Register the interceptor on one Writer view controller (idempotent per controller)."""
    if controller is None:
        return
    _cleanup_disposed_controllers()
    try:
        with _lock:
            if controller in _registered_controllers:  # UNO-identity equality, see above
                return
        model = controller.getModel()
        if not _is_writer(model):
            return
        # PyUNO exposes every supported interface's methods directly on the proxy; calling
        # registerContextMenuInterceptor on the controller is the working form.
        if not hasattr(controller, "registerContextMenuInterceptor"):
            return
        interceptor = _make_interceptor(model)
        with _lock:
            if controller in _registered_controllers:
                return
            controller.registerContextMenuInterceptor(interceptor)
            _registered_controllers.append(controller)
            _interceptors.append(interceptor)
        log.debug("change_context_menu: registered on Writer controller")
        # Same controller, same lifecycle: also attach the click-to-review popup handler.
        try:
            from plugin.writer.review_click_popup import register_click_review

            register_click_review(controller)
        except Exception:
            log.warning("change_context_menu: click-review registration failed", exc_info=True)
    except Exception:
        # WARNING, not debug: a silent registration failure looks identical to "menu ignored".
        log.warning("change_context_menu: register failed", exc_info=True)


def _register_frame(frame: Any) -> None:
    try:
        controller = frame.getController() if frame is not None else None
    except Exception:
        return
    _register_controller(controller)


def _install_doc_event_listener(ctx: Any) -> None:
    """Attach a global listener so documents/views opened AFTER bootstrap get the menu too."""
    global _doc_listener
    with _lock:
        if _doc_listener is not None:
            return
    try:
        from plugin.framework.uno_listeners import BaseDocumentEventListener

        class _NewDocListener(BaseDocumentEventListener):  # type: ignore[misc, valid-type]
            def on_document_event(self, Event: Any) -> None:  # noqa: N803 -- UNO signature
                try:
                    name = getattr(Event, "EventName", "") or ""
                    if name not in ("OnViewCreated", "OnLoadFinished", "OnLoad", "OnNew"):
                        return
                    # Prefer the event's own controller: it is guaranteed attached to the view
                    # shell (OnViewCreated is broadcast after attach), while getCurrentController
                    # can race and yield a controller whose registration is a silent no-op.
                    controller = getattr(Event, "ViewController", None)
                    if controller is not None:
                        _register_controller(controller)
                        return
                    source = getattr(Event, "Source", None)
                    if source is not None and hasattr(source, "getCurrentController"):
                        _register_controller(source.getCurrentController())
                except Exception:
                    log.warning("change_context_menu: doc-event handling failed", exc_info=True)

        smgr = ctx.getServiceManager()
        broadcaster = smgr.createInstanceWithContext("com.sun.star.frame.GlobalEventBroadcaster", ctx)
        listener = _NewDocListener()
        broadcaster.addDocumentEventListener(listener)
        with _lock:
            _doc_listener = listener
        log.debug("change_context_menu: global doc-event listener attached")
    except Exception:
        log.warning("change_context_menu: doc-event listener install failed", exc_info=True)


def install_writer_change_context_menu(ctx: Any) -> None:
    """Register the Accept/Reject context menu on open Writer views and future ones."""
    try:
        from plugin.framework.uno_context import get_desktop

        desktop = get_desktop(ctx)
        if desktop is not None:
            frames = desktop.getFrames()
            if frames is not None:
                for i in range(frames.getCount()):
                    try:
                        _register_frame(frames.getByIndex(i))
                    except Exception:
                        log.debug("change_context_menu: frame %s skipped", i, exc_info=True)
            try:
                current = desktop.getCurrentFrame()
                if current is not None:
                    _register_frame(current)
            except Exception:
                log.debug("change_context_menu: current frame skipped", exc_info=True)
        _install_doc_event_listener(ctx)
    except Exception:
        log.warning("change_context_menu: install failed", exc_info=True)
