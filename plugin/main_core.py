# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""LibrePy UNO bootstrap: scientific Python menus and =PY() without chat/MCP."""

from __future__ import annotations

import logging
import os
import sys
import threading
from typing import TYPE_CHECKING, Any, Callable, cast

_this = os.path.abspath(__file__)
for __ in range(2):
    _this = os.path.dirname(_this)
if _this not in sys.path:
    sys.path.insert(0, _this)

from plugin.framework.uno_bootstrap import ensure_plugin_on_path

ensure_plugin_on_path(
    __file__,
    levels_up=2,
    also_add_plugin_dir=True,
    also_add_lib=True,
    also_add_vendor=True,
)

import unohelper
from com.sun.star.frame import DispatchDescriptor, XDispatch, XDispatchProvider
from com.sun.star.lang import XInitialization, XServiceInfo
from com.sun.star.task import XJob, XJobExecutor

if TYPE_CHECKING:
    from com.sun.star.util import URL as UnoURL

from plugin.framework.logging import init_logging, log as wa_log, log_exception
from plugin.framework.uno_context import get_ctx, set_fallback_ctx, set_package_extension_id
from plugin.framework.url_utils import dispatch_command_from_url, matches_librepy_dispatch_url

EXTENSION_ID = "org.extension.librepy"
_DISPATCH_PROTOCOL = "org.extension.librepy:"

log = logging.getLogger(__name__)
_initialized = False
_init_lock = threading.Lock()

from plugin.framework.main_shared import (
    register_action_handler,
    get_action_handler,
    open_dialog_safely,
    register_common_handlers,
)

def _register_librepy_handlers() -> None:
    from plugin.librepy.settings import open_librepy_settings

    register_action_handler(
        "main",
        "settings",
        lambda: open_dialog_safely(open_librepy_settings, "Failed to open settings"),
    )
    register_common_handlers()


def bootstrap(ctx=None) -> None:
    global _initialized
    if _initialized:
        return
    with _init_lock:
        if _initialized:
            return
        if ctx is None:
            ctx = get_ctx()
        set_fallback_ctx(ctx)
        set_package_extension_id(EXTENSION_ID)
        from plugin.framework.config import init_config

        init_config(ctx)
        try:
            from plugin.scripting.audio_recorder_service import ensure_downloaded_audio_on_path

            ensure_downloaded_audio_on_path()
        except Exception:
            pass
        from plugin.framework.i18n import init_i18n

        init_i18n(ctx)
        _register_librepy_handlers()
        try:
            from plugin.calc.python.editor_context_menu import install_calc_cell_context_menu

            install_calc_cell_context_menu(ctx)
        except Exception:
            log.debug("Calc cell context menu install failed", exc_info=True)
        _initialized = True


def _dispatch_command(command: str, ctx: Any | None = None) -> None:
    bootstrap(ctx)
    handler = get_action_handler(command)
    if handler:
        try:
            handler()
        except Exception:
            log.exception("Action %s failed", command)
        return
    log.warning("Unhandled LibrePy command: %s", command)


class MainBootstrapJob(unohelper.Base, XJobExecutor, XJob):
    def __init__(self, ctx) -> None:
        self.ctx = ctx

    def execute(self, Arguments) -> tuple[()]:
        try:
            from plugin.framework.config import init_config

            init_config(self.ctx)
        except Exception:
            pass
        try:
            bootstrap(self.ctx)
            init_logging(self.ctx)
        except Exception as e:
            log.exception("LibrePy bootstrap failed: %s", e)
        return ()

    def trigger(self, Event) -> None:
        bootstrap(self.ctx)
        init_logging(self.ctx)
        args = Event
        if args and isinstance(args, str) and "." in args:
            cmd = args[7:] if args.startswith("plugin.") else args
            _dispatch_command(cmd, self.ctx)


class DispatchHandler(unohelper.Base, XDispatch, XDispatchProvider, XInitialization, XServiceInfo):
    IMPL_NAME = "org.extension.librepy.DispatchHandler"
    SERVICE_NAMES = ("com.sun.star.frame.ProtocolHandler",)

    def __init__(self, ctx) -> None:
        self.ctx = ctx

    def initialize(self, aArguments) -> None:
        pass

    def getImplementationName(self) -> str:
        return self.IMPL_NAME

    def supportsService(self, ServiceName: str) -> bool:
        return ServiceName in self.SERVICE_NAMES

    def getSupportedServiceNames(self) -> tuple[str, ...]:
        return self.SERVICE_NAMES

    def queryDispatch(self, URL: UnoURL, TargetFrameName: str, SearchFlags: int) -> XDispatch:  # pyright: ignore[reportIncompatibleMethodOverride]
        if matches_librepy_dispatch_url(URL):
            return cast("XDispatch", self)
        return cast("XDispatch", None)

    def queryDispatches(self, Requests: tuple[DispatchDescriptor, ...]) -> tuple[XDispatch, ...]:  # pyright: ignore[reportIncompatibleMethodOverride]
        return tuple(self.queryDispatch(r.FeatureURL, r.FrameName, r.SearchFlags) for r in Requests)

    def dispatch(self, URL, Arguments) -> None:
        command = dispatch_command_from_url(URL)
        try:
            bootstrap(self.ctx)
            init_logging(self.ctx)
            wa_log.warning(
                "LibrePy dispatch: command=%r complete=%r path=%r",
                command,
                getattr(URL, "Complete", ""),
                getattr(URL, "Path", ""),
            )
            _dispatch_command(command, self.ctx)
        except Exception as e:
            log_exception(e, context="LibrePy dispatch")
            from plugin.chatbot.dialogs import msgbox
            from plugin.framework.i18n import _

            msgbox(self.ctx, _("Dispatch Error"), _(str(e)), box_type=3)

    def addStatusListener(self, Control, URL) -> None:
        pass

    def removeStatusListener(self, Control, URL) -> None:
        pass


g_ImplementationHelper = unohelper.ImplementationHelper()
g_ImplementationHelper.addImplementation(
    MainBootstrapJob,
    "org.extension.librepy.Main",
    ("com.sun.star.task.Job",),
)
g_ImplementationHelper.addImplementation(DispatchHandler, DispatchHandler.IMPL_NAME, DispatchHandler.SERVICE_NAMES)
