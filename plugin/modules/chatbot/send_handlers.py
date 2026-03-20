"""SendHandlersMixin: specialized send paths for the chat sidebar.

This mixin is used by SendButtonListener in panel.py and contains
alternate send flows that would otherwise bloat that class:

- Audio transcription fallback
- Direct image generation (Use Image model)
- External agent backends (Aider, Hermes)
- Web research sub-agent
"""

import queue
import logging
import json

from plugin.framework.errors import safe_json_loads

log = logging.getLogger(__name__)

class SendHandlersMixin:
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

            transcript_text = run_blocking_in_thread(
                self.ctx, self.client.transcribe_audio, wav_path, model=stt_model
            )

            # Clean up audio file
            import os

            try:
                os.remove(wav_path)
            except Exception:
                pass
            self.audio_wav_path = None

            if transcript_text:
                combined_text = query_text
                if transcript_text:
                    combined_text = (
                        (combined_text + "\n" + transcript_text).strip()
                        if combined_text
                        else transcript_text
                    )

                # Proceed to send with the transcript
                self._do_send_chat_with_tools(
                    combined_text, model, self._get_doc_type_str(model).lower()
                )
            else:
                self._terminal_status = "Ready"
                self._set_status("Ready")

        except Exception as e:
            doc_type = self._get_doc_type_str(model).lower() if model else "unknown"
            log.error("Transcription error in _transcribe_audio_async [doc: %s]: %s", doc_type, e)
            self._append_response(
                "\n[Transcription error: %s]\n" % format_error_message(e)
            )
            self._terminal_status = "Error"
            self._set_status("Error")

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
                if self.aspect_ratio_selector and hasattr(
                    self.aspect_ratio_selector, "getText"
                ):
                    aspect_ratio_str = self.aspect_ratio_selector.getText()

                aspect_map = {
                    "Square": "square",
                    "Landscape (16:9)": "landscape_16_9",
                    "Portrait (9:16)": "portrait_9_16",
                    "Landscape (3:2)": "landscape_3_2",
                    "Portrait (2:3)": "portrait_2_3",
                }
                mapped_aspect = aspect_map.get(aspect_ratio_str, "square")

                image_model_text = ""
                if self.image_model_selector and hasattr(
                    self.image_model_selector, "getText"
                ):
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
                    stop_checker=lambda: self.stop_requested,
                    doc_type="writer",
                    services=get_tools()._services,
                    caller="chat",
                    status_callback=lambda t: q.put(("status", t)),
                )
                try:
                    from plugin.framework.config import update_lru_history

                    update_lru_history(
                        self.ctx, base_size_val, "image_base_size_lru", ""
                    )
                except Exception as elru:
                    log.error("LRU update error: %s" % elru)

                import json

                res = get_tools().execute(
                    "generate_image",
                    tctx,
                    **{
                        "prompt": query_text,
                        "aspect_ratio": mapped_aspect,
                        "base_size": base_size_val,
                        "image_model": image_model_text,
                    }
                )
                result = json.dumps(res) if isinstance(res, dict) else str(res)
                data = safe_json_loads(result, default={})
                if isinstance(data, dict):
                    note = data.get("message", data.get("status", "done"))
                else:
                    log.error("Failed to parse generate_image result in _do_send_direct_image")
                    note = "done"
                q.put(("chunk", "[generate_image: %s]\n" % note))
                q.put(("stream_done", {}))
            except Exception as e:
                doc_type = self._get_doc_type_str(model).lower() if model else "unknown"
                log.error("Direct image path ERROR in _do_send_direct_image [doc: %s]: %s",
                          doc_type, e)
                from plugin.framework.errors import format_error_payload
                q.put(("error", format_error_payload(e)))

        from plugin.framework.worker_pool import run_in_background
        run_in_background(run_direct_image)
        try:
            toolkit = self.ctx.getServiceManager().createInstanceWithContext(
                "com.sun.star.awt.Toolkit", self.ctx
            )
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
            # Simple stream (no tools) does not add to ChatSession; just update status.
            self._terminal_status = "Stopped"
            self._set_status("Stopped")

        def on_error(e):
            from plugin.modules.http.client import format_error_message

            self._append_response("\n[%s]\n" % format_error_message(e))
            self._terminal_status = "Error"
            self._set_status("Error")

        from plugin.framework.async_stream import run_stream_drain_loop

        run_stream_drain_loop(
            q,
            toolkit,
            job_done,
            apply_chunk,
            on_stream_done=on_stream_done,
            on_stopped=on_stopped,
            on_error=on_error,
            on_status_fn=self._set_status,
            ctx=self.ctx,
            stop_checker=lambda: self.stop_requested,
        )
        if self._terminal_status != "Error":
            self._terminal_status = "Ready"

    def _do_send_via_agent_backend(self, query_text, model, doc_type_str):
        """Send via external agent backend (Aider, Hermes). No fallback to built-in on failure."""
        from plugin.framework.config import get_config
        from plugin.framework.document import get_document_context_for_chat
        from plugin.modules.agent_backend import get_backend

        self.session.add_user_message(query_text)
        self._append_response("\nYou: %s\n" % query_text)
        self._append_response("\n[Using external agent backend.]\n")
        self._append_response("AI: ")
        self._set_status("Starting agent...")

        document_url = ""
        try:
            if model and hasattr(model, "getURL"):
                document_url = str(model.getURL() or "")
        except Exception:
            pass

        max_context = int(get_config(self.ctx, "chat_context_length"))
        try:
            doc_context = get_document_context_for_chat(
                model,
                max_context,
                include_end=True,
                include_selection=True,
                ctx=self.ctx,
            )
        except Exception as e:
            self._append_response("\n[Document context error: %s]\n" % str(e))
            self._terminal_status = "Error"
            self._set_status("Error")
            return

        backend_id = str(
            get_config(self.ctx, "agent_backend.backend_id") or "builtin"
        ).strip().lower()
        adapter = get_backend(backend_id, ctx=self.ctx)
        if not adapter:
            self._append_response("\n[Agent backend '%s' not found.]\n" % backend_id)
            self._terminal_status = "Error"
            self._set_status("Error")
            return
        if not adapter.is_available(self.ctx):
            self._append_response(
                "\n[Agent backend '%s' is not available. Check Settings (path, install).]\n"
                % getattr(adapter, "display_name", backend_id)
            )
            self._terminal_status = "Error"
            self._set_status("Error")
            return

        q = queue.Queue()
        job_done = [False]
        self._current_agent_backend = adapter

        def run_agent():
            try:
                from plugin.framework.constants import CORE_DIRECTIVES
                from plugin.framework.config import as_bool

                # Lean system prompt for external agents: instructions + MCP connection info
                mcp_url = self._get_mcp_url()
                
                # Check if MCP is enabled; if so, tell the agent about it.
                mcp_instructions = ""
                if mcp_url and as_bool(get_config(self.ctx, "http.mcp_enabled")):
                    mcp_instructions = (
                        f"\n\n[MCP SERVER AVAILABLE]\n"
                        f"A Model Context Protocol (MCP) server is running at: {mcp_url}\n"
                        f"You can discover and use all LibreOffice tools (Writer, Calc, Draw) via this server.\n"
                        f"Target the current document by passing the 'X-Document-URL' header: {document_url}\n"
                    )

                lean_system_prompt = (
                    f"{CORE_DIRECTIVES}\n\n"
                    f"You are currently interacting with a LibreOffice document.\n"
                    f"{mcp_instructions}\n"
                    f"Please proceed with the user's request."
                )
                
                # Add optional instructions from settings
                extra = str(get_config(self.ctx, "additional_instructions") or "").strip()
                if extra:
                    lean_system_prompt += "\n\n" + extra

                adapter.send(
                    queue=q,
                    user_message=query_text,
                    document_context=doc_context,
                    document_url=document_url,
                    system_prompt=lean_system_prompt,
                    mcp_url=mcp_url,
                    stop_checker=lambda: self.stop_requested,
                )
            except Exception as e:
                log.error("Agent backend ERROR in _do_send_via_agent_backend [backend: %s, doc: %s]: %s",
                          backend_id, doc_type_str, e)
                from plugin.framework.errors import format_error_payload
                q.put(("error", format_error_payload(e)))
            finally:
                self._current_agent_backend = None

        from plugin.framework.worker_pool import run_in_background
        run_in_background(run_agent)

        try:
            toolkit = self.ctx.getServiceManager().createInstanceWithContext(
                "com.sun.star.awt.Toolkit", self.ctx
            )
        except Exception as e:
            self._append_response("\n[Error: %s]\n" % str(e))
            self._terminal_status = "Error"
            self._current_agent_backend = None
            return

        def apply_chunk(text, is_thinking=False):
            self._append_response(text)

        def on_stream_done(item):
            job_done[0] = True
            self._terminal_status = "Ready"
            self._set_status("Ready")
            return True

        def on_stopped():
            # Ensure conversation roles alternate user/assistant when stopping an
            # external agent backend mid-response.
            self.session.add_assistant_message(content="No response.")
            job_done[0] = True
            self._terminal_status = "Stopped"
            self._set_status("Stopped")
            self._append_response("\n[Stopped by user]\n")

        def on_error(e):
            from plugin.modules.http.client import format_error_message

            self._append_response("\n[Error: %s]\n" % format_error_message(e))
            self._terminal_status = "Error"
            self._set_status("Error")

        def on_approval_required(item):
            # item = ("approval_required", description, tool_name, args, request_id)
            from plugin.framework.dialogs import show_approval_dialog

            description = item[1] if len(item) > 1 else ""
            tool_name = item[2] if len(item) > 2 else ""
            request_id = item[4] if len(item) > 4 else None
            approved = show_approval_dialog(self.ctx, description, tool_name)
            if request_id is not None and hasattr(adapter, "submit_approval"):
                try:
                    adapter.submit_approval(request_id, approved)
                except Exception:
                    pass

        from plugin.framework.async_stream import run_stream_drain_loop

        run_stream_drain_loop(
            q,
            toolkit,
            job_done,
            apply_chunk,
            on_stream_done=on_stream_done,
            on_stopped=on_stopped,
            on_error=on_error,
            on_status_fn=self._set_status,
            ctx=self.ctx,
            stop_checker=lambda: self.stop_requested,
            on_approval_required=on_approval_required,
        )
        if self._terminal_status not in ("Error", "Stopped"):
            self._terminal_status = "Ready"
        self._current_agent_backend = None

    def _run_web_research(self, query_text, model):
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

            show_thinking = as_bool(get_config(self.ctx, "chatbot.show_search_thinking"))
        except Exception:
            show_thinking = False

        history_text = ""
        if self.response_control and self.response_control.getModel():
            history_text = self.response_control.getModel().Text or ""

        def run_search():
            doc_type = (
                "calc"
                if is_calc(model)
                else "draw"
                if is_draw(model)
                else "writer"
            )
            try:

                def status_cb(msg):
                    q.put(("status", msg))

                # Always push thinking to the queue so the drain loop stays active
                # (processEventsToIdle fires each iteration). Display is controlled
                # by show_thinking in apply_chunk below.
                def thinking_cb(msg):
                    q.put(("thinking", msg))

                from plugin.framework.tool_context import ToolContext

                tctx = ToolContext(
                    doc=model,
                    ctx=self.ctx,
                    stop_checker=lambda: self.stop_requested,
                    doc_type=doc_type,
                    services=get_tools()._services,
                    caller="chat",
                    status_callback=status_cb,
                    append_thinking_callback=thinking_cb,
                )

                import json

                res = get_tools().execute(
                    "web_research",
                    tctx,
                    **{"query": query_text, "history_text": history_text}
                )
                result = json.dumps(res) if isinstance(res, dict) else str(res)

                data = safe_json_loads(result)
                if not isinstance(data, dict):
                    from plugin.framework.errors import AgentParsingError, format_error_payload
                    log.error("Failed to parse web_research result in _run_web_research [doc: %s]", doc_type)
                    parsed_err = AgentParsingError("Invalid JSON from web search tool.", details={"raw_result": result})
                    data = format_error_payload(parsed_err)

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
                log.error("Web research path ERROR in _run_web_research [doc: %s]: %s", doc_type, e)
                from plugin.framework.errors import format_error_payload
                q.put(("error", format_error_payload(e)))

        from plugin.framework.worker_pool import run_in_background
        run_in_background(run_search)

        try:
            toolkit = self.ctx.getServiceManager().createInstanceWithContext(
                "com.sun.star.awt.Toolkit", self.ctx
            )
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

        from plugin.framework.async_stream import run_stream_drain_loop

        run_stream_drain_loop(
            q,
            toolkit,
            job_done,
            apply_chunk,
            on_stream_done=on_stream_done,
            on_stopped=on_stopped,
            on_error=on_error,
            on_status_fn=self._set_status,
            ctx=self.ctx,
            stop_checker=lambda: self.stop_requested,
        )

    def _get_mcp_url(self):
        """Construct the local MCP server URL from config."""
        try:
            from plugin.framework.config import get_config
            
            port = get_config(self.ctx, "http.mcp_port") or 8765
            host = get_config(self.ctx, "http.host") or "localhost"
            use_ssl = get_config(self.ctx, "http.use_ssl")
            scheme = "https" if use_ssl else "http"
            return f"{scheme}://{host}:{port}"
        except Exception:
            return None
