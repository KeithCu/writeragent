# WriterAgent - shared menu handlers (LibrePy + WriterAgent)
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Menu handlers shared by WriterAgent and LibrePy without importing ``plugin.main``."""

from __future__ import annotations

import logging
from typing import Any

from plugin.framework.main_shared import open_dialog_safely, register_action_handler

log = logging.getLogger("writeragent.menu_handlers")


def register_common_handlers() -> None:
    """Register handlers shared between the core extension and the main extension."""
    from plugin.framework.uno_context import get_ctx

    try:
        from plugin.calc.python.editor_context_menu import install_calc_cell_context_menu

        install_calc_cell_context_menu(get_ctx())
    except Exception:
        log.debug("Calc cell context menu install failed", exc_info=True)

    try:
        from plugin.calc.excel_py_convert.auto_open import install_excel_py_auto_convert

        install_excel_py_auto_convert(get_ctx())
    except Exception:
        log.debug("Excel PY auto-convert on open install failed", exc_info=True)

    try:
        from plugin.calc.python.geometric_recalc import install_geometric_recalc

        install_geometric_recalc()
    except Exception:
        log.debug("Geometric recalc install failed", exc_info=True)

    try:
        from plugin.notebook.notebook_controls import install_notebook_run_button_wiring

        install_notebook_run_button_wiring(get_ctx())
    except Exception:
        log.debug("Notebook run button wiring install failed", exc_info=True)

    def _report_bug(frame: Any | None = None) -> None:
        from plugin.chatbot.bug_report import open_bug_report_in_browser

        open_bug_report_in_browser(get_ctx(), title="Bug report")

    register_action_handler("main", "report_bug", _report_bug)

    def _run_python(frame: Any | None = None) -> None:
        from plugin.scripting.python_runner import run_python_dialog

        run_python_dialog(get_ctx())

    register_action_handler("scripting", "run_python_dialog", _run_python)

    def _edit_python_cell(frame: Any | None = None) -> None:
        from plugin.calc.python.editor import open_python_cell_editor

        open_python_cell_editor(get_ctx())

    register_action_handler("scripting", "edit_python_cell", _edit_python_cell)

    def _reset_python_session(frame: Any | None = None) -> None:
        from plugin.framework.worker_pool import run_in_background
        from plugin.scripting.session_manager import reset_workbook_python_session

        ctx = get_ctx()
        run_in_background(lambda: reset_workbook_python_session(ctx), name="reset-python-session")

    register_action_handler("scripting", "reset_python_session", _reset_python_session)

    def _notebook_run_all(frame: Any | None = None) -> None:
        from plugin.notebook.notebook_runner import run_all_from_menu

        run_all_from_menu(get_ctx(), frame)

    register_action_handler("notebook", "run_all", _notebook_run_all)

    def _notebook_run_from_here(frame: Any | None = None) -> None:
        from plugin.notebook.notebook_runner import run_from_here_from_menu

        run_from_here_from_menu(get_ctx(), frame)

    register_action_handler("notebook", "run_from_here", _notebook_run_from_here)

    def _notebook_stop(frame: Any | None = None) -> None:
        from plugin.notebook.notebook_runner import stop_from_menu

        stop_from_menu(get_ctx(), frame)

    register_action_handler("notebook", "stop", _notebook_stop)

    def _edit_init_script(frame: Any | None = None) -> None:
        from plugin.calc.python.init_script_editor import open_init_script_editor

        open_init_script_editor(get_ctx())

    register_action_handler("scripting", "edit_init_script", _edit_init_script)

    def _open_vision_settings(frame: Any | None = None) -> None:
        from plugin.chatbot.module_config_dialog import show_vision_settings_dialog

        open_dialog_safely(show_vision_settings_dialog, "Failed to open Vision OCR settings")

    register_action_handler("vision", "open_settings", _open_vision_settings)

    def _insert_latex(frame: Any | None = None) -> None:
        from plugin.writer.math.latex_dialog import insert_latex_math_dialog

        insert_latex_math_dialog(get_ctx())

    register_action_handler("writer", "insert_latex_dialog", _insert_latex)

    def _open_text_analytics(frame: Any | None = None) -> None:
        from plugin.scripting.text_analytics_ui import TextAnalyticsDialog

        TextAnalyticsDialog(get_ctx())

    register_action_handler("textanalytics", "open_dialog", _open_text_analytics)
