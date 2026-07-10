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
from typing import Any, Callable, cast

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

import uno
import unohelper
from com.sun.star.frame import DispatchDescriptor, XDispatch, XDispatchProvider
from com.sun.star.lang import XInitialization, XServiceInfo
from com.sun.star.task import XJob, XJobExecutor
from com.sun.star.util import URL as UnoURL

from plugin.framework.logging import init_logging
from plugin.framework.uno_context import get_ctx, set_fallback_ctx, set_package_extension_id

EXTENSION_ID = "org.extension.librepy"
_DISPATCH_PROTOCOL = "org.extension.librepy:"

log = logging.getLogger(__name__)
_ACTION_HANDLERS: dict[str, Callable[[], Any]] = {}
_initialized = False
_init_lock = threading.Lock()


def register_action_handler(module_name: str, action_name: str, handler_func: Callable[[], Any]) -> None:
    _ACTION_HANDLERS[f"{module_name}.{action_name}"] = handler_func


def _open_dialog_safely(dialog_func, error_msg: str, *args, **kwargs) -> None:
    from plugin.framework.errors import DocumentDisposedError, UnoObjectError

    try:
        dialog_func(get_ctx(), *args, **kwargs)
    except DocumentDisposedError:
        log.debug("Dialog opening aborted: document disposed")
    except UnoObjectError as e:
        log.warning("UNO error opening dialog: %s", e.message)
    except Exception as e:
        log.exception("%s", error_msg)
        from plugin.chatbot.dialogs import msgbox_with_report
        from plugin.framework.i18n import _

        msgbox_with_report(
            get_ctx(),
            _("Error"),
            _(f"{error_msg}: {str(e)}"),
            box_type=3,
            reportable=True,
            report_title=error_msg,
            report_extra=str(e),
        )


def _register_librepy_handlers() -> None:
    from plugin.librepy.settings import open_librepy_settings

    register_action_handler("main", "settings", lambda: _open_dialog_safely(open_librepy_settings, "Failed to open settings"))

    def _run_python() -> None:
        from plugin.scripting.python_runner import run_python_dialog

        run_python_dialog(get_ctx())

    register_action_handler("scripting", "run_python_dialog", _run_python)

    def _edit_python_cell() -> None:
        from plugin.calc.python.editor import open_python_cell_editor

        open_python_cell_editor(get_ctx())

    register_action_handler("scripting", "edit_python_cell", _edit_python_cell)

    def _reset_python_session() -> None:
        from plugin.scripting.session_manager import reset_workbook_python_session

        reset_workbook_python_session(get_ctx())

    register_action_handler("scripting", "reset_python_session", _reset_python_session)

    def _open_vision_settings() -> None:
        from plugin.chatbot.module_config_dialog import show_vision_settings_dialog

        _open_dialog_safely(show_vision_settings_dialog, "Failed to open Vision OCR settings")

    register_action_handler("vision", "open_settings", _open_vision_settings)

    def _insert_latex() -> None:
        from plugin.writer.math.latex_dialog import insert_latex_math_dialog

        insert_latex_math_dialog(get_ctx())

    register_action_handler("writer", "insert_latex_dialog", _insert_latex)

    def _open_text_analytics() -> None:
        from plugin.scripting.text_analytics_ui import TextAnalyticsDialog

        TextAnalyticsDialog.show(get_ctx())

    register_action_handler("textanalytics", "open_dialog", _open_text_analytics)


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
    handler = _ACTION_HANDLERS.get(command)
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
        if URL.Protocol == "org.extension.librepy:":
            return cast("XDispatch", self)
        return cast("XDispatch", None)

    def queryDispatches(self, Requests: tuple[DispatchDescriptor, ...]) -> tuple[XDispatch, ...]:  # pyright: ignore[reportIncompatibleMethodOverride]
        return tuple(self.queryDispatch(r.FeatureURL, r.FrameName, r.SearchFlags) for r in Requests)

    def dispatch(self, URL, Arguments) -> None:
        from plugin.framework.logging import log_exception

        try:
            bootstrap(self.ctx)
            init_logging(self.ctx)
            _dispatch_command(URL.Path, self.ctx)
        except Exception as e:
            log_exception(e, context="LibrePy dispatch")
            from plugin.chatbot.dialogs import msgbox_with_report
            from plugin.framework.i18n import _

            msgbox_with_report(
                self.ctx,
                _("Dispatch Error"),
                _(str(e)),
                box_type=3,
                reportable=True,
                report_title="Dispatch Error",
                report_extra=str(e),
            )

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
