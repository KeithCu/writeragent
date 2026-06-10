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

ChatSession holds conversation history. SendButtonListener drives the
streaming tool-calling loop. StopButtonListener and ClearButtonListener
are wired by panel_factory. UNO UI element factory and XDL wiring
remain in panel_factory.py.
"""

from __future__ import annotations

import logging
import threading
import uno
from plugin.chatbot.send_handlers import SendHandlersMixin
from plugin.chatbot.tool_loop import ToolCallingMixin

from plugin.framework.logging import update_activity_state
from plugin.framework.queue_executor import QueueExecutor
from plugin.chatbot.history_db import get_chat_history

# Recording available only if audio_recorder (and plugin/contrib/audio) is present
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from plugin.framework.client.llm_client import LlmClient

_AudioRecorderCls: type[Any] | None
try:
    from plugin.chatbot.audio_recorder import AudioRecorder as _AR

    _AudioRecorderCls = _AR
except ImportError:
    _AudioRecorderCls = None
HAS_RECORDING = _AudioRecorderCls is not None

# Default max tool rounds when not in config (get_api_config supplies chat_max_tool_rounds)
DEFAULT_MAX_TOOL_ROUNDS = 5


_GRAMMAR_STATUS_PREVIEW_CHARS = 10


def _clip_grammar_status_preview(s: str, max_len: int = _GRAMMAR_STATUS_PREVIEW_CHARS) -> str:
    """One-line snippet for the sidebar status field (short to save space)."""
    compact = " ".join(s.strip().split())
    if not compact:
        return "(empty)"
    if len(compact) <= max_len:
        return compact
    return f"{compact[:max_len]}…"


def _grammar_status_area(phase: str, result: str, preview: str) -> Literal["language", "grammar"]:
    """Sidebar label bucket: language-detection LLM / failures vs grammar pipeline."""
    if phase == "request" and result == "Detecting language":
        return "language"
    if phase == "failed" and preview.strip().lower() == "language detection":
        return "language"
    return "grammar"


def format_grammar_status(data: dict[str, Any]) -> str:
    """Format native grammar proofreader progress for the sidebar status field."""
    phase = str(data.get("phase") or "")
    preview_raw = str(data.get("preview") or "")
    result = str(data.get("result") or "")
    try:
        length = int(data.get("length") or 0)
    except Exception:
        length = 0
    elapsed = data.get("elapsed_ms")
    area = _grammar_status_area(phase, result, preview_raw)
    preview = _clip_grammar_status_preview(preview_raw)
    prefix = "Language:" if area == "language" else "Grammar:"
    if phase == "start":
        return f"{prefix} queued '{preview}' len {length}"
    if phase == "join":
        return f"{prefix} waiting '{preview}' len {length}"
    if phase == "request":
        verb = "detecting" if area == "language" else "checking"
        return f"{prefix} {verb} '{preview}' len {length}"
    if phase == "complete":
        suffix = result or "done"
        if elapsed is not None:
            suffix = f"{suffix}, {elapsed}ms"
        return f"{prefix} done '{preview}' len {length}: {suffix}"
    if phase == "done":
        suffix = result or "done"
        if elapsed is not None:
            suffix = f"{suffix}, {elapsed}ms"
        return f"{prefix} done '{preview}' len {length}: {suffix}"
    if phase == "timeout":
        return f"{prefix} still running '{preview}' len {length}: {result}"
    if phase == "skipped":
        return f"{prefix} skipped '{preview}' len {length}: {result}"
    if phase == "failed":
        return f"{prefix} failed '{preview}' len {length}: {result}"
    return f"{prefix} {phase or 'update'} '{preview}' len {length}"


# ---------------------------------------------------------------------------
# ChatSession - holds conversation history for multi-turn chat
# ---------------------------------------------------------------------------


class ChatSession:
    """Maintains the message history for one sidebar chat session."""

    tool_streamed_texts: dict[str, list[str]]

    def __init__(self, system_prompt=None, session_id=None):
        self.session_id = session_id
        self.db = None
        self.messages = []
        self.base_system_prompt = system_prompt or ""
        self.document_context = ""

        self.active_specialized_domain = None
        self.python_tool_domain = None
        self.tool_streamed_texts = {}

        if session_id:
            try:
                self.db = get_chat_history(session_id)
                self.messages = self.db.get_messages()
            except Exception as e:
                from plugin.framework.errors import WriterAgentException

                if isinstance(e, WriterAgentException):
                    log.error("ChatSession history load WriterAgentException: %s" % e)
                else:
                    log.error("ChatSession history load error: %s" % e)

        # If no history, or system prompt forced
        if not self.messages and self.base_system_prompt:
            self.set_system_context(self.base_system_prompt, "")
            if self.db:
                self.db.add_message("system", self.messages[0]["content"])

    def set_system_context(self, base_prompt, doc_text=""):
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

    def add_user_message(self, content):
        self.messages.append({"role": "user", "content": content})
        if self.db:
            self.db.add_message("user", content)

    def add_assistant_message(self, content=None, tool_calls=None, reasoning_replay=None):
        msg = {"role": "assistant"}
        if content:
            msg["content"] = content
        else:
            msg["content"] = ""
        if tool_calls:
            msg["tool_calls"] = tool_calls
        if reasoning_replay:
            msg.update(reasoning_replay)
        self.messages.append(msg)
        if self.db:
            # Only persist the text content to history; tool calls are ephemeral.
            self.db.add_message("assistant", content)

    def add_tool_result(self, tool_call_id, content):
        self.messages.append({"role": "tool", "tool_call_id": tool_call_id, "content": content})
        # Note: We do NOT persist tool results to history_db.
        # This keeps the persistent history clean of tool formatting requirements.

    def clear(self):
        """Reset to just the system prompt."""
        self.messages = []
        self.document_context = ""
        if self.db:
            self.db.clear()
            
        if self.base_system_prompt:
            self.set_system_context(self.base_system_prompt, "")
            if self.db:
                self.db.add_message("system", self.messages[0]["content"])


# ---------------------------------------------------------------------------
# QueryTextListener - dynamic button toggling
# ---------------------------------------------------------------------------

from plugin.chatbot.listeners import BaseActionListener, BaseKeyListener, BaseTextListener
from plugin.chatbot.audio_recorder_state import AudioRecorderState
from plugin.chatbot.send_state import SendButtonState, SendEvent, SendEventKind, StartRecordingEffect, StartSendEffect, StopRecordingEffect, StopSendEffect, UpdateUIEffect
from plugin.chatbot.sidebar_state import LogSidebarEffect, SidebarCompositeState, SidebarEvent, SidebarEventKind, sidebar_next_state

log = logging.getLogger(__name__)


def _uno_model_probe_for_log(model: Any) -> str:
    """Short UNO diagnostic for error logs. No document text."""
    if model is None:
        return "None"
    impl = "?"
    try:
        impl = model.getImplementationName()
    except Exception:
        pass
    try:
        from plugin.doc.document_helpers import get_document_type

        return "impl=%s doc_type=%s" % (impl, get_document_type(model).name)
    except Exception:
        return "impl=%s doc_type=?" % impl


class QueryTextListener(BaseTextListener):
    def __init__(self, send_listener):
        # We now keep a reference to the main SendButtonListener which holds the state
        self.send_listener = send_listener

    def on_text_changed(self, rEvent):
        model = getattr(rEvent.Source, "Model", None)
        if not model:
            model = rEvent.Source.getModel()
        text = model.Text.strip()

        # Dispatch event to the state machine
        self.send_listener.dispatch(SendEvent(SendEventKind.TEXT_UPDATED, {"has_text": bool(text)}))


# UNO Key.RETURN / KeyModifier.SHIFT (test-friendly integer codes)
_QUERY_KEY_RETURN = 1280
_QUERY_KEY_MODIFIER_SHIFT = 1


def query_enter_triggers_primary_send(key_code: int, modifiers: int) -> bool:
    """True when this key event should run the same primary action as Send (Enter without Shift)."""
    return bool(key_code == _QUERY_KEY_RETURN and (modifiers & _QUERY_KEY_MODIFIER_SHIFT) == 0)


_DOC_CHAT_ENTER_SENDS = "doc.chat_enter_key_sends_message"


class QueryKeyListener(BaseKeyListener):
    """Enter in the query field triggers Send when enabled in Settings (Shift+Enter inserts a newline)."""

    def __init__(self, send_listener):
        self.send_listener = send_listener

    def on_key_pressed(self, e):
        if not query_enter_triggers_primary_send(e.KeyCode, e.Modifiers):
            return
        try:
            from plugin.framework.config import get_config_bool

            if not get_config_bool(self.send_listener.ctx, _DOC_CHAT_ENTER_SENDS):
                return
        except Exception:
            pass
        sc = self.send_listener.send_control
        if not sc or not sc.getModel():
            return
        if not sc.getModel().Enabled:
            return
        try:
            if hasattr(e, "Consume"):
                setattr(e, "Consume", True)
        except Exception:
            pass
        self.send_listener.on_action_performed(e)


# ---------------------------------------------------------------------------
# SendButtonListener - handles Send button click with tool-calling loop
# ---------------------------------------------------------------------------


class SendButtonListener(SendHandlersMixin, ToolCallingMixin, BaseActionListener):
    """Listener for the Send button - runs chat with document, supports tool-calling."""

    client: LlmClient | None
    initial_doc_type: str | None
    _record_assistant_start: bool

    def __init__(
        self, ctx, frame, send_control, stop_control, query_control, response_control, image_model_selector, model_selector, status_control, session, chat_mode_selector=None, aspect_ratio_selector=None, base_size_input=None, sidebar_include_brainstorming=True, ensure_path_fn=None, clear_control=None
    ):
        self.ctx = ctx
        self.frame = frame
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
        self.ensure_path_fn = ensure_path_fn
        self.initial_doc_type = None  # Set by _wireControls
        self._stop_requested_fallback = False
        self._send_cancellation = None
        self._terminal_status = "Ready"
        self._send_busy = False
        self._in_librarian_mode = False
        self._in_brainstorming_mode = False
        self._brainstorming_topic = ""
        self.client = None
        self.audio_wav_path = None
        self._current_agent_backend = None  # Set during _do_send_via_agent_backend for Stop button
        self._fixed_send_width = None
        self._active_q: Any = None
        self._active_client: Any = None
        self._active_max_tokens: Any = None
        self._active_tools: Any = None
        self._active_execute_tool_fn: Any = None
        self._active_max_tool_rounds: Any = None
        self._active_query_text: Any = None
        self._active_model: Any = None
        self._active_async_tools: Any = None
        self._active_supports_status: Any = None
        self._active_round_num: Any = None
        self._active_pending_tools: Any = None
        self._current_tool_call_id = None
        self._record_assistant_start = False
        self._assistant_stream_start_len = None
        self._approval_event = None
        self._approval_ui_backup = None
        self._approval_query_for_engine = None
        self.rich_text_widget = None
        self._rich_plain_fallback_warned = False
        if HAS_RECORDING:
            assert _AudioRecorderCls is not None
            self.audio_recorder = _AudioRecorderCls()
        else:
            self.audio_recorder = None
        self.queue_executor = QueueExecutor()

        send_initial = SendButtonState(is_busy=False, is_recording=False, has_text=False, has_audio=False, audio_supported=HAS_RECORDING)
        self.sidebar_state = SidebarCompositeState(send=send_initial, tool_loop=None, audio=AudioRecorderState(status="idle"))

        # Subscribe to MCP/tool bus events
        try:
            from plugin.main import get_tools
            from plugin.framework.event_bus import global_event_bus

            event_bus = getattr(get_tools()._services, "events", None)
            if event_bus:
                event_bus.subscribe("mcp:request", self._on_mcp_request)
                event_bus.subscribe("mcp:result", self._on_mcp_result)
                log.debug(f"*** SendButtonListener subscribed to MCP events on services.events (id={id(event_bus)}) ***")
            global_event_bus.subscribe("grammar:status", self._on_grammar_status, weak=True)
        except Exception:
            log.exception("SendButtonListener event subscribe error")

    def set_rich_text_widget(self, widget):
        """Enable RichTextControl sidebar rendering via hidden-doc formatted copy."""
        self.rich_text_widget = widget
        log.info("[RICH-CONTROL] SendButtonListener.set_rich_text_widget called")

    def rerender_rich_text_session(self):
        """Re-render the final streamed assistant response with HTML formatting, leaving previous text untouched.

        Called after streaming completes to replace the last plain-text assistant response
        with full HTML rendering instead of raw chunks.
        """
        widget = getattr(self, "rich_text_widget", None)
        if widget is None:
            return
        try:
            widget.rerender_last_assistant_if_html(
                self.session,
                getattr(self, "_assistant_stream_start_len", None),
            )
        except Exception:
            log.exception("rerender_rich_text_session (rich control) failed")

    @property
    def state(self):
        """Send-button slice of :attr:`sidebar_state` (migration alias)."""
        return self.sidebar_state.send

    @state.setter
    def state(self, value):
        import dataclasses

        self.sidebar_state = dataclasses.replace(self.sidebar_state, send=value)

    @property
    def stop_requested(self) -> bool:
        scope = getattr(self, "_send_cancellation", None)
        if scope is not None:
            return scope.is_cancelled()
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

    def resolve_stop_checker(self):
        """Stable stop predicate for worker threads (survives clearing ``_send_cancellation``)."""
        from plugin.framework.queue_executor import bind_send_stop_checker

        return bind_send_stop_checker(getattr(self, "_send_cancellation", None), lambda: self._stop_requested_fallback)

    def sync_audio_slice(self):
        """Mirror :attr:`audio_recorder.state` into the composite (strategy A)."""
        import dataclasses

        if self.audio_recorder is None:
            return
        self.sidebar_state = dataclasses.replace(self.sidebar_state, audio=self.audio_recorder.state)

    def set_session(self, session):
        """Update the active session (e.g. when switching between Document and Research chat)."""
        self.session = session
        self.client = None  # Force client recreation if needed, though they usually share same config

    def sync_brainstorming_delegate_start(self, topic: str) -> None:
        """Match sidebar dropdown when main chat delegates domain=brainstorming."""
        from plugin.chatbot.chat_sidebar_mode import CHAT_MODE_BRAINSTORMING, set_selector_mode

        self._in_brainstorming_mode = True
        self._brainstorming_topic = str(topic or "")
        if self.chat_mode_selector:
            set_selector_mode(self.chat_mode_selector, CHAT_MODE_BRAINSTORMING, include_brainstorming=self.sidebar_include_brainstorming)

    def on_brainstorming_session_finished(self) -> None:
        """Reset sidebar after brainstorming_finished (dropdown returns to Chat)."""
        from plugin.chatbot.chat_sidebar_mode import CHAT_MODE_CHAT, clear_brainstorming_session, persist_mode_to_config, set_selector_mode

        clear_brainstorming_session(self)
        if self.chat_mode_selector:
            set_selector_mode(self.chat_mode_selector, CHAT_MODE_CHAT, include_brainstorming=self.sidebar_include_brainstorming)
        persist_mode_to_config(self.ctx, CHAT_MODE_CHAT)

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
        try:
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
        except Exception as e:
            from com.sun.star.lang import DisposedException
            from com.sun.star.uno import RuntimeException, Exception as UnoException

            if isinstance(e, (DisposedException, RuntimeException, UnoException)):
                log.debug("begin_inline_web_approval backup (likely disposed): %s", e)
            else:
                log.debug("begin_inline_web_approval backup: %s", e)

        try:
            if self.send_control and self.send_control.getModel():
                m = self.send_control.getModel()
                m.Label = _("Accept")
                m.Enabled = True
                if self._fixed_send_width:
                    try:
                        r = self.send_control.getPosSize()
                        if r.Width != self._fixed_send_width:
                            self.send_control.setPosSize(r.X, r.Y, self._fixed_send_width, r.Height, 15)
                    except Exception as e:
                        from com.sun.star.lang import DisposedException
                        from com.sun.star.uno import RuntimeException, Exception as UnoException

                        if isinstance(e, (DisposedException, RuntimeException, UnoException)):
                            log.debug("begin_inline_web_approval setPosSize (likely disposed): %s", e)
            if self.stop_control and self.stop_control.getModel():
                m = self.stop_control.getModel()
                m.Label = _("Change")
                m.Enabled = True
            if self.clear_control and self.clear_control.getModel():
                m = self.clear_control.getModel()
                m.Label = _("Reject")
                m.Enabled = True
        except Exception as e:
            from com.sun.star.lang import DisposedException
            from com.sun.star.uno import RuntimeException, Exception as UnoException

            if isinstance(e, (DisposedException, RuntimeException, UnoException)):
                log.debug("begin_inline_web_approval error (likely disposed): %s", e)
            else:
                log.exception("begin_inline_web_approval failed")

        # Approval is inline (Accept / Change / Reject); no chat transcript for the pending query.
        self._set_status(_("Waiting for approval…"))
        log.info("Inline web approval: waiting for Accept, Change, or Reject")

    def _open_web_search_change_dialog(self):
        """Open edit dialog for the pending web_search query; OK continues with optional override."""
        from plugin.chatbot.dialogs import show_web_search_query_edit_dialog

        initial = getattr(self, "_approval_query_for_engine", None) or ""
        text = show_web_search_query_edit_dialog(self.ctx, self.frame, initial)
        if text is None:
            return
        log.debug("_open_web_search_change_dialog: applying edited query len=%d", len(text))
        self._finish_inline_web_approval(True, query_override=text)

    def _finish_inline_web_approval(self, approved, query_override=None):
        ev = getattr(self, "_approval_event", None)
        if ev is None:
            return
        self._approval_event = None
        self._approval_query_for_engine = None
        b = self._approval_ui_backup or {}
        self._approval_ui_backup = None
        try:
            if self.send_control and self.send_control.getModel():
                m = self.send_control.getModel()
                if "send_label" in b:
                    m.Label = b["send_label"]
                if "send_enabled" in b:
                    m.Enabled = b["send_enabled"]
            if self.stop_control and self.stop_control.getModel():
                m = self.stop_control.getModel()
                if "stop_label" in b:
                    m.Label = b["stop_label"]
                if "stop_enabled" in b:
                    m.Enabled = b["stop_enabled"]
            if self.clear_control and self.clear_control.getModel() and "clear_enabled" in b:
                cm = self.clear_control.getModel()
                cm.Enabled = b["clear_enabled"]
                if "clear_label" in b:
                    cm.Label = b["clear_label"]
            if self.status_control and "status_text" in b:
                self.status_control.setText(b["status_text"])
        except Exception as e:
            from com.sun.star.lang import DisposedException
            from com.sun.star.uno import RuntimeException, Exception as UnoException

            if isinstance(e, (DisposedException, RuntimeException, UnoException)):
                log.debug("_finish_inline_web_approval restore (likely disposed): %s", e)
            else:
                log.debug("_finish_inline_web_approval restore: %s", e)
        try:
            ev.approved = approved
            ev.query_override = query_override if approved else None
            if approved and query_override is not None:
                log.debug("_finish_inline_web_approval: approved with query_override len=%d", len(query_override))
            ev.set()
        except Exception:
            log.exception("_finish_inline_web_approval threading event error")

    def _set_status(self, text):
        """Update the status field in the sidebar (read-only TextField).
        Uses setText() (XTextComponent) to write directly to the control/peer,
        bypassing model→view notifications which can desync after document edits."""
        try:
            if self.status_control:
                self.status_control.setText(text)
            else:
                log.debug("_set_status: NO CONTROL for '%s'" % text)
        except Exception as e:
            from com.sun.star.lang import DisposedException
            from com.sun.star.uno import RuntimeException, Exception as UnoException

            if isinstance(e, (DisposedException, RuntimeException, UnoException)):
                log.debug("_set_status('%s', level=logging.DEBUG) likely disposed: %s" % (text, e))
            else:
                log.debug("_set_status('%s', level=logging.DEBUG) EXCEPTION: %s" % (text, e))

    def _on_grammar_status(self, **data):
        """Show native grammar proofreader progress in the sidebar status field."""
        if self._send_busy or self._approval_event is not None:
            return
        text = format_grammar_status(data)
        try:
            from plugin.framework.queue_executor import post_to_main_thread

            post_to_main_thread(self._set_status, text)
        except Exception as e:
            log.debug("_on_grammar_status: post_to_main_thread failed: %s", e)
            self._set_status(text)

    def _scroll_response_to_bottom(self):
        """Scroll the response area to show the bottom (newest content).
        Uses XTextComponent.setSelection to place caret at end, which scrolls the view."""
        try:
            if self.response_control:
                model = self.response_control.getModel()
                if model and hasattr(self.response_control, "setSelection"):
                    text = model.Text or ""
                    length = len(text)
                    self.response_control.setSelection(uno.createUnoStruct("com.sun.star.awt.Selection", length, length))
        except Exception as e:
            from com.sun.star.lang import DisposedException
            from com.sun.star.uno import RuntimeException, Exception as UnoException

            if isinstance(e, (DisposedException, RuntimeException, UnoException)):
                log.debug("_scroll_response_to_bottom failed (likely disposed): %s", e)

    '''
    def _get_scrollbar(self):
        ...  # commented out — scrollbar was never found in embedded frames
    '''

    def _should_auto_scroll(self):
        """Always returns True for now — forces scroll to bottom on every append.

        Future: implement sticky scroll by reading VCL scrollbar position and
        returning False when user has manually scrolled up.
        """
        return True

    def _run_rich_ui(self, fn, *args, **kwargs):
        """Run rich-control UI work inline on the main thread; post from workers."""
        if threading.current_thread() is threading.main_thread():
            return fn(*args, **kwargs)
        self.queue_executor.post(fn, *args, **kwargs)

    def _append_response(self, text, is_thinking=False, role="assistant"):
        """Append text to the response area (RichTextControl or plain multiline field)."""
        try:
            widget = getattr(self, "rich_text_widget", None)
            if widget:
                auto_scroll = self._should_auto_scroll()
                log.debug("_append_response: rich-control len=%d role=%s", len(text) if text else 0, role)
                if role == "user":

                    def _on_user_inserted(control_len: int) -> None:
                        self._assistant_stream_start_len = control_len
                        log.debug("_append_response: rich-control stream start len=%d", control_len)

                    self._run_rich_ui(
                        widget.append_user_message,
                        text,
                        on_after_insert=_on_user_inserted,
                    )
                else:
                    if getattr(self, "_record_assistant_start", False):
                        self._record_assistant_start = False
                        self._assistant_stream_start_len = widget.get_text_length()
                        log.debug(
                            "_append_response: rich-control stream start len=%d (final answer)",
                            self._assistant_stream_start_len,
                        )
                    
                    if getattr(self, "_plain_text_stripper", None) is not None:
                        clean_text = self._plain_text_stripper.feed(text)
                    else:
                        from plugin.framework.html_stripper import strip_html_tags
                        clean_text = strip_html_tags(text)

                    self._run_rich_ui(
                        widget.append_assistant_stream_chunk,
                        clean_text,
                        auto_scroll=auto_scroll,
                    )
                return

            if not getattr(self, "_rich_plain_fallback_warned", False):
                from plugin.framework.config import get_config_bool_safe

                if get_config_bool_safe(self.ctx, "rich_text_control_sidebar"):
                    log.warning(
                        "[RICH-CONTROL] _append_response plain fallback while rich_text_control_sidebar enabled",
                    )
                    self._rich_plain_fallback_warned = True

            if self.response_control and self.response_control.getModel():
                from plugin.chatbot.dialogs import get_control_text, set_control_text
                from plugin.framework.html_stripper import strip_html_tags

                should_scroll = self._should_auto_scroll()
                current = get_control_text(self.response_control) or ""
                
                if role == "assistant" and getattr(self, "_plain_text_stripper", None) is not None:
                    clean_text = self._plain_text_stripper.feed(text)
                else:
                    clean_text = strip_html_tags(text)

                set_control_text(self.response_control, current + clean_text)
                if should_scroll:
                    self._scroll_response_to_bottom()
        except Exception as e:
            from com.sun.star.lang import DisposedException
            from com.sun.star.uno import RuntimeException, Exception as UnoException

            if isinstance(e, (DisposedException, RuntimeException, UnoException)):
                log.debug("_append_response failed (likely disposed): %s", e)

    def _on_mcp_request(self, tool="", args=None, method=None, **kwargs):
        """Handle MCP request events from the bus (background thread)."""
        try:
            from plugin.framework.logging import format_tool_call_for_display

            fmt_str = format_tool_call_for_display(tool, args, method)
            log.debug(f"MCP Request (hidden from UI, level=logging.DEBUG): {fmt_str}")
        except Exception:
            log.exception("_on_mcp_request error")

    def _on_mcp_result(self, tool="", result_snippet="", **kwargs):
        """Handle MCP result events from the bus (background thread)."""

        def _update_ui():
            try:
                from plugin.framework.logging import format_tool_result_for_display

                fmt_str = format_tool_result_for_display(tool, result_snippet, args=kwargs.get("args"))
                self._append_response(f"[MCP Result] {fmt_str}\n")
            except Exception:
                log.exception("_on_mcp_result UI update error")

        try:
            self.queue_executor.post(_update_ui)
        except Exception:
            log.exception("_on_mcp_result post error")

    def _get_document_model(self):
        """Get the document model strictly from the frame.

        Always prefers the document bound to this sidebar's frame (same window as the user)
        instead of ``Desktop.getCurrentComponent()``, which can point at the wrong
        document if focus changes.
        """
        model = None
        frame_exc: BaseException | None = None
        if self.frame:
            try:
                model = self.frame.getController().getModel()
            except Exception as e:
                frame_exc = e

        from plugin.doc.document_helpers import is_writer, is_calc, is_draw

        if model and (is_writer(model) or is_calc(model) or is_draw(model)):
            return model

        # Only log when chat send will fail (same moment as the sidebar error message).
        detail_parts = ["has_frame=%s" % bool(self.frame), "model_probe=%s" % _uno_model_probe_for_log(model)]
        if frame_exc is not None:
            detail_parts.append("frame_get_model_failed=[%s] %s" % (type(frame_exc).__name__, frame_exc))
        if model is not None:
            detail_parts.append("reject_reason=unsupported_component probe=%s" % _uno_model_probe_for_log(model))
        log.error("SendButtonListener: no compatible document model for chat (%s)", "; ".join(detail_parts))
        return None

    def set_fixed_send_width(self, width_px):
        self._fixed_send_width = width_px

    def _set_button_states(self, send_enabled, stop_enabled):
        """Set Send/Stop enabled flags (per-control try/except so one UNO failure cannot strand the other)."""
        if self.send_control and self.send_control.getModel():
            try:
                self.send_control.getModel().Enabled = bool(send_enabled)
            except Exception as e:
                from com.sun.star.lang import DisposedException
                from com.sun.star.uno import RuntimeException, Exception as UnoException

                if isinstance(e, (DisposedException, RuntimeException, UnoException)):
                    log.debug("Failed to set send_control enabled state (likely disposed): %s", e)
        if self.stop_control and self.stop_control.getModel():
            try:
                self.stop_control.getModel().Enabled = bool(stop_enabled)
            except Exception as e:
                from com.sun.star.lang import DisposedException
                from com.sun.star.uno import RuntimeException, Exception as UnoException

                if isinstance(e, (DisposedException, RuntimeException, UnoException)):
                    log.debug("Failed to set stop_control enabled state (likely disposed): %s", e)

    def dispatch(self, event):
        """Dispatch an event to the state machine, compute new state, and apply effects."""
        tr = sidebar_next_state(self.sidebar_state, SidebarEvent(kind=SidebarEventKind.SEND, payload=event))
        self.sidebar_state = tr.state
        self._send_busy = self.sidebar_state.send.is_busy

        for effect in tr.effects:
            self._interpret_effect(effect)

    def _interpret_effect(self, effect):
        """Interpret a state machine effect and apply side-effects."""
        from plugin.framework.i18n import _

        match effect:
            case LogSidebarEffect():
                log.debug("%s", effect.message)
            case UpdateUIEffect():
                self._set_button_states(effect.send_enabled, effect.stop_enabled)

                if self.send_control and self.send_control.getModel():
                    btn_model = self.send_control.getModel()
                    if btn_model.Label != _(effect.send_label):
                        btn_model.Label = _(effect.send_label)
                    if self._fixed_send_width:
                        try:
                            r = self.send_control.getPosSize()
                            if r.Width != self._fixed_send_width:
                                self.send_control.setPosSize(r.X, r.Y, self._fixed_send_width, r.Height, 15)
                        except Exception as e:
                            from com.sun.star.lang import DisposedException
                            from com.sun.star.uno import RuntimeException, Exception as UnoException

                            if isinstance(e, (DisposedException, RuntimeException, UnoException)):
                                log.debug("Failed to set pos size for send_control (likely disposed): %s", e)

                if effect.status_text is not None and effect.status_text != "":
                    self._set_status(_(effect.status_text))

            case StartRecordingEffect():
                if not self.audio_recorder:
                    return
                try:
                    self.audio_recorder.start_recording()
                except RuntimeError as re:
                    self._append_response("\n[Audio error: %s]\n" % str(re))
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

            case StartSendEffect():
                self._stop_requested_fallback = False
                self._terminal_status = "Ready"
                try:
                    from plugin.framework.queue_executor import agent_session

                    with agent_session() as cancel_scope:
                        self._send_cancellation = cancel_scope
                        try:
                            self._do_send()
                        finally:
                            self._send_cancellation = None
                except Exception as e:
                    doc_type_for_log = getattr(self, "initial_doc_type", "unknown")
                    log.exception("SendButton unhandled exception [doc: %s]", doc_type_for_log)
                    self._append_response("\n\n[Error: %s]\n" % str(e))
                    self.dispatch(SendEvent(SendEventKind.ERROR_OCCURRED))
                finally:
                    update_activity_state("")
                    if self._terminal_status == "Error":
                        self.dispatch(SendEvent(SendEventKind.ERROR_OCCURRED))
                    else:
                        self.dispatch(SendEvent(SendEventKind.SEND_COMPLETED))
                        if self._terminal_status:
                            self._set_status(_(self._terminal_status))

            case StopSendEffect():
                scope = getattr(self, "_send_cancellation", None)
                if scope is not None:
                    scope.cancel()
                self._stop_requested_fallback = True

            case _:
                log.debug("SendButtonListener: unhandled effect type %s", type(effect).__name__)

    def on_action_performed(self, rEvent):
        from plugin.framework.i18n import _

        if getattr(self, "_approval_event", None) is not None and self.send_control and self.send_control.getModel():
            if self.send_control.getModel().Label == _("Accept"):
                self._finish_inline_web_approval(True)
                return
        btn_model = self.send_control.getModel()
        label = btn_model.Label

        if label == _("Record"):
            self.dispatch(SendEvent(SendEventKind.RECORD_CLICKED))
        elif label == _("Stop Rec"):
            self.dispatch(SendEvent(SendEventKind.STOP_REC_CLICKED))
        elif label == _("Send"):
            self.dispatch(SendEvent(SendEventKind.SEND_CLICKED))

    # _transcribe_audio_async is provided by SendHandlersMixin.

    def _get_doc_type_str(self, model):
        from plugin.doc.document_helpers import get_document_type, DocumentType

        doc_type = get_document_type(model)
        if doc_type == DocumentType.CALC:
            return "Calc"
        if doc_type in (DocumentType.DRAW, DocumentType.IMPRESS):
            return "Draw"
        if doc_type == DocumentType.WRITER:
            return "Writer"
        return "Unknown"

    def _do_send(self):
        from plugin.framework.i18n import _
        from plugin.framework.html_stripper import StreamingHTMLStripper

        self._plain_text_stripper = StreamingHTMLStripper()
        self._set_status(_("Starting..."))
        update_activity_state("do_send")
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

        doc_type_str = self._get_doc_type_str(model)
        log.debug("_do_send: detected document type: %s" % doc_type_str)

        if self.initial_doc_type and doc_type_str != self.initial_doc_type:
            err_msg = _("[Internal Error: Document type changed from {0} to {1}! Please file an error.]").format(self.initial_doc_type, doc_type_str)
            log.exception("_do_send ERROR: %s", err_msg)
            self._append_response("\n%s\n" % err_msg)
            self._terminal_status = "Error"
            return

        if doc_type_str == "Unknown":
            err_msg = _("[Internal Error: Could not identify document type for {0}. Please report this!]").format(model.getImplementationName() if hasattr(model, "getImplementationName") else "Unknown")
            log.exception("_do_send ERROR: %s", err_msg)
            self._append_response("\n%s\n" % err_msg)
            self._terminal_status = "Error"
            return

        # Get user query and clear field (before loading tools, so direct-image path can return early)
        query_text = ""
        if self.query_control and self.query_control.getModel():
            from plugin.chatbot.dialogs import get_control_text

            query_text = (get_control_text(self.query_control) or "").strip()

        # Audio implies we have input even if text is empty
        if not query_text and not self.audio_wav_path:
            self._terminal_status = ""
            return

        if self.query_control and self.query_control.getModel():
            from plugin.chatbot.dialogs import set_control_text

            set_control_text(self.query_control, "")

        # Transcription Fallback check
        if self.audio_wav_path:
            from plugin.framework.config import get_current_endpoint
            from plugin.framework.client.model_fetcher import get_text_model, has_native_audio, get_stt_model

            current_model = get_text_model(self.ctx)
            current_endpoint = get_current_endpoint(self.ctx)

            if has_native_audio(self.ctx, current_model, current_endpoint) is False:
                stt_model = get_stt_model(self.ctx)
                if stt_model:
                    log.warning("_do_send: model %s has no native audio, using stt fallback %s" % (current_model, stt_model))
                    try:
                        transcript = self._transcribe_audio(self.audio_wav_path, stt_model)
                        if transcript:
                            query_text = (query_text + "\n" + transcript).strip() if query_text else transcript
                    except Exception as e:
                        from plugin.framework.errors import NetworkError

                        if isinstance(e, NetworkError):
                            log.exception("NetworkError during STT fallback")
                        else:
                            log.exception("Error during STT fallback")
                        self._terminal_status = "Error"
                        return
                else:
                    err_msg = _("[Model {0} does not support native audio. Please select an STT Model in Settings.]").format(current_model)
                    self._append_response("\n%s\n" % err_msg)
                    self._terminal_status = "Error"
                    self._set_status(_("Error"))
                    return
            else:
                log.debug("_do_send: model %s supports native audio, proceeding" % current_model)

        from plugin.chatbot.chat_sidebar_mode import (
            CHAT_MODE_BRAINSTORMING,
            CHAT_MODE_IMAGE,
            CHAT_MODE_WEB_RESEARCH,
            mode_from_selector,
        )

        sidebar_mode = mode_from_selector(self.chat_mode_selector, include_brainstorming=self.sidebar_include_brainstorming)

        if sidebar_mode == CHAT_MODE_WEB_RESEARCH:
            log.info("_do_send: using web research sub-agent — skip chat model and direct image")
            self._run_web_research(query_text, model)
            return

        if sidebar_mode == CHAT_MODE_IMAGE:
            log.debug("_do_send: using image model (direct, level=logging.INFO) — skip chat model")
            self._do_send_direct_image(query_text, model)
            return

        if sidebar_mode == CHAT_MODE_BRAINSTORMING and doc_type_str.lower() == "writer":
            if not self._brainstorming_topic:
                self._brainstorming_topic = query_text
            log.info("_do_send: using brainstorming sub-agent")
            self._run_brainstorming(query_text, model)
            return

        # Agent backend (Aider, Hermes): use external agent instead of built-in LLM
        try:
            from plugin.framework.config import get_config
            from plugin.agent_backend.registry import normalize_backend_id

            agent_backend_id = normalize_backend_id(get_config(self.ctx, "agent_backend.backend_id"))
            if agent_backend_id and agent_backend_id != "builtin":
                log.info("_do_send: using agent backend %s" % agent_backend_id)
                self._do_send_via_agent_backend(query_text, model, doc_type_str.lower())
                return
        except Exception:
            log.exception("_do_send: agent backend check failed")

        if self._in_librarian_mode:
            log.info("_do_send: continuing librarian onboarding agent")
            self._run_librarian(query_text, model)
            return

        # Check if USER.md exists for Librarian Onboarding entry
        user_md_exists = False
        from plugin.chatbot.memory import MemoryStore

        store = MemoryStore(self.ctx)
        if store.read("user"):
            user_md_exists = True

        # Start onboarding when no user profile exists yet. Once started, the
        # per-panel librarian flag keeps later turns in onboarding until the
        # librarian explicitly switches modes.
        if not user_md_exists:
            self._in_librarian_mode = True
            log.info("_do_send: using librarian onboarding agent")
            self._run_librarian(query_text, model)
            return

        # Regular Chat with Tools or Streams
        # Cast to Any to satisfy ty since SendButtonListener mixes in multiple protocol hosts
        getattr(self, "_do_send_chat_with_tools")(query_text, model, doc_type_str.lower())

    # _do_send_direct_image is provided by SendHandlersMixin.

    # _do_send_chat_with_tools is provided by ToolCallingMixin.

    # _do_send_via_agent_backend is provided by SendHandlersMixin.

    # Writer edit selection uses WriterStreamedRewriteSession (document compound undo). Broader
    # chat/tool undo grouping is still future work.

    # _run_web_research is provided by SendHandlersMixin.

    @property
    def _sm_state(self) -> Any:
        return self.sidebar_state.tool_loop

    @_sm_state.setter
    def _sm_state(self, value: Any) -> None:
        import dataclasses

        self.sidebar_state = dataclasses.replace(self.sidebar_state, tool_loop=value)

    def disposing(self, Source):
        try:
            from plugin.framework.event_bus import global_event_bus

            global_event_bus.unsubscribe("mcp:request", self._on_mcp_request)
            global_event_bus.unsubscribe("mcp:result", self._on_mcp_result)
            global_event_bus.unsubscribe("grammar:status", self._on_grammar_status)
        except Exception as e:
            log.debug("SendButtonListener.disposing: error unsubscribing from event bus: %s", e)


# ---------------------------------------------------------------------------
# StopButtonListener - allows user to cancel the AI request
# ---------------------------------------------------------------------------


class StopButtonListener(BaseActionListener):
    """Listener for the Stop button - sets a flag in SendButtonListener to halt loops."""

    def __init__(self, send_listener):
        self.send_listener = send_listener

    def on_action_performed(self, rEvent):
        if self.send_listener and getattr(self.send_listener, "_approval_event", None) is not None:
            from plugin.framework.i18n import _

            if self.send_listener.stop_control and self.send_listener.stop_control.getModel() and self.send_listener.stop_control.getModel().Label == _("Change"):
                self.send_listener._open_web_search_change_dialog()
                return
            if self.send_listener.stop_control and self.send_listener.stop_control.getModel() and self.send_listener.stop_control.getModel().Label == _("Reject"):
                self.send_listener._finish_inline_web_approval(False)
                return
        if self.send_listener:
            self.send_listener.dispatch(SendEvent(SendEventKind.STOP_CLICKED))


# ---------------------------------------------------------------------------
# ClearButtonListener - resets the conversation
# ---------------------------------------------------------------------------


class ClearButtonListener(BaseActionListener):
    """Listener for the Clear button - resets conversation history."""

    def __init__(self, session, response_control, status_control, greeting="", send_listener=None):
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

    def set_session(self, session, greeting=None):
        """Update the active session and optionally the greeting used for clear."""
        self.session = session
        if greeting is not None:
            self.greeting = greeting

    def on_action_performed(self, rEvent):
        if self.send_listener and getattr(self.send_listener, "_approval_event", None) is not None:
            self.send_listener._finish_inline_web_approval(False)
            return
        self.session.clear()

        if self.send_listener and self.send_listener.rich_text_widget:
            try:
                self.send_listener.rich_text_widget.clear_and_greeting(self.greeting or "")
            except Exception:
                log.exception("Error clearing RichTextControl sidebar")
            if self.status_control:
                self.status_control.setText("")
            return

        if self.response_control and self.response_control.getModel():
            from plugin.chatbot.dialogs import set_control_text

            text = self.greeting + "\n" if self.greeting else ""
            set_control_text(self.response_control, text)
        if self.status_control:
            self.status_control.setText("")
