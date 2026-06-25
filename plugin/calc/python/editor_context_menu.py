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

log = logging.getLogger(__name__)

_EDIT_PYTHON_CELL_URL = "org.extension.writeragent:scripting.edit_python_cell"
_CELL_MENU_FIRST_COMMAND = ".uno:Cut"

_lock = threading.RLock()
_registered_frames: set[int] = set()
_interceptor: Any = None


def _frame_key(frame: Any) -> int | None:
    try:
        return id(frame)
    except Exception:
        return None


def _is_calc_spreadsheet(frame: Any) -> bool:
    try:
        controller = frame.getController()
        if controller is None:
            return False
        model = controller.getModel()
        if model is None:
            return False
        return bool(model.supportsService("com.sun.star.sheet.SpreadsheetDocument"))
    except Exception:
        log.debug("python_editor_context_menu: could not resolve frame model", exc_info=True)
        return False


def _looks_like_cell_context_menu(container: Any) -> bool:
    try:
        if container is None or container.getCount() == 0:
            return False
        first = container.getByIndex(0)
        if first is None:
            return False
        cmd = first.getPropertyValue("CommandURL")
        return str(cmd) == _CELL_MENU_FIRST_COMMAND
    except Exception:
        return False


def _get_interceptor() -> Any:
    global _interceptor
    if _interceptor is not None:
        return _interceptor

    from com.sun.star.ui import ActionTriggerSeparatorType, XContextMenuInterceptor
    from com.sun.star.ui.ContextMenuInterceptorAction import IGNORED, CONTINUE_MODIFIED

    class _CalcCellContextMenuInterceptor(unohelper.Base, XContextMenuInterceptor):  # type: ignore[misc, valid-type]
        def notifyContextMenuExecute(self, aEvent):  # noqa: N802 — UNO API
            try:
                if not _is_calc_spreadsheet(aEvent.SourceWindow):
                    return IGNORED
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
                entry.setPropertyValue("CommandURL", _EDIT_PYTHON_CELL_URL)

                count = container.getCount()
                container.insertByIndex(count, separator)
                container.insertByIndex(count + 1, entry)
                return CONTINUE_MODIFIED
            except Exception:
                log.exception("Calc cell context menu interceptor failed")
                return IGNORED

    _interceptor = _CalcCellContextMenuInterceptor()
    return _interceptor


def _register_frame(frame: Any) -> None:
    key = _frame_key(frame)
    if key is None:
        return
    with _lock:
        if key in _registered_frames:
            return
    try:
        controller = frame.getController()
        if controller is None:
            return
        import uno
        interception = controller.queryInterface(uno.getTypeByName("com.sun.star.ui.XContextMenuInterception"))
        if interception is None:
            return
        interception.registerContextMenuInterceptor(_get_interceptor())
        with _lock:
            _registered_frames.add(key)
        log.debug("python_editor_context_menu: registered on frame %s", key)
    except Exception:
        log.debug("python_editor_context_menu: register failed", exc_info=True)


def install_calc_cell_context_menu(ctx: Any) -> None:
    """Register the cell context menu interceptor on open Calc frames."""
    try:
        from plugin.framework.uno_context import get_desktop

        desktop = get_desktop(ctx)
        if desktop is None:
            return
        frames = desktop.getFrames()
        if frames is not None:
            for i in range(frames.getCount()):
                try:
                    frame = frames.getByIndex(i)
                    if frame is not None and _is_calc_spreadsheet(frame):
                        _register_frame(frame)
                except Exception:
                    log.debug("python_editor_context_menu: frame %s skipped", i, exc_info=True)
        try:
            current = desktop.getCurrentFrame()
            if current is not None and _is_calc_spreadsheet(current):
                _register_frame(current)
        except Exception:
            log.debug("python_editor_context_menu: current frame skipped", exc_info=True)
    except Exception:
        log.debug("python_editor_context_menu: install failed", exc_info=True)
