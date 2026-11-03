# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""LibrePy UNO bootstrap: scientific Python menus and =PY() without chat/MCP."""

from __future__ import annotations

import logging
import os
import sys
from typing import TYPE_CHECKING, Any, ClassVar

_ext_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ext_root not in sys.path:
    sys.path.insert(0, _ext_root)

from plugin.framework.uno_bootstrap import ensure_plugin_on_path

ensure_plugin_on_path(
    __file__,
    levels_up=2,
    also_add_plugin_dir=True,
    also_add_lib=True,
    also_add_vendor=True,
)

import unohelper

if TYPE_CHECKING:
    from com.sun.star.util import URL as UnoURL

from plugin.framework.logging import init_logging, log as wa_log, log_exception
from plugin.framework.constants import EXTENSION_ID_LIBREPY
from plugin.framework.url_utils import dispatch_command_from_url

EXTENSION_ID = EXTENSION_ID_LIBREPY

log = logging.getLogger(__name__)

from plugin.bootstrap import (
    BootstrapState,
    bootstrap_extension,
    MainBootstrapJob as BaseMainBootstrapJob,
)

_bootstrap_state: BootstrapState = BootstrapState.UNINITIALIZED
_bootstrap_error: BaseException | None = None

from plugin.framework.main_shared import (
    ExtensionProtocolHandler,
    get_action_handler,
    open_dialog_safely,
    register_action_handler,
    report_action_failure,
)
from plugin.menu_handlers import register_common_handlers


def _register_librepy_handlers() -> None:
    def _open_settings() -> None:
        from plugin.librepy.settings import open_librepy_settings

        open_dialog_safely(open_librepy_settings, "Failed to open settings")

    register_action_handler("main", "settings", _open_settings)
    register_common_handlers()


def _librepy_bootstrap_finish(ctx: Any) -> None:
    global _bootstrap_state, _bootstrap_error

    _register_librepy_handlers()
    _bootstrap_state = BootstrapState.INITIALIZED
    _bootstrap_error = None


def bootstrap(ctx: Any | None = None) -> None:
    bootstrap_extension(
        ctx,
        extension_id=EXTENSION_ID,
        finish=_librepy_bootstrap_finish,
        state=globals(),
    )


def _run_startup(ctx: Any) -> None:
    try:
        init_logging(ctx)
    except Exception:
        log.exception("LibrePy init_logging failed")
    try:
        bootstrap(ctx)
        from plugin.chatbot.extension_update_check import schedule_extension_update_check_once

        schedule_extension_update_check_once(ctx, EXTENSION_ID)
    except Exception as e:
        log.exception("LibrePy bootstrap failed: %s", e)


def _dispatch_command(command: str, ctx: Any | None = None) -> None:
    bootstrap(ctx)
    handler = get_action_handler(command)
    if handler:
        try:
            handler()
        except Exception as e:
            report_action_failure(command, e)
        return
    log.warning("Unhandled LibrePy command: %s", command)


class MainBootstrapJob(BaseMainBootstrapJob):
    def init_logging(self, ctx: Any) -> None:
        init_logging(ctx)

    def on_start(self, ctx: Any) -> None:
        try:
            bootstrap(ctx)
            from plugin.chatbot.extension_update_check import schedule_extension_update_check_once

            schedule_extension_update_check_once(ctx, EXTENSION_ID)
        except Exception as e:
            log.exception("LibrePy bootstrap failed: %s", e)

    def trigger(self, Event: str) -> None:
        _run_startup(self.ctx)
        if Event and "." in Event:
            cmd = Event[7:] if Event.startswith("plugin.") else Event
            _dispatch_command(cmd, self.ctx)


class DispatchHandler(ExtensionProtocolHandler):
    EXTENSION_ID: ClassVar[str] = EXTENSION_ID
    IMPL_NAME: ClassVar[str] = f"{EXTENSION_ID}.DispatchHandler"

    def dispatch(self, URL: UnoURL, Arguments: Any) -> None:
        command = dispatch_command_from_url(URL)
        try:
            init_logging(self.ctx)
            wa_log.debug(
                "LibrePy dispatch: command=%r complete=%r path=%r",
                command,
                getattr(URL, "Complete", ""),
                getattr(URL, "Path", ""),
            )
            _dispatch_command(command, self.ctx)
        except Exception as e:
            log_exception(e, context="LibrePy dispatch")
            try:
                report_action_failure(command, e)
            except Exception:
                log.exception("LibrePy dispatch error report failed")



g_ImplementationHelper = unohelper.ImplementationHelper()
g_ImplementationHelper.addImplementation(
    MainBootstrapJob,
    f"{EXTENSION_ID}.Main",
    ("com.sun.star.task.Job",),
)
g_ImplementationHelper.addImplementation(DispatchHandler, DispatchHandler.IMPL_NAME, DispatchHandler.SERVICE_NAMES)
