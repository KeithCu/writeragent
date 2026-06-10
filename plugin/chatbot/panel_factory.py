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

from __future__ import annotations

import logging
import os
import sys
from typing import TYPE_CHECKING, cast
import hashlib
import uuid
import uno
import unohelper
import traceback

from com.sun.star.lang import DisposedException, IllegalArgumentException
from com.sun.star.uno import RuntimeException, Exception as UnoException
from com.sun.star.container import NoSuchElementException

# Common exceptions for UI components that may be disposed during layout/refresh
UNO_DISPOSED_EXCEPTIONS = (DisposedException, RuntimeException, UnoException)

# Ensure the extension's install directory is on sys.path so that normal
# "import plugin.xxx" statements work when LibreOffice loads this module.
# See plugin/framework/uno_bootstrap.py for the centralized implementation
# and rationale (this used to be duplicated fragile path logic).

# Minimal stdlib-only bootstrap (must run before the "from plugin..." import below)
# because unopkg writeRegistryInfo loads this file before the OXT root is on sys.path.
_this = os.path.abspath(__file__)
for __ in range(3):  # plugin/chatbot/panel_factory.py → plugin/chatbot/ → plugin/ → extension root
    _this = os.path.dirname(_this)
if _this not in sys.path:
    sys.path.insert(0, _this)

from plugin.framework.uno_bootstrap import ensure_plugin_on_path

ensure_plugin_on_path(__file__, levels_up=3, also_add_contrib=True)

# Recording available only if audio_recorder (and thus plugin/contrib/audio) is present
try:
    from plugin.chatbot.audio_recorder import AudioRecorder  # noqa: F401

    HAS_RECORDING = True
except ImportError:
    HAS_RECORDING = False

from plugin.framework.logging import start_watchdog_thread, init_logging
from plugin.chatbot.dialogs import get_optional as get_optional_control, set_control_text, set_control_enabled, set_control_visible
from plugin.framework.uno_context import get_extension_url, get_extension_path
from plugin.chatbot.panel_wiring import _wireControls as wire_chatpanel_controls

if TYPE_CHECKING:
    from com.sun.star.uno import XInterface

from com.sun.star.ui import XUIElementFactory, XUIElement, XToolPanel, XSidebarPanel

try:
    from com.sun.star.ui.UIElementType import TOOLPANEL  # type: ignore
except ImportError:
    TOOLPANEL = 3  # Fallback

from plugin.chatbot.listeners import BaseItemListener
from plugin.framework.config import get_config, set_config, get_current_endpoint
from plugin.framework.client.model_fetcher import get_text_model, get_image_model, set_image_model
from plugin.framework.i18n import _
from plugin.framework.errors import UnoObjectError, ConfigError
from plugin.framework.constants import get_chat_system_prompt_for_document, get_greeting_for_document, DEFAULT_RESEARCH_GREETING, DEFAULT_BRAINSTORMING_GREETING
from plugin.doc.document_helpers import get_document_property, set_document_property, get_document_type, DocumentType

log = logging.getLogger(__name__)

# Extension ID from description.xml; XDL path inside the .oxt
EXTENSION_ID = "org.extension.writeragent"
XDL_PATH = "WriterAgentDialogs/ChatPanelDialog.xdl"
_PRE_NEGOTIATION_PANEL_WIDTH = 420

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


_paths_initialized = False


def _initialize_extension_paths(ctx):
    """Initialize extension paths once per session."""
    global _paths_initialized
    if _paths_initialized:
        return

    try:
        ext_path = get_extension_path(ctx)
        if ext_path and ext_path not in sys.path:
            sys.path.insert(0, ext_path)

        contrib_dir = os.path.join(ext_path, "contrib")
        if contrib_dir not in sys.path:
            sys.path.insert(0, contrib_dir)

        # Audio paths (only if needed)
        if HAS_RECORDING:
            audio_dir = os.path.join(ext_path, "plugin", "contrib", "audio")
            if audio_dir not in sys.path:
                sys.path.insert(0, audio_dir)

        init_logging(ctx)
        log.info("Initialized extension paths for session: %s" % ext_path)
        try:
            from plugin.writer.locale.ai_grammar_proofreader import ensure_writeragent_proofreader_configured

            ensure_writeragent_proofreader_configured(ctx)
        except Exception as e:
            log.warning("[grammar] sidebar init: could not load or run grammar proofreader bootstrap: %s", e, exc_info=True)
        _paths_initialized = True
    except Exception as e:
        init_logging(ctx)
        log.error("_initialize_extension_paths ERROR: %s" % e)


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

    def getWindow(self):
        return self.Window

    def createAccessible(self, ParentAccessible):
        return self.PanelWindow

    def getHeightForWidth(self, nWidth: int):  # pyright: ignore[reportIncompatibleMethodOverride]
        """Return LayoutSize and ensure our PanelWindow width matches the allocated sidebar column.

        This is now the *single source of truth* for the panel's horizontal size.
        The old bidirectional root-sync dance in _PanelResizeListener has been removed.

        Key fix for the persistent H scrollbar:
        - On startup (and some resizes) LO passes a huge deck_hint (main frame width, ~1170px)
          even when the sidebar is docked narrow.
        - If our current actual width is modest (<450) but the hint is huge, we clamp
          instead of widening the root to ~1160px and creating a permanent scrollbar.
        """
        width = nWidth
        if not self.parent_window or not self.PanelWindow or width <= 0:
            return uno.createUnoStruct("com.sun.star.ui.LayoutSize", 100, -1, 400)

        parent_rect = self.parent_window.getPosSize()
        parent_w = parent_rect.Width
        parent_h = parent_rect.Height
        deck_w = width

        # Read current actual size *before* we decide.
        try:
            before = self.PanelWindow.getPosSize()
            current_w = before.Width if before else 0
            current_h = before.Height if before else 0
        except Exception as e:
            if isinstance(e, UNO_DISPOSED_EXCEPTIONS):
                log.debug("getHeightForWidth: PanelWindow likely disposed: %s", e)
            before = None
            current_w = 0
            current_h = 0

        # Width is negotiated here; height stays whatever LO/deck already allocated.
        if current_h <= 0:
            current_h = parent_h if parent_h > 0 else 400

        # NOTE (2026-05): This getHeightForWidth logic (including the relatively simple
        # handling of large deck_hints) is restored from commit af649476 because it
        # produced better real-world horizontal scrollbar behavior in the plain-text
        # sidebar. Later experiments (aggressive 480px caps on large hints + tighter
        # child margins) made the H scrollbar appear more persistently, even when
        # the user widened the sidebar.
        #
        # Future changes to this function should be made very carefully and tested
        # thoroughly with the layout_sanity log and real sidebar resizing.
        #
        # Simple policy:
        # - Prefer the deck hint when it looks like a real column width.
        # - If deck hint is huge (>500, typical of startup "frame width" queries) but we are
        #   currently narrow (<450), this is the classic docked-startup mis-hint.
        #   Clamp to something that will actually fit the docked column.
        if deck_w > 0:
            if deck_w > 500 and 0 < current_w < 450:
                # Startup huge-hint while actually docked narrow → clamp hard.
                eff_w = min(deck_w, parent_w if parent_w > 0 else 380, 420)
            else:
                eff_w = deck_w
        elif parent_w > 0:
            eff_w = parent_w
        else:
            eff_w = 220

        log.debug("getHeightForWidth deck_hint=%s parent=%sx%s current_root=%s eff_W=%s" % (deck_w, parent_w, parent_h, "%sx%s" % (before.Width, before.Height) if before else None, eff_w))
        rl = getattr(self, "resize_listener", None)
        if rl is not None and hasattr(rl, "note_width_negotiated"):
            try:
                rl.note_width_negotiated()
            except Exception as e:
                if isinstance(e, UNO_DISPOSED_EXCEPTIONS):
                    log.debug("getHeightForWidth: resize listener likely disposed: %s", e)
        try:
            self.PanelWindow.setPosSize(0, 0, eff_w, current_h, 15)
            after = self.PanelWindow.getPosSize()
            log.debug("getHeightForWidth root_after=%sx%s" % (after.Width, after.Height))
        except Exception as e:
            if isinstance(e, UNO_DISPOSED_EXCEPTIONS):
                log.debug("getHeightForWidth: failed to set or get pos size (likely disposed): %s", e)

        if rl is not None:
            try:
                from plugin.chatbot.rich_text_control import log_rich_scroll

                rich = rl._c.get("response_rich") if hasattr(rl, "_c") else None
                log_rich_scroll("getHeightForWidth_before", control=rich, eff_w=eff_w)
                rl.relayout_now(self.PanelWindow)
                log_rich_scroll("getHeightForWidth_after", control=rich, eff_w=eff_w)
            except Exception as e:
                if isinstance(e, UNO_DISPOSED_EXCEPTIONS):
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
        self.rich_text_widget = None
        log.debug("[RICH-LIFECYCLE] ChatPanelElement.__init__ resource_url=%s parent_window=%s",
                  resource_url, id(parent_window) if parent_window else None)

    def _on_config_changed(self, **kwargs):
        """Event bus listener for config changes."""
        self._refresh_controls_from_config()

    def getRealInterface(self) -> XInterface:  # pyright: ignore[reportIncompatibleMethodOverride]
        log.debug("[RICH-LIFECYCLE] ChatPanelElement.getRealInterface called (toolpanel already exists=%s)", bool(self.toolpanel))
        if not self.toolpanel:
            try:
                # Ensure extension on path early so _wireControls imports work
                _initialize_extension_paths(self.ctx)
                root_window = self._getOrCreatePanelRootWindow()
                log.info("[RICH-LIFECYCLE] root_window created: %s", bool(root_window))
                self.toolpanel = ChatToolPanel(root_window, self.xParentWindow, self.ctx)
                wire_chatpanel_controls(self, root_window, HAS_RECORDING, _initialize_extension_paths)
                log.info("[RICH-LIFECYCLE] getRealInterface completed successfully (rich_text wiring done)")
            except Exception as e:
                log.error("getRealInterface ERROR [resource_url=%s]: %s", self.ResourceURL, e)
                log.error(traceback.format_exc())
                raise UnoObjectError("Failed to create ChatPanel UI element", details={"resource": self.ResourceURL}) from e
        return cast("XInterface", self.toolpanel)

    def _getOrCreatePanelRootWindow(self):
        log.debug("[RICH-LIFECYCLE] _getOrCreatePanelRootWindow entered (xParentWindow=%s)",
                  id(self.xParentWindow) if self.xParentWindow else None)
        base_url = get_extension_url()
        dialog_url = base_url + "/" + XDL_PATH
        log.debug("dialog_url: %s" % dialog_url)
        provider = self.ctx.getServiceManager().createInstanceWithContext("com.sun.star.awt.ContainerWindowProvider", self.ctx)
        log.info("[RICH-LIFECYCLE] calling createContainerWindow for rich-text sidebar...")
        self.m_panelRootWindow = provider.createContainerWindow(dialog_url, "", self.xParentWindow, None)
        log.info("[RICH-LIFECYCLE] createContainerWindow returned root_window=%s", bool(self.m_panelRootWindow))
        # Sidebar does not show the panel content without this (framework does not make it visible).
        if self.m_panelRootWindow and hasattr(self.m_panelRootWindow, "setVisible"):
            try:
                self.m_panelRootWindow.setVisible(True)
            except Exception as e:
                if isinstance(e, UNO_DISPOSED_EXCEPTIONS):
                    log.debug("Failed to set panel root window visible (likely disposed): %s", e)
        # Bug fix: on restored-wide startup, createContainerWindow can leave the root
        # at a stale frame-sized width before DeckLayouter calls getHeightForWidth.
        # Briefly cap that pre-negotiation size so sfx2 does not seed an H-scroll
        # range from the temporary root; getHeightForWidth expands to deck width.
        try:
            parent_rect = self.xParentWindow.getPosSize()
            current_rect = self.m_panelRootWindow.getPosSize()
            source_w = parent_rect.Width if parent_rect.Width > 0 else current_rect.Width
            target_w = min(source_w if source_w > 0 else self.toolpanel.getMinimalWidth() if self.toolpanel else 180, _PRE_NEGOTIATION_PANEL_WIDTH)
            target_h = current_rect.Height if current_rect.Height > 0 else (
                parent_rect.Height if parent_rect.Height > 0 else 400
            )
            if target_w > 0 and target_h > 0:
                self.m_panelRootWindow.setPosSize(0, 0, target_w, target_h, 15)
                log.debug("panel pre-negotiation constrained to W=%s H=%s" % (target_w, target_h))
        except Exception as e:
            if isinstance(e, UNO_DISPOSED_EXCEPTIONS):
                log.debug("Failed to constrain panel window (likely disposed): %s", e)
        return self.m_panelRootWindow

    def disposing(self, Source=None):
        """Best-effort lifecycle hook for sidebar resources (and future use).

        The LO sidebar framework does not automatically call this on XUIElement
        teardown for tool panels, but having it (and calling the SendButtonListener
        path) documents the intent and provides an explicit cleanup entry point.
        """
        log.info("[RICH-LIFECYCLE] ChatPanelElement.disposing called Source=%s has_send_listener=%s",
                 id(Source) if Source else None,
                 hasattr(self, "send_listener") and bool(self.send_listener))
        try:
            if hasattr(self, "send_listener") and self.send_listener:
                self.send_listener.disposing(None)
        except Exception as e:
            log.info("[RICH-SHUTDOWN]   send_listener.disposing raised from element: %s", e)

        # Clean up the always-present resize listener.
        # This listener is attached unconditionally in panel_wiring. Failing to
        # remove it during late VCL/sidebar teardown can contribute to crashes.
        try:
            tp = getattr(self, "toolpanel", None)
            rl = getattr(tp, "resize_listener", None) if tp else None
            root = getattr(self, "m_panelRootWindow", None)
            if rl and root and hasattr(root, "removeWindowListener"):
                root.removeWindowListener(rl)
            if tp:
                tp.resize_listener = None
        except Exception:
            pass

        self.rich_text_widget = None

    def _render_session_history(self, session, response_ctrl, model, greeting=""):
        """Update the response control with the contents of the given session."""
        try:
            if self.rich_text_widget:
                self.rich_text_widget.render_session_history(session, greeting)
                return

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
                    response_ctrl.setSelection(uno.createUnoStruct("com.sun.star.awt.Selection", length, length))
        except Exception as e:
            log.error("_render_session_history error [greeting=%s]: %s", greeting, e)

    def _refresh_controls_from_config(self):
        """Reload model and prompt selectors from config (e.g. after user changes Settings)."""
        root = self.m_panelRootWindow
        if not root or not hasattr(root, "getControl"):
            return
        from plugin.chatbot.config_ui_helpers import populate_combobox_with_lru, populate_image_model_selector

        def get_optional(name):
            return get_optional_control(root, name)

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
        chat_mode_selector = get_optional("chat_mode_selector")
        if chat_mode_selector:
            try:
                from plugin.chatbot.chat_sidebar_mode import populate_mode_selector, resolve_initial_mode, set_selector_mode

                model = self._get_document_model()
                include_brainstorming = self._sidebar_include_brainstorming(model)
                populate_mode_selector(chat_mode_selector, include_brainstorming=include_brainstorming)
                mode = resolve_initial_mode(self.ctx, include_brainstorming=include_brainstorming)
                set_selector_mode(chat_mode_selector, mode, include_brainstorming=include_brainstorming)
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
            from plugin.agent_backend.registry import AGENT_BACKEND_REGISTRY, normalize_backend_id

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

            chat_mode_selector = get_optional_control(root, "chat_mode_selector")
            if chat_mode_selector and hasattr(chat_mode_selector, "getModel"):
                set_control_enabled(chat_mode_selector, not is_external)

        except Exception as e:
            log.error("_update_backend_indicator error: %s" % e)

    def _get_document_model(self):
        """Helper to get the current document model strictly from the frame."""
        model = None
        if self.xFrame:
            try:
                model = self.xFrame.getController().getModel()
            except Exception as e:
                if isinstance(e, UNO_DISPOSED_EXCEPTIONS):
                    log.debug("Failed to get model from frame controller (likely disposed): %s", e)
        return model

    def _wire_model_selectors(self, model_selector, image_model_selector):
        """Initializes model selectors and their sync listeners."""
        from plugin.chatbot.config_ui_helpers import populate_combobox_with_lru, populate_image_model_selector

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
                def __init__(self, ctx):
                    self.ctx = ctx

                def on_item_state_changed(self, rEvent):
                    from plugin.chatbot.config_ui_helpers import update_lru_history

                    txt = model_selector.getText()
                    if not txt or txt == get_text_model(self.ctx):
                        return
                    set_config(self.ctx, "text_model", txt)
                    # Bug fix: sidebar only wrote text_model; Settings dialog debounced refresh calls populate_combobox_with_lru(..., "", ...) which falls back to to_show[0] from LRU — stale head reverted the UI. Mirror apply_settings_result (update model_lru).
                    update_lru_history(self.ctx, txt, "model_lru", get_current_endpoint(self.ctx))

            model_selector.addItemListener(ModelSyncListener(self.ctx))

        if image_model_selector and hasattr(image_model_selector, "addItemListener"):

            class ImageModelSyncListener(BaseItemListener):
                def __init__(self, ctx):
                    self.ctx = ctx

                def on_item_state_changed(self, rEvent):
                    txt = image_model_selector.getText()
                    if not txt:
                        return
                    prov = get_config(self.ctx, "image_provider")
                    sk = "aihorde_model" if prov == "aihorde" else "image_model"
                    if txt == str(get_config(self.ctx, sk) or "").strip():
                        return
                    set_image_model(self.ctx, txt, update_lru=False)

            image_model_selector.addItemListener(ImageModelSyncListener(self.ctx))

    def _sidebar_include_brainstorming(self, model) -> bool:
        return get_document_type(model) == DocumentType.WRITER

    def _greeting_for_sidebar_mode(self, mode, model):
        from plugin.chatbot.chat_sidebar_mode import CHAT_MODE_BRAINSTORMING, CHAT_MODE_WEB_RESEARCH

        if mode == CHAT_MODE_WEB_RESEARCH:
            return _(DEFAULT_RESEARCH_GREETING)
        if mode == CHAT_MODE_BRAINSTORMING:
            return _(DEFAULT_BRAINSTORMING_GREETING)
        return get_greeting_for_document(model)

    def _wire_chat_mode_ui(
        self,
        aspect_ratio_selector,
        base_size_input,
        base_size_label,
        chat_mode_selector,
        model_label,
        model_selector,
        image_model_selector,
        model,
    ):
        """Initializes sidebar mode dropdown and image-related controls; returns (initial_mode, include_brainstorming, toggle_image_ui)."""
        from plugin.chatbot.chat_sidebar_mode import is_image_mode, populate_mode_selector, resolve_initial_mode, set_selector_mode

        if aspect_ratio_selector:
            aspect_ratio_selector.addItems(("Square", "Landscape (16:9)", "Portrait (9:16)", "Landscape (3:2)", "Portrait (2:3)"), 0)
            aspect_ratio_selector.setText(get_config(self.ctx, "image_default_aspect") or "Square")

        if base_size_input:
            from plugin.chatbot.config_ui_helpers import populate_combobox_with_lru

            populate_combobox_with_lru(self.ctx, base_size_input, str(get_config(self.ctx, "image_base_size")), "image_base_size_lru", "")

        def update_base_size_label(aspect_str):

            if not base_size_label:
                return
            txt = _("Size:")
            if "Landscape" in aspect_str:
                txt = _("Height:")
            elif "Portrait" in aspect_str:
                txt = _("Width:")
            if hasattr(base_size_label, "setText"):
                base_size_label.setText(txt)
            elif hasattr(base_size_label.getModel(), "Label"):
                base_size_label.getModel().Label = txt

        if aspect_ratio_selector:
            update_base_size_label(aspect_ratio_selector.getText())
            if hasattr(aspect_ratio_selector, "addItemListener"):

                class AspectListener(BaseItemListener):
                    def on_item_state_changed(self, rEvent):
                        ev = rEvent
                        idx = getattr(ev, "Selected", -1)
                        if idx >= 0:
                            update_base_size_label(aspect_ratio_selector.getItem(idx))

                aspect_ratio_selector.addItemListener(AspectListener())

        # We now use the global set_control_enabled and set_control_visible from plugin.chatbot.dialogs

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
                    if isinstance(e, UNO_DISPOSED_EXCEPTIONS):
                        log.debug("Failed to relayout after toggling image UI (likely disposed): %s", e)

        include_brainstorming = self._sidebar_include_brainstorming(model)
        initial_mode = resolve_initial_mode(self.ctx, include_brainstorming=include_brainstorming)

        if chat_mode_selector:
            try:
                populate_mode_selector(chat_mode_selector, include_brainstorming=include_brainstorming)
                set_selector_mode(chat_mode_selector, initial_mode, include_brainstorming=include_brainstorming)
                toggle_image_ui(is_image_mode(initial_mode))
            except Exception as e:
                if isinstance(e, ConfigError):
                    log.error("chat_mode_selector ConfigError: %s" % e)
                else:
                    if isinstance(e, UNO_DISPOSED_EXCEPTIONS):
                        log.debug("chat_mode_selector wire error (likely disposed): %s", e)
                    else:
                        log.error("chat_mode_selector wire error: %s" % e)

        return initial_mode, include_brainstorming, toggle_image_ui

    def _apply_sidebar_mode(self, mode, model, response_ctrl, send_listener, clear_listener, toggle_image_ui):
        from plugin.chatbot.chat_sidebar_mode import CHAT_MODE_BRAINSTORMING, CHAT_MODE_WEB_RESEARCH, clear_brainstorming_session, is_image_mode

        if mode != CHAT_MODE_BRAINSTORMING and send_listener:
            clear_brainstorming_session(send_listener)
        if mode == CHAT_MODE_WEB_RESEARCH:
            self.session = self.web_session
        else:
            self.session = self.doc_session
        toggle_image_ui(is_image_mode(mode))
        greeting = self._greeting_for_sidebar_mode(mode, model)
        if send_listener:
            send_listener.set_session(self.session)
        if clear_listener:
            clear_listener.set_session(self.session, greeting=greeting)
        if response_ctrl:
            self._render_session_history(self.session, response_ctrl, model, greeting)
        return greeting

    def _wire_chat_mode_listener(self, chat_mode_selector, model, response_ctrl, send_listener, clear_listener, toggle_image_ui, include_brainstorming):
        from plugin.chatbot.chat_sidebar_mode import mode_from_selector, persist_mode_to_config

        if not chat_mode_selector or not hasattr(chat_mode_selector, "addItemListener"):
            return

        class ChatModeListener(BaseItemListener):
            def __init__(self, panel, ctx, selector, include_brainstorming_flag, apply_target):
                self.panel = panel
                self.ctx = ctx
                self.selector = selector
                self.include_brainstorming = include_brainstorming_flag
                self.apply_target = apply_target

            def on_item_state_changed(self, rEvent):
                mode = mode_from_selector(self.selector, include_brainstorming=self.include_brainstorming)
                persist_mode_to_config(self.ctx, mode)
                self.apply_target(mode)

        def apply_mode(mode):
            self._apply_sidebar_mode(mode, model, response_ctrl, send_listener, clear_listener, toggle_image_ui)

        chat_mode_selector.addItemListener(ChatModeListener(self, self.ctx, chat_mode_selector, include_brainstorming, apply_mode))
        return apply_mode

    def _setup_sessions(self, model, extra_instructions):
        """Creates the document and web research chat sessions."""
        # Deferred: importing panel.py at module load breaks unopkg (writeRegistryInfo) — heavy stack.
        from plugin.chatbot.panel import ChatSession

        # This resolves model logic internally
        system_prompt = get_chat_system_prompt_for_document(model, extra_instructions or "")

        session_id = get_document_property(model, "WriterAgentSessionID")
        if not session_id:
            if model and hasattr(model, "getURL"):
                url = model.getURL()
                if url:
                    session_id = hashlib.sha256(url.encode("utf-8")).hexdigest()
            if not session_id:
                session_id = str(uuid.uuid4())
            if model:
                set_document_property(model, "WriterAgentSessionID", session_id)

        self.doc_session = ChatSession(system_prompt, session_id=session_id)
        self.web_session = ChatSession("Observe: Always use the web_search tool to answer questions.", session_id=session_id + "_web")
        self.session = self.doc_session

    def _wire_buttons(self, controls, model, initial_mode, include_brainstorming, toggle_image_ui):
        """Wires up the Send, Stop, Clear, and chat mode selector."""
        from plugin.chatbot.panel import ClearButtonListener, SendButtonListener, StopButtonListener

        send_listener = None
        try:
            send_listener = SendButtonListener(
                self.ctx,
                self.xFrame,
                controls["send"],
                controls["stop"],
                controls["query"],
                controls["response"],
                controls["image_model_selector"],
                controls["model_selector"],
                controls["status"],
                self.session,
                chat_mode_selector=controls["chat_mode_selector"],
                aspect_ratio_selector=controls["aspect_ratio_selector"],
                base_size_input=controls["base_size_input"],
                sidebar_include_brainstorming=include_brainstorming,
                ensure_path_fn=_initialize_extension_paths,
                clear_control=controls.get("clear"),
            )

            # Save it to the instance so panel_wiring can use it for QueryTextListener
            self.send_listener = send_listener



            doc_type = get_document_type(model)
            if doc_type == DocumentType.CALC:
                send_listener.initial_doc_type = "Calc"
            elif doc_type in (DocumentType.DRAW, DocumentType.IMPRESS):
                send_listener.initial_doc_type = "Draw"
            elif doc_type == DocumentType.WRITER:
                send_listener.initial_doc_type = "Writer"
            else:
                send_listener.initial_doc_type = "Unknown"

            if controls["send"]:
                controls["send"].addActionListener(send_listener)
            start_watchdog_thread(self.ctx, controls["status"])

            if controls["stop"]:
                controls["stop"].addActionListener(StopButtonListener(send_listener))
            send_listener._set_button_states(send_enabled=True, stop_enabled=False)
        except Exception as e:
            log.error("Send/Stop button wiring error: %s", e)

        clear_listener = None
        active_greeting = self._greeting_for_sidebar_mode(initial_mode, model)
        if controls["clear"]:
            try:
                clear_listener = ClearButtonListener(self.session, controls["response"], controls["status"], greeting=active_greeting, send_listener=send_listener)
                controls["clear"].addActionListener(clear_listener)
            except Exception as e:
                log.exception("Clear button wiring error: %s", e)

        self._apply_sidebar_mode(initial_mode, model, controls["response"], send_listener, clear_listener, toggle_image_ui)
        self._wire_chat_mode_listener(
            controls["chat_mode_selector"],
            model,
            controls["response"],
            send_listener,
            clear_listener,
            toggle_image_ui,
            include_brainstorming,
        )


class ChatPanelFactory(unohelper.Base, XUIElementFactory):
    """Factory that creates ChatPanelElement instances for the sidebar."""

    def __init__(self, ctx):
        self.ctx = ctx

    # Called externally by LibreOffice UNO framework; do not remove.
    def createUIElement(self, ResourceURL, Args):
        resource_url = ResourceURL
        args = Args
        log.debug("createUIElement: %s" % resource_url)
        if "ChatPanel" not in resource_url:
            raise NoSuchElementException("Unknown resource: " + resource_url)
        frame = _get_arg(args, "Frame")
        parent_window = _get_arg(args, "ParentWindow")
        log.debug("ParentWindow: %s" % (parent_window is not None))
        if not parent_window:
            raise IllegalArgumentException("ParentWindow is required")

        return ChatPanelElement(self.ctx, frame, parent_window, resource_url)


g_ImplementationHelper = unohelper.ImplementationHelper()
g_ImplementationHelper.addImplementation(ChatPanelFactory, "org.extension.writeragent.ChatPanelFactory", ("com.sun.star.ui.UIElementFactory",))
