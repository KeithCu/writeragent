import weakref
import logging

from plugin.framework.dialogs import (
    get_optional as get_optional_control,
    get_checkbox_state,
    set_control_enabled,
    get_control_text,
    set_control_text
)
from plugin.modules.chatbot.panel_resize import _PanelResizeListener

log = logging.getLogger(__name__)


def _measure_send_button_max_width(send_ctrl, has_recording):
    """Max pixel width for Send/Record/Stop Rec so label toggles do not resize the row."""
    if not send_ctrl or not hasattr(send_ctrl, "getModel"):
        return None
    try:
        m = send_ctrl.getModel()
        saved = m.Label
        labels = ["Send", "Record", "Stop Rec"] if has_recording else ["Send"]
        wmax = send_ctrl.getPosSize().Width
        for lab in labels:
            m.Label = lab
            wmax = max(wmax, send_ctrl.getPosSize().Width)
        m.Label = saved
        return wmax if wmax > 0 else None
    except Exception:
        return None


def _wireControls(self, root_window, has_recording, ensure_extension_on_path):
    """Main entry point to wire all controls for the panel."""
    log.debug("_wireControls entered")
    if not hasattr(root_window, "getControl"):
        log.error("_wireControls: root_window has no getControl, aborting")
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
        "web_research_check": get_optional("web_research_check"),
        "aspect_ratio_selector": get_optional("aspect_ratio_selector"),
        "base_size_input": get_optional("base_size_input"),
        "base_size_label": get_optional("base_size_label"),
        "response_label": get_optional("response_label"),
        "query_label": get_optional("query_label"),
        "backend_indicator": get_optional("backend_indicator"),
    }

    # Helper to show errors visibly in the response area
    def _show_init_error(msg):
        log.error("_wireControls ERROR: %s" % msg)
        try:
            if controls["response"] and controls["response"].getModel():
                current = get_control_text(controls["response"]) or ""
                set_control_text(controls["response"], current + "[Init error: %s]\n" % msg)
        except Exception:
            pass

    ensure_extension_on_path(self.ctx)

    # 1. Config, Models, and UI
    try:
        from plugin.framework.config import get_config
        extra_instructions = get_config(self.ctx, "additional_instructions")
        
        self._wire_model_selectors(controls["model_selector"], controls["image_model_selector"])
        
        self._wire_image_ui(
            controls["aspect_ratio_selector"], controls["base_size_input"], controls["base_size_label"],
            controls["direct_image_check"], controls["web_research_check"], controls["model_label"], 
            controls["model_selector"], controls["image_model_selector"]
        )
    except Exception as e:
        import traceback
        _show_init_error("Config: %s" % e)
        log.error(traceback.format_exc())
        extra_instructions = ""

    # 2. Setup Sessions
    model = self._get_document_model()
    self._setup_sessions(model, extra_instructions)

    # 3. Determine Mode & Greeting
    from plugin.framework.constants import get_greeting_for_document, DEFAULT_RESEARCH_GREETING
    web_checked = False
    if controls["web_research_check"]:
        try: web_checked = (get_checkbox_state(controls["web_research_check"]) == 1)
        except Exception as e: log.exception("Error reading web_research_check state: %s", e)
        
    if web_checked:
        self.session = self.web_session
        active_greeting = DEFAULT_RESEARCH_GREETING
    else:
        self.session = self.doc_session
        active_greeting = get_greeting_for_document(model)

    self._render_session_history(self.session, controls["response"], model, active_greeting)

    # 4. Buttons
    self._wire_buttons(controls, model, active_greeting)

    # Wire query listener to update Record/Send button label (fixed width applied after relayout)
    query_text_listener = None
    if controls.get("query") and controls.get("send"):
        try:
            from plugin.modules.chatbot.panel import QueryTextListener
            # Pass the send_listener stored on self from _wire_buttons instead of the send control.
            # _wire_buttons runs before this in _wireControls, so self.send_listener is available.
            if hasattr(self, "send_listener") and self.send_listener:
                query_text_listener = QueryTextListener(self.send_listener)
                controls["query"].addTextListener(query_text_listener)

                # The label update logic is now handled correctly by the state machine
                # so we can just trigger a fake text update event to sync the state
                has_text = bool(get_control_text(controls["query"]).strip())
                from plugin.modules.chatbot.send_state import SendEvent, SendEventKind
                self.send_listener.dispatch(SendEvent(SendEventKind.TEXT_UPDATED, {"has_text": has_text}))
            else:
                log.warning("No send_listener available for QueryTextListener setup")
        except Exception as e:
            log.error("QueryTextListener setup error: %s" % e)

    if controls["status"] and hasattr(controls["status"], "setText"):
        try: controls["status"].setText("Ready")
        except Exception as e: log.exception("Error setting status text: %s", e)

    # 5. Timer and Resize
    try:
        from main import try_ensure_mcp_timer
        try_ensure_mcp_timer(self.ctx)
    except Exception as e:
        log.error("try_ensure_mcp_timer: %s" % e)

    try:
        log.debug(
            "Attaching _PanelResizeListener to root_window; controls=%s"
            % (sorted(k for k, v in controls.items() if v))
        )
        _parent = None
        _tp = getattr(self, "toolpanel", None)
        if _tp is not None:
            _parent = _tp.parent_window
        _deck_getter = None
        if _tp is not None:
            _deck_getter = lambda: getattr(_tp, "_last_deck_w", None)
        _resize = _PanelResizeListener(
            controls, parent_window=_parent, deck_w_getter=_deck_getter
        )
        root_window.addWindowListener(_resize)
        if _tp is not None:
            _tp.resize_listener = _resize
        _resize.relayout_now(root_window)
        # Stop +22px/windowResized loop when Record <-> Send (see writeragent_debug.log).
        if controls["send"] and query_text_listener is not None:
            try:
                fw = _measure_send_button_max_width(controls["send"], has_recording)
                if fw:
                    if hasattr(self, "send_listener"):
                        self.send_listener.set_fixed_send_width(fw)
                    sr = controls["send"].getPosSize()
                    controls["send"].setPosSize(sr.X, sr.Y, fw, sr.Height, 15)
            except Exception as e:
                log.debug("send button width stabilize skipped: %s" % e)
    except Exception as e:
        log.error("Resize listener error: %s" % e)

    # Backend indicator (Aider / Hermes when external agent enabled)
    self._update_backend_indicator(root_window)

    # 6. Global Config Listener
    from plugin.framework.config import add_config_listener
    _self_ref = weakref.ref(self)
    def on_config_changed(ctx):
        panel = _self_ref()
        if panel is not None:
            panel._refresh_controls_from_config()
    add_config_listener(on_config_changed)