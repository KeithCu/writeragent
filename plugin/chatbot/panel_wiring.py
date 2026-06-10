import logging
import traceback

from com.sun.star.lang import DisposedException
from com.sun.star.uno import RuntimeException, Exception as UnoException

# Common exceptions for UI components that may be disposed during layout/refresh
UNO_DISPOSED_EXCEPTIONS = (DisposedException, RuntimeException, UnoException)

from plugin.chatbot.dialogs import get_optional as get_optional_control, get_control_text, set_control_text, translate_dialog
from plugin.chatbot.panel_resize import _PanelResizeListener
from plugin.framework.config import get_config
from plugin.framework.i18n import _
from plugin.framework.event_bus import global_event_bus
from plugin.framework.logging import init_logging

log = logging.getLogger(__name__)


def _measure_send_button_max_width(send_ctrl, has_recording):
    """Max pixel width for Send/Record/Stop Rec so label toggles do not resize the row."""
    if not send_ctrl or not hasattr(send_ctrl, "getModel"):
        return None
    try:
        m = send_ctrl.getModel()
        saved = m.Label
        labels = ["Send", "Record", "Stop Rec", "Accept"] if has_recording else ["Send", "Accept"]
        wmax = send_ctrl.getPosSize().Width
        for lab in labels:
            m.Label = lab
            wmax = max(wmax, send_ctrl.getPosSize().Width)
        m.Label = saved
        return wmax if wmax > 0 else None
    except Exception as e:
        if isinstance(e, UNO_DISPOSED_EXCEPTIONS):
            log.debug("Failed to measure send button width (likely disposed): %s", e)
        else:
            log.debug("Unexpected error measuring send button width: %s", e)
        return None


def _measure_aux_button_max_width(ctrl, labels):
    """Stabilize width when a button's label toggles (e.g. Stop/Change, Clear/Reject)."""
    if not ctrl or not hasattr(ctrl, "getModel") or not labels:
        return None
    try:
        m = ctrl.getModel()
        saved = m.Label
        wmax = ctrl.getPosSize().Width
        for lab in labels:
            m.Label = lab
            wmax = max(wmax, ctrl.getPosSize().Width)
        m.Label = saved
        return wmax if wmax > 0 else None
    except Exception as e:
        if isinstance(e, UNO_DISPOSED_EXCEPTIONS):
            log.debug("Failed to measure aux button width '%s' (likely disposed): %s", ctrl.getName() if hasattr(ctrl, "getName") else "?", e)
        else:
            log.debug("Unexpected error measuring aux button width: %s", e)
        return None


def _wireControls(self, root_window, has_recording, ensure_extension_on_path):
    """Main entry point to wire all controls for the panel."""
    log.debug("_wireControls entered")
    if not hasattr(root_window, "getControl"):
        log.error("_wireControls: root_window has no getControl, aborting")
        return

    def get_optional(name):
        return get_optional_control(root_window, name)

    translate_dialog(root_window)

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
        "chat_mode_selector": get_optional("chat_mode_selector"),
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
        except Exception as e:
            if isinstance(e, UNO_DISPOSED_EXCEPTIONS):
                log.debug("Failed to show init error on response control (likely disposed): %s", e)
            else:
                log.error("Unexpected error displaying init error: %s", e)

    ensure_extension_on_path(self.ctx)

    extra_instructions = ""
    model = self._get_document_model()
    initial_mode = "chat"
    include_brainstorming = False

    def toggle_image_ui(_is_image):
        return None

    # 1. Config, Models, and UI
    try:
        extra_instructions = get_config(self.ctx, "additional_instructions")

        self._wire_model_selectors(controls["model_selector"], controls["image_model_selector"])

        initial_mode, include_brainstorming, toggle_image_ui = self._wire_chat_mode_ui(
            controls["aspect_ratio_selector"],
            controls["base_size_input"],
            controls["base_size_label"],
            controls["chat_mode_selector"],
            controls["model_label"],
            controls["model_selector"],
            controls["image_model_selector"],
            model,
        )
    except Exception as e:
        _show_init_error("Config: %s" % e)
        log.error(traceback.format_exc())

    # 2. Setup Sessions
    self._setup_sessions(model, extra_instructions)

    # 3. Buttons (applies initial mode, session history, and mode listener)
    self._wire_buttons(controls, model, initial_mode, include_brainstorming, toggle_image_ui)

    # Wire query listener to update Record/Send button label (fixed width captured in snapshot before relayout)
    query_text_listener = None
    if controls.get("query") and controls.get("send"):
        try:
            from plugin.chatbot.panel import QueryTextListener

            # Pass the send_listener stored on self from _wire_buttons instead of the send control.
            # _wire_buttons runs before this in _wireControls, so self.send_listener is available.
            if hasattr(self, "send_listener") and self.send_listener:
                query_text_listener = QueryTextListener(self.send_listener)
                controls["query"].addTextListener(query_text_listener)

                from plugin.chatbot.panel import QueryKeyListener

                controls["query"].addKeyListener(QueryKeyListener(self.send_listener))

                # The label update logic is now handled correctly by the state machine
                # so we can just trigger a fake text update event to sync the state
                has_text = bool(get_control_text(controls["query"]).strip())
                from plugin.chatbot.send_state import SendEvent, SendEventKind

                self.send_listener.dispatch(SendEvent(SendEventKind.TEXT_UPDATED, {"has_text": has_text}))
            else:
                log.warning("No send_listener available for QueryTextListener setup")
        except Exception as e:
            log.error("QueryTextListener setup error: %s" % e)

    if controls["status"] and hasattr(controls["status"], "setText"):
        try:
            controls["status"].setText(_("Ready"))
        except Exception as e:
            if isinstance(e, UNO_DISPOSED_EXCEPTIONS):
                log.debug("Failed to set status text on init (likely disposed): %s", e)
            else:
                log.exception("Error setting status text: %s", e)

    # Stop +22px/windowResized loop when Record <-> Send (see writeragent_debug.log).
    # Measure before first relayout so snapshot preserves stabilized button widths.
    if controls["send"]:
        try:
            fw = _measure_send_button_max_width(controls["send"], has_recording)
            if fw:
                if hasattr(self, "send_listener"):
                    self.send_listener.set_fixed_send_width(fw)
                sr = controls["send"].getPosSize()
                controls["send"].setPosSize(sr.X, sr.Y, fw, sr.Height, 15)
        except Exception as e:
            if isinstance(e, UNO_DISPOSED_EXCEPTIONS):
                log.debug("send button width stabilize skipped (likely disposed): %s", e)
            else:
                log.debug("send button width stabilize skipped: %s", e)
        try:
            for c, lab_list in ((controls.get("stop"), ["Stop", "Change", "Reject"]), (controls.get("clear"), ["Clear", "Reject"])):
                if not c:
                    continue
                aw = _measure_aux_button_max_width(c, lab_list)
                if aw:
                    r = c.getPosSize()
                    c.setPosSize(r.X, r.Y, aw, r.Height, 15)
        except Exception as e:
            if isinstance(e, UNO_DISPOSED_EXCEPTIONS):
                log.debug("stop/clear button width stabilize skipped (likely disposed): %s", e)
            else:
                log.debug("stop/clear button width stabilize skipped: %s", e)

    try:
        log.debug("Attaching _PanelResizeListener to root_window; controls=%s" % (sorted(k for k, v in controls.items() if v)))
        _tp = getattr(self, "toolpanel", None)
        _resize = _PanelResizeListener(controls)
        _resize._root_window = root_window  # for defensive self-removal in disposing()
        root_window.addWindowListener(_resize)
        self._panel_resize_listener = _resize
        if _tp is not None:
            _tp.resize_listener = _resize
        _resize.relayout_now(root_window)

        # One-time marker for the very first layout after the panel is created.
        # Extremely useful for diagnosing the "starts wide on restart → scrollbar" case.
        log.info("[FIRST LAYOUT] root_w=%d (this is the initial size on app start / sidebar show)",
                 root_window.getPosSize().Width)

        # Lightweight layout sanity log (always emitted at DEBUG after first wiring).
        # Helps catch cases where child controls would overflow the allocated panel width.
        try:
            rw = root_window.getPosSize().Width
            max_right = 0
            for nm in ("clear", "send", "stop", "model_selector", "query", "response"):
                c = controls.get(nm)
                if c:
                    try:
                        pr = c.getPosSize()
                        max_right = max(max_right, pr.X + pr.Width)
                    except Exception:
                        pass
            log.debug("layout_sanity: root_w=%d max_child_right=%d overflow=%s" % (rw, max_right, "YES" if max_right > rw - 2 else "no"))
        except Exception:
            pass
    except Exception as e:
        log.error("Resize listener error: %s" % e)

    # Backend indicator (Aider / Hermes when external agent enabled)
    self._update_backend_indicator(root_window)

    # 6. Global Config Listener
    global_event_bus.subscribe("config:changed", self._on_config_changed, weak=True)

    # Weekly extension update check: run once per process, late (after sidebar UI is wired)
    # so logging is initialized and AsyncCallback/msgbox are reliable.
    try:
        init_logging(self.ctx)
        from plugin.main import _schedule_extension_update_check_once

        _schedule_extension_update_check_once(self.ctx)
    except Exception as e:
        log.warning("extension update check schedule failed: %s", e)

    try:
        from plugin.doc.embeddings_periodic import schedule_periodic_embeddings_indexer_once

        schedule_periodic_embeddings_indexer_once(self.ctx)
    except Exception as e:
        log.warning("embeddings periodic indexer schedule failed: %s", e)

    # 7. Rich Text Control Sidebar (RichTextControl; embedded Writer path removed)
    from plugin.framework.config import get_config_bool_safe

    rich_sidebar_enabled = get_config_bool_safe(self.ctx, "rich_text_control_sidebar")
    log.info("[RICH-CONTROL] config rich_text_control_sidebar=%s", rich_sidebar_enabled)
    if rich_sidebar_enabled:
        try:
            from plugin.chatbot.rich_text_control import RichTextChatWidget, RichTextControlListener, log_rich_control_context, log_rich_scroll

            def on_rich_control_ready(rich_control):
                log.info("[RICH-CONTROL] on_rich_control_ready control=%s", bool(rich_control))
                widget = RichTextChatWidget(self.ctx, rich_control, style_window=root_window)
                self.rich_text_widget = widget
                controls["response_rich"] = rich_control
                if hasattr(self, "_panel_resize_listener") and self._panel_resize_listener:
                    self._panel_resize_listener._c["response_rich"] = rich_control
                if hasattr(self, "send_listener") and self.send_listener:
                    self.send_listener.set_rich_text_widget(widget)
                try:
                    from plugin.calc.navigation import attach_calc_cell_link_listener

                    def _calc_doc_from_panel():
                        sl = getattr(self, "send_listener", None)
                        if sl is None or not hasattr(sl, "_get_document_model"):
                            return None
                        model = sl._get_document_model()
                        if model is not None and hasattr(model, "getSheets"):
                            return model
                        return None

                    attach_calc_cell_link_listener(self.ctx, rich_control, _calc_doc_from_panel)
                except Exception as e:
                    log.debug("calc cell link listener attach failed: %s", e)
                hide_plain_ok = True
                hide_response = False
                hide_label = False
                try:
                    from plugin.chatbot.dialogs import set_control_visible

                    if controls.get("response"):
                        set_control_visible(controls["response"], False)
                        hide_response = True
                    if controls.get("response_label"):
                        set_control_visible(controls["response_label"], False)
                        hide_label = True
                except Exception as e:
                    hide_plain_ok = False
                    log.warning("[RICH-CONTROL] phase=hide_plain ok=0 error=%s", e)
                log_rich_control_context(
                    self.ctx,
                    "hide_plain",
                    ok=int(hide_plain_ok),
                    response=int(hide_response),
                    response_label=int(hide_label),
                )
                if hasattr(self, "_panel_resize_listener") and self._panel_resize_listener:
                    try:
                        log_rich_scroll("on_ready_step", control=rich_control, step="relayout")
                        self._panel_resize_listener.relayout_now(root_window)
                    except Exception as e:
                        log.debug("on_rich_control_ready relayout_now: %s", e)
                try:
                    log_rich_scroll("on_ready_step", control=rich_control, step="history")
                    rich_greeting = self._greeting_for_sidebar_mode(initial_mode, model)
                    self._render_session_history(self.session, controls["response"], model, rich_greeting)
                except Exception as e:
                    log.error("Initial RichTextControl render failed: %s", e)
                if hasattr(self, "_rich_control_listener") and self._rich_control_listener:
                    try:
                        log_rich_scroll("on_ready_step", control=rich_control, step="sync_bounds")
                        self._rich_control_listener._sync_bounds()
                    except Exception as e:
                        log.debug("on_rich_control_ready sync bounds: %s", e)
                try:
                    log_rich_scroll("on_ready_step", control=rich_control, step="nudge")
                    widget.nudge_view_to_end(reason="on_ready")
                except Exception as e:
                    log.debug("on_rich_control_ready nudge scroll: %s", e)

            rich_control_listener = RichTextControlListener(
                self.ctx,
                root_window,
                controls["response"],
                on_rich_control_ready,
                placeholder_rect_fn=lambda: (
                    self._panel_resize_listener.last_response_rect
                    if getattr(self, "_panel_resize_listener", None)
                    else None
                ),
            )
            self._rich_control_listener = rich_control_listener
            root_window.addWindowListener(rich_control_listener)
            log.info("[RICH-CONTROL] RichTextControlListener attached to root_window")
            log_rich_control_context(self.ctx, "listener_attached", peer=int(bool(rich_control_listener._root_peer())))
            # GNOME sidebar deck: peer is often ready here but windowShown never fires — init
            # must not wait on the listener alone (KDE usually gets windowShown instead).
            rich_control_listener.try_eager_init()
        except Exception as e:
            log.error("RichTextControl sidebar initialization failed: %s", e)
