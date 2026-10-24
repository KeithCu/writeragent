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
from typing import Any, Callable

_ACTION_HANDLERS: dict[str, Callable[..., Any]] = {}
log = logging.getLogger(__name__)

_TESTS_AVAILABLE: bool | None = None


def tests_bundled() -> bool:
    """True when `plugin.tests` test modules are included in this build."""
    global _TESTS_AVAILABLE
    if _TESTS_AVAILABLE is None:
        try:
            import importlib.util

            _TESTS_AVAILABLE = importlib.util.find_spec("plugin.tests.conftest") is not None
        except Exception:
            _TESTS_AVAILABLE = False
    return bool(_TESTS_AVAILABLE)


def _show_tests_unavailable(test_name: str) -> None:
    """Show a message when test suites are not bundled (release builds)."""
    from plugin.chatbot.dialogs import msgbox
    from plugin.framework.uno_context import get_ctx

    msgbox(get_ctx(), test_name, "This WriterAgent build was packaged without the optional `plugin.tests` test modules.")


def _run_test_suite(module_name: str, doc_checker: Callable[[Any], bool], test_name: str) -> None:
    """Run a bundled in-OXT test suite on the UI thread and show the result.

    Must not use ``run_blocking_in_thread``: synchronous UNO tools reject calls from
    worker threads (see ``ToolBase.execute_safe``).
    """
    from plugin.chatbot.dialogs import msgbox

    if not tests_bundled():
        _show_tests_unavailable(test_name)
        return

    from plugin.framework.i18n import _
    from plugin.framework.uno_context import get_active_document, get_ctx

    ctx = get_ctx()
    try:
        log.info(f"_run_test_suite start: {test_name}")
        # What was wrong: test modules were imported during handler registration even when test suites were unbundled or never invoked.
        # How it happened: handlers passed an eagerly imported module object into _run_test_suite.
        # Why this change: pass the module name string and import dynamically only after tests_bundled() succeeds.
        import importlib

        test_module = importlib.import_module(module_name)
        from plugin.testing_runner import run_module_suite

        model = get_active_document(ctx)
        doc_model = model if (model and doc_checker(model)) else None
        log.debug(f"Calling run_module_suite for {test_name}")
        p, f, suite_log = run_module_suite(ctx, test_module, test_name, doc_model)
        log.info(f"_run_test_suite finished: {test_name}, p={p}, f={f}")
        msgbox(ctx, test_name, _("{0}: {1} passed, {2} failed.").format(test_name, p, f) + "\n\n" + "\n".join(suite_log))
    except Exception as e:
        log.exception("_run_test_suite failed: %s", test_name)
        msgbox(ctx, test_name, _("Tests failed to run: {0}").format(str(e)))


def register_core_handlers() -> None:
    """Register core application handlers during bootstrap."""
    from plugin.chatbot.dialog_views import settings_box
    from plugin.chatbot.eval_dashboard_ui import show_eval_dashboard
    from plugin.doc.doc_type import is_calc, is_draw, is_writer
    from plugin.framework.uno_context import get_active_document, get_ctx

    def _install_optional(fn: Callable[..., Any], *args: Any, message: str, level: int = logging.DEBUG) -> None:
        try:
            fn(*args)
        except Exception:
            log.log(level, message, exc_info=True)

    def _open_settings() -> None:
        open_dialog_safely(settings_box, "Failed to open settings")

    register_action_handler("main", "settings", _open_settings)
    register_action_handler("main", "EvaluationDashboard", lambda: open_dialog_safely(show_eval_dashboard, "Failed to show eval dashboard"))

    test_suites = (
        ("RunFormatTests", "plugin.tests.writer.test_format_uno", is_writer, "writer.format_tests"),
        ("RunCalcTests", "plugin.tests.calc.test_calc_uno", is_calc, "calc.tests"),
        ("RunCalcIntegrationTests", "plugin.tests.calc.test_calc_uno", is_calc, "calc.integration_tests"),
        ("RunDrawTests", "plugin.tests.draw.test_draw_uno", is_draw, "draw.tests"),
    )
    for action, mod_name, checker, test_name in test_suites:
        register_action_handler(
            "main",
            action,
            lambda m=mod_name, c=checker, t=test_name: _run_test_suite(m, c, t),
        )
    register_action_handler("main", "NoOp", lambda: None)

    # Register shared handlers (report_bug, run_python, edit_python, reset_python, vision settings, latex, textanalytics)
    register_common_handlers()

    def _convert_spreadsheet() -> None:
        from plugin.calc.spreadsheet_import.import_dialog import show_import_dialog
        open_dialog_safely(show_import_dialog, "Failed to open Convert Sheet to Python dialog")

    register_action_handler("calc", "convert_spreadsheet_to_python", _convert_spreadsheet)

    def _install_calc_menus() -> None:
        from plugin.calc.python.editor_context_menu import install_calc_cell_context_menu
        from plugin.notebook.notebook_controls import install_notebook_run_button_wiring

        install_calc_cell_context_menu(get_ctx())
        install_notebook_run_button_wiring(get_ctx())

    _install_optional(_install_calc_menus, message="Calc cell context menu install failed", level=logging.DEBUG)

    # What was wrong: the from-import sat outside _install_optional.
    # How it happened: only the call was wrapped, so ImportError left this
    # function. _bootstrap_state is already INITIALIZED, so bootstrap does not retry and
    # handlers after the import (search, review) never register.
    # Why this change: import inside the callable, same as _install_calc_menus.
    def _install_excel_py_auto_convert() -> None:
        from plugin.calc.excel_py_convert.auto_open import install_excel_py_auto_convert

        install_excel_py_auto_convert(get_ctx())

    _install_optional(_install_excel_py_auto_convert, message="Excel PY auto-convert on open install failed", level=logging.DEBUG)

    def _install_geometric_recalc() -> None:
        from plugin.calc.python.geometric_recalc import install_geometric_recalc

        install_geometric_recalc()

    _install_optional(_install_geometric_recalc, message="Geometric recalc install failed", level=logging.DEBUG)

    def _open_search_dialog() -> None:
        from plugin.embeddings.search_ui import show_search_dialog
        show_search_dialog(get_ctx())

    register_action_handler("embeddings", "search_dialog", _open_search_dialog)

    def _refresh_review_toolbar(model: Any) -> None:
        """Re-evaluate the review toolbar's visibility (hide once nothing is left to review).
        Also a catch-up point if changes were resolved via LibreOffice's native UI."""
        try:
            from plugin.writer.review_toolbar import refresh_review_toolbar

            refresh_review_toolbar(model)
        except Exception:  # UNO boundary: refresh_review_toolbar layout/controller access
            log.debug("review toolbar refresh failed", exc_info=True)

    def _resolve_change_at_cursor(accept: bool) -> None:
        from plugin.writer.inline_review import resolve_change_at_cursor, show_review_message

        c = get_ctx()
        model = get_active_document(c)
        if model is not None:
            ok, msg = resolve_change_at_cursor(model, c, accept)
            log.debug("inline review: accept=%s -> ok=%s msg=%s", accept, ok, msg)
            if not ok:
                show_review_message(c, msg)
            _refresh_review_toolbar(model)
        else:
            log.debug("inline review: no current component")

    register_action_handler("writer", "accept_change", lambda: _resolve_change_at_cursor(True))
    register_action_handler("writer", "reject_change", lambda: _resolve_change_at_cursor(False))

    def _resolve_all_agent(accept: bool) -> None:
        from plugin.writer.inline_review import resolve_all_with_feedback, show_review_message

        c = get_ctx()
        model = get_active_document(c)
        if model is not None:
            n, msg = resolve_all_with_feedback(model, c, accept)
            log.debug("inline review: resolve-all accept=%s -> %d changes", accept, n)
            show_review_message(c, msg)
            _refresh_review_toolbar(model)

    register_action_handler("writer", "review_accept_all", lambda: _resolve_all_agent(True))
    register_action_handler("writer", "review_reject_all", lambda: _resolve_all_agent(False))

    def _goto_adjacent_change(forward: bool) -> None:
        """Fast-travel (#2): move the cursor to the next/previous pending agent change, in document
        order, cycling. Drives the review toolbar's Next/Previous buttons (and any shortcut)."""
        from plugin.writer.inline_review import goto_adjacent_agent_change

        c = get_ctx()
        model = get_active_document(c)
        if model is None:
            return
        token = goto_adjacent_agent_change(model, forward)
        log.debug("review nav: forward=%s -> token=%s", forward, token)
        _refresh_review_toolbar(model)

    register_action_handler("writer", "review_next", lambda: _goto_adjacent_change(True))
    register_action_handler("writer", "review_prev", lambda: _goto_adjacent_change(False))

    def _install_writer_change_context_menu() -> None:
        from plugin.writer.change_context_menu import install_writer_change_context_menu

        install_writer_change_context_menu(get_ctx())

    _install_optional(_install_writer_change_context_menu, message="Writer change context menu install failed", level=logging.WARNING)

    def _install_review_toolbar() -> None:
        from plugin.writer.review_toolbar import install_review_toolbar

        install_review_toolbar(get_ctx())

    _install_optional(_install_review_toolbar, message="Review toolbar visibility install failed", level=logging.WARNING)


def register_action_handler(module_name: str, action_name: str, handler_func: Callable[..., Any]) -> None:
    """Register an action handler function."""
    _ACTION_HANDLERS[f"{module_name}.{action_name}"] = handler_func


def get_action_handler(command: str) -> Callable[..., Any] | None:
    """Get a registered action handler by command string."""
    return _ACTION_HANDLERS.get(command)


def _show_error_report(ctx: Any, detail: str, report_title: str, exc: BaseException) -> None:
    from plugin.framework.uno_context import get_ctx

    try:
        from plugin.chatbot.dialogs import msgbox_with_report
        from plugin.framework.i18n import _

        msgbox_with_report(
            ctx or get_ctx(),
            _("Error"),
            detail,
            box_type=3,
            reportable=True,
            report_title=report_title,
            report_extra=str(exc),
        )
    except Exception:
        log.exception("msgbox_with_report failed")


def report_action_failure(command: str, exc: BaseException) -> None:
    """Report an action handler exception via dialog and logging."""
    from plugin.framework.uno_context import get_ctx

    log.exception("Action %s failed", command)
    detail = f"{command} failed: {exc}"
    _show_error_report(get_ctx(), detail, command, exc)


def open_dialog_safely(dialog_func: Callable[..., Any], error_msg: str, *args: Any, **kwargs: Any) -> None:
    """Safely open a dialog with standardized error handling and reporting."""
    from plugin.framework.errors import UnoObjectError, is_disposed_exception
    from plugin.framework.uno_context import get_ctx

    try:
        dialog_func(get_ctx(), *args, **kwargs)
    except UnoObjectError as e:
        log.warning("UNO error opening dialog: %s", e.message)
    except Exception as e:
        if is_disposed_exception(e):
            log.debug("Dialog opening aborted: document disposed")
            return
        log.exception("%s", error_msg)
        # error_msg is a caller sentence, not a catalog msgid. Do not pass it through _().
        detail = f"{error_msg}: {e}"
        _show_error_report(get_ctx(), detail, error_msg, e)


def register_common_handlers() -> None:
    """Register handlers shared between the core extension and the main extension."""
    from plugin.framework.uno_context import get_ctx

    def _report_bug() -> None:
        from plugin.chatbot.bug_report import open_bug_report_in_browser

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

    def _edit_init_script() -> None:
        from plugin.calc.python.init_script_editor import open_init_script_editor

        open_init_script_editor(get_ctx())

    register_action_handler("scripting", "edit_init_script", _edit_init_script)

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
