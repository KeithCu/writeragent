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
# Chat with Document - Sidebar Panel implementation
# Follows the working pattern from LibreOffice's Python ToolPanel example:
# XUIElement wrapper creates panel in getRealInterface() via ContainerWindowProvider + XDL.

import logging
import os
import sys
import hashlib
import uuid
import uno
import unohelper

# Ensure the extension's install directory is on sys.path
# so that "plugin.xxx" imports work correctly. This file lives at
# plugin/modules/chatbot/panel_factory.py; the extension root (directory
# containing the "plugin" package) is 4 levels up from here.
_this_file = os.path.abspath(__file__)
for _i in range(4):
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
    from plugin.modules.chatbot.audio_recorder import stop_recording  # noqa: F401
    HAS_RECORDING = True
except ImportError:
    HAS_RECORDING = False

from plugin.framework.logging import start_watchdog_thread, init_logging
from plugin.modules.chatbot.panel import ChatSession, SendButtonListener, StopButtonListener, ClearButtonListener
from plugin.framework.dialogs import (
    get_optional as get_optional_control,
    get_checkbox_state,
    set_checkbox_state,
    set_control_text,
    set_control_enabled,
    set_control_visible
)
from plugin.framework.uno_context import get_active_document, get_extension_url, get_extension_path
from plugin.framework.document import is_writer, is_calc, is_draw
from plugin.modules.chatbot.panel_wiring import _wireControls as wire_chatpanel_controls

from com.sun.star.ui import XUIElementFactory, XUIElement, XToolPanel, XSidebarPanel
from com.sun.star.ui.UIElementType import TOOLPANEL
from plugin.framework.listeners import BaseItemListener
from com.sun.star.awt import XItemListener

log = logging.getLogger(__name__)

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
            log.info("Added extension path to sys.path: %s" % ext_path)
        else:
            init_logging(ctx)
            log.debug("Extension path already on sys.path: %s" % ext_path)
    except Exception as e:
        init_logging(ctx)
        log.error("_ensure_extension_on_path ERROR: %s" % e)




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
        # Set by panel wiring after _PanelResizeListener is created.
        self.resize_listener = None
        # Last deck hint from getHeightForWidth; used with parent to detect intrinsic inflation.
        self._last_deck_w = None

    def getWindow(self):
        return self.Window

    def createAccessible(self, parent_accessible):
        return self.PanelWindow

    def getHeightForWidth(self, width):
        # LayoutSize(Minimum, Maximum, Preferred) — IDL field order.
        # Maximum=-1 means unbounded; the sidebar gives all remaining height
        # to panels with unbounded max (see DeckLayouter.cxx DistributeHeights).
        if not self.parent_window or not self.PanelWindow or width <= 0:
            return uno.createUnoStruct("com.sun.star.ui.LayoutSize", 100, -1, 400)

        parent_rect = self.parent_window.getPosSize()
        parent_w = parent_rect.Width
        parent_h = parent_rect.Height
        h = parent_h if parent_h > 0 else 400
        deck_w = width
        self._last_deck_w = deck_w
        # When parent tracks deck (typical user-sized sidebar), fill full parent so fluid
        # controls stretch. When parent runs far ahead of deck (intrinsic inflation), clamp
        # to min(parent, deck) — see writeragent_debug.log (must match panel_resize).
        _DIVERGENCE_PX = 80
        if parent_w > 0 and deck_w > 0:
            if parent_w > deck_w + _DIVERGENCE_PX:
                eff_w = min(parent_w, deck_w)
            else:
                eff_w = parent_w
        elif parent_w > 0:
            eff_w = min(parent_w, deck_w)
        else:
            eff_w = deck_w

        try:
            before = self.PanelWindow.getPosSize()
        except Exception as e:
            from com.sun.star.lang import DisposedException
            from com.sun.star.uno import RuntimeException, Exception as UnoException
            if isinstance(e, (DisposedException, RuntimeException, UnoException)):
                log.debug("getHeightForWidth: PanelWindow likely disposed: %s", e)
            before = None

        log.debug(
            "getHeightForWidth deck_hint=%s parent=%sx%s eff_W=%s root_before=%s"
            % (
                deck_w,
                parent_w,
                parent_h,
                eff_w,
                "%sx%s" % (before.Width, before.Height) if before else None,
            )
        )
        try:
            self.PanelWindow.setPosSize(0, 0, eff_w, h, 15)
            after = self.PanelWindow.getPosSize()
            log.debug(
                "getHeightForWidth root_after=%sx%s" % (after.Width, after.Height)
            )
        except Exception as e:
            from com.sun.star.lang import DisposedException
            from com.sun.star.uno import RuntimeException, Exception as UnoException
            if isinstance(e, (DisposedException, RuntimeException, UnoException)):
                log.debug("getHeightForWidth: failed to set or get pos size (likely disposed): %s", e)

        rl = getattr(self, "resize_listener", None)
        if rl is not None:
            try:
                rl.relayout_now(self.PanelWindow)
            except Exception as e:
                from com.sun.star.lang import DisposedException
                from com.sun.star.uno import RuntimeException, Exception as UnoException
                if isinstance(e, (DisposedException, RuntimeException, UnoException)):
                    log.debug("getHeightForWidth relayout_now failed (likely disposed): %s", e)
                else:
                    log.debug("getHeightForWidth relayout_now: %s" % e)

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
        log.debug("=== getRealInterface called ===")
        if not self.toolpanel:
            try:
                # Ensure extension on path early so _wireControls imports work
                _ensure_extension_on_path(self.ctx)
                root_window = self._getOrCreatePanelRootWindow()
                log.debug("root_window created: %s" % (root_window is not None))
                self.toolpanel = ChatToolPanel(root_window, self.xParentWindow, self.ctx)
                wire_chatpanel_controls(self, root_window, HAS_RECORDING, _ensure_extension_on_path)
                log.info("getRealInterface completed successfully")
            except Exception as e:
                from plugin.framework.errors import UnoObjectError
                log.error("getRealInterface ERROR [resource_url=%s]: %s", self.ResourceURL, e)
                import traceback
                log.error(traceback.format_exc())
                raise UnoObjectError("Failed to create ChatPanel UI element", details={"resource": self.ResourceURL}) from e
        return self.toolpanel

    def _getOrCreatePanelRootWindow(self):
        log.debug("_getOrCreatePanelRootWindow entered")
        base_url = get_extension_url()
        dialog_url = base_url + "/" + XDL_PATH
        log.debug("dialog_url: %s" % dialog_url)
        provider = self.ctx.getServiceManager().createInstanceWithContext(
            "com.sun.star.awt.ContainerWindowProvider", self.ctx)
        log.debug("calling createContainerWindow...")
        self.m_panelRootWindow = provider.createContainerWindow(
            dialog_url, "", self.xParentWindow, None)
        log.debug("createContainerWindow returned")
        # Sidebar does not show the panel content without this (framework does not make it visible).
        if self.m_panelRootWindow and hasattr(self.m_panelRootWindow, "setVisible"):
            try:
                self.m_panelRootWindow.setVisible(True)
            except Exception as e:
                from com.sun.star.lang import DisposedException
                from com.sun.star.uno import RuntimeException, Exception as UnoException
                if isinstance(e, (DisposedException, RuntimeException, UnoException)):
                    log.debug("Failed to set panel root window visible (likely disposed): %s", e)
        # Constrain panel only when parent already has size (layout may be 0x0 here).
        try:
            parent_rect = self.xParentWindow.getPosSize()
            if parent_rect.Width > 0 and parent_rect.Height > 0:
                self.m_panelRootWindow.setPosSize(
                    0, 0, parent_rect.Width, parent_rect.Height, 15)
                log.debug("panel constrained to W=%s H=%s" % (parent_rect.Width, parent_rect.Height))
        except Exception as e:
            from com.sun.star.lang import DisposedException
            from com.sun.star.uno import RuntimeException, Exception as UnoException
            if isinstance(e, (DisposedException, RuntimeException, UnoException)):
                log.debug("Failed to constrain panel window (likely disposed): %s", e)
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
                
                set_control_text(response_ctrl, text)
                # Scroll to bottom
                if hasattr(response_ctrl, "setSelection"):
                    length = len(text)
                    import uno
                    response_ctrl.setSelection(uno.createUnoStruct("com.sun.star.awt.Selection", length, length))
        except Exception as e:
            log.error("_render_session_history error [greeting=%s]: %s", greeting, e)

    def _refresh_controls_from_config(self):
        """Reload model and prompt selectors from config (e.g. after user changes Settings)."""
        root = self.m_panelRootWindow
        if not root or not hasattr(root, "getControl"):
            return
        from plugin.framework.config import get_config, get_current_endpoint, populate_combobox_with_lru, get_text_model, get_image_model, populate_image_model_selector, set_config, set_image_model

        def get_optional(name):
            return get_optional_control(root, name)

        from plugin.framework.dialogs import translate_dialog
        translate_dialog(root)

        model_selector = get_optional("model_selector")
        prompt_selector = get_optional("prompt_selector")
        image_model_selector = get_optional("image_model_selector")
        
        current_model = get_text_model(self.ctx)
        extra_instructions = get_config(self.ctx, "additional_instructions")
        
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
                direct_checked = get_config(self.ctx, "chat_direct_image")
                set_checkbox_state(direct_image_check, 1 if direct_checked else 0)
            except Exception:
                pass
        try:
            # Backend indicator: show "Aider" / "Hermes" when external agent backend is enabled
            self._update_backend_indicator(root)
        except Exception as e:
            log.error("_refresh_controls_from_config backend indicator error: %s", e)

    def _update_backend_indicator(self, root_window=None):
        """Set backend indicator label from config (visible when external backend enabled) and gray out controls."""
        try:
            from plugin.framework.config import get_config
            from plugin.framework.i18n import _
            from plugin.modules.agent_backend.registry import (
                AGENT_BACKEND_REGISTRY,
                normalize_backend_id,
            )

            root = root_window or (getattr(self, "m_panelRootWindow", None))
            if not root or not hasattr(root, "getControl"):
                return

            backend_id = normalize_backend_id(get_config(self.ctx, "agent_backend.backend_id"))
            is_external = bool(backend_id and backend_id != "builtin")

            ctrl = get_optional_control(root, "backend_indicator")
            if ctrl:
                if is_external:
                    entry = AGENT_BACKEND_REGISTRY.get(backend_id)
                    display_en = entry[0] if entry else backend_id.capitalize()
                    set_control_text(ctrl, _(display_en))
                    if hasattr(ctrl, "setVisible"):
                        ctrl.setVisible(True)
                else:
                    set_control_text(ctrl, "")
                    if hasattr(ctrl, "setVisible"):
                        ctrl.setVisible(False)

            # Enable/disable the LLM model selector based on the agent backend
            model_selector = get_optional_control(root, "model_selector")
            if model_selector and hasattr(model_selector, "getModel"):
                set_control_enabled(model_selector, not is_external)
                
            direct_image_check = get_optional_control(root, "direct_image_check")
            if direct_image_check and hasattr(direct_image_check, "getModel"):
                set_control_enabled(direct_image_check, not is_external)
                
            web_research_check = get_optional_control(root, "web_research_check")
            if web_research_check and hasattr(web_research_check, "getModel"):
                set_control_enabled(web_research_check, not is_external)
                
        except Exception as e:
            log.error("_update_backend_indicator error: %s" % e)

    def _get_document_model(self):
        """Helper to get the current document model."""
        model = None
        if self.xFrame:
            try:
                model = self.xFrame.getController().getModel()
            except Exception as e:
                from com.sun.star.lang import DisposedException
                from com.sun.star.uno import RuntimeException, Exception as UnoException
                if isinstance(e, (DisposedException, RuntimeException, UnoException)):
                    log.debug("Failed to get model from frame controller (likely disposed): %s", e)
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
            class ModelSyncListener(BaseItemListener):
                def __init__(self, ctx): self.ctx = ctx
                def on_item_state_changed(self, ev):
                    txt = model_selector.getText()
                    if txt: set_config(self.ctx, "text_model", txt)
            model_selector.addItemListener(ModelSyncListener(self.ctx))

        if image_model_selector and hasattr(image_model_selector, "addItemListener"):
            class ImageModelSyncListener(BaseItemListener):
                def __init__(self, ctx): self.ctx = ctx
                def on_item_state_changed(self, ev):
                    txt = image_model_selector.getText()
                    if txt: set_image_model(self.ctx, txt, update_lru=False)
            image_model_selector.addItemListener(ImageModelSyncListener(self.ctx))

    def _wire_image_ui(self, aspect_ratio_selector, base_size_input, base_size_label, 
                       direct_image_check, web_research_check, model_label, model_selector, image_model_selector):
        """Initializes image-related UI controls and their listeners."""
        from plugin.framework.config import get_config, set_config
        
        if aspect_ratio_selector:
            aspect_ratio_selector.addItems(("Square", "Landscape (16:9)", "Portrait (9:16)", "Landscape (3:2)", "Portrait (2:3)"), 0)
            aspect_ratio_selector.setText(get_config(self.ctx, "image_default_aspect") or "Square")
            
        if base_size_input:
            from plugin.framework.config import populate_combobox_with_lru
            populate_combobox_with_lru(self.ctx, base_size_input, str(get_config(self.ctx, "image_base_size")), "image_base_size_lru", "")

        def update_base_size_label(aspect_str):
            from plugin.framework.i18n import _
            if not base_size_label: return
            txt = _("Size:")
            if "Landscape" in aspect_str: txt = _("Height:")
            elif "Portrait" in aspect_str: txt = _("Width:")
            if hasattr(base_size_label, "setText"):
                base_size_label.setText(txt)
            elif hasattr(base_size_label.getModel(), "Label"):
                base_size_label.getModel().Label = txt

        if aspect_ratio_selector:
            update_base_size_label(aspect_ratio_selector.getText())
            if hasattr(aspect_ratio_selector, "addItemListener"):
                class AspectListener(BaseItemListener):
                    def on_item_state_changed(self, ev):
                        idx = getattr(ev, "Selected", -1)
                        if idx >= 0:
                            update_base_size_label(aspect_ratio_selector.getItem(idx))
                aspect_ratio_selector.addItemListener(AspectListener())

        # We now use the global set_control_enabled and set_control_visible from plugin.framework.dialogs

        def toggle_image_ui(is_image_mode):
            set_control_visible(model_label, not is_image_mode)
            set_control_visible(model_selector, not is_image_mode)
            set_control_visible(image_model_selector, is_image_mode)
            set_control_visible(aspect_ratio_selector, is_image_mode)
            set_control_visible(base_size_input, is_image_mode)
            set_control_visible(base_size_label, is_image_mode)
            # Visibility swap changes vertical cluster; reflow so combos keep correct width.
            tp = getattr(self, "toolpanel", None)
            root = getattr(self, "m_panelRootWindow", None)
            rl = getattr(tp, "resize_listener", None) if tp else None
            if rl and root:
                try:
                    rl.relayout_now(root)
                except Exception as e:
                    from com.sun.star.lang import DisposedException
                    from com.sun.star.uno import RuntimeException, Exception as UnoException
                    if isinstance(e, (DisposedException, RuntimeException, UnoException)):
                        log.debug("Failed to relayout after toggling image UI (likely disposed): %s", e)

        if direct_image_check:
            try:
                direct_checked = get_config(self.ctx, "chat_direct_image")
                set_checkbox_state(direct_image_check, 1 if direct_checked else 0)
                toggle_image_ui(direct_checked)
                
                if direct_checked:
                    set_control_enabled(web_research_check, False)
                    
                if hasattr(direct_image_check, "addItemListener"):
                    class DirectImageCheckListener(BaseItemListener):
                        def __init__(self, ctx, toggle_cb, web_check):
                            self.ctx = ctx
                            self.toggle_cb = toggle_cb
                            self.web_check = web_check
                        def on_item_state_changed(self, ev):
                            is_checked = (getattr(ev, "Selected", 0) == 1)
                            set_config(self.ctx, "chat_direct_image", is_checked)
                            self.toggle_cb(is_checked)
                            set_control_enabled(self.web_check, not is_checked)
                    direct_image_check.addItemListener(DirectImageCheckListener(self.ctx, toggle_image_ui, web_research_check))
            except Exception as e:
                from plugin.framework.errors import ConfigError
                if isinstance(e, ConfigError):
                    log.error("direct_image_check ConfigError: %s" % e)
                else:
                    from com.sun.star.lang import DisposedException
                    from com.sun.star.uno import RuntimeException, Exception as UnoException
                    if isinstance(e, (DisposedException, RuntimeException, UnoException)):
                        log.debug("direct_image_check wire error (likely disposed): %s", e)
                    else:
                        log.error("direct_image_check wire error: %s" % e)

        if web_research_check:
            try:
                if get_checkbox_state(web_research_check) == 1:
                    set_control_enabled(direct_image_check, False)
            except Exception as e:
                from com.sun.star.lang import DisposedException
                from com.sun.star.uno import RuntimeException, Exception as UnoException
                if isinstance(e, (DisposedException, RuntimeException, UnoException)):
                    log.debug("web_research_check initial wire error (likely disposed): %s", e)
                else:
                    log.error("web_research_check initial wire error: %s", e)

    def _setup_sessions(self, model, extra_instructions):
        """Creates the document and web research chat sessions."""
        from plugin.framework.constants import get_chat_system_prompt_for_document, DEFAULT_CHAT_SYSTEM_PROMPT
        from plugin.framework.document import get_document_property, set_document_property
        
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

    def _wire_buttons(self, controls, model, active_greeting):
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
                web_research_checkbox=controls["web_research_check"],
                ensure_path_fn=_ensure_extension_on_path,
                clear_control=controls.get("clear"))

            # Save it to the instance so panel_wiring can use it for QueryTextListener
            self.send_listener = send_listener

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
            log.error("Send/Stop button wiring error: %s", e)

        clear_listener = None
        if controls["clear"]:
            try:
                clear_listener = ClearButtonListener(
                    self.session,
                    controls["response"],
                    controls["status"],
                    greeting=active_greeting,
                    send_listener=send_listener,
                )
                controls["clear"].addActionListener(clear_listener)
            except Exception as e:
                log.exception("Clear button wiring error: %s", e)

        if controls["web_research_check"] and hasattr(controls["web_research_check"], "addItemListener"):
            from plugin.framework.constants import get_greeting_for_document, DEFAULT_RESEARCH_GREETING
            class ResearchChatToggledListener(BaseItemListener):
                def __init__(self, panel, response_ctrl, model, send_listener, clear_listener, img_check):
                    self.panel = panel
                    self.response_ctrl = response_ctrl
                    self.model = model
                    self.send_listener = send_listener
                    self.clear_listener = clear_listener
                    self.img_check = img_check

                def on_item_state_changed(self, ev):
                    from plugin.framework.i18n import _
                    is_research = (getattr(ev, "Selected", 0) == 1)
                    set_control_enabled(self.img_check, not is_research)

                    if is_research:
                        self.panel.session = self.panel.web_session
                        greeting = _(DEFAULT_RESEARCH_GREETING)
                    else:
                        self.panel.session = self.panel.doc_session
                        greeting = get_greeting_for_document(self.model)

                    if self.send_listener:
                        self.send_listener.set_session(self.panel.session)
                    if self.clear_listener:
                        self.clear_listener.set_session(self.panel.session, greeting=greeting)
                    self.panel._render_session_history(self.panel.session, self.response_ctrl, self.model, greeting)

            controls["web_research_check"].addItemListener(ResearchChatToggledListener(
                self, controls["response"], model, send_listener, clear_listener, controls["direct_image_check"]))



class ChatPanelFactory(unohelper.Base, XUIElementFactory):
    """Factory that creates ChatPanelElement instances for the sidebar."""

    def __init__(self, ctx):
        self.ctx = ctx

    def createUIElement(self, resource_url, args):
        log.debug("createUIElement: %s" % resource_url)
        if "ChatPanel" not in resource_url:
            from com.sun.star.container import NoSuchElementException
            raise NoSuchElementException("Unknown resource: " + resource_url)

        frame = _get_arg(args, "Frame")
        parent_window = _get_arg(args, "ParentWindow")
        log.debug("ParentWindow: %s" % (parent_window is not None))
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