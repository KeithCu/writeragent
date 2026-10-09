from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

from plugin.chatbot.dialogs import get_optional as get_optional_control, get_control_text, set_control_text, set_control_visible, translate_dialog
from plugin.chatbot.panel_resize import _PanelResizeListener
from plugin.framework.config import get_config
from plugin.framework.errors import suppress_disposed
from plugin.framework.i18n import _
from plugin.framework.event_bus import global_event_bus
from plugin.framework.logging import init_logging

log = logging.getLogger(__name__)


def _measure_send_button_max_width(send_ctrl: Any, has_recording: bool) -> int | None:
    """Max pixel width for Send/Record/Stop Rec so label toggles do not resize the row."""
    if not send_ctrl or not hasattr(send_ctrl, "getModel"):
        return None
    with suppress_disposed("measure send button width", logger=log):
        m = send_ctrl.getModel()
        saved = m.Label
        try:
            labels = ["Send", "Record", "Stop Rec", "Accept"] if has_recording else ["Send", "Accept"]
            wmax = send_ctrl.getPosSize().Width
            for lab in labels:
                # Live labels are _(effect.send_label). Measuring the English
                # string let a longer translation change width before the pin.
                m.Label = _(lab)
                wmax = max(wmax, send_ctrl.getPosSize().Width)
            return wmax if wmax > 0 else None
        finally:
            # Restore the label here. A failed width read otherwise leaves
            # the last candidate on the button, and suppress_disposed swallows
            # the error before a restore after the loop.
            m.Label = saved
    return None


def _measure_aux_button_max_width(ctrl: Any, labels: list[str]) -> int | None:
    """Stabilize width when a button's label toggles (e.g. Stop/Change, Clear/Reject)."""
    if not ctrl or not hasattr(ctrl, "getModel") or not labels:
        return None
    with suppress_disposed("measure aux button width", logger=log):
        m = ctrl.getModel()
        saved = m.Label
        try:
            wmax = ctrl.getPosSize().Width
            for lab in labels:
                m.Label = _(lab)
                wmax = max(wmax, ctrl.getPosSize().Width)
            return wmax if wmax > 0 else None
        finally:
            # Same as send-width measurement: restore Label even when the
            # width read raises and suppress_disposed swallows it.
            m.Label = saved
    return None


def _install_frame_session_listeners(
    session: Any,
    ctx: Any,
    query: Any,
    leave_query_controls: Any,
) -> None:
    """Pin Ask and attach this frame's focus and click listeners.

    Re-raise a ``UNO thread violation`` ``RuntimeError``. ``install``
    raises that from ``getController``, ``addFocusListener``,
    ``addMouseListener``, and ``addMouseClickHandler`` so a missed attach
    is not a successful return. Catching it here keeps the contract inside
    the sidebar. Any other attach failure stays a debug log.
    """
    try:
        session.set_focus_pin(query)
        session.install(ctx, query=query, leave_query_controls=leave_query_controls)
    except Exception as exc:
        from plugin.framework.uno_listeners import listener_boundary

        boundary = listener_boundary(exc)
        if boundary is not None and boundary.kind == "thread":
            raise
        log.debug("frame session focus install: %s", exc)


def make_toggle_image_ui(panel: Any, controls: dict[str, Any]) -> Callable[[bool], None]:
    """Toggle visibility of text vs image model controls and relayout."""

    def toggle_image_ui(is_image: bool) -> None:
        set_control_visible(controls.get("model_label"), not is_image)
        set_control_visible(controls.get("model_selector"), not is_image)
        set_control_visible(controls.get("image_model_selector"), is_image)
        set_control_visible(controls.get("aspect_ratio_selector"), is_image)
        set_control_visible(controls.get("base_size_input"), is_image)
        set_control_visible(controls.get("base_size_label"), is_image)
        tp = getattr(panel, "toolpanel", None)
        root = getattr(panel, "m_panelRootWindow", None)
        rl = getattr(tp, "resize_listener", None) if tp else None
        if rl and root:
            with suppress_disposed("relayout after toggling image UI", logger=log):
                rl.relayout_now(root)

    return toggle_image_ui


def _wireControls(self: Any, root_window: Any, has_recording: bool, ensure_extension_on_path: Any) -> None:  # pyright: ignore[reportUnusedFunction]  # imported as wire_chatpanel_controls by panel_factory
    """Main entry point to wire all controls for the panel."""
    log.debug("_wireControls entered")
    if not hasattr(root_window, "getControl"):
        log.error("_wireControls: root_window has no getControl, aborting")
        return

    def get_optional(name: str) -> Any:
        return get_optional_control(root_window, name)

    translate_dialog(root_window)

    controls = {
        "send": root_window.getControl("send"),
        "query": root_window.getControl("query"),
        "response": root_window.getControl("response"),
        "stop": get_optional("stop"),
        "clear": get_optional("clear"),
        "chk_voice": get_optional("chk_voice"),
        "image_model_selector": get_optional("image_model_selector"),
        "prompt_selector": get_optional("prompt_selector"),
        "model_selector": get_optional("model_selector"),
        "model_label": get_optional("model_label"),
        "status": get_optional("status"),
        "chat_mode_selector": get_optional("chat_mode_selector"),
        "aspect_ratio_selector": get_optional("aspect_ratio_selector"),
        "base_size_input": get_optional("base_size_input"),
        "base_size_label": get_optional("base_size_label"),
        "btn_settings": get_optional("btn_settings"),
        "btn_python": get_optional("btn_python"),
        "btn_latex": get_optional("btn_latex"),
        "btn_search": get_optional("btn_search"),
        "btn_hamburger": get_optional("btn_hamburger"),
        "response_label": get_optional("response_label"),
        "query_label": get_optional("query_label"),
        "backend_indicator": get_optional("backend_indicator"),
        "slash_popup": get_optional("slash_popup"),
    }

    # Overlay completion menu — hide until `/` is typed (not a permanent widget).
    if controls.get("slash_popup"):
        set_control_visible(controls["slash_popup"], False)

    # Helper to show errors visibly in the response area
    def _show_init_error(msg: str) -> None:
        log.error("_wireControls ERROR: %s" % msg)
        with suppress_disposed("show init error on response control", logger=log, exc_info=True):
            if controls["response"] and controls["response"].getModel():
                current = get_control_text(controls["response"]) or ""
                set_control_text(controls["response"], current + "[Init error: %s]\n" % msg)

    ensure_extension_on_path(self.ctx)

    extra_instructions = ""
    model = self._get_document_model()
    initial_mode = "chat"
    # A real flags object lets Send/Stop attach even when the mode
    # dropdown fails to wire. Leaving mode_flags as None makes
    # include_brainstorming raise inside the Send/Stop try, and that
    # except skips addActionListener.
    from plugin.chatbot.chat_sidebar_mode import SidebarModeFlags

    mode_flags: SidebarModeFlags = SidebarModeFlags()

    toggle_image_ui = make_toggle_image_ui(self, controls)

    # 1. Config, Models, and UI
    try:
        extra_instructions = get_config("additional_instructions")
    except Exception as e:
        _show_init_error("Config: %s" % e)
        log.exception("Config instructions read failed")

    try:
        self._wire_model_selectors(controls["model_selector"], controls["image_model_selector"])
    except Exception as e:
        # Model selector wiring is its own try so a failure here still
        # wires the chat mode UI.
        _show_init_error("Model selectors: %s" % e)
        log.exception("Model selectors wiring failed")

    try:
        initial_mode, mode_flags, toggle_image_ui = self._wire_chat_mode_ui(
            controls["aspect_ratio_selector"],
            controls["base_size_input"],
            controls["base_size_label"],
            controls["chat_mode_selector"],
            controls["model_label"],
            controls["model_selector"],
            controls["image_model_selector"],
            model,
            toggle_image_ui=toggle_image_ui,
        )
    except Exception as e:
        _show_init_error("Mode UI: %s" % e)
        log.exception("Chat mode UI wiring failed")

    # 2. Setup Sessions
    self._setup_sessions(model, extra_instructions)

    # 3. Buttons (applies initial mode, session history, and mode listener)
    self._wire_buttons(controls, model, initial_mode, mode_flags, toggle_image_ui)

    # Wire query listener to update Record/Send button label (fixed width captured in snapshot before relayout)
    query_text_listener = None
    if controls.get("query") and controls.get("send"):
        try:
            from plugin.chatbot.panel import QueryTextListener

            # Pass the send_listener stored on self from _wire_buttons instead of the send control.
            # _wire_buttons runs before this in _wireControls, so self.send_listener is available.
            if hasattr(self, "send_listener") and self.send_listener:
                query_ctrl = controls["query"]
                if query_ctrl is None:
                    log.warning("Query control missing; cannot attach text/key listeners")
                else:
                    query_text_listener = QueryTextListener(self.send_listener)
                    query_ctrl.addTextListener(query_text_listener)

                    from plugin.chatbot.panel import QueryKeyListener

                    query_ctrl.addKeyListener(QueryKeyListener(self.send_listener))

                    slash_ctrl = controls.get("slash_popup")
                    if slash_ctrl is not None:
                        from plugin.chatbot.slash_popup import SlashPopupController

                        self.send_listener.slash_popup = SlashPopupController(
                            slash_ctrl,
                            self.send_listener,
                            query_ctrl,
                            overlay_parent=root_window,
                        )
                        log.info("slash popup attached to Ask field")

                    # The label update logic is now handled correctly by the state machine
                    # so we can just trigger a fake text update event to sync the state
                    has_text = bool(get_control_text(query_ctrl).strip())
                    from plugin.chatbot.send_state import SendEvent, SendEventKind

                    self.send_listener.dispatch(SendEvent(SendEventKind.TEXT_UPDATED, {"has_text": has_text}))
            else:
                log.warning("No send_listener available for QueryTextListener setup")
        except Exception:
            log.exception("QueryTextListener setup failed")

    if controls["status"] and hasattr(controls["status"], "setText"):
        with suppress_disposed("set status text on init", logger=log, exc_info=True):
            controls["status"].setText(_("Ready"))

    # Stop +22px/windowResized loop when Record <-> Send (see writeragent_debug.log).
    # Measure before first relayout so snapshot preserves stabilized button widths.
    if controls["send"]:
        with suppress_disposed("send button width stabilize", logger=log):
            fw = _measure_send_button_max_width(controls["send"], has_recording)
            if fw:
                # No width is pinned on the listener any more: the layout shares
                # the button row and _relabel_button keeps that rect.
                sr = controls["send"].getPosSize()
                controls["send"].setPosSize(sr.X, sr.Y, fw, sr.Height, 15)
        with suppress_disposed("stop/clear button width stabilize", logger=log):
            for c, lab_list in ((controls.get("stop"), ["Stop", "Change", "Reject"]), (controls.get("clear"), ["Clear", "Reject"])):
                if not c:
                    continue
                aw = _measure_aux_button_max_width(c, lab_list)
                if aw:
                    r = c.getPosSize()
                    c.setPosSize(r.X, r.Y, aw, r.Height, 15)

    _frame_session = getattr(self, "frame_session", None)
    _restore_focus = _frame_session.restore_focus if _frame_session is not None else None
    try:
        log.debug("Attaching _PanelResizeListener to root_window; controls=%s" % (sorted(k for k, v in controls.items() if v)))
        _tp = getattr(self, "toolpanel", None)

        def _release_sidebar_on_window_dispose() -> None:
            # Same send-cancel and live-panel drop as ChatPanelElement.disposing.
            # That method is not called when the deck closes. Do not resolve the
            # model from the frame: it may already be dead. The uid stored at
            # register time is the slot this panel owns.
            from plugin.chatbot.panel_factory import release_live_sidebar

            release_live_sidebar(self, controls.get("query"))

        _resize = _PanelResizeListener(controls, on_dispose=_release_sidebar_on_window_dispose, restore_focus=_restore_focus)
        _resize._root_window = root_window
        _resize._parent_window = getattr(_tp, "parent_window", None) or getattr(self, "xParentWindow", None)
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
            log.info("[LAYOUT] layout_sanity root_w=%d max_child_right=%d overflow=%s" % (rw, max_right, "YES" if max_right > rw - 2 else "no"))
        except Exception:
            pass
    except Exception:
        log.exception("Resize listener setup failed")

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
        from plugin.embeddings.embeddings_periodic import schedule_periodic_embeddings_indexer_once

        schedule_periodic_embeddings_indexer_once(self.ctx)
    except Exception as e:
        log.warning("embeddings periodic indexer schedule failed: %s", e)

    # 7. Rich Text Control Sidebar (RichTextControl; embedded Writer path removed)
    from plugin.framework.config import get_config_bool_safe

    rich_sidebar_enabled = get_config_bool_safe("rich_text_control_sidebar")
    log.info("[RICH-CONTROL] config rich_text_control_sidebar=%s", rich_sidebar_enabled)
    if rich_sidebar_enabled:
        try:
            from plugin.chatbot.rich_text_control import RichTextChatWidget, RichTextControlListener, log_rich_control_context, log_rich_scroll

            def on_rich_control_ready(rich_control: Any) -> None:
                log.info("[RICH-CONTROL] on_rich_control_ready control=%s", bool(rich_control))
                session = getattr(self, "frame_session", None)
                restore_focus = session.restore_focus if session is not None else None
                widget = RichTextChatWidget(
                    self.ctx,
                    rich_control,
                    style_window=root_window,
                    query=controls.get("query"),
                    restore_focus=restore_focus,
                )
                self.rich_text_widget = widget
                if session is not None:
                    _install_frame_session_listeners(
                        session,
                        self.ctx,
                        controls.get("query"),
                        (
                            controls.get("stop"),
                            controls.get("clear"),
                            controls.get("send"),
                            controls.get("btn_settings"),
                            controls.get("btn_python"),
                            controls.get("btn_latex"),
                            controls.get("btn_search"),
                            controls.get("btn_hamburger"),
                            controls.get("chat_mode_selector"),
                            controls.get("model_selector"),
                        ),
                    )
                controls["response_rich"] = rich_control
                if hasattr(self, "_panel_resize_listener") and self._panel_resize_listener:
                    self._panel_resize_listener._c["response_rich"] = rich_control
                if hasattr(self, "send_listener") and self.send_listener:
                    self.send_listener.set_rich_text_widget(widget)
                try:
                    from plugin.calc.navigation import attach_calc_cell_link_listener

                    def _calc_doc_from_panel() -> Any | None:
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
                except Exception:
                    log.exception("Initial RichTextControl render failed")
                if hasattr(self, "_rich_control_listener") and self._rich_control_listener:
                    try:
                        log_rich_scroll("on_ready_step", control=rich_control, step="sync_bounds")
                        self._rich_control_listener._sync_bounds()
                    except Exception as e:
                        log.debug("on_rich_control_ready sync bounds: %s", e)
                try:
                    log_rich_scroll("on_ready_step", control=rich_control, step="reveal_caret")
                    widget.reveal_caret(reason="on_ready")
                except Exception as e:
                    log.debug("on_rich_control_ready reveal caret: %s", e)

            rich_control_listener = RichTextControlListener(
                self.ctx,
                root_window,
                controls["response"],
                on_rich_control_ready,
                restore_focus=_restore_focus,
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
        except Exception:
            log.exception("RichTextControl sidebar initialization failed")
