# Chat with Document - Sidebar Panel implementation
# Follows the working pattern from LibreOffice's Python ToolPanel example:
# XUIElement wrapper creates panel in getRealInterface() via ContainerWindowProvider + XDL.

import os
import sys
import weakref
import hashlib
import uuid
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

# Add contrib (and contrib/audio for sounddevice when recording is enabled) so this file can be loaded by LibreOffice
_vendor_dir = os.path.join(_ext_root, "contrib")
if _vendor_dir not in sys.path:
    sys.path.insert(0, _vendor_dir)
_audio_dir = os.path.join(_ext_root, "contrib", "audio")
if _audio_dir not in sys.path:
    sys.path.insert(0, _audio_dir)

# Recording available only if audio_recorder (and thus contrib/audio) is present
try:
    from plugin.modules.chatbot.audio_recorder import start_recording, stop_recording  # noqa: F401
    HAS_RECORDING = True
except ImportError:
    HAS_RECORDING = False

from plugin.framework.logging import debug_log, start_watchdog_thread, init_logging
from plugin.modules.chatbot.panel import ChatSession, SendButtonListener, StopButtonListener, ClearButtonListener
from plugin.framework.uno_helpers import get_optional as get_optional_control, get_checkbox_state, set_checkbox_state, get_active_document, get_extension_url, get_extension_path, is_writer, is_calc, is_draw

from com.sun.star.ui import XUIElementFactory, XUIElement, XToolPanel, XSidebarPanel
from com.sun.star.ui.UIElementType import TOOLPANEL
from com.sun.star.awt import XItemListener, XWindowListener

# Extension ID from description.xml; XDL path inside the .oxt
EXTENSION_ID = "org.extension.writeragent"
XDL_PATH = "WriterAgentDialogs/ChatPanelDialog.xdl"

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
    try:
        ext_path = get_extension_path(ctx)
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



class _PanelResizeListener(unohelper.Base, XWindowListener):
    """Adjusts panel layout on resize. Reads control sizes/gaps from the XDL;
    only the response area height changes to fill available space."""

    def __init__(self, controls):
        self._c = controls        # dict name -> control or None
        self._initial = None      # captured from XDL-loaded pixel positions
        self._in_relayout = False

    def windowResized(self, evt):
        r = evt.Source.getPosSize()
        debug_log("windowResized: W=%d H=%d" % (r.Width, r.Height), context="Chat")
        if self._in_relayout:
            debug_log("windowResized: skipped (in_relayout)", context="Chat")
            return
        try:
            self._in_relayout = True
            self._relayout(evt.Source)
        except Exception as e:
            debug_log("windowResized error: %s" % e, context="Chat")
        finally:
            self._in_relayout = False

    def windowMoved(self, evt): pass
    def windowShown(self, evt): pass
    def windowHidden(self, evt): pass
    def disposing(self, evt): pass

    def _capture_initial(self, win):
        """Snapshot XDL-loaded pixel positions/sizes of every control."""
        r = win.getPosSize()
        if r.Width <= 0 or r.Height <= 0:
            return
        info = {"win_w": r.Width, "win_h": r.Height, "ctrls": {}}
        resp = self._c.get("response")
        if resp:
            rr = resp.getPosSize()
            info["resp_bottom"] = rr.Y + rr.Height
        for name, ctrl in self._c.items():
            if ctrl:
                cr = ctrl.getPosSize()
                info["ctrls"][name] = (cr.X, cr.Y, cr.Width, cr.Height)
        self._initial = info

    def _relayout(self, win):
        r = win.getPosSize()
        w, h = r.Width, r.Height
        if w <= 0 or h <= 0:
            return

        if self._initial is None:
            self._capture_initial(win)
        if self._initial is None:
            debug_log("_relayout: no initial state, skip", context="Chat")
            return

        # Use anchoring/filling instead of scaling ratios to prevent feedback loops.
        # Controls in fluid_controls will stretch to fill width.
        # Buttons and labels stay fixed size and anchored left.
        fluid_controls = ("response", "query", "model_selector", "image_model_selector", "status", "aspect_ratio_selector")

        top_of_bottom = h  # will track highest new_y below response
        for name, ctrl in self._c.items():
            if not ctrl or name == "response":
                continue
            orig = self._initial["ctrls"].get(name)
            if not orig:
                continue
            ox, oy, ow, oh = orig

            if name in fluid_controls:
                # Fill space to right margin
                new_x = ox
                margin_right = iw - (ox + ow)
                new_w = max(10, w - ox - margin_right)
            else:
                # Fixed size, anchored left
                new_x = ox
                new_w = ow

            if oy >= resp_bottom:
                # Anchor to bottom: preserve distance from bottom edge
                new_y = h - (ih - oy)
                cur = ctrl.getPosSize()
                if cur.X != new_x or cur.Y != new_y or cur.Width != new_w or cur.Height != oh:
                    ctrl.setPosSize(new_x, new_y, new_w, oh, 15)
                top_of_bottom = min(top_of_bottom, new_y)
            else:
                # Above response: stay anchored to top
                cur = ctrl.getPosSize()
                if cur.X != new_x or cur.Y != oy or cur.Width != new_w or cur.Height != oh:
                    ctrl.setPosSize(new_x, oy, new_w, oh, 15)

        # Second pass: stretch response area to fill remaining vertical gap
        resp_orig = self._initial["ctrls"].get("response")
        resp_ctrl = self._c.get("response")
        if resp_orig and resp_ctrl:
            rx, ry, rw, rh = resp_orig
            gap = resp_bottom - (ry + rh)  # original gap below response
            if gap < 0:
                gap = 2
            new_rh = max(30, top_of_bottom - gap - ry)
            
            # Fill width to right margin
            margin_right = iw - (rx + rw)
            new_rw = max(10, w - rx - margin_right)
            
            cur = resp_ctrl.getPosSize()
            if cur.X != rx or cur.Y != ry or cur.Width != new_rw or cur.Height != new_rh:
                resp_ctrl.setPosSize(rx, ry, new_rw, new_rh, 15)


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
            h = parent_rect.Height if parent_rect.Height > 0 else 400
            self.PanelWindow.setPosSize(0, 0, width, h, 15)
            debug_log("panel constrained to W=%s H=%s" % (width, h), context="Chat")
        # LayoutSize(Minimum, Maximum, Preferred) — IDL field order.
        # Maximum=-1 means unbounded; the sidebar gives all remaining height
        # to panels with unbounded max (see DeckLayouter.cxx DistributeHeights).
        return uno.createUnoStruct("com.sun.star.ui.LayoutSize", 100, -1, 400)

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
        base_url = get_extension_url()
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

    def _render_session_history(self, session, response_ctrl, model, greeting=""):
        """Update the response control with the contents of the given session."""
        try:
            if response_ctrl and response_ctrl.getModel():
                text = greeting + "\n" if greeting else ""
                
                # Append loaded history (skipping system context)
                for msg in session.messages:
                    role = msg.get("role", "")
                    content = msg.get("content", "")
                    if role == "user":
                        text += "\nUser: %s\n" % content
                    elif role == "assistant":
                        if content:
                            text += "\nAssistant: %s" % content
                        elif msg.get("tool_calls"):
                            text += "\nAssistant: [Thinking...]"
                        text += "\n"
                
                response_ctrl.getModel().Text = text
                # Scroll to bottom
                if hasattr(response_ctrl, "setSelection"):
                    length = len(text)
                    import uno
                    response_ctrl.setSelection(uno.createUnoStruct("com.sun.star.awt.Selection", length, length))
        except Exception as e:
            debug_log("_render_session_history error: %s" % e, context="Chat")

    def _refresh_controls_from_config(self):
        """Reload model and prompt selectors from config (e.g. after user changes Settings)."""
        root = self.m_panelRootWindow
        if not root or not hasattr(root, "getControl"):
            return
        from plugin.framework.config import get_config, get_current_endpoint, populate_combobox_with_lru, get_text_model, get_image_model, populate_image_model_selector, set_config, set_image_model

        def get_optional(name):
            return get_optional_control(root, name)

        model_selector = get_optional("model_selector")
        prompt_selector = get_optional("prompt_selector")
        image_model_selector = get_optional("image_model_selector")
        
        current_model = get_text_model(self.ctx)
        extra_instructions = get_config(self.ctx, "additional_instructions", "")
        
        current_endpoint = get_current_endpoint(self.ctx)
        
        if model_selector:
            set_val = populate_combobox_with_lru(self.ctx, model_selector, current_model, "model_lru", current_endpoint)
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

    def _get_document_model(self):
        """Helper to get the current document model."""
        model = None
        if self.xFrame:
            try:
                model = self.xFrame.getController().getModel()
            except Exception:
                pass
        if model is None:
            model = get_active_document(self.ctx)
        return model

    def _wire_model_selectors(self, model_selector, image_model_selector):
        """Initializes model selectors and their sync listeners."""
        from plugin.framework.config import get_current_endpoint, get_text_model, get_image_model, populate_combobox_with_lru, populate_image_model_selector, set_image_model, set_config
        
        current_model = get_text_model(self.ctx)
        current_endpoint = get_current_endpoint(self.ctx)
        
        if model_selector:
            set_model_val = populate_combobox_with_lru(self.ctx, model_selector, current_model, "model_lru", current_endpoint)
            if set_model_val != current_model:
                set_config(self.ctx, "text_model", set_model_val)
                
        if image_model_selector:
            current_image = get_image_model(self.ctx)
            set_image_val = populate_image_model_selector(self.ctx, image_model_selector)
            if set_image_val != current_image:
                set_image_model(self.ctx, set_image_val, update_lru=False)

        if model_selector and hasattr(model_selector, "addItemListener"):
            class ModelSyncListener(unohelper.Base, XItemListener):
                def __init__(self, ctx): self.ctx = ctx
                def itemStateChanged(self, ev):
                    try:
                        txt = model_selector.getText()
                        if txt: set_config(self.ctx, "text_model", txt)
                    except Exception: pass
                def disposing(self, ev): pass
            model_selector.addItemListener(ModelSyncListener(self.ctx))

        if image_model_selector and hasattr(image_model_selector, "addItemListener"):
            class ImageModelSyncListener(unohelper.Base, XItemListener):
                def __init__(self, ctx): self.ctx = ctx
                def itemStateChanged(self, ev):
                    try:
                        txt = image_model_selector.getText()
                        if txt: set_image_model(self.ctx, txt, update_lru=False)
                    except Exception: pass
                def disposing(self, ev): pass
            image_model_selector.addItemListener(ImageModelSyncListener(self.ctx))

    def _wire_image_ui(self, aspect_ratio_selector, base_size_input, base_size_label, 
                       direct_image_check, web_search_check, model_label, model_selector, image_model_selector):
        """Initializes image-related UI controls and their listeners."""
        from plugin.framework.config import get_config, set_config
        
        if aspect_ratio_selector:
            aspect_ratio_selector.addItems(("Square", "Landscape (16:9)", "Portrait (9:16)", "Landscape (3:2)", "Portrait (2:3)"), 0)
            aspect_ratio_selector.setText(get_config(self.ctx, "image_default_aspect", "Square"))
            
        if base_size_input:
            from plugin.framework.config import populate_combobox_with_lru
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

        def set_control_enabled(ctrl, enabled):
            if ctrl:
                if hasattr(ctrl, "setEnable"): ctrl.setEnable(enabled)
                elif hasattr(ctrl.getModel(), "Enabled"): ctrl.getModel().Enabled = enabled

        def toggle_image_ui(is_image_mode):
            if model_label and hasattr(model_label, "setVisible"): model_label.setVisible(not is_image_mode)
            if model_selector and hasattr(model_selector, "setVisible"): model_selector.setVisible(not is_image_mode)
            if image_model_selector and hasattr(image_model_selector, "setVisible"): image_model_selector.setVisible(is_image_mode)
            if aspect_ratio_selector and hasattr(aspect_ratio_selector, "setVisible"): aspect_ratio_selector.setVisible(is_image_mode)
            if base_size_input and hasattr(base_size_input, "setVisible"): base_size_input.setVisible(is_image_mode)
            if base_size_label and hasattr(base_size_label, "setVisible"): base_size_label.setVisible(is_image_mode)

        if direct_image_check:
            try:
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
                                is_checked = (getattr(ev, "Selected", 0) == 1)
                                set_config(self.ctx, "chat_direct_image", is_checked)
                                self.toggle_cb(is_checked)
                                set_control_enabled(self.web_check, not is_checked)
                            except Exception as e:
                                debug_log("Image checkbox listener error: %s" % e, context="Chat")
                        def disposing(self, ev): pass
                    direct_image_check.addItemListener(DirectImageCheckListener(self.ctx, toggle_image_ui, web_search_check))
            except Exception as e:
                debug_log("direct_image_check wire error: %s" % e, context="Chat")

        if web_search_check:
            try:
                if get_checkbox_state(web_search_check) == 1:
                    set_control_enabled(direct_image_check, False)
            except Exception as e:
                debug_log("web_search_check initial wire error: %s" % e, context="Chat")
                
        return set_control_enabled

    def _setup_sessions(self, model, extra_instructions):
        """Creates the document and web research chat sessions."""
        from plugin.framework.constants import get_chat_system_prompt_for_document, DEFAULT_CHAT_SYSTEM_PROMPT
        from plugin.framework.uno_helpers import get_document_property, set_document_property
        
        if model and (is_writer(model) or is_calc(model) or is_draw(model)):
            system_prompt = get_chat_system_prompt_for_document(model, extra_instructions or "")
        else:
            system_prompt = (DEFAULT_CHAT_SYSTEM_PROMPT + "\n\n" + str(extra_instructions)) if extra_instructions else DEFAULT_CHAT_SYSTEM_PROMPT

        session_id = get_document_property(model, "WriterAgentSessionID")
        if not session_id:
            if model and hasattr(model, "getURL"):
                url = model.getURL()
                if url: session_id = hashlib.sha256(url.encode('utf-8')).hexdigest()
            if not session_id:
                session_id = str(uuid.uuid4())
            if model:
                set_document_property(model, "WriterAgentSessionID", session_id)
        
        self.doc_session = ChatSession(system_prompt, session_id=session_id)
        self.web_session = ChatSession("Observe: Always use the web_search tool to answer questions.", session_id=session_id + "_web")
        self.session = self.doc_session

    def _wire_buttons(self, controls, model, active_greeting, set_control_enabled):
        """Wires up the Send, Stop, Clear, and Research toggle buttons."""
        send_listener = None
        try:
            send_listener = SendButtonListener(
                self.ctx, self.xFrame,
                controls["send"], controls["stop"], controls["query"], controls["response"],
                controls["image_model_selector"], controls["model_selector"], controls["status"], self.session,
                direct_image_checkbox=controls["direct_image_check"],
                aspect_ratio_selector=controls["aspect_ratio_selector"],
                base_size_input=controls["base_size_input"],
                web_search_checkbox=controls["web_search_check"],
                ensure_path_fn=_ensure_extension_on_path)

            if model:
                if is_calc(model): send_listener.initial_doc_type = "Calc"
                elif is_draw(model): send_listener.initial_doc_type = "Draw"
                elif is_writer(model): send_listener.initial_doc_type = "Writer"
                else: send_listener.initial_doc_type = "Unknown"

            if controls["send"]:
                controls["send"].addActionListener(send_listener)
            start_watchdog_thread(self.ctx, controls["status"])

            if controls["stop"]:
                controls["stop"].addActionListener(StopButtonListener(send_listener))
            send_listener._set_button_states(send_enabled=True, stop_enabled=False)
        except Exception as e:
            debug_log("Send/Stop button error: %s" % e, context="Chat")

        clear_listener = None
        if controls["clear"]:
            try:
                clear_listener = ClearButtonListener(self.session, controls["response"], controls["status"], greeting=active_greeting)
                controls["clear"].addActionListener(clear_listener)
            except Exception: pass

        if controls["web_search_check"] and hasattr(controls["web_search_check"], "addItemListener"):
            from plugin.framework.constants import get_greeting_for_document, DEFAULT_RESEARCH_GREETING
            class ResearchChatToggledListener(unohelper.Base, XItemListener):
                def __init__(self, panel, response_ctrl, model, send_listener, clear_listener, img_check, set_control_enabled):
                    self.panel = panel
                    self.response_ctrl = response_ctrl
                    self.model = model
                    self.send_listener = send_listener
                    self.clear_listener = clear_listener
                    self.img_check = img_check
                    self.set_control_enabled = set_control_enabled

                def itemStateChanged(self, ev):
                    try:
                        is_research = (getattr(ev, "Selected", 0) == 1)
                        self.set_control_enabled(self.img_check, not is_research)
                        
                        if is_research:
                            self.panel.session = self.panel.web_session
                            greeting = DEFAULT_RESEARCH_GREETING
                        else:
                            self.panel.session = self.panel.doc_session
                            greeting = get_greeting_for_document(self.model)
                        
                        if self.send_listener:
                            self.send_listener.set_session(self.panel.session)
                        if self.clear_listener:
                            self.clear_listener.set_session(self.panel.session, greeting=greeting)
                        self.panel._render_session_history(self.panel.session, self.response_ctrl, self.model, greeting)
                    except Exception as e:
                        debug_log("Research Chat listener error: %s" % e, context="Chat")
                def disposing(self, ev): pass
            controls["web_search_check"].addItemListener(ResearchChatToggledListener(
                self, controls["response"], model, send_listener, clear_listener, controls["direct_image_check"], set_control_enabled))

    def _wireControls(self, root_window):
        """Main entry point to wire all controls for the panel."""
        debug_log("_wireControls entered", context="Chat")
        if not hasattr(root_window, "getControl"):
            debug_log("_wireControls: root_window has no getControl, aborting", context="Chat")
            return

        def get_optional(name):
            return get_optional_control(root_window, name)

        controls = {
            "send": root_window.getControl("send"),
            "query": root_window.getControl("query"),
            "response": root_window.getControl("response"),
            "stop": get_optional("stop"),
            "clear": get_optional("clear"),
            "image_model_selector": get_optional("image_model_selector"),
            "prompt_selector": get_optional("prompt_selector"),
            "model_selector": get_optional("model_selector"),
            "model_label": get_optional("model_label"),
            "status": get_optional("status"),
            "direct_image_check": get_optional("direct_image_check"),
            "web_search_check": get_optional("web_search_check"),
            "aspect_ratio_selector": get_optional("aspect_ratio_selector"),
            "base_size_input": get_optional("base_size_input"),
            "base_size_label": get_optional("base_size_label"),
            "response_label": get_optional("response_label"),
            "query_label": get_optional("query_label")
        }

        # Helper to show errors visibly in the response area
        def _show_init_error(msg):
            debug_log("_wireControls ERROR: %s" % msg, context="Chat")
            try:
                if controls["response"] and controls["response"].getModel():
                    current = controls["response"].getModel().Text or ""
                    controls["response"].getModel().Text = current + "[Init error: %s]\n" % msg
            except Exception:
                pass

        _ensure_extension_on_path(self.ctx)

        # 1. Config, Models, and UI
        try:
            from plugin.framework.config import get_config
            extra_instructions = get_config(self.ctx, "additional_instructions", "")
            
            self._wire_model_selectors(controls["model_selector"], controls["image_model_selector"])
            
            set_control_enabled = self._wire_image_ui(
                controls["aspect_ratio_selector"], controls["base_size_input"], controls["base_size_label"],
                controls["direct_image_check"], controls["web_search_check"], controls["model_label"], 
                controls["model_selector"], controls["image_model_selector"]
            )
        except Exception as e:
            import traceback
            _show_init_error("Config: %s" % e)
            debug_log(traceback.format_exc(), context="Chat")
            extra_instructions = ""
            set_control_enabled = lambda ctrl, en: None

        # 2. Setup Sessions
        model = self._get_document_model()
        self._setup_sessions(model, extra_instructions)

        # 3. Determine Mode & Greeting
        from plugin.framework.constants import get_greeting_for_document, DEFAULT_RESEARCH_GREETING
        web_checked = False
        if controls["web_search_check"]:
            try: web_checked = (get_checkbox_state(controls["web_search_check"]) == 1)
            except Exception: pass
            
        if web_checked:
            self.session = self.web_session
            active_greeting = DEFAULT_RESEARCH_GREETING
        else:
            self.session = self.doc_session
            active_greeting = get_greeting_for_document(model)

        self._render_session_history(self.session, controls["response"], model, active_greeting)

        # 4. Buttons
        self._wire_buttons(controls, model, active_greeting, set_control_enabled)

        # Wire query listener to update Record/Send button label
        if controls["query"] and controls["send"]:
            try:
                from plugin.modules.chatbot.panel import QueryTextListener
                controls["query"].addTextListener(QueryTextListener(controls["send"]))
                if controls["query"].getModel().Text.strip():
                    controls["send"].getModel().Label = "Send"
                else:
                    controls["send"].getModel().Label = "Record" if HAS_RECORDING else "Send"
            except Exception as e:
                debug_log("QueryTextListener setup error: %s" % e, context="Chat")

        if controls["status"] and hasattr(controls["status"], "setText"):
            try: controls["status"].setText("Ready")
            except Exception: pass

        # 5. Timer and Resize
        try:
            from main import try_ensure_mcp_timer
            try_ensure_mcp_timer(self.ctx)
        except Exception as e:
            debug_log("try_ensure_mcp_timer: %s" % e, context="Chat")

        try:
            _resize = _PanelResizeListener(controls)
            root_window.addWindowListener(_resize)
            _resize._relayout(root_window)
        except Exception as e:
            debug_log("Resize listener error: %s" % e, context="Chat")
            
        # 6. Global Config Listener
        from plugin.framework.config import add_config_listener
        _self_ref = weakref.ref(self)
        def on_config_changed(ctx):
            panel = _self_ref()
            if panel is not None:
                panel._refresh_controls_from_config()
        add_config_listener(on_config_changed)


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
    "org.extension.writeragent.ChatPanelFactory",
    ("com.sun.star.ui.UIElementFactory",),
)
