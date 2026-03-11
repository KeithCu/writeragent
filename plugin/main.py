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

# Ensure the extension's install directory is on sys.path
# so that "plugin.xxx" imports work correctly.
_plugin_dir = os.path.dirname(os.path.abspath(__file__))
_ext_root = os.path.dirname(_plugin_dir)
if _ext_root not in sys.path:
    sys.path.insert(0, _ext_root)
if _plugin_dir not in sys.path:
    sys.path.insert(0, _plugin_dir)

# Add the vendor directory so cross-platform audio wheels (sounddevice, cffi) can be found
_vendor_dir = os.path.join(_plugin_dir, "vendor")
if _vendor_dir not in sys.path:
    sys.path.insert(0, _vendor_dir)

import unohelper
import officehelper

from plugin.framework.logging import init_logging
import uno
import logging

from com.sun.star.task import XJobExecutor, XJob
from com.sun.star.frame import XDispatch, XDispatchProvider
from com.sun.star.lang import XInitialization, XServiceInfo

from plugin.framework.uno_helpers import get_active_document, get_extension_url

# ---------------------------------------------------------------------------
# HTTP / MCP Server (Module wrapper)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Bootstrapping (Dynamic discovery from loaded manifest)
# ---------------------------------------------------------------------------

import threading
_services = None
_tools = None
_modules = []
_init_lock = threading.Lock()
_initialized = False

def get_services():
    global _services
    if _services is None:
        bootstrap()
    return _services

def get_tools():
    global _tools
    if _tools is None:
        bootstrap()
    return _tools

def _load_manifest():
    try:
        from plugin._manifest import MODULES
        return MODULES
    except ImportError:
        return []

def _topo_sort(modules):
    by_name = {m["name"]: m for m in modules}
    provides = {}
    for m in modules:
        for svc in m.get("provides_services", []):
            provides[svc] = m["name"]

    visited = set()
    order = []

    def visit(name):
        if name in visited:
            return
        visited.add(name)
        m = by_name.get(name)
        if m is None:
            return
        for req in m.get("requires", []):
            provider = provides.get(req, req)
            if provider in by_name:
                visit(provider)
        order.append(m)

    if "core" in by_name:
        visit("core")
    for name in by_name:
        visit(name)
    return order

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
        _services.register_instance("uno", ctx)

        # 3. Core Services (Framework)
        from plugin.framework.config import ConfigService
        from plugin.framework.document import DocumentService
        from plugin.framework.format import FormatService
        from plugin.framework.event_bus import get_event_bus

        _services.register(ConfigService())
        _services.register(DocumentService())
        _services.register(FormatService())
        _services.register_instance("events", get_event_bus())

        # 4. Tool Registry
        from plugin.framework.tool_registry import ToolRegistry
        _tools = ToolRegistry(_services)
        _services.register_instance("tools", _tools)

        # Set initialized early to prevent recursive calls from re-running bootstrap
        # but after _services and _tools are created.
        _initialized = True

        # 5. Load manifest and initialize modules
        manifests = _topo_sort(_load_manifest())
        
        for manifest in manifests:
            name = manifest["name"]
            if name == "core":
                continue
            
            # Auto-discover tools from tools/ subpackage
            # Try nested path first (e.g. "launcher/providers/claude")
            rel_path = name.replace(".", os.sep)
            module_dir = os.path.join(os.path.dirname(__file__), "modules", rel_path)
            import_path = "plugin.modules." + name
            
            if not os.path.isdir(module_dir):
                # Legacy fallback for flat paths (e.g. "launcher_claude")
                dir_name = name.replace(".", "_")
                module_dir = os.path.join(os.path.dirname(__file__), "modules", dir_name)
                import_path = "plugin.modules." + dir_name
                if not os.path.isdir(module_dir):
                    continue
                
            # Tools may be in module root (like localwriter2 draw/calc)
            _tools.discover(module_dir, import_path)
            
            # Structure approach (like the writer tools we generated)
            tools_dir = os.path.join(module_dir, "tools")
            if os.path.isdir(tools_dir):
                _tools.discover(tools_dir, import_path + ".tools")

            # Dynamic ModuleBase initialization
            try:
                import importlib
                import inspect
                
                mod_pkg = importlib.import_module(import_path)
                module_class = None
                
                # Look for a class subclassing ModuleBase by checking MRO names (avoids LO sys.path duplicate issues)
                for attr_name in dir(mod_pkg):
                    attr = getattr(mod_pkg, attr_name)
                    if inspect.isclass(attr) and attr.__name__ != "ModuleBase":
                        if any(b.__name__ == "ModuleBase" for b in attr.__mro__):
                            module_class = attr
                            break
                        
                if module_class:
                    mod = module_class()
                    mod.name = name
                    mod.initialize(_services)
                    _modules.append(mod)
            except Exception as e:
                logging.getLogger("writeragent").warning("Failed to load module %s: %s", name, e)

        # Wire event bus into config service
        events_svc = _services.get("events")
        if events_svc:
            # Subscribe to menu:update for dynamic menu text + icons
            events_svc.subscribe("menu:update",
                                 lambda **kw: notify_menu_update())

        # Pre-load icons into ImageManager so first menu display has them
        threading.Thread(target=_update_menu_icons, daemon=True).start()



# ── Dynamic menu text infrastructure ─────────────────────────────────

_DISPATCH_PROTOCOL = "org.extension.writeragent:"

_status_listeners = []  # [(listener, url)]
_status_lock = threading.Lock()

EXTENSION_ID = "org.extension.writeragent"


def _run_test_suite(test_func, doc_checker, test_name):
    """Helper to run a test suite in a blocking thread and show the result."""
    from plugin.framework.uno_context import get_ctx
    from plugin.framework.dialogs import msgbox
    ctx = get_ctx()
    try:
        from plugin.framework.async_stream import run_blocking_in_thread
        model = get_active_document(ctx)
        doc_model = model if (model and doc_checker(model)) else None
        p, f, log = run_blocking_in_thread(ctx, test_func, ctx, doc_model)
        msgbox(ctx, test_name, f"{test_name}: {p} passed, {f} failed.\n\n" + "\n".join(log))
    except Exception as e:
        msgbox(ctx, test_name, f"Tests failed to run: {e}")


def _dispatch_command(command):
    """Dispatch a module.action command. Used by both MainJob and DispatchHandler."""
    dot = command.find(".")
    if dot <= 0:
        log = logging.getLogger("writeragent.main")
        log.warning("Unhandled command: %s", command)
        return

    mod_name = command[:dot]
    action = command[dot + 1:]

    # Framework actions
    if mod_name == "main":
        if action == "settings":
            from plugin.framework.uno_context import get_ctx
            from plugin.framework.legacy_ui import settings_box
            try:
                settings_box(get_ctx())
                _start_mcp_server(get_ctx())
            except Exception as e:
                logging.getLogger("writeragent.main").error("Failed to open settings: %s", e, exc_info=True)
                pass
        elif action == "about":
            from plugin.framework.uno_context import get_ctx
            from plugin.framework.dialogs import about_dialog
            try:
                about_dialog(get_ctx())
            except Exception as e:
                logging.getLogger("writeragent.main").error("Failed to open about dialog: %s", e, exc_info=True)
                pass
        elif action == "EvaluationDashboard":
            from plugin.framework.uno_context import get_ctx
            from plugin.framework.legacy_ui import show_eval_dashboard
            try:
                show_eval_dashboard(get_ctx())
            except Exception as e:
                logging.getLogger("writeragent.main").error("Failed to show eval dashboard: %s", e, exc_info=True)
                pass
        elif action == "RunFormatTests":
            from plugin.tests.format_tests import run_markdown_tests
            from plugin.framework.document import is_writer
            _run_test_suite(run_markdown_tests, is_writer, "Format tests")
        elif action == "RunCalcTests":
            from plugin.tests.test_calc import run_calc_tests
            from plugin.framework.document import is_calc
            _run_test_suite(run_calc_tests, is_calc, "Calc tests")
        elif action == "RunCalcIntegrationTests":
            from plugin.tests.test_calc import run_calc_integration_tests
            from plugin.framework.document import is_calc
            _run_test_suite(run_calc_integration_tests, is_calc, "Calc API tests")
        elif action == "RunDrawTests":
            from plugin.tests.test_draw import run_draw_tests
            from plugin.framework.document import is_draw
            _run_test_suite(run_draw_tests, is_draw, "Draw tests")
        elif action == "NoOp":
            pass
        return

    # Module actions
    for mod in _modules:
        if mod.name == mod_name:
            mod.on_action(action)
            return

    log = logging.getLogger("writeragent.main")
    log.warning("Module not found for command: %s", command)


def get_menu_text(command):
    """Get dynamic menu text for a command, or None for default."""
    dot = command.find(".")
    if dot <= 0:
        return None
    mod_name = command[:dot]
    action = command[dot + 1:]
    for mod in _modules:
        if mod.name == mod_name:
            return mod.get_menu_text(action)
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
            except Exception:
                pass
        _status_listeners[:] = alive
    # Update icons in a background thread (avoids blocking UI)
    threading.Thread(target=_update_menu_icons, daemon=True).start()


def _fire_status_event(listener, url, text):
    """Send a FeatureStateEvent to one listener."""
    ev = uno.createUnoStruct("com.sun.star.frame.FeatureStateEvent")
    ev.FeatureURL = url
    ev.IsEnabled = True
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
    for m in MODULES:
        mod_name = m["name"]
        action_icons = m.get("action_icons", {})
        for action_name, default_icon in action_icons.items():
            cmd_url = "%s%s.%s" % (_DISPATCH_PROTOCOL, mod_name, action_name)
            # Ask the module for dynamic icon (may override the default)
            dynamic = _get_menu_icon("%s.%s" % (mod_name, action_name))
            result[cmd_url] = (mod_name, dynamic or default_icon)
    return result


def _load_icon_graphic(module_name, icon_filename):
    """Load a PNG icon from a module's icons/ directory as XGraphic."""
    try:
        from com.sun.star.beans import PropertyValue
        gp = smgr.createInstanceWithContext(
            "com.sun.star.graphic.GraphicProvider", ctx)
        ext_url = get_extension_url(ctx)
        if not ext_url:
            return None
        pv = PropertyValue()
        # Support nested module directories
        mod_dir = module_name.replace(".", "/")
        pv.Value = "%s/plugin/modules/%s/icons/%s" % (
            ext_url, mod_dir, icon_filename)
        return gp.queryGraphic((pv,))
    except Exception:
        return None


def _update_menu_icons():
    """Push current-state icons into every module's ImageManager."""
    try:
        import uno
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
            graphic = _load_icon_graphic(mod_name, filename)
            if graphic:
                key_graphics[key] = graphic

        if not key_graphics:
            return

        ctx = uno.getComponentContext()
        smgr = ctx.ServiceManager

        supplier = smgr.createInstanceWithContext(
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
                        except Exception:
                            pass
            except Exception:
                pass
    except Exception:
        pass

def _get_http_module(ctx=None):
    if ctx:
        bootstrap(ctx)
    for mod in _modules:
        if getattr(mod, "name", "") == "http":
            return mod
    return None

def _start_mcp_server(ctx):
    """Ensure HTTP/MCP server is loaded. Start happens natively in module lifecycle."""
    from plugin.framework.config import get_config, as_bool
    if not as_bool(get_config(ctx, "mcp_enabled")):
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
    _start_mcp_server(ctx)


# Bootstrapper replaces the previous monolithic MainJob.
# It acts as an OnStartApp hook and a proxy for legacy toolbar triggers.
class MainBootstrapJob(unohelper.Base, XJobExecutor, XJob):
    def __init__(self, ctx):
        self.ctx = ctx
        try:
            self.sm = ctx.getServiceManager()
        except NameError:
            self.sm = ctx.ServiceManager

    def execute(self, args):
        """Called by the Jobs framework on OnStartApp."""
        try:
            bootstrap(self.ctx)
        except Exception:
            pass
        return ()

    def trigger(self, args):
        bootstrap(self.ctx)
        init_logging(self.ctx)

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
        from plugin.framework.document import is_writer, is_calc
        
        if is_writer(model):
            self._handle_writer_actions(args, model)
        elif is_calc(model):
            self._handle_calc_actions(args, model)

    def _handle_framework_actions(self, args):
        framework_args = ("ToggleMCPServer", "MCPStatus", "TestTypes", "DrainMCP", "RunFormatTests", "RunCalcTests", "RunDrawTests", "RunCalcIntegrationTests", "EvaluationDashboard", "NoOp")
        if args not in framework_args:
            return False
            
        if args == "ToggleMCPServer": _dispatch_command("http.toggle_server")
        elif args == "MCPStatus": _dispatch_command("http.server_status")
        elif args == "DrainMCP":
            from plugin.modules.http.mcp_protocol import drain_mcp_queue
            drain_mcp_queue()
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
        ctx = XSCRIPTCONTEXT
    except NameError:
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

    def initialize(self, args):
        pass

    # ── XServiceInfo ─────────────────────────────────────────────

    def getImplementationName(self):
        return self.IMPL_NAME

    def supportsService(self, name):
        return name in self.SERVICE_NAMES

    def getSupportedServiceNames(self):
        return self.SERVICE_NAMES

    # ── XDispatchProvider ────────────────────────────────────────

    def queryDispatch(self, url, name, flags):
        if url.Protocol == "org.extension.writeragent:":
            return self
        return None

    def queryDispatches(self, requests):
        return [self.queryDispatch(r.FeatureURL, r.FrameName,
                                   r.SearchFlags) for r in requests]

    # ── XDispatch ────────────────────────────────────────────────

    def dispatch(self, url, args):
        command = url.Path
        try:
            bootstrap(self.ctx)
            init_logging(self.ctx)
            _dispatch_command(command)
            # After action, push updated menu text
            threading.Thread(target=notify_menu_update,
                             daemon=True).start()
        except Exception:
            pass

    def addStatusListener(self, listener, url):
        with _status_lock:
            _status_listeners.append((listener, url))
        # Send current state immediately
        command = url.Path
        text = get_menu_text(command)
        if text is not None:
            try:
                _fire_status_event(listener, url, text)
            except Exception:
                pass

    def removeStatusListener(self, listener, url):
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
