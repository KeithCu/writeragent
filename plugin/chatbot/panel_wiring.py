from __future__ import annotations

import logging
from typing import Any


from plugin.chatbot.dialogs import get_optional as get_optional_control, get_control_text, set_control_text, set_control_visible, translate_dialog
from plugin.chatbot.panel_resize import _PanelResizeListener
from plugin.framework.config import get_config
from plugin.framework.errors import best_effort, suppress_disposed
from plugin.framework.i18n import _
from plugin.framework.event_bus import get_event_bus
from plugin.framework.logging import init_logging

log = logging.getLogger(__name__)


def _measure_button_max_width(ctrl: Any, labels: list[str]) -> int | None:
    """Stabilize width when a button's label toggles (Send/Record, Stop/Change, Clear/Reject)."""
    if not ctrl or not hasattr(ctrl, "getModel") or not labels:
        return None
    with suppress_disposed("measure button width", logger=log):
        m = ctrl.getModel()
        saved = m.Label
        try:
            wmax = ctrl.getPosSize().Width
            for lab in labels:
                # Live labels are _(effect.send_label). Measuring the English
                # string let a longer translation change width before the pin.
                m.Label = _(lab)
                wmax = max(wmax, ctrl.getPosSize().Width)
            return wmax if wmax > 0 else None
        finally:
            # Restore the label here. A failed width read otherwise leaves
            # the last candidate on the button, and suppress_disposed swallows
            # the error before a restore after the loop.
            m.Label = saved
    return None


_LEAVE_QUERY_CONTROL_NAMES: tuple[str, ...] = (
    "stop",
    "clear",
    "send",
    "btn_settings",
    "btn_python",
    "btn_latex",
    "btn_search",
    "btn_hamburger",
    "chat_mode_selector",
    "model_selector",
)


def _install_frame_session_listeners(
    session: Any,
    controls: dict[str, Any],
) -> None:
    """Pin Ask and attach this frame's focus and click listeners.

    Re-raise a ``UNO thread violation`` ``RuntimeError``. ``install``
    raises that from ``getController``, ``addFocusListener``,
    ``addMouseListener``, and ``addMouseClickHandler`` so a missed attach
    is not a successful return. Catching it here keeps the contract inside
    the sidebar. Any other attach failure stays a debug log.
    """
    query = controls.get("query")
    leave_query_controls = tuple(controls.get(name) for name in _LEAVE_QUERY_CONTROL_NAMES)
    try:
        session.set_focus_pin(query)
        session.install(query=query, leave_query_controls=leave_query_controls)
    except Exception as exc:
        from plugin.framework.uno_listeners import listener_boundary

        boundary = listener_boundary(exc)
        if boundary is not None and boundary.kind == "thread":
            raise
        log.debug("frame session focus install: %s", exc)


def _pin_button_width(ctrl: Any, labels: list[str]) -> None:
    """Measure and pin control width across toggle labels."""
    if not ctrl:
        return
    w = _measure_button_max_width(ctrl, labels)
    if w:
        r = ctrl.getPosSize()
        ctrl.setPosSize(r.X, r.Y, w, r.Height, 15)


def setup_rich_control_ready(
    panel: Any,
    root_window: Any,
    controls: dict[str, Any],
    initial_mode: Any,
    model: Any,
    rich_control: Any,
) -> None:
    """Initialize rich text widget and wire listeners when rich control is ready."""
    log.info("[RICH-CONTROL] on_rich_control_ready control=%s", bool(rich_control))
    from plugin.chatbot.rich_text_control import (
        RichTextChatWidget,
        log_rich_control_context,
        log_rich_scroll,
    )

    session = panel.frame_session
    restore_focus = session.restore_focus if session is not None else None
    widget = RichTextChatWidget(
        panel.ctx,
        rich_control,
        style_window=root_window,
        query=controls.get("query"),
        restore_focus=restore_focus,
    )
    panel.rich_text_widget = widget
    if session is not None:
        _install_frame_session_listeners(
            session,
            controls,
        )
    controls["response_rich"] = rich_control
    rl = panel.toolpanel.resize_listener if panel.toolpanel else None
    if rl:
        rl.set_control("response_rich", rich_control)
    if panel.send_listener:
        panel.send_listener.set_rich_text_widget(widget)

    def _calc_doc_from_panel() -> Any | None:
        sl = panel.send_listener
        if sl is None:
            return None
        doc = sl._get_document_model()
        if doc is not None and hasattr(doc, "getSheets"):
            return doc
        return None

    def _attach_calc_cell_link(ctrl: Any, fail_msg: str) -> None:
        try:
            from plugin.calc.navigation import attach_calc_cell_link_listener

            attach_calc_cell_link_listener(panel.ctx, ctrl, _calc_doc_from_panel)
        except Exception as e:
            log.debug(fail_msg, e)

    _attach_calc_cell_link(rich_control, "calc cell link listener attach failed: %s")

    def _on_control_replaced(new_ctrl: Any) -> None:
        widget.replace_control(new_ctrl)
        _attach_calc_cell_link(new_ctrl, "calc cell link listener re-attach failed: %s")

    if panel._rich_control_listener:
        panel._rich_control_listener.on_replaced = _on_control_replaced
    if rl:
        rl.on_control_replaced = _on_control_replaced

    set_control_visible(controls.get("response"), False)
    log_rich_control_context(
        panel.ctx,
        "hide_plain",
        ok=1,
        response=int(bool(controls.get("response"))),
    )
    if rl:
        log_rich_scroll("on_ready_step", control=rich_control, step="relayout")
        rl.relayout_now(root_window)
    try:
        log_rich_scroll("on_ready_step", control=rich_control, step="history")
        from plugin.chatbot.chat_sidebar_mode import greeting_for_sidebar_mode

        rich_greeting = greeting_for_sidebar_mode(initial_mode, model)
        panel._render_session_history(panel.session, controls["response"], rich_greeting)
    except Exception:
        log.exception("Initial RichTextControl render failed")
    if panel._rich_control_listener:
        log_rich_scroll("on_ready_step", control=rich_control, step="sync_bounds")
        best_effort(panel._rich_control_listener._sync_bounds, "on_rich_control_ready sync bounds")
    log_rich_scroll("on_ready_step", control=rich_control, step="reveal_caret")
    best_effort(lambda: widget.reveal_caret(reason="on_ready"), "on_rich_control_ready reveal caret")


def _wireControls(self: Any, root_window: Any) -> None:  # pyright: ignore[reportUnusedFunction]  # imported as wire_chatpanel_controls by panel_factory
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
        "model_selector": get_optional("model_selector"),
        "reasoning_effort_selector": get_optional("reasoning_effort_selector"),
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

    from plugin.chatbot.panel_factory import _initialize_extension_paths

    _initialize_extension_paths(self.ctx)

    extra_instructions = ""
    model = self._get_document_model()
    try:
        from plugin.doc.doc_type import (
            doc_type_label_for_enum,
            doc_type_title_for_label,
            get_document_type,
            get_document_uno_services,
        )

        doc_type = get_document_type(model)
        self._doc_type_label = doc_type_label_for_enum(doc_type)
        self._doc_type_title = doc_type_title_for_label(self._doc_type_label)
        self._doc_uno_services = get_document_uno_services(model)
    except Exception as e:
        log.exception("Failed to query document type / UNO services: %s", e)
        self._doc_type_label = "unknown"
        self._doc_type_title = "Unknown"
        self._doc_uno_services = frozenset()

    initial_mode = "chat"
    # This seed is what remains when _wire_chat_mode_ui fails;
    # _wire_buttons must receive a SidebarModeFlags, not None.
    from plugin.chatbot.chat_sidebar_mode import SidebarModeFlags

    mode_flags: SidebarModeFlags = SidebarModeFlags()

    # 1. Config, Models, and UI
    try:
        extra_instructions = get_config("additional_instructions")
    except Exception as e:
        _show_init_error("Config: %s" % e)
        log.exception("Config instructions read failed")

    try:
        self._wire_model_selectors(controls)
    except Exception as e:
        # Model selector wiring is its own try so a failure here still
        # wires the chat mode UI.
        _show_init_error("Model selectors: %s" % e)
        log.exception("Model selectors wiring failed")

    try:
        initial_mode, mode_flags = self._wire_chat_mode_ui(controls)
    except Exception as e:
        _show_init_error("Mode UI: %s" % e)
        log.exception("Chat mode UI wiring failed")

    # Session and button failures propagate. getRealInterface clears toolpanel
    # and the next call rebuilds. Config, models, and mode UI stay degradable.
    # 2. Setup Sessions
    self._setup_sessions(model, extra_instructions)

    # 3. Buttons (applies initial mode, session history, and mode listener)
    send_listener = self._wire_buttons(controls, model, initial_mode, mode_flags)

    # Wire query listener to update Record/Send button label (fixed width captured in snapshot before relayout)
    query_text_listener = None
    if controls.get("query") and controls.get("send"):
        try:
            from plugin.chatbot.panel import QueryKeyListener, QueryTextListener

            query_ctrl = controls["query"]
            query_text_listener = QueryTextListener(send_listener)
            query_ctrl.addTextListener(query_text_listener)
            query_ctrl.addKeyListener(QueryKeyListener(send_listener))

            slash_ctrl = controls.get("slash_popup")
            if slash_ctrl is not None:
                from plugin.chatbot.slash_popup import SlashPopupController

                send_listener.slash_popup = SlashPopupController(
                    slash_ctrl,
                    send_listener,
                    query_ctrl,
                    overlay_parent=root_window,
                )
                log.info("slash popup attached to Ask field")

            # The label update logic is now handled correctly by the state machine
            # so we can just trigger a fake text update event to sync the state
            has_text = bool(get_control_text(query_ctrl).strip())
            from plugin.chatbot.send_state import SendEvent, SendEventKind

            send_listener.dispatch(SendEvent(SendEventKind.TEXT_UPDATED, has_text=has_text))
        except Exception:
            log.exception("QueryTextListener setup failed")

    if self.frame_session is not None:
        _install_frame_session_listeners(
            self.frame_session,
            controls,
        )

    if controls["status"] and hasattr(controls["status"], "setText"):
        with suppress_disposed("set status text on init", logger=log, exc_info=True):
            controls["status"].setText(_("Ready"))

    # Stop +22px/windowResized loop when Record <-> Send (see writeragent_debug.log).
    # Measure before first relayout so snapshot preserves stabilized button widths.
    if controls["send"]:
        from plugin.chatbot.panel import HAS_RECORDING

        send_labels = ["Send", "Record", "Stop Rec", "Accept"] if HAS_RECORDING else ["Send", "Accept"]
        with suppress_disposed("send button width stabilize", logger=log):
            _pin_button_width(controls["send"], send_labels)
        with suppress_disposed("stop/clear button width stabilize", logger=log):
            _pin_button_width(controls.get("stop"), ["Stop", "Change", "Reject"])
            _pin_button_width(controls.get("clear"), ["Clear", "Reject"])

    _frame_session = self.frame_session
    _restore_focus = _frame_session.restore_focus if _frame_session is not None else None
    _resize: Any = None
    try:
        log.debug("Attaching _PanelResizeListener to root_window; controls=%s" % (sorted(k for k, v in controls.items() if v)))
        _tp = self.toolpanel

        def _release_sidebar_on_window_dispose() -> None:
            # Same send-cancel and live-panel drop as ChatPanelElement.disposing.
            # That method is not called when the deck closes. Do not resolve the
            # model from the frame: it may already be dead. The uid stored at
            # register time is the slot this panel owns.
            from plugin.chatbot.panel_factory import release_live_sidebar

            release_live_sidebar(self, controls.get("query"))

        _resize = _PanelResizeListener(controls, on_dispose=_release_sidebar_on_window_dispose, restore_focus=_restore_focus)
        _resize.set_windows(root_window, (_tp.parent_window if _tp else None) or self.xParentWindow)
        root_window.addWindowListener(_resize)
        if _tp is not None:
            _tp.resize_listener = _resize
        _resize.relayout_now(root_window)

        # One-time marker for the very first layout after the panel is created.
        # Extremely useful for diagnosing the "starts wide on restart → scrollbar" case.
        log.debug("[FIRST LAYOUT] root_w=%d (this is the initial size on app start / sidebar show)",
                  root_window.getPosSize().Width)

        # Lightweight layout sanity log (always emitted at DEBUG after first wiring).
        # Helps catch cases where child controls would overflow the allocated panel width.
        if log.isEnabledFor(logging.DEBUG):
            rw = root_window.getPosSize().Width
            max_right = 0
            for nm in ("clear", "send", "stop", "model_selector", "query", "response"):
                c = controls.get(nm)
                if c:
                    pr = c.getPosSize()
                    max_right = max(max_right, pr.X + pr.Width)
            log.debug("[LAYOUT] layout_sanity root_w=%d max_child_right=%d overflow=%s" % (rw, max_right, "YES" if max_right > rw - 2 else "no"))
    except Exception:
        log.exception("Resize listener setup failed")

    # Backend indicator (when external agent enabled)
    self._update_backend_indicator(root_window)

    # 6. Global Config Listener
    get_event_bus().subscribe("config:changed", self._on_config_changed, weak=True)

    # Weekly extension update check: run once per process, late (after sidebar UI is wired)
    # so logging is initialized and AsyncCallback/msgbox are reliable.
    try:
        init_logging(self.ctx)
        from plugin.chatbot.extension_update_check import schedule_extension_update_check_once
        from plugin.framework.constants import EXTENSION_ID_WRITERAGENT

        schedule_extension_update_check_once(self.ctx, EXTENSION_ID_WRITERAGENT)
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
            from plugin.chatbot.rich_text_control import RichTextControlListener, log_rich_control_context

            rich_control_listener = RichTextControlListener(
                self.ctx,
                root_window,
                controls["response"],
                lambda rc: setup_rich_control_ready(self, root_window, controls, initial_mode, model, rc),
                restore_focus=_restore_focus,
                placeholder_rect_fn=lambda: (
                    _resize.last_response_rect
                    if _resize
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
