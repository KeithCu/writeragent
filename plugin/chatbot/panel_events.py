from __future__ import annotations

import logging
from typing import Any

from plugin.framework.errors import suppress_disposed

log = logging.getLogger("plugin.chatbot.panel")


def _relabel_button(ctrl: Any, label: str, mnemonics: dict[str, str]) -> None:
    """Change a sidebar button's label without moving it or losing its mnemonic.

    Keep the rect the layout gave the button, and put back the mnemonic
    VCL chose for that label the last time the button showed it. A
    Send/Record relabel that also sets a fixed send width (the old
    ``_fixed_send_width``, measured from the XDL before the panel layout
    shared the button row) shrinks Record (1x) or grows it over Stop (2x)
    until the next relayout. Setting the label also drops the mnemonic VCL
    added (``~Record``), so the R underline goes away. The model keeps the
    plain label that the click handlers compare.
    """
    model = ctrl.getModel()
    if model is None or model.Label == label:
        return
    rect = ctrl.getPosSize()
    peer = ctrl.getPeer()
    if peer is not None:
        shown = peer.getProperty("Label")
        if isinstance(shown, str) and "~" in shown:
            mnemonics[shown.replace("~", "")] = shown
    model.Label = label
    if peer is not None and label in mnemonics:
        peer.setProperty("Label", mnemonics[label])
    if rect is not None and rect.Width > 0:
        cur = ctrl.getPosSize()
        if (cur.X, cur.Y, cur.Width, cur.Height) != (rect.X, rect.Y, rect.Width, rect.Height):
            ctrl.setPosSize(rect.X, rect.Y, rect.Width, rect.Height, 15)


class PanelEventsController:
    """Events controller for MCP, HTTP server events, and inline web search approval."""

    _panel: Any

    def __init__(self, panel: Any) -> None:
        self._panel = panel

    @staticmethod
    def _mcp_subscribe_must_defer() -> bool:
        """True when get_tools() could need bootstrap and this is not the Python main thread."""
        import sys

        from plugin.framework.thread_guard import on_python_main_thread

        # Read the same module object that ``from plugin.main import get_tools`` resolves.
        main_mod = sys.modules.get("plugin.main")
        if main_mod is not None and getattr(main_mod, "_initialized", False) is True:
            return False
        return not on_python_main_thread()

    @staticmethod
    def _post_to_main_thread(fn: Any) -> None:
        from plugin.framework.queue_executor import post_to_main_thread

        post_to_main_thread(fn)

    def _subscribe_mcp_events(self) -> None:
        """Join services.events for mcp:request / mcp:result (may bootstrap; main thread)."""
        if self._panel._panel_teardown:
            return
        from plugin.main import get_tools

        event_bus = getattr(get_tools()._services, "events", None)
        if event_bus:
            # Remember this object and unsubscribe it in disposing.
            # Unsubscribing global_event_bus leaves this subscription,
            # which keeps calling a closed panel.
            self._panel._mcp_event_bus = event_bus
            event_bus.subscribe("mcp:request", self._panel._on_mcp_request, weak=True)
            event_bus.subscribe("mcp:result", self._panel._on_mcp_result, weak=True)
            event_bus.subscribe("http:server_started", self._panel._on_http_server_started, weak=True)
            event_bus.subscribe("http:server_stopped", self._panel._on_http_server_stopped, weak=True)
            log.debug(f"*** SendButtonListener subscribed to MCP events on services.events (id={id(event_bus)}) ***")

    def begin_inline_web_approval(self, query: str, tool: str, event: Any) -> None:
        """Replace Send/Stop/Clear with Accept/Change/Reject (all enabled). Unblock ``event`` when user chooses.

        Approval mode only mutates UNO control labels/enabled flags here and restores them from
        ``_approval_ui_backup`` in ``_finish_inline_web_approval``. It does **not** update
        ``sidebar_state.send`` or go through :meth:`dispatch` for those temporary labels—by design.
        Do not "fix" this by routing approval chrome through the send FSM; keep backup/restore
        as the source of truth for this overlay.
        """
        from plugin.framework.i18n import _

        if event is None:
            log.warning("begin_inline_web_approval: no event")
            return
        if getattr(self._panel, "_approval_event", None) is not None:
            log.warning("begin_inline_web_approval: superseding pending approval")
            self._panel._finish_inline_web_approval(False)
        self._panel._approval_event = event
        self._panel._approval_query_for_engine = query
        self._panel._approval_ui_backup = {}
        with suppress_disposed("begin_inline_web_approval backup", logger=log):
            if self._panel.send_control and self._panel.send_control.getModel():
                m = self._panel.send_control.getModel()
                self._panel._approval_ui_backup["send_label"] = m.Label
                self._panel._approval_ui_backup["send_enabled"] = m.Enabled
            if self._panel.stop_control and self._panel.stop_control.getModel():
                m = self._panel.stop_control.getModel()
                self._panel._approval_ui_backup["stop_label"] = m.Label
                self._panel._approval_ui_backup["stop_enabled"] = m.Enabled
            if self._panel.clear_control and self._panel.clear_control.getModel():
                cm = self._panel.clear_control.getModel()
                self._panel._approval_ui_backup["clear_enabled"] = cm.Enabled
                self._panel._approval_ui_backup["clear_label"] = cm.Label
            if self._panel.status_control:
                self._panel._approval_ui_backup["status_text"] = self._panel.status_control.getText()

        with suppress_disposed("begin_inline_web_approval", logger=log):
            if self._panel.send_control and self._panel.send_control.getModel():
                m = self._panel.send_control.getModel()
                _relabel_button(self._panel.send_control, _("Accept"), self._panel._mnemonic_cache())
                m.Enabled = True
            if self._panel.stop_control and self._panel.stop_control.getModel():
                m = self._panel.stop_control.getModel()
                _relabel_button(self._panel.stop_control, _("Change"), self._panel._mnemonic_cache())
                m.Enabled = True
            if self._panel.clear_control and self._panel.clear_control.getModel():
                m = self._panel.clear_control.getModel()
                _relabel_button(self._panel.clear_control, _("Reject"), self._panel._mnemonic_cache())
                m.Enabled = True

        # Approval is inline (Accept / Change / Reject); search preview is already in the transcript.
        self._panel._set_status(_("Waiting for approval…"))
        log.info("Inline web approval: waiting for Accept, Change, or Reject")

    def _open_web_search_change_dialog(self) -> None:
        """Open edit dialog for the pending web_search query; OK continues with optional override."""
        from plugin.chatbot.dialogs import show_web_search_query_edit_dialog

        initial = getattr(self._panel, "_approval_query_for_engine", None) or ""
        text = show_web_search_query_edit_dialog(self._panel.ctx, self._panel.frame, initial)
        if text is None:
            return
        log.debug("_open_web_search_change_dialog: applying edited query len=%d", len(text))
        self._panel._finish_inline_web_approval(True, query_override=text)

    def _finish_inline_web_approval(self, approved: bool, query_override: str | None = None) -> None:
        ev = getattr(self._panel, "_approval_event", None)
        if ev is None:
            return
        self._panel._approval_event = None
        self._panel._approval_query_for_engine = None
        b: dict[str, Any] = self._panel._approval_ui_backup or {}
        self._panel._approval_ui_backup = None
        with suppress_disposed("_finish_inline_web_approval restore", logger=log):
            if self._panel.send_control and self._panel.send_control.getModel():
                m = self._panel.send_control.getModel()
                if "send_label" in b:
                    _relabel_button(self._panel.send_control, b["send_label"], self._panel._mnemonic_cache())
                if "send_enabled" in b:
                    m.Enabled = b["send_enabled"]
            if self._panel.stop_control and self._panel.stop_control.getModel():
                m = self._panel.stop_control.getModel()
                if "stop_label" in b:
                    _relabel_button(self._panel.stop_control, b["stop_label"], self._panel._mnemonic_cache())
                if "stop_enabled" in b:
                    m.Enabled = b["stop_enabled"]
            if self._panel.clear_control and self._panel.clear_control.getModel() and "clear_enabled" in b:
                cm = self._panel.clear_control.getModel()
                cm.Enabled = b["clear_enabled"]
                if "clear_label" in b:
                    _relabel_button(self._panel.clear_control, b["clear_label"], self._panel._mnemonic_cache())
            if self._panel.status_control and "status_text" in b:
                self._panel.status_control.setText(b["status_text"])
        ev.approved = approved
        ev.query_override = query_override if approved else None
        if approved and query_override is not None:
            log.debug("_finish_inline_web_approval: approved with query_override len=%d", len(query_override))
        ev.set()

    def _on_http_server_started(self, url: str = "", **kwargs: Any) -> None:
        from plugin.framework.i18n import _

        self._panel._post_server_status((_("MCP server started") + " " + url).strip())

    def _on_http_server_stopped(self, **kwargs: Any) -> None:
        from plugin.framework.i18n import _

        self._panel._post_server_status(_("MCP server stopped"))

    def _post_server_status(self, text: str) -> None:
        """MCP start/stop goes to the status line; a modal box would eat typed prompts."""
        if getattr(self._panel, "_panel_teardown", False) or getattr(self._panel, "ctx", None) is None:
            return
        # Tip: don't overwrite the status of a running send or approval.
        if getattr(self._panel, "_send_busy", False) or getattr(self._panel, "_approval_event", None) is not None:
            return
        from plugin.framework.queue_executor import post_to_main_thread

        post_to_main_thread(self._panel._set_status, text)

    def _on_mcp_request(self, tool: str = "", args: Any = None, method: Any = None, **kwargs: Any) -> None:
        """Handle MCP request events from the bus (background thread)."""
        if getattr(self._panel, "_panel_teardown", False) or getattr(self._panel, "ctx", None) is None:
            return

        from plugin.chatbot.tool_loop_actions import current_turn

        # Bugfix: parallel tools/call requests rebuilt _last_mcp_turn while other
        # threads called pop or disposing cleared on the UI thread, causing race conditions.
        # EventBus runs mcp:request / mcp:result on HTTP workers, while disposing clears
        # on the UI thread. Mutating under _mcp_turn_lock with a second teardown check
        # prevents data races and stops late requests from storing turns after teardown.
        rid = str(kwargs.get("req_id", ""))
        turn = current_turn(self._panel)
        with self._panel._mcp_turn_lock:
            if self._panel._panel_teardown or getattr(self._panel, "ctx", None) is None:
                return
            kept = {k: v for k, v in self._panel._last_mcp_turn.items() if getattr(v, "alive", False)}
            kept[rid] = turn
            self._panel._last_mcp_turn = kept
        from plugin.framework.logging import format_tool_call_for_display

        fmt_str = format_tool_call_for_display(tool, args, method)
        log.debug(f"MCP Request (hidden from UI, level=logging.DEBUG): {fmt_str}")

    def _on_mcp_result(self, tool: str = "", result_snippet: str = "", **kwargs: Any) -> None:
        """Handle MCP result events from the bus (background thread)."""
        # Drop the result once teardown has started or ctx is cleared.
        # The bus callback and the queued UI hop are different turns, so a
        # result posted before disposing still runs _append_response after
        # the panel is gone.
        if getattr(self._panel, "_panel_teardown", False) or getattr(self._panel, "ctx", None) is None:
            return

        # Each request is stored on _last_mcp_turn[rid]; one shared last id
        # would drop the earlier of two concurrent MCP requests.
        from plugin.chatbot.tool_loop_actions import TurnController, current_turn

        rid = str(kwargs.get("req_id", ""))
        with self._panel._mcp_turn_lock:
            last_turn = self._panel._last_mcp_turn.pop(rid, None)
        if not isinstance(last_turn, TurnController) or current_turn(self._panel) is not last_turn or not last_turn.alive:
            return

        def _update_ui() -> None:
            if getattr(self._panel, "_panel_teardown", False) or getattr(self._panel, "ctx", None) is None:
                return
            from plugin.chatbot.tool_loop_actions import TurnController, current_turn
            if not isinstance(last_turn, TurnController) or current_turn(self._panel) is not last_turn or not last_turn.alive:
                return
            try:
                from plugin.framework.logging import format_tool_result_for_display

                fmt_str = format_tool_result_for_display(tool, result_snippet, args=kwargs.get("args"))
                self._panel._append_response(f"[MCP Result] {fmt_str}\n")
            except Exception:
                log.exception("_on_mcp_result UI update error")

        try:
            # The send drain pumps default_executor only. A post on the panel
            # queue sits until the drain ends, so an MCP result during Send
            # never reaches the sidebar until the turn is over.
            from plugin.framework.queue_executor import post_to_main_thread
            from plugin.framework.thread_guard import get_background_task_name, on_main_thread

            if on_main_thread() and not get_background_task_name():
                _update_ui()
            else:
                post_to_main_thread(_update_ui)
        except Exception:
            log.exception("_on_mcp_result post error")
