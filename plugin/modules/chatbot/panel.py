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

import logging
import uno
import unohelper
from plugin.framework.uno_context import get_active_document
from plugin.framework.dialogs import get_checkbox_state
from com.sun.star.awt import XActionListener
from plugin.modules.chatbot.send_handlers import SendHandlersMixin
from plugin.modules.chatbot.tool_loop import ToolCallingMixin

from plugin.framework.logging import update_activity_state
from plugin.modules.chatbot.history_db import get_chat_history

# Recording available only if audio_recorder (and contrib/audio) is present
try:
    from plugin.modules.chatbot.audio_recorder import start_recording, stop_recording  # noqa: F401
    HAS_RECORDING = True
except ImportError:
    HAS_RECORDING = False

# Default max tool rounds when not in config (get_api_config supplies chat_max_tool_rounds)
DEFAULT_MAX_TOOL_ROUNDS = 5


# ---------------------------------------------------------------------------
# ChatSession - holds conversation history for multi-turn chat
# ---------------------------------------------------------------------------

class ChatSession:
    """Maintains the message history for one sidebar chat session."""

    def __init__(self, system_prompt=None, session_id=None):
        self.session_id = session_id
        self.db = None
        self.messages = []
        
        if session_id:
            try:
                self.db = get_chat_history(session_id)
                self.messages = self.db.get_messages()
            except Exception as e:
                log.error("ChatSession history load error: %s" % e)

        # If no history, or system prompt forced
        if not self.messages and system_prompt:
            self.messages.append({"role": "system", "content": system_prompt})
            if self.db:
                self.db.add_message("system", system_prompt)

    def add_user_message(self, content):
        self.messages.append({"role": "user", "content": content})
        if self.db:
            self.db.add_message("user", content)

    def add_assistant_message(self, content=None, tool_calls=None):
        msg = {"role": "assistant"}
        if content:
            msg["content"] = content
        else:
            msg["content"] = None
        if tool_calls:
            msg["tool_calls"] = tool_calls
        self.messages.append(msg)
        if self.db:
            # Only persist the text content to history; tool calls are ephemeral.
            self.db.add_message("assistant", content)

    def add_tool_result(self, tool_call_id, content):
        self.messages.append({
            "role": "tool",
            "tool_call_id": tool_call_id,
            "content": content,
        })
        # Note: We do NOT persist tool results to history_db. 
        # This keeps the persistent history clean of tool formatting requirements.

    def update_document_context(self, doc_text):
        """Update or insert the document context as a system message.
        Replaces the existing document context if present, otherwise appends."""
        context_marker = "[DOCUMENT CONTENT]"
        context_msg = "%s\n%s\n[END DOCUMENT]" % (context_marker, doc_text)

        # Check if we already have a document context message
        for i, msg in enumerate(self.messages):
            if msg["role"] == "system" and context_marker in (msg.get("content") or ""):
                self.messages[i]["content"] = context_msg
                return
        # Insert after the first system prompt
        insert_at = 1 if self.messages and self.messages[0]["role"] == "system" else 0
        self.messages.insert(insert_at, {"role": "system", "content": context_msg})

    def clear(self):
        """Reset to just the system prompt."""
        system = None
        for msg in self.messages:
            if msg["role"] == "system" and "[DOCUMENT CONTENT]" not in (msg.get("content") or ""):
                system = msg
                break
        self.messages = []
        if self.db:
            self.db.clear()
        if system:
            self.messages.append(system)
            if self.db:
                self.db.add_message("system", system["content"])


# ---------------------------------------------------------------------------
# QueryTextListener - dynamic button toggling
# ---------------------------------------------------------------------------

from plugin.framework.listeners import BaseTextListener, BaseActionListener

log = logging.getLogger(__name__)

class QueryTextListener(BaseTextListener):
    def __init__(self, send_button):
        self.send_button = send_button
        # Pixel width measured for Record/Send/Stop Rec; stops sidebar width creep on GTK.
        self._fixed_send_width = None

    def set_fixed_send_width(self, width_px):
        self._fixed_send_width = width_px

    def on_text_changed(self, ev):
        model = getattr(ev.Source, "Model", None)
        if not model:
            model = ev.Source.getModel()
        text = model.Text.strip()

        btn_model = self.send_button.getModel()
        # If currently recording, do not toggle back to Record
        if btn_model.Label == "Stop Rec":
            return

        if text:
            new_label = "Send"
        else:
            new_label = "Record" if HAS_RECORDING else "Send"

        if btn_model.Label != new_label:
            log.debug("QueryTextListener: toggle label '%s' -> '%s'" % (btn_model.Label, new_label))
            btn_model.Label = new_label
        if self._fixed_send_width:
            try:
                r = self.send_button.getPosSize()
                if r.Width != self._fixed_send_width:
                    self.send_button.setPosSize(
                        r.X, r.Y, self._fixed_send_width, r.Height, 15
                    )
            except Exception:
                pass


# ---------------------------------------------------------------------------
# SendButtonListener - handles Send button click with tool-calling loop
# ---------------------------------------------------------------------------

class SendButtonListener(SendHandlersMixin, ToolCallingMixin, BaseActionListener):
    """Listener for the Send button - runs chat with document, supports tool-calling."""

    def __init__(self, ctx, frame, send_control, stop_control, query_control, response_control, image_model_selector, model_selector, status_control, session, direct_image_checkbox=None, aspect_ratio_selector=None, base_size_input=None, web_research_checkbox=None, ensure_path_fn=None):
        self.ctx = ctx
        self.frame = frame
        self.send_control = send_control
        self.stop_control = stop_control
        self.query_control = query_control
        self.response_control = response_control
        self.image_model_selector = image_model_selector
        self.model_selector = model_selector
        self.status_control = status_control
        self.session = session
        self.direct_image_checkbox = direct_image_checkbox
        self.aspect_ratio_selector = aspect_ratio_selector
        self.base_size_input = base_size_input
        self.web_research_checkbox = web_research_checkbox
        self.ensure_path_fn = ensure_path_fn
        self.initial_doc_type = None  # Set by _wireControls
        self.stop_requested = False
        self._terminal_status = "Ready"
        self._send_busy = False
        self.client = None
        self.audio_wav_path = None
        self._current_agent_backend = None  # Set during _do_send_via_agent_backend for Stop button
        # Subscribe to MCP/tool bus events
        try:
            from plugin.main import get_tools
            event_bus = getattr(get_tools()._services, "events", None)
            if event_bus:
                event_bus.subscribe("mcp:request", self._on_mcp_request)
                event_bus.subscribe("mcp:result", self._on_mcp_result)
                log.debug(f"*** SendButtonListener subscribed to MCP events on services.events (id={id(event_bus)}) ***")
        except Exception as e:
            log.error("MCP subscribe error: %s" % e)

    def set_session(self, session):
        """Update the active session (e.g. when switching between Document and Research chat)."""
        self.session = session
        self.client = None # Force client recreation if needed, though they usually share same config

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
            log.debug("_set_status('%s', level=logging.DEBUG) EXCEPTION: %s" % (text, e))

    def _scroll_response_to_bottom(self):
        """Scroll the response area to show the bottom (newest content).
        Uses XTextComponent.setSelection to place caret at end, which scrolls the view."""
        try:
            if self.response_control:
                model = self.response_control.getModel()
                if model and hasattr(self.response_control, "setSelection"):
                    text = model.Text or ""
                    length = len(text)
                    self.response_control.setSelection(
                        uno.createUnoStruct("com.sun.star.awt.Selection", length, length))
        except Exception:
            pass

    def _append_response(self, text, is_thinking=False):
        """Append text to the response area."""
        try:
            if self.response_control and self.response_control.getModel():
                current = get_control_text(self.response_control) or ""
                set_control_text(self.response_control, current + text)
                self._scroll_response_to_bottom()
        except Exception:
            pass

    def _on_mcp_request(self, tool="", args=None, method=None, **kwargs):
        """Handle MCP request events from the bus (background thread)."""
        try:
            from plugin.framework.logging import format_tool_call_for_display
            fmt_str = format_tool_call_for_display(tool, args, method)
            log.debug(f"MCP Request (hidden from UI, level=logging.DEBUG): {fmt_str}")
        except Exception as e:
                        log.error("_on_mcp_request error: %s" % e)

    def _on_mcp_result(self, tool="", result_snippet="", **kwargs):
        """Handle MCP result events from the bus (background thread)."""
        from plugin.framework.main_thread import execute_on_main_thread

        def _update_ui():
            try:
                from plugin.framework.logging import format_tool_result_for_display
                fmt_str = format_tool_result_for_display(tool, result_snippet, args=kwargs.get("args"))
                self._append_response(f"[MCP Result] {fmt_str}\n")
            except Exception as e:
                                log.error("_on_mcp_result UI update error: %s" % e)

        try:
            execute_on_main_thread(_update_ui)
        except Exception as e:
                        log.error("_on_mcp_result post error: %s" % e)

    def _get_document_model(self):
        """Get the Writer document model."""
        model = get_active_document(self.ctx)

        from plugin.framework.document import is_writer, is_calc, is_draw
        if model and (is_writer(model) or is_calc(model) or is_draw(model)):
            return model
        return None

    def _set_button_states(self, send_enabled, stop_enabled):
        """Set Send/Stop button enabled states. Per-control try/except so one failure cannot leave Send stuck disabled.
        Prefer model Enabled property (LibreOffice UNO); fallback to control.setEnable if available."""
        def set_control_enabled(control, enabled):
            if control and control.getModel():
                control.getModel().Enabled = bool(enabled)
        set_control_enabled(self.send_control, send_enabled)
        set_control_enabled(self.stop_control, stop_enabled)

    def on_action_performed(self, evt):
        try:
            btn_model = self.send_control.getModel()
            if HAS_RECORDING and btn_model.Label == "Record":
                # Start recording
                from plugin.modules.chatbot.audio_recorder import start_recording
                try:
                    start_recording()
                except RuntimeError as re:
                    self._append_response("\n[Audio error: %s]\n" % str(re))
                    return
                btn_model.Label = "Stop Rec"
                self._set_status("Recording audio...")
                return
            elif HAS_RECORDING and btn_model.Label == "Stop Rec":
                # Stop recording and proceed to send
                from plugin.modules.chatbot.audio_recorder import stop_recording
                self.audio_wav_path = stop_recording()
                if self.query_control and get_control_text(self.query_control).strip():
                    btn_model.Label = "Send"
                else:
                    btn_model.Label = "Record"

            self.stop_requested = False
            self._terminal_status = "Ready"
            self._send_busy = True
            self._set_button_states(send_enabled=False, stop_enabled=True)
            self._do_send()
        except Exception as e:
            self._terminal_status = "Error"
            import traceback
            tb = traceback.format_exc()

            # Use richer logging context before appending
            doc_type_for_log = getattr(self, "initial_doc_type", "unknown")
            log.error("SendButton unhandled exception [doc: %s]: %s\n%s", doc_type_for_log, e, tb)

            self._append_response("\n\n[Error: %s]\n" % str(e))
            raise
        finally:
            self._send_busy = False
            log.debug("actionPerformed finally: resetting UI")
            self._set_status(self._terminal_status)
            if self.send_control and self.send_control.getModel().Label not in ("Record", "Stop Rec"):
                # if empty, set to Record (when recording available) else Send
                if self.query_control and (get_control_text(self.query_control).strip() or self.audio_wav_path):
                    self.send_control.getModel().Label = "Send"
                else:
                    self.send_control.getModel().Label = "Record" if HAS_RECORDING else "Send"
            self._set_button_states(send_enabled=True, stop_enabled=False)
            log.debug("control returned to LibreOffice")
            update_activity_state("")  # clear phase so watchdog does not report after we return

    # _transcribe_audio_async is provided by SendHandlersMixin.

    def _get_doc_type_str(self, model):
        from plugin.framework.document import is_writer, is_calc, is_draw
        return "Calc" if is_calc(model) else "Draw" if is_draw(model) else "Writer" if is_writer(model) else "Unknown"

    def _do_send(self):
        self._set_status("Starting...")
        update_activity_state("do_send")
        log.info("=== _do_send START ===")

        # Ensure extension directory is on sys.path (injected by panel_factory to avoid circular import)
        if self.ensure_path_fn:
            self.ensure_path_fn(self.ctx)

        # 1. Get document model
        self._set_status("Getting document...")
        log.debug("_do_send: getting document model...")
        model = self._get_document_model()
        if not model:
            log.info("_do_send: no document found")
            self._append_response("\n[No compatible LibreOffice document (Writer, Calc, or Draw) found in the active window.]\n")
            self._terminal_status = "Error"
            return
        log.debug("_do_send: got document model OK")

        from plugin.framework.document import is_writer, is_calc, is_draw
        doc_type_str = "Calc" if is_calc(model) else "Draw" if is_draw(model) else "Writer" if is_writer(model) else "Unknown"
        log.debug("_do_send: detected document type: %s" % doc_type_str)
        
        if self.initial_doc_type and doc_type_str != self.initial_doc_type:
            err_msg = "[Internal Error: Document type changed from %s to %s! Please file an error.]" % (self.initial_doc_type, doc_type_str)
            log.error("_do_send ERROR: %s" % err_msg)
            self._append_response("\n%s\n" % err_msg)
            self._terminal_status = "Error"
            return

        if doc_type_str == "Unknown":
            err_msg = "[Internal Error: Could not identify document type for %s. Please report this!]" % (model.getImplementationName() if hasattr(model, "getImplementationName") else "Unknown")
            log.error("_do_send ERROR: %s" % err_msg)
            self._append_response("\n%s\n" % err_msg)
            self._terminal_status = "Error"
            return

        # Get user query and clear field (before loading tools, so direct-image path can return early)
        query_text = ""
        if self.query_control and self.query_control.getModel():
            query_text = (get_control_text(self.query_control) or "").strip()

        # Audio implies we have input even if text is empty
        if not query_text and not self.audio_wav_path:
            self._terminal_status = ""
            return

        if self.query_control and self.query_control.getModel():
            set_control_text(self.query_control, "")

        # Transcription Fallback check
        if self.audio_wav_path:
            from plugin.framework.config import get_text_model, get_current_endpoint, has_native_audio, get_stt_model
            current_model = get_text_model(self.ctx)
            current_endpoint = get_current_endpoint(self.ctx)
            
            if has_native_audio(self.ctx, current_model, current_endpoint) is False:
                stt_model = get_stt_model(self.ctx)
                if stt_model:
                    log.warning("_do_send: model %s has no native audio, using stt fallback %s" % (current_model, stt_model))
                    self._transcribe_audio_async(self.audio_wav_path, stt_model, model, query_text=query_text)
                    return
                else:
                    err_msg = "[Model %s does not support native audio. Please select an STT Model in Settings.]" % current_model
                    self._append_response("\n%s\n" % err_msg)
                    self._terminal_status = "Error"
                    self._set_status("Error")
                    return
            else:
                log.debug("_do_send: model %s supports native audio, proceeding" % current_model)

        # Optional web-research path
        web_research_checked = False
        if self.web_research_checkbox:
            try:
                web_research_checked = (get_checkbox_state(self.web_research_checkbox) == 1)
            except Exception:
                pass
        if web_research_checked:
            log.info("_do_send: using web research sub-agent — skip chat model and direct image")
            self._run_web_research(query_text, model)
            return

        # Direct image path
        direct_image_checked = False
        if self.direct_image_checkbox:
            try:
                direct_image_checked = (get_checkbox_state(self.direct_image_checkbox) == 1)
            except Exception as e:
                log.error("_do_send: Use Image model checkbox read error: %s" % e)
        if direct_image_checked:
            log.debug("_do_send: using image model (direct, level=logging.INFO) — skip chat model")
            self._do_send_direct_image(query_text, model)
            return

        # Agent backend (Aider, Hermes): use external agent instead of built-in LLM
        try:
            from plugin.framework.config import get_config
            agent_backend_id = str(get_config(self.ctx, "agent_backend.backend_id") or "builtin").strip().lower()
            if agent_backend_id and agent_backend_id != "builtin":
                log.info("_do_send: using agent backend %s" % agent_backend_id)
                self._do_send_via_agent_backend(query_text, model, doc_type_str.lower())
                return
        except Exception as e:
            log.error("_do_send: agent backend check failed: %s" % e)

        # Regular Chat with Tools or Streams
        self._do_send_chat_with_tools(query_text, model, doc_type_str.lower())

    # _do_send_direct_image is provided by SendHandlersMixin.

    # _do_send_chat_with_tools is provided by ToolCallingMixin.

    # _do_send_via_agent_backend is provided by SendHandlersMixin.

    # Future work: Undo grouping for AI edits (user can undo all edits from one turn with Ctrl+Z).
    # Previous attempt used enterUndoContext("AI Edit") / leaveUndoContext() but leaveUndoContext
    # was failing in some environments. Revisit when integrating with the async tool-calling path.

    # _run_web_research is provided by SendHandlersMixin.



    # _spawn_llm_worker is provided by ToolCallingMixin.

    # _spawn_final_stream is provided by ToolCallingMixin.

    # _start_tool_calling_async is provided by ToolCallingMixin.

    # _start_simple_stream_async is provided by ToolCallingMixin.

    def disposing(self, evt):
        try:
            from plugin.framework.event_bus import global_event_bus
            global_event_bus.unsubscribe("mcp:request", self._on_mcp_request)
            global_event_bus.unsubscribe("mcp:result", self._on_mcp_result)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# StopButtonListener - allows user to cancel the AI request
# ---------------------------------------------------------------------------

class StopButtonListener(BaseActionListener):
    """Listener for the Stop button - sets a flag in SendButtonListener to halt loops."""

    def __init__(self, send_listener):
        self.send_listener = send_listener

    def on_action_performed(self, evt):
        if self.send_listener:
            self.send_listener.stop_requested = True

            # 1. Stop the HTTP client immediately (breaks hanging reads)
            client = getattr(self.send_listener, "client", None)
            if client and hasattr(client, "stop"):
                try:
                    client.stop()
                except Exception as e:
                    log.error("StopButton error stopping client: %s", e)

            # 2. If an external agent backend is running, tell it to stop
            adapter = getattr(self.send_listener, "_current_agent_backend", None)
            if adapter and hasattr(adapter, "stop"):
                try:
                    adapter.stop()
                except Exception as e:
                    log.error("StopButton error stopping agent backend: %s", e)
            # Update status immediately
            self.send_listener._set_status("Stopping...")


# ---------------------------------------------------------------------------
# ClearButtonListener - resets the conversation
# ---------------------------------------------------------------------------

class ClearButtonListener(BaseActionListener):
    """Listener for the Clear button - resets conversation history."""

    def __init__(self, session, response_control, status_control, greeting=""):
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

    def on_action_performed(self, evt):
        self.session.clear()
        if self.response_control and self.response_control.getModel():
            text = self.greeting + "\n" if self.greeting else ""
            set_control_text(self.response_control, text)
        if self.status_control:
            self.status_control.setText("")