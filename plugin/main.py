# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2024 John Balis
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
from __future__ import annotations

import os
import sys

# --- Minimal stdlib-only bootstrap (MUST be before any "from plugin..." import) ---
# unopkg / pythonloader can load this file during `make deploy` (writeRegistryInfo)
# before the extension root is on sys.path. We must fix that using only stdlib.
_ext_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ext_root not in sys.path:
    sys.path.insert(0, _ext_root)

import logging
import threading

from plugin.framework.uno_bootstrap import ensure_plugin_on_path

# Central bootstrap for UNO-loaded entry points.
# This replaces the previous manual sys.path manipulation that was duplicated
# across main.py and the Calc add-ins.
ensure_plugin_on_path(
    __file__,
    levels_up=2,                    # plugin/main.py → plugin/ → extension root
    also_add_plugin_dir=True,
    also_add_lib=True,
    also_add_vendor=True,           # root-level vendor/ when present (optional dev wheels)
)

def _install_native_binaries() -> None:
    try:
        from plugin.scripting.native_binaries import ensure_native_binaries_on_path

        ensure_native_binaries_on_path()
    except Exception:
        logging.getLogger(__name__).debug("ensure_native_binaries_on_path failed", exc_info=True)


_install_native_binaries()

import unohelper
from typing import TYPE_CHECKING, Any, Callable, ClassVar, cast

if TYPE_CHECKING:
    from types import ModuleType

    from com.sun.star.util import URL as UnoURL
    from plugin.framework.module_base import ModuleBase
    from plugin.framework.tool import ToolRegistry

officehelper: ModuleType | None = None
try:
    import officehelper as _officehelper_module

    officehelper = _officehelper_module
except ImportError:
    pass

from plugin.framework.logging import init_logging
import uno

from com.sun.star.task import XJobExecutor, XJob
from com.sun.star.frame import DispatchDescriptor, XDispatch, XDispatchProvider
from com.sun.star.lang import XInitialization, XServiceInfo

from plugin.framework.constants import EXTENSION_ID_WRITERAGENT
from plugin.framework.uno_context import (
    get_active_document,
    get_extension_url,
    get_ctx,
    get_service_manager,
    menu_icon_asset_url,
    set_fallback_ctx,
    set_package_extension_id,
    uno_same,
)
from plugin.framework.thread_guard import background, on_main_thread
from plugin.framework.url_utils import dispatch_command_from_url, matches_extension_dispatch_url

EXTENSION_ID = EXTENSION_ID_WRITERAGENT
_DISPATCH_PROTOCOL = EXTENSION_ID + ":"

# ---------------------------------------------------------------------------
# Bootstrapping (Dynamic discovery from loaded manifest)
# ---------------------------------------------------------------------------

_services = None
log = logging.getLogger(__name__)

from plugin.framework.main_shared import (
    register_action_handler,
    get_action_handler,
    register_common_handlers,
    open_dialog_safely as _open_dialog_safely,
)


_tools: ToolRegistry | None = None
_modules: list[ModuleBase] = []
# What was wrong: _initialized remained True even if module loading or
# background start failed, permanently wedging startup so subsequent calls
# returned immediately and menus only logged "No handler or module found".
# How it happened: _initialized was set early to prevent re-entrant calls
# during module imports, but unhandled exceptions after that point never cleared it.
# Why this change: retry only when _modules is still empty; otherwise a second
# pass would construct another registry and start background services again.
_bootstrapping = False
_initialized = False
_bootstrap_error: BaseException | None = None
_terminate_listener: Any | None = None


def _install_terminate_listener(ctx: Any) -> None:
    """Register a desktop XTerminateListener to clean up child processes on app exit."""
    global _terminate_listener
    # The first successful addTerminateListener is process-lifetime and is not
    # removed or replaced; a failed install leaves the global None so the next
    # bootstrap retries.
    if _terminate_listener is not None:
        return
    try:
        from plugin.framework.uno_context import get_desktop

        desktop = get_desktop(ctx)
        if desktop is None:
            return

        from com.sun.star.frame import XTerminateListener

        class _TerminateListener(unohelper.Base, XTerminateListener):
            def queryTermination(self, Event: Any) -> None:
                pass

            def notifyTermination(self, Event: Any) -> None:
                try:
                    from plugin.audio.kokoro_pool import shutdown_kokoro_pool

                    shutdown_kokoro_pool()
                except Exception:
                    log.exception("Error shutting down Kokoro pool on desktop termination")

                # shutdown_all kill_trees registered children and does not signal
                # register=False venv workers. Do not call
                # PythonWorkerManager.shutdown_all here either: it joins the reap on
                # this thread. Idle children exit on stdin EOF. Linux native stalls
                # use PR_SET_PDEATHSIG in the harness.
                try:
                    from plugin.framework.managed_subprocess import shutdown_all

                    shutdown_all()
                except Exception:
                    log.exception("Error shutting down managed subprocesses on desktop termination")

            def disposing(self, Source: Any) -> None:
                pass

        listener = _TerminateListener()
        desktop.addTerminateListener(listener)
        _terminate_listener = listener
    except Exception:
        log.exception("Failed to install terminate listener")

def _schedule_extension_update_check_once(ctx: Any) -> None:  # pyright: ignore[reportUnusedFunction]  # called from panel_wiring after sidebar init
    """Run weekly update check at most once per process per product, after init_logging."""
    from plugin.chatbot.extension_update_check import schedule_extension_update_check_once

    schedule_extension_update_check_once(ctx, EXTENSION_ID)


def get_services() -> Any:
    global _services
    if _services is None:
        bootstrap()
    if _services is None:
        from plugin.framework.errors import WriterAgentException

        raise WriterAgentException(
            "Service registry is unavailable because bootstrap did not finish.",
            code="BOOTSTRAP_FAILED",
        )
    return _services


def get_tools() -> ToolRegistry:
    global _tools
    if _tools is None:
        bootstrap()
    if _tools is None:
        from plugin.framework.errors import WriterAgentException

        raise WriterAgentException(
            "Tool registry is unavailable because bootstrap did not finish.",
            code="BOOTSTRAP_FAILED",
        )
    return _tools


def bootstrap(ctx: Any | None = None) -> None:
    """Bootstraps the service container and modules.

    This is currently triggered eagerly by Jobs.xcu on OnStartApp to ensure the
    MCP server starts automatically in headless mode. We could make this lazy
    again in the future if startup performance becomes a concern.
    """
    global _services, _tools, _modules, _initialized, _bootstrap_error, _bootstrapping

    # Off-thread get_menu_text and _get_menu_icon depend on returning early
    # when initialized, before the on_main_thread check raises BOOTSTRAP_OFF_THREAD.
    if _initialized:
        return

    from plugin.framework.thread_guard import on_main_thread

    if not on_main_thread():
        from plugin.framework.errors import WriterAgentException

        raise WriterAgentException(
            "WriterAgent bootstrap must run on the main thread before tools or services are used.",
            code="BOOTSTRAP_OFF_THREAD",
        )

    if _bootstrapping:
        return

    # What was wrong: _init_lock wrapped only the _initialized recheck, then
    # released before any init work.
    # How it happened: _bootstrapping was added around the old with-block and
    # the body was dedented out of the lock (bd07cb792).
    # Why this change: bootstrap() already raises BOOTSTRAP_OFF_THREAD on any
    # other thread, and a nested call on this thread returns at _bootstrapping.
    # The lock excluded nobody. It is also not reentrant, so putting the body
    # back under it would deadlock if a nested call ever reached it.
    _bootstrapping = True
    try:
        # 1. Basic UNO context
        if ctx is None:
            from plugin.framework.uno_context import get_ctx

            ctx = get_ctx()

        if ctx is not None:
            from plugin.framework.queue_executor import default_executor

            # get_ctx() prefers this over uno.getComponentContext(). LibrePy
            # already pins it; without it WriterAgent lookups can hit a
            # different context (no VCL, wrong package, Desktop segfault).
            set_fallback_ctx(ctx)
            default_executor.set_context(ctx)

        set_package_extension_id(EXTENSION_ID)

        from plugin.framework.config import init_config

        init_config(ctx)

        # 2. Service Container
        from plugin.framework.service import ServiceRegistry

        _services = ServiceRegistry()
        _services.register("uno", ctx)

        # Skip terminate listener when running under the eval harness.
        # The eval client already has a remote Desktop; this listener would shut down client subprocesses on office teardown.
        if os.environ.get("WRITERAGENT_EVAL_HARNESS") != "1":
            _install_terminate_listener(ctx)

        # 3. Core Services (Framework)
        from plugin.framework.config_service import ConfigService
        from plugin.doc.document_helpers import DocumentService
        from plugin.framework.event_bus import get_event_bus

        _services.register("config", ConfigService())
        _services.register("document", DocumentService())
        _services.register("events", get_event_bus())

        # 4. Tool Registry
        from plugin.framework.tool import ToolRegistry

        _tools = ToolRegistry(_services)
        _services.register("tools", _tools)

        # 4b. Main Thread Execution Service
        from plugin.framework.queue_executor import default_executor

        _services.register("main_thread", default_executor)

        # Wire config service to events, then load module.yaml public flags.
        # initialize() fills _manifest. set_events alone leaves it empty, so
        # a cross-module read of a key marked public in module.yaml is denied
        # as private. initialize() does not start a second service lifecycle.
        config_svc = _services.get("config")
        events_svc = _services.get("events")
        if config_svc and events_svc:
            config_svc.set_events(events_svc)
        if config_svc is not None:
            config_svc.initialize(ctx)

        # Initialize i18n
        from plugin.framework.i18n import init_i18n

        init_i18n(ctx)

        # Set initialized early to prevent recursive calls from re-running bootstrap
        # but after _services and _tools are created.
        _initialized = True

        # 5. Load manifest and initialize modules
        from plugin.framework.module_base import ModuleLoader

        module_phase_failed = False
        try:
            _modules.extend(ModuleLoader.load_modules(_services))

            # 6. Background phase: modules that start listeners/servers (e.g. McpModule when MCP enabled)
            for mod in _modules:
                mod.start_background(_services)
        except Exception as exc:
            module_phase_failed = True
            log.exception("bootstrap failed after the init flag was set")
            _bootstrap_error = exc
            if not _modules:
                _initialized = False
                return
            # What was wrong: a module start_background failure used to return before
            # core handlers, so Settings and the other register_action_handler calls never ran.
            # How it happened: the except block unconditionally returned on any error.
            # Why this change: fall through so core handlers and menu updates register;
            # the error is kept so an unhandled command still raises BOOTSTRAP_FAILED.

        try:
            # Wire event bus into config service
            events_svc = _services.get("events")
            if events_svc:
                # Subscribe to menu:update for dynamic menu text + icons.
                # execute runs this @background function on the main thread;
                # dispatch already queues it with run_in_background.
                from plugin.framework.worker_pool import run_in_background

                events_svc.subscribe("menu:update", lambda **kw: run_in_background(notify_menu_update))

            if os.environ.get("WRITERAGENT_TESTING") != "1" and os.environ.get("WRITERAGENT_EVAL_HARNESS") != "1":
                # Pre-load icons into ImageManager so first menu display has them.
                # Eval harness is headless with no VCL pump — posting icons to "main"
                # trips the UNO thread guard and can segfault soffice (issue #402 family).
                from plugin.framework.worker_pool import run_in_background

                run_in_background(_update_menu_icons)

            # Headless eval connects via UNO pipe; menu/context-menu install
            # calls get_desktop() in this process and can segfault soffice.
            if os.environ.get("WRITERAGENT_EVAL_HARNESS") != "1":
                _register_core_handlers()
        except Exception as exc:
            log.exception("bootstrap failed after the init flag was set")
            if _bootstrap_error is None:
                _bootstrap_error = exc
            return

        if not module_phase_failed:
            _bootstrap_error = None
    finally:
        _bootstrapping = False


def _register_core_handlers() -> None:
    """Register core application handlers during bootstrap."""
    from plugin.chatbot.dialog_views import settings_box
    from plugin.chatbot.eval_dashboard_ui import show_eval_dashboard
    from plugin.doc.doc_type import is_writer, is_calc, is_draw

    def _install_optional(fn: Callable[..., Any], *args: Any, message: str, level: int = logging.DEBUG) -> None:
        try:
            fn(*args)
        except Exception:
            log.log(level, message, exc_info=True)

    def _open_settings() -> None:
        _open_dialog_safely(settings_box, "Failed to open settings")

    register_action_handler("main", "settings", _open_settings)
    register_action_handler("main", "EvaluationDashboard", lambda: _open_dialog_safely(show_eval_dashboard, "Failed to show eval dashboard"))

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
        _open_dialog_safely(show_import_dialog, "Failed to open Convert Sheet to Python dialog")

    register_action_handler("calc", "convert_spreadsheet_to_python", _convert_spreadsheet)

    def _install_calc_menus() -> None:
        from plugin.calc.python.editor_context_menu import install_calc_cell_context_menu
        from plugin.notebook.notebook_controls import install_notebook_run_button_wiring

        install_calc_cell_context_menu(get_ctx())
        install_notebook_run_button_wiring(get_ctx())

    _install_optional(_install_calc_menus, message="Calc cell context menu install failed", level=logging.DEBUG)

    # What was wrong: the from-import sat outside _install_optional.
    # How it happened: only the call was wrapped, so ImportError left this
    # function. _initialized is already True, so bootstrap does not retry and
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
        except Exception:
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



# ── Dynamic menu text infrastructure ─────────────────────────────────

_status_listeners: list[tuple[Any, Any]] = []  # [(listener, url)]
_status_lock = threading.Lock()

_TESTS_AVAILABLE = None


def _tests_bundled() -> bool:
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
    try:
        from plugin.chatbot.dialogs import msgbox

        msgbox(get_ctx(), test_name, "This WriterAgent build was packaged without the optional `plugin.tests` test modules.")
    except Exception:
        # What was wrong: failure to show dialog was silently ignored without logging.
        # How it happened: bare except: pass hid UI or dialog creation errors.
        # Why this change: log the exception while still avoiding breaking callers.
        log.exception("test unavailable dialog failed: %s", test_name)


def _run_test_suite(module_name: str, doc_checker: Callable[[Any], bool], test_name: str) -> None:
    """Run a bundled in-OXT test suite on the UI thread and show the result.

    Must not use ``run_blocking_in_thread``: synchronous UNO tools reject calls from
    worker threads (see ``ToolBase.execute_safe``).
    """
    from plugin.chatbot.dialogs import msgbox

    if not _tests_bundled():
        _show_tests_unavailable(test_name)
        return

    from plugin.framework.i18n import _

    ctx = get_ctx()
    try:
        log.info(f"_run_test_suite start: {test_name}")
        # What was wrong: test modules were imported during handler registration even when test suites were unbundled or never invoked.
        # How it happened: handlers passed an eagerly imported module object into _run_test_suite.
        # Why this change: pass the module name string and import dynamically only after _tests_bundled() succeeds.
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


def _split_command(command: str) -> tuple[str, str] | None:
    """Split a command into (module_name, action) at the first dot, or None if dot <= 0."""
    dot = command.find(".")
    if dot <= 0:
        return None
    return command[:dot], command[dot + 1 :]


def _find_module(mod_name: str) -> ModuleBase | None:
    for mod in _modules:
        if mod.name == mod_name:
            return mod
    return None


def _dispatch_command(command: str) -> None:
    """Dispatch command using handler registry, falling back to module actions."""
    bootstrap()
    if command.startswith("chatbot.debug_sidebar"):
        # Dev/debug only. sidebar_test_hooks is omitted from release OXTs.
        try:
            from plugin.chatbot.sidebar_test_hooks import handle_debug_sidebar_command

            handle_debug_sidebar_command(command)
        except ImportError:
            log.debug("debug_sidebar omitted (release)")
        except Exception:
            log.exception("chatbot.debug_sidebar failed")
        return
    # First try the action registry
    handler = get_action_handler(command)
    if handler:
        try:
            handler()
        except Exception as e:
            from plugin.framework.main_shared import report_action_failure

            report_action_failure(command, e)
        return

    # If no handler, check for module delegation
    parts = _split_command(command)
    if parts is not None:
        mod_name, action = parts

        # Module actions
        mod = _find_module(mod_name)
        if mod is not None:
            mod.on_action(action)
            return

    if _bootstrap_error is not None:
        from plugin.framework.errors import WriterAgentException

        raise WriterAgentException(
            f"WriterAgent did not finish starting: {_bootstrap_error}",
            code="BOOTSTRAP_FAILED",
        ) from _bootstrap_error

    if parts is None:
        log.warning("Unhandled command: %s", command)
        return

    log.warning("No handler or module found for command: %s", command)


# Hardcoded fallback for static Addons.xcu items so they get translated
# without needing dynamic state in their respective modules.
# Translate at lookup because the locale is not ready at import.
_STATIC_MENU_TITLES: dict[str, str] = {
    "chatbot.extend_selection": "Extend Selection",
    "chatbot.edit_selection": "Edit Selection",
    "main.settings": "Settings",
    "main.report_bug": "Report bug...",
    "mcp.toggle_server": "Toggle MCP Server",
    "mcp.server_status": "MCP Server Status",
    # addStatusListener pushes get_menu_text() via FeatureStateEvent.
    # Returning None leaves the en-US Addons.xcu string (Vision OCR was
    # absent from this map). A raw English literal overwrites the catalog
    # ("Debug"). Use the same literal map as the other WriterAgent
    # menu titles so the JA msgstrs apply.
    "vision.open_settings": "Vision OCR Settings...",
    "main.NoOp": "Debug",
    "main.RunFormatTests": "Run format tests",
    "main.RunCalcTests": "Run calc tests",
    "main.RunCalcIntegrationTests": "Run Calc API integration tests",
    "main.RunDrawTests": "Run draw tests",
    "main.EvaluationDashboard": "Evaluation Dashboard",
    "scripting.run_python_dialog": "Run Python Script...",
    "scripting.edit_python_cell": "Edit Python in Cell...",
    "scripting.reset_python_session": "Reset Python Session",
    "notebook.run_all": "Run All",
    "notebook.run_from_here": "Run From Here",
    "notebook.stop": "Stop",
    "writer.insert_latex_dialog": "Edit LaTeX Math...",
    # None would leave the en-US toolbar title; map these so translations apply
    "writer.review_prev": "◀ Prev",
    "writer.review_next": "Next ▶",
    "writer.review_accept_all": "Accept all",
    "writer.review_reject_all": "Reject all",
    "embeddings.search_dialog": "Search Nearby Files...",
    "textanalytics.open_dialog": "Text Analytics...",
    "calc.convert_spreadsheet_to_python": "Convert Sheet to Python...",
}


def get_menu_text(command: str) -> str | None:
    """Get dynamic menu text for a command, or None for default."""
    bootstrap()
    parts = _split_command(command)
    if parts is None:
        return None
    mod_name, action = parts

    from plugin.framework.i18n import _

    # Check if the module provides a dynamic text
    mod = _find_module(mod_name)
    if mod is not None:
        text = mod.get_menu_text(action)
        if text is not None:
            return _(text)

    msgid = _STATIC_MENU_TITLES.get(command)
    if msgid is not None:
        return _(msgid)

    return None


def _command_from_dispatch_url(url: Any) -> str:
    """Extract command from a WriterAgent dispatch URL.

    Packet G: ``org.extension.writeragent:chatbot.debug_sidebar?RECORD_CLICKED``.
    LO may put the debug op in ``Query``, or in ``Path`` as ``chatbot.debug_sidebar?OP``.
    """
    # What was wrong: dispatch command extraction read url.Path directly, ignoring Complete-only URLs and double-appending queries.
    # How it happened: Path and Query handling was duplicated across dispatch and status listeners without Complete-only fallback.
    # Why this change: delegate base extraction to dispatch_command_from_url and append Query only when ? is not already in command.
    command = dispatch_command_from_url(url, protocol_prefix=_DISPATCH_PROTOCOL)
    query = getattr(url, "Query", None) or ""
    if query and "?" not in command:
        return "%s.%s" % (command, query)
    return command


def _prune_failed_status_listeners(failed: list[tuple[Any, Any]]) -> None:
    with _status_lock:
        # What was wrong: prune used 'is' on PyUNO listener proxies, failing to match.
        # How it happened: PyUNO creates different proxy instances for the same UNO object.
        # Why this change: compare with uno_same and Complete URL matching.
        _status_listeners[:] = [
            (lstnr, u) for (lstnr, u) in _status_listeners
            if not any(uno_same(lstnr, fl) and u.Complete == fu.Complete for (fl, fu) in failed)
        ]


@background
def notify_menu_update() -> None:
    """Push current menu text and icons to all registered status listeners.

    Called by modules when state changes (e.g. server start/stop).
    """
    # Snapshot the listeners under the lock, then fire OUTSIDE it. _fire_status_event ->
    # listener.statusChanged is a UNO call to a UI control that marshals to the main thread, so
    # holding _status_lock across it deadlocks: the main thread, while rebinding a toolbar
    # controller (addStatusListener), waits for the same lock this background thread holds. (This is
    # what froze LibreOffice on the toolbar's Accept all.) addStatusListener already fires outside
    # the lock; this makes notify_menu_update consistent.
    with _status_lock:
        snapshot = list(_status_listeners)
    failed = []
    for listener, url in snapshot:
        command = ""
        try:
            # What was wrong: menu text retrieval and statusChanged were grouped in a single try block, so text errors treated the listener as dead.
            # How it happened: get_menu_text exception caused the listener to be appended to failed and pruned from listeners.
            # Why this change: isolate command/text lookup; log and skip on text failure without marking listener failed or pruning it.
            command = _command_from_dispatch_url(url)
            text = get_menu_text(command)
        except Exception:
            log.exception(
                "notify_menu_update: menu text failed for %s",
                getattr(url, "Complete", ""),
            )
            continue
        try:
            _fire_status_event(listener, url, text)
        except Exception as e:
            failed.append((listener, url))
            log.warning(
                "notify_menu_update: failed to fire status event for %s: %s",
                command,
                e,
            )
    if failed:
        # What was wrong: this prune called uno_same on the pool worker.
        # uno_same is @main_thread_only, so a dev build raised before the
        # list was filtered and the icon refresh below never ran.
        # How it happened: notify_menu_update is @background and is only
        # started with run_in_background. The unit test calls it on the
        # main thread, where the guard does not fire.
        # Why this change: post the comparison to the main thread. A direct
        # call that is already on the main thread still prunes before return.
        # post, not execute: a blocking marshal during startup deadlocks
        # AsyncCallback lazy-init, same as _update_menu_icons.
        if on_main_thread():
            _prune_failed_status_listeners(failed)
        else:
            try:
                from plugin.framework.queue_executor import post_to_main_thread

                # What was wrong: prune post defaulted to the current send scope and was cancelled if user clicked Stop.
                # How it happened: post_to_main_thread omitted bound_scope, inheriting the worker thread's copied contextvar.
                # Why this change: pass bound_scope=None because listener cleanup is UI maintenance, not send work.
                post_to_main_thread(_prune_failed_status_listeners, list(failed), bound_scope=None)
            except Exception:
                log.exception("notify_menu_update: failed to post listener prune")
    if os.environ.get("WRITERAGENT_TESTING") != "1" and os.environ.get("WRITERAGENT_EVAL_HARNESS") != "1":
        # Update icons in a background thread (avoids blocking UI)
        from plugin.framework.worker_pool import run_in_background

        run_in_background(_update_menu_icons)


def _fire_status_event(listener: Any, url: Any, text: str | None) -> None:
    """Send a FeatureStateEvent to one listener."""
    ev = cast("Any", uno.createUnoStruct("com.sun.star.frame.FeatureStateEvent"))
    ev.FeatureURL = url
    command = _command_from_dispatch_url(url)
    ev.IsEnabled = True
    if command in {"main.RunFormatTests", "main.RunCalcTests", "main.RunCalcIntegrationTests", "main.RunDrawTests"}:
        ev.IsEnabled = _tests_bundled()
    ev.Requery = False
    if text is not None:
        ev.State = text
    listener.statusChanged(ev)


# ── Dynamic menu icons via XImageManager ──────────────────────────────

# LO document modules that have their own ImageManager
_IMAGE_MANAGER_MODULES = ("com.sun.star.text.TextDocument", "com.sun.star.sheet.SpreadsheetDocument", "com.sun.star.presentation.PresentationDocument", "com.sun.star.drawing.DrawingDocument")


def _get_menu_icon(command: str) -> str | None:
    """Get dynamic icon prefix for a command, or None."""
    bootstrap()
    parts = _split_command(command)
    if parts is None:
        return None
    mod_name, action = parts
    mod = _find_module(mod_name)
    if mod is not None:
        return mod.get_menu_icon(action)
    return None


def _collect_icon_commands() -> dict[str, tuple[str, str]]:
    """Collect all command URLs that declare icons in their manifest.

    Returns {command_url: (module_name, icon_prefix)} for the current state.
    """
    try:
        from plugin._manifest import MODULES
    except ImportError:
        return {}

    result: dict[str, tuple[str, str]] = {}
    import typing

    for m in cast("list[dict[str, Any]]", MODULES):
        mod_name = str(m["name"])
        action_icons = typing.cast("typing.Dict[str, str]", m.get("action_icons", {}))
        for action_name, default_icon in action_icons.items():
            cmd_url = "%s%s.%s" % (_DISPATCH_PROTOCOL, mod_name, action_name)
            # Ask the module for dynamic icon (may override the default)
            dynamic = _get_menu_icon("%s.%s" % (mod_name, action_name))
            result[cmd_url] = (mod_name, dynamic or default_icon)
    return result


_loaded_icon_graphics: dict[tuple[str, int], Any] = {}
_icon_apply_stamp: tuple[tuple[tuple[str, str], ...], int] | None = None


def _icon_apply_signature(
    icon_cmds: dict[str, tuple[str, str]], px: int
) -> tuple[tuple[tuple[str, str], ...], int]:
    return (tuple(sorted((url, prefix) for url, (_mod, prefix) in icon_cmds.items())), px)


def _icons_unchanged(
    icon_cmds: dict[str, tuple[str, str]],
    px: int,
    stamp: tuple[tuple[tuple[str, str], ...], int] | None,
) -> bool:
    return _icon_apply_signature(icon_cmds, px) == stamp


def _load_icon_graphic(module_name: str, icon_filename: str, ctx: Any | None = None) -> Any:
    """Load a PNG icon from OXT assets/ as XGraphic."""
    try:
        from com.sun.star.beans import PropertyValue

        # A bootstrap-less uno.getComponentContext() is a different context
        # (no VCL, wrong package). get_ctx() is the context bootstrap stored.
        if ctx is None:
            ctx = get_ctx()
        if ctx is None:
            log.warning("_load_icon_graphic: get_ctx() returned None for %s/%s", module_name, icon_filename)
            return None
        ctx_any = cast("Any", ctx)
        # What was wrong: getattr fallback evaluated getServiceManager on every call, and assert smgr is not None disappears under -O.
        # How it happened: ad-hoc getattr ladder was used instead of the standard get_service_manager helper.
        # Why this change: call get_service_manager and safely return None with a warning if missing.
        smgr = get_service_manager(ctx_any)
        if smgr is None:
            log.warning("_load_icon_graphic: get_service_manager returned None for %s/%s", module_name, icon_filename)
            return None
        gp = cast("Any", smgr).createInstanceWithContext("com.sun.star.graphic.GraphicProvider", ctx_any)
        ext_url = get_extension_url(ctx_any)
        if not ext_url:
            return None
        pv = PropertyValue()
        pv.Name = "URL"
        pv.Value = menu_icon_asset_url(ext_url, icon_filename)
        graphic = gp.queryGraphic((pv,))
        if graphic is None:
            log.warning("_load_icon_graphic: GraphicProvider returned None for %s (%s)", pv.Value, module_name)
        return graphic
    except Exception as e:
        log.warning("_load_icon_graphic failed for %s/%s: %s", module_name, icon_filename, e)
        return None


def _update_menu_icons_impl() -> None:
    """Push current-state icons into every module's ImageManager (UNO main thread)."""
    global _icon_apply_stamp
    try:
        # A bootstrap-less uno.getComponentContext() is a different context
        # (no VCL, wrong package). get_ctx() is the context bootstrap stored.
        ctx = get_ctx()
        if ctx is None:
            log.warning("_update_menu_icons_impl: get_ctx() returned None")
            return

        icon_cmds = _collect_icon_commands()
        if not icon_cmds:
            return

        from plugin.framework.menu_icon_dpi import (
            image_type_for_pixel_size,
            menu_icon_filename,
            resolve_menu_icon_pixel_size,
        )

        # One-shot DPI probe → pixel size (16 at 1×, 32 on HiDPI).
        # A probe miss caches 32 until a real VCL DPI reading. Warm
        # StyleSettings so startup does not stick on that weak reading.
        try:
            from plugin.framework.appearance import get_style_window

            get_style_window(ctx=ctx)
        except Exception:
            pass
        icon_px = resolve_menu_icon_pixel_size(ctx)
        image_type = image_type_for_pixel_size(icon_px)

        # Each dispatch used to re-query every PNG and rewrite four image
        # managers; the stamp skips that when the command-to-prefix map and the
        # pixel size are unchanged, and it stays unset when a load or replace
        # failed so the next click retries.
        if _icons_unchanged(icon_cmds, icon_px, _icon_apply_stamp):
            return

        # Group by (module, prefix) to avoid loading the same graphic twice
        key_cmds: dict[tuple[str, str], list[str]] = {}  # (mod_name, prefix) -> [cmd_urls]
        for cmd_url, (mod_name, prefix) in icon_cmds.items():
            key_cmds.setdefault((mod_name, prefix), []).append(cmd_url)

        # Load graphics
        apply_failed = False
        key_graphics = {}
        for key in key_cmds:
            mod_name, prefix = key
            cache_key = (prefix, icon_px)
            graphic = _loaded_icon_graphics.get(cache_key)
            if graphic is None:
                filename = menu_icon_filename(prefix, icon_px, ctx)
                graphic = _load_icon_graphic(mod_name, filename, ctx)
                if graphic is not None:
                    _loaded_icon_graphics[cache_key] = graphic
            if graphic is not None:
                key_graphics[key] = graphic
            else:
                apply_failed = True
                log.warning("Icon graphic is None for assets/%s (module %s)", prefix, mod_name)

        if not key_graphics:
            return

        smgr = get_service_manager(ctx)
        if smgr is None:
            log.warning("_update_menu_icons_impl: get_service_manager returned None")
            return

        supplier = cast("Any", smgr).createInstanceWithContext("com.sun.star.ui.ModuleUIConfigurationManagerSupplier", ctx)
        for mod_id in _IMAGE_MANAGER_MODULES:
            try:
                cfg_mgr = supplier.getUIConfigurationManager(mod_id)
                img_mgr = cfg_mgr.getImageManager()
                for key, cmds in key_cmds.items():
                    graphic = key_graphics.get(key)
                    if not graphic:
                        continue
                    # Menus usually fetch SIZE_DEFAULT; also stamp LARGE/32 when we
                    # chose a bigger asset so HiDPI/toolbars can pick either.
                    type_set = {0, int(image_type)}
                    for cmd in cmds:
                        for itype in sorted(type_set):
                            try:
                                if img_mgr.hasImage(itype, cmd):
                                    img_mgr.replaceImages(itype, (cmd,), (graphic,))
                                else:
                                    img_mgr.insertImages(itype, (cmd,), (graphic,))
                            except Exception as e:
                                apply_failed = True
                                log.warning(
                                    "_update_menu_icons: failed to insert/replace image for %s type=%s: %s",
                                    cmd,
                                    itype,
                                    e,
                                )
            except Exception as e:
                apply_failed = True
                log.warning("_update_menu_icons: failed to process ImageManager for %s: %s", mod_id, e)

        if not apply_failed and len(key_graphics) == len(key_cmds):
            _icon_apply_stamp = _icon_apply_signature(icon_cmds, icon_px)
    except Exception:
        log.exception("_update_menu_icons: outer exception")


@background
def _update_menu_icons() -> None:
    """Background entrypoint: marshal UNO icon updates onto the main thread.

    Use post (not blocking execute): at startup the UI thread may be inside
    ``set_context`` / ``init_config``, and a blocking marshal can deadlock
    with AsyncCallback lazy-init.
    """
    from plugin.framework.queue_executor import post_to_main_thread

    # What was wrong: icon updates inherited the worker thread's send scope and were cancelled when Stop was clicked.
    # How it happened: post_to_main_thread omitted bound_scope, binding to the active send task contextvar.
    # Why this change: pass bound_scope=None because menu icon application is UI maintenance, not send work.
    post_to_main_thread(_update_menu_icons_impl, bound_scope=None)


_LEGACY_FRAMEWORK_COMMANDS: dict[str, str] = {
    "ToggleMCPServer": "mcp.toggle_server",
    "MCPStatus": "mcp.server_status",
    "RunFormatTests": "main.RunFormatTests",
    "RunCalcTests": "main.RunCalcTests",
    "RunDrawTests": "main.RunDrawTests",
    "RunCalcIntegrationTests": "main.RunCalcIntegrationTests",
    "EvaluationDashboard": "main.EvaluationDashboard",
    "NoOp": "main.NoOp",
}


# Bootstrapper replaces the previous monolithic MainJob.
# It acts as an OnStartApp hook (triggered on office startup) and a proxy for legacy toolbar triggers.
class MainBootstrapJob(unohelper.Base, XJobExecutor, XJob):
    ctx: Any

    def __init__(self, ctx: Any) -> None:
        self.ctx = ctx

    def execute(self, Arguments: Any) -> tuple[()]:
        """Called by the Jobs framework on OnStartApp."""
        try:
            init_logging(self.ctx)
        except Exception:
            log.exception("MainBootstrapJob.execute: init_logging failed")
        try:
            bootstrap(self.ctx)
        except Exception as e:
            log.exception("MainBootstrapJob.execute failed to bootstrap: %s", e)
        try:
            from plugin.writer.locale.ai_grammar_proofreader import ensure_writeragent_proofreader_configured

            ensure_writeragent_proofreader_configured(self.ctx)
        except Exception as e:
            log.warning("[grammar] OnStartApp: could not load or run grammar proofreader bootstrap: %s", e, exc_info=True)
        return ()

    def trigger(self, Event: str) -> None:
        try:
            init_logging(self.ctx)
        except Exception:
            log.exception("MainBootstrapJob.trigger: init_logging failed")

        try:
            args = Event
            if args and isinstance(args, str) and ("." in args or args.startswith("plugin.")):
                cmd = args
                if cmd.startswith("plugin."):
                    cmd = cmd[7:]
                _dispatch_command(cmd)
                return

            if args == "settings":
                _dispatch_command("main.settings")
                return

            if self._handle_framework_actions(args):
                return

            model = get_active_document(self.ctx)
            from plugin.doc.doc_type import get_document_type, DocumentType

            doc_type = get_document_type(model)
            if doc_type in (DocumentType.WRITER, DocumentType.CALC):
                self._handle_selection_actions(args, model)
        except Exception as exc:
            from plugin.framework.main_shared import report_action_failure

            report_action_failure(str(Event or "trigger"), exc)

    def _handle_framework_actions(self, args: str) -> bool:
        cmd = _LEGACY_FRAMEWORK_COMMANDS.get(args)
        if cmd is None:
            return False
        _dispatch_command(cmd)
        return True

    def _handle_selection_actions(self, args: str, model: Any) -> None:
        if args in ("ExtendSelection", "EditSelection"):
            self._handle_selection_action(args, model)

    def _handle_selection_action(self, args: str, model: Any) -> None:
        # What was wrong: accelerator actions bypassed _dispatch_command and never bootstrapped.
        # How it happened: accelerators invoke trigger with ExtendSelection directly.
        # Why this change: bootstrap eagerly before calling selection action.
        bootstrap()
        from plugin.chatbot.dialog_views import input_box
        from plugin.chatbot.selection import do_selection_action_for_document

        do_selection_action_for_document(self.ctx, model, input_box, args == "EditSelection")


class DispatchHandler(unohelper.Base, XDispatch, XDispatchProvider, XInitialization, XServiceInfo):
    """Protocol handler for WriterAgent dispatch URLs.

    Handles menu dispatch and supports dynamic menu text via
    FeatureStateEvent / addStatusListener.
    """

    IMPL_NAME: ClassVar[str] = f"{EXTENSION_ID}.DispatchHandler"
    SERVICE_NAMES: ClassVar[tuple[str, ...]] = ("com.sun.star.frame.ProtocolHandler",)
    ctx: Any

    def __init__(self, ctx: Any) -> None:
        self.ctx = ctx

    # ── XInitialization ──────────────────────────────────────────

    def initialize(self, aArguments: Any) -> None:
        pass

    # ── XServiceInfo ─────────────────────────────────────────────

    def getImplementationName(self) -> str:
        return self.IMPL_NAME

    def supportsService(self, ServiceName: str) -> bool:
        return ServiceName in self.SERVICE_NAMES

    def getSupportedServiceNames(self) -> tuple[str, ...]:
        return self.SERVICE_NAMES

    # ── XDispatchProvider ────────────────────────────────────────

    def queryDispatch(self, URL: UnoURL, TargetFrameName: str, SearchFlags: int) -> XDispatch:  # pyright: ignore[reportIncompatibleMethodOverride]
        if matches_extension_dispatch_url(URL, EXTENSION_ID):
            return cast("XDispatch", self)
        # UNO allows null dispatch; stub types reject None → cast via object.
        return cast("XDispatch", cast("object", None))

    def queryDispatches(self, Requests: tuple[DispatchDescriptor, ...]) -> tuple[XDispatch, ...]:  # pyright: ignore[reportIncompatibleMethodOverride]
        return tuple(self.queryDispatch(r.FeatureURL, r.FrameName, r.SearchFlags) for r in Requests)

    # ── XDispatch ────────────────────────────────────────────────

    def dispatch(self, URL: UnoURL, Arguments: Any) -> None:
        url = URL
        command = _command_from_dispatch_url(url)
        from plugin.framework.logging import init_logging, log_exception

        try:
            init_logging(self.ctx)
            log.info(f"Dispatch entered: {command}")
            # msgbox(self.ctx, "Dispatch", f"Command: {command}") # Temporary probe
            _dispatch_command(command)
            # After action, push updated menu text
            from plugin.framework.worker_pool import run_in_background

            run_in_background(notify_menu_update)
        except Exception as e:
            log_exception(e, context="Dispatch")
            try:
                from plugin.chatbot.dialogs import msgbox_with_report
                from plugin.framework.i18n import _

                msgbox_with_report(
                    self.ctx,
                    _("Dispatch Error"),
                    str(e),
                    box_type=3,
                    reportable=True,
                    report_title="Dispatch Error",
                    report_extra=str(e),
                )
            except Exception:
                log.exception("msgbox_with_report failed")
                try:
                    from plugin.chatbot.dialogs import msgbox
                    from plugin.framework.i18n import _

                    msgbox(self.ctx, _("Dispatch Error"), str(e), box_type=3)
                except Exception:
                    log.exception("msgbox failed")

    def addStatusListener(self, Control: Any, URL: UnoURL) -> None:
        listener = Control
        url = URL
        with _status_lock:
            # What was wrong: re-registering a status listener for the same control and URL added duplicates.
            # How it happened: addStatusListener unconditionally appended without deduplication.
            # Why this change: drop existing entries matching uno_same and Complete URL before appending.
            _status_listeners[:] = [
                (lstnr, u)
                for lstnr, u in _status_listeners
                if not (uno_same(lstnr, listener) and u.Complete == url.Complete)
            ]
            _status_listeners.append((listener, url))
        # Send current state immediately
        try:
            command = _command_from_dispatch_url(url)
            text = get_menu_text(command)
        except Exception:
            log.exception(
                "addStatusListener: menu text failed for %s",
                getattr(url, "Complete", ""),
            )
            return

        if text is not None:
            try:
                _fire_status_event(listener, url, text)
            except Exception as e:
                log.warning("addStatusListener: failed to fire initial status event for %s: %s", command, e)

    def removeStatusListener(self, Control: Any, URL: UnoURL) -> None:
        listener = Control
        url = URL
        with _status_lock:
            # PyUNO hands removeStatusListener a new proxy for the same listener,
            # so 'is' left the row in _status_listeners and every rebind appended another.
            # uno_same is the identity ladder already used for document objects.
            _status_listeners[:] = [
                (lstnr, u)
                for lstnr, u in _status_listeners
                if not (uno_same(lstnr, listener) and u.Complete == url.Complete)
            ]


# pythonloader loads a static g_ImplementationHelper variable
g_ImplementationHelper = unohelper.ImplementationHelper()
g_ImplementationHelper.addImplementation(
    MainBootstrapJob,  # UNO object class
    f"{EXTENSION_ID}.Main",  # implementation name
    ("com.sun.star.task.Job",),
)  # implemented services (only 1)
g_ImplementationHelper.addImplementation(DispatchHandler, DispatchHandler.IMPL_NAME, DispatchHandler.SERVICE_NAMES)


# Starting from Python IDE
def main() -> None:
    try:
        # Using locals()/globals() bypasses static analyzer checks for XSCRIPTCONTEXT
        ctx = globals()["XSCRIPTCONTEXT"]
    except KeyError:
        if officehelper is None:
            print("ERROR: officehelper is not available (ImportError).")
            sys.exit(1)
        ctx = officehelper.bootstrap()
        if ctx is None:
            print("ERROR: Could not bootstrap default Office.")
            sys.exit(1)
    job = MainBootstrapJob(ctx)
    job.execute(())


# Starting from command line
if __name__ == "__main__":
    main()
# vim: set shiftwidth=4 softtabstop=4 expandtab:
