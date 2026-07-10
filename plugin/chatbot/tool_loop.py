"""ToolCallingMixin: core chat-with-tools engine for the sidebar.

This mixin is used by SendButtonListener in panel.py and contains the
multi-round tool-calling loop plus simple streaming fallback.
"""

import logging
import inspect
import dataclasses
import queue
import base64
import os
from typing import TYPE_CHECKING, Protocol, Any, Callable, Sequence, cast

try:
    from com.sun.star.lang import DisposedException
    from com.sun.star.uno import RuntimeException, Exception as UnoException
    UNO_DISPOSED_EXCEPTIONS = (DisposedException, RuntimeException, UnoException)
except ImportError:
    UNO_DISPOSED_EXCEPTIONS = cast("Any", (Exception,))

if TYPE_CHECKING:
    from plugin.framework.client.llm_client import LlmClient
    from plugin.chatbot.panel import ChatSession

from plugin.framework.async_stream import run_stream_drain_loop, StreamQueueKind, BatchingStreamQueue
from plugin.framework.logging import agent_log, update_activity_state
from plugin.framework.client.errors import format_error_message, is_audio_unsupported_error
from plugin.framework.config import (
    get_api_config,
    get_config,
    get_config_int,
    get_config_str,
    get_current_endpoint,
    validate_api_config,
)
from plugin.framework.client.model_fetcher import (
    get_stt_model,
    get_text_model,
    set_image_model,
    set_native_audio_support,
)
from plugin.chatbot.config_ui_helpers import sync_sidebar_text_model
from plugin.framework.constants import CHAT_DOCUMENT_CONTEXT_MAX_CHARS
from plugin.framework.prompts import get_chat_system_prompt_for_document
from plugin.doc.document_helpers import get_document_context_for_chat
from plugin.framework.errors import format_error_payload, UnoObjectError, NetworkError
from plugin.framework.queue_executor import llm_request_lane
from plugin.framework.client.llm_client import LlmClient
from plugin.framework.config import as_bool

from plugin.framework.worker_pool import run_in_background
from plugin.framework.uno_context import get_toolkit
from plugin.framework.i18n import _
from plugin.chatbot.tool_loop_actions import ToolLoopEffectInterpreter, build_tool_execute_fn

from plugin.chatbot.tool_loop_state import (
    ToolLoopState,
    ToolLoopEvent,
    EventKind,
    next_state,
)

log = logging.getLogger(__name__)

# DEFAULT_MAX_TOOL_ROUNDS removed; now managed by WriterAgentConfig.chat_max_tool_rounds

# Producer-side batch interval for streamed chat display text (CHUNK and THINKING items).
# The BatchingStreamQueue uses a hard deadline measured from the *first* fragment
# of each burst ("send data every N ms max, or when done" / flush on boundary).
# Change this one constant to experiment with different smoothing cadences.
# 0.25 = 250 ms (current recommended default for "leisurely but still alive" feel).
CHAT_STREAM_BATCH_INTERVAL = 1.0  # seconds


class ToolLoopHost(Protocol):
    ctx: Any
    session: "ChatSession"
    client: "LlmClient | None"
    model_selector: Any
    image_model_selector: Any
    audio_wav_path: str | None

    @property
    def stop_requested(self) -> bool: ...

    def resolve_stop_checker(self) -> Callable[[], bool]: ...

    sidebar_state: Any
    _terminal_status: str

    _active_q: "queue.Queue[Any]"
    _active_client: "LlmClient"
    _active_max_tokens: int
    _active_tools: list[dict[str, Any]]
    _active_execute_tool_fn: Callable[..., Any]
    _active_max_tool_rounds: int
    _active_query_text: str | None
    _active_model: Any
    _active_async_tools: frozenset[str]
    _active_supports_status: bool
    _active_round_num: int
    _active_pending_tools: list[Any]
    _current_tool_call_id: str | None
    _assistant_stream_start_len: int | None
    _record_assistant_start: bool
    _tool_loop_interpreter: ToolLoopEffectInterpreter | None
    _in_brainstorming_mode: bool
    _brainstorming_topic: str

    def _append_response(self, text: str, is_thinking: bool = False, role: str = "assistant") -> None: ...
    def _set_status(self, text: str) -> None: ...
    def _get_document_model(self) -> Any: ...
    def _get_doc_type_str(self, model: Any) -> str: ...
    def begin_inline_web_approval(self, query: str, tool: str, event: Any) -> None: ...
    def _transcribe_audio(self, path: str, model_id: str) -> str: ...
    def _get_mcp_url(self) -> str | None: ...

    @property
    def _sm_state(self) -> "ToolLoopState": ...
    @_sm_state.setter
    def _sm_state(self, value: "ToolLoopState | None") -> None: ...

    # Mixin methods called on self
    def _start_tool_calling_async(self, client: "LlmClient", model: Any, max_tokens: int, tools: list[dict[str, Any]], execute_tool_fn: Callable[..., Any], max_tool_rounds: int | None = None, query_text: str | None = None) -> None: ...
    def _spawn_llm_worker(self, q: "queue.Queue[Any] | BatchingStreamQueue", client: "LlmClient", max_tokens: int, tools: list[dict[str, Any]], round_num: int, query_text: str | None = None) -> None: ...
    def _spawn_final_stream(self, q: "queue.Queue[Any] | BatchingStreamQueue", client: "LlmClient", max_tokens: int) -> None: ...
    def _create_event_from_stream_item(self, item: Any) -> ToolLoopEvent | None: ...
    def _handle_stream_completion(self, item: Any) -> bool: ...
    def _handle_stream_stopped(self) -> None: ...
    def _handle_stream_error(self, e: Any) -> None: ...
    def _on_tool_loop_approval_required(self, item: Any) -> None: ...
    def _execute_effect(self, effect: Any) -> bool: ...
    def _do_send_chat_with_tools(self, query_text: str, model: Any, doc_type_str: str) -> None: ...
    def _refresh_active_tools_for_session(self) -> None: ...
    def _is_400_input_validation(self, err: Any) -> bool: ...
    def rerender_rich_text_session(self) -> None: ...

    # Producer batcher for the current send (set in _start_tool_calling_async when batching is active)
    _active_batched_q: "BatchingStreamQueue | None"


class ToolCallingMixin:
    """Tool loop state lives in ``sidebar_state.tool_loop`` when mixed with SendButtonListener."""

    client: LlmClient | None
    audio_wav_path: str | None

    @property
    def _sm_state(self: ToolLoopHost) -> ToolLoopState:
        if not hasattr(self, "sidebar_state"):
            raise AttributeError("ToolCallingMixin requires sidebar_state (SendButtonListener provides it)")
        tl = self.sidebar_state.tool_loop
        if tl is None:
            raise RuntimeError("Tool loop state used without active session")
        return tl

    @_sm_state.setter
    def _sm_state(self: ToolLoopHost, value: ToolLoopState | None) -> None:
        self.sidebar_state = dataclasses.replace(self.sidebar_state, tool_loop=value)

    def rerender_rich_text_session(self: ToolLoopHost) -> None:
        """Re-render session with HTML formatting. Overridden in SendButtonListener."""

    def _do_send_chat_with_tools(self: ToolLoopHost, query_text: str, model: Any, doc_type_str: str) -> None:
        try:
            log.debug("_do_send: importing core modules...")
            from plugin.main import get_tools

            log.debug("_do_send: core modules imported OK")
        except Exception as e:
            log.exception("_do_send: core modules import FAILED")
            self._append_response("\n[Import error - core: %s]\n" % e)
            self._terminal_status = "Error"
            return

        # Callback for updating active domain in the session
        def set_active_domain(domain, python_tool_domain=None):
            if hasattr(self, "session") and self.session:
                self.session.active_specialized_domain = domain
                self.session.python_tool_domain = python_tool_domain
                log.debug("_do_send: updated active specialized domain to: %s (python_tool_domain: %s)", domain, python_tool_domain)

        try:
            log.debug("_do_send: loading %s schema..." % doc_type_str)
            active_domain = getattr(self.session, "active_specialized_domain", None) if hasattr(self, "session") else None
            python_tool_domain = getattr(self.session, "python_tool_domain", None) if hasattr(self, "session") else None
            from plugin.framework.queue_executor import pump_ui_idle
            from plugin.framework.uno_context import get_toolkit

            toolkit = get_toolkit(self.ctx)
            if toolkit:
                pump_ui_idle(toolkit, max_queue_items=4)
            active_tools = get_tools().get_schemas(
                "openai",
                doc_type=doc_type_str,
                uno_services_supported=getattr(self, "cached_uno_services", None),
                active_domain=active_domain,
                ctx=self.ctx,
            )
            execute_fn = build_tool_execute_fn(self, doc_type_str, active_domain, python_tool_domain, set_active_domain)

        except Exception as e:
            log.exception("_do_send: tool import FAILED")
            self._append_response("\n[Import error - tools: %s]\n" % e)
            self._terminal_status = "Error"
            return

        # base_prompt will be set after reading the document context
        extra_instructions = get_config_str("additional_instructions")

        synced_model = sync_sidebar_text_model(self.ctx, self.model_selector)
        if synced_model:
            log.debug("_do_send: text model updated to %s" % synced_model)
        if self.image_model_selector:
            from plugin.chatbot.config_ui_helpers import _sanitize_model_combobox_value

            selected_image_model = _sanitize_model_combobox_value(str(self.image_model_selector.getText() or ""))
            if selected_image_model:
                set_image_model(selected_image_model)
                log.debug("_do_send: image model updated to %s" % selected_image_model)

        max_context = CHAT_DOCUMENT_CONTEXT_MAX_CHARS
        max_tokens = get_config_int("chat_max_tokens")
        log.debug("_do_send: config loaded: max_tokens=%d, max_context=%d" % (max_tokens, max_context))

        use_tools = True

        api_config = get_api_config()
        ok, err_msg = validate_api_config(api_config)
        if not ok:
            self._append_response("\n[%s]\n" % err_msg)
            self._terminal_status = "Error"
            self._set_status("Error")
            return

        from plugin.framework.url_utils import get_api_version_suffix

        endpoint_stored = str(api_config.get("endpoint") or "").strip()
        if "z.ai" in endpoint_stored.lower():
            combobox_raw = str(self.model_selector.getText() or "") if self.model_selector else ""
            log.debug(
                "_do_send z.ai diag: endpoint=%r api_path=%r combobox_raw=%r synced_model=%r config_model=%r get_text_model=%r",
                endpoint_stored,
                get_api_version_suffix(endpoint_stored),
                combobox_raw,
                synced_model,
                api_config.get("model"),
                get_text_model(),
            )

        # contextvars (SendCancellation) do not propagate to worker threads — LlmClient
        # picks up resolve_stop_checker() via get_current_send_cancellation when created on
        # the UI thread; spawned workers pass stop_checker= explicitly (_spawn_llm_worker).
        if not self.client:
            self.client = LlmClient(api_config, self.ctx)
        else:
            self.client.config = api_config
        assert self.client is not None
        client = self.client

        self._set_status("Reading document...")
        try:
            doc_text = get_document_context_for_chat(model, max_context, include_end=True, include_selection=True, ctx=self.ctx)
            log.debug("_do_send: document context length=%d" % len(doc_text))
            agent_log("chat_panel.py:doc_context", "Document context for AI", data={"doc_length": len(doc_text), "doc_prefix_first_200": (doc_text or "")[:200], "max_context": max_context}, hypothesis_id="B")
            
            base_prompt = get_chat_system_prompt_for_document(model, extra_instructions, ctx=self.ctx)
            self.session.set_system_context(base_prompt, doc_text)
        except UnoObjectError:
            log.exception("Document unavailable")
            self._append_response("\n[Document closed or unavailable.]\n")
            self._terminal_status = "Error"
            self._set_status("Error")
            return
        except Exception as e:
            if isinstance(e, UNO_DISPOSED_EXCEPTIONS):
                log.debug("Document likely disposed while reading context: %s", e)
                self._append_response("\n[Document closed or unavailable.]\n")
            else:
                log.exception("Unexpected document error")
                wrapped_error = UnoObjectError("Failed to get document context", code="DOCUMENT_CONTEXT_ERROR", details={"original_error": str(e), "type": type(e).__name__})
                self._append_response("\n[Error reading document: %s]\n" % wrapped_error.message)
            self._terminal_status = "Error"
            self._set_status("Error")
            return

        # Check for vision capability and selected image base64
        # Note: `model` here is the UNO document object, not the model ID string.
        # The text model ID is in api_config["text_model"].
        b64_image = None
        from plugin.framework.client.model_fetcher import has_native_vision
        text_model_id = api_config.get("text_model", "")
        if has_native_vision(text_model_id, client._endpoint()):
            doc = self._get_document_model() if hasattr(self, "_get_document_model") else None
            if doc:
                try:
                    from plugin.writer.images.image_tools import get_selected_image_base64
                    b64_image = get_selected_image_base64(doc, self.ctx)
                except Exception as e:
                    log.debug("Failed to get selected image base64: %s", e)

        if b64_image or self.audio_wav_path:
            content_list: list[dict[str, Any]] = []
            if query_text:
                content_list.append({"type": "text", "text": query_text})

            attachments = []
            if b64_image:
                content_list.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{b64_image}"}
                })
                attachments.append("Image")

            if self.audio_wav_path:
                try:
                    with open(self.audio_wav_path, "rb") as f:
                        wav_data = f.read()
                    b64_audio = base64.b64encode(wav_data).decode("utf-8")
                    audio_msg = {"type": "input_audio", "input_audio": {"data": b64_audio, "format": "wav"}}
                    content_list.append(audio_msg)
                    attachments.append("Audio")
                except (IOError, OSError):
                    log.exception("Audio file error")
                    log.debug("Audio file preserved at: %s" % self.audio_wav_path)
                    self.audio_wav_path = None
                except Exception as e:
                    from plugin.framework.errors import NetworkError
                    if isinstance(e, NetworkError):
                        log.exception("NetworkError while handling audio message")
                    else:
                        log.exception("Unexpected audio error")
                    self.audio_wav_path = None

            self.session.add_user_message(content_list)

            attach_str = " & ".join(attachments)
            if attach_str:
                display_text = f"{query_text} [{attach_str} Attached]" if query_text else f"[{attach_str} Message]"
            else:
                display_text = query_text
            self._append_response(display_text, role="user")
        else:
            self.session.add_user_message(query_text)
            self._append_response(query_text, role="user")

        self._append_response("\n[Using chat model.]\n")
        log.info("_do_send: using chat model")

        self._set_status("Connecting to AI (tools=%s)..." % use_tools)
        log.debug("_do_send: calling AI, use_tools=%s, messages=%d" % (use_tools, len(self.session.messages)))

        max_tool_rounds = api_config["chat_max_tool_rounds"]
        self._start_tool_calling_async(client, model, max_tokens, active_tools, execute_fn, max_tool_rounds, query_text=query_text)

        log.debug("=== _do_send END (async started, level=logging.INFO) ===")

    def _refresh_active_tools_for_session(self: ToolLoopHost) -> None:
        """Recompute OpenAI tool schemas from ``session.active_specialized_domain``.

        In-place specialized delegation updates the session after ``delegate`` or
        ``specialized_workflow_finished``; each LLM round must see the matching list.
        """
        try:
            from plugin.main import get_tools

            active_domain = getattr(self.session, "active_specialized_domain", None) if hasattr(self, "session") and self.session else None
            self._active_tools = get_tools().get_schemas(
                "openai",
                doc_type=getattr(self, "cached_doc_type", None),
                uno_services_supported=getattr(self, "cached_uno_services", None),
                active_domain=active_domain,
                ctx=getattr(self, "ctx", None),
            )
        except Exception as e:
            log.warning("Failed to refresh active tools: %s", e)

    def _spawn_llm_worker(self: ToolLoopHost, q: "queue.Queue[Any] | BatchingStreamQueue", client: "LlmClient", max_tokens: int, tools: list[dict[str, Any]], round_num: int, query_text: str | None = None) -> None:
        """Spawn a background thread that streams the LLM response into q (or the batcher's raw queue)."""
        batched = q if isinstance(q, BatchingStreamQueue) else None
        real_q = batched.raw if batched is not None else q

        update_activity_state("tool_loop", round_num=round_num)
        log.debug("Tool loop round %d: sending %d messages to API..." % (round_num, len(self.session.messages)))
        self._set_status("Thinking..." if round_num == 0 else "Thinking (round %d)..." % (round_num + 1))

        self._record_assistant_start = True

        def run():
            try:
                with llm_request_lane():
                    response = client.stream_request_with_tools(
                        self.session.messages, max_tokens, tools=tools,
                        append_callback=(batched.content_cb() if batched else lambda t: real_q.put((StreamQueueKind.CHUNK, t))),
                        append_thinking_callback=(batched.thinking_cb() if batched else lambda t: real_q.put((StreamQueueKind.THINKING, t))),
                        stop_checker=self.resolve_stop_checker(),
                    )
                if self.stop_requested:
                    if batched: batched.flush()
                    real_q.put((StreamQueueKind.STOPPED,))
                else:
                    update_activity_state("tool_loop", round_num=round_num)
                    if batched: batched.flush()
                    real_q.put((StreamQueueKind.STREAM_DONE, response))
            except Exception as e:
                if isinstance(e, NetworkError):
                    log.exception("Tool loop round %d: NetworkError" % round_num)
                else:
                    log.exception("Tool loop round %d: API ERROR" % round_num)
                if batched: batched.flush()
                real_q.put((StreamQueueKind.ERROR, format_error_payload(e)))

        run_in_background(run, name=f"llm-worker-{round_num}")

    def _spawn_final_stream(self: ToolLoopHost, q: "queue.Queue[Any] | BatchingStreamQueue", client: "LlmClient", max_tokens: int) -> None:
        """Spawn a background thread for a final no-tools stream into q (or the batcher's raw queue)."""
        batched = q if isinstance(q, BatchingStreamQueue) else None
        real_q = batched.raw if batched is not None else q

        update_activity_state("exhausted_rounds")
        self._set_status("Finishing...")
        self._append_response("\nAI: ")
        self._record_assistant_start = True

        def run_final():
            last_streamed: list[str] = []
            try:
                def append_c(c: str):
                    (batched.content_cb() if batched else lambda t: real_q.put((StreamQueueKind.CHUNK, t)))(c)
                    last_streamed.append(c)

                def append_t(t: str):
                    (batched.thinking_cb() if batched else lambda t: real_q.put((StreamQueueKind.THINKING, t)))(t)

                with llm_request_lane():
                    client.stream_chat_response(self.session.messages, max_tokens, append_c, append_t, stop_checker=self.resolve_stop_checker())
                if self.stop_requested:
                    if batched: batched.flush()
                    real_q.put((StreamQueueKind.STOPPED,))
                else:
                    if batched: batched.flush()
                    real_q.put((StreamQueueKind.FINAL_DONE, "".join(last_streamed)))
            except Exception as e:
                if isinstance(e, NetworkError):
                    log.error("Final stream NetworkError: %s", e)
                else:
                    log.error("Final stream error: %s", e)
                if batched: batched.flush()
                real_q.put((StreamQueueKind.ERROR, format_error_payload(e)))

        run_in_background(run_final, name="llm-worker-final")

    def _create_event_from_stream_item(self: ToolLoopHost, item: Any) -> ToolLoopEvent | None:
        """Factory method to convert a raw stream item tuple into a ToolLoopEvent."""
        raw_kind = item[0] if isinstance(item, (tuple, list)) else item
        if not isinstance(raw_kind, StreamQueueKind):
            return None
        kind = raw_kind
        data = item[1] if isinstance(item, (tuple, list)) and len(item) > 1 else None

        if kind == StreamQueueKind.STREAM_DONE:
            return ToolLoopEvent(kind=EventKind.STREAM_DONE, data={"response": data, "has_audio": bool(self.audio_wav_path)})
        elif kind == StreamQueueKind.NEXT_TOOL:
            return ToolLoopEvent(kind=EventKind.NEXT_TOOL)
        elif kind == StreamQueueKind.TOOL_DONE:
            mutates = False
            raw = item if isinstance(item, (tuple, list)) else ()
            s = cast("Sequence[Any]", raw)
            ln = len(s)
            if ln > 4:
                try:
                    from plugin.main import get_tools as _get_tools_registry

                    tool = _get_tools_registry().get(s[2])
                    if tool and tool.detects_mutation():
                        mutates = True
                except Exception as e:
                    if isinstance(e, UNO_DISPOSED_EXCEPTIONS):
                        log.debug("Tool loop event: mutates_document check failed (likely disposed): %s", e)
            return ToolLoopEvent(kind=EventKind.TOOL_RESULT, data={"call_id": s[1] if ln > 1 else None, "func_name": s[2] if ln > 2 else None, "func_args_str": s[3] if ln > 3 else None, "result": s[4] if ln > 4 else None, "mutates_document": mutates})
        elif kind == StreamQueueKind.FINAL_DONE:
            return ToolLoopEvent(kind=EventKind.FINAL_DONE, data={"content": data})
        elif kind == StreamQueueKind.ERROR:
            return ToolLoopEvent(kind=EventKind.ERROR, data={"error": data})
        return None

    def _execute_effect(self: ToolLoopHost, effect: Any) -> bool:
        """Execute a single pure effect returned by the state machine."""
        interpreter = getattr(self, "_tool_loop_interpreter", None)
        if interpreter is None:
            interpreter = ToolLoopEffectInterpreter(self)
            self._tool_loop_interpreter = interpreter
        return interpreter.execute(effect)

    def _handle_stream_completion(self: ToolLoopHost, item: Any) -> bool:
        raw_kind = item[0] if isinstance(item, (tuple, list)) else item
        kind = raw_kind if isinstance(raw_kind, StreamQueueKind) else None
        if kind == StreamQueueKind.NEXT_TOOL and self.stop_requested and not self._sm_state.is_stopped:
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

    def _handle_stream_stopped(self: ToolLoopHost) -> None:
        event = ToolLoopEvent(kind=EventKind.STOP_REQUESTED)
        tr = next_state(self._sm_state, event)
        self._sm_state = tr.state

        for effect in tr.effects:
            self._execute_effect(effect)

    def _is_400_input_validation(self: ToolLoopHost, err: Any) -> bool:
        """Treat HTTP 400 with 'input validation' or 'bad request' as likely audio-format rejection (e.g. Together AI)."""
        msg = str(err).lower()
        return "400" in msg and ("input validation" in msg or "bad request" in msg)

    def _handle_stream_error(self: ToolLoopHost, e: Any) -> None:
        current_model = get_text_model()
        current_endpoint = get_current_endpoint()

        # If native audio failed, cache it and try STT fallback
        if self.audio_wav_path and (is_audio_unsupported_error(e) or self._is_400_input_validation(e)):
            log.warning("Model %s failed native audio, caching and falling back to STT" % current_model)
            set_native_audio_support(current_model, current_endpoint, supported=False)

            stt_model = get_stt_model()
            if stt_model:
                if self.session.messages and self.session.messages[-1]["role"] == "user":
                    self.session.messages.pop()

                self._append_response("\n[Model does not support audio. Falling back to STT...]\n")
                try:
                    transcript = self._transcribe_audio(self.audio_wav_path, stt_model)
                    if transcript:
                        combined = (self._active_query_text + "\n" + transcript).strip() if self._active_query_text else transcript
                        doc_type = getattr(self, "cached_doc_type", None) or "writer"
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
            try:
                os.remove(self.audio_wav_path)
            except OSError as e:
                log.debug("Failed to remove audio_wav_path during error handling: %s", e)
            self.audio_wav_path = None

    def _on_tool_loop_approval_required(self: ToolLoopHost, item: Any) -> None:
        """Main-thread handler: show inline Accept/Reject and unblock the tool worker."""
        query_for_engine = item[1] if len(item) > 1 else ""
        tool_name = item[2] if len(item) > 2 else ""
        event_obj = item[3] if len(item) > 3 else None
        if event_obj is not None:
            self.begin_inline_web_approval(query_for_engine, tool_name, event_obj)
        log.info("tool_loop on_approval_required: tool=%s (inline Accept/Change/Reject)", tool_name)

    def _start_tool_calling_async(self: ToolLoopHost, client: "LlmClient", model: Any, max_tokens: int, tools: list[dict[str, Any]], execute_tool_fn: Callable[..., Any], max_tool_rounds: int | None = None, query_text: str | None = None) -> None:
        """Tool-calling event loop: single queue, single main-thread loop.

        Background threads push messages onto q. The main thread dispatches
        on message type, keeping the UI responsive via processEventsToIdle().
        """
        if max_tool_rounds is None:
            max_tool_rounds = get_config_int("chatbot.max_tool_rounds")
        log.info("=== Tool-calling loop START (max %d rounds) ===" % max_tool_rounds)
        self._append_response("\nAI: ")
        self._record_assistant_start = True

        try:
            from plugin.main import get_tools as _get_tools_registry

            registry = _get_tools_registry()
            async_tools = frozenset([tool.name for tool in registry.get_tools(filter_doc_type=False, exclude_tiers=()) if getattr(tool, "is_async", lambda: False)()])
        except Exception as e:
            log.debug("Failed to get async tools list, falling back to defaults: %s", e)
            async_tools = frozenset({"web_research", "generate_image"})

        self._sm_state = ToolLoopState(round_num=0, pending_tools=[], max_rounds=max_tool_rounds, status="Thinking...", async_tools=async_tools)

        try:
            raw_q: queue.Queue[Any] = queue.Queue()
            self._active_q = raw_q
            self._active_batched_q: BatchingStreamQueue | None = BatchingStreamQueue(
                raw_q, batch_interval=CHAT_STREAM_BATCH_INTERVAL
            )
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
            self._tool_loop_interpreter = ToolLoopEffectInterpreter(self)

            # Read config once for web research thinking display
            try:
                show_search_thinking = as_bool(get_config("chatbot.show_search_thinking"))
            except (ValueError, TypeError) as e:
                log.debug("Failed to read 'chatbot.show_search_thinking' from config: %s", e)
                show_search_thinking = False

            toolkit = get_toolkit(self.ctx)
            if toolkit is None:

                self._append_response("\n[" + _("Error: Toolkit unavailable") + "]\n")
                self._terminal_status = "Error"
                self._set_status("Error")
                return

            # Check once whether execute_tool_fn accepts status_callback
            sig = inspect.signature(execute_tool_fn)
            self._active_supports_status = "status_callback" in sig.parameters or "kwargs" in sig.parameters

            # --- Thinking display state (mirrors run_stream_drain_loop behavior) ---

            # --- Kick off the first LLM stream (producer batching at 250 ms) ---
            self._refresh_active_tools_for_session()
            self._spawn_llm_worker(self._active_batched_q or self._active_q, self._active_client, self._active_max_tokens, self._active_tools, self._active_round_num, query_text=self._active_query_text)

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
                stop_checker=self.resolve_stop_checker(),
                show_search_thinking=show_search_thinking,
                on_approval_required=self._on_tool_loop_approval_required,
            )

            from plugin.chatbot.rich_text import finalize_sidebar_assistant_response

            finalize_sidebar_assistant_response(self)
        finally:
            self._tool_loop_interpreter = None
            self.sidebar_state = dataclasses.replace(self.sidebar_state, tool_loop=None)

    def begin_inline_web_approval(self, query: str, tool: str, event: Any) -> None:
        """Override on ``SendButtonListener`` for real UI. Default: auto-approve (tests / no panel)."""
        if event is not None:
            event.approved = True
            event.query_override = None
            event.set()
