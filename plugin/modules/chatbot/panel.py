"""Chat sidebar panel logic: session, send/tool loop, and button listeners.

ChatSession holds conversation history. SendButtonListener drives the
streaming tool-calling loop. StopButtonListener and ClearButtonListener
are wired by panel_factory. UNO UI element factory and XDL wiring
remain in panel_factory.py.
"""

import json
import queue
import threading

import uno
import unohelper
from plugin.framework.uno_helpers import get_active_document
from com.sun.star.awt import XActionListener

from plugin.framework.logging import agent_log, debug_log, update_activity_state
from plugin.framework.uno_helpers import get_checkbox_state
from plugin.framework.async_stream import run_stream_completion_async, run_stream_drain_loop
from plugin.framework.history_db import get_chat_history

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
                debug_log("ChatSession history load error: %s" % e, context="Chat")

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

from com.sun.star.awt import XTextListener

class QueryTextListener(unohelper.Base, XTextListener):
    def __init__(self, send_button):
        self.send_button = send_button

    def textChanged(self, ev):
        try:
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
                debug_log("QueryTextListener: toggle label '%s' -> '%s'" % (btn_model.Label, new_label), context="Chat")
                btn_model.Label = new_label
            else:
                debug_log("QueryTextListener: label already '%s'" % new_label, context="Chat")
        except Exception as e:
            debug_log("QueryTextListener error: %s" % e, context="Chat")

    def disposing(self, ev):
        pass

# ---------------------------------------------------------------------------
# SendButtonListener - handles Send button click with tool-calling loop
# ---------------------------------------------------------------------------

class SendButtonListener(unohelper.Base, XActionListener):
    """Listener for the Send button - runs chat with document, supports tool-calling."""

    def __init__(self, ctx, frame, send_control, stop_control, query_control, response_control, image_model_selector, model_selector, status_control, session, direct_image_checkbox=None, aspect_ratio_selector=None, base_size_input=None, web_search_checkbox=None, ensure_path_fn=None):
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
        self.web_search_checkbox = web_search_checkbox
        self.ensure_path_fn = ensure_path_fn
        self.initial_doc_type = None  # Set by _wireControls
        self.stop_requested = False
        self._terminal_status = "Ready"
        self._send_busy = False
        self.client = None
        self.audio_wav_path = None
        # Subscribe to MCP/tool bus events
        try:
            from plugin.main import get_tools
            event_bus = getattr(get_tools()._services, "events", None)
            if event_bus:
                event_bus.subscribe("mcp:request", self._on_mcp_request)
                event_bus.subscribe("mcp:result", self._on_mcp_result)
                from plugin.framework.logging import debug_log
                debug_log(f"*** SendButtonListener subscribed to MCP events on services.events (id={id(event_bus)}) ***", context="Chat")
        except Exception as e:
            from plugin.framework.logging import debug_log
            debug_log("MCP subscribe error: %s" % e, context="Chat")

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
                debug_log("_set_status: NO CONTROL for '%s'" % text, context="Chat")
        except Exception as e:
            debug_log("_set_status('%s') EXCEPTION: %s" % (text, e), context="Chat")

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
                current = self.response_control.getModel().Text or ""
                self.response_control.getModel().Text = current + text
                self._scroll_response_to_bottom()
        except Exception:
            pass

    def _on_mcp_request(self, tool="", args=None, method=None, **kwargs):
        """Handle MCP request events from the bus (background thread)."""
        try:
            from plugin.framework.logging import format_tool_call_for_display, debug_log
            fmt_str = format_tool_call_for_display(tool, args, method)
            debug_log(f"MCP Request (hidden from UI): {fmt_str}", context="Chat")
        except Exception as e:
            from plugin.framework.logging import debug_log
            debug_log("_on_mcp_request error: %s" % e, context="Chat")

    def _on_mcp_result(self, tool="", result_snippet="", **kwargs):
        """Handle MCP result events from the bus (background thread)."""
        from plugin.framework.main_thread import execute_on_main_thread

        def _update_ui():
            try:
                from plugin.framework.logging import format_tool_result_for_display
                fmt_str = format_tool_result_for_display(tool, result_snippet, args=kwargs.get("args"))
                self._append_response(f"[MCP Result] {fmt_str}\n")
            except Exception as e:
                from plugin.framework.logging import debug_log
                debug_log("_on_mcp_result UI update error: %s" % e, context="Chat")

        try:
            execute_on_main_thread(_update_ui)
        except Exception as e:
            from plugin.framework.logging import debug_log
            debug_log("_on_mcp_result post error: %s" % e, context="Chat")

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

    def actionPerformed(self, evt):
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
                if self.query_control and self.query_control.getModel().Text.strip():
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
            self._append_response("\n\n[Error: %s]\n%s\n" % (str(e), tb))
            debug_log("SendButton error: %s\n%s" % (e, tb), context="Chat")
        finally:
            self._send_busy = False
            debug_log("actionPerformed finally: resetting UI", context="Chat")
            self._set_status(self._terminal_status)
            if self.send_control and self.send_control.getModel().Label not in ("Record", "Stop Rec"):
                # if empty, set to Record (when recording available) else Send
                if self.query_control and (self.query_control.getModel().Text.strip() or self.audio_wav_path):
                    self.send_control.getModel().Label = "Send"
                else:
                    self.send_control.getModel().Label = "Record" if HAS_RECORDING else "Send"
            self._set_button_states(send_enabled=True, stop_enabled=False)
            debug_log("control returned to LibreOffice", context="Chat")
            update_activity_state("")  # clear phase so watchdog does not report after we return

    def _transcribe_audio_async(self, wav_path, stt_model, model, query_text=""):
        """Transcribe audio asynchronously and then proceed to chat."""
        from plugin.framework.async_stream import run_blocking_in_thread
        from plugin.modules.http.client import format_error_message
        
        self._set_status("Transcribing audio...")
        self._append_response("\n[Transcribing audio...]\n")
        
        try:
            # Ensure client is initialized
            if not self.client:
                from plugin.framework.config import get_api_config
                from plugin.modules.http.client import LlmClient
                api_config = get_api_config(self.ctx)
                self.client = LlmClient(api_config, self.ctx)
            
            transcript_text = run_blocking_in_thread(self.ctx, self.client.transcribe_audio, wav_path, model=stt_model)
            
            # Clean up audio file
            import os
            try: os.remove(wav_path)
            except Exception: pass
            self.audio_wav_path = None
            
            if transcript_text:
                combined_text = query_text
                if transcript_text:
                    combined_text = (combined_text + "\n" + transcript_text).strip() if combined_text else transcript_text
                
                # Proceed to send with the transcript
                self._do_send_chat_with_tools(combined_text, model, self._get_doc_type_str(model).lower())
            else:
                self._terminal_status = "Ready"
                self._set_status("Ready")
            
        except Exception as e:
            self._append_response("\n[Transcription error: %s]\n" % format_error_message(e))
            self._terminal_status = "Error"
            self._set_status("Error")

    def _get_doc_type_str(self, model):
        from plugin.framework.document import is_writer, is_calc, is_draw
        return "Calc" if is_calc(model) else "Draw" if is_draw(model) else "Writer" if is_writer(model) else "Unknown"

    def _do_send(self):
        self._set_status("Starting...")
        update_activity_state("do_send")
        debug_log("=== _do_send START ===", context="Chat")

        # Ensure extension directory is on sys.path (injected by panel_factory to avoid circular import)
        if self.ensure_path_fn:
            self.ensure_path_fn(self.ctx)

        # 1. Get document model
        self._set_status("Getting document...")
        debug_log("_do_send: getting document model...", context="Chat")
        model = self._get_document_model()
        if not model:
            debug_log("_do_send: no document found", context="Chat")
            self._append_response("\n[No compatible LibreOffice document (Writer, Calc, or Draw) found in the active window.]\n")
            self._terminal_status = "Error"
            return
        debug_log("_do_send: got document model OK", context="Chat")

        from plugin.framework.document import is_writer, is_calc, is_draw
        doc_type_str = "Calc" if is_calc(model) else "Draw" if is_draw(model) else "Writer" if is_writer(model) else "Unknown"
        debug_log("_do_send: detected document type: %s" % doc_type_str, context="Chat")
        
        if self.initial_doc_type and doc_type_str != self.initial_doc_type:
            err_msg = "[Internal Error: Document type changed from %s to %s! Please file an error.]" % (self.initial_doc_type, doc_type_str)
            debug_log("_do_send ERROR: %s" % err_msg, context="Chat")
            self._append_response("\n%s\n" % err_msg)
            self._terminal_status = "Error"
            return

        if doc_type_str == "Unknown":
            err_msg = "[Internal Error: Could not identify document type for %s. Please report this!]" % (model.getImplementationName() if hasattr(model, "getImplementationName") else "Unknown")
            debug_log("_do_send ERROR: %s" % err_msg, context="Chat")
            self._append_response("\n%s\n" % err_msg)
            self._terminal_status = "Error"
            return

        # Get user query and clear field (before loading tools, so direct-image path can return early)
        query_text = ""
        if self.query_control and self.query_control.getModel():
            query_text = (self.query_control.getModel().Text or "").strip()

        # Audio implies we have input even if text is empty
        if not query_text and not self.audio_wav_path:
            self._terminal_status = ""
            return

        if self.query_control and self.query_control.getModel():
            self.query_control.getModel().Text = ""

        # Transcription Fallback check
        if self.audio_wav_path:
            from plugin.framework.config import get_text_model, get_current_endpoint, has_native_audio, get_stt_model
            current_model = get_text_model(self.ctx)
            current_endpoint = get_current_endpoint(self.ctx)
            
            if has_native_audio(self.ctx, current_model, current_endpoint) is False:
                stt_model = get_stt_model(self.ctx)
                if stt_model:
                    debug_log("_do_send: model %s has no native audio, using stt fallback %s" % (current_model, stt_model), context="Chat")
                    self._transcribe_audio_async(self.audio_wav_path, stt_model, model, query_text=query_text)
                    return
                else:
                    err_msg = "[Model %s does not support native audio. Please select an STT Model in Settings.]" % current_model
                    self._append_response("\n%s\n" % err_msg)
                    self._terminal_status = "Error"
                    self._set_status("Error")
                    return
            else:
                debug_log("_do_send: model %s supports native audio, proceeding" % current_model, context="Chat")

        # Optional web-search path
        web_search_checked = False
        if self.web_search_checkbox:
            try:
                web_search_checked = (get_checkbox_state(self.web_search_checkbox) == 1)
            except Exception as e:
                debug_log("_do_send: Web search checkbox read error: %s" % e, context="Chat")
        if web_search_checked:
            debug_log("_do_send: using web search sub-agent — skip chat model and direct image", context="Chat")
            self._run_web_search(query_text, model)
            return

        # Direct image path
        direct_image_checked = False
        if self.direct_image_checkbox:
            try:
                direct_image_checked = (get_checkbox_state(self.direct_image_checkbox) == 1)
            except Exception as e:
                debug_log("_do_send: Use Image model checkbox read error: %s" % e, context="Chat")
        if direct_image_checked:
            debug_log("_do_send: using image model (direct) — skip chat model", context="Chat")
            self._do_send_direct_image(query_text, model)
            return

        # Regular Chat with Tools or Streams
        self._do_send_chat_with_tools(query_text, model, doc_type_str.lower())

    def _do_send_direct_image(self, query_text, model):
        self._append_response("\nYou: %s\n" % query_text)
        self._append_response("\n[Using image model (direct).]\n")
        self._append_response("AI: Creating image...\n")
        self._set_status("Creating image...")
        q = queue.Queue()
        job_done = [False]

        def run_direct_image():
            try:
                aspect_ratio_str = "Square"
                if self.aspect_ratio_selector and hasattr(self.aspect_ratio_selector, "getText"):
                    aspect_ratio_str = self.aspect_ratio_selector.getText()
                    
                aspect_map = {
                    "Square": "square",
                    "Landscape (16:9)": "landscape_16_9",
                    "Portrait (9:16)": "portrait_9_16",
                    "Landscape (3:2)": "landscape_3_2",
                    "Portrait (2:3)": "portrait_2_3"
                }
                mapped_aspect = aspect_map.get(aspect_ratio_str, "square")
                
                image_model_text = ""
                if self.image_model_selector and hasattr(self.image_model_selector, "getText"):
                    image_model_text = self.image_model_selector.getText()

                base_size_val = 512
                if self.base_size_input:
                    if hasattr(self.base_size_input, "getText"):
                        base_size_val = self.base_size_input.getText()
                    elif hasattr(self.base_size_input.getModel(), "Text"):
                        base_size_val = self.base_size_input.getModel().Text
                try:
                    base_size_val = int(base_size_val)
                except (ValueError, TypeError):
                    base_size_val = 512

                from plugin.main import get_tools
                from plugin.framework.tool_context import ToolContext
                tctx = ToolContext(
                    doc=model,
                    ctx=self.ctx,
                    doc_type="writer",
                    services=get_tools()._services,
                    caller="chat",
                    status_callback=lambda t: q.put(("status", t))
                )
                try:
                    from plugin.framework.config import update_lru_history
                    update_lru_history(self.ctx, base_size_val, "image_base_size_lru", "")
                except Exception as elru:
                    debug_log("LRU update error: %s" % elru, context="Chat")
                    
                import json
                res = get_tools().execute(
                    "generate_image",
                    tctx,
                    **{
                        "prompt": query_text,
                        "aspect_ratio": mapped_aspect,
                        "base_size": base_size_val,
                        "image_model": image_model_text
                    }
                )
                result = json.dumps(res) if isinstance(res, dict) else str(res)
                try:
                    data = json.loads(result)
                    note = data.get("message", data.get("status", "done"))
                except Exception:
                    note = "done"
                q.put(("chunk", "[generate_image: %s]\n" % note))
                q.put(("stream_done", {}))
            except Exception as e:
                debug_log("Direct image path ERROR: %s" % e, context="Chat")
                q.put(("error", e))

        threading.Thread(target=run_direct_image, daemon=True).start()
        try:
            toolkit = self.ctx.getServiceManager().createInstanceWithContext(
                "com.sun.star.awt.Toolkit", self.ctx)
        except Exception as e:
            self._append_response("\n[Error: %s]\n" % str(e))
            self._terminal_status = "Error"
            return

        def apply_chunk(chunk_text, is_thinking=False):
            self._append_response(chunk_text)

        def on_stream_done(response):
            job_done[0] = True
            return True

        def on_stopped():
            self._terminal_status = "Stopped"
            self._set_status("Stopped")

        def on_error(e):
            from plugin.modules.http.client import format_error_message
            self._append_response("\n[%s]\n" % format_error_message(e))
            self._terminal_status = "Error"
            self._set_status("Error")

        run_stream_drain_loop(
            q, toolkit, job_done, apply_chunk,
            on_stream_done=on_stream_done,
            on_stopped=on_stopped,
            on_error=on_error,
            on_status_fn=self._set_status,
            ctx=self.ctx,
        )
        if self._terminal_status != "Error":
            self._terminal_status = "Ready"

    def _do_send_chat_with_tools(self, query_text, model, doc_type_str):
        try:
            debug_log("_do_send: importing core modules...", context="Chat")
            from plugin.framework.config import get_config, get_api_config, update_lru_history, validate_api_config, set_config, set_image_model, get_current_endpoint
            from plugin.modules.http.client import LlmClient
            from plugin.framework.document import get_document_context_for_chat
            from plugin.main import get_tools
            debug_log("_do_send: core modules imported OK", context="Chat")
        except Exception as e:
            debug_log("_do_send: core import FAILED: %s" % e, context="Chat")
            self._append_response("\n[Import error - core: %s]\n" % e)
            self._terminal_status = "Error"
            return
            
        try:
            debug_log("_do_send: loading %s schema..." % doc_type_str, context="Chat")
            active_tools = get_tools().get_openai_schemas(doc_type=doc_type_str)

            def execute_fn(name, args, doc, ctx, status_callback=None, append_thinking_callback=None, stop_checker=None):
                import json
                from plugin.framework.tool_context import ToolContext
                tctx = ToolContext(
                    doc=doc,
                    ctx=ctx,
                    doc_type=doc_type_str,
                    services=get_tools()._services,
                    caller="chat",
                    status_callback=status_callback,
                    append_thinking_callback=append_thinking_callback,
                    stop_checker=stop_checker
                )
                try:
                    res = get_tools().execute(name, tctx, **args)
                    return json.dumps(res) if isinstance(res, dict) else str(res)
                except Exception as e:
                    return json.dumps({"status": "error", "message": str(e)})
        except Exception as e:
            debug_log("_do_send: tool import FAILED: %s" % e, context="Chat")
            self._append_response("\n[Import error - tools: %s]\n" % e)
            self._terminal_status = "Error"
            return

        extra_instructions = get_config(self.ctx, "additional_instructions", "") or ""
        from plugin.framework.constants import get_chat_system_prompt_for_document
        self.session.messages[0]["content"] = get_chat_system_prompt_for_document(model, extra_instructions)

        if self.model_selector:
            selected_model = self.model_selector.getText()
            if selected_model:
                set_config(self.ctx, "text_model", selected_model)
                current_endpoint = get_current_endpoint(self.ctx)
                update_lru_history(self.ctx, selected_model, "model_lru", current_endpoint)
                debug_log("_do_send: text model updated to %s" % selected_model, context="Chat")
        if self.image_model_selector:
            selected_image_model = self.image_model_selector.getText()
            if selected_image_model:
                set_image_model(self.ctx, selected_image_model)
                debug_log("_do_send: image model updated to %s" % selected_image_model, context="Chat")

        max_context = int(get_config(self.ctx, "chat_context_length", 8000))
        max_tokens = int(get_config(self.ctx, "chat_max_tokens", 16384))
        debug_log("_do_send: config loaded: max_tokens=%d, max_context=%d" % (max_tokens, max_context), context="Chat")

        use_tools = True

        api_config = get_api_config(self.ctx)
        ok, err_msg = validate_api_config(api_config)
        if not ok:
            self._append_response("\n[%s]\n" % err_msg)
            self._terminal_status = "Error"
            self._set_status("Error")
            return

        if not self.client:
            self.client = LlmClient(api_config, self.ctx)
        else:
            self.client.config = api_config
        client = self.client

        self._set_status("Reading document...")
        try:
            doc_text = get_document_context_for_chat(model, max_context, include_end=True, include_selection=True, ctx=self.ctx)
            debug_log("_do_send: document context length=%d" % len(doc_text), context="Chat")
            agent_log("chat_panel.py:doc_context", "Document context for AI", data={"doc_length": len(doc_text), "doc_prefix_first_200": (doc_text or "")[:200], "max_context": max_context}, hypothesis_id="B")
            self.session.update_document_context(doc_text)
        except Exception as e:
            debug_log("_do_send: document context FAILED: %s" % e, context="Chat")
            self._append_response("\n[Document unavailable or closed.]\n")
            self._terminal_status = "Error"
            self._set_status("Error")
            return

        # If there's audio, embed it
        if self.audio_wav_path:
            import base64
            try:
                with open(self.audio_wav_path, "rb") as f:
                    wav_data = f.read()
                b64_audio = base64.b64encode(wav_data).decode("utf-8")
                audio_msg = {
                    "type": "input_audio",
                    "input_audio": {
                        "data": b64_audio,
                        "format": "wav"
                    }
                }

                content_list = []
                if query_text:
                    content_list.append({"type": "text", "text": query_text})
                content_list.append(audio_msg)

                self.session.add_user_message(content_list)

                display_text = query_text + " [Audio Attached]" if query_text else "[Audio Message]"
                self._append_response("\nYou: %s\n" % display_text)
                # Note: We do NOT delete the audio file yet, in case native call fails and we need STT fallback
            except Exception as e:
                debug_log("_do_send: Error reading audio: %s" % e, context="Chat")
                self.session.add_user_message(query_text)
                self._append_response("\nYou: %s\n" % query_text)
                self.audio_wav_path = None
        else:
            self.session.add_user_message(query_text)
            self._append_response("\nYou: %s\n" % query_text)

        self._append_response("\n[Using chat model.]\n")
        debug_log("_do_send: using chat model", context="Chat")

        self._set_status("Connecting to AI (tools=%s)..." % use_tools)
        debug_log("_do_send: calling AI, use_tools=%s, messages=%d" % (use_tools, len(self.session.messages)), context="Chat")

        max_tool_rounds = api_config.get("chat_max_tool_rounds", DEFAULT_MAX_TOOL_ROUNDS)
        self._start_tool_calling_async(client, model, max_tokens, active_tools, execute_fn, max_tool_rounds, query_text=query_text)

        debug_log("=== _do_send END (async started) ===", context="Chat")

    # Future work: Undo grouping for AI edits (user can undo all edits from one turn with Ctrl+Z).
    # Previous attempt used enterUndoContext("AI Edit") / leaveUndoContext() but leaveUndoContext
    # was failing in some environments. Revisit when integrating with the async tool-calling path.

    def _run_web_search(self, query_text, model):
        """Run the web_research tool via the sub-agent and stream its result into the response area."""
        from plugin.modules.http.client import format_error_message
        from plugin.main import get_tools
        from plugin.framework.document import is_calc, is_draw

        self._append_response("\nYou: %s\n" % query_text)
        self._append_response("\n[Using research chat.]\n")
        self._set_status("Starting research...")

        # Persist user message to the research session
        self.session.add_user_message(query_text)

        q = queue.Queue()
        job_done = [False]
        # Read show_thinking before spawning the thread so apply_chunk can use it
        try:
            from plugin.framework.config import get_config, as_bool
            show_thinking = as_bool(get_config(self.ctx, "show_search_thinking", False))
        except Exception:
            show_thinking = False

        history_text = ""
        if self.response_control and self.response_control.getModel():
            history_text = self.response_control.getModel().Text or ""
        
        def run_search():
            try:

                def status_cb(msg):
                    q.put(("status", msg))

                # Always push thinking to the queue so the drain loop stays active
                # (processEventsToIdle fires each iteration). Display is controlled
                # by show_thinking in apply_chunk below.
                def thinking_cb(msg):
                    q.put(("thinking", msg))

                from plugin.framework.tool_context import ToolContext
                doc_type = "calc" if is_calc(model) else "draw" if is_draw(model) else "writer"
                tctx = ToolContext(
                    doc=model,
                    ctx=self.ctx,
                    doc_type=doc_type,
                    services=get_tools()._services,
                    caller="chat",
                    status_callback=status_cb,
                    append_thinking_callback=thinking_cb,
                    stop_checker=lambda: self.stop_requested
                )

                import json
                res = get_tools().execute(
                    "web_research",
                    tctx,
                    **{"query": query_text, "history_text": history_text}
                )
                result = json.dumps(res) if isinstance(res, dict) else str(res)

                try:
                    data = json.loads(result)
                except Exception:
                    data = {"status": "error", "message": "Invalid JSON from web search tool."}

                if data.get("status") == "ok":
                    answer = data.get("result", "")
                    if not isinstance(answer, str):
                        answer = str(answer)
                    msg = "AI (research): %s\n" % answer
                    q.put(("chunk", msg))
                    # Persist assistant result to current session
                    self.session.add_assistant_message(content=msg)
                else:
                    msg = data.get("message", "Unknown research error.")
                    q.put(("chunk", "[Research error: %s]\n" % msg))

                q.put(("stream_done", {}))
            except Exception as e:
                q.put(("error", e))

        threading.Thread(target=run_search, daemon=True).start()

        try:
            toolkit = self.ctx.getServiceManager().createInstanceWithContext(
                "com.sun.star.awt.Toolkit", self.ctx)
        except Exception as e:
            self._append_response("\n[Error: %s]\n" % str(e))
            self._terminal_status = "Error"
            self._set_status("Error")
            return

        def apply_chunk(chunk_text, is_thinking=False):
            # Thinking items always flow through the queue to keep the drain loop
            # active, but we only display them if the setting is on.
            if is_thinking and not show_thinking:
                return
            self._append_response(chunk_text)

        def on_stream_done(response):
            job_done[0] = True
            if self._terminal_status != "Error":
                self._terminal_status = "Ready"
                self._set_status("Ready")
            return True

        def on_stopped():
            # Web research cannot currently be cancelled mid-run; treat Stop as best-effort.
            self._terminal_status = "Stopped"
            self._set_status("Stopped")

        def on_error(e):
            err_msg = format_error_message(e)
            self._append_response("\n[Research Chat error: %s]\n" % err_msg)
            self._terminal_status = "Error"
            self._set_status("Error")

        run_stream_drain_loop(
            q, toolkit, job_done, apply_chunk,
            on_stream_done=on_stream_done,
            on_stopped=on_stopped,
            on_error=on_error,
            on_status_fn=self._set_status,
            ctx=self.ctx,
        )



    def _spawn_llm_worker(self, q, client, max_tokens, tools, round_num, query_text=None):
        """Spawn a background thread that streams the LLM response into q."""
        update_activity_state("tool_loop", round_num=round_num)
        debug_log("Tool loop round %d: sending %d messages to API..." % (round_num, len(self.session.messages)), context="Chat")
        self._set_status("Thinking..." if round_num == 0 else "Connecting (round %d)..." % (round_num + 1))

        def run():
            try:
                response = client.stream_request_with_tools(
                    self.session.messages, max_tokens, tools=tools,
                    append_callback=lambda t: q.put(("chunk", t)),
                    append_thinking_callback=lambda t: q.put(("thinking", t)),
                    stop_checker=lambda: self.stop_requested,
                )
                if self.stop_requested:
                    q.put(("stopped",))
                else:
                    update_activity_state("tool_loop", round_num=round_num)
                    q.put(("stream_done", response))
            except Exception as e:
                debug_log("Tool loop round %d: API ERROR: %s" % (round_num, e), context="Chat")
                q.put(("error", e))

        threading.Thread(target=run, daemon=True).start()

    def _spawn_final_stream(self, q, client, max_tokens):
        """Spawn a background thread for a final no-tools stream into q."""
        update_activity_state("exhausted_rounds")
        self._set_status("Finishing...")
        self._append_response("\nAI: ")

        def run_final():
            last_streamed = []
            try:
                def append_c(c):
                    q.put(("chunk", c))
                    last_streamed.append(c)

                def append_t(t):
                    q.put(("thinking", t))

                client.stream_chat_response(
                    self.session.messages, max_tokens, append_c, append_t,
                    stop_checker=lambda: self.stop_requested,
                )
                if self.stop_requested:
                    q.put(("stopped",))
                else:
                    q.put(("final_done", "".join(last_streamed)))
            except Exception as e:
                q.put(("error", e))

        threading.Thread(target=run_final, daemon=True).start()

    def _start_tool_calling_async(self, client, model, max_tokens, tools, execute_tool_fn, max_tool_rounds=None, query_text=None):
        """Tool-calling event loop: single queue, single main-thread loop.
        
        Background threads push messages onto q. The main thread dispatches
        on message type, keeping the UI responsive via processEventsToIdle().
        """
        if max_tool_rounds is None:
            max_tool_rounds = DEFAULT_MAX_TOOL_ROUNDS
        debug_log("=== Tool-calling loop START (max %d rounds) ===" % max_tool_rounds, context="Chat")
        self._append_response("\nAI: ")

        q = queue.Queue()
        round_num = 0
        pending_tools = []
        ASYNC_TOOLS = {"web_research", "generate_image", "edit_image"}

        # Read config once for web research thinking display
        try:
            from plugin.framework.config import get_config, as_bool
            show_search_thinking = as_bool(get_config(self.ctx, "show_search_thinking", False))
        except Exception:
            show_search_thinking = False

        try:
            toolkit = self.ctx.getServiceManager().createInstanceWithContext(
                "com.sun.star.awt.Toolkit", self.ctx)
        except Exception as e:
            self._append_response("\n[Error: %s]\n" % str(e))
            self._terminal_status = "Error"
            self._set_status("Error")
            return

        # Check once whether execute_tool_fn accepts status_callback
        import inspect
        sig = inspect.signature(execute_tool_fn)
        supports_status = ("status_callback" in sig.parameters or "kwargs" in sig.parameters)

        # --- Thinking display state (mirrors run_stream_drain_loop behavior) ---
        thinking_open = [False]

        # --- Kick off the first LLM stream ---
        self._spawn_llm_worker(q, client, max_tokens, tools, round_num, query_text=query_text)

        def on_stream_done(item):
            nonlocal round_num, pending_tools
            # item can be ('stream_done', response) or ('tool_done', ...) or ('final_done', ...) or ('next_tool',)
            kind = item[0] if isinstance(item, (tuple, list)) else item
            data = item[1] if isinstance(item, (tuple, list)) and len(item) > 1 else None

            if kind == "stream_done":
                response = data
                tool_calls = response.get("tool_calls")
                if isinstance(tool_calls, list) and len(tool_calls) == 0:
                    tool_calls = None
                content = response.get("content")
                finish_reason = response.get("finish_reason")

                agent_log("chat_panel.py:tool_round", "Tool loop round response",
                          data={"round": round_num, "has_tool_calls": bool(tool_calls),
                                "num_tool_calls": len(tool_calls) if tool_calls else 0},
                          hypothesis_id="A")

                # If we were using audio and it reached here, cache success
                if self.audio_wav_path:
                    from plugin.framework.config import set_native_audio_support, get_text_model, get_current_endpoint
                    set_native_audio_support(self.ctx, get_text_model(self.ctx), get_current_endpoint(self.ctx), supported=True)
                    # Successful native call -> we can now delete the audio file
                    import os
                    try: os.remove(self.audio_wav_path)
                    except: pass
                    self.audio_wav_path = None

                # --- No tool calls: conversation is done ---
                if not tool_calls:
                    agent_log("chat_panel.py:exit_no_tools", "Exiting loop: no tool_calls",
                              data={"round": round_num}, hypothesis_id="A")
                    if content:
                        debug_log("Tool loop: Adding assistant message to session", context="Chat")
                        self.session.add_assistant_message(content=content)
                        self._append_response("\n")
                    elif finish_reason == "length":
                        self._append_response(
                            "\n[Response truncated -- the model ran out of tokens...]\n")
                    elif finish_reason == "content_filter":
                        self._append_response("\n[Content filter: response was truncated.]\n")
                    else:
                        self._append_response(
                            "\n[No text from model; any tool changes were still applied.]\n")
                    self._terminal_status = "Ready"
                    self._set_status("Ready")
                    return True  # EXIT the drain loop

                # --- Has tool calls: queue them up ---
                self.session.add_assistant_message(content=content, tool_calls=tool_calls)
                if content:
                    self._append_response("\n")

                pending_tools.extend(tool_calls)
                q.put(("next_tool",))
                return False

            elif kind == "next_tool":
                if not pending_tools or self.stop_requested:
                    # --- Advance to next round ---
                    if not self.stop_requested:
                        self._set_status("Sending results to AI...")
                    round_num += 1
                    if round_num >= max_tool_rounds:
                        agent_log("chat_panel.py:exit_exhausted",
                                  "Exiting loop: exhausted max_tool_rounds",
                                  data={"rounds": max_tool_rounds}, hypothesis_id="A")
                        self._spawn_final_stream(q, client, max_tokens)
                    else:
                        self._spawn_llm_worker(q, client, max_tokens, tools, round_num, query_text=query_text)
                    return False

                tc = pending_tools.pop(0)
                func_name = tc.get("function", {}).get("name", "unknown")
                func_args_str = tc.get("function", {}).get("arguments", "{}")
                call_id = tc.get("id", "")

                self._set_status("Running: %s" % func_name)
                self._append_response("[Running tool: %s...]\n" % func_name)
                update_activity_state("tool_execute", round_num=round_num, tool_name=func_name)

                try:
                    func_args = json.loads(func_args_str)
                except (json.JSONDecodeError, TypeError):
                    try:
                        import ast
                        func_args = ast.literal_eval(func_args_str)
                        if not isinstance(func_args, dict):
                            func_args = {}
                    except Exception:
                        func_args = {}

                agent_log("chat_panel.py:tool_execute", "Executing tool",
                          data={"tool": func_name, "round": round_num}, hypothesis_id="C,D,E")
                debug_log("Tool call: %s(%s)" % (func_name, func_args_str), context="Chat")

                image_model_override = self.image_model_selector.getText() if self.image_model_selector else None
                if image_model_override and func_name in ("generate_image", "edit_image"):
                    func_args["image_model"] = image_model_override

                def tool_status_callback(msg):
                    q.put(("status", msg))

                if func_name in ASYNC_TOOLS:
                    # --- ASYNC EXECUTION ---
                    def run_async():
                        try:
                            def tool_thinking_callback(msg):
                                q.put(("tool_thinking", msg))
                            
                            if supports_status:
                                res = execute_tool_fn(func_name, func_args, model, self.ctx,
                                                      status_callback=tool_status_callback,
                                                      append_thinking_callback=tool_thinking_callback,
                                                      stop_checker=lambda: self.stop_requested)
                            else:
                                res = execute_tool_fn(func_name, func_args, model, self.ctx,
                                                     stop_checker=lambda: self.stop_requested)
                            q.put(("tool_done", call_id, func_name, func_args_str, res))
                        except Exception as e:
                            q.put(("tool_done", call_id, func_name, func_args_str, json.dumps({"status": "error", "message": str(e)})))
                    
                    threading.Thread(target=run_async, daemon=True).start()
                else:
                    # --- SYNC EXECUTION (UNO tools) ---
                    try:
                        if supports_status:
                            res = execute_tool_fn(func_name, func_args, model, self.ctx,
                                                  status_callback=tool_status_callback)
                        else:
                            res = execute_tool_fn(func_name, func_args, model, self.ctx)
                        q.put(("tool_done", call_id, func_name, func_args_str, res))
                    except Exception as e:
                        q.put(("tool_done", call_id, func_name, func_args_str, json.dumps({"status": "error", "message": str(e)})))
                return False

            elif kind == "tool_done":
                call_id, func_name, func_args_str, result = item[1], item[2], item[3], item[4]
                
                debug_log("Tool result: %s" % result, context="Chat")
                try:
                    result_data = json.loads(result)
                    note = result_data.get("message", result_data.get("status", "done"))
                except Exception:
                    note = "done"
                self._append_response("[%s: %s]\n" % (func_name, note))
                if (func_name == "apply_document_content"
                        and (note or "").strip().startswith("Replaced 0 occurrence")):
                    params_display = func_args_str if len(func_args_str) <= 800 else func_args_str[:800] + "..."
                    self._append_response("[Debug: params %s]\n" % params_display)
                self.session.add_tool_result(call_id, result)

                # Trigger next tool
                q.put(("next_tool",))
                return False

            elif kind == "final_done":
                final_content = data
                if final_content:
                    self.session.add_assistant_message(content=final_content)
                    self._append_response("\n")
                self._terminal_status = "Ready"
                self._set_status("Ready")
                return True

            return False

        def on_stopped():
            self._terminal_status = "Stopped"
            self._set_status("Stopped")
            self._append_response("\n[Stopped by user]\n")

        def on_error(e):
            from plugin.modules.http.client import format_error_message, is_audio_unsupported_error
            from plugin.framework.config import set_native_audio_support, get_text_model, get_current_endpoint, get_stt_model
            
            current_model = get_text_model(self.ctx)
            current_endpoint = get_current_endpoint(self.ctx)
            
            # If native audio failed, cache it and try STT fallback
            if self.audio_wav_path and is_audio_unsupported_error(e):
                debug_log("Model %s failed native audio, caching and falling back to STT" % current_model, context="Chat")
                set_native_audio_support(self.ctx, current_model, current_endpoint, supported=False)
                
                stt_model = get_stt_model(self.ctx)
                if stt_model:
                    # Remove the failed message from session so we can retry with text
                    if self.session.messages and self.session.messages[-1]["role"] == "user":
                        # If it was a list (audio+text), just pop it
                        self.session.messages.pop()
                    
                    self._append_response("\n[Model does not support audio. Falling back to STT...]\n")
                    self._transcribe_audio_async(self.audio_wav_path, stt_model, model, query_text=query_text)
                    return

            # If we reached here, it's either not a modality error or STT is not configured
            err_msg = format_error_message(e)
            self._append_response("\n[API error: %s]\n" % err_msg)
            self._terminal_status = "Error"
            self._set_status("Error")
            # Cleanup audio if we aren't falling back
            if self.audio_wav_path:
                import os
                try: os.remove(self.audio_wav_path)
                except: pass
                self.audio_wav_path = None

        run_stream_drain_loop(
            q, toolkit, [False], self._append_response,
            on_stream_done=on_stream_done,
            on_stopped=on_stopped,
            on_error=on_error,
            on_status_fn=self._set_status,
            ctx=self.ctx,
            show_search_thinking=show_search_thinking,
        )

    def _start_simple_stream_async(self, client, max_tokens):
        """Start simple streaming (no tools) via async helper; returns immediately."""
        debug_log("=== Simple stream START ===", context="Chat")
        self._set_status("Thinking...")
        self._append_response("\nAI: ")

        last_user = ""
        doc_context = ""
        for msg in reversed(self.session.messages):
            if msg["role"] == "user" and not last_user:
                last_user = msg["content"]
            if msg["role"] == "system" and "[DOCUMENT CONTENT]" in (msg.get("content") or ""):
                doc_context = msg["content"]
        prompt = "%s\n\nUser question: %s" % (doc_context, last_user) if doc_context else last_user
        system_prompt = ""
        for msg in self.session.messages:
            if msg["role"] == "system" and "[DOCUMENT CONTENT]" not in (msg.get("content") or ""):
                system_prompt = msg["content"]
                break

        collected = []

        def apply_chunk(chunk_text, is_thinking=False):
            self._append_response(chunk_text)
            if not is_thinking:
                collected.append(chunk_text)

        def on_done():
            full_response = "".join(collected)
            self.session.add_assistant_message(content=full_response)
            self._terminal_status = "Ready"
            self._set_status("Ready")
            self._append_response("\n")
            if self.stop_requested:
                self._append_response("\n[Stopped by user]\n")

        def on_error(e):
            from plugin.modules.http.client import format_error_message
            err_msg = format_error_message(e)
            self._append_response("[Error: %s]\n" % err_msg)
            self._terminal_status = "Error"
            self._set_status("Error")

        run_stream_completion_async(
            self.ctx, client, prompt, system_prompt, max_tokens,
            apply_chunk, on_done, on_error, on_status_fn=self._set_status,
            stop_checker=lambda: self.stop_requested,
        )

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

class StopButtonListener(unohelper.Base, XActionListener):
    """Listener for the Stop button - sets a flag in SendButtonListener to halt loops."""

    def __init__(self, send_listener):
        self.send_listener = send_listener

    def actionPerformed(self, evt):
        if self.send_listener:
            self.send_listener.stop_requested = True
            # Update status immediately
            self.send_listener._set_status("Stopping...")

    def disposing(self, evt):
        pass


# ---------------------------------------------------------------------------
# ClearButtonListener - resets the conversation
# ---------------------------------------------------------------------------

class ClearButtonListener(unohelper.Base, XActionListener):
    """Listener for the Clear button - resets conversation history."""

    def __init__(self, session, response_control, status_control, greeting=""):
        self.session = session
        self.response_control = response_control
        self.status_control = status_control
        self.greeting = greeting

    def set_session(self, session, greeting=None):
        """Update the active session and optionally the greeting used for clear."""
        self.session = session
        if greeting is not None:
            self.greeting = greeting

    def actionPerformed(self, evt):
        self.session.clear()
        if self.response_control and self.response_control.getModel():
            text = self.greeting + "\n" if self.greeting else ""
            self.response_control.getModel().Text = text
        if self.status_control:
            self.status_control.setText("")

    def disposing(self, evt):
        pass
