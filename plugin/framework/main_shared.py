# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Shared action dispatching and dialog helpers for main.py and main_core.py."""

from __future__ import annotations

import logging
from typing import Any, Callable

_ACTION_HANDLERS: dict[str, Callable[..., Any]] = {}
log = logging.getLogger(__name__)


def register_action_handler(module_name: str, action_name: str, handler_func: Callable[..., Any]) -> None:
    """Register an action handler function."""
    _ACTION_HANDLERS[f"{module_name}.{action_name}"] = handler_func


def get_action_handler(command: str) -> Callable[..., Any] | None:
    """Get a registered action handler by command string."""
    return _ACTION_HANDLERS.get(command)


def open_dialog_safely(dialog_func: Callable[..., Any], error_msg: str, *args: Any, **kwargs: Any) -> None:
    """Safely open a dialog with standardized error handling and reporting."""
    from plugin.framework.errors import DocumentDisposedError, UnoObjectError
    from plugin.framework.uno_context import get_ctx

    try:
        dialog_func(get_ctx(), *args, **kwargs)
    except DocumentDisposedError:
        log.debug("Dialog opening aborted: document disposed")
    except UnoObjectError as e:
        log.warning("UNO error opening dialog: %s", e.message)
    except Exception as e:
        log.exception("%s", error_msg)
        # Try to use msgbox_with_report if available, otherwise msgbox
        try:
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
        except Exception:
            try:
                from plugin.chatbot.dialogs import msgbox
                from plugin.framework.i18n import _
                msgbox(
                    get_ctx(),
                    _("Error"),
                    _(f"{error_msg}: {str(e)}"),
                    box_type=3,
                )
            except Exception:
                pass


def register_common_handlers() -> None:
    """Register handlers shared between the core extension and the main extension."""
    from plugin.framework.uno_context import get_ctx

    def _report_bug() -> None:
        from plugin.framework.bug_report import open_bug_report_in_browser
        open_bug_report_in_browser(get_ctx(), title="Bug report")

    register_action_handler("main", "report_bug", _report_bug)

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
        open_dialog_safely(show_vision_settings_dialog, "Failed to open Vision OCR settings")

    register_action_handler("vision", "open_settings", _open_vision_settings)

    def _insert_latex() -> None:
        from plugin.writer.math.latex_dialog import insert_latex_math_dialog
        insert_latex_math_dialog(get_ctx())

    register_action_handler("writer", "insert_latex_dialog", _insert_latex)

    def _open_text_analytics() -> None:
        from plugin.scripting.text_analytics_ui import TextAnalyticsDialog
        TextAnalyticsDialog.show(get_ctx())

    register_action_handler("textanalytics", "open_dialog", _open_text_analytics)
