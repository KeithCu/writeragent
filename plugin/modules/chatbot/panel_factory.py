# Chat with Document - Sidebar Panel implementation
# Follows the working pattern from LibreOffice's Python ToolPanel example:
# XUIElement wrapper creates panel in getRealInterface() via ContainerWindowProvider + XDL.

import os
import sys
import json
import queue
import threading
import weakref
import uno
import unohelper

# Ensure the extension's install directory is on sys.path
# so that "plugin.xxx" imports work correctly. This file lives at
# plugin/modules/chatbot/panel_factory.py; the extension root (directory
# containing the "plugin" package) is 4 levels up from here.
_this_file = os.path.abspath(__file__)
for _ in range(4):
    _this_file = os.path.dirname(_this_file)
_ext_root = _this_file
if _ext_root not in sys.path:
    sys.path.insert(0, _ext_root)

from plugin.framework.logging import agent_log, debug_log, update_activity_state, start_watchdog_thread, init_logging
from plugin.modules.chatbot.streaming import run_stream_completion_async, run_stream_drain_loop
from plugin.modules.chatbot.panel import ChatSession, SendButtonListener, StopButtonListener, ClearButtonListener
from plugin.framework.uno_helpers import get_optional as get_optional_control, get_checkbox_state, set_checkbox_state

from com.sun.star.ui import XUIElementFactory, XUIElement, XToolPanel, XSidebarPanel
from com.sun.star.ui.UIElementType import TOOLPANEL
from com.sun.star.awt import XActionListener, XItemListener

# Extension ID from description.xml; XDL path inside the .oxt
EXTENSION_ID = "org.extension.localwriter"
XDL_PATH = "LocalWriterDialogs/ChatPanelDialog.xdl"

# Default max tool rounds when not in config (get_api_config supplies chat_max_tool_rounds)
DEFAULT_MAX_TOOL_ROUNDS = 5

# Default system prompt for the chat sidebar (imported from main inside methods to avoid unopkg errors)
DEFAULT_SYSTEM_PROMPT_FALLBACK = "You are a helpful assistant."


def _get_arg(args, name):
    """Extract PropertyValue from args by Name."""
    for pv in args:
        if hasattr(pv, "Name") and pv.Name == name:
            return pv.Value
    return None





def _ensure_extension_on_path(ctx):
    """Add the extension's directory to sys.path so cross-module imports work.
    LibreOffice registers each .py as a UNO component individually but does not
    put the extension folder on sys.path, so 'from main import ...' and
    'from document_tools import ...' fail without this."""
    import sys
    try:
        pip = ctx.getValueByName(
            "/singletons/com.sun.star.deployment.PackageInformationProvider")
        ext_url = pip.getPackageLocation(EXTENSION_ID)
        if ext_url.startswith("file://"):
            ext_path = str(uno.fileUrlToSystemPath(ext_url))
        else:
            ext_path = ext_url
        if ext_path and ext_path not in sys.path:
            sys.path.insert(0, ext_path)
            init_logging(ctx)
            debug_log("Added extension path to sys.path: %s" % ext_path, context="Chat")
        else:
            init_logging(ctx)
            debug_log("Extension path already on sys.path: %s" % ext_path, context="Chat")
    except Exception as e:
        init_logging(ctx)
        debug_log("_ensure_extension_on_path ERROR: %s" % e, context="Chat")



# FIXME: Dynamic resizing of panel controls when sidebar is resized.
# The sidebar allocates a fixed height (from getHeightForWidth) and does not
# scroll, so a PanelResizeListener (XWindowListener) that repositions controls
# bottom-up would be the right approach.  However, the sidebar gives the panel
# window a very large initial height (1375px) before settling to the requested
# size, which causes controls to be positioned off-screen during the first
# layout pass.  Needs investigation into the sidebar's resize lifecycle.
# For now the XDL uses a compact fixed layout that works at the default size.


# ---------------------------------------------------------------------------
# ChatToolPanel, ChatPanelElement, ChatPanelFactory (sidebar plumbing)
# ---------------------------------------------------------------------------

class ChatToolPanel(unohelper.Base, XToolPanel, XSidebarPanel):
    """Holds the panel window; implements XToolPanel and XSidebarPanel."""

    def __init__(self, panel_window, parent_window, ctx):
        self.ctx = ctx
        self.PanelWindow = panel_window
        self.Window = panel_window
        self.parent_window = parent_window

    def getWindow(self):
        return self.Window

    def createAccessible(self, parent_accessible):
        return self.PanelWindow

    def getHeightForWidth(self, width):
        debug_log("getHeightForWidth(width=%s)" % width, context="Chat")
        # Constrain panel to sidebar width (and parent height when available).
        if self.parent_window and self.PanelWindow and width > 0:
            parent_rect = self.parent_window.getPosSize()
            h = parent_rect.Height if parent_rect.Height > 0 else 280
            self.PanelWindow.setPosSize(0, 0, width, h, 15)
            debug_log("panel constrained to W=%s H=%s" % (width, h), context="Chat")
        # Min 280, preferred -1 (let sidebar decide), max 280 — matches working Git layout.
        return uno.createUnoStruct("com.sun.star.ui.LayoutSize", 280, -1, 280)

    def getMinimalWidth(self):
        return 180


class ChatPanelElement(unohelper.Base, XUIElement):
    """XUIElement wrapper; creates panel window in getRealInterface() via ContainerWindowProvider."""

    def __init__(self, ctx, frame, parent_window, resource_url):
        self.ctx = ctx
        self.xFrame = frame
        self.xParentWindow = parent_window
        self.ResourceURL = resource_url
        self.Frame = frame
        self.Type = TOOLPANEL
        self.toolpanel = None
        self.m_panelRootWindow = None
        self.session = None  # Created in _wireControls

    def getRealInterface(self):
        debug_log("=== getRealInterface called ===", context="Chat")
        if not self.toolpanel:
            try:
                # Ensure extension on path early so _wireControls imports work
                _ensure_extension_on_path(self.ctx)
                root_window = self._getOrCreatePanelRootWindow()
                debug_log("root_window created: %s" % (root_window is not None), context="Chat")
                self.toolpanel = ChatToolPanel(root_window, self.xParentWindow, self.ctx)
                self._wireControls(root_window)
                debug_log("getRealInterface completed successfully", context="Chat")
            except Exception as e:
                debug_log("getRealInterface ERROR: %s" % e, context="Chat")
                import traceback
                debug_log(traceback.format_exc(), context="Chat")
                raise
        return self.toolpanel

    def _getOrCreatePanelRootWindow(self):
        debug_log("_getOrCreatePanelRootWindow entered", context="Chat")
        pip = self.ctx.getValueByName(
            "/singletons/com.sun.star.deployment.PackageInformationProvider")
        base_url = pip.getPackageLocation(EXTENSION_ID)
        dialog_url = base_url + "/" + XDL_PATH
        debug_log("dialog_url: %s" % dialog_url, context="Chat")
        provider = self.ctx.getServiceManager().createInstanceWithContext(
            "com.sun.star.awt.ContainerWindowProvider", self.ctx)
        debug_log("calling createContainerWindow...", context="Chat")
        self.m_panelRootWindow = provider.createContainerWindow(
            dialog_url, "", self.xParentWindow, None)
        debug_log("createContainerWindow returned", context="Chat")
        # Sidebar does not show the panel content without this (framework does not make it visible).
        if self.m_panelRootWindow and hasattr(self.m_panelRootWindow, "setVisible"):
            self.m_panelRootWindow.setVisible(True)
        # Constrain panel only when parent already has size (layout may be 0x0 here).
        parent_rect = self.xParentWindow.getPosSize()
        if parent_rect.Width > 0 and parent_rect.Height > 0:
            self.m_panelRootWindow.setPosSize(
                0, 0, parent_rect.Width, parent_rect.Height, 15)
            debug_log("panel constrained to W=%s H=%s" % (
                parent_rect.Width, parent_rect.Height), context="Chat")
        return self.m_panelRootWindow

    def _refresh_controls_from_config(self):
        """Reload model and prompt selectors from config (e.g. after user changes Settings)."""
        root = self.m_panelRootWindow
        if not root or not hasattr(root, "getControl"):
            return
        from plugin.modules.core.services.config import get_config, get_current_endpoint, populate_combobox_with_lru, get_text_model, get_image_model, populate_image_model_selector, set_config, set_image_model

        def get_optional(name):
            return get_optional_control(root, name)

        model_selector = get_optional("model_selector")
        prompt_selector = get_optional("prompt_selector")
        image_model_selector = get_optional("image_model_selector")
        
        current_model = get_text_model(self.ctx)
        extra_instructions = get_config(self.ctx, "additional_instructions", "")
        
        current_endpoint = get_current_endpoint(self.ctx)
        
        if model_selector:
            set_val = populate_combobox_with_lru(self.ctx, model_selector, current_model, "model_lru", current_endpoint, strict=True)
            if set_val != current_model:
                set_config(self.ctx, "text_model", set_val)
        if prompt_selector:
            populate_combobox_with_lru(self.ctx, prompt_selector, extra_instructions, "prompt_lru", "")
            
        # Refresh visual (image) model via shared helper; persist correction if strict replaced value
        if image_model_selector:
            current_image = get_image_model(self.ctx)
            set_image_val = populate_image_model_selector(self.ctx, image_model_selector)
            if set_image_val != current_image:
                set_image_model(self.ctx, set_image_val, update_lru=False)
        # Sync "Use Image model" checkbox from config (same write as Settings: setState first, else model.State)
        direct_image_check = get_optional("direct_image_check")
        if direct_image_check:
            try:
                direct_checked = get_config(self.ctx, "chat_direct_image", False)
                set_checkbox_state(direct_image_check, 1 if direct_checked else 0)
            except Exception:
                pass

    def _wireControls(self, root_window):
        """Attach listeners to Send and Clear buttons."""
        debug_log("_wireControls entered", context="Chat")
        if not hasattr(root_window, "getControl"):
            debug_log("_wireControls: root_window has no getControl, aborting", context="Chat")
            return

        # Get controls -- these must exist in the XDL
        send_btn = root_window.getControl("send")
        query_ctrl = root_window.getControl("query")
        response_ctrl = root_window.getControl("response")

        def get_optional(name):
            return get_optional_control(root_window, name)

        image_model_selector = get_optional("image_model_selector")
        prompt_selector = get_optional("prompt_selector")
        model_selector = get_optional("model_selector")
        model_label = get_optional("model_label")
        status_ctrl = get_optional("status")
        direct_image_check = get_optional("direct_image_check")
        web_search_check = get_optional("web_search_check")
        aspect_ratio_selector = get_optional("aspect_ratio_selector")
        base_size_input = get_optional("base_size_input")
        base_size_label = get_optional("base_size_label")
        
        if status_ctrl:
             debug_log("_wireControls: got status control", context="Chat")
        else:
             debug_log("_wireControls: no status control in XDL (ok)", context="Chat")

        # Helper to show errors visibly in the response area
        def _show_init_error(msg):
            debug_log("_wireControls ERROR: %s" % msg, context="Chat")
            try:
                if response_ctrl and response_ctrl.getModel():
                    current = response_ctrl.getModel().Text or ""
                    response_ctrl.getModel().Text = current + "[Init error: %s]\n" % msg
            except Exception:
                pass

        # Ensure extension directory is on sys.path for cross-module imports
        _ensure_extension_on_path(self.ctx)

        model = None
        try:
            # Read system prompt from config; use helper so Writer/Calc prompt matches document
            debug_log("_wireControls: importing core config...", context="Chat")
            from plugin.modules.core.services.config import get_config, get_current_endpoint, get_text_model, get_image_model, populate_combobox_with_lru, populate_image_model_selector, set_image_model, set_config
            from plugin.framework.constants import get_chat_system_prompt_for_document, DEFAULT_CHAT_SYSTEM_PROMPT
            from plugin.modules.core.services.document import is_writer, is_calc, is_draw
            
            extra_instructions = get_config(self.ctx, "additional_instructions", "")
            current_model = get_text_model(self.ctx)
            current_endpoint = get_current_endpoint(self.ctx)
            
            # Model selector: strict so only current endpoint's models shown; persist correction if needed
            if model_selector:
                set_model_val = populate_combobox_with_lru(self.ctx, model_selector, current_model, "model_lru", current_endpoint, strict=True)
                if set_model_val != current_model:
                    set_config(self.ctx, "text_model", set_model_val)
            # Adaptive image model population via shared helper (uses strict for endpoint); persist correction if needed
            if image_model_selector:
                current_image = get_image_model(self.ctx)
                set_image_val = populate_image_model_selector(self.ctx, image_model_selector)
                if set_image_val != current_image:
                    set_image_model(self.ctx, set_image_val, update_lru=False)

            # Add real-time sync listeners to selectors
            if model_selector and hasattr(model_selector, "addItemListener"):
                class ModelSyncListener(unohelper.Base, XItemListener):
                    def __init__(self, ctx): self.ctx = ctx
                    def itemStateChanged(self, ev):
                        try:
                            txt = model_selector.getText()
                            if txt:
                                set_config(self.ctx, "text_model", txt)
                                # No LRU update here to avoid cluttering history from accidental clicks
                        except Exception: pass
                    def disposing(self, ev): pass
                model_selector.addItemListener(ModelSyncListener(self.ctx))

            if image_model_selector and hasattr(image_model_selector, "addItemListener"):
                class ImageModelSyncListener(unohelper.Base, XItemListener):
                    def __init__(self, ctx): self.ctx = ctx
                    def itemStateChanged(self, ev):
                        try:
                            txt = image_model_selector.getText()
                            if txt:
                                set_image_model(self.ctx, txt, update_lru=False)
                        except Exception: pass
                    def disposing(self, ev): pass
                image_model_selector.addItemListener(ImageModelSyncListener(self.ctx))

            # Initialize aspect ratio and base size
            if aspect_ratio_selector:
                aspect_ratio_selector.addItems(("Square", "Landscape (16:9)", "Portrait (9:16)", "Landscape (3:2)", "Portrait (2:3)"), 0)
                aspect_ratio_selector.setText(get_config(self.ctx, "image_default_aspect", "Square"))
            if base_size_input:
                populate_combobox_with_lru(self.ctx, base_size_input, str(get_config(self.ctx, "image_base_size", 512)), "image_base_size_lru", "")

            def update_base_size_label(aspect_str):
                if not base_size_label: return
                txt = "Size:"
                if "Landscape" in aspect_str: txt = "Height:"
                elif "Portrait" in aspect_str: txt = "Width:"
                if hasattr(base_size_label, "setText"):
                    base_size_label.setText(txt)
                elif hasattr(base_size_label.getModel(), "Label"):
                    base_size_label.getModel().Label = txt

            if aspect_ratio_selector:
                update_base_size_label(aspect_ratio_selector.getText())
                if hasattr(aspect_ratio_selector, "addItemListener"):
                    class AspectListener(unohelper.Base, XItemListener):
                        def itemStateChanged(self, ev):
                            try:
                                idx = getattr(ev, "Selected", -1)
                                if idx >= 0:
                                    update_base_size_label(aspect_ratio_selector.getItem(idx))
                            except Exception: pass
                        def disposing(self, ev): pass
                    aspect_ratio_selector.addItemListener(AspectListener())

            # Helper to toggle visibility
            def toggle_image_ui(is_image_mode):
                if model_label and hasattr(model_label, "setVisible"):
                    model_label.setVisible(not is_image_mode)
                if model_selector and hasattr(model_selector, "setVisible"):
                    model_selector.setVisible(not is_image_mode)
                
                if image_model_selector and hasattr(image_model_selector, "setVisible"):
                    image_model_selector.setVisible(is_image_mode)
                    
                if aspect_ratio_selector and hasattr(aspect_ratio_selector, "setVisible"):
                    aspect_ratio_selector.setVisible(is_image_mode)
                    
                if base_size_input and hasattr(base_size_input, "setVisible"):
                    base_size_input.setVisible(is_image_mode)
                if base_size_label and hasattr(base_size_label, "setVisible"):
                    base_size_label.setVisible(is_image_mode)

            # Helper to enable/disable controls
            def set_control_enabled(ctrl, enabled):
                if ctrl:
                    if hasattr(ctrl, "setEnable"):
                        ctrl.setEnable(enabled)
                    elif hasattr(ctrl.getModel(), "Enabled"):
                        ctrl.getModel().Enabled = enabled

            # "Use Image model" checkbox
            if direct_image_check:
                try:
                    from plugin.modules.core.services.config import set_config
                    direct_checked = get_config(self.ctx, "chat_direct_image", False)
                    set_checkbox_state(direct_image_check, 1 if direct_checked else 0)
                    toggle_image_ui(direct_checked)
                    
                    if direct_checked:
                        set_control_enabled(web_search_check, False)
                        
                    if hasattr(direct_image_check, "addItemListener"):
                        class DirectImageCheckListener(unohelper.Base, XItemListener):
                            def __init__(self, ctx, toggle_cb, web_check):
                                self.ctx = ctx
                                self.toggle_cb = toggle_cb
                                self.web_check = web_check
                            def itemStateChanged(self, ev):
                                try:
                                    state = getattr(ev, "Selected", 0)
                                    is_checked = (state == 1)
                                    
                                    set_config(self.ctx, "chat_direct_image", is_checked)
                                    self.toggle_cb(is_checked)
                                    set_control_enabled(self.web_check, not is_checked)
                                except Exception as e:
                                    debug_log("Image checkbox listener error: %s" % e, context="Chat")
                            def disposing(self, ev):
                                pass
                        direct_image_check.addItemListener(DirectImageCheckListener(self.ctx, toggle_image_ui, web_search_check))
                except Exception as e:
                    debug_log("direct_image_check wire error: %s" % e, context="Chat")

            # "Web Research" checkbox
            if web_search_check:
                try:
                    # If web search should also disable image model at startup if it was checked:
                    from plugin.framework.uno_helpers import get_checkbox_state
                    web_is_checked = get_checkbox_state(web_search_check) == 1
                    if web_is_checked:
                        set_control_enabled(direct_image_check, False)

                    if hasattr(web_search_check, "addItemListener"):
                        class WebSearchCheckListener(unohelper.Base, XItemListener):
                            def __init__(self, img_check):
                                self.img_check = img_check
                            def itemStateChanged(self, ev):
                                try:
                                    state = getattr(ev, "Selected", 0)
                                    is_checked = (state == 1)
                                    set_control_enabled(self.img_check, not is_checked)
                                except Exception as e:
                                    debug_log("Web search check listener error: %s" % e, context="Chat")
                            def disposing(self, ev):
                                pass
                        web_search_check.addItemListener(WebSearchCheckListener(direct_image_check))
                except Exception as e:
                    debug_log("web_search_check wire error: %s" % e, context="Chat")

            # Register for config changes (e.g. Settings dialog). Weakref so this panel can be
            # GC'd without unregistering; callback no-ops if panel is gone.
            from plugin.modules.core.services.config import add_config_listener
            _self_ref = weakref.ref(self)
            def on_config_changed(ctx):
                panel = _self_ref()
                if panel is not None:
                    panel._refresh_controls_from_config()
            add_config_listener(on_config_changed)

            if self.xFrame:
                try:
                    model = self.xFrame.getController().getModel()
                except Exception:
                    pass
            if model is None:
                try:
                    smgr = self.ctx.getServiceManager()
                    desktop = smgr.createInstanceWithContext("com.sun.star.frame.Desktop", self.ctx)
                    model = desktop.getCurrentComponent()
                except Exception:
                    pass
            if model and (is_writer(model) or is_calc(model) or is_draw(model)):
                system_prompt = get_chat_system_prompt_for_document(model, extra_instructions or "")
            else:
                system_prompt = (DEFAULT_CHAT_SYSTEM_PROMPT + "\n\n" + str(extra_instructions)) if extra_instructions else DEFAULT_CHAT_SYSTEM_PROMPT
            debug_log("_wireControls: config loaded", context="Chat")
        except Exception as e:
            import traceback
            _show_init_error("Config: %s" % e)
            debug_log(traceback.format_exc(), context="Chat")
            system_prompt = DEFAULT_SYSTEM_PROMPT_FALLBACK

        # Create session
        self.session = ChatSession(system_prompt)

        # Wire Send button
        try:
            stop_btn = root_window.getControl("stop")
            send_listener = SendButtonListener(
                self.ctx, self.xFrame,
                send_btn, stop_btn, query_ctrl, response_ctrl,
                image_model_selector, model_selector, status_ctrl, self.session,
                direct_image_checkbox=direct_image_check,
                aspect_ratio_selector=aspect_ratio_selector,
                base_size_input=base_size_input,
                web_search_checkbox=web_search_check,
                ensure_path_fn=_ensure_extension_on_path)

            # Detect and store initial document type for strict verification
            if model:
                from plugin.modules.core.services.document import is_calc, is_draw, is_writer
                if is_calc(model):
                    send_listener.initial_doc_type = "Calc"
                elif is_draw(model):
                    send_listener.initial_doc_type = "Draw"
                elif is_writer(model):
                    send_listener.initial_doc_type = "Writer"
                else:
                    send_listener.initial_doc_type = "Unknown"
                debug_log("_wireControls: detected initial_doc_type=%s" % send_listener.initial_doc_type, context="Chat")

            send_btn.addActionListener(send_listener)
            debug_log("Send button wired", context="Chat")
            start_watchdog_thread(self.ctx, status_ctrl)

            if stop_btn:
                stop_btn.addActionListener(StopButtonListener(send_listener))
                debug_log("Stop button wired", context="Chat")
            # Initial state: Send enabled, Stop disabled (no AI running yet)
            send_listener._set_button_states(send_enabled=True, stop_enabled=False)
        except Exception as e:
            _show_init_error("Send/Stop button: %s" % e)

        # Show ready message
        try:
            if response_ctrl and response_ctrl.getModel():
                from plugin.framework.constants import get_greeting_for_document
                greeting = get_greeting_for_document(model)
                response_ctrl.getModel().Text = "%s\n" % greeting
        except Exception:
            pass

        # Wire Clear button (may not exist in older XDL)
        try:
            clear_btn = root_window.getControl("clear")
            if clear_btn:
                clear_btn.addActionListener(ClearButtonListener(
                    self.session, response_ctrl, status_ctrl))
                debug_log("Clear button wired", context="Chat")
        except Exception:
            pass

        try:
            if status_ctrl and hasattr(status_ctrl, "setText"):
                status_ctrl.setText("Ready")
        except Exception:
            pass

        # Start MCP drain timer if server is running but timer was not started from main.
        # If timer fails (e.g. no 'com' in this context), we still drain on user interaction below.
        try:
            from main import try_ensure_mcp_timer
            try_ensure_mcp_timer(self.ctx)
        except Exception as e:
            debug_log("try_ensure_mcp_timer: %s" % e, context="Chat")

        # FIXME: Wire PanelResizeListener here once dynamic resizing is fixed.
        # See FIXME comment above the commented-out PanelResizeListener class.


class ChatPanelFactory(unohelper.Base, XUIElementFactory):
    """Factory that creates ChatPanelElement instances for the sidebar."""

    def __init__(self, ctx):
        self.ctx = ctx

    def createUIElement(self, resource_url, args):
        debug_log("createUIElement: %s" % resource_url, context="Chat")
        if "ChatPanel" not in resource_url:
            from com.sun.star.container import NoSuchElementException
            raise NoSuchElementException("Unknown resource: " + resource_url)

        frame = _get_arg(args, "Frame")
        parent_window = _get_arg(args, "ParentWindow")
        debug_log("ParentWindow: %s" % (parent_window is not None), context="Chat")
        if not parent_window:
            from com.sun.star.lang import IllegalArgumentException
            raise IllegalArgumentException("ParentWindow is required")

        return ChatPanelElement(self.ctx, frame, parent_window, resource_url)


g_ImplementationHelper = unohelper.ImplementationHelper()
g_ImplementationHelper.addImplementation(
    ChatPanelFactory,
    "org.extension.localwriter.ChatPanelFactory",
    ("com.sun.star.ui.UIElementFactory",),
)
