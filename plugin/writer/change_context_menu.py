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

from plugin.framework.constants import EXTENSION_ID_WRITERAGENT
from plugin.framework.errors import is_real_disposal
from plugin.framework.uno_listeners import install_view_doc_listener

log = logging.getLogger(__name__)

_ACCEPT_URL = f"{EXTENSION_ID_WRITERAGENT}:writer.accept_change"
_REJECT_URL = f"{EXTENSION_ID_WRITERAGENT}:writer.reject_change"

_lock = threading.RLock()
# PyUNO gives a new proxy per event, so dedup must use uno_same, not in/==;
# one list of pairs means removal cannot desync the two lists.
# Strong refs are kept so the Python objects outlive the registration calls.
_registered: list[tuple[Any, Any]] = []  # (controller, interceptor); UNO-identity dedup
_doc_listener: Any = None
_interceptor_cls: Any = None


def _is_writer(model: Any) -> bool:
    try:
        return bool(model is not None and model.supportsService("com.sun.star.text.TextDocument"))
    except Exception as exc:
        # Bugfix: supportsService on a closed document raises DisposedException,
        # and returning False skipped registration as if the model were not Writer.
        # Re-raise real disposal so the doc-event wrapper can surface it.
        # Any other failure still means not Writer.
        if is_real_disposal(exc):
            raise
        return False


def _build_menu(model: Any, event: Any, ignored: Any, continue_modified: Any) -> Any:
    """Append the Accept/Reject entries when the doc has tracked changes.

    EVERYTHING (imports included) stays inside the try: an exception escaping this callback
    causes LibreOffice to silently unregister the interceptor forever.
    """
    try:
        from com.sun.star.ui import ActionTriggerSeparatorType

        from plugin.framework.i18n import _
        from plugin.writer.review_scan import agent_change_presence

        count, reliable = agent_change_presence(model, stop_at_first=True)
        # Fail open when the scan is unreliable: show the menu rather than hide a real change.
        has = count > 0 or not reliable
        log.debug("change_context_menu: notifyContextMenuExecute (Writer), has_agent_changes=%s", has)
        if not has:
            return ignored
        container = event.ActionTriggerContainer
        # The container implements XMultiServiceFactory; call it directly on the proxy
        # (queryInterface with an imported interface class does not work in PyUNO).
        if container is None or not hasattr(container, "createInstance"):
            return ignored

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

        insert_at = container.getCount()
        container.insertByIndex(insert_at, separator)
        container.insertByIndex(insert_at + 1, accept)
        container.insertByIndex(insert_at + 2, reject)
        log.debug("change_context_menu: added review entries at index %d", insert_at)
        return continue_modified
    except Exception:
        log.exception("change_context_menu: building menu failed")
        return ignored


def _make_interceptor(model: Any) -> Any:
    global _interceptor_cls
    if _interceptor_cls is None:
        from com.sun.star.ui import XContextMenuInterceptor
        from com.sun.star.ui.ContextMenuInterceptorAction import CONTINUE_MODIFIED, IGNORED

        class _ChangeContextMenuInterceptor(unohelper.Base, XContextMenuInterceptor):  # type: ignore[misc, valid-type]
            _model: Any

            def __init__(self, model: Any) -> None:
                super().__init__()
                self._model = model

            def notifyContextMenuExecute(self, aEvent: Any) -> Any:  # noqa: N802, N803 -- UNO API
                return _build_menu(self._model, aEvent, IGNORED, CONTINUE_MODIFIED)

        _interceptor_cls = _ChangeContextMenuInterceptor
    return _interceptor_cls(model)


def _controller_alive(controller: Any) -> bool:
    try:
        return controller.getModel() is not None
    except Exception:
        return False


def _cleanup_disposed_controllers() -> None:
    """Passively remove any disposed controllers/interceptors from our tracking lists."""
    try:
        with _lock:
            _registered[:] = [pair for pair in _registered if _controller_alive(pair[0])]

        from plugin.writer import review_click_popup
        review_click_popup.cleanup_disposed_controllers()
    except Exception:
        log.debug("change_context_menu: cleanup failed", exc_info=True)


def _unregister_model(model: Any) -> None:
    """Remove registered interceptor for the given model on document unload.

    The registry used to hold a strong ref for the whole session, so each closed
    window leaked its document; OnUnload is the only place the last closed document
    can be released.
    """
    if model is None:
        return
    from plugin.framework.uno_context import uno_same

    with _lock:
        keep: list[tuple[Any, Any]] = []
        removed: list[tuple[Any, Any]] = []
        for pair in _registered:
            interceptor = pair[1]
            interceptor_model = getattr(interceptor, "_model", None)
            if uno_same(interceptor_model, model):
                removed.append(pair)
            else:
                keep.append(pair)
        _registered[:] = keep

    for controller, interceptor in removed:
        try:
            controller.releaseContextMenuInterceptor(interceptor)
        except Exception:
            log.debug("change_context_menu: releaseContextMenuInterceptor failed", exc_info=True)


def _register_controller(controller: Any) -> None:
    """Register the interceptor on one Writer view controller (idempotent per controller)."""
    if controller is None:
        return
    _cleanup_disposed_controllers()
    try:
        from plugin.framework.uno_context import uno_same

        with _lock:
            if any(uno_same(controller, pair[0]) for pair in _registered):
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
            if any(uno_same(controller, pair[0]) for pair in _registered):
                return
            controller.registerContextMenuInterceptor(interceptor)
            _registered.append((controller, interceptor))
        log.debug("change_context_menu: registered on Writer controller")
        # Same controller, same lifecycle: also attach the click-to-review popup handler.
        try:
            from plugin.writer.review_click_popup import register_click_review

            register_click_review(controller)
        except Exception:
            log.warning("change_context_menu: click-review registration failed", exc_info=True)
    except Exception as exc:
        # Bugfix: getModel() on a closed document raises DisposedException, and this
        # except logged "register failed" and returned. The doc-event wrapper never
        # saw it. Re-raise real disposal. Other failures stay a warning so install
        # can skip one controller.
        if is_real_disposal(exc):
            raise
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
    listener = install_view_doc_listener(
        ctx,
        _register_controller,
        "change_context_menu",
        on_unload=_unregister_model,
    )
    if listener is not None:
        with _lock:
            _doc_listener = listener


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
