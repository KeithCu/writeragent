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
import sys
import os
import logging

# Ensure the extension's install directory is on sys.path
# so that "plugin.xxx" imports work correctly.
# We must do this before any imports from "plugin.xyz"
_plugin_dir = os.path.dirname(os.path.abspath(__file__))
_ext_root = os.path.dirname(_plugin_dir)
if _ext_root not in sys.path:
    sys.path.insert(0, _ext_root)
if _plugin_dir not in sys.path:
    sys.path.insert(0, _plugin_dir)


# Add the vendor directory so cross-platform audio wheels (sounddevice, cffi) can be found
# Root vendor/: used during development/tests.
_root_dir = os.path.dirname(_plugin_dir)
_vendor_root = os.path.join(_root_dir, "vendor")
if os.path.isdir(_vendor_root) and _vendor_root not in sys.path:
    sys.path.insert(0, _vendor_root)

# plugin/lib/: used in the bundled .oxt (see scripts/build_oxt.py)
_lib_dir = os.path.join(_plugin_dir, "lib")
if os.path.isdir(_lib_dir) and _lib_dir not in sys.path:
    sys.path.insert(0, _lib_dir)

# plugin/vendor/: legacy/future audio wheels path; kept for fallback
_vendor_plugin = os.path.join(_plugin_dir, "vendor")
if os.path.isdir(_vendor_plugin) and _vendor_plugin not in sys.path:
    sys.path.insert(0, _vendor_plugin)

import unohelper
from types import ModuleType

officehelper: ModuleType | None = None
try:
    import officehelper as _officehelper_module

    officehelper = _officehelper_module
except ImportError:
    pass

from plugin.framework.logging import init_logging
import uno

from com.sun.star.task import XJobExecutor, XJob
from com.sun.star.frame import XDispatch, XDispatchProvider
from com.sun.star.lang import XInitialization, XServiceInfo

from plugin.framework.uno_context import get_active_document, get_extension_url
from plugin.framework.tool_registry import ToolRegistry
from typing import Any, cast

# ---------------------------------------------------------------------------
# HTTP / MCP Server (Module wrapper)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Bootstrapping (Dynamic discovery from loaded manifest)
# ---------------------------------------------------------------------------

import threading
_services = None
log = logging.getLogger(__name__)

# Action handler registry
_ACTION_HANDLERS = {}

def register_action_handler(module_name, action_name, handler_func):
    """Register an action handler function."""
    key = f"{module_name}.{action_name}"
    _ACTION_HANDLERS[key] = handler_func


_tools: ToolRegistry | None = None
_modules = []
_init_lock = threading.Lock()
_initialized = False

_extension_update_check_scheduled = False
_extension_update_check_lock = threading.Lock()


def _schedule_extension_update_check_once(ctx):
    """Run weekly update check at most once per process, after init_logging."""
    global _extension_update_check_scheduled
    with _extension_update_check_lock:
        if _extension_update_check_scheduled:
            log.info(
                "extension update check: already queued this process, skipping duplicate schedule"
            )
            return
        _extension_update_check_scheduled = True
    from plugin.framework.worker_pool import run_in_background
    from plugin.framework.extension_update_check import run_extension_update_check

    log.info("extension update check: scheduling background worker")
    run_in_background(run_extension_update_check, ctx, name="extension_update_check")


def get_services():
    global _services
    if _services is None:
        bootstrap()
    return _services

def get_tools() -> ToolRegistry:
    global _tools
    if _tools is None:
        bootstrap()
    assert _tools is not None
    return _tools

def bootstrap(ctx=None):
    global _services, _tools, _modules, _initialized
    
    if _initialized:
        return
        
    with _init_lock:
        if _initialized:
            return
            
        # 1. Basic UNO context
        if ctx is None:
            from plugin.framework.uno_context import get_ctx
            ctx = get_ctx()
        
        # 2. Service Container
        from plugin.framework.service_registry import ServiceRegistry
        _services = ServiceRegistry()
        _services.register("uno", ctx)

        # 3. Core Services (Framework)
        from plugin.framework.config import ConfigService
        from plugin.framework.document import DocumentService
        from plugin.framework.format import FormatService
        from plugin.framework.event_bus import get_event_bus

        _services.register("config", ConfigService())
        _services.register("document", DocumentService())
        _services.register("format", FormatService())
        _services.register("events", get_event_bus())

        # 4. Tool Registry
        from plugin.framework.tool_registry import ToolRegistry
        _tools = ToolRegistry(_services)
        _services.register("tools", _tools)

        # 4b. Main Thread Execution Service
        from plugin.framework.queue_executor import default_executor
        _services.register("main_thread", default_executor)

        # Wire config service to events
        config_svc = _services.get("config")
        events_svc = _services.get("events")
        if config_svc and events_svc:
            config_svc.set_events(events_svc)

        # Initialize i18n
        from plugin.framework.i18n import init_i18n
        init_i18n(ctx)

        # Set initialized early to prevent recursive calls from re-running bootstrap
        # but after _services and _tools are created.
        _initialized = True

        # 5. Load manifest and initialize modules
        from plugin.framework.module_loader import ModuleLoader
        _modules.extend(ModuleLoader.load_modules(_services))

        # 6. Background phase: modules that start listeners/servers (e.g. HttpModule when MCP enabled)
        for mod in _modules:
            mod.start_background(_services)

        # Wire event bus into config service
        events_svc = _services.get("events")
        if events_svc:
            # Subscribe to menu:update for dynamic menu text + icons
            main_thread = _services.get("main_thread")
            events_svc.subscribe("menu:update",
                                 lambda **kw: main_thread.execute(notify_menu_update) if main_thread else notify_menu_update())

        # Pre-load icons into ImageManager so first menu display has them
        from plugin.framework.worker_pool import run_in_background
        run_in_background(_update_menu_icons)

        # Register core handlers
        _register_core_handlers()


def _register_core_handlers():
    """Register core application handlers during bootstrap."""
    from plugin.framework.legacy_ui import settings_box, show_eval_dashboard
    from plugin.framework.dialogs import about_dialog
    from plugin.framework.document import is_writer, is_calc, is_draw
    from plugin.framework.uno_context import get_ctx
    import importlib

    def _open_settings():
        _open_dialog_safely(settings_box, "Failed to open settings")

    register_action_handler("main", "settings", _open_settings)

    register_action_handler("main", "about",
        lambda: _open_dialog_safely(about_dialog, "Failed to open about dialog"))

    register_action_handler("main", "EvaluationDashboard",
        lambda: _open_dialog_safely(show_eval_dashboard, "Failed to show eval dashboard"))

    register_action_handler("main", "RunFormatTests",
        lambda: _run_test_suite(
            importlib.import_module("plugin.tests.uno.format_tests"),
            is_writer,
            "writer.format_tests",
        ) if _tests_bundled() else _show_tests_unavailable("writer.format_tests"))

    register_action_handler("main", "RunCalcTests",
        lambda: _run_test_suite(
            importlib.import_module("plugin.tests.uno.test_calc"),
            is_calc,
            "calc.tests",
        ) if _tests_bundled() else _show_tests_unavailable("calc.tests"))

    register_action_handler("main", "RunCalcIntegrationTests",
        lambda: _run_test_suite(
            importlib.import_module("plugin.tests.uno.test_calc"),
            is_calc,
            "calc.integration_tests",
        ) if _tests_bundled() else _show_tests_unavailable("calc.integration_tests"))

    register_action_handler("main", "RunDrawTests",
        lambda: _run_test_suite(
            importlib.import_module("plugin.tests.uno.test_draw"),
            is_draw,
            "draw.tests",
        ) if _tests_bundled() else _show_tests_unavailable("draw.tests"))

    register_action_handler("main", "NoOp", lambda: None)


# ── Dynamic menu text infrastructure ─────────────────────────────────

_DISPATCH_PROTOCOL = "org.extension.writeragent:"

_status_listeners: list[tuple[Any, Any]] = []  # [(listener, url)]
_status_lock = threading.Lock()

EXTENSION_ID = "org.extension.writeragent"

_TESTS_AVAILABLE = None


def _tests_bundled() -> bool:
    """True when `plugin.tests` test modules are included in this build."""
    global _TESTS_AVAILABLE
    if _TESTS_AVAILABLE is None:
        try:
            import importlib.util
            _TESTS_AVAILABLE = importlib.util.find_spec("plugin.tests.uno") is not None
        except Exception:
            _TESTS_AVAILABLE = False
    return bool(_TESTS_AVAILABLE)


def _show_tests_unavailable(test_name: str) -> None:
    """Show a message when test suites are not bundled (release builds)."""
    try:
        from plugin.framework.dialogs import msgbox
        from plugin.framework.uno_context import get_ctx

        msgbox(
            get_ctx(),
            test_name,
            "This WriterAgent build was packaged without the optional `plugin.tests` test modules.",
        )
    except Exception:
        # Never let test menu state/messaging break core UI dispatch.
        pass


def _open_dialog_safely(dialog_func, error_msg, *args, **kwargs):
    """Safely open a dialog with standardized error handling."""
    from plugin.framework.errors import DocumentDisposedError, UnoObjectError
    from plugin.framework.uno_context import get_ctx

    ctx_getter = get_ctx
    try:
        dialog_func(ctx_getter(), *args, **kwargs)
    except DocumentDisposedError:
        log.debug("Dialog opening aborted: document disposed")
    except UnoObjectError as e:
        log.warning(f"UNO error opening dialog: {e.message}")
    except Exception as e:
        log.error(f"{error_msg}: {e}", exc_info=True)
        # Show user-friendly error message
        from plugin.framework.dialogs import msgbox
        from plugin.framework.i18n import _
        msgbox(ctx_getter(), _("Error"), _(f"{error_msg}: {str(e)}"))

def _run_test_suite(test_func, doc_checker, test_name):
    """Helper to run a test suite in a blocking thread and show the result."""
    from plugin.framework.uno_context import get_ctx
    from plugin.framework.dialogs import msgbox
    ctx = get_ctx()
    try:
        log.info(f"_run_test_suite start: {test_name}")
        from plugin.framework.async_stream import run_blocking_in_thread
        from plugin.testing_runner import run_module_suite
        model = get_active_document(ctx)
        doc_model = model if (model and doc_checker(model)) else None
        log.debug(f"Calling run_blocking_in_thread for {test_name}")
        p, f, suite_log = run_blocking_in_thread(ctx, run_module_suite, ctx, test_func, test_name, doc_model)
        log.info(f"_run_test_suite finished: {test_name}, p={p}, f={f}")
        from plugin.framework.i18n import _
        msgbox(ctx, test_name, _("{0}: {1} passed, {2} failed.").format(test_name, p, f) + "\n\n" + "\n".join(suite_log))
    except Exception as e:
        from plugin.framework.i18n import _
        msgbox(ctx, test_name, _("Tests failed to run: {0}").format(str(e)))


def _dispatch_command(command):
    """Dispatch command using handler registry, falling back to module actions."""
    # First try the action registry
    handler = _ACTION_HANDLERS.get(command)
    if handler:
        try:
            handler()
        except Exception as e:
            logging.getLogger("writeragent.main").error(f"Action {command} failed: {e}", exc_info=True)
        return

    # If no handler, check for module delegation
    dot = command.find(".")
    if dot <= 0:
        log = logging.getLogger("writeragent.main")
        log.warning("Unhandled command: %s", command)
        return

    mod_name = command[:dot]
    action = command[dot + 1:]

    # Module actions
    for mod in _modules:
        if mod.name == mod_name:
            mod.on_action(action)
            return

    log = logging.getLogger("writeragent.main")
    log.warning("No handler or module found for command: %s", command)


def get_menu_text(command):
    """Get dynamic menu text for a command, or None for default."""
    dot = command.find(".")
    if dot <= 0:
        return None
    mod_name = command[:dot]
    action = command[dot + 1:]

    from plugin.framework.i18n import _

    # Check if the module provides a dynamic text
    for mod in _modules:
        if mod.name == mod_name:
            text = mod.get_menu_text(action)
            if text is not None:
                return _(text)

    # Fallback to the title from the manifest
    try:
        from plugin._manifest import MODULES
        for m in MODULES:
            if m["name"] == mod_name:
                # The manifest doesn't store action titles directly in a map,
                # but it might have an action list. If we don't have a specific title,
                # we can translate the action name by capitalizing it as a fallback,
                # or better, let the module define it. But for static menus,
                # we can translate the static titles from the manifest if we had them.
                # Since Addons.xcu has the static names, and notify_menu_update sets them,
                # if we return None, LO uses the Addons.xcu name.
                # To override Addons.xcu with translated static names, we need a map.
                # For now, let's map known static actions directly here if mod didn't provide it.
                pass
    except ImportError:
        pass

    from plugin.framework.i18n import _
    # Hardcoded fallback for static Addons.xcu items so they get translated
    # without needing dynamic state in their respective modules.
    static_titles = {
        "chatbot.extend_selection": _("Extend Selection"),
        "chatbot.edit_selection": _("Edit Selection"),
        "main.settings": _("Settings"),
        "http.toggle_server": _("Toggle MCP Server"),
        "http.server_status": _("MCP Server Status"),
        "main.NoOp": "Debug", # Excluded from translation per user request
        "main.RunFormatTests": _("Run format tests"),
        "main.RunCalcTests": _("Run calc tests"),
        "main.RunCalcIntegrationTests": _("Run Calc API integration tests"),
        "main.RunDrawTests": _("Run draw tests"),
        "main.EvaluationDashboard": _("Evaluation Dashboard"),
        "main.about": _("About WriterAgent")
    }

    if command in static_titles:
        return static_titles[command]

    return None


def notify_menu_update():
    """Push current menu text and icons to all registered status listeners.

    Called by modules when state changes (e.g. server start/stop).
    """
    with _status_lock:
        alive = []
        for listener, url in _status_listeners:
            command = url.Path
            text = get_menu_text(command)
            try:
                _fire_status_event(listener, url, text)
                alive.append((listener, url))
            except Exception as e:
                log.warning("notify_menu_update: failed to fire status event for %s: %s", command, e)
        _status_listeners[:] = alive
    # Update icons in a background thread (avoids blocking UI)
    from plugin.framework.worker_pool import run_in_background
    run_in_background(_update_menu_icons)


def _fire_status_event(listener, url, text):
    """Send a FeatureStateEvent to one listener."""
    import typing
    if typing.TYPE_CHECKING:
        from com.sun.star.frame import FeatureStateEvent
        ev = FeatureStateEvent()
    else:
        ev = uno.createUnoStruct("com.sun.star.frame.FeatureStateEvent")
    ev.FeatureURL = url
    command = url.Path
    ev.IsEnabled = True
    if command in {
        "main.RunFormatTests",
        "main.RunCalcTests",
        "main.RunCalcIntegrationTests",
        "main.RunDrawTests",
    }:
        ev.IsEnabled = _tests_bundled()
    ev.Requery = False
    if text is not None:
        ev.State = text
    listener.statusChanged(ev)


# ── Dynamic menu icons via XImageManager ──────────────────────────────

# LO document modules that have their own ImageManager
_IMAGE_MANAGER_MODULES = (
    "com.sun.star.text.TextDocument",
    "com.sun.star.sheet.SpreadsheetDocument",
    "com.sun.star.presentation.PresentationDocument",
    "com.sun.star.drawing.DrawingDocument",
)


def _get_menu_icon(command):
    """Get dynamic icon prefix for a command, or None."""
    dot = command.find(".")
    if dot <= 0:
        return None
    mod_name = command[:dot]
    action = command[dot + 1:]
    for mod in _modules:
        if mod.name == mod_name:
            return mod.get_menu_icon(action)
    return None


def _collect_icon_commands():
    """Collect all command URLs that declare icons in their manifest.

    Returns {command_url: (module_name, icon_prefix)} for the current state.
    """
    try:
        from plugin._manifest import MODULES
    except ImportError:
        return {}

    result = {}
    import typing
    for m in MODULES:
        mod_name = m["name"]
        action_icons = typing.cast(typing.Dict[str, str], m.get("action_icons", {}))
        for action_name, default_icon in action_icons.items():
            cmd_url = "%s%s.%s" % (_DISPATCH_PROTOCOL, mod_name, action_name)
            # Ask the module for dynamic icon (may override the default)
            dynamic = _get_menu_icon("%s.%s" % (mod_name, action_name))
            result[cmd_url] = (mod_name, dynamic or default_icon)
    return result


def _load_icon_graphic(module_name, icon_filename, ctx=None):
    """Load a PNG icon from a module's icons/ directory as XGraphic."""
    try:
        from com.sun.star.beans import PropertyValue
        import uno
        if ctx is None:
            ctx = uno.getComponentContext()
        assert ctx is not None
        ctx_any = cast(Any, ctx)
        smgr = getattr(ctx_any, "ServiceManager", getattr(ctx_any, "getServiceManager", lambda: None)())
        assert smgr is not None
        gp = cast(Any, smgr).createInstanceWithContext(
            "com.sun.star.graphic.GraphicProvider", ctx_any)
        ext_url = get_extension_url(ctx_any)
        if not ext_url:
            return None
        pv = PropertyValue()
        # Support nested module directories
        mod_dir = module_name.replace(".", "/")
        pv.Value = "%s/plugin/modules/%s/icons/%s" % (
            ext_url, mod_dir, icon_filename)
        return gp.queryGraphic((pv,))
    except Exception as e:
        log.warning("_load_icon_graphic failed for %s/%s: %s", module_name, icon_filename, e)
        return None


def _update_menu_icons():
    """Push current-state icons into every module's ImageManager."""
    try:
        import uno
        ctx = _services.get("uno") if _services else None
        if ctx is None:
            ctx = uno.getComponentContext()

        icon_cmds = _collect_icon_commands()
        if not icon_cmds:
            return

        # Group by (module, prefix) to avoid loading the same graphic twice
        key_cmds = {}  # (mod_name, prefix) -> [cmd_urls]
        for cmd_url, (mod_name, prefix) in icon_cmds.items():
            key_cmds.setdefault((mod_name, prefix), []).append(cmd_url)

        # Load graphics
        key_graphics = {}
        for key in key_cmds:
            mod_name, prefix = key
            filename = "%s_16.png" % prefix
            graphic = _load_icon_graphic(mod_name, filename, ctx)
            if graphic:
                key_graphics[key] = graphic

        if not key_graphics:
            return

        smgr = getattr(ctx, "ServiceManager", getattr(ctx, "getServiceManager", lambda: None)())
        assert smgr is not None

        supplier = cast(Any, smgr).createInstanceWithContext(
            "com.sun.star.ui.ModuleUIConfigurationManagerSupplier", ctx)
        for mod_id in _IMAGE_MANAGER_MODULES:
            try:
                cfg_mgr = supplier.getUIConfigurationManager(mod_id)
                img_mgr = cfg_mgr.getImageManager()
                for key, cmds in key_cmds.items():
                    graphic = key_graphics.get(key)
                    if not graphic:
                        continue
                    for cmd in cmds:
                        try:
                            if img_mgr.hasImage(0, cmd):
                                img_mgr.replaceImages(0, (cmd,), (graphic,))
                            else:
                                img_mgr.insertImages(0, (cmd,), (graphic,))
                        except Exception as e:
                            log.warning("_update_menu_icons: failed to insert/replace image for %s: %s", cmd, e)
            except Exception as e:
                log.warning("_update_menu_icons: failed to process ImageManager for %s: %s", mod_id, e)
    except Exception as e:
        log.warning("_update_menu_icons: outer exception: %s", e)

def _get_http_module(ctx=None):
    if ctx:
        bootstrap(ctx)
    for mod in _modules:
        if getattr(mod, "name", "") == "http":
            return mod
    return None

def _start_mcp_server(ctx):
    """Ensure HTTP/MCP server is loaded. Start happens natively in module lifecycle."""
    from plugin.framework.config import get_config_bool
    if not get_config_bool(ctx, "mcp_enabled"):
        return
    bootstrap(ctx)

def _stop_mcp_server():
    mod = _get_http_module()
    if mod:
        mod.shutdown()

def _toggle_mcp_server(ctx):
    bootstrap(ctx)
    mod = _get_http_module(ctx)
    if mod:
        mod._action_toggle_server()

def _do_mcp_status(ctx):
    bootstrap(ctx)
    mod = _get_http_module(ctx)
    if mod:
        mod._action_server_status()

def try_ensure_mcp_timer(ctx):
    """Legacy entry point from sidebar to ensure server is running.
    The new framework main_thread executes drains natively without timers."""
    pass


# Bootstrapper replaces the previous monolithic MainJob.
# It acts as an OnStartApp hook and a proxy for legacy toolbar triggers.
class MainBootstrapJob(unohelper.Base, XJobExecutor, XJob):
    def __init__(self, ctx):
        self.ctx = ctx
        try:
            self.sm = ctx.getServiceManager()
        except NameError:
            self.sm = ctx.ServiceManager

    def execute(self, Arguments):
        """Called by the Jobs framework on OnStartApp."""
        try:
            bootstrap(self.ctx)
            init_logging(self.ctx)
        except Exception as e:
            log.exception("MainBootstrapJob.execute failed to bootstrap: %s", e)
        return ()

    def trigger(self, Event):
        bootstrap(self.ctx)
        init_logging(self.ctx)

        args = Event
        if args and isinstance(args, str) and ("." in args or args.startswith("plugin.")):
            cmd = args
            if cmd.startswith("plugin."): cmd = cmd[7:]
            _dispatch_command(cmd)
            return

        if args == "settings":
            _dispatch_command("main.settings")
            return

        if self._handle_framework_actions(args):
            return

        model = get_active_document(self.ctx)
        from plugin.framework.document import get_document_type, DocumentType
        
        doc_type = get_document_type(model)
        if doc_type == DocumentType.WRITER:
            self._handle_writer_actions(args, model)
        elif doc_type == DocumentType.CALC:
            self._handle_calc_actions(args, model)

    def _handle_framework_actions(self, args):
        framework_args = ("ToggleMCPServer", "MCPStatus", "TestTypes", "RunFormatTests", "RunCalcTests", "RunDrawTests", "RunCalcIntegrationTests", "EvaluationDashboard", "NoOp")
        if args not in framework_args:
            return False
            
        if args == "ToggleMCPServer": _dispatch_command("http.toggle_server")
        elif args == "MCPStatus": _dispatch_command("http.server_status")
        else:
            _dispatch_command("main." + args)
        return True

    def _handle_writer_actions(self, args, model):
        if args == "ExtendSelection":
            from plugin.modules.writer.legacy import do_extend_selection
            from plugin.framework.legacy_ui import input_box
            do_extend_selection(self.ctx, model, input_box)
        elif args == "EditSelection":
            from plugin.modules.writer.legacy import do_edit_selection
            from plugin.framework.legacy_ui import input_box
            do_edit_selection(self.ctx, model, input_box)

    def _handle_calc_actions(self, args, model):
        if args in ("ExtendSelection", "EditSelection"):
            from plugin.modules.calc.legacy import do_calc_extend_edit
            from plugin.framework.legacy_ui import input_box
            do_calc_extend_edit(self.ctx, model, input_box, args == "EditSelection")

# Starting from Python IDE
def main():
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
    job.trigger("hello")

# Starting from command line
if __name__ == "__main__":
    main()

class DispatchHandler(unohelper.Base, XDispatch, XDispatchProvider,
                      XInitialization, XServiceInfo):
    """Protocol handler for org.extension.writeragent: URLs.

    Handles menu dispatch and supports dynamic menu text via
    FeatureStateEvent / addStatusListener.
    """

    IMPL_NAME = "org.extension.writeragent.DispatchHandler"
    SERVICE_NAMES = ("com.sun.star.frame.ProtocolHandler",)

    def __init__(self, ctx):
        self.ctx = ctx

    # ── XInitialization ──────────────────────────────────────────

    def initialize(self, aArguments):
        pass

    # ── XServiceInfo ─────────────────────────────────────────────

    def getImplementationName(self):
        return self.IMPL_NAME

    def supportsService(self, ServiceName):
        return ServiceName in self.SERVICE_NAMES

    def getSupportedServiceNames(self):
        return self.SERVICE_NAMES

    # ── XDispatchProvider ────────────────────────────────────────

    def queryDispatch(self, URL, TargetFrameName, SearchFlags):  # pyright: ignore[reportIncompatibleMethodOverride]
        url = URL
        if url.Protocol == "org.extension.writeragent:":
            return self
        return None

    def queryDispatches(self, Requests):  # pyright: ignore[reportIncompatibleMethodOverride]
        requests = Requests
        return [self.queryDispatch(r.FeatureURL, r.FrameName,
                                   r.SearchFlags) for r in requests]

    # ── XDispatch ────────────────────────────────────────────────

    def dispatch(self, URL, Arguments):
        url = URL
        command = url.Path
        from plugin.framework.dialogs import msgbox
        from plugin.framework.logging import init_logging, log_exception
        try:
            init_logging(self.ctx)
            log.info(f"Dispatch entered: {command}")
            # msgbox(self.ctx, "Dispatch", f"Command: {command}") # Temporary probe
            bootstrap(self.ctx)
            _dispatch_command(command)
            # After action, push updated menu text
            from plugin.framework.worker_pool import run_in_background
            run_in_background(notify_menu_update)
        except Exception as e:
            log_exception(e, context="Dispatch")
            from plugin.framework.i18n import _
            msgbox(self.ctx, _("Dispatch Error"), _(str(e)))

    def addStatusListener(self, Control, URL):
        listener = Control
        url = URL
        with _status_lock:
            _status_listeners.append((listener, url))
        # Send current state immediately
        command = url.Path
        text = get_menu_text(command)
        if text is not None:
            try:
                _fire_status_event(listener, url, text)
            except Exception as e:
                log.warning("addStatusListener: failed to fire initial status event for %s: %s", command, e)

    def removeStatusListener(self, Control, URL):
        listener = Control
        url = URL
        with _status_lock:
            _status_listeners[:] = [
                (l, u) for l, u in _status_listeners
                if not (l is listener and u.Complete == url.Complete)
            ]

# pythonloader loads a static g_ImplementationHelper variable
g_ImplementationHelper = unohelper.ImplementationHelper()
g_ImplementationHelper.addImplementation(
    MainBootstrapJob,  # UNO object class
    "org.extension.writeragent.Main",  # implementation name
    ("com.sun.star.task.Job",), )  # implemented services (only 1)
g_ImplementationHelper.addImplementation(
    DispatchHandler,
    DispatchHandler.IMPL_NAME,
    DispatchHandler.SERVICE_NAMES,
)
# vim: set shiftwidth=4 softtabstop=4 expandtab: