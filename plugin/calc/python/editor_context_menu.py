# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Calc cell context menu: Edit Python in Cell… (right-click on a cell)."""

from __future__ import annotations

import logging
import threading
from typing import Any, cast

import unohelper

from plugin.framework.errors import best_effort, uno_probe
from plugin.framework.thread_guard import main_thread_only

log = logging.getLogger(__name__)

_CELL_MENU_FIRST_COMMAND = ".uno:Cut"

_lock = threading.RLock()
_registered_controllers: list[Any] = []
_interceptor: Any = None
_doc_listener: Any = None


def _edit_python_cell_url() -> str:
    """CommandURL for this OXT. LibrePy bootstrap is OnStartApp, so resolve at intercept time."""
    from plugin.framework.uno_context import resolve_package_extension_id

    return f"{resolve_package_extension_id()}:scripting.edit_python_cell"


def _is_calc_controller(controller: Any) -> bool:
    if controller is None:
        return False
    return bool(
        uno_probe(
            lambda: bool(controller.getModel() and controller.getModel().supportsService("com.sun.star.sheet.SpreadsheetDocument")),
            False,
        )
    )


def _looks_like_cell_context_menu(container: Any) -> bool:
    # .uno:Cut is the first item of sc/uiconfig/scalc/popupmenu/cell.xml.
    # Several other Calc popups share that first item (column/row headers, etc.).
    if container is None or container.getCount() == 0:
        return False
    return bool(
        uno_probe(
            lambda: str(container.getByIndex(0).getPropertyValue("CommandURL")) == _CELL_MENU_FIRST_COMMAND,
            False,
        )
    )


def _prune_disposed_controllers() -> None:
    """Prune disposed controllers from _registered_controllers (must hold _lock)."""
    _registered_controllers[:] = [
        ctrl for ctrl in _registered_controllers
        if uno_probe(lambda: ctrl.getModel() is not None, False)
    ]


def _get_interceptor() -> Any:
    global _interceptor
    if _interceptor is not None:
        return _interceptor

    from com.sun.star.ui import ActionTriggerSeparatorType, XContextMenuInterceptor
    from com.sun.star.ui.ContextMenuInterceptorAction import IGNORED, CONTINUE_MODIFIED

    class _CalcCellContextMenuInterceptor(unohelper.Base, XContextMenuInterceptor):  # type: ignore[misc, valid-type]
        def notifyContextMenuExecute(self, aEvent: Any) -> Any:  # noqa: N802 — UNO API
            try:
                # Bugfix: SourceWindow is a com.sun.star.awt.XWindow (ContextMenuExecuteEvent.idl),
                # not a frame. Calling getController() on it always failed with AttributeError,
                # the exception handler returned false, and "Edit Python in Cell..." never appeared.
                # Calc document type was already validated during controller registration in
                # _register_controller. The Cut check below (sc/uiconfig/scalc/popupmenu/cell.xml)
                # still filters out non-cell menus from the same controller (sheet tabs, anchors, etc.).
                container = aEvent.ActionTriggerContainer
                if not _looks_like_cell_context_menu(container):
                    return IGNORED

                import uno

                factory = cast("Any", container).queryInterface(uno.getTypeByName("com.sun.star.lang.XMultiServiceFactory"))
                if factory is None:
                    return IGNORED
                separator = factory.createInstance("com.sun.star.ui.ActionTriggerSeparator")
                separator.setPropertyValue("SeparatorType", ActionTriggerSeparatorType.LINE)

                entry = factory.createInstance("com.sun.star.ui.ActionTrigger")
                from plugin.framework.i18n import _

                entry.setPropertyValue("Text", _("Edit Python in Cell..."))
                entry.setPropertyValue("CommandURL", _edit_python_cell_url())

                count = container.getCount()
                container.insertByIndex(count, separator)
                container.insertByIndex(count + 1, entry)
                return CONTINUE_MODIFIED
            except Exception:
                log.exception("Calc cell context menu interceptor failed")
                return IGNORED

    _interceptor = _CalcCellContextMenuInterceptor()
    return _interceptor


def _register_controller(controller: Any) -> None:
    """Register the interceptor on one Calc view controller (idempotent per controller)."""
    if controller is None:
        return
    if not _is_calc_controller(controller):
        return
    try:
        import uno
        from plugin.framework.uno_context import uno_same

        interception = controller.queryInterface(uno.getTypeByName("com.sun.star.ui.XContextMenuInterception"))
        if interception is None:
            return
        with _lock:
            # Bugfix: PyUNO returns a fresh Python proxy object per call, so id() dedup
            # failed and registered multiple interceptors on the same controller.
            # OInterfaceContainerHelper::addInterface appends without deduping,
            # adding duplicate separators and menu items (see plugin/writer/change_context_menu.py).
            # We use uno_same to compare UNO identity and prune disposed controllers.
            _prune_disposed_controllers()
            if any(uno_same(controller, existing) for existing in _registered_controllers):
                return
            interception.registerContextMenuInterceptor(_get_interceptor())
            _registered_controllers.append(controller)
        log.debug("python_editor_context_menu: registered on Calc controller")
    except Exception:
        log.debug("python_editor_context_menu: register failed", exc_info=True)


def _register_frame(frame: Any) -> None:
    controller = uno_probe(frame.getController, None) if frame is not None else None
    _register_controller(controller)


def _install_doc_event_listener(ctx: Any) -> None:
    """Attach a global listener so Calc views opened AFTER OnStartApp bootstrap get the menu too."""
    global _doc_listener
    with _lock:
        if _doc_listener is not None:
            return
    try:
        from plugin.framework.uno_listeners import BaseDocumentEventListener

        class _NewDocListener(BaseDocumentEventListener):  # type: ignore[misc, valid-type]
            def on_document_event(self, Event: Any) -> None:  # noqa: N803 -- UNO signature
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

        smgr = ctx.getServiceManager()
        broadcaster = smgr.createInstanceWithContext("com.sun.star.frame.GlobalEventBroadcaster", ctx)
        listener = _NewDocListener()
        broadcaster.addDocumentEventListener(listener)
        with _lock:
            _doc_listener = listener
        log.debug("python_editor_context_menu: global doc-event listener attached")
    except Exception:
        log.warning("python_editor_context_menu: doc-event listener install failed", exc_info=True)


@main_thread_only
def install_calc_cell_context_menu(ctx: Any) -> None:
    """Register the cell context menu interceptor on open Calc frames and future views."""
    try:
        from plugin.framework.uno_context import get_desktop

        desktop = get_desktop(ctx)
        if desktop is not None:
            frames = desktop.getFrames()
            if frames is not None:
                for i in range(frames.getCount()):

                    def _frame(idx: int = i) -> Any:
                        return frames.getByIndex(idx)

                    frame = best_effort(_frame, "get frame")
                    if frame is not None:
                        _register_frame(frame)
        _install_doc_event_listener(ctx)
    except Exception:
        log.debug("python_editor_context_menu: install failed", exc_info=True)
