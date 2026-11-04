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

# At import, config is not resolved, so the user-config half no-ops and only in-tree contrib/ is prepended.
from plugin.scripting.native_binaries import ensure_native_binaries_on_path

ensure_native_binaries_on_path()

import unohelper
from typing import TYPE_CHECKING, Any, ClassVar, Callable

if TYPE_CHECKING:
    from types import ModuleType

    from com.sun.star.util import URL as UnoURL

officehelper: ModuleType | None = None
try:
    import officehelper as _officehelper_module

    officehelper = _officehelper_module
except ImportError:
    pass

from plugin.framework.logging import init_logging, log_exception


from plugin.framework.constants import EXTENSION_ID_WRITERAGENT
from plugin.framework.uno_context import (
    get_active_document,
    get_ctx,
)

EXTENSION_ID = EXTENSION_ID_WRITERAGENT

# ---------------------------------------------------------------------------
# Bootstrapping (Dynamic discovery from loaded manifest)
# ---------------------------------------------------------------------------

log = logging.getLogger(__name__)

from plugin.bootstrap import (
    BootstrapState,
    MainBootstrapJob as BaseMainBootstrapJob,
)
from plugin.framework.main_shared import ExtensionProtocolHandler, report_action_failure


def _install_terminate_listener(ctx: Any) -> None:
    """Register a desktop XTerminateListener to clean up child processes on app exit."""
    import plugin.app as app

    # The first successful addTerminateListener is process-lifetime and is not
    # removed or replaced; a failed install leaves the global None so the next
    # bootstrap retries.
    if app._terminate_listener is not None:
        return
    try:
        from plugin.framework.uno_context import get_desktop

        desktop = get_desktop(ctx)
        if desktop is None:
            return

        from plugin.framework.uno_listeners import BaseTerminateListener

        class _TerminateListener(BaseTerminateListener):
            def notifyTermination(self, Event: Any) -> None:
                try:
                    from plugin.framework.quit_hooks import run_quit_hooks

                    run_quit_hooks()
                except Exception:
                    # UNO boundary: XTerminateListener.notifyTermination callback
                    log.exception("Error running quit hooks on desktop termination")

                try:
                    from plugin.audio.kokoro_pool import shutdown_kokoro_pool

                    shutdown_kokoro_pool()
                except Exception:
                    # UNO boundary: XTerminateListener.notifyTermination callback
                    log.exception("Error shutting down Kokoro pool on desktop termination")

                try:
                    from plugin.framework.managed_subprocess import shutdown_all

                    shutdown_all()
                except Exception:
                    # UNO boundary: XTerminateListener.notifyTermination callback
                    log.exception("Error shutting down managed subprocesses on desktop termination")

                try:
                    from plugin.framework.warm_child import supervisor

                    supervisor.shutdown_all()
                except Exception:
                    # UNO boundary: XTerminateListener.notifyTermination callback
                    log.exception("Error shutting down warm child supervisor on desktop termination")

        listener = _TerminateListener()
        desktop.addTerminateListener(listener)
        app._terminate_listener = listener
    except Exception:
        # UNO boundary: desktop.addTerminateListener registration
        log.exception("Failed to install terminate listener")


def _register_writeragent_prompt_hooks() -> None:
    from plugin.writeragent_hooks import register_writeragent_prompt_hooks

    register_writeragent_prompt_hooks()


def _writeragent_bootstrap_finish(ctx: Any) -> None:
    import plugin.app as app
    import plugin.bootstrap as bootstrap

    from plugin.framework.queue_executor import Policy, main

    from plugin.framework.service import ServiceRegistry

    app._services = ServiceRegistry()
    app._services.register("uno", ctx)

    # Skip terminate listener when running under the eval harness.
    # The eval client already has a remote Desktop; this listener would shut down client subprocesses on office teardown.
    if main.policy is not Policy.eval_harness:
        _install_terminate_listener(ctx)

    from plugin.doc.document_helpers import DocumentService
    from plugin.framework.event_bus import get_event_bus

    app._services.register("document", DocumentService())
    app._services.register("events", get_event_bus())

    from plugin.framework.tool import ToolRegistry
    from plugin.writeragent_hooks import register_writeragent_tool_filters

    app._tools = ToolRegistry(app._services)
    register_writeragent_tool_filters(app._tools)
    app._services.register("tools", app._tools)
    app._services.register("main_thread", main)

    bootstrap._bootstrap_state = BootstrapState.INITIALIZED

    from plugin.framework.module_base import ModuleLoader

    module_phase_failed = False
    try:
        app._modules.extend(ModuleLoader.load_modules(app._services))
    except Exception as exc:
        module_phase_failed = True
        log.exception("bootstrap failed while loading modules")
        bootstrap._bootstrap_error = exc
        # What was wrong: bootstrap flag remained INITIALIZED even if module loading failed,
        # wedging startup so later calls returned immediately.
        # How it happened: INITIALIZED was set early to block re-entry during imports.
        # Why this change: reset only when app._modules is still empty so a retry does not
        # build a second registry.
        if not app._modules:
            bootstrap._bootstrap_state = BootstrapState.UNINITIALIZED
            return

    for mod in app._modules:
        try:
            mod.start_background(app._services)
        except Exception as exc:
            module_phase_failed = True
            log.exception("start_background failed for module %s", getattr(mod, "name", "?"))
            if bootstrap._bootstrap_error is None:
                bootstrap._bootstrap_error = exc

    try:
        # Subscribe to menu:update for dynamic menu text + icons.
        # publish is @background and warns if it runs on the main thread; this subscriber
        # always queues menu_status.publish with run_in_background.
        from plugin.framework.worker_pool import run_in_background

        app._services.get("events").subscribe(
            "menu:update", lambda **kw: run_in_background(menu_status.publish)
        )

        if main.policy is Policy.production:
            # Pre-load icons into ImageManager so first menu display has them.
            # Eval harness is headless with no VCL pump — posting icons to "main"
            # trips the UNO thread guard and can segfault soffice (issue #402 family).
            run_in_background(menu_icons.post_apply)

        # Headless eval connects via UNO pipe; menu/context-menu install
        # calls get_desktop() in this process and can segfault soffice.
        if main.policy is not Policy.eval_harness:
            register_core_handlers()
    except Exception as exc:
        log.exception("bootstrap failed after the init flag was set")
        if bootstrap._bootstrap_error is None:
            bootstrap._bootstrap_error = exc
        return

    if not module_phase_failed:
        bootstrap._bootstrap_error = None


def _show_tests_unavailable(test_name: str) -> None:
    """Show a message when test suites are not bundled (release builds)."""
    from plugin.chatbot.dialogs import msgbox

    msgbox(get_ctx(), test_name, "This WriterAgent build was packaged without the optional `plugin.tests` test modules.")


def _run_test_suite(module_name: str, test_name: str) -> None:
    """Run a bundled in-OXT test suite on the UI thread and show the result.

    Must not use ``run_blocking_in_thread``: synchronous UNO tools reject calls from
    worker threads (see ``ToolBase.execute_safe``).
    """
    from plugin.chatbot.dialogs import msgbox

    from plugin.app import tests_bundled

    if not tests_bundled():
        _show_tests_unavailable(test_name)
        return

    from plugin.framework.i18n import _

    ctx = get_ctx()
    try:
        log.info(f"_run_test_suite start: {test_name}")
        # Import test module dynamically on demand.
        import importlib

        test_module = importlib.import_module(module_name)
        from plugin.testing_runner import run_module_suite

        log.debug(f"Calling run_module_suite for {test_name}")
        p, f, suite_log = run_module_suite(ctx, test_module, test_name)
        log.info(f"_run_test_suite finished: {test_name}, p={p}, f={f}")
        msgbox(ctx, test_name, _("{0}: {1} passed, {2} failed.").format(test_name, p, f) + "\n\n" + "\n".join(suite_log))
    except Exception as e:
        log.exception("_run_test_suite failed: %s", test_name)
        msgbox(ctx, test_name, _("Tests failed to run: {0}").format(str(e)))


def register_core_handlers() -> None:
    """Register core application handlers during bootstrap."""
    from plugin.chatbot.dialog_views import settings_box
    from plugin.chatbot.eval_dashboard_ui import show_eval_dashboard
    from plugin.framework.main_shared import open_dialog_safely, register_action_handler
    from plugin.framework.uno_context import get_active_document

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
        ("RunFormatTests", "plugin.tests.writer.test_format_uno", "writer.format_tests"),
        ("RunCalcTests", "plugin.tests.calc.test_calc_uno", "calc.tests"),
        ("RunCalcIntegrationTests", "plugin.tests.calc.test_calc_uno", "calc.integration_tests"),
        ("RunDrawTests", "plugin.tests.draw.test_draw_uno", "draw.tests"),
    )
    for action, mod_name, test_name in test_suites:
        register_action_handler(
            "main",
            action,
            lambda m=mod_name, t=test_name: _run_test_suite(m, t),
        )
    register_action_handler("main", "NoOp", lambda: None)

    from plugin.menu_handlers import register_common_handlers

    register_common_handlers()

    def _convert_spreadsheet() -> None:
        from plugin.calc.spreadsheet_import.import_dialog import show_import_dialog
        open_dialog_safely(show_import_dialog, "Failed to open Convert Sheet to Python dialog")

    register_action_handler("calc", "convert_spreadsheet_to_python", _convert_spreadsheet)

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


# ── Dynamic menu text infrastructure ─────────────────────────────────

from plugin.menu_state import MenuIcons, MenuStatusPublisher, command_from_dispatch_url

menu_icons = MenuIcons()
menu_status = MenuStatusPublisher(menu_icons)


_LEGACY_FRAMEWORK_COMMANDS: dict[str, str] = {
    "settings": "main.settings",
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
class MainBootstrapJob(BaseMainBootstrapJob):
    def init_logging(self, ctx: Any) -> None:
        init_logging(ctx)

    def on_start(self, ctx: Any) -> None:
        from plugin.app import bootstrap

        try:
            bootstrap(self.ctx)
        except Exception as e:  # UNO boundary: XJob.execute entry point
            log.exception("MainBootstrapJob.execute failed to bootstrap: %s", e)
        try:
            from plugin.writer.locale.ai_grammar_proofreader import ensure_writeragent_proofreader_configured

            ensure_writeragent_proofreader_configured(self.ctx)
        except Exception as e:  # UNO boundary: XJob.execute entry point
            log.warning("[grammar] OnStartApp: could not load or run grammar proofreader bootstrap: %s", e, exc_info=True)

    def trigger(self, Event: str) -> None:
        try:
            init_logging(self.ctx)
        except Exception:  # UNO boundary: XJobExecutor.trigger entry point
            log.exception("MainBootstrapJob.trigger: init_logging failed")

        from plugin.app import bootstrap, dispatch_command

        try:
            if Event and ("." in Event or Event.startswith("plugin.")):
                cmd = Event[7:] if Event.startswith("plugin.") else Event
                dispatch_command(cmd)
                return

            legacy = _LEGACY_FRAMEWORK_COMMANDS.get(Event)
            if legacy is not None:
                dispatch_command(legacy)
                return

            model = get_active_document(self.ctx)
            from plugin.framework.doc_type import get_document_type, DocumentType

            doc_type = get_document_type(model)
            if doc_type in (DocumentType.WRITER, DocumentType.CALC) and Event in ("ExtendSelection", "EditSelection"):
                # What was wrong: accelerator actions bypassed _dispatch_command and never bootstrapped.
                # How it happened: accelerators invoke trigger with ExtendSelection directly.
                # Why this change: bootstrap eagerly before calling selection action.
                bootstrap()
                from plugin.chatbot.selection import do_selection_action_for_document

                do_selection_action_for_document(self.ctx, model, Event == "EditSelection")
        except Exception as exc:  # UNO boundary: XJobExecutor.trigger entry point
            report_action_failure(str(Event or "trigger"), exc)


class DispatchHandler(ExtensionProtocolHandler):
    """Protocol handler for WriterAgent dispatch URLs.

    Handles menu dispatch and supports dynamic menu text via
    FeatureStateEvent / addStatusListener.
    """

    EXTENSION_ID: ClassVar[str] = EXTENSION_ID
    IMPL_NAME: ClassVar[str] = f"{EXTENSION_ID}.DispatchHandler"

    # ── XDispatch ────────────────────────────────────────────────

    def dispatch(self, URL: UnoURL, Arguments: Any) -> None:
        from plugin.app import dispatch_command

        command = command_from_dispatch_url(URL)
        try:
            init_logging(self.ctx)
            log.info(f"Dispatch entered: {command}")
            dispatch_command(command)
            # After action, push updated menu text
            from plugin.framework.worker_pool import run_in_background

            run_in_background(menu_status.publish)
        except Exception as e:  # UNO boundary: XDispatch.dispatch entry point
            log_exception(e, context="Dispatch")
            from plugin.chatbot.dialogs import msgbox_with_report
            from plugin.framework.i18n import _

            msgbox_with_report(
                self.ctx,
                _("Dispatch Error"),
                str(e),
                reportable=True,
                report_title="Dispatch Error",
                report_extra=str(e),
            )

    def addStatusListener(self, Control: Any, URL: UnoURL) -> None:
        menu_status.add(Control, URL)

    def removeStatusListener(self, Control: Any, URL: UnoURL) -> None:
        menu_status.remove(Control, URL)


# pythonloader loads a static g_ImplementationHelper variable
g_ImplementationHelper = unohelper.ImplementationHelper()
g_ImplementationHelper.addImplementation(
    MainBootstrapJob,  # UNO object class
    f"{EXTENSION_ID}.Main",  # implementation name
    ("com.sun.star.task.Job",),
)  # implemented services (only 1)
g_ImplementationHelper.addImplementation(DispatchHandler, DispatchHandler.IMPL_NAME, DispatchHandler.SERVICE_NAMES)

from plugin.app import bind_writeragent


def bind() -> None:
    """Register WriterAgent finish hooks (first call wins).

    Out-of-office hosts must call this before ``plugin.app.bootstrap`` or ``get_tools()``.
    """
    bind_writeragent(
        finish=_writeragent_bootstrap_finish,
        register_prompt_hooks=_register_writeragent_prompt_hooks,
    )


bind()


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
