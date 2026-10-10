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
"""Chat sidebar panel logic: session, send/tool loop, and button listeners.

ChatSession holds conversation history and refreshes ``[DOCUMENT CONTENT]``
on Chat-mode switch. SendButtonListener drives the streaming tool-calling
loop (via SendHandlersMixin / ToolCallingMixin). StopButtonListener and
ClearButtonListener are wired by panel_factory. UNO UI element factory
and XDL wiring remain in panel_factory.py.
"""

from __future__ import annotations

import logging
import threading
import uno
from plugin.chatbot.send_handlers import SendHandlersMixin
from plugin.chatbot.tool_loop import ToolCallingMixin

from plugin.framework.errors import suppress_disposed
from plugin.framework.logging import update_activity_state
from plugin.framework.queue_executor import QueueExecutor
from plugin.chatbot.history_db import get_chat_history
from plugin.chatbot.record_gesture import (
    EMPTY_TAKES_EXIT,
    RECORD_HOLD_MS,
    RECORDING_STATUS,
    STICKY_TTS_POLL_MS,
    RecordGesture,
    StickyRestart,
    TakeStop,
    exit_sticky,
    gesture_action,
    gesture_hold_elapsed,
    gesture_press,
    gesture_release,
    hands_free_silence_text,
    hands_free_status_text,
    stop_during_take,
    sticky_restart,
)

# Recording shipped unless built with --no-recording (see scripts/build_oxt.py).
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable
    from plugin.framework.client.llm_client import LlmClient

_AudioRecorderCls: type[Any] | None
try:
    from plugin.chatbot.audio_recorder import AudioRecorder as _AR

    _AudioRecorderCls = _AR
except ImportError:
    _AudioRecorderCls = None
HAS_RECORDING = _AudioRecorderCls is not None
from plugin.scripting.audio_recorder_service import is_audio_recording_supported


from plugin.chatbot.grammar_status import (
    format_grammar_status,
)


# ---------------------------------------------------------------------------
# ChatSession - holds conversation history for multi-turn chat
# ---------------------------------------------------------------------------


class ChatSession:
    """Maintains the message history for one sidebar chat session."""

    session_id: str | None
    db: Any
    messages: list[dict[str, Any]]
    base_system_prompt: str
    document_context: str
    active_specialized_domain: str | None
    python_tool_domain: str | None
    compaction: Any

    def __init__(self, system_prompt: str | None = None, session_id: str | None = None) -> None:
        self.session_id = session_id
        self.db = None
        self.messages = []
        self.base_system_prompt = system_prompt or ""
        self.document_context = ""

        self.active_specialized_domain = None
        self.python_tool_domain = None
        # Cached compact view (CompactionState). Duck-typed by compaction.py;
        # never persisted. New chat / clear() must drop it or the next send
        # would keep summarizing against a stale first_kept_index.
        self.compaction = None

        history_load_failed = False
        if session_id:
            try:
                self.db = get_chat_history(session_id)
                self.messages = self.db.get_messages()
            except Exception:
                # JSONDecodeError and sqlite json.loads used to land here with
                # messages still []. The seed below then add_message'd a system
                # row without deleting the old ones (or over a truncated file).
                log.exception("ChatSession history load failed")
                history_load_failed = True

        # Empty new session still seeds and is written. A failed read (the
        # backend opened, then get_messages raised) must not insert a system
        # row: that replaced a corrupt history with a one-message session.
        # If the backend never opened, there is no file to protect, so the
        # prompt stays in memory for this run and nothing is written.
        if history_load_failed:
            if self.db is None and self.base_system_prompt:
                self.set_system_context(self.base_system_prompt, "")
        elif not self.messages and self.base_system_prompt:
            self.set_system_context(self.base_system_prompt, "")
            if self.db:
                self.db.add_message("system", self.messages[0]["content"])

    def set_system_context(self, base_prompt: str, doc_text: str = "") -> None:
        """Update the system prompt and document context, combining them into the first message."""
        self.base_system_prompt = base_prompt
        self.document_context = doc_text
        
        content = base_prompt
        if doc_text:
            content += f"\n\n[DOCUMENT CONTENT]\n{doc_text}\n[END DOCUMENT]"
            
        if not self.messages or self.messages[0]["role"] != "system":
            self.messages.insert(0, {"role": "system", "content": content})
        else:
            self.messages[0]["content"] = content

    def refresh_document_context(self, model: Any, ctx: Any) -> None:
        """Reload the Chat system prompt and ``[DOCUMENT CONTENT]`` from the live document.

        Why this lives on ChatSession, not panel_factory: the factory only wires
        XDL/controls. Mode switch, each send, and mid-loop refresh after a
        mutating tool all need a fresh snapshot. Send/tool_loop call this
        helper; they do not import the builder.
        """
        from plugin.doc.document_helpers import get_document_context_for_chat
        from plugin.framework.config import get_config
        from plugin.framework.constants import CHAT_DOCUMENT_CONTEXT_MAX_CHARS
        from plugin.framework.prompts import get_chat_system_prompt_for_document

        extra_instructions = str(get_config("additional_instructions") or "")
        base_prompt = get_chat_system_prompt_for_document(model, extra_instructions, ctx=ctx)
        doc_text = get_document_context_for_chat(
            model, CHAT_DOCUMENT_CONTEXT_MAX_CHARS, include_end=True, include_selection=True, ctx=ctx
        )
        self.set_system_context(base_prompt, doc_text)

    def add_user_message(self, content: Any) -> None:
        self.messages.append({"role": "user", "content": content})
        if self.db:
            self.db.add_message("user", content)

    def add_assistant_message(self, content: Any = None, tool_calls: Any = None, reasoning_replay: Any = None) -> None:
        msg = {"role": "assistant"}
        if content:
            msg["content"] = content
        else:
            msg["content"] = ""
        if tool_calls:
            msg["tool_calls"] = tool_calls
        if reasoning_replay:
            msg.update(reasoning_replay)
        # Streamed tokens sit in an open row so the sidebar can paint them.
        # This committed message replaces that row. Leaving both showed the
        # answer twice, and the open row is not a history write.
        if self.messages and self.messages[-1].get("_open_transcript"):
            self.messages.pop()
        self.messages.append(msg)
        # Tool calls stay out of history. content=None used to be written
        # anyway, and message_to_dict stored JSON null for a tool-only turn.
        # Memory keeps "" above; disk skips empty assistant text.
        if self.db and content:
            self.db.add_message("assistant", content)

    def add_tool_result(self, tool_call_id: str, content: Any) -> None:
        self.messages.append({"role": "tool", "tool_call_id": tool_call_id, "content": content})
        # Note: We do NOT persist tool results to history_db.
        # This keeps the persistent history clean of tool formatting requirements.

    def clear(self) -> None:
        """Reset to just the system prompt."""
        self.messages = []
        self.document_context = ""
        self.compaction = None
        # The next send advertises tools from these fields. Leaving the
        # previous delegate set meant Clear still offered that domain.
        self.active_specialized_domain = None
        self.python_tool_domain = None
        if self.db:
            self.db.clear()
            
        if self.base_system_prompt:
            self.set_system_context(self.base_system_prompt, "")
            if self.db:
                self.db.add_message("system", self.messages[0]["content"])


# ---------------------------------------------------------------------------
# QueryTextListener - dynamic button toggling
# ---------------------------------------------------------------------------

from plugin.framework.uno_listeners import BaseActionListener, BaseKeyListener, BaseTextListener
from plugin.chatbot.audio_recorder_state import AudioRecorderState
from plugin.chatbot.send_state import (
    CancelRecordingEffect,
    SendButtonState,
    SendEvent,
    SendEventKind,
    StartRecordingEffect,
    StartSendEffect,
    StopRecordingEffect,
    StopSendEffect,
    UpdateUIEffect,
)
from plugin.chatbot.sidebar_state import LogSidebarEffect, SidebarCompositeState, SidebarEvent, SidebarEventKind, sidebar_next_state

log = logging.getLogger(__name__)


def _uno_model_probe_for_log(model: Any, *, cached_doc_type: str | None = None) -> str:
    """Short UNO diagnostic for error logs. No document text or type probing."""
    if model is None:
        return "None"
    impl = "?"
    try:
        impl = model.getImplementationName()
    except Exception:
        pass
    if cached_doc_type:
        return "impl=%s doc_type=%s" % (impl, cached_doc_type)
    return "impl=%s" % impl


def _stop_tts() -> None:
    """Stop active speech synthesis, logging errors at debug level."""
    try:
        from plugin.audio.tts_service import stop_speech

        stop_speech()
    except Exception:
        log.debug("stop_speech failed", exc_info=True)


def _is_approval_pending(send_listener: Any) -> bool:
    """True when web-search inline approval is waiting for user response."""
    if send_listener is None:
        return False
    return getattr(send_listener, "_approval_event", None) is not None


class QueryTextListener(BaseTextListener):
    send_listener: Any

    def __init__(self, send_listener: Any) -> None:
        # We now keep a reference to the main SendButtonListener which holds the state
        self.send_listener = send_listener

    def on_text_changed(self, rEvent: Any) -> None:
        # Disposing must drop this listener from the Ask control, or a
        # late text event dispatches TEXT_UPDATED into a dead panel.
        # ``is True`` so a MagicMock host (tests) is not treated as dead.
        if getattr(self.send_listener, "_panel_teardown", False) is True:
            return
        model = getattr(rEvent.Source, "Model", None)
        if not model:
            model = rEvent.Source.getModel()
        raw = model.Text or ""
        text = raw.strip()

        # ENABLE_SLASH is parked. Keep this listener so Send enablement still
        # sees TEXT_UPDATED, but do not call the overlay or emit per-keystroke
        # [SLASH-OV] lines. Breadcrumbs go through _ovlog (silent unless
        # SLASH_OV_VERBOSE_DEBUG).
        from plugin.chatbot.slash_popup import ENABLE_SLASH, _ovlog

        if ENABLE_SLASH:
            from plugin.chatbot.slash_commands import slash_typed_prefix

            # Overlay first. TEXT_UPDATED UpdateUI has hung before on_query_text ran.
            # Do not getPosSize Ask here: after the TOP overlay exists it deadlocks VCL.
            snippet = raw[:40] if isinstance(raw, str) else raw
            _ovlog("query_text entered")
            _ovlog("query_text raw=%r", snippet)
            popup = getattr(self.send_listener, "slash_popup", None)
            on_text = getattr(popup, "on_query_text", None) if popup is not None else None
            _ovlog(
                "query_text popup=%s call_show=%s raw=%r",
                popup is not None,
                callable(on_text),
                snippet,
            )
            if callable(on_text):
                owner = getattr(on_text, "__self__", None)
                _ovlog(
                    "query_text invoking on_query_text ENABLE_SLASH=%s id=%s",
                    bool(ENABLE_SLASH),
                    id(owner) if owner is not None else None,
                )
                try:
                    on_text(raw)
                except Exception:
                    log.exception("[SLASH-OV] query_text on_query_text raised")
                _ovlog("query_text on_query_text returned")
            # UpdateUI after creating/mapping a TOP overlay deadlocks VCL.
            if slash_typed_prefix(raw) is not None:
                _ovlog("query_text skip TEXT_UPDATED slash prefix")
                return
            _ovlog("query_text before dispatch")
        self.send_listener.dispatch(SendEvent(SendEventKind.TEXT_UPDATED, {"has_text": bool(text)}))
        if ENABLE_SLASH:
            _ovlog("query_text after dispatch")


# UNO Key.RETURN / KeyModifier.SHIFT (test-friendly integer codes)
_QUERY_KEY_RETURN = 1280
_QUERY_KEY_MODIFIER_SHIFT = 1


def query_enter_triggers_primary_send(key_code: int, modifiers: int) -> bool:
    """True when this key event should run the same primary action as Send (Enter without Shift)."""
    return bool(key_code == _QUERY_KEY_RETURN and (modifiers & _QUERY_KEY_MODIFIER_SHIFT) == 0)


_DOC_CHAT_ENTER_SENDS = "doc.chat_enter_key_sends_message"


class QueryKeyListener(BaseKeyListener):
    """Enter in the query field triggers Send when enabled in Settings (Shift+Enter inserts a newline)."""

    send_listener: Any

    def __init__(self, send_listener: Any) -> None:
        self.send_listener = send_listener

    def on_key_pressed(self, e: Any) -> None:
        # Popup Enter must not also Send. Consume only when handle_key is True
        # (MagicMock hosts in unit tests return a mock, which is not True).
        # While ENABLE_SLASH is false, skip the overlay so keystrokes do not
        # run slash work or emit [SLASH-OV] query_key lines. Enter-to-send stays.
        from plugin.chatbot.slash_popup import ENABLE_SLASH, _ovlog

        if ENABLE_SLASH:
            popup = getattr(self.send_listener, "slash_popup", None)
            handle = getattr(popup, "handle_key", None) if popup is not None else None
            _ovlog(
                "query_key code=%s mods=%s has_handle=%s",
                int(getattr(e, "KeyCode", -1) or -1),
                int(getattr(e, "Modifiers", 0) or 0),
                callable(handle),
            )
            if callable(handle) and handle(e.KeyCode, e.Modifiers) is True:
                with suppress_disposed("QueryKeyListener slash Consume", logger=log):
                    if hasattr(e, "Consume"):
                        setattr(e, "Consume", True)
                return
        if not query_enter_triggers_primary_send(e.KeyCode, e.Modifiers):
            return
        try:
            from plugin.framework.config import get_config_bool

            if not get_config_bool(_DOC_CHAT_ENTER_SENDS):
                return
        except Exception:
            return
        sc = self.send_listener.send_control
        if not sc or not sc.getModel():
            return
        if not sc.getModel().Enabled:
            return
        from plugin.framework.i18n import _

        # Enter in the query box sends only when the button label is
        # "Send". Enabled alone is not enough: an empty box reads "Record"
        # (Enter would start recording), "Stop Rec" while recording would
        # stop and send, and "Accept" is web-search approval.
        if sc.getModel().Label != _("Send"):
            return
        with suppress_disposed("QueryKeyListener Consume", logger=log):
            if hasattr(e, "Consume"):
                setattr(e, "Consume", True)
        self.send_listener.on_action_performed(e)


# ---------------------------------------------------------------------------
# SendButtonListener - handles Send button click with tool-calling loop
# ---------------------------------------------------------------------------


def _chunk_text(turn: Any, text: str, role: str, *, strip_non_assistant: bool) -> str:
    """Plain text for one sidebar chunk. The stripper lives on the turn."""
    from plugin.chatbot.tool_loop_actions import TurnController
    from plugin.framework.html_stripper import strip_html_tags

    stripper = turn.stripper if isinstance(turn, TurnController) else None
    if role == "assistant" and stripper is not None:
        return stripper.feed(text)
    if role == "assistant" or strip_non_assistant:
        return strip_html_tags(text)
    return text


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


class SendButtonListener(SendHandlersMixin, ToolCallingMixin, BaseActionListener):
    """Listener for the Send button - runs chat with document, supports tool-calling."""

    ctx: Any
    frame: Any
    frame_session: Any
    send_control: Any
    stop_control: Any
    clear_control: Any
    query_control: Any
    response_control: Any
    image_model_selector: Any
    model_selector: Any
    status_control: Any
    session: ChatSession
    chat_mode_selector: Any
    aspect_ratio_selector: Any
    base_size_input: Any
    sidebar_include_brainstorming: bool
    sidebar_mode_flags: Any
    ensure_path_fn: Any
    client: LlmClient | None
    initial_doc_type: str | None
    cached_doc_type: str | None
    cached_uno_services: frozenset[str] | None
    _stop_requested_fallback: bool
    _terminal_status: str
    _stt_inflight: bool
    _stt_kill: Any
    _send_busy: bool
    _in_librarian_mode: bool
    _in_brainstorming_mode: bool
    _brainstorming_topic: str
    _in_writing_plan_mode: bool
    _writing_plan_topic: str
    _in_ppt_master_mode: bool
    _ppt_master_topic: str
    panel: Any
    audio_wav_path: str | None
    _current_agent_backend: Any
    _current_tool_call_id: str | None
    _approval_event: Any
    _extracted_peer_query: str
    _extracted_peer_already_appended: bool
    _record_assistant_start: bool
    slash_popup: Any
    clear_listener: Any
    rich_text_widget: Any
    _rich_plain_fallback_warned: bool
    queue_executor: QueueExecutor
    audio_recorder: Any
    sidebar_state: SidebarCompositeState
    _record_gesture: RecordGesture
    _record_hold_gen: int
    _sticky_restart_gen: int
    _sticky_restart_pending: bool
    _empty_take_count: int
    _panel_teardown: bool
    _mcp_event_bus: Any
    _turn: Any
    _last_mcp_turn: dict[str, Any]
    _last_mcp_req_id: int | str | None
    _send_start_msg_count: int | None
    _stop_mouse_listener: Any
    _stop_mouse_control: Any
    _record_mouse_listener: Any
    _record_mouse_control: Any

    def clear_pending_audio_wav(self) -> None:
        """Clear and delete any un-sent audio recording."""
        if hasattr(self, "audio_wav_path") and self.audio_wav_path:
            try:
                import os

                os.remove(self.audio_wav_path)
            except Exception:
                pass
            self.audio_wav_path = None

    def __init__(
        self,
        ctx: Any,
        frame: Any,
        send_control: Any,
        stop_control: Any,
        query_control: Any,
        response_control: Any,
        image_model_selector: Any,
        model_selector: Any,
        status_control: Any,
        session: Any,
        chat_mode_selector: Any = None,
        aspect_ratio_selector: Any = None,
        base_size_input: Any = None,
        sidebar_include_brainstorming: bool = True,
        ensure_path_fn: Any = None,
        clear_control: Any = None,
    ) -> None:
        self.ctx = ctx
        self.frame = frame
        self.frame_session = None
        self.send_control = send_control
        self.stop_control = stop_control
        self.clear_control = clear_control
        self.query_control = query_control
        self.response_control = response_control
        self.image_model_selector = image_model_selector
        self.model_selector = model_selector
        self.status_control = status_control
        self.session = session
        self.chat_mode_selector = chat_mode_selector
        self.aspect_ratio_selector = aspect_ratio_selector
        self.base_size_input = base_size_input
        self.sidebar_include_brainstorming = sidebar_include_brainstorming
        from plugin.chatbot.chat_sidebar_mode import SidebarModeFlags

        self.sidebar_mode_flags = SidebarModeFlags(include_brainstorming=sidebar_include_brainstorming)
        self.ensure_path_fn = ensure_path_fn
        self.initial_doc_type = None  # Set by _wireControls
        self.cached_doc_type = None
        self.cached_uno_services = None
        self._stop_requested_fallback = False
        self._send_cancellation: Any = None
        self._terminal_status = "Ready"
        self._stt_inflight = False
        self._stt_kill = None
        self._send_busy = False
        self._in_librarian_mode = False
        self._in_brainstorming_mode = False
        self._brainstorming_topic = ""
        self._in_writing_plan_mode = False
        self._writing_plan_topic = ""
        self._in_ppt_master_mode = False
        self._ppt_master_topic = ""
        self.panel = None
        self.client = None
        self.audio_wav_path = None
        self._current_agent_backend = None  # Set during _do_send_via_agent_backend for Stop button
        self._button_mnemonics: dict[str, str] = {}
        # Session I/O handles for the tool-loop interpreter (not FSM control state).
        # The queue, stripper, and document model live on ``_turn``.
        self._turn = None
        self._last_mcp_turn = {}
        self._last_mcp_req_id = None
        self._active_client: Any = None
        self._active_max_tokens: Any = None
        self._active_tools: Any = None
        self._active_execute_tool_fn: Any = None
        self._active_query_text: Any = None
        self._active_supports_status: Any = None
        self._current_tool_call_id = None
        self._record_assistant_start = False
        self._assistant_stream_start_len: int | None = None
        self._approval_event = None
        self._approval_ui_backup: dict[str, Any] | None = None
        self._approval_query_for_engine: str | None = None
        self._dispatch_reenter: list[Any] | None = None
        self._extracted_peer_query = ""
        self._extracted_peer_already_appended = False
        self._send_start_msg_count = None
        self._stop_mouse_listener = None
        self._stop_mouse_control = None
        self._record_mouse_listener = None
        self._record_mouse_control = None
        self.slash_popup = None
        self.clear_listener = None
        self.rich_text_widget = None
        self._rich_plain_fallback_warned = False
        self.queue_executor = QueueExecutor(ctx=ctx)
        if HAS_RECORDING:
            assert _AudioRecorderCls is not None
            self.audio_recorder = _AudioRecorderCls(ctx)
            self.audio_recorder.set_auto_stop_callbacks(
                on_auto_stop=lambda: self.queue_executor.post(self._on_audio_auto_stop),
                on_silence_progress=lambda ms: self.queue_executor.post(self._on_audio_silence_progress, ms),
                on_error=lambda msg: self.queue_executor.post(self._on_audio_recorder_error, msg),
            )
        else:
            self.audio_recorder = None
        audio_supported = HAS_RECORDING and is_audio_recording_supported(ctx)

        send_initial = SendButtonState(is_busy=False, is_recording=False, has_text=False, has_audio=False, audio_supported=audio_supported)
        self.sidebar_state = SidebarCompositeState(send=send_initial, tool_loop=None, audio=AudioRecorderState(status="idle"))
        # Hands-free Record is a panel flag. The send FSM stays timer-free.
        self._record_gesture = RecordGesture()
        self._record_hold_gen = 0
        self._sticky_restart_gen = 0
        self._sticky_restart_pending = False
        # Empty transcripts in a row while sticky (EMPTY_TAKES_EXIT ends the loop).
        self._empty_take_count = 0
        # Set at the start of disposing so a drain still on the stack skips
        # status, TTS, and a sticky re-record after ctx is cleared.
        self._panel_teardown = False
        # services.events is not always global_event_bus (a second import can
        # hold another bus). disposing must unsubscribe the bus we joined.
        self._mcp_event_bus = None

        # Subscribe to MCP/tool bus events
        try:
            from plugin.main import get_tools
            from plugin.framework.event_bus import global_event_bus

            event_bus = getattr(get_tools()._services, "events", None)
            if event_bus:
                # Remember this object and unsubscribe it in disposing.
                # Unsubscribing global_event_bus leaves this subscription,
                # which keeps calling a closed panel.
                self._mcp_event_bus = event_bus
                event_bus.subscribe("mcp:request", self._on_mcp_request, weak=True)
                event_bus.subscribe("mcp:result", self._on_mcp_result, weak=True)
                log.debug(f"*** SendButtonListener subscribed to MCP events on services.events (id={id(event_bus)}) ***")
            global_event_bus.subscribe("grammar:status", self._on_grammar_status, weak=True)
        except Exception:
            log.exception("SendButtonListener event subscribe error")

    def set_rich_text_widget(self, widget: Any) -> None:
        """Enable RichTextControl sidebar rendering via hidden-doc formatted copy."""
        self.rich_text_widget = widget
        log.info("[RICH-CONTROL] SendButtonListener.set_rich_text_widget called")

    def rerender_rich_text_session(self) -> bool:
        """Paint the control from the session message list.

        Called after streaming completes so the hidden Writer shows the
        committed messages, not the plain chunks that were painted while the
        open row was growing.

        True only when that paint ran. False leaves the control as it is so a
        held stripper leftover can still be appended.
        """
        widget = getattr(self, "rich_text_widget", None)
        if widget is None:
            return False
        try:
            return bool(widget.rerender_last_assistant_if_html(
                self.session,
                getattr(self, "_assistant_stream_start_len", None),
            ))
        except Exception:
            log.exception("rerender_rich_text_session (rich control) failed")
            return False

    @property
    def stop_requested(self) -> bool:
        scope = getattr(self, "_send_cancellation", None)
        if scope is not None and scope.is_cancelled():
            return True
        return self._stop_requested_fallback

    @stop_requested.setter
    def stop_requested(self, value: bool) -> None:
        if value:
            scope = getattr(self, "_send_cancellation", None)
            if scope is not None:
                scope.cancel()
            self._stop_requested_fallback = True
        else:
            self._stop_requested_fallback = False

    def resolve_stop_checker(self) -> Callable[[], bool]:
        """Stop predicate bound to the scope on this panel right now.

        Call this on the send thread when spawning a worker (``capture_send_stop``)
        and close over the result. Calling it again inside the worker binds
        whatever ``_send_cancellation`` is then. The drain clears that field
        when it exits, and the next send stores a new scope there.

        The returned callable keeps the scope object from this call, so it
        stays true after the field is cleared. Do not pass
        ``lambda: self.stop_requested`` alone: that property reads the live field.
        See ``docs/framework/streaming-and-threading.md`` § Stop / cancellation.
        """
        from plugin.framework.queue_executor import bind_send_stop_checker

        return bind_send_stop_checker(getattr(self, "_send_cancellation", None), lambda: self._stop_requested_fallback)

    def _kill_inflight_stt(self) -> None:
        """Kill the Whisper child for the transcription that is running now.

        The scope hook covers Stop on the send that started STT. This covers
        Stop after a second send replaced ``_send_cancellation``: that click
        cancels the new scope, which does not own the first child.
        """
        kill = self._stt_kill
        if not callable(kill):
            return
        try:
            kill()
        except Exception:
            log.debug("STT kill failed", exc_info=True)

    def sync_audio_slice(self) -> None:
        """Mirror :attr:`audio_recorder.state` into the composite (strategy A)."""
        import dataclasses

        if self.audio_recorder is None:
            return
        self.sidebar_state = dataclasses.replace(self.sidebar_state, audio=self.audio_recorder.state)

    def set_session(self, session: Any) -> None:
        """Swap the visible session after the in-flight turn has been aborted.

        A mode change used to replace ``host.session`` while the turn was
        still alive, and the next bind wrote the reply onto the transcript
        just shown. Abort first. The turn keeps the session it started with.
        """
        from plugin.chatbot.tool_loop_actions import abort_turn

        abort_turn(self)
        self.session = session
        self.client = None  # Force client recreation if needed, though they usually share same config

    def on_brainstorming_session_finished(self, spec_saved: bool = False) -> None:
        """Reset sidebar after brainstorming_finished (dropdown transitions to Writing Plan or Chat)."""
        from plugin.chatbot.chat_sidebar_mode import (
            CHAT_MODE_CHAT,
            CHAT_MODE_WRITING_PLAN,
            clear_brainstorming_session,
            set_selector_mode_with_flags,
        )

        flags = getattr(self, "sidebar_mode_flags", None)
        # Read _brainstorming_topic before clear_brainstorming_session blanks
        # it, or the writing-plan handoff is always "Implement the saved spec: ".
        # selectItemPos often does not fire ChatModeListener, so the combo
        # moves and the sidebar stays in the old mode until the user touches it.
        # Apply the mode here.
        topic = getattr(self, "_brainstorming_topic", "") or ""
        clear_brainstorming_session(self)
        apply_fn = getattr(self, "_apply_sidebar_mode_fn", None)
        if spec_saved:
            self._in_writing_plan_mode = True
            self._writing_plan_topic = f"Implement the saved spec: {topic}"
            if self.chat_mode_selector and flags:
                set_selector_mode_with_flags(self.chat_mode_selector, CHAT_MODE_WRITING_PLAN, flags)
            if callable(apply_fn):
                apply_fn(CHAT_MODE_WRITING_PLAN)
        else:
            if self.chat_mode_selector and flags:
                set_selector_mode_with_flags(self.chat_mode_selector, CHAT_MODE_CHAT, flags)
            if callable(apply_fn):
                apply_fn(CHAT_MODE_CHAT)

    def on_librarian_session_finished(self) -> None:
        """Reset sidebar after switch_to_document_mode (dropdown returns to Chat). History is kept."""
        from plugin.chatbot.chat_sidebar_mode import (
            CHAT_MODE_CHAT,
            clear_librarian_session,
            set_selector_mode_with_flags,
        )

        flags = getattr(self, "sidebar_mode_flags", None)
        clear_librarian_session(self)
        if self.chat_mode_selector and flags:
            set_selector_mode_with_flags(self.chat_mode_selector, CHAT_MODE_CHAT, flags)
        # Do not rely on ComboBox item-changed: swap to doc_session, re-render Chat
        # pane, and refresh [DOCUMENT CONTENT] for the next send.
        apply_fn = getattr(self, "_apply_sidebar_mode_fn", None)
        if callable(apply_fn):
            apply_fn(CHAT_MODE_CHAT)

    def on_writing_plan_session_finished(self) -> None:
        """Reset sidebar after writing_plan_finished (dropdown returns to Chat)."""
        from plugin.chatbot.chat_sidebar_mode import (
            CHAT_MODE_CHAT,
            clear_writing_plan_session,
            set_selector_mode_with_flags,
        )

        flags = getattr(self, "sidebar_mode_flags", None)
        clear_writing_plan_session(self)
        if self.chat_mode_selector and flags:
            set_selector_mode_with_flags(self.chat_mode_selector, CHAT_MODE_CHAT, flags)
        # Same as librarian: the combo change does not reliably run ChatModeListener.
        apply_fn = getattr(self, "_apply_sidebar_mode_fn", None)
        if callable(apply_fn):
            apply_fn(CHAT_MODE_CHAT)

    def on_ppt_master_session_finished(self, exported: bool = False) -> None:
        """Reset sidebar after ppt_master_finished (dropdown returns to Chat)."""
        from plugin.chatbot.chat_sidebar_mode import (
            CHAT_MODE_CHAT,
            clear_ppt_master_session,
            set_selector_mode_with_flags,
        )

        del exported
        flags = getattr(self, "sidebar_mode_flags", None)
        clear_ppt_master_session(self)
        if self.chat_mode_selector and flags:
            set_selector_mode_with_flags(self.chat_mode_selector, CHAT_MODE_CHAT, flags)
        apply_fn = getattr(self, "_apply_sidebar_mode_fn", None)
        if callable(apply_fn):
            apply_fn(CHAT_MODE_CHAT)

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
        if getattr(self, "_approval_event", None) is not None:
            log.warning("begin_inline_web_approval: superseding pending approval")
            self._finish_inline_web_approval(False)
        self._approval_event = event
        self._approval_query_for_engine = query
        self._approval_ui_backup = {}
        with suppress_disposed("begin_inline_web_approval backup", logger=log):
            if self.send_control and self.send_control.getModel():
                m = self.send_control.getModel()
                self._approval_ui_backup["send_label"] = m.Label
                self._approval_ui_backup["send_enabled"] = m.Enabled
            if self.stop_control and self.stop_control.getModel():
                m = self.stop_control.getModel()
                self._approval_ui_backup["stop_label"] = m.Label
                self._approval_ui_backup["stop_enabled"] = m.Enabled
            if self.clear_control and self.clear_control.getModel():
                cm = self.clear_control.getModel()
                self._approval_ui_backup["clear_enabled"] = cm.Enabled
                self._approval_ui_backup["clear_label"] = cm.Label
            if self.status_control:
                self._approval_ui_backup["status_text"] = self.status_control.getText()

        with suppress_disposed("begin_inline_web_approval", logger=log):
            if self.send_control and self.send_control.getModel():
                m = self.send_control.getModel()
                _relabel_button(self.send_control, _("Accept"), self._mnemonic_cache())
                m.Enabled = True
            if self.stop_control and self.stop_control.getModel():
                m = self.stop_control.getModel()
                _relabel_button(self.stop_control, _("Change"), self._mnemonic_cache())
                m.Enabled = True
            if self.clear_control and self.clear_control.getModel():
                m = self.clear_control.getModel()
                _relabel_button(self.clear_control, _("Reject"), self._mnemonic_cache())
                m.Enabled = True

        # Approval is inline (Accept / Change / Reject); search preview is already in the transcript.
        self._set_status(_("Waiting for approval…"))
        log.info("Inline web approval: waiting for Accept, Change, or Reject")

    def _open_web_search_change_dialog(self) -> None:
        """Open edit dialog for the pending web_search query; OK continues with optional override."""
        from plugin.chatbot.dialogs import show_web_search_query_edit_dialog

        initial = getattr(self, "_approval_query_for_engine", None) or ""
        text = show_web_search_query_edit_dialog(self.ctx, self.frame, initial)
        if text is None:
            return
        log.debug("_open_web_search_change_dialog: applying edited query len=%d", len(text))
        self._finish_inline_web_approval(True, query_override=text)

    def _finish_inline_web_approval(self, approved: bool, query_override: str | None = None) -> None:
        ev = getattr(self, "_approval_event", None)
        if ev is None:
            return
        self._approval_event = None
        self._approval_query_for_engine = None
        b: dict[str, Any] = self._approval_ui_backup or {}
        self._approval_ui_backup = None
        with suppress_disposed("_finish_inline_web_approval restore", logger=log):
            if self.send_control and self.send_control.getModel():
                m = self.send_control.getModel()
                if "send_label" in b:
                    _relabel_button(self.send_control, b["send_label"], self._mnemonic_cache())
                if "send_enabled" in b:
                    m.Enabled = b["send_enabled"]
            if self.stop_control and self.stop_control.getModel():
                m = self.stop_control.getModel()
                if "stop_label" in b:
                    _relabel_button(self.stop_control, b["stop_label"], self._mnemonic_cache())
                if "stop_enabled" in b:
                    m.Enabled = b["stop_enabled"]
            if self.clear_control and self.clear_control.getModel() and "clear_enabled" in b:
                cm = self.clear_control.getModel()
                cm.Enabled = b["clear_enabled"]
                if "clear_label" in b:
                    _relabel_button(self.clear_control, b["clear_label"], self._mnemonic_cache())
            if self.status_control and "status_text" in b:
                self.status_control.setText(b["status_text"])
        try:
            ev.approved = approved
            ev.query_override = query_override if approved else None
            if approved and query_override is not None:
                log.debug("_finish_inline_web_approval: approved with query_override len=%d", len(query_override))
            ev.set()
        except Exception:
            log.exception("_finish_inline_web_approval threading event error")

    def _set_status(self, text: str) -> None:
        """Update the status field in the sidebar (read-only TextField).
        Uses setText() (XTextComponent) to write directly to the control/peer,
        bypassing model→view notifications which can desync after document edits."""
        with suppress_disposed(f"_set_status({text})", logger=log):
            if self.status_control:
                self.status_control.setText(text)
            else:
                log.debug("_set_status: NO CONTROL for '%s'" % text)

    def _on_grammar_status(self, **data: Any) -> None:
        """Show native grammar proofreader progress in the sidebar status field."""
        if getattr(self, "_panel_teardown", False) or self.ctx is None:
            return
        # grammar:status is process-global. A Writer proofreader pass used to
        # paint "Grammar: …" on every open sidebar, including Calc and Draw.
        if getattr(self, "cached_doc_type", None) != "writer":
            return
        if self._send_busy or self._approval_event is not None:
            return
        text = format_grammar_status(data)
        try:
            from plugin.framework.queue_executor import post_to_main_thread
            from plugin.framework.thread_guard import get_background_task_name, on_main_thread

            if on_main_thread() and not get_background_task_name():
                self._set_status(text)
            else:
                post_to_main_thread(self._set_status, text)
        except Exception as e:
            log.debug("_on_grammar_status: post_to_main_thread failed: %s", e)
            return

    def _scroll_response_to_bottom(self) -> None:
        """Scroll the response area to show the bottom (newest content).
        Uses XTextComponent.setSelection to place caret at end, which scrolls the view."""
        with suppress_disposed("_scroll_response_to_bottom", logger=log):
            if self.response_control:
                model = self.response_control.getModel()
                if model and hasattr(self.response_control, "setSelection"):
                    text = model.Text or ""
                    length = len(text)
                    self.response_control.setSelection(uno.createUnoStruct("com.sun.star.awt.Selection", length, length))

    '''
    def _get_scrollbar(self):
        ...  # commented out — scrollbar was never found in embedded frames
    '''

    def _should_auto_scroll(self) -> bool:
        """Always returns True for now — forces scroll to bottom on every append.

        Future: implement sticky scroll by reading VCL scrollbar position and
        returning False when user has manually scrolled up.
        """
        return True

    def _run_rich_ui(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        """Run rich-control UI work inline on the main thread; post from workers."""
        if threading.current_thread() is threading.main_thread():
            return fn(*args, **kwargs)
        self.queue_executor.post(fn, *args, **kwargs)

    def render_session_messages(self, session: Any) -> None:
        """Draw the sidebar from ``session.messages``."""
        def _paint() -> None:
            widget = getattr(self, "rich_text_widget", None)
            if widget is not None:
                widget.paint_session(session)
                return
            control = getattr(self, "response_control", None)
            if control is None or not control.getModel():
                return
            from plugin.chatbot.dialogs import set_control_text
            from plugin.chatbot.rich_text_paste import plain_transcript_text

            set_control_text(control, plain_transcript_text(session))
            if self._should_auto_scroll():
                self._scroll_response_to_bottom()

        run_ui = getattr(self, "_run_rich_ui", None)
        if callable(run_ui):
            run_ui(_paint)
        else:
            _paint()

    def _project_closing_line(self, text: str) -> None:
        """Write a closing line onto this turn's session after ``abort``.

        The drain aborts before the send ``finally`` can report an exception.
        ``_append_response`` then treats the line as a late chunk. The turn
        still owns the list until a newer send or Clear replaces it.
        """
        from plugin.chatbot.tool_loop_actions import TurnController, current_turn

        turn = current_turn(self)
        if isinstance(turn, TurnController) and not turn.same_messages():
            return
        if isinstance(turn, TurnController) and turn.fold_chunk(self, text, "assistant"):
            if getattr(self, "session", None) is turn.session and turn.session is not None:
                self.render_session_messages(turn.session)
            return
        self._append_response(text)

    def _start_local_error_turn(self, text: str) -> None:
        from plugin.chatbot.tool_loop_actions import begin_send_turn, drop_turn, running_turn

        # A live send owns the response area: append to it rather than abort it.
        if running_turn(self) is not None:
            self._append_response(text)
            return
        begin_send_turn(self, "")
        self._append_response(text)
        self._terminal_status = "Error"
        drop_turn(self)

    def _append_response(self, text: str, is_thinking: bool = False, role: str = "assistant") -> None:
        """Project ``text`` onto the session, then draw that list.

        Chunks land only while this turn is alive. Stop writes its line
        through the turn, so a stop string after abort is not a special
        paint. A message list with no live turn is not written. A unit host
        with no list still concatenates the plain control.
        """
        from plugin.chatbot.tool_loop_actions import TurnController, current_turn

        turn = current_turn(self)
        session = getattr(self, "session", None)
        messages = getattr(session, "messages", None) if session is not None else None
        has_list = isinstance(messages, list)
        if has_list:
            if (
                not isinstance(turn, TurnController)
                or not turn.alive
                or not turn.accepts_history(self)
                or turn.session is not session
            ):
                return
        elif isinstance(turn, TurnController) and not turn.alive:
            return
        with suppress_disposed("_append_response", logger=log):
            widget = getattr(self, "rich_text_widget", None)
            if widget:
                from plugin.chatbot.rich_text_control import skip_legacy_assistant_stream_chunk
                from plugin.framework.logging import note_activity

                log.debug("_append_response: rich-control len=%d role=%s", len(text) if text else 0, role)
                # "AI:" / "Using chat model" are plain-sidebar labels. The paint
                # writes Assistant: from the message list, so they are not rows.
                if role != "user" and skip_legacy_assistant_stream_chunk(text):
                    return
                clean_text = _chunk_text(turn, text, role, strip_non_assistant=False)
                note_activity()
                # A held tag fragment is not a list change. Drawing now would
                # rebuild the same paint.
                if role == "assistant" and not clean_text:
                    return

                def _paint_from_list() -> None:
                    # Captured at send time. A post that runs after Stop or a
                    # new send dropped this turn must not fold into the next one.
                    if (
                        not isinstance(turn, TurnController)
                        or current_turn(self) is not turn
                        or not turn.alive
                        or not turn.same_messages()
                    ):
                        return
                    if not turn.fold_chunk(self, clean_text, role):
                        return
                    if role != "user" and getattr(self, "_record_assistant_start", False):
                        self._record_assistant_start = False
                        self._assistant_stream_start_len = widget.get_text_length()
                        log.debug(
                            "_append_response: rich-control stream start len=%s (final answer)",
                            self._assistant_stream_start_len,
                        )
                    # Append, do not repaint. paint_session on every
                    # streamed batch (about 3 a second) builds a hidden Writer
                    # doc, clears the control, and refills the whole transcript.
                    # VCL then draws from a stale layout: a blank transcript, or
                    # a gap below the last line that grows with the session.
                    # stream_session appends only the new text. The full
                    # repaint stays for load/switch, Stop, and Clear.
                    widget.stream_session(turn.session)
                    if role == "user":
                        self._assistant_stream_start_len = widget.get_text_length()
                        log.debug(
                            "_append_response: rich-control stream start len=%s",
                            self._assistant_stream_start_len,
                        )

                self._run_rich_ui(_paint_from_list)
                return

            if not getattr(self, "_rich_plain_fallback_warned", False):
                from plugin.framework.config import get_config_bool_safe

                if get_config_bool_safe("rich_text_control_sidebar"):
                    log.warning(
                        "[RICH-CONTROL] _append_response plain fallback while rich_text_control_sidebar enabled",
                    )
                    self._rich_plain_fallback_warned = True

            if self.response_control and self.response_control.getModel():
                from plugin.chatbot.dialogs import get_control_text, set_control_text
                from plugin.chatbot.rich_text_control import skip_legacy_assistant_stream_chunk
                from plugin.framework.logging import note_activity

                # A session list is the transcript. Hosts with no list (unit
                # tests) still append onto the control.
                if has_list:
                    if role != "user" and skip_legacy_assistant_stream_chunk(text):
                        return
                    clean_text = _chunk_text(turn, text, role, strip_non_assistant=True)
                    note_activity()
                    if role == "assistant" and not clean_text:
                        return
                    if isinstance(turn, TurnController):
                        turn.fold_chunk(self, clean_text, role)
                    self.render_session_messages(session)
                    return

                should_scroll = self._should_auto_scroll()
                current = get_control_text(self.response_control) or ""
                clean_text = _chunk_text(turn, text, role, strip_non_assistant=True)
                set_control_text(self.response_control, current + clean_text)
                if should_scroll:
                    self._scroll_response_to_bottom()

    def _on_mcp_request(self, tool: str = "", args: Any = None, method: Any = None, **kwargs: Any) -> None:
        """Handle MCP request events from the bus (background thread)."""
        if getattr(self, "_panel_teardown", False) or self.ctx is None:
            return

        try:

            from plugin.chatbot.tool_loop_actions import current_turn

            rid = str(kwargs.get("req_id", ""))
            self._last_mcp_turn = {k: v for k, v in self._last_mcp_turn.items() if getattr(v, "alive", False)}
            self._last_mcp_turn[rid] = current_turn(self)
            from plugin.framework.logging import format_tool_call_for_display

            fmt_str = format_tool_call_for_display(tool, args, method)
            log.debug(f"MCP Request (hidden from UI, level=logging.DEBUG): {fmt_str}")
        except Exception:
            log.exception("_on_mcp_request error")

    def _on_mcp_result(self, tool: str = "", result_snippet: str = "", **kwargs: Any) -> None:
        """Handle MCP result events from the bus (background thread)."""
        # Drop the result once teardown has started or ctx is cleared.
        # The bus callback and the queued UI hop are different turns, so a
        # result posted before disposing still runs _append_response after
        # the panel is gone.
        if self._panel_teardown or self.ctx is None:
            return

        # Each request is tied to its originating TurnController in
        # _last_mcp_turn[rid]. Gating on kwargs["req_id"] == self._last_mcp_req_id
        # drops the earlier of two concurrent MCP requests: _on_mcp_request
        # overwrites _last_mcp_req_id with the newest id.
        try:
            from plugin.chatbot.tool_loop_actions import TurnController, current_turn

            rid = str(kwargs.get("req_id", ""))
            last_turn = self._last_mcp_turn.pop(rid, None)
            if not isinstance(last_turn, TurnController) or current_turn(self) is not last_turn or not last_turn.alive:
                return
        except Exception:
            return

        def _update_ui() -> None:
            if self._panel_teardown or self.ctx is None:
                return
            try:
                from plugin.chatbot.tool_loop_actions import TurnController, current_turn
                if not isinstance(last_turn, TurnController) or current_turn(self) is not last_turn or not last_turn.alive:
                    return
            except Exception:
                return
            try:
                from plugin.framework.logging import format_tool_result_for_display

                fmt_str = format_tool_result_for_display(tool, result_snippet, args=kwargs.get("args"))
                self._append_response(f"[MCP Result] {fmt_str}\n")
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

    def _get_document_model(self) -> Any | None:
        """Get the document model strictly from the frame.

        Always prefers the document bound to this sidebar's frame (same window as the user)
        instead of ``Desktop.getCurrentComponent()``, which can point at the wrong
        document if focus changes.
        """
        from plugin.framework.uno_context import get_document_from_frame

        model = get_document_from_frame(self.frame)

        _COMPATIBLE_DOC_TYPES = frozenset({"writer", "calc", "draw", "impress"})
        cached_doc_type = getattr(self, "cached_doc_type", None)

        if model and cached_doc_type in _COMPATIBLE_DOC_TYPES:
            return model

        # Only log when chat send will fail (same moment as the sidebar error message).
        detail_parts = [
            "has_frame=%s" % bool(self.frame),
            "cached_doc_type=%s" % cached_doc_type,
            "model_probe=%s" % _uno_model_probe_for_log(model, cached_doc_type=cached_doc_type),
        ]
        if model is not None:
            detail_parts.append("reject_reason=unsupported_or_uncached_doc_type probe=%s" % _uno_model_probe_for_log(model, cached_doc_type=cached_doc_type))
        log.error("SendButtonListener: no compatible document model for chat (%s)", "; ".join(detail_parts))
        return None

    def _mnemonic_cache(self) -> dict[str, str]:
        """Plain label -> label with the mnemonic VCL gave it (see _relabel_button)."""
        cache = getattr(self, "_button_mnemonics", None)
        if cache is None:
            cache = {}
            self._button_mnemonics = cache
        return cache

    def _set_button_states(self, send_enabled: bool, stop_enabled: bool) -> None:
        """Set Send/Stop enabled flags (per-control try/except so one UNO failure cannot strand the other)."""
        if self.send_control and self.send_control.getModel():
            with suppress_disposed("set send_control enabled state", logger=log):
                self.send_control.getModel().Enabled = bool(send_enabled)
        if self.stop_control and self.stop_control.getModel():
            with suppress_disposed("set stop_control enabled state", logger=log):
                self.stop_control.getModel().Enabled = bool(stop_enabled)

    def dispatch(self, event: Any) -> None:
        """Dispatch an event to the state machine, compute new state, and apply effects."""
        kind = getattr(event, "kind", None)
        # Stop and aborting errors must drop sticky before SEND_COMPLETED, or
        # the turn-end hook would arm the mic again.
        if kind in (SendEventKind.STOP_CLICKED, SendEventKind.CANCEL_REC_CLICKED, SendEventKind.ERROR_OCCURRED):
            self.exit_hands_free_record()
        was_busy = self.sidebar_state.send.is_busy
        tr = sidebar_next_state(self.sidebar_state, SidebarEvent(kind=SidebarEventKind.SEND, payload=event))
        self.sidebar_state = tr.state
        self._send_busy = self.sidebar_state.send.is_busy

        # Nested dispatch during an effect (e.g. Record start failure) must run
        # after remaining effects. RECORD_CLICKED emits UpdateUIEffect after
        # StartRecordingEffect; a nested ERROR_OCCURRED used to restore Stop Rec
        # on a listener that was no longer recording.
        reenter: list[Any] = []
        self._dispatch_reenter = reenter
        try:
            for effect in tr.effects:
                self._interpret_effect(effect)
        finally:
            self._dispatch_reenter = None
        for nested in reenter:
            self.dispatch(nested)
        # Flag only. The drain flushes after speak_text_async so a voice reply
        # is already speaking (or known idle) before we touch the mic.
        if kind == SendEventKind.SEND_COMPLETED and was_busy and not self.sidebar_state.send.is_busy and self._record_gesture.sticky:
            self._sticky_restart_pending = True

    def exit_hands_free_record(self) -> None:
        """Drop sticky so the next reply does not arm Record again."""
        was_sticky = self._record_gesture.sticky
        self._record_gesture = exit_sticky(self._record_gesture)
        self._sticky_restart_pending = False
        self._sticky_restart_gen += 1
        self._record_hold_gen += 1
        self._empty_take_count = 0
        if was_sticky:
            log.info("Hands-free record cleared")

    def stop_during_take(self) -> bool:
        """Stop while a take is recording. True when this click was handled.

        One step down per click. Stop disabled during every take leaves a
        hands-free loop (a take between turns) with no exit short of Clear
        (which wipes the chat) or closing the sidebar. Stop Rec cannot be
        the exit: it is the send, and loud rooms need it when silence never
        fires. A locked take becomes a one-shot take that keeps recording
        (Stop Rec or silence sends it, nothing re-arms); Stop on a one-shot
        take cancels it without sending.
        """
        from plugin.framework.i18n import _

        if not self.sidebar_state.send.is_recording:
            return False
        if stop_during_take(sticky=self._record_gesture.sticky) == TakeStop.EXIT_LOCK:
            self.exit_hands_free_record()
            self._set_status(_("Hands-free off"))
        else:
            log.info("Stop during take: recording cancelled")
            self.dispatch(SendEvent(SendEventKind.CANCEL_REC_CLICKED))
        return True

    def _apply_record_gesture(self, step: Any) -> None:
        was_sticky = self._record_gesture.sticky
        self._record_gesture = step.gesture
        if step.gesture.sticky and not was_sticky:
            log.info("Hands-free record armed")
        if step.cancel_timer:
            self._record_hold_gen += 1
        if step.start_timer:
            self._arm_record_hold_timer()
        if step.dispatch_record:
            _stop_tts()
            self.dispatch(SendEvent(SendEventKind.RECORD_CLICKED))

    def _arm_record_hold_timer(self) -> None:
        self._record_hold_gen += 1
        self._spawn_record_hold_wait(self._record_hold_gen)

    def _spawn_record_hold_wait(self, gen: int) -> None:
        """Sleep off the UI thread, then hop back through QueueExecutor.

        ``dedicated`` so a 2s hold does not pin a shared pool worker. The
        generation is checked on the UI thread; releasing early just bumps it.
        """
        import time

        from plugin.framework.worker_pool import run_in_background

        delay_s = RECORD_HOLD_MS / 1000.0

        def _wait() -> None:
            time.sleep(delay_s)
            try:
                self.queue_executor.post(self._on_record_hold_elapsed, gen)
            except Exception:
                log.debug("record hold timer post failed", exc_info=True)

        run_in_background(_wait, name="record-hold", dedicated=True)

    def _on_record_hold_elapsed(self, gen: int) -> None:
        if gen != self._record_hold_gen:
            return
        self._apply_record_gesture(gesture_hold_elapsed(self._record_gesture))

    def _release_open_microphone(self) -> None:
        """Stop capture and leave the Stop Rec label.

        Clear and sidebar close must stop the mic, not only
        exit_hands_free_record. Leaving AudioRecorder running keeps the
        take: recording is not is_busy, so STOP_CLICKED does not apply, and
        the next click sends it. ERROR_OCCURRED clears is_recording without
        starting a send.
        """
        recorder = getattr(self, "audio_recorder", None)
        if recorder is not None:
            try:
                recorder.cleanup()
            except Exception:
                log.debug("audio recorder cleanup failed", exc_info=True)
        if self.sidebar_state.send.is_recording:
            try:
                self.dispatch(SendEvent(SendEventKind.ERROR_OCCURRED))
            except Exception:
                log.debug("leave Stop Rec on teardown failed", exc_info=True)

    def _flush_sticky_restart(self) -> None:
        """Arm Record again if this completed turn left sticky on."""
        pending = self._sticky_restart_pending
        self._sticky_restart_pending = False
        if not pending or not self._record_gesture.sticky:
            return
        self._begin_sticky_rerecord()

    def _begin_sticky_rerecord(self) -> None:
        send = self.sidebar_state.send
        if send.is_busy or send.is_recording or not send.audio_supported:
            return
        speaking = False
        try:
            from plugin.audio.tts_service import is_speaking

            speaking = bool(is_speaking())
        except Exception:
            log.debug("hands-free re-record: is_speaking failed", exc_info=True)
        decision = sticky_restart(sticky=True, speaking=speaking)
        if decision == StickyRestart.RECORD:
            log.info("Hands-free record restarting")
            self.dispatch(SendEvent(SendEventKind.RECORD_CLICKED))
        elif decision == StickyRestart.WAIT_FOR_TTS:
            self._sticky_restart_gen += 1
            log.debug("Hands-free record waiting for TTS")
            self._spawn_sticky_tts_wait(self._sticky_restart_gen)

    def _spawn_sticky_tts_wait(self, gen: int) -> None:
        """Poll ``is_speaking`` later. Do not sleep on the UNO thread."""
        import time

        from plugin.framework.worker_pool import run_in_background

        delay_s = STICKY_TTS_POLL_MS / 1000.0

        def _wait() -> None:
            time.sleep(delay_s)
            try:
                self.queue_executor.post(self._poll_sticky_rerecord, gen)
            except Exception:
                log.debug("hands-free TTS wait post failed", exc_info=True)

        run_in_background(_wait, name="sticky-rerecord", dedicated=True)

    def _poll_sticky_rerecord(self, gen: int) -> None:
        if gen != self._sticky_restart_gen or not self._record_gesture.sticky:
            return
        self._begin_sticky_rerecord()

    def _on_audio_auto_stop(self) -> None:
        """Silence detector ended capture; same FSM path as clicking Stop Rec (stop + send).

        Hands-free does not replace this. The sticky flag stays set so the
        reply's ``SEND_COMPLETED`` can arm Record again.
        """
        # A post can already be queued when disposing clears the recorder hooks.
        if self._panel_teardown or self.ctx is None:
            return
        if not self.sidebar_state.send.is_recording:
            log.info("audio auto-stop ignored (not recording)")
            return
        log.info("Audio silence pause detected — treating as Stop Rec")
        self.dispatch(SendEvent(SendEventKind.STOP_REC_CLICKED))

    def _on_audio_recorder_error(self, msg: str) -> None:
        """Child error after ready, on the UI thread.

        The stdout monitor only posts here. Applying the recorder event on
        that thread raised out of ReportErrorEffect and left the button on
        Stop Rec. ERROR_OCCURRED drops the recording label.
        """
        if self._panel_teardown or self.ctx is None:
            return
        recorder = self.audio_recorder
        if recorder is not None:
            recorder.apply_stdout_error(msg)
        self._start_local_error_turn("\n[Audio error: %s]\n" % msg)
        self.dispatch(SendEvent(SendEventKind.ERROR_OCCURRED))
        self.sync_audio_slice()

    def _on_audio_silence_progress(self, silence_ms: int) -> None:
        from plugin.framework.i18n import _

        if self._panel_teardown or self.ctx is None:
            return
        if self.sidebar_state.send.is_recording:
            if self._record_gesture.sticky:
                self._set_status(hands_free_silence_text(silence_ms))
            else:
                self._set_status(_("Recording audio… (%d ms silence)") % silence_ms)

    def _interpret_effect(self, effect: Any) -> None:
        """Interpret a state machine effect and apply side-effects."""
        from plugin.framework.i18n import _

        match effect:
            case LogSidebarEffect():
                log.debug("%s", effect.message)
            case UpdateUIEffect():
                # Preserve button states and labels while approval is
                # pending. Web search approval replaces Send/Stop/Clear with
                # Accept/Change/Reject; a late UpdateUIEffect writes Accept
                # back to Send (disabled).
                if not _is_approval_pending(self):
                    self._set_button_states(effect.send_enabled, effect.stop_enabled)

                    if self.send_control and self.send_control.getModel():
                        with suppress_disposed("relabel send_control", logger=log):
                            _relabel_button(self.send_control, _(effect.send_label), self._mnemonic_cache())

                if effect.status_text is not None and effect.status_text != "":
                    # Sticky takes share the Record transition; only the status line differs.
                    if effect.status_text == RECORDING_STATUS and self._record_gesture.sticky:
                        self._set_status(hands_free_status_text())
                    else:
                        self._set_status(_(effect.status_text))

            case StartRecordingEffect():
                _stop_tts()
                if not self.audio_recorder:
                    return
                try:
                    self.audio_recorder.start_recording()
                except RuntimeError as re:
                    self._start_local_error_turn("\n[Audio error: %s]\n" % str(re))
                    pending = getattr(self, "_dispatch_reenter", None)
                    if pending is not None:
                        pending.append(SendEvent(SendEventKind.ERROR_OCCURRED))
                    else:
                        self.dispatch(SendEvent(SendEventKind.ERROR_OCCURRED))
                self.sync_audio_slice()

            case StopRecordingEffect():
                if not self.audio_recorder:
                    return
                try:
                    self.audio_wav_path = self.audio_recorder.stop_recording()
                except Exception as e:
                    from plugin.framework.errors import WriterAgentException

                    if isinstance(e, WriterAgentException):
                        log.exception("WriterAgentException stopping recording")
                    else:
                        log.exception("Error stopping recording")
                self.sync_audio_slice()

            case CancelRecordingEffect():
                # cleanup() stops capture and deletes the temp WAV; a stopped
                # take's path (if any) goes too, so no later send attaches it.
                if self.audio_recorder:
                    try:
                        self.audio_recorder.cleanup()
                    except Exception:
                        log.debug("audio recorder cleanup on cancel failed", exc_info=True)
                    self.sync_audio_slice()
                self.clear_pending_audio_wav()

            case StartSendEffect():
                from plugin.framework.queue_executor import SendCancellation

                # Leave the first scope in place until that child exits.
                # STT runs inside run_blocking_in_thread, which pumps the UI.
                # A second Send replaces _send_cancellation and clears the
                # stop fallback while the first Whisper child is still alive,
                # so Stop for the first send does not kill it.
                if getattr(self, "_stt_inflight", False):
                    log.info("StartSend ignored while speech-to-text is running")
                    return

                _stop_tts()

                self._stop_requested_fallback = False
                self._terminal_status = "Ready"
                # Create the scope before posting so a Stop click between Send
                # returning and the AsyncCallback drain still latches cancel.
                # Do not bind_executor yet: cancel() would cancel_pending_work
                # and drop this posted drain, so SEND_COMPLETED never runs and
                # the button stays Stop. Bind inside _run_send_drain instead.
                scope: Any = SendCancellation()
                self._send_cancellation = scope
                # Bug: drain used to run inside Send actionPerformed. On GTK,
                # processEventsToIdle from that stack does not deliver a second
                # dialog ActionEvent, so Stop looked enabled but never fired
                # (Packet B ramble completed all 200 words). Post to the next
                # VCL tick so the listener returns first.
                self.queue_executor.post(self._run_send_drain)

            case StopSendEffect():
                log.info("Stop clicked (cancel in-flight send)")
                try:
                    session = getattr(self, "frame_session", None)
                    if session is not None:
                        session.note_user_wants_query()
                        if hasattr(session, "restore_focus"):
                            session.restore_focus()
                except Exception as e:
                    log.debug("query setFocus on Stop: %s", e)
                from plugin.chatbot.tool_loop_actions import abort_turn

                # Drop later worker callbacks. The drain still closes this
                # turn: the stop line is written onto the session, then the
                # send drain forgets the controller.
                abort_turn(self)
                _stop_tts()
                scope = getattr(self, "_send_cancellation", None)
                if scope is not None:
                    scope.cancel()

                # AI/DEV INVARIANT: Do NOT clear audio_wav_path or kill in-flight STT here.
                # If Stop is clicked while recording or transcribing, we want speech-to-text to finish
                # and populate the query box so the user's spoken words are preserved and not discarded.
                # The STT client is not registered with the send scope, so this Stop
                # does not abort transcription HTTP requests.

                self._stop_requested_fallback = True
                from plugin.doc.peer_message import drop_listener_queue

                drop_listener_queue(self)

            case _:
                log.debug("SendButtonListener: unhandled effect type %s", type(effect).__name__)

    def on_action_performed(self, rEvent: Any) -> None:
        from plugin.framework.i18n import _

        if not self.send_control or not self.send_control.getModel():
            return

        if getattr(self, "_approval_event", None) is not None and self.send_control and self.send_control.getModel():
            if self.send_control.getModel().Label == _("Accept"):
                self._finish_inline_web_approval(True)
                return
        btn_model = self.send_control.getModel()
        label = btn_model.Label

        # Mouse press/hold/release owns Record. ActionEvent still runs, and by
        # then the label may already say Stop Rec — handling it here would send
        # the take that the long-press just started.
        action_step = gesture_action(self._record_gesture)
        self._record_gesture = action_step.gesture
        if action_step.swallowed_action:
            return

        if label == _("Record"):
            _stop_tts()
            self.dispatch(SendEvent(SendEventKind.RECORD_CLICKED))
        elif label == _("Stop Rec"):
            self.dispatch(SendEvent(SendEventKind.STOP_REC_CLICKED))
        elif label == _("Send"):
            _stop_tts()
            self.dispatch(SendEvent(SendEventKind.SEND_CLICKED))

    # _transcribe_audio_async is provided by SendHandlersMixin.

    def _sync_has_text_from_query(self) -> None:
        """Ask is the source of truth after SEND_COMPLETED forces has_text False.

        Ask is the source of truth after completion. Completion always
        clears has_text, so text typed while a reply streamed, and the
        extracted-peer path that never clears Ask, then look empty. With
        recording support the button becomes Record. The FSM still forces
        False so a missed text listener cannot leave has_text true on an
        empty box. This event corrects it from the control.
        """
        ctrl = getattr(self, "query_control", None)
        if ctrl is None:
            return
        try:
            from plugin.chatbot.dialogs import get_control_text

            text = get_control_text(ctrl) or ""
        except Exception:
            log.debug("has_text sync skipped", exc_info=True)
            return
        self.dispatch(SendEvent(SendEventKind.TEXT_UPDATED, {"has_text": bool(str(text).strip())}))

    def _run_send_drain(self) -> None:
        """Run ``_do_send`` on a VCL tick after Send ``actionPerformed`` returns."""
        from plugin.framework.queue_executor import agent_session

        # A drain posted before StartSend learned STT was in flight must not
        # clear the first send's scope or dispatch SEND_COMPLETED under it.
        if getattr(self, "_stt_inflight", False):
            log.info("Nested send drain ignored during speech-to-text")
            return

        # The event-driven drain returns before the stream ends. Exiting
        # agent_session and running SEND_COMPLETED on that return would clear
        # the stop scope and abort the turn while chunks are still landing.
        # defer_until_drain_done runs this close on the terminal slice; the
        # blocking drain has already finished, so it runs now.
        from plugin.framework.async_stream import clear_drain_capture, defer_until_drain_done

        # Another document's drain may still be open; this send must not
        # defer its completion onto it.
        clear_drain_capture()
        sess = getattr(self, "session", None)
        self._send_start_msg_count = len(sess.messages) if (sess and getattr(sess, "messages", None) is not None) else 0
        cm = agent_session(getattr(self, "_send_cancellation", None))
        entered = False
        exit_error: list[BaseException | None] = [None]
        try:
            cancel_scope = cm.__enter__()
            entered = True
            # Safe now: this callback is already running, not a pending post.
            cancel_scope.bind_executor(self.queue_executor)
            self._send_cancellation = cancel_scope
            if cancel_scope.is_cancelled() or self._stop_requested_fallback:
                log.info("Send drain skipped (Stop before drain started)")
                # Terminal status is "Stopped" so completion and TTS skip
                # this turn. Leaving it at "Ready" replays the previous
                # answer when Stop is pressed before the drain begins.
                self._terminal_status = "Stopped"
                # Drop the take. STT never starts on this path, so it
                # cannot be kept as text. Stop Rec stores it on
                # audio_wav_path; if Stop lands before this drain runs,
                # nothing consumes the WAV and the next typed Send
                # transcribes or attaches it.
                self.clear_pending_audio_wav()
                return
            self._do_send()
        except Exception as e:
            exit_error[0] = e
            doc_type_for_log = getattr(self, "initial_doc_type", "unknown")
            log.exception("SendButton unhandled exception [doc: %s]", doc_type_for_log)
        finally:
            def _close_send_drain() -> None:
                err = exit_error[0]
                if entered:
                    try:
                        if err is None:
                            cm.__exit__(None, None, None)
                        else:
                            cm.__exit__(type(err), err, err.__traceback__)
                    except Exception:
                        log.exception("send drain agent_session close failed")
                if err is not None:
                    # The tool-loop epilogue already aborted the turn, so
                    # _append_response would treat this as a late chunk.
                    self._project_closing_line("\n\n[Error: %s]\n" % str(err))
                    self._terminal_status = "Error"
                self._finish_send_drain_ui()

            defer_until_drain_done(_close_send_drain)

    def _finish_send_drain_ui(self) -> None:
        """SEND_COMPLETED, TTS, and drop_turn after the stream drain is done."""
        from plugin.framework.i18n import _

        update_activity_state("")
        # Dispose runs inside this drain, then sets ctx to None. The
        # completion dispatch writes the status line, and the rest starts
        # TTS or arms the mic on a dead panel.
        # Drop _send_cancellation only while the panel is still alive.
        # An inner finally that clears it after disposing cancelled that
        # scope leaves a late reader with None on a dead panel.
        try:
            if not self._panel_teardown:
                self._send_cancellation = None
                if self._terminal_status == "Error":
                    self.clear_pending_audio_wav()
                    self.dispatch(SendEvent(SendEventKind.ERROR_OCCURRED))
                else:
                    self.dispatch(SendEvent(SendEventKind.SEND_COMPLETED))
                    self._sync_has_text_from_query()
                    if self._terminal_status:
                        self._set_status(_(self._terminal_status))
                    try:
                        from plugin.framework.config import get_config_bool_safe
                        # Speak from session_for_turn(self) (valid until
                        # drop_turn) when TTS is enabled and terminal status
                        # is not Stopped. An alive-turn check fails here: the
                        # tool-loop finally already ran abort_turn(self).
                        if get_config_bool_safe("audio.tts_enabled") and self._terminal_status != "Stopped":
                            from plugin.chatbot.tool_loop_actions import session_for_turn

                            spoken = session_for_turn(self)
                            start_count = getattr(self, "_send_start_msg_count", None)
                            if spoken and spoken.messages:
                                last_msg = spoken.messages[-1]
                                # Speak only when an assistant message
                                # exists and this send appended a new message.
                                # A whitespace-only query or a cancelled send
                                # otherwise replays the previous turn: the
                                # session's last message is still that reply.
                                has_new_assistant_msg = (
                                    last_msg.get("role") == "assistant"
                                    and bool(last_msg.get("content"))
                                    and (start_count is None or len(spoken.messages) > start_count)
                                )
                                if has_new_assistant_msg:
                                    from plugin.chatbot.tool_loop_actions import _STOP_LINE
                                    content_to_speak = last_msg["content"].replace(_STOP_LINE, "")
                                    if content_to_speak.strip():
                                        from plugin.audio.tts_service import speak_text_async, is_speaking

                                        # Restore the send-complete status after download/fallback lines.
                                        prior_status = self._terminal_status or "Ready"

                                        def _on_tts_status(message: str) -> None:
                                            # Speech runs on a worker; the status control is a UNO widget.
                                            def _apply() -> None:
                                                self._set_status(message)

                                            try:
                                                self.queue_executor.post(_apply)
                                            except Exception:
                                                log.debug("TTS status post failed", exc_info=True)

                                        def _on_speech_complete() -> None:
                                            def _disable_stop() -> None:
                                                if not getattr(self, "_send_busy", False):
                                                    # A take may already be recording (barge-in or sticky
                                                    # restart); its Stop is the step-down exit.
                                                    if self.stop_control and self.stop_control.getModel() and not self.sidebar_state.send.is_recording:
                                                        with suppress_disposed("disable stop after speech", logger=log):
                                                            self.stop_control.getModel().Enabled = False
                                                    # A sticky restart may already be capturing; Ready would hide it.
                                                    if self._record_gesture.sticky and self.sidebar_state.send.is_recording:
                                                        self._set_status(hands_free_status_text())
                                                    else:
                                                        self._set_status(_(prior_status))
                                            self.queue_executor.post(_disable_stop)

                                        speak_text_async(
                                            content_to_speak,
                                            on_complete=_on_speech_complete,
                                            on_status=_on_tts_status,
                                            # Sentence breaks use BreakIterator on this UI
                                            # thread. The audio worker only receives the list.
                                            ctx=self.ctx,
                                        )
                                        if is_speaking():
                                            if self.stop_control and self.stop_control.getModel():
                                                with suppress_disposed("enable stop for speech", logger=log):
                                                    self.stop_control.getModel().Enabled = True
                    except Exception as e:
                        log.debug("TTS playback trigger: %s", e)
                    self._flush_sticky_restart()
        finally:
            # drop_turn and kick_pending_peer_starts run under finally.
            # An early return when content_to_speak is empty (or a TTS
            # exception) skips them and leaks the turn, including Stop
            # with TTS enabled.
            from plugin.chatbot.tool_loop_actions import drop_turn, session_for_turn
            from plugin.doc.peer_message import kick_pending_peer_starts

            # This runs once per turn, from the deferred drain-done close
            # (_close_send_drain), on success, Stop and error alike. A slice
            # returning mid-stream does not reach here, so the audio the model
            # is still answering is not stripped early.
            sess = session_for_turn(self) or getattr(self, "session", None)
            if sess and getattr(sess, "messages", None):
                # Ensure in-memory audio is not resent on the next turn.
                # Iterate backwards to find the last user message.
                for msg in reversed(sess.messages):
                    if msg.get("role") == "user":
                        content = msg.get("content")
                        if isinstance(content, list) and any(c.get("type") == "input_audio" for c in content):
                            kept_parts = [c for c in content if c.get("type") != "input_audio"]
                            if kept_parts:
                                msg["content"] = kept_parts
                            else:
                                msg["content"] = _("[Voice message]")
                        break

            # Spoken text was copied above. Later callbacks must not find this turn.
            drop_turn(self)
            kick_pending_peer_starts()
            self._send_start_msg_count = None

    def _get_doc_type_str(self, model: Any) -> str:
        from plugin.doc.doc_type import doc_type_title_for_label

        return doc_type_title_for_label(getattr(self, "cached_doc_type", None))

    def _restore_query_text(self, text: str) -> None:
        """Put text back in the Ask box after a send returns early (Stop, STT error).

        _do_send clears the Ask box before transcription, so every early return
        must hand the typed text (plus any transcript) back or it is lost.
        """
        if self.query_control and self.query_control.getModel():
            from plugin.chatbot.dialogs import set_control_text
            set_control_text(self.query_control, text)

    def _do_send(self) -> None:
        from plugin.framework.i18n import _
        from plugin.chatbot.tool_loop_actions import begin_send_turn

        # begin_send_turn aborts the turn already in flight. A pump re-entry
        # during Whisper must not do that, and must not clear the WAV.
        # Missing means not transcribing, same as the mixin default.
        # Reading self._stt_inflight raises AttributeError on the smol
        # _do_send double: that SimpleNamespace never ran __init__.
        if getattr(self, "_stt_inflight", False):
            log.info("_do_send re-entered during speech-to-text; the first Stop still applies")
            return

        # The turn exists before any worker and before early error rows.
        # Mode and the document are filled in once this send knows them.
        begin_send_turn(self, "")
        self._set_status(_("Starting..."))
        update_activity_state("do_send", status_control=getattr(self, "status_control", None))
        log.info("=== _do_send START ===")

        # Ensure extension directory is on sys.path (injected by panel_factory to avoid circular import)
        if self.ensure_path_fn:
            self.ensure_path_fn(self.ctx)

        # 1. Get document model
        self._set_status(_("Getting document..."))
        log.debug("_do_send: getting document model...")
        model = self._get_document_model()
        if not model:
            self._append_response("\n" + _("[No compatible LibreOffice document (Writer, Calc, or Draw) found in the active window.]") + "\n")
            self._terminal_status = "Error"
            return
        log.debug("_do_send: got document model OK")

        doc_type_label = getattr(self, "cached_doc_type", None)
        log.debug("_do_send: document type (cached): %s" % doc_type_label)

        if not doc_type_label or doc_type_label == "unknown":
            err_msg = _("[Internal Error: Could not identify document type for {0}. Please report this!]").format(model.getImplementationName() if hasattr(model, "getImplementationName") else "Unknown")
            log.error("_do_send ERROR: %s", err_msg)
            self._append_response("\n%s\n" % err_msg)
            self._terminal_status = "Error"
            return

        # Get user query and clear field (before loading tools, so direct-image path can return early)
        query_text = ""
        if self.query_control and self.query_control.getModel():
            from plugin.chatbot.dialogs import get_control_text

            query_text = (get_control_text(self.query_control) or "").strip()

        # Audio implies we have input even if text is empty.
        # Stop Rec sets has_audio and always starts a send. A missing WAV used
        # to clear _terminal_status and return, so the click looked like it did
        # nothing (no Whisper, no chat) after the stop handshake lost the file.
        if not query_text and not self.audio_wav_path:
            if self.sidebar_state.send.has_audio:
                log.warning("_do_send: Stop Rec finished without a WAV path")
                self._append_response("\n" + _("[Audio error: recording stopped without a sound file.]") + "\n")
                self._terminal_status = "Error"
                self._set_status(_("Error"))
                return
            # Mark the terminal status "Ready" so the UI updates, and
            # rely on message-count tracking to suppress TTS when no new
            # message was added. An empty-query return that leaves
            # _terminal_status as "" skips _set_status in
            # _finish_send_drain_ui (status stays "Getting document…") and
            # speaks the prior turn's reply.
            self._terminal_status = "Ready"
            return

        if self.query_control and self.query_control.getModel():
            from plugin.chatbot.dialogs import set_control_text

            set_control_text(self.query_control, "")
            # Send button click leaves focus on Send; keep the query field
            # ready for the next question (reveal/scroll must not win later).
            try:
                session = getattr(self, "frame_session", None)
                if session is not None:
                    session.note_user_wants_query()
                if hasattr(self.query_control, "setFocus"):
                    self.query_control.setFocus()
            except Exception as e:
                log.debug("query setFocus after send: %s", e)

        try:

            from plugin.chatbot.config_ui_helpers import sync_sidebar_text_model

            sync_sidebar_text_model(self.ctx, self.model_selector)

            # Transcription Fallback check
            if self.audio_wav_path:
                from plugin.audio.stt_service import uses_local_stt
                from plugin.framework.client.model_fetcher import get_stt_model, get_text_model, has_native_audio
                from plugin.framework.config import get_current_endpoint

                current_model = get_text_model()
                current_endpoint = get_current_endpoint()

                # Local Whisper is the user's STT choice: transcribe in the venv and
                # send text, including when the chat model could take input_audio.
                # Endpoint STT still waits until the chat model cannot take audio.
                local_stt = uses_local_stt()
                if local_stt or has_native_audio(current_model, current_endpoint) is False:
                    stt_model = get_stt_model()
                    if local_stt or stt_model:
                        if local_stt:
                            log.info("_do_send: local Whisper STT (chat model %s)" % current_model)
                        else:
                            log.warning("_do_send: model %s has no native audio, using stt fallback %s" % (current_model, stt_model))
                        try:
                            transcript = self._transcribe_audio(self.audio_wav_path, stt_model)
                            if self._terminal_status == "Stopped":
                                new_text = (query_text + "\n" + transcript).strip() if (query_text and transcript) else (transcript or query_text)
                                self._restore_query_text(new_text)
                                self._sync_has_text_from_query()
                                return
                            if transcript:
                                self._empty_take_count = 0
                                query_text = (query_text + "\n" + transcript).strip() if query_text else transcript
                        except Exception as e:
                            from plugin.framework.errors import NetworkError

                            if isinstance(e, NetworkError):
                                log.exception("NetworkError during STT fallback")
                            else:
                                log.exception("Error during STT fallback")
                            self._terminal_status = "Error"
                            self._restore_query_text(query_text)
                            return
                        # WAV is deleted in _transcribe_audio finally. Empty STT must not
                        # fall through into a chat POST with a blank user message (G27).
                        if not query_text.strip():
                            self._append_response("\n" + _("[No speech detected.]") + "\n")
                            self._terminal_status = "Stopped"
                            # End hands-free after EMPTY_TAKES_EXIT empty
                            # takes in a row. An empty take finishes as
                            # Stopped, not Error, so sticky re-arms Record
                            # and noise that trips silence auto-stop loops
                            # empty takes forever.
                            gesture = getattr(self, "_record_gesture", None)
                            if gesture is not None and gesture.sticky:
                                self._empty_take_count = getattr(self, "_empty_take_count", 0) + 1
                                if self._empty_take_count >= EMPTY_TAKES_EXIT:
                                    log.info("Hands-free off after %d empty takes", self._empty_take_count)
                                    self.exit_hands_free_record()
                                    self._append_response(_("[Hands-free off: no speech heard.]") + "\n")
                            return
                    else:
                        err_msg = _("[Model {0} does not support native audio. Please select an STT Model in Settings.]").format(current_model)
                        self._append_response("\n%s\n" % err_msg)
                        self._terminal_status = "Error"
                        self._set_status(_("Error"))
                        self._restore_query_text(query_text)
                        return
                else:
                    log.debug("_do_send: model %s supports native audio, proceeding" % current_model)
                    if self._terminal_status == "Stopped":
                        self._restore_query_text(query_text)
                        return

            from plugin.chatbot.chat_sidebar_mode import (
                CHAT_MODE_BRAINSTORMING,
                CHAT_MODE_DEEP_RESEARCH,
                CHAT_MODE_IMAGE,
                CHAT_MODE_LIBRARIAN,
                CHAT_MODE_PPT_MASTER,
                CHAT_MODE_WEB_RESEARCH,
                CHAT_MODE_WRITING_PLAN,
                mode_from_selector_with_flags,
                sidebar_mode_flags_for_doc_type,
            )

            flags = getattr(self, "sidebar_mode_flags", None) or sidebar_mode_flags_for_doc_type(doc_type_label or "writer")
            sidebar_mode = mode_from_selector_with_flags(self.chat_mode_selector, flags)
            from plugin.chatbot.tool_loop_actions import TurnController, current_turn

            # Mode and the document are arguments of the turn already started.
            # A later dropdown change aborts it; it does not retarget the turn.
            started = current_turn(self)
            if isinstance(started, TurnController):
                started.mode = str(sidebar_mode or "")
                started.model = model

            if sidebar_mode == CHAT_MODE_LIBRARIAN:
                log.info("_do_send: using librarian onboarding agent")
                self._run_librarian(query_text, model)
                return

            if sidebar_mode == CHAT_MODE_WEB_RESEARCH:
                log.info("_do_send: using web research sub-agent — skip chat model and direct image")
                self._run_web_research(query_text, model)
                return

            if sidebar_mode == CHAT_MODE_DEEP_RESEARCH:
                log.info("_do_send: using deep web research sub-agent — skip chat model and direct image")
                self._run_deep_web_research(query_text, model)
                return

            if sidebar_mode == CHAT_MODE_IMAGE:
                log.debug("_do_send: using image model (direct, level=logging.INFO) — skip chat model")
                self._do_send_direct_image(query_text, model)
                return

            if sidebar_mode == CHAT_MODE_BRAINSTORMING and doc_type_label == "writer":
                if not self._brainstorming_topic:
                    self._brainstorming_topic = query_text
                log.info("_do_send: using brainstorming sub-agent")
                self._run_brainstorming(query_text, model)
                return

            if sidebar_mode == CHAT_MODE_WRITING_PLAN and doc_type_label == "writer":
                if not getattr(self, "_writing_plan_topic", None):
                    self._writing_plan_topic = query_text
                log.info("_do_send: using writing plan sub-agent")
                self._run_writing_plan(query_text, model)
                return

            if sidebar_mode == CHAT_MODE_PPT_MASTER and doc_type_label in ("draw", "impress"):
                if not getattr(self, "_ppt_master_topic", None):
                    self._ppt_master_topic = query_text
                log.info("_do_send: using PPT-Master sub-agent")
                self._run_ppt_master(query_text, model)
                return

            # Agent backend (Aider, Hermes): use external agent instead of built-in LLM.
            # Show the error and end the send here. The handler documents
            # no builtin fallback. Leaving `_do_send_via_agent_backend` in
            # this try means the except only logs, then execution falls
            # through to `_do_send_chat_with_tools` and one Send starts a
            # second builtin turn.
            try:
                from plugin.framework.config import get_config
                from plugin.acp.registry import normalize_backend_id

                agent_backend_id = normalize_backend_id(get_config("agent_backend.backend_id"))
                if agent_backend_id and agent_backend_id != "builtin":
                    log.info("_do_send: using agent backend %s" % agent_backend_id)
                    self._do_send_via_agent_backend(query_text, model, doc_type_label)
                    return
            except Exception as exc:
                log.exception("_do_send: agent backend check failed")
                self._append_response("\n" + _("[Agent backend error: {0}]").format(str(exc)) + "\n")
                self._terminal_status = "Error"
                self._set_status(_("Error"))
                self._restore_query_text(query_text)
                return

            # Regular Chat with Tools or Streams
            # Cast to Any to satisfy ty since SendButtonListener mixes in multiple protocol hosts
            getattr(self, "_do_send_chat_with_tools")(query_text, model, doc_type_label)

        except Exception:
            self._restore_query_text(query_text)
            raise
    def start_extracted_peer_send(self, query_text: str, *, already_appended: bool) -> bool:
        """Start a peer-injected turn. Caller must not hold a drain owner.

        Does not read/clear Ask, setFocus, or route librarian/image.
        """
        from plugin.framework.async_drain_guard import get_drain_owner

        if get_drain_owner() is not None:
            return False
        if self.sidebar_state.send.is_busy:
            return False
        self._extracted_peer_query = query_text
        self._extracted_peer_already_appended = already_appended
        self.dispatch(SendEvent(SendEventKind.EXTRACTED_SEND))
        if not self.sidebar_state.send.is_busy:
            return False
        self._run_extracted_peer_drain()
        return True

    def _run_extracted_peer_drain(self) -> None:
        """Same completion FSM as ``_run_send_drain``, without Ask-box ``_do_send``.

        We keep this separate from ``_run_send_drain``, not one shared drain,
        because the Ask-box path also owns TTS, pending WAV and ``_stt_inflight``
        handling that a peer turn must not trigger.
        """
        from plugin.framework.i18n import _
        from plugin.framework.async_stream import clear_drain_capture, defer_until_drain_done
        from plugin.framework.queue_executor import SendCancellation, agent_session

        clear_drain_capture()

        query_text = getattr(self, "_extracted_peer_query", "") or ""
        already_appended = bool(getattr(self, "_extracted_peer_already_appended", True))
        self._stop_requested_fallback = False
        self._terminal_status = "Ready"
        scope = SendCancellation()
        self._send_cancellation = scope
        cm = agent_session(scope)
        entered = False
        exit_error: list[BaseException | None] = [None]
        try:
            cancel_scope = cm.__enter__()
            entered = True
            cancel_scope.bind_executor(self.queue_executor)
            self._send_cancellation = cancel_scope
            if not (cancel_scope.is_cancelled() or self._stop_requested_fallback):
                self._do_send_extracted_peer(query_text, already_appended=already_appended)
        except Exception as e:
            exit_error[0] = e
            doc_type_for_log = getattr(self, "initial_doc_type", "unknown")
            log.exception("Extracted peer send unhandled exception [doc: %s]", doc_type_for_log)
        finally:
            def _close_peer_drain() -> None:
                # Clear the scope only once the drain has finished. Doing it
                # when the event-driven call returns would drop Stop for the
                # rest of the turn.
                if entered and not getattr(self, "_panel_teardown", False):
                    self._send_cancellation = None
                err = exit_error[0]
                if entered:
                    try:
                        if err is None:
                            cm.__exit__(None, None, None)
                        else:
                            cm.__exit__(type(err), err, err.__traceback__)
                    except Exception:
                        log.exception("peer drain agent_session close failed")
                if err is not None:
                    self._project_closing_line("\n\n[Error: %s]\n" % str(err))
                    self._terminal_status = "Error"
                update_activity_state("")
                # Same teardown guard as _run_send_drain: completion writes status
                # and can arm the mic after the sidebar is gone.
                try:
                    if not self._panel_teardown:
                        if self._terminal_status == "Error":
                            self.dispatch(SendEvent(SendEventKind.ERROR_OCCURRED))
                        else:
                            self.dispatch(SendEvent(SendEventKind.SEND_COMPLETED))
                            self._sync_has_text_from_query()
                            # Ty: _terminal_status defaults to "Ready", which is unconditionally true.
                            # _run_send_drain may leave it "" to keep the label as-is, but extracted
                            # peer ignores those paths. Avoid the redundant if-check.
                            self._set_status(_(self._terminal_status))
                            self._flush_sticky_restart()
                finally:
                    # Same inner finally as _run_send_drain. Drop the turn
                    # after SEND_COMPLETED, never before, so the next peer
                    # turn starts only once this one is finished. An exception
                    # from the completion dispatch otherwise skips drop_turn
                    # and kick_pending_peer_starts, leaking the turn and
                    # stalling queued peer turns.
                    from plugin.chatbot.tool_loop_actions import drop_turn
                    from plugin.doc.peer_message import kick_pending_peer_starts

                    drop_turn(self)
                    # We kick inline here, not via post like _on_drain_idle, because
                    # SEND_COMPLETED and drop_turn already ran; nesting is bounded by
                    # one full peer turn per hop.
                    kick_pending_peer_starts()

            defer_until_drain_done(_close_peer_drain)

    def _do_send_extracted_peer(self, query_text: str, *, already_appended: bool) -> None:
        """Force chat-with-tools. No Ask read/clear, no setFocus, no librarian/image."""
        from plugin.framework.i18n import _
        from plugin.chatbot.chat_sidebar_mode import (
            CHAT_MODE_CHAT,
            mode_from_selector_with_flags,
            sidebar_mode_flags_for_doc_type,
        )

        from plugin.chatbot.tool_loop_actions import TurnController, begin_send_turn, current_turn

        begin_send_turn(self, CHAT_MODE_CHAT)
        self._set_status(_("Starting..."))
        update_activity_state("do_send", status_control=getattr(self, "status_control", None))
        if self.ensure_path_fn:
            self.ensure_path_fn(self.ctx)
        model = self._get_document_model()
        if not model:
            self._append_response("\n" + _("[No compatible LibreOffice document (Writer, Calc, or Draw) found in the active window.]") + "\n")
            self._terminal_status = "Error"
            return
        doc_type_label = getattr(self, "cached_doc_type", None)
        if not doc_type_label or doc_type_label == "unknown":
            self._append_response("\n[Internal Error: Could not identify document type.]\n")
            self._terminal_status = "Error"
            return
        flags = getattr(self, "sidebar_mode_flags", None) or sidebar_mode_flags_for_doc_type(doc_type_label or "writer")
        sidebar_mode = mode_from_selector_with_flags(self.chat_mode_selector, flags)
        if sidebar_mode != CHAT_MODE_CHAT:
            self._append_response(
                "\n" + _("[Peer send requires Chat mode on this sidebar.]") + "\n"
            )
            self._terminal_status = "Error"
            return
        started = current_turn(self)
        if isinstance(started, TurnController):
            started.mode = CHAT_MODE_CHAT
            started.model = model
        if not already_appended:
            self.session.add_user_message(query_text)
            self._append_response(query_text, role="user")
        getattr(self, "_do_send_chat_with_tools")(
            query_text, model, doc_type_label, skip_append_user=True
        )

    # _do_send_direct_image is provided by SendHandlersMixin.

    # _do_send_chat_with_tools is provided by ToolCallingMixin.

    # _do_send_via_agent_backend is provided by SendHandlersMixin.

    # Writer edit selection uses WriterStreamedRewriteSession (document compound undo). Broader
    # chat/tool undo grouping is still future work.

    # _run_web_research is provided by SendHandlersMixin.

    @property
    def _sm_state(self) -> Any:
        # Idle panel has tool_loop=None; mixin raises when a session is required.
        return self.sidebar_state.tool_loop

    @_sm_state.setter
    def _sm_state(self, value: Any) -> None:
        import dataclasses

        self.sidebar_state = dataclasses.replace(self.sidebar_state, tool_loop=value)

    def disposing(self, Source: Any = None) -> None:
        _stop_tts()
        # Detach both mouse listeners on dispose. attach_stop_mouse_listener
        # and attach_record_mouse_listener add listeners that strongly hold
        # send_listener; leaving them attached leaks the listener.
        stop_mouse = getattr(self, "_stop_mouse_listener", None)
        stop_ctrl = getattr(self, "_stop_mouse_control", None) or self.stop_control
        if stop_mouse is not None and stop_ctrl is not None:
            self._stop_mouse_listener = None
            self._stop_mouse_control = None
            if hasattr(stop_ctrl, "removeMouseListener"):
                with suppress_disposed("remove stop mouse listener on dispose", logger=log):
                    stop_ctrl.removeMouseListener(stop_mouse)

        rec_mouse = getattr(self, "_record_mouse_listener", None)
        rec_ctrl = getattr(self, "_record_mouse_control", None) or self.send_control
        if rec_mouse is not None and rec_ctrl is not None:
            self._record_mouse_listener = None
            self._record_mouse_control = None
            if hasattr(rec_ctrl, "removeMouseListener"):
                with suppress_disposed("remove record mouse listener on dispose", logger=log):
                    rec_ctrl.removeMouseListener(rec_mouse)

        # Dispose can run on a later VCL turn while an event-driven drain
        # session is still open. Set the flag first so _finish_send_drain_ui
        # does not write status or start TTS after ctx is cleared. Cancel the
        # send scope (same object already captured by resolve_stop_checker) so
        # the drain stop checker fires instead of streaming into a dead panel.
        # Match StopSendEffect: cancel the scope and latch the fallback.
        self._panel_teardown = True
        self._last_mcp_turn.clear()
        # Drop silence auto-stop lambdas before cleanup. They stay on the
        # recorder and post UI work after this listener is gone, and stop
        # then re-enters the panel.
        recorder = getattr(self, "audio_recorder", None)
        if recorder is not None:
            try:
                recorder.set_auto_stop_callbacks(
                    on_auto_stop=None,
                    on_silence_progress=None,
                    on_error=None,
                )
            except Exception:
                log.debug("SendButtonListener.disposing: clear audio callbacks failed", exc_info=True)
        from plugin.chatbot.tool_loop_actions import abort_turn

        abort_turn(self)
        scope = getattr(self, "_send_cancellation", None)
        if scope is not None:
            scope.cancel()
        self._stop_requested_fallback = True
        self._kill_inflight_stt()
        self._release_open_microphone()
        self.exit_hands_free_record()
        self.clear_pending_audio_wav()
        try:
            from plugin.doc.peer_message import drop_listener_queue

            drop_listener_queue(self)
        except Exception as e:
            log.debug("SendButtonListener.disposing: drop peer queue: %s", e)
        try:
            from plugin.framework.event_bus import global_event_bus

            # Unsubscribe the bus saved at subscribe time. mcp:request /
            # mcp:result subscribed on services.events and unsubscribed on
            # global_event_bus stay subscribed when those are different
            # objects. grammar:status was subscribed on global_event_bus
            # and stays on that bus.
            mcp_bus = getattr(self, "_mcp_event_bus", None)
            if mcp_bus is not None:
                mcp_bus.unsubscribe("mcp:request", self._on_mcp_request)
                mcp_bus.unsubscribe("mcp:result", self._on_mcp_result)
            global_event_bus.unsubscribe("grammar:status", self._on_grammar_status)
        except Exception as e:
            log.debug("SendButtonListener.disposing: error unsubscribing from event bus: %s", e)
        finally:
            self._mcp_event_bus = None
            self.panel = None
            self.ctx = None



# ---------------------------------------------------------------------------
# StopButtonListener - allows user to cancel the AI request
# ---------------------------------------------------------------------------


def notify_stop_mouse_entered(send_listener: Any = None) -> None:
    """Hovering Stop: do not restore this frame's Ask field on the next stream chunk."""
    session = getattr(send_listener, "frame_session", None)
    if session is not None:
        session.note_user_left_query()


def notify_stop_mouse_pressed(send_listener: Any) -> None:
    """Stop mousePressed: drop query restore and cancel if a send is in flight.

    Bug: stream SelectAll called ``query.setFocus()`` every chunk. That aborts
    the Stop ``ActionEvent`` on GTK (Packet B1: no ``STOP_CLICKED`` in the log,
    ramble ran to word199). mousePressed is earlier; latching cancel here is
    belt-and-suspenders if ActionEvent still never fires. Change/Reject during
    web-search approval stays on ActionEvent — do not treat those as Stop.
    """
    session = getattr(send_listener, "frame_session", None)
    if session is not None:
        session.note_user_left_query()
    if send_listener is None:
        return
    if getattr(send_listener, "_approval_event", None) is not None:
        return
    from plugin.audio.tts_service import is_speaking
    if is_speaking():
        _stop_tts()
        if not getattr(send_listener, "_send_busy", False):
            # Playback-only Stop does not dispatch STOP_CLICKED. Still leave
            # hands-free, or the TTS poll would arm the mic again.
            send_listener.exit_hands_free_record()
            if send_listener.stop_control and send_listener.stop_control.getModel():
                with suppress_disposed("disable stop on mousePressed speech stopped", logger=log):
                    send_listener.stop_control.getModel().Enabled = False
            return
    send = getattr(getattr(send_listener, "sidebar_state", None), "send", None)
    # Not busy includes a take: StopButtonListener's ActionEvent owns that
    # step-down. Acting here too would take two steps on one click.
    if send is None or not send.is_busy:
        return
    log.info("StopButtonListener: STOP_CLICKED (mousePressed)")
    send_listener.dispatch(SendEvent(SendEventKind.STOP_CLICKED))


def attach_stop_mouse_listener(stop_control: Any, send_listener: Any) -> None:
    """Deliver Stop during stream even when ActionEvent is swallowed."""
    if stop_control is None or not hasattr(stop_control, "addMouseListener"):
        return
    try:
        import unohelper
        from com.sun.star.awt import XMouseListener
    except ImportError:
        return

    # Detach the prior listener before attaching a new one. Repeated
    # attachments or stale sidebar instances otherwise retain mouse
    # listeners that strongly reference the send_listener.
    prior = getattr(send_listener, "_stop_mouse_listener", None)
    if prior is not None and hasattr(stop_control, "removeMouseListener"):
        with suppress_disposed("remove prior stop mouse listener", logger=log):
            stop_control.removeMouseListener(prior)

    class _StopMouse(unohelper.Base, XMouseListener):  # type: ignore[misc]
        def disposing(self, Source: Any) -> None:  # noqa: N802, N803 -- UNO signature
            return

        def mousePressed(self, e: Any) -> None:  # noqa: N802 -- UNO signature
            notify_stop_mouse_pressed(send_listener)

        def mouseReleased(self, e: Any) -> None:  # noqa: N802 -- UNO signature
            return

        def mouseEntered(self, e: Any) -> None:  # noqa: N802 -- UNO signature
            notify_stop_mouse_entered(send_listener)

        def mouseExited(self, e: Any) -> None:  # noqa: N802 -- UNO signature
            return

    try:
        listener = _StopMouse()
        stop_control.addMouseListener(listener)
        if send_listener is not None:
            send_listener._stop_mouse_listener = listener
            send_listener._stop_mouse_control = stop_control
    except Exception:
        log.exception("Stop mouse listener attach failed")


def _send_button_label(send_listener: Any) -> str:
    try:
        model = send_listener.send_control.getModel()
    except Exception:
        return ""
    if model is None:
        return ""
    return str(getattr(model, "Label", "") or "")


def notify_record_mouse_pressed(send_listener: Any) -> None:
    """Record mousePressed: arm the 2s hold timer. Do not start capture yet."""
    if send_listener is None:
        return
    from plugin.framework.i18n import _

    label_is_record = _send_button_label(send_listener) == _("Record")
    send_listener._apply_record_gesture(gesture_press(send_listener._record_gesture, label_is_record=label_is_record))


def notify_record_mouse_released(send_listener: Any) -> None:
    """Record mouseReleased: short click records once; long press already did."""
    if send_listener is None:
        return
    send_listener._apply_record_gesture(gesture_release(send_listener._record_gesture))


def attach_record_mouse_listener(send_control: Any, send_listener: Any) -> None:
    """Own Record press/hold/release so ActionEvent cannot double-start or instant-send."""
    if send_control is None or not hasattr(send_control, "addMouseListener"):
        return
    try:
        import unohelper
        from com.sun.star.awt import XMouseListener
    except ImportError:
        return

    # Detach the prior listener before attaching a new one. Repeated
    # attachments or stale sidebar instances otherwise retain mouse
    # listeners that strongly reference the send_listener.
    prior = getattr(send_listener, "_record_mouse_listener", None)
    if prior is not None and hasattr(send_control, "removeMouseListener"):
        with suppress_disposed("remove prior record mouse listener", logger=log):
            send_control.removeMouseListener(prior)

    class _RecordMouse(unohelper.Base, XMouseListener):  # type: ignore[misc]
        def disposing(self, Source: Any) -> None:  # noqa: N802, N803 -- UNO signature
            return

        def mousePressed(self, e: Any) -> None:  # noqa: N802 -- UNO signature
            notify_record_mouse_pressed(send_listener)

        def mouseReleased(self, e: Any) -> None:  # noqa: N802 -- UNO signature
            notify_record_mouse_released(send_listener)

        def mouseEntered(self, e: Any) -> None:  # noqa: N802 -- UNO signature
            return

        def mouseExited(self, e: Any) -> None:  # noqa: N802 -- UNO signature
            return

    try:
        listener = _RecordMouse()
        send_control.addMouseListener(listener)
        if send_listener is not None:
            send_listener._record_mouse_listener = listener
            send_listener._record_mouse_control = send_control
    except Exception:
        log.exception("Record mouse listener attach failed")


class StopButtonListener(BaseActionListener):
    """Listener for the Stop button - sets a flag in SendButtonListener to halt loops."""

    send_listener: Any

    def __init__(self, send_listener: Any) -> None:
        self.send_listener = send_listener

    def on_action_performed(self, rEvent: Any) -> None:
        if self.send_listener and getattr(self.send_listener, "_approval_event", None) is not None:
            from plugin.framework.i18n import _

            if self.send_listener.stop_control and self.send_listener.stop_control.getModel() and self.send_listener.stop_control.getModel().Label == _("Change"):
                self.send_listener._open_web_search_change_dialog()
                return
            if self.send_listener.stop_control and self.send_listener.stop_control.getModel() and self.send_listener.stop_control.getModel().Label == _("Reject"):
                self.send_listener._finish_inline_web_approval(False)
                return
        # During a take Stop steps down (leave hands-free, then cancel). Only
        # this ActionEvent path does it; mousePressed returns while not busy,
        # so one click is one step.
        # ``is True`` so a MagicMock host (tests) is not treated as recording.
        take_stop = getattr(self.send_listener, "stop_during_take", None)
        if callable(take_stop) and take_stop() is True:
            return
        if self.send_listener:
            from plugin.audio.tts_service import is_speaking
            if is_speaking():
                _stop_tts()
                if not getattr(self.send_listener, "_send_busy", False):
                    # Playback-only Stop does not dispatch STOP_CLICKED. Still leave
                    # hands-free, or the TTS poll would arm the mic again.
                    self.send_listener.exit_hands_free_record()
                    if self.send_listener.stop_control and self.send_listener.stop_control.getModel():
                        with suppress_disposed("disable stop on speech stopped", logger=log):
                            self.send_listener.stop_control.getModel().Enabled = False
                    return
            log.info("StopButtonListener: STOP_CLICKED")
            self.send_listener.dispatch(SendEvent(SendEventKind.STOP_CLICKED))


# ---------------------------------------------------------------------------
# ClearButtonListener - resets the conversation
# ---------------------------------------------------------------------------


class ClearButtonListener(BaseActionListener):
    """Listener for the Clear button - resets conversation history."""

    send_listener: Any
    session: Any
    response_control: Any
    status_control: Any
    greeting: str

    def __init__(self, session: Any, response_control: Any, status_control: Any, greeting: str = "", send_listener: Any = None) -> None:
        self.send_listener = send_listener
        self.session = session
        # NOTE: When enabling the experimental planning/todo tool, consider
        # attaching a session-scoped TodoStore to the SendButtonListener and
        # resetting it here on Clear so each conversation starts with an empty
        # task list, e.g.:
        #   from plugin.contrib.todo_store import TodoStore
        #   send_listener._todo_store = TodoStore()
        self.response_control = response_control
        self.status_control = status_control
        self.greeting = greeting

    def set_session(self, session: Any, greeting: str | None = None) -> None:
        """Update the active session and optionally the greeting used for clear."""
        self.session = session
        if greeting is not None:
            self.greeting = greeting

    def on_action_performed(self, rEvent: Any) -> None:
        _stop_tts()
        if self.send_listener is not None:
            self.send_listener.exit_hands_free_record()
            self.send_listener._release_open_microphone()
        if self.send_listener and getattr(self.send_listener, "_approval_event", None) is not None:
            self.send_listener._finish_inline_web_approval(False)
            return
        # Latch Stop before wiping messages. Clear on the UI thread while
        # a send drain is still active leaves the in-flight reply to append
        # onto that wiped chat.
        send_state = getattr(getattr(self.send_listener, "sidebar_state", None), "send", None)
        if self.send_listener is not None and send_state is not None and send_state.is_busy:
            self.send_listener.dispatch(SendEvent(SendEventKind.STOP_CLICKED))
        if self.send_listener is not None:
            from plugin.chatbot.tool_loop_actions import abort_turn

            # Abort before the list is replaced. Stop already aborted a busy
            # send; abort again when it was idle so a worker that outlived
            # the button cannot paint onto the new list.
            abort_turn(self.send_listener)
            # Reset mode topics so the next prompt sets a fresh one. Clear
            # wipes session history; leaving _brainstorming_topic,
            # _writing_plan_topic, and _ppt_master_topic lets _do_send reuse
            # the stale topic with an empty history.
            self.send_listener._brainstorming_topic = ""
            self.send_listener._writing_plan_topic = ""
            self.send_listener._ppt_master_topic = ""
        self.session.clear()

        greeting = self.greeting
        if not greeting:
            # Nothing sets .doc on the listener. Use a real doc source.
            model = self.send_listener._get_document_model() if self.send_listener else None
            try:
                from plugin.framework.prompts import get_greeting_for_document, DEFAULT_WRITER_GREETING
                greeting = get_greeting_for_document(model) or DEFAULT_WRITER_GREETING
            except Exception:
                greeting = "AI: I can edit or translate this document, or help you outline ideas. What would you like to work on?"

        if self.send_listener and self.send_listener.rich_text_widget:
            try:
                self.send_listener.rich_text_widget.clear_and_greeting(greeting)
            except Exception:
                log.exception("Error clearing RichTextControl sidebar")
                try:
                    ctrl = getattr(self.send_listener.rich_text_widget, "control", None)
                    if ctrl is not None:
                        from plugin.chatbot.dialogs import set_control_text

                        set_control_text(ctrl, greeting + "\n")
                except Exception:
                    pass
            if self.status_control:
                self.status_control.setText("")
            return

        if self.response_control and self.response_control.getModel():
            from plugin.chatbot.dialogs import set_control_text

            text = greeting + "\n" if greeting else ""
            set_control_text(self.response_control, text)
        if self.status_control:
            self.status_control.setText("")


# ---------------------------------------------------------------------------
# SettingsButtonListener - opens Settings dialog from sidebar
# ---------------------------------------------------------------------------


class SettingsButtonListener(BaseActionListener):
    """Listener for the Settings button in the Chat sidebar."""

    ctx: Any

    def __init__(self, ctx: Any = None) -> None:
        self.ctx = ctx

    def on_action_performed(self, rEvent: Any) -> None:
        from plugin.framework.main_shared import get_action_handler, open_dialog_safely

        handler = get_action_handler("main.settings")
        if handler:
            handler()
            return
        from plugin.chatbot.dialog_views import settings_box

        open_dialog_safely(settings_box, "Failed to open settings")


class ActionHandlerButtonListener(BaseActionListener):
    """Generic listener that delegates to an action handler registered in get_action_handler."""

    handler_id: str
    ctx: Any

    def __init__(self, handler_id: str, ctx: Any = None) -> None:
        self.handler_id = handler_id
        self.ctx = ctx

    def on_action_performed(self, rEvent: Any) -> None:
        from plugin.framework.main_shared import get_action_handler

        handler = get_action_handler(self.handler_id)
        if handler:
            handler()


class HamburgerButtonListener(BaseActionListener):
    """Listener for the Hamburger menu button in the Chat sidebar."""

    ctx: Any
    _frame: Any

    def __init__(self, ctx: Any = None, frame: Any = None) -> None:
        self.ctx = ctx
        self._frame = frame

    def on_action_performed(self, rEvent: Any) -> None:
        from plugin.chatbot.hamburger_menu import show_hamburger_menu

        button_ctrl = getattr(rEvent, "Source", None)
        show_hamburger_menu(self.ctx, self._frame, button_ctrl)




