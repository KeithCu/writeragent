# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Calc cell context menu: Edit Python in Cell… (right-click on a cell)."""

from __future__ import annotations

import logging
import threading
from typing import Any

import uno
import unohelper

from plugin.framework.errors import is_disposal, uno_read
from plugin.framework.i18n import _
from plugin.framework.thread_guard import main_thread_only
from plugin.framework.uno_context import get_desktop, resolve_package_extension_id, uno_same
from plugin.framework.uno_listeners import controller_alive, install_view_doc_listener

log = logging.getLogger(__name__)

_CELL_MENU_FIRST_COMMAND = ".uno:Cut"

# Commands present only in Calc's cell.xml + sparkline.xml popups in this LO version.
# Any-of: one conditionally hidden item must not drop the feature.
_CELL_MENU_ONLY_COMMANDS = frozenset({
    ".uno:InsertCell",
    ".uno:DeleteCell",
    ".uno:MergeCells",
    ".uno:DataSelect",
    ".uno:CurrentValidation",
})

_lock = threading.RLock()
_registered_controllers: list[Any] = []
_interceptor: Any = None
_doc_listener: Any = None


def _edit_python_cell_url() -> str:
    """CommandURL for this OXT. LibrePy bootstrap is OnStartApp, so resolve at intercept time."""
    return f"{resolve_package_extension_id()}:scripting.edit_python_cell"


def _is_calc_controller(controller: Any) -> bool:
    if controller is None:
        return False
    model = uno_read(controller.getModel, None)
    return bool(model and uno_read(lambda: model.supportsService("com.sun.star.sheet.SpreadsheetDocument"), False))


def _looks_like_cell_context_menu(container: Any) -> bool:
    # .uno:Cut is only a cheap prefilter: row/column-header and object popups share it.
    # A cell menu also contains a cell-only command (InsertCell etc.).
    if container is None:
        return False
    try:
        count = container.getCount()
        if count == 0:
            return False
        if str(container.getByIndex(0).getPropertyValue("CommandURL")) != _CELL_MENU_FIRST_COMMAND:
            return False
        for i in range(1, count):
            try:
                if str(container.getByIndex(i).getPropertyValue("CommandURL")) in _CELL_MENU_ONLY_COMMANDS:
                    return True
            except Exception:  # noqa: S112 - separator has no CommandURL; continue scan
                continue
    except Exception:
        return False
    return False


def _get_interceptor() -> Any:
    global _interceptor
    if _interceptor is not None:
        return _interceptor

    from com.sun.star.ui import ActionTriggerSeparatorType, XContextMenuInterceptor
    from com.sun.star.ui.ContextMenuInterceptorAction import IGNORED, CONTINUE_MODIFIED

    class _CalcCellContextMenuInterceptor(unohelper.Base, XContextMenuInterceptor):  # type: ignore[misc, valid-type]
        def notifyContextMenuExecute(self, aEvent: Any) -> Any:  # noqa: N802 — UNO API
            try:
                # Context menu SourceWindow is XWindow, not a frame—no getController(). Cut check plus
                # _CELL_MENU_ONLY_COMMANDS limits the item to cell menus (see scalc popupmenu/cell.xml).
                container = aEvent.ActionTriggerContainer
                if not _looks_like_cell_context_menu(container):
                    return IGNORED

                factory = container.queryInterface(uno.getTypeByName("com.sun.star.lang.XMultiServiceFactory"))
                if factory is None:
                    return IGNORED
                separator = factory.createInstance("com.sun.star.ui.ActionTriggerSeparator")
                separator.setPropertyValue("SeparatorType", ActionTriggerSeparatorType.LINE)

                entry = factory.createInstance("com.sun.star.ui.ActionTrigger")
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
    if not _is_calc_controller(controller):
        return
    try:
        interception = controller.queryInterface(uno.getTypeByName("com.sun.star.ui.XContextMenuInterception"))
        if interception is None:
            return
        with _lock:
            # Dedup controllers with uno_same, not id(); PyUNO hands out fresh proxies per call.
            _registered_controllers[:] = [
                ctrl for ctrl in _registered_controllers
                if controller_alive(ctrl)
            ]
            if any(uno_same(controller, existing) for existing in _registered_controllers):
                return
            interception.registerContextMenuInterceptor(_get_interceptor())
            _registered_controllers.append(controller)
        log.debug("python_editor_context_menu: registered on Calc controller")
    except Exception as exc:
        if is_disposal(exc):
            raise
        log.warning("python_editor_context_menu: register failed", exc_info=True)


def _install_doc_event_listener(ctx: Any) -> None:
    """Attach a global listener so Calc views opened AFTER OnStartApp bootstrap get the menu too."""
    global _doc_listener
    with _lock:
        if _doc_listener is not None:
            return
        listener = install_view_doc_listener(ctx, _register_controller, "python_editor_context_menu")
        if listener is not None:
            _doc_listener = listener


@main_thread_only
def install_calc_cell_context_menu(ctx: Any) -> None:
    """Register the cell context menu interceptor on open Calc frames and future views."""
    try:
        desktop = get_desktop(ctx)
        if desktop is not None:
            frames = desktop.getFrames()
            if frames is not None:
                for i in range(frames.getCount()):
                    try:
                        frame = frames.getByIndex(i)
                        controller = uno_read(frame.getController, None)
                        _register_controller(controller)
                    except Exception:
                        log.debug("python_editor_context_menu: frame %s skipped", i, exc_info=True)
        _install_doc_event_listener(ctx)
    except Exception:
        log.debug("python_editor_context_menu: install failed", exc_info=True)
