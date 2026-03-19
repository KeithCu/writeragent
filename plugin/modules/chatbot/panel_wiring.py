import weakref
import logging

from plugin.framework.dialogs import (
    get_optional as get_optional_control,
    get_checkbox_state,
)
from plugin.modules.chatbot.panel_resize import _PanelResizeListener

log = logging.getLogger(__name__)


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
                current = controls["response"].getModel().Text or ""
                controls["response"].getModel().Text = current + "[Init error: %s]\n" % msg
        except Exception:
            pass

    ensure_extension_on_path(self.ctx)

    # 1. Config, Models, and UI
    try:
        from plugin.framework.config import get_config
        extra_instructions = get_config(self.ctx, "additional_instructions")
        
        self._wire_model_selectors(controls["model_selector"], controls["image_model_selector"])
        
        set_control_enabled = self._wire_image_ui(
            controls["aspect_ratio_selector"], controls["base_size_input"], controls["base_size_label"],
            controls["direct_image_check"], controls["web_research_check"], controls["model_label"], 
            controls["model_selector"], controls["image_model_selector"]
        )
    except Exception as e:
        import traceback
        _show_init_error("Config: %s" % e)
        log.error(traceback.format_exc())
        extra_instructions = ""
        set_control_enabled = lambda ctrl, en: None

    # 2. Setup Sessions
    model = self._get_document_model()
    self._setup_sessions(model, extra_instructions)

    # 3. Determine Mode & Greeting
    from plugin.framework.constants import get_greeting_for_document, DEFAULT_RESEARCH_GREETING
    web_checked = False
    if controls["web_research_check"]:
        try: web_checked = (get_checkbox_state(controls["web_research_check"]) == 1)
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
                controls["send"].getModel().Label = "Record" if has_recording else "Send"
        except Exception as e:
            log.error("QueryTextListener setup error: %s" % e)

    if controls["status"] and hasattr(controls["status"], "setText"):
        try: controls["status"].setText("Ready")
        except Exception: pass

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
        _resize = _PanelResizeListener(controls)
        root_window.addWindowListener(_resize)
        _resize._relayout(root_window)
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