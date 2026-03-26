"""ToolCallingMixin: core chat-with-tools engine for the sidebar.

This mixin is used by SendButtonListener in panel.py and contains the
multi-round tool-calling loop plus simple streaming fallback.
"""

import logging
import inspect
import dataclasses
import json
import queue

from plugin.framework.async_stream import (
    run_stream_completion_async,
    run_stream_drain_loop,
)
from plugin.framework.logging import agent_log, update_activity_state
from plugin.modules.http.errors import (
    format_error_message,
    is_audio_unsupported_error,
)
from plugin.framework.config import (
    get_api_config,
    get_config,
    get_current_endpoint,
    get_stt_model,
    get_text_model,
    set_image_model,
    set_native_audio_support,
    update_lru_history,
    validate_api_config,
)
from plugin.framework.constants import get_chat_system_prompt_for_document
from plugin.framework.document import get_document_context_for_chat
from plugin.framework.errors import (
    format_error_payload,
    safe_json_loads,
    WriterAgentException,
    ToolExecutionError,
    UnoObjectError,
    NetworkError,
    ConfigError
)
from plugin.modules.http.client import LlmClient
from plugin.framework.config import as_bool

from plugin.modules.chatbot.tool_loop_state import (
    ToolLoopState, ToolLoopEvent, EventKind,
    SpawnLLMWorkerEffect, SpawnToolWorkerEffect,
    UIEffect, LogAgentEffect, AddMessageEffect,
    UpdateActivityStateEffect, next_state
)

log = logging.getLogger(__name__)

# Default max tool rounds when not in config (get_api_config supplies chat_max_tool_rounds)
DEFAULT_MAX_TOOL_ROUNDS = 5


class ToolCallingMixin:
    """Tool loop state lives in ``sidebar_state.tool_loop`` when mixed with SendButtonListener."""

    @property
    def _sm_state(self):
        if not hasattr(self, "sidebar_state"):
            raise AttributeError(
                "ToolCallingMixin requires sidebar_state (SendButtonListener provides it)"
            )
        tl = self.sidebar_state.tool_loop
        if tl is None:
            raise RuntimeError("Tool loop state used without active session")
        return tl

    @_sm_state.setter
    def _sm_state(self, value):
        self.sidebar_state = dataclasses.replace(self.sidebar_state, tool_loop=value)

    def _do_send_chat_with_tools(self, query_text, model, doc_type_str):
        from plugin.framework.errors import UnoObjectError
        try:
            log.debug("_do_send: importing core modules...")
            from plugin.main import get_tools

            log.debug("_do_send: core modules imported OK")
        except Exception as e:
            if isinstance(e, UnoObjectError):
                log.error("_do_send: core import failed (UnoObjectError): %s" % e)
            else:
                log.error("_do_send: core import FAILED: %s" % e)
            self._append_response("\n[Import error - core: %s]\n" % e)
            self._terminal_status = "Error"
            return

        # Callback for updating active domain in the session
        def set_active_domain(domain):
            if hasattr(self, "session") and self.session:
                self.session.active_specialized_domain = domain
                log.debug("_do_send: updated active specialized domain to: %s", domain)

        try:
            log.debug("_do_send: loading %s schema..." % doc_type_str)
            active_domain = getattr(self.session, "active_specialized_domain", None) if hasattr(self, "session") else None
            active_tools = get_tools().get_schemas("openai", doc=model, active_domain=active_domain)

            def execute_fn(
                name,
                args,
                doc,
                ctx,
                status_callback=None,
                append_thinking_callback=None,
                stop_checker=None,
            ):
                import json
                import threading
                from plugin.framework.tool_context import ToolContext
                from plugin.main import get_tools as _get_tools

                # NOTE: Experimental planning/TodoStore wiring is intentionally
                # commented out. When enabling the hermes-style todo tool,
                # you can attach a session-scoped TodoStore here and expose it
                # via ToolContext.services, e.g.:
                #
                # from plugin.contrib.todo_store import TodoStore
                # if not hasattr(self, "_todo_store"):
                #     self._todo_store = TodoStore()
                # services = dict(_get_tools()._services)
                # services["todo_store"] = self._todo_store
                #
                # and then pass `services=services` into ToolContext below.

                approval_callback = None
                chat_append_callback = None
                if name == "web_research":
                    def chat_append_callback(text):
                        aq = getattr(self, "_active_q", None)
                        if aq is not None:
                            aq.put(("chunk", text))

                    try:
                        if as_bool(get_config(ctx, "chatbot.prompt_for_web_research")):
                            def approval_callback(query_for_engine, tool_name, args):
                                q = getattr(self, "_active_q", None)
                                if q is None:
                                    log.warning(
                                        "tool_loop: web_research approval skipped (_active_q missing)"
                                    )
                                    return True
                                event = threading.Event()
                                event.approved = False
                                event.query_override = None
                                q.put(
                                    (
                                        "approval_required",
                                        query_for_engine,
                                        tool_name,
                                        event,
                                    )
                                )
                                event.wait()
                                if not event.approved:
                                    q.put(("stopped",))
                                return (
                                    bool(event.approved),
                                    getattr(event, "query_override", None),
                                )
                    except Exception as ex:
                        log.warning("tool_loop: web_research approval setup failed: %s", ex)

                tctx = ToolContext(
                    doc=doc,
                    ctx=ctx,
                    doc_type=doc_type_str,
                    services=_get_tools()._services,
                    caller="chat",
                    status_callback=status_callback,
                    append_thinking_callback=append_thinking_callback,
                    stop_checker=stop_checker,
                    approval_callback=approval_callback,
                    chat_append_callback=chat_append_callback
                    if name == "web_research"
                    else None,
                    set_active_domain_callback=set_active_domain,
                )
                try:
                    res = _get_tools().execute(name, tctx, **args)
                    return json.dumps(res) if isinstance(res, dict) else str(res)
                except (ToolExecutionError, UnoObjectError) as e:
                    log.error("Tool execution failed: %s" % e, extra={"context": "tool_execution"})
                    agent_log("tool_loop.py:execute_fn", "Tool execution failed", data={"type": type(e).__name__, "message": str(e)})
                    return json.dumps(format_error_payload(e))
                except Exception as e:
                    wrapped_error = ToolExecutionError(
                        "Unexpected error executing tool '%s'" % name,
                        code="TOOL_UNEXPECTED_ERROR",
                        details={"tool_name": name, "original_error": str(e), "type": type(e).__name__}
                    )
                    log.error("Unexpected tool error: %s" % wrapped_error)
                    return json.dumps(format_error_payload(wrapped_error))

        except Exception as e:
            if isinstance(e, UnoObjectError):
                log.error("_do_send: tool import failed (UnoObjectError): %s" % e)
            else:
                log.error("_do_send: tool import FAILED: %s" % e)
            self._append_response("\n[Import error - tools: %s]\n" % e)
            self._terminal_status = "Error"
            return

        extra_instructions = get_config(self.ctx, "additional_instructions") or ""
        self.session.messages[0]["content"] = get_chat_system_prompt_for_document(
            model, extra_instructions, ctx=self.ctx
        )

        if self.model_selector:
            selected_model = self.model_selector.getText()
            if selected_model:
                from plugin.framework.config import set_config

                set_config(self.ctx, "text_model", selected_model)
                current_endpoint = get_current_endpoint(self.ctx)
                update_lru_history(
                    self.ctx, selected_model, "model_lru", current_endpoint
                )
                log.debug("_do_send: text model updated to %s" % selected_model)
        if self.image_model_selector:
            selected_image_model = self.image_model_selector.getText()
            if selected_image_model:
                set_image_model(self.ctx, selected_image_model)
                log.debug("_do_send: image model updated to %s" % selected_image_model)

        max_context = int(get_config(self.ctx, "chat_context_length"))
        max_tokens = int(get_config(self.ctx, "chat_max_tokens"))
        log.debug(
            "_do_send: config loaded: max_tokens=%d, max_context=%d"
            % (max_tokens, max_context)
        )

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
            doc_text = get_document_context_for_chat(
                model,
                max_context,
                include_end=True,
                include_selection=True,
                ctx=self.ctx,
            )
            log.debug("_do_send: document context length=%d" % len(doc_text))
            agent_log(
                "chat_panel.py:doc_context",
                "Document context for AI",
                data={
                    "doc_length": len(doc_text),
                    "doc_prefix_first_200": (doc_text or "")[:200],
                    "max_context": max_context,
                },
                hypothesis_id="B",
            )
            self.session.update_document_context(doc_text)
        except UnoObjectError as e:
            log.error("Document unavailable: %s" % e, extra={"context": "document_context"})
            self._append_response("\n[Document closed or unavailable.]\n")
            self._terminal_status = "Error"
            self._set_status("Error")
            return
        except Exception as e:
            try:
                from com.sun.star.lang import DisposedException
                from com.sun.star.uno import RuntimeException, Exception as UnoException
                is_disposed = isinstance(e, (DisposedException, RuntimeException, UnoException))
            except ImportError:
                is_disposed = False
            if is_disposed:
                log.debug("Document likely disposed while reading context: %s", e)
                self._append_response("\n[Document closed or unavailable.]\n")
            else:
                log.error("Unexpected document error: %s" % e, extra={"context": "document_context"})
                wrapped_error = UnoObjectError(
                    "Failed to get document context",
                    code="DOCUMENT_CONTEXT_ERROR",
                    details={"original_error": str(e), "type": type(e).__name__}
                )
                self._append_response("\n[Error reading document: %s]\n" % wrapped_error.message)
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
                    "input_audio": {"data": b64_audio, "format": "wav"},
                }

                content_list = []
                if query_text:
                    content_list.append({"type": "text", "text": query_text})
                content_list.append(audio_msg)

                self.session.add_user_message(content_list)

                display_text = (
                    query_text + " [Audio Attached]"
                    if query_text
                    else "[Audio Message]"
                )
                self._append_response("\nYou: %s\n" % display_text)
                # Note: We do NOT delete the audio file yet, in case native call fails and we need STT fallback
            except (IOError, OSError) as e:
                log.error("Audio file error: %s" % e, extra={"context": "audio_handling"})
                # Preserve file for debugging
                log.debug("Audio file preserved at: %s" % self.audio_wav_path)
                self.session.add_user_message(query_text)
                self._append_response("\nYou: %s\n" % query_text)
                self.audio_wav_path = None
            except Exception as e:
                from plugin.framework.errors import NetworkError
                if isinstance(e, NetworkError):
                    log.error("NetworkError while handling audio message: %s", e, extra={"context": "audio_handling"})
                else:
                    log.error("Unexpected audio error: %s" % e, extra={"context": "audio_handling"})
                self.session.add_user_message(query_text)
                self._append_response("\nYou: %s\n" % query_text)
                self.audio_wav_path = None
        else:
            self.session.add_user_message(query_text)
            self._append_response("\nYou: %s\n" % query_text)

        self._append_response("\n[Using chat model.]\n")
        log.info("_do_send: using chat model")

        self._set_status("Connecting to AI (tools=%s)..." % use_tools)
        log.debug(
            "_do_send: calling AI, use_tools=%s, messages=%d"
            % (use_tools, len(self.session.messages))
        )

        max_tool_rounds = api_config.get(
            "chat_max_tool_rounds", DEFAULT_MAX_TOOL_ROUNDS
        )
        self._start_tool_calling_async(
            client,
            model,
            max_tokens,
            active_tools,
            execute_fn,
            max_tool_rounds,
            query_text=query_text,
        )

        log.debug("=== _do_send END (async started, level=logging.INFO) ===")

    def _spawn_llm_worker(self, q, client, max_tokens, tools, round_num, query_text=None):
        """Spawn a background thread that streams the LLM response into q."""
        update_activity_state("tool_loop", round_num=round_num)
        log.debug(
            "Tool loop round %d: sending %d messages to API..."
            % (round_num, len(self.session.messages))
        )
        self._set_status(
            "Thinking..." if round_num == 0 else "Thinking (round %d)..."
            % (round_num + 1)
        )

        def run():
            try:
                response = client.stream_request_with_tools(
                    self.session.messages,
                    max_tokens,
                    tools=tools,
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
                from plugin.framework.errors import NetworkError
                if isinstance(e, NetworkError):
                    log.error("Tool loop round %d: NetworkError: %s" % (round_num, e))
                else:
                    log.error("Tool loop round %d: API ERROR: %s" % (round_num, e))
                q.put(("error", format_error_payload(e)))

        from plugin.framework.worker_pool import run_in_background
        run_in_background(run, name=f"llm-worker-{round_num}")

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
                    self.session.messages,
                    max_tokens,
                    append_c,
                    append_t,
                    stop_checker=lambda: self.stop_requested,
                )
                if self.stop_requested:
                    q.put(("stopped",))
                else:
                    q.put(("final_done", "".join(last_streamed)))
            except Exception as e:
                from plugin.framework.errors import NetworkError
                if isinstance(e, NetworkError):
                    log.error("Final stream NetworkError: %s", e)
                else:
                    log.error("Final stream error: %s", e)
                q.put(("error", format_error_payload(e)))

        from plugin.framework.worker_pool import run_in_background
        run_in_background(run_final, name="llm-worker-final")

    def _create_event_from_stream_item(self, item):
        """Factory method to convert a raw stream item tuple into a ToolLoopEvent."""
        kind = item[0] if isinstance(item, (tuple, list)) else item
        data = item[1] if isinstance(item, (tuple, list)) and len(item) > 1 else None

        if kind == "stream_done":
            return ToolLoopEvent(kind=EventKind.STREAM_DONE, data={
                "response": data,
                "has_audio": bool(self.audio_wav_path)
            })
        elif kind == "next_tool":
            return ToolLoopEvent(kind=EventKind.NEXT_TOOL)
        elif kind == "tool_done":
            mutates = False
            try:
                from plugin.main import get_tools as _get_tools_registry
                tool = _get_tools_registry().get(item[2])
                if tool and tool.detects_mutation():
                    mutates = True
            except Exception as e:
                try:
                    from com.sun.star.lang import DisposedException
                    from com.sun.star.uno import RuntimeException, Exception as UnoException
                    if isinstance(e, (DisposedException, RuntimeException, UnoException)):
                        log.debug("Tool loop event: mutates_document check failed (likely disposed): %s", e)
                except ImportError:
                    pass
            except Exception as e:
                try:
                    from com.sun.star.lang import DisposedException
                    from com.sun.star.uno import RuntimeException, Exception as UnoException
                    if isinstance(e, (DisposedException, RuntimeException, UnoException)):
                        log.debug("_execute_effect 'update_document_context' failed (likely disposed): %s", e)
                    else:
                        log.debug("_execute_effect 'update_document_context' exception: %s", e)
                except ImportError:
                    log.debug("_execute_effect 'update_document_context' exception: %s", e)
            except OSError as e:
                log.debug("Failed to remove audio_wav_path during CleanupAudioEffect: %s", e)
            return ToolLoopEvent(
                kind=EventKind.TOOL_RESULT,
                data={
                    "call_id": item[1],
                    "func_name": item[2],
                    "func_args_str": item[3],
                    "result": item[4],
                    "mutates_document": mutates
                }
            )
        elif kind == "final_done":
            return ToolLoopEvent(kind=EventKind.FINAL_DONE, data={"content": data})
        elif kind == "error":
            return ToolLoopEvent(kind=EventKind.ERROR, data={"error": data})
        return None

    def _execute_effect(self, effect):
        """Execute a single pure effect returned by the state machine."""
        if effect == "exit_loop":
            return True
        elif effect == "trigger_next_tool":
            self._active_q.put(("next_tool",))
        elif effect == "spawn_final_stream":
            self._spawn_final_stream(self._active_q, self._active_client, self._active_max_tokens)
        elif effect == "update_document_context":
            try:
                doc = self._get_document_model() if hasattr(self, "_get_document_model") else None
                if doc:
                    max_ctx = get_config(self.ctx, "chat_context_length") or 8000
                    doc_text = get_document_context_for_chat(
                        doc,
                        max_ctx,
                        include_end=True,
                        include_selection=True,
                        ctx=self.ctx,
                    )
                    self.session.update_document_context(doc_text)
            except Exception:
                pass

        elif isinstance(effect, UIEffect):
            if effect.kind == "append":
                self._append_response(effect.text)
            elif effect.kind == "status":
                self._set_status(effect.text)
                if effect.text in ("Stopped", "Ready", "Error"):
                    self._terminal_status = effect.text
            elif effect.kind == "debug":
                log.debug(effect.text)
            elif effect.kind == "info":
                log.info(effect.text)

        elif isinstance(effect, LogAgentEffect):
            agent_log(effect.location, effect.message, data=effect.data, hypothesis_id=effect.hypothesis_id)

        elif isinstance(effect, AddMessageEffect):
            if effect.role == "assistant":
                self.session.add_assistant_message(content=effect.content, tool_calls=effect.tool_calls)
            elif effect.role == "tool":
                self.session.add_tool_result(effect.call_id, effect.content)

        elif isinstance(effect, SpawnLLMWorkerEffect):
            self._spawn_llm_worker(
                self._active_q,
                self._active_client,
                self._active_max_tokens,
                self._active_tools,
                effect.round_num,
                query_text=self._active_query_text,
            )

        elif isinstance(effect, UpdateActivityStateEffect):
            if effect.action == "tool_execute":
                update_activity_state("tool_execute", round_num=effect.round_num, tool_name=effect.tool_name)
            elif effect.action == "exhausted_rounds":
                update_activity_state("exhausted_rounds")

        elif effect.__class__.__name__ == "CleanupAudioEffect":
            current_model = get_text_model(self.ctx)
            current_endpoint = get_current_endpoint(self.ctx)
            set_native_audio_support(
                self.ctx, current_model, current_endpoint, supported=True
            )
            import os
            try:
                os.remove(self.audio_wav_path)
            except Exception:
                pass
            self.audio_wav_path = None

        elif isinstance(effect, SpawnToolWorkerEffect):
            func_name = effect.func_name
            func_args_str = effect.func_args_str
            func_args = effect.func_args
            call_id = effect.call_id

            image_model_override = (
                self.image_model_selector.getText()
                if self.image_model_selector
                else None
            )
            if image_model_override and func_name == "generate_image":
                func_args["image_model"] = image_model_override

            def tool_status_callback(msg):
                self._active_q.put(("status", msg))

            if effect.is_async:
                def run_async():
                    try:
                        def tool_thinking_callback(msg):
                            self._active_q.put(("tool_thinking", msg))

                        if self._active_supports_status:
                            res = self._active_execute_tool_fn(
                                func_name,
                                func_args,
                                self._active_model,
                                self.ctx,
                                status_callback=tool_status_callback,
                                append_thinking_callback=tool_thinking_callback,
                                stop_checker=lambda: self.stop_requested,
                            )
                        else:
                            res = self._active_execute_tool_fn(
                                func_name,
                                func_args,
                                self._active_model,
                                self.ctx,
                                stop_checker=lambda: self.stop_requested,
                            )
                        self._active_q.put(("tool_done", call_id, func_name, func_args_str, res))
                    except Exception as e:
                        import json
                        self._active_q.put(("tool_done", call_id, func_name, func_args_str, json.dumps(format_error_payload(e))))

                from plugin.framework.worker_pool import run_in_background
                run_in_background(run_async, name=f"tool-async-{func_name}")
            else:
                try:
                    if self._active_supports_status:
                        res = self._active_execute_tool_fn(
                            func_name,
                            func_args,
                            self._active_model,
                            self.ctx,
                            status_callback=tool_status_callback,
                        )
                    else:
                        res = self._active_execute_tool_fn(
                            func_name, func_args, self._active_model, self.ctx
                        )
                    self._active_q.put(("tool_done", call_id, func_name, func_args_str, res))
                except Exception as e:
                    import json
                    self._active_q.put(("tool_done", call_id, func_name, func_args_str, json.dumps(format_error_payload(e))))

        return False

    def _handle_stream_completion(self, item):
        kind = item[0] if isinstance(item, (tuple, list)) else item
        if kind == "next_tool" and self.stop_requested and not self._sm_state.is_stopped:
            # Synchronize stop state into the state machine
            self._sm_state = dataclasses.replace(self._sm_state, is_stopped=True)

        event = self._create_event_from_stream_item(item)
        if not event:
            return False

        # Run the state machine transition
        tr = next_state(self._sm_state, event)
        self._sm_state = tr.state

        # Keep old instance variables synced for external readers or edge cases
        self._active_round_num = self._sm_state.round_num
        self._active_pending_tools = list(self._sm_state.pending_tools)

        # Execute the effects
        exit_loop = False
        for effect in tr.effects:
            if self._execute_effect(effect):
                exit_loop = True

        return exit_loop

    def _handle_stream_stopped(self):
        event = ToolLoopEvent(kind=EventKind.STOP_REQUESTED)
        tr = next_state(self._sm_state, event)
        self._sm_state = tr.state

        for effect in tr.effects:
            self._execute_effect(effect)

    def _is_400_input_validation(self, err):
        """Treat HTTP 400 with 'input validation' or 'bad request' as likely audio-format rejection (e.g. Together AI)."""
        msg = str(err).lower()
        return "400" in msg and ("input validation" in msg or "bad request" in msg)

    def _handle_stream_error(self, e):
        current_model = get_text_model(self.ctx)
        current_endpoint = get_current_endpoint(self.ctx)

        # If native audio failed, cache it and try STT fallback
        if self.audio_wav_path and (
            is_audio_unsupported_error(e) or self._is_400_input_validation(e)
        ):
            log.warning(
                "Model %s failed native audio, caching and falling back to STT"
                % current_model
            )
            set_native_audio_support(
                self.ctx, current_model, current_endpoint, supported=False
            )

            stt_model = get_stt_model(self.ctx)
            if stt_model:
                if (
                    self.session.messages
                    and self.session.messages[-1]["role"] == "user"
                ):
                    self.session.messages.pop()

                self._append_response(
                    "\n[Model does not support audio. Falling back to STT...]\n"
                )
                try:
                    transcript = self._transcribe_audio(self.audio_wav_path, stt_model)
                    if transcript:
                        combined = (self._active_query_text + "\n" + transcript).strip() if self._active_query_text else transcript
                        doc_type = self._get_doc_type_str(self._active_model).lower() if hasattr(self, "_get_doc_type_str") else "writer"
                        self._do_send_chat_with_tools(combined, self._active_model, doc_type)
                except Exception:
                    pass
                return

        # If we reached here, it's either not a modality error or STT is not configured
        err_msg = format_error_message(e)
        self._append_response("\n[API error: %s]\n" % err_msg)
        self._terminal_status = "Error"
        self._set_status("Error")
        # Cleanup audio if we aren't falling back
        if self.audio_wav_path:
            import os

            try:
                os.remove(self.audio_wav_path)
            except OSError as e:
                log.debug("Failed to remove audio_wav_path during error handling: %s", e)
            self.audio_wav_path = None

    def _on_tool_loop_approval_required(self, item):
        """Main-thread handler: show inline Accept/Reject and unblock the tool worker."""
        query_for_engine = item[1] if len(item) > 1 else ""
        tool_name = item[2] if len(item) > 2 else ""
        event_obj = item[3] if len(item) > 3 else None
        if event_obj is not None:
            self.begin_inline_web_approval(query_for_engine, tool_name, event_obj)
        log.info(
            "tool_loop on_approval_required: tool=%s (inline Accept/Change/Reject)",
            tool_name,
        )

    def _start_tool_calling_async(
        self,
        client,
        model,
        max_tokens,
        tools,
        execute_tool_fn,
        max_tool_rounds=None,
        query_text=None,
    ):
        """Tool-calling event loop: single queue, single main-thread loop.

        Background threads push messages onto q. The main thread dispatches
        on message type, keeping the UI responsive via processEventsToIdle().
        """
        if max_tool_rounds is None:
            max_tool_rounds = DEFAULT_MAX_TOOL_ROUNDS
        log.info(
            "=== Tool-calling loop START (max %d rounds) ==="
            % max_tool_rounds
        )
        self._append_response("\nAI: ")

        try:
            from plugin.main import get_tools as _get_tools_registry
            registry = _get_tools_registry()
            async_tools = frozenset([
                tool.name for tool in registry.values()
                if getattr(tool, "is_async", lambda: False)()
            ])
        except Exception as e:
            log.debug("Failed to get async tools list, falling back to defaults: %s", e)
            async_tools = frozenset({"web_research", "generate_image"})

        self._sm_state = ToolLoopState(
            round_num=0,
            pending_tools=[],
            max_rounds=max_tool_rounds,
            status="Thinking...",
            async_tools=async_tools
        )

        try:
            self._active_q = queue.Queue()
            self._active_round_num = 0
            self._active_pending_tools = []
            self._active_async_tools = async_tools

            self._active_client = client
            self._active_model = model
            self._active_max_tokens = max_tokens
            self._active_tools = tools
            self._active_execute_tool_fn = execute_tool_fn
            self._active_max_tool_rounds = max_tool_rounds
            self._active_query_text = query_text

            # Read config once for web research thinking display
            try:
                show_search_thinking = as_bool(
                    get_config(self.ctx, "chatbot.show_search_thinking")
                )
            except (ValueError, TypeError) as e:
                log.debug("Failed to read 'chatbot.show_search_thinking' from config: %s", e)
                show_search_thinking = False

            try:
                toolkit = self.ctx.getServiceManager().createInstanceWithContext(
                    "com.sun.star.awt.Toolkit", self.ctx
                )
            except Exception as e:
                from com.sun.star.lang import DisposedException
                from com.sun.star.uno import RuntimeException, Exception as UnoException
                if isinstance(e, (DisposedException, RuntimeException, UnoException)):
                    log.debug("Failed to create Toolkit instance (likely disposed): %s", e)
                else:
                    log.error("Failed to create Toolkit instance: %s", e)
                self._append_response("\n[Error: %s]\n" % str(e))
                self._terminal_status = "Error"
                self._set_status("Error")
                return

            # Check once whether execute_tool_fn accepts status_callback
            sig = inspect.signature(execute_tool_fn)
            self._active_supports_status = (
                "status_callback" in sig.parameters or "kwargs" in sig.parameters
            )

            # --- Thinking display state (mirrors run_stream_drain_loop behavior) ---
            thinking_open = [False]

            # --- Kick off the first LLM stream ---
            self._spawn_llm_worker(
                self._active_q, self._active_client, self._active_max_tokens, self._active_tools, self._active_round_num, query_text=self._active_query_text
            )

            run_stream_drain_loop(
                self._active_q,
                toolkit,
                [False],
                self._append_response,
                on_stream_done=self._handle_stream_completion,
                on_stopped=self._handle_stream_stopped,
                on_error=self._handle_stream_error,
                on_status_fn=self._set_status,
                ctx=self.ctx,
                stop_checker=lambda: self.stop_requested,
                show_search_thinking=show_search_thinking,
                on_approval_required=self._on_tool_loop_approval_required,
            )
        finally:
            self.sidebar_state = dataclasses.replace(
                self.sidebar_state, tool_loop=None
            )

    def _start_simple_stream_async(self, client, max_tokens):
        """Start simple streaming (no tools) via async helper; returns immediately."""
        log.info("=== Simple stream START ===")
        self._set_status("Thinking...")
        self._append_response("\nAI: ")

        last_user = ""
        doc_context = ""
        for msg in reversed(self.session.messages):
            if msg["role"] == "user" and not last_user:
                last_user = msg["content"]
            if msg["role"] == "system" and "[DOCUMENT CONTENT]" in (
                msg.get("content") or ""
            ):
                doc_context = msg["content"]
        prompt = (
            "%s\n\nUser question: %s" % (doc_context, last_user)
            if doc_context
            else last_user
        )
        system_prompt = ""
        for msg in self.session.messages:
            if msg["role"] == "system" and "[DOCUMENT CONTENT]" not in (
                msg.get("content") or ""
            ):
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
            err_msg = format_error_message(e)
            self._append_response("[Error: %s]\n" % err_msg)
            self._terminal_status = "Error"
            self._set_status("Error")

        run_stream_completion_async(
            self.ctx,
            client,
            prompt,
            system_prompt,
            max_tokens,
            apply_chunk,
            on_done,
            on_error,
            on_status_fn=self._set_status,
            stop_checker=lambda: self.stop_requested,
        )

    def begin_inline_web_approval(self, description, tool_name, event_obj):
        """Override on ``SendButtonListener`` for real UI. Default: auto-approve (tests / no panel)."""
        if event_obj is not None:
            event_obj.approved = True
            event_obj.query_override = None
            event_obj.set()
