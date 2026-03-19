"""ToolCallingMixin: core chat-with-tools engine for the sidebar.

This mixin is used by SendButtonListener in panel.py and contains the
multi-round tool-calling loop plus simple streaming fallback.
"""

import logging
import inspect
import json
import queue

from plugin.framework.async_stream import (
    run_stream_completion_async,
    run_stream_drain_loop,
)
from plugin.framework.logging import agent_log, debug_log, update_activity_state
from plugin.modules.http.client import (
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
from plugin.modules.http.client import LlmClient
from plugin.framework.config import as_bool

log = logging.getLogger(__name__)

# Default max tool rounds when not in config (get_api_config supplies chat_max_tool_rounds)
DEFAULT_MAX_TOOL_ROUNDS = 5


class ToolCallingMixin:
    def _do_send_chat_with_tools(self, query_text, model, doc_type_str):
        try:
            log.debug("_do_send: importing core modules...")
            from plugin.main import get_tools

            log.debug("_do_send: core modules imported OK")
        except Exception as e:
            log.error("_do_send: core import FAILED: %s" % e)
            self._append_response("\n[Import error - core: %s]\n" % e)
            self._terminal_status = "Error"
            return

        try:
            log.debug("_do_send: loading %s schema..." % doc_type_str)
            active_tools = get_tools().get_openai_schemas(doc_type=doc_type_str)

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

                tctx = ToolContext(
                    doc=doc,
                    ctx=ctx,
                    doc_type=doc_type_str,
                    services=_get_tools()._services,
                    caller="chat",
                    status_callback=status_callback,
                    append_thinking_callback=append_thinking_callback,
                    stop_checker=stop_checker,
                )
                try:
                    res = _get_tools().execute(name, tctx, **args)
                    return json.dumps(res) if isinstance(res, dict) else str(res)
                except Exception as e:
                    return json.dumps({"status": "error", "message": str(e)})

        except Exception as e:
            log.error("_do_send: tool import FAILED: %s" % e)
            self._append_response("\n[Import error - tools: %s]\n" % e)
            self._terminal_status = "Error"
            return

        extra_instructions = get_config(self.ctx, "additional_instructions") or ""
        self.session.messages[0]["content"] = get_chat_system_prompt_for_document(
            model, extra_instructions
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
        except Exception as e:
            log.error("_do_send: document context FAILED: %s" % e)
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
            except Exception as e:
                log.error("_do_send: Error reading audio: %s" % e)
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
                log.error("Tool loop round %d: API ERROR: %s" % (round_num, e))
                q.put(("error", e))

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
                q.put(("error", e))

        from plugin.framework.worker_pool import run_in_background
        run_in_background(run_final, name="llm-worker-final")

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

        q = queue.Queue()
        round_num = 0
        pending_tools = []
        ASYNC_TOOLS = {"web_research", "generate_image"}

        # Read config once for web research thinking display
        try:
            show_search_thinking = as_bool(
                get_config(self.ctx, "chatbot.show_search_thinking")
            )
        except Exception:
            show_search_thinking = False

        try:
            toolkit = self.ctx.getServiceManager().createInstanceWithContext(
                "com.sun.star.awt.Toolkit", self.ctx
            )
        except Exception as e:
            self._append_response("\n[Error: %s]\n" % str(e))
            self._terminal_status = "Error"
            self._set_status("Error")
            return

        # Check once whether execute_tool_fn accepts status_callback
        sig = inspect.signature(execute_tool_fn)
        supports_status = (
            "status_callback" in sig.parameters or "kwargs" in sig.parameters
        )

        # --- Thinking display state (mirrors run_stream_drain_loop behavior) ---
        thinking_open = [False]

        # --- Kick off the first LLM stream ---
        self._spawn_llm_worker(
            q, client, max_tokens, tools, round_num, query_text=query_text
        )

        def on_stream_done(item):
            nonlocal round_num, pending_tools
            # item can be ('stream_done', response) or ('tool_done', ...) or ('final_done', ...) or ('next_tool',)
            kind = item[0] if isinstance(item, (tuple, list)) else item
            data = (
                item[1]
                if isinstance(item, (tuple, list)) and len(item) > 1
                else None
            )

            if kind == "stream_done":
                response = data
                tool_calls = response.get("tool_calls")
                if isinstance(tool_calls, list) and len(tool_calls) == 0:
                    tool_calls = None
                content = response.get("content")
                finish_reason = response.get("finish_reason")

                agent_log(
                    "chat_panel.py:tool_round",
                    "Tool loop round response",
                    data={
                        "round": round_num,
                        "has_tool_calls": bool(tool_calls),
                        "num_tool_calls": len(tool_calls) if tool_calls else 0,
                    },
                    hypothesis_id="A",
                )

                # If we were using audio and it reached here, cache success
                if self.audio_wav_path:
                    current_model = get_text_model(self.ctx)
                    current_endpoint = get_current_endpoint(self.ctx)
                    set_native_audio_support(
                        self.ctx, current_model, current_endpoint, supported=True
                    )
                    # Successful native call -> we can now delete the audio file
                    import os

                    try:
                        os.remove(self.audio_wav_path)
                    except Exception:
                        pass
                    self.audio_wav_path = None

                # --- No tool calls: conversation is done ---
                if not tool_calls:
                    agent_log(
                        "chat_panel.py:exit_no_tools",
                        "Exiting loop: no tool_calls",
                        data={"round": round_num},
                        hypothesis_id="A",
                    )
                    if content:
                        log.debug("Tool loop: Adding assistant message to session")
                        self.session.add_assistant_message(content=content)
                        self._append_response("\n")
                    elif finish_reason == "length":
                        self._append_response(
                            "\n[Response truncated -- the model ran out of tokens...]\n"
                        )
                    elif finish_reason == "content_filter":
                        self._append_response(
                            "\n[Content filter: response was truncated.]\n"
                        )
                    else:
                        self._append_response(
                            "\n[No text from model; any tool changes were still applied.]\n"
                        )
                    self._terminal_status = "Ready"
                    self._set_status("Ready")
                    return True  # EXIT the drain loop

                # --- Has tool calls: queue them up ---
                self.session.add_assistant_message(
                    content=content, tool_calls=tool_calls
                )
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
                        agent_log(
                            "chat_panel.py:exit_exhausted",
                            "Exiting loop: exhausted max_tool_rounds",
                            data={"rounds": max_tool_rounds},
                            hypothesis_id="A",
                        )
                        self._spawn_final_stream(q, client, max_tokens)
                    else:
                        self._spawn_llm_worker(
                            q,
                            client,
                            max_tokens,
                            tools,
                            round_num,
                            query_text=query_text,
                        )
                    return False

                tc = pending_tools.pop(0)
                func_name = tc.get("function", {}).get("name", "unknown")
                func_args_str = tc.get("function", {}).get("arguments", "{}")
                call_id = tc.get("id", "")

                self._set_status("Running: %s" % func_name)
                self._append_response("[Running tool: %s...]\n" % func_name)
                update_activity_state(
                    "tool_execute", round_num=round_num, tool_name=func_name
                )

                try:
                    func_args = json.loads(func_args_str)
                    if not isinstance(func_args, dict):
                        func_args = {}
                except (json.JSONDecodeError, TypeError):
                    func_args = {}

                agent_log(
                    "chat_panel.py:tool_execute",
                    "Executing tool",
                    data={"tool": func_name, "round": round_num},
                    hypothesis_id="C,D,E",
                )
                log.debug("Tool call: %s(%s)" % (func_name, func_args_str))

                image_model_override = (
                    self.image_model_selector.getText()
                    if self.image_model_selector
                    else None
                )
                if image_model_override and func_name == "generate_image":
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
                                res = execute_tool_fn(
                                    func_name,
                                    func_args,
                                    model,
                                    self.ctx,
                                    status_callback=tool_status_callback,
                                    append_thinking_callback=tool_thinking_callback,
                                    stop_checker=lambda: self.stop_requested,
                                )
                            else:
                                res = execute_tool_fn(
                                    func_name,
                                    func_args,
                                    model,
                                    self.ctx,
                                    stop_checker=lambda: self.stop_requested,
                                )
                            q.put(
                                (
                                    "tool_done",
                                    call_id,
                                    func_name,
                                    func_args_str,
                                    res,
                                )
                            )
                        except Exception as e:
                            q.put(
                                (
                                    "tool_done",
                                    call_id,
                                    func_name,
                                    func_args_str,
                                    json.dumps(
                                        {"status": "error", "message": str(e)}
                                    ),
                                )
                            )

                    from plugin.framework.worker_pool import run_in_background
                    run_in_background(run_async, name=f"tool-async-{func_name}")
                else:
                    # --- SYNC EXECUTION (UNO tools) ---
                    try:
                        if supports_status:
                            res = execute_tool_fn(
                                func_name,
                                func_args,
                                model,
                                self.ctx,
                                status_callback=tool_status_callback,
                            )
                        else: 
                            res = execute_tool_fn(
                                func_name, func_args, model, self.ctx
                            )
                        q.put(
                            (
                                "tool_done",
                                call_id,
                                func_name,
                                func_args_str,
                                res,
                            )
                        )
                    except Exception as e:
                        q.put(
                            (
                                "tool_done",
                                call_id,
                                func_name,
                                func_args_str,
                                json.dumps(
                                    {"status": "error", "message": str(e)}
                                ),
                            )
                        )
                return False

            elif kind == "tool_done":
                call_id, func_name, func_args_str, result = (
                    item[1],
                    item[2],
                    item[3],
                    item[4],
                )

                log.debug("Tool result: %s" % result)
                try:
                    result_data = json.loads(result)
                    note = result_data.get("message", result_data.get("status", "done"))
                except Exception:
                    result_data = {}
                    note = "done"
                self._append_response("[%s: %s]\n" % (func_name, note))
                if func_name == "apply_document_content" and (
                    (note or "").strip().startswith("Replaced 0 occurrence")
                ):
                    params_display = (
                        func_args_str
                        if len(func_args_str) <= 800
                        else func_args_str[:800] + "..."
                    )
                    self._append_response("[Debug: params %s]\n" % params_display)
                self.session.add_tool_result(call_id, result)

                # After a successful document-mutating tool, refresh document context
                # so the next round sees the updated doc and does not repeat the edit.
                try:
                    is_success = (
                        result_data.get("success") is True
                        or result_data.get("status") == "ok"
                    )
                    doc = self._get_document_model() if hasattr(self, "_get_document_model") else None
                    if is_success and doc:
                        from plugin.main import get_tools as _get_tools_registry
                        tool = _get_tools_registry().get(func_name)
                        if tool and tool.detects_mutation():
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
            # Ensure conversation roles continue to alternate user/assistant even
            # when the user stops a response mid-stream.
            self.session.add_assistant_message(content="No response.")
            self._terminal_status = "Stopped"
            self._set_status("Stopped")
            self._append_response("\n[Stopped by user]\n")

        def _is_400_input_validation(err):
            """Treat HTTP 400 with 'input validation' or 'bad request' as likely audio-format rejection (e.g. Together AI)."""
            msg = str(err).lower()
            return "400" in msg and ("input validation" in msg or "bad request" in msg)

        def on_error(e):
            current_model = get_text_model(self.ctx)
            current_endpoint = get_current_endpoint(self.ctx)

            # If native audio failed, cache it and try STT fallback
            if self.audio_wav_path and (
                is_audio_unsupported_error(e) or _is_400_input_validation(e)
            ):
                debug_log(
                    "Model %s failed native audio, caching and falling back to STT"
                    % current_model,
                    context="Chat",
                    level=logging.WARNING
                )
                set_native_audio_support(
                    self.ctx, current_model, current_endpoint, supported=False
                )

                stt_model = get_stt_model(self.ctx)
                if stt_model:
                    # Remove the failed message from session so we can retry with text
                    if (
                        self.session.messages
                        and self.session.messages[-1]["role"] == "user"
                    ):
                        # If it was a list (audio+text), just pop it
                        self.session.messages.pop()

                    self._append_response(
                        "\n[Model does not support audio. Falling back to STT...]\n"
                    )
                    self._transcribe_audio_async(
                        self.audio_wav_path, stt_model, model, query_text=query_text
                    )
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
                except Exception:
                    pass
                self.audio_wav_path = None

        run_stream_drain_loop(
            q,
            toolkit,
            [False],
            self._append_response,
            on_stream_done=on_stream_done,
            on_stopped=on_stopped,
            on_error=on_error,
            on_status_fn=self._set_status,
            ctx=self.ctx,
            stop_checker=lambda: self.stop_requested,
            show_search_thinking=show_search_thinking,
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
