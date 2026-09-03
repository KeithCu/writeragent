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
from typing import TYPE_CHECKING, Any, cast

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
from plugin.framework.constants import EXTENSION_ID_LIBREPY
from plugin.framework.uno_context import get_ctx, set_fallback_ctx, set_package_extension_id
from plugin.framework.url_utils import dispatch_command_from_url, matches_librepy_dispatch_url

EXTENSION_ID = EXTENSION_ID_LIBREPY
_DISPATCH_PROTOCOL = EXTENSION_ID + ":"

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
        if ctx is not None:
            from plugin.framework.queue_executor import default_executor

            default_executor.set_context(ctx)
        from plugin.framework.config import init_config

        init_config(ctx)
        try:
            from plugin.scripting.native_binaries import ensure_downloaded_audio_on_path

            ensure_downloaded_audio_on_path()
        except Exception:
            log.debug("Native binary path setup failed", exc_info=True)
        from plugin.framework.i18n import init_i18n

        init_i18n(ctx)
        _register_librepy_handlers()
        try:
            from plugin.calc.python.editor_context_menu import install_calc_cell_context_menu

            install_calc_cell_context_menu(ctx)
        except Exception:
            log.debug("Calc cell context menu install failed", exc_info=True)
        try:
            from plugin.calc.excel_py_convert.auto_open import install_excel_py_auto_convert

            install_excel_py_auto_convert(ctx)
        except Exception:
            log.debug("Excel PY auto-convert on open install failed", exc_info=True)
        try:
            from plugin.calc.python.geometric_recalc import install_geometric_recalc

            install_geometric_recalc()
        except Exception:
            log.debug("Geometric recalc install failed", exc_info=True)
        try:
            from plugin.notebook.notebook_controls import install_notebook_run_button_wiring

            install_notebook_run_button_wiring(ctx)
        except Exception:
            log.debug("Notebook run button wiring install failed", exc_info=True)
        _initialized = True


def _schedule_update_check(ctx: Any) -> None:
    try:
        from plugin.chatbot.extension_update_check import schedule_extension_update_check_once

        schedule_extension_update_check_once(ctx, EXTENSION_ID)
    except Exception as e:
        log.warning("extension update check schedule failed: %s", e)


_NOTEBOOK_RUN_CELL_PREFIX = "notebook.run_cell."


def _dispatch_command(command: str, ctx: Any | None = None) -> None:
    bootstrap(ctx)
    if command.startswith(_NOTEBOOK_RUN_CELL_PREFIX):
        from plugin.notebook.notebook_runner import run_cell_by_hex

        run_cell_by_hex(get_ctx() if ctx is None else ctx, command[len(_NOTEBOOK_RUN_CELL_PREFIX) :])
        return
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
            init_logging(self.ctx)
            bootstrap(self.ctx)
            _schedule_update_check(self.ctx)
        except Exception as e:
            log.exception("LibrePy bootstrap failed: %s", e)
        return ()

    def trigger(self, Event) -> None:
        try:
            init_logging(self.ctx)
            bootstrap(self.ctx)
            _schedule_update_check(self.ctx)
        except Exception as e:
            log.exception("LibrePy trigger bootstrap failed: %s", e)
        args = Event
        if args and isinstance(args, str) and "." in args:
            cmd = args[7:] if args.startswith("plugin.") else args
            _dispatch_command(cmd, self.ctx)


class DispatchHandler(unohelper.Base, XDispatch, XDispatchProvider, XInitialization, XServiceInfo):
    IMPL_NAME = f"{EXTENSION_ID}.DispatchHandler"
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
        # UNO allows null dispatch; stub types reject None → cast via object.
        return cast("XDispatch", cast("object", None))

    def queryDispatches(self, Requests: tuple[DispatchDescriptor, ...]) -> tuple[XDispatch, ...]:  # pyright: ignore[reportIncompatibleMethodOverride]
        return tuple(self.queryDispatch(r.FeatureURL, r.FrameName, r.SearchFlags) for r in Requests)

    def dispatch(self, URL, Arguments) -> None:
        command = dispatch_command_from_url(URL)
        try:
            init_logging(self.ctx)
            bootstrap(self.ctx)
            wa_log.debug(
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
    f"{EXTENSION_ID}.Main",
    ("com.sun.star.task.Job",),
)
g_ImplementationHelper.addImplementation(DispatchHandler, DispatchHandler.IMPL_NAME, DispatchHandler.SERVICE_NAMES)
