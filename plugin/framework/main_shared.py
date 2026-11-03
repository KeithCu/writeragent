# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Shared action dispatching and dialog helpers for main.py and main_core.py.

Concurrency: menu action names map to Python callables in
``_ACTION_HANDLERS``, filled during bootstrap (register_core_handlers,
register_common_handlers, and ChatbotModule.initialize), then only read when
the user picks a menu item. No lock. ``open_dialog_safely`` talks to
LibreOffice dialogs — call it from the UI thread, not from an HTTP or
LLM worker.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, ClassVar, cast

import unohelper
from com.sun.star.frame import DispatchDescriptor, XDispatch, XDispatchProvider
from com.sun.star.lang import XInitialization, XServiceInfo

from plugin.framework.url_utils import matches_extension_dispatch_url

_ACTION_HANDLERS: dict[str, Callable[..., Any]] = {}
log = logging.getLogger(__name__)

_action_error_ui: Callable[[Any, str, str, BaseException], None] | None = None


def set_action_error_ui(fn: Callable[[Any, str, str, BaseException], None] | None) -> None:
    """Register action/dialog error UI; ``None`` clears."""
    global _action_error_ui
    _action_error_ui = fn


class ExtensionProtocolHandler(unohelper.Base, XDispatch, XDispatchProvider, XInitialization, XServiceInfo):
    EXTENSION_ID: ClassVar[str]
    IMPL_NAME: ClassVar[str]
    SERVICE_NAMES: ClassVar[tuple[str, ...]] = ("com.sun.star.frame.ProtocolHandler",)
    ctx: Any

    def __init__(self, ctx: Any) -> None:
        self.ctx = ctx

    def initialize(self, aArguments: Any) -> None:
        pass

    def getImplementationName(self) -> str:
        return self.IMPL_NAME

    def supportsService(self, ServiceName: str) -> bool:
        return ServiceName in self.SERVICE_NAMES

    def getSupportedServiceNames(self) -> tuple[str, ...]:
        return self.SERVICE_NAMES

    def queryDispatch(self, URL: Any, TargetFrameName: str, SearchFlags: int) -> XDispatch:  # pyright: ignore[reportIncompatibleMethodOverride]
        if matches_extension_dispatch_url(URL, self.EXTENSION_ID):
            return cast("XDispatch", self)
        return cast("XDispatch", cast("object", None))

    def queryDispatches(self, Requests: tuple[DispatchDescriptor, ...]) -> tuple[XDispatch, ...]:  # pyright: ignore[reportIncompatibleMethodOverride]
        return tuple(self.queryDispatch(r.FeatureURL, r.FrameName, r.SearchFlags) for r in Requests)

    def addStatusListener(self, Control: Any, URL: Any) -> None:
        pass

    def removeStatusListener(self, Control: Any, URL: Any) -> None:
        pass


def register_action_handler(module_name: str, action_name: str, handler_func: Callable[..., Any]) -> None:
    """Register an action handler function."""
    _ACTION_HANDLERS[f"{module_name}.{action_name}"] = handler_func


def get_action_handler(command: str) -> Callable[..., Any] | None:
    """Get a registered action handler by command string."""
    return _ACTION_HANDLERS.get(command)


def _show_error_report(ctx: Any, detail: str, report_title: str, exc: BaseException) -> None:
    ui = _action_error_ui
    if ui is None:
        log.exception("action error UI is not registered (%s)", report_title)
        return
    try:
        ui(ctx, detail, report_title, exc)
    except Exception:
        log.exception("action error UI callback failed")


def report_action_failure(command: str, exc: BaseException) -> None:
    """Report an action handler exception via dialog and logging."""
    from plugin.framework.uno_context import get_ctx

    log.exception("Action %s failed", command)
    detail = f"{command} failed: {exc}"
    _show_error_report(get_ctx(), detail, command, exc)


def open_dialog_safely(dialog_func: Callable[..., Any], error_msg: str, *args: Any, **kwargs: Any) -> None:
    """Safely open a dialog with standardized error handling and reporting."""
    from plugin.framework.errors import UnoObjectError, suppress_disposed
    from plugin.framework.uno_context import get_ctx

    try:
        with suppress_disposed("dialog opening", suppress_all=False):
            dialog_func(get_ctx(), *args, **kwargs)
    except UnoObjectError as e:
        log.warning("UNO error opening dialog: %s", e.message)
    except Exception as e:
        log.exception("%s", error_msg)
        # error_msg is a caller sentence, not a catalog msgid. Do not pass it through _().
        detail = f"{error_msg}: {e}"
        _show_error_report(get_ctx(), detail, error_msg, e)
