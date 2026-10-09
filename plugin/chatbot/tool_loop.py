"""ToolCallingMixin: core chat-with-tools engine for the sidebar.

This mixin is used by SendButtonListener in panel.py and contains the
multi-round tool-calling loop plus simple streaming fallback.

Concurrency: the model stream runs on a **dedicated** background thread
so typing in Writer stays live. Tokens are queued; the LibreOffice UI
thread drains them and updates the sidebar. Only one tool runs per LLM
round (the loop waits for ``TOOL_RESULT`` before the next). This mixin
holds its own ``LlmClient``; the grammar checker has a separate client.
Document edits and widget updates stay on the drain / main thread, not
on the LLM worker.
"""

from __future__ import annotations

import logging
import inspect
import dataclasses
import queue
from typing import TYPE_CHECKING, Protocol, Any, Callable, Sequence, cast

if TYPE_CHECKING:
    from plugin.framework.client.llm_client import LlmClient
    from plugin.chatbot.panel import ChatSession

from plugin.framework.async_stream import BatchingStreamQueue, StreamQueueKind, defer_until_drain_done, run_stream_drain_loop
from plugin.framework.logging import agent_log, update_activity_state
from plugin.framework.errors import format_error_message, is_disposed_exception, suppress_disposed, UnoObjectError
from plugin.framework.client.errors import (
    is_local_model_server_crash,
    local_model_overflow_message,
)
from plugin.framework.config import (
    get_api_config,
    get_config,
    get_config_bool_safe,
    get_config_int,
    validate_api_config,
)
from plugin.framework.client.model_fetcher import (
    get_text_model,
    set_image_model,
)
from plugin.chatbot.config_ui_helpers import sync_sidebar_text_model
from plugin.framework.constants import CHAT_DOCUMENT_CONTEXT_MAX_CHARS
from plugin.framework.errors import format_error_payload, NetworkError
from plugin.framework.queue_executor import capture_send_stop, llm_request_lane
from plugin.framework.client.llm_client import LlmClient
from plugin.framework.config_schema import as_bool

from plugin.framework.worker_pool import run_in_background
from plugin.framework.uno_context import get_toolkit
from plugin.framework.i18n import _
from plugin.chatbot.tool_loop_actions import (
    ToolLoopEffectInterpreter,
    TurnController,
    abort_turn,
    build_tool_execute_fn,
    current_turn,
    persist_assistant_on_turn,
    put_for_turn,
    running_turn,
    session_for_turn,
    spawn_queue,
)

from plugin.chatbot.tool_loop_state import (
    ToolLoopState,
    ToolLoopEvent,
    EventKind,
    next_state,
)
from plugin.chatbot.compaction import (
    MAX_OVERFLOW_COMPACTION_ATTEMPTS,
    compact_session,
    is_context_overflow_error,
    is_process_death_error,
    messages_for_llm,
    resolve_context_window,
    should_retry_overflow,
)

log = logging.getLogger(__name__)

# Producer-side batch interval for streamed chat display text (CHUNK and THINKING items).
# The BatchingStreamQueue uses a hard deadline measured from the *first* fragment
# of each burst ("send data every N ms max, or when done" / flush on boundary).
# Change this one constant to experiment with different smoothing cadences.
# 0.25 = 250 ms (current recommended default for "leisurely but still alive" feel).
CHAT_STREAM_BATCH_INTERVAL = 0.25  # seconds


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

    # Session I/O handles for the effect interpreter (not FSM control state).
    # The queue, stripper, and document model live on ``_turn``.
    _active_client: "LlmClient"
    _active_max_tokens: int
    _active_tools: list[dict[str, Any]]
    _active_execute_tool_fn: Callable[..., Any]
    _active_query_text: str | None
    _active_supports_status: bool
    _current_tool_call_id: str | None
    _assistant_stream_start_len: int | None
    _record_assistant_start: bool
    _tool_loop_interpreter: ToolLoopEffectInterpreter | None
    _in_brainstorming_mode: bool
    _brainstorming_topic: str
    _turn: Any

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
    def _sm_state(self, value: "ToolLoopState | None") -> None:  # pyright: ignore[reportPropertyTypeMismatch]  # clear session with None
        ...

    # Mixin methods called on self
    def _start_tool_calling_async(self, client: "LlmClient", model: Any, max_tokens: int, tools: list[dict[str, Any]], execute_tool_fn: Callable[..., Any], max_tool_rounds: int | None = None, query_text: str | None = None) -> None: ...
    def _spawn_llm_worker(self, q: "queue.Queue[Any] | BatchingStreamQueue", client: "LlmClient", max_tokens: int, tools: list[dict[str, Any]], round_num: int, query_text: str | None = None, force_compact: bool = False) -> None: ...
    def _spawn_final_stream(self, q: "queue.Queue[Any] | BatchingStreamQueue", client: "LlmClient", max_tokens: int) -> None: ...
    def _create_event_from_stream_item(self, item: Any) -> ToolLoopEvent | None: ...
    def _handle_stream_completion(self, item: Any) -> bool: ...
    def _handle_stream_stopped(self) -> None: ...
    def _handle_stream_error(self, e: Any) -> bool | None: ...
    def _on_tool_loop_approval_required(self, item: Any) -> None: ...
    def _execute_effect(self, effect: Any) -> bool: ...
    def _do_send_chat_with_tools(self, query_text: str, model: Any, doc_type_str: str) -> None: ...
    def _refresh_active_tools_for_session(self) -> None: ...
    def rerender_rich_text_session(self) -> bool: ...


def _live_text(turn: Any, callback: Callable[[str], None]) -> Callable[[str], None]:
    """Ignore deltas after this turn has been aborted."""

    def wrapped(text: str) -> None:
        if isinstance(turn, TurnController) and not turn.alive:
            return
        callback(text)

    return wrapped


def note_stop_partial(turn: Any, response: Any) -> None:
    """Remember assistant text from a stopped round that had no tool calls.

    The drain's stop callback takes no queue payload, so the text rides
    on the turn. Partial ``tool_calls`` must not be executed, so a response
    that includes them is not stored as the assistant message. Dropping the
    worker text on ``STOPPED`` stores ``No response.`` after the sidebar
    has already shown the tokens.
    """
    if not isinstance(turn, TurnController):
        return
    if isinstance(response, dict):
        calls = response.get("tool_calls")
        if isinstance(calls, list) and calls:
            return
        raw = response.get("content")
        text = raw if isinstance(raw, str) else ""
    elif isinstance(response, str):
        text = response
    else:
        return
    turn._stop_partial_text = text.strip()


def take_stop_partial(turn: Any) -> str | None:
    if not isinstance(turn, TurnController):
        return None
    fields = getattr(turn, "__dict__", None)
    if not isinstance(fields, dict):
        return None
    text = fields.pop("_stop_partial_text", None)
    return text if isinstance(text, str) else None


class ToolCallingMixin:
    """Tool-loop control state lives only in ``sidebar_state.tool_loop`` (via ``_sm_state``).

    Remaining ``_active_*`` fields on the host are the client, tool schemas, and
    query text for :class:`ToolLoopEffectInterpreter`. The queue, stripper, and
    document model live on the turn — not a second copy of round/pending/stop.
    """

    # Defaults satisfy basedpyright reportUninitializedInstanceVariable; panel __init__ overwrites.
    client: LlmClient | None = None
    audio_wav_path: str | None = None
    sidebar_state: Any = None
    _terminal_status: str = "Ready"
    _active_tools: list[dict[str, Any]] | None = None
    _record_assistant_start: bool = False
    _tool_loop_interpreter: ToolLoopEffectInterpreter | None = None
    _turn: Any = None
    _active_client: LlmClient | None = None
    _active_max_tokens: int = 0
    _active_execute_tool_fn: Callable[..., Any] | None = None
    _active_query_text: str | None = None
    _active_supports_status: bool = False

    @property
    def _sm_state(self: ToolLoopHost) -> ToolLoopState:
        if not hasattr(self, "sidebar_state"):
            raise AttributeError("ToolCallingMixin requires sidebar_state (SendButtonListener provides it)")
        tl = self.sidebar_state.tool_loop
        if tl is None:
            raise RuntimeError("Tool loop state used without active session")
        return tl

    @_sm_state.setter
    def _sm_state(self: ToolLoopHost, value: ToolLoopState | None) -> None:  # pyright: ignore[reportPropertyTypeMismatch]  # clear session with None
        self.sidebar_state = dataclasses.replace(self.sidebar_state, tool_loop=value)

    def rerender_rich_text_session(self: ToolLoopHost) -> bool:
        """Re-render session with HTML formatting. Overridden in SendButtonListener."""
        return False

    def _do_send_chat_with_tools(self: ToolLoopHost, query_text: str, model: Any, doc_type_str: str, skip_append_user: bool = False) -> None:
        # The send already called begin_send_turn. A mode click inside
        # pump_ui_idle aborts that turn and swaps the session. Do not start
        # another turn here: that rebind wrote this reply onto the other chat.
        if running_turn(self) is None:
            return
        live = current_turn(self)
        if isinstance(live, TurnController) and live.model is None:
            live.model = model
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
        bound_turn = current_turn(self)

        def set_active_domain(domain: Any, python_tool_domain: Any = None) -> None:
            # The tool callback runs later. A new send must not retarget the
            # domain onto the session that replaced this one.
            if current_turn(self) is not bound_turn or not isinstance(bound_turn, TurnController):
                return
            if not bound_turn.accepts_history(self) or bound_turn.session is None:
                return
            session = bound_turn.session
            session.active_specialized_domain = domain
            session.python_tool_domain = python_tool_domain
            log.debug("_do_send: updated active specialized domain to: %s (python_tool_domain: %s)", domain, python_tool_domain)

        try:
            log.debug("_do_send: loading %s schema..." % doc_type_str)
            turn_session = session_for_turn(self)
            active_domain = getattr(turn_session, "active_specialized_domain", None) if turn_session is not None else None
            python_tool_domain = getattr(turn_session, "python_tool_domain", None) if turn_session is not None else None
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
                doc=model,
            )
            from plugin.doc.peer_message import log_peer_tool_on_wire

            log_peer_tool_on_wire(active_tools)
            execute_fn = build_tool_execute_fn(self, doc_type_str, active_domain, python_tool_domain, set_active_domain)

        except Exception as e:
            log.exception("_do_send: tool import FAILED")
            self._append_response("\n[Import error - tools: %s]\n" % e)
            self._terminal_status = "Error"
            return

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
        from plugin.framework.queue_executor import get_current_send_cancellation

        if not self.client:
            self.client = LlmClient(api_config, self.ctx)
        else:
            self.client.config = api_config
            # New send: clear Stop latch from a prior turn (UI thread only).
            self.client.clear_stop()
            # Reused clients registered on the previous send's scope; Stop on
            # this send must close HTTP via the current SendCancellation.
            reuse_scope = get_current_send_cancellation()
            if reuse_scope is not None:
                reuse_scope.register_client(self.client)
        assert self.client is not None
        # Fresh client also: if Stop already fired before register, abort now.
        scope = get_current_send_cancellation()
        if scope is not None and not self.client._stopped:
            # New clients register in __init__; ensure late Stop before spawn still latches.
            if scope.is_cancelled():
                self.client.stop()
        client = self.client

        self._set_status("Reading document...")
        started = running_turn(self)
        turn_session = started.session if started is not None else None
        if turn_session is None:
            # Clear replaced messages, or Stop aborted the turn, while this
            # send was still starting. The user row must not land on the wiped chat.
            return
        try:
            turn_session.refresh_document_context(model, self.ctx)
            doc_text = turn_session.document_context
            log.debug("_do_send: document context length=%d" % len(doc_text))
            agent_log("tool_loop.py:doc_context", "Document context for AI", data={"doc_length": len(doc_text), "doc_prefix_first_200": (doc_text or "")[:200], "max_context": max_context}, hypothesis_id="B")
        except UnoObjectError:
            log.exception("Document unavailable")
            self._append_response("\n[Document closed or unavailable.]\n")
            self._terminal_status = "Error"
            self._set_status("Error")
            return
        except Exception as e:
            if is_disposed_exception(e):
                log.debug("Document likely disposed while reading context: %s", e)
                self._append_response("\n[Document closed or unavailable.]\n")
            else:
                log.exception("Unexpected document error")
                wrapped_error = UnoObjectError("Failed to get document context", code="DOCUMENT_CONTEXT_ERROR", details={"original_error": str(e), "type": type(e).__name__})
                self._append_response("\n[Error reading document: %s]\n" % wrapped_error.message)
            self._terminal_status = "Error"
            self._set_status("Error")
            return

        # Peer extracted send already appended the envelope + body once.
        # Calling add_user_message again would double-post that turn.
        # The turn was bound before pump_ui_idle. Refresh edits that list
        # in place, so the pin still names it.
        if skip_append_user:
            b64_image = None
        else:
            # Check for vision capability and selected image base64.
            # `model` in this function is the UNO document, not the model id.
            # get_api_config stores the chat model as "model". Reading
            # "text_model" was always empty, so has_native_vision returned
            # False and a selected image was never attached.
            # allow_fetch=False: _do_send runs on the UI thread. A cold
            # OpenRouter/Together catalog GET would freeze LibreOffice.
            # The static catalog and vision_support_map still apply.
            b64_image = None
            from plugin.framework.client.model_fetcher import has_native_vision
            text_model_id = str(api_config.get("model") or "")
            if has_native_vision(text_model_id, client._endpoint(), allow_fetch=False):
                doc = self._get_document_model() if hasattr(self, "_get_document_model") else None
                if doc:
                    try:
                        from plugin.writer.images.image_tools import get_selected_image_base64
                        b64_image = get_selected_image_base64(doc, self.ctx)
                    except Exception as e:
                        log.debug("Failed to get selected image base64: %s", e)

        if skip_append_user:
            pass
        elif b64_image or self.audio_wav_path:
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

            audio_content: list[dict[str, Any]] | None = None
            if self.audio_wav_path:
                from plugin.scripting.audio_recorder_service import append_wav_as_input_audio

                audio_content = []
                if append_wav_as_input_audio(audio_content, self.audio_wav_path):
                    attachments.append("Audio")
                    # Capture the model/endpoint the audio is sent to, so a model
                    # switch mid-send does not mark the wrong model as audio-capable.
                    audio_turn = current_turn(self)
                    if isinstance(audio_turn, TurnController):
                        audio_turn.text_model = str(api_config.get("model") or "") or None
                        audio_turn.endpoint = client._endpoint()
                else:
                    self.audio_wav_path = None

            # Add only text (and images) to DB to avoid 404s when reading history
            if not content_list and attachments == ["Audio"]:
                from plugin.framework.i18n import _
                turn_session.add_user_message(_("[Voice message]"))
            else:
                turn_session.add_user_message(content_list)

            if audio_content and turn_session.messages:
                # Append audio to the in-memory message so it's sent this turn
                last_msg = turn_session.messages[-1]
                if isinstance(last_msg.get("content"), list):
                    last_msg["content"].extend(audio_content)
                else:
                    existing_text = last_msg.get("content", "")
                    new_content = []
                    if existing_text:
                        new_content.append({"type": "text", "text": existing_text})
                    new_content.extend(audio_content)
                    last_msg["content"] = new_content

            attach_str = " & ".join(attachments)
            if attach_str:
                display_text = f"{query_text} [{attach_str} Attached]" if query_text else f"[{attach_str} Message]"
            else:
                display_text = query_text
            self._append_response(display_text, role="user")
        else:
            turn_session.add_user_message(query_text)
            self._append_response(query_text, role="user")

        self._append_response("\n[Using chat model.]\n")
        log.info("_do_send: using chat model")

        self._set_status("Connecting to AI (tools=%s)..." % use_tools)
        log.debug("_do_send: calling AI, use_tools=%s, messages=%d" % (use_tools, len(turn_session.messages)))

        max_tool_rounds = cast("int", api_config["chat_max_tool_rounds"])
        self._start_tool_calling_async(client, model, max_tokens, active_tools, execute_fn, max_tool_rounds, query_text=query_text)

        log.debug("=== _do_send END (async started, level=logging.INFO) ===")

    def _refresh_active_tools_for_session(self: ToolLoopHost) -> None:
        """Recompute OpenAI tool schemas from ``session.active_specialized_domain``.

        In-place specialized delegation updates the session after ``delegate`` or
        ``specialized_workflow_finished``; each LLM round must see the matching list.
        """
        try:
            from plugin.main import get_tools

            turn_session = session_for_turn(self)
            active_domain = getattr(turn_session, "active_specialized_domain", None) if turn_session is not None else None
            bound = current_turn(self)
            refresh_doc = bound.model if isinstance(bound, TurnController) and bound.model is not None else None
            if refresh_doc is None and hasattr(self, "_get_document_model"):
                refresh_doc = self._get_document_model()
            self._active_tools = get_tools().get_schemas(
                "openai",
                doc_type=getattr(self, "cached_doc_type", None),
                uno_services_supported=getattr(self, "cached_uno_services", None),
                active_domain=active_domain,
                ctx=getattr(self, "ctx", None),
                doc=refresh_doc,
            )
            from plugin.doc.peer_message import log_peer_tool_on_wire

            log_peer_tool_on_wire(self._active_tools)
        except Exception as e:
            log.warning("Failed to refresh active tools: %s", e)

    def _spawn_llm_worker(self: ToolLoopHost, q: "queue.Queue[Any] | BatchingStreamQueue", client: "LlmClient", max_tokens: int, tools: list[dict[str, Any]], round_num: int, query_text: str | None = None, force_compact: bool = False) -> None:
        """Spawn a background thread that streams the LLM response into q (or the batcher's raw queue)."""
        batched = q if isinstance(q, BatchingStreamQueue) else None

        update_activity_state("tool_loop", round_num=round_num)
        log.debug("Tool loop round %d: sending %d messages to API..." % (round_num, len(self.session.messages)))
        # Overflow retry already set "Compacting conversation..." on the drain
        # thread. Host Thinking here would flash over that ack before the
        # worker's status_callback can post Compacting again.
        if not force_compact:
            self._set_status("Thinking..." if round_num == 0 else "Thinking (round %d)..." % (round_num + 1))

        self._record_assistant_start = True

        turn = current_turn(self)
        # Captured at spawn. session_for_turn at run time would be the next send.
        bound_session = turn.session if isinstance(turn, TurnController) else None
        # Spawn-time scope. The body must not call resolve_stop_checker or
        # read _send_cancellation: Stop clears the field and the next send replaces it.
        bound_scope, stop_checker = capture_send_stop(self)

        def emit(item: Any) -> None:
            put_for_turn(turn, item)

        def stopped_for_this_send() -> bool:
            if bound_scope is not None and bound_scope.is_cancelled():
                return True
            if stop_checker():
                return True
            return bool(getattr(client, "_stopped", False))

        def run() -> None:
            session = bound_session
            if not isinstance(turn, TurnController) or not turn.alive or session is None:
                emit((StreamQueueKind.STOPPED,))
                return
            try:
                # B13: Stop before first SSE — do not acquire llm_request_lane.
                # The checker and scope were captured at spawn. Resolving here
                # would bind the next send after Stop cleared the panel field.
                if stopped_for_this_send():
                    if batched:
                        batched.flush()
                    emit((StreamQueueKind.STOPPED,))
                    return
                # Status via queue only — never self._set_status from this worker (UNO).
                def status_cb(t: str) -> None:
                    emit((StreamQueueKind.STATUS, t))
                resume_status = "Thinking..." if round_num == 0 else "Thinking (round %d)..." % (round_num + 1)
                with llm_request_lane(status_callback=status_cb, resume_status=resume_status):
                    # Compact + stream share one lane hold. compaction.py must
                    # not take the non-reentrant lock itself.
                    if get_config_bool_safe("chat_compaction_enabled"):
                        result = compact_session(
                            session,
                            client,
                            window=resolve_context_window(client),
                            tools=tools,
                            max_tokens=max_tokens,
                            stop_checker=stop_checker,
                            force=force_compact,
                            status_callback=status_cb,
                            enabled=True,
                        )
                        if isinstance(turn, TurnController):
                            turn._last_compact_reason = result.reason
                            turn._last_compact_tokens_before = result.tokens_before
                            turn._last_compact_tokens_after = result.tokens_after
                        if result.reason == "aborted":
                            if batched:
                                batched.flush()
                            emit((StreamQueueKind.STOPPED,))
                            return
                    payload = messages_for_llm(session)
                    response = client.stream_request_with_tools(
                        payload, max_tokens, tools=tools,
                        append_callback=_live_text(turn, batched.content_cb() if batched else lambda t: emit((StreamQueueKind.CHUNK, t))),
                        append_thinking_callback=_live_text(turn, batched.thinking_cb() if batched else lambda t: emit((StreamQueueKind.THINKING, t))),
                        stop_checker=stop_checker,
                        status_callback=status_cb,
                    )
                # Stop during pre-send host-gap wait returns finish_reason "stop"
                # without raising. The captured scope stays cancelled after the
                # next send clears the panel field and calls clear_stop() on
                # this shared client, so a live stop_requested read is wrong.
                if stopped_for_this_send():
                    if batched: batched.flush()
                    note_stop_partial(turn, response)
                    emit((StreamQueueKind.STOPPED,))
                else:
                    update_activity_state("tool_loop", round_num=round_num)
                    if batched: batched.flush()
                    emit((StreamQueueKind.STREAM_DONE, response))
            except Exception as e:
                from plugin.framework.async_stream import BlockingWaitStopped
                if isinstance(e, BlockingWaitStopped):
                    if batched: batched.flush()
                    emit((StreamQueueKind.STOPPED,))
                else:
                    if isinstance(e, NetworkError):
                        log.exception("Tool loop round %d: NetworkError" % round_num)
                    else:
                        log.exception("Tool loop round %d: API ERROR" % round_num)
                    if batched: batched.flush()
                    emit((StreamQueueKind.ERROR, format_error_payload(e)))

        run_in_background(run, name=f"llm-worker-{round_num}", dedicated=True)

    def _spawn_final_stream(self: ToolLoopHost, q: "queue.Queue[Any] | BatchingStreamQueue", client: "LlmClient", max_tokens: int) -> None:
        """Spawn a background thread for a final no-tools stream into q (or the batcher's raw queue)."""
        batched = q if isinstance(q, BatchingStreamQueue) else None

        update_activity_state("exhausted_rounds")
        self._set_status("Finishing...")
        self._append_response("\nAI: ")
        self._record_assistant_start = True

        turn = current_turn(self)
        bound_session = turn.session if isinstance(turn, TurnController) else None
        # Same spawn-time capture as _spawn_llm_worker. The final-stream body
        # must not resolve the panel field when it later starts.
        bound_scope, stop_checker = capture_send_stop(self)

        def emit(item: Any) -> None:
            put_for_turn(turn, item)

        def stopped_for_this_send() -> bool:
            if bound_scope is not None and bound_scope.is_cancelled():
                return True
            if stop_checker():
                return True
            return bool(getattr(client, "_stopped", False))

        def run_final() -> None:
            session = bound_session
            if not isinstance(turn, TurnController) or not turn.alive or session is None:
                emit((StreamQueueKind.STOPPED,))
                return
            last_streamed: list[str] = []
            try:
                content_cb = _live_text(turn, batched.content_cb() if batched else lambda t: emit((StreamQueueKind.CHUNK, t)))
                thinking_cb = _live_text(turn, batched.thinking_cb() if batched else lambda t: emit((StreamQueueKind.THINKING, t)))

                def append_c(c: str) -> None:
                    content_cb(c)
                    last_streamed.append(c)

                def append_t(t: str) -> None:
                    thinking_cb(t)

                if stopped_for_this_send():
                    if batched:
                        batched.flush()
                    emit((StreamQueueKind.STOPPED,))
                    return
                def status_cb(t: str) -> None:
                    emit((StreamQueueKind.STATUS, t))
                with llm_request_lane(status_callback=status_cb, resume_status="Finishing..."):
                    # Same compact-then-view path as _spawn_llm_worker. Final
                    # stream has no tools; still compact when the transcript
                    # is over the tiered threshold.
                    if get_config_bool_safe("chat_compaction_enabled"):
                        result = compact_session(
                            session,
                            client,
                            window=resolve_context_window(client),
                            tools=None,
                            max_tokens=max_tokens,
                            stop_checker=stop_checker,
                            force=False,
                            status_callback=status_cb,
                            enabled=True,
                        )
                        if isinstance(turn, TurnController):
                            turn._last_compact_reason = result.reason
                            turn._last_compact_tokens_before = result.tokens_before
                            turn._last_compact_tokens_after = result.tokens_after
                        if result.reason == "aborted":
                            if batched:
                                batched.flush()
                            emit((StreamQueueKind.STOPPED,))
                            return
                    client.stream_chat_response(
                        messages_for_llm(session), max_tokens, append_c, append_t,
                        stop_checker=stop_checker,
                        status_callback=status_cb,
                    )
                if stopped_for_this_send():
                    if batched: batched.flush()
                    note_stop_partial(turn, "".join(last_streamed))
                    emit((StreamQueueKind.STOPPED,))
                else:
                    if batched: batched.flush()
                    emit((StreamQueueKind.FINAL_DONE, "".join(last_streamed)))
            except Exception as e:
                from plugin.framework.async_stream import BlockingWaitStopped
                if isinstance(e, BlockingWaitStopped):
                    if batched: batched.flush()
                    emit((StreamQueueKind.STOPPED,))
                else:
                    if isinstance(e, NetworkError):
                        log.exception("Final stream NetworkError")
                    else:
                        log.exception("Final stream failed")
                    if batched: batched.flush()
                    emit((StreamQueueKind.ERROR, format_error_payload(e)))

        run_in_background(run_final, name="llm-worker-final", dedicated=True)

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
                with suppress_disposed("Tool loop event: mutates_document check", logger=log):
                    from plugin.main import get_tools as _get_tools_registry

                    tool = _get_tools_registry().get(s[2])
                    if tool and tool.detects_mutation():
                        mutates = True
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
        # A sentinel after abort is not another tool round.
        live = current_turn(self)
        if isinstance(live, TurnController) and not live.alive:
            return True
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

        # Execute the effects
        exit_loop = False
        for effect in tr.effects:
            if self._execute_effect(effect):
                exit_loop = True

        return exit_loop

    def _handle_stream_stopped(self: ToolLoopHost) -> None:
        # The turn commits the open row, closes tool calls that have no
        # result, and writes the stop line. The FSM only latches Stopped.
        turn = current_turn(self)
        partial = take_stop_partial(turn)
        if isinstance(turn, TurnController):
            turn.close_stopped(self, partial)
        event = ToolLoopEvent(kind=EventKind.STOP_REQUESTED, data={})
        tr = next_state(self._sm_state, event)
        self._sm_state = tr.state

        for effect in tr.effects:
            self._execute_effect(effect)

    def _handle_stream_error(self: ToolLoopHost, e: Any) -> bool | None:
        from plugin.scripting.audio_recorder_service import clear_pending_audio_wav, try_native_audio_stt_fallback

        live = current_turn(self)
        if isinstance(live, TurnController) and not live.alive:
            # Dead turn (e.g. after Stop): nothing will retry, so drop the WAV.
            # Not a try/finally: the overflow respawn below returns True, and a
            # native-audio rejection on that retry still needs the WAV for STT.
            clear_pending_audio_wav(self)
            return None
        # Native-audio rejection retries as text on this drain. WAV attach and
        # the STT retry live in audio_recorder_service, next to recording.

        fallback = try_native_audio_stt_fallback(self, e)
        if fallback is not False:
            return fallback

        # If we reached here, it's either not a modality error or STT is not configured.
        # Drain ERROR items are format_error_payload dicts (see _spawn_llm_worker),
        # not Exception — format_error_message() requires Exception (deal.pre).
        if isinstance(e, dict):
            err_msg = str(e.get("message") or e.get("code") or e)
            crash_blob = "%s %s" % (err_msg, e)
        elif isinstance(e, Exception):
            err_msg = format_error_message(e)
            crash_blob = err_msg
        else:
            err_msg = str(e)
            crash_blob = err_msg
        # Prompt-too-large: respawn the worker with force_compact. Never call
        # compact_session on this drain / UI thread (UNO + lane). Process death
        # is not overflow — compact-and-retry on a dead llama-server is worse
        # than today's sentence. Kill switch offs this path too.
        if get_config_bool_safe("chat_compaction_enabled"):
            stop_checker = self.resolve_stop_checker()
            stopped = bool(self.stop_requested or stop_checker())
            live_turn = running_turn(self)
            retry_q = spawn_queue(live_turn) if live_turn is not None else None
            if (
                not stopped
                and retry_q is not None
                and self._active_client is not None
                and not is_process_death_error(crash_blob)
                and is_context_overflow_error(crash_blob)
                and isinstance(live_turn, TurnController)
                and live_turn._overflow_compact_attempts < MAX_OVERFLOW_COMPACTION_ATTEMPTS
                and should_retry_overflow(
                    live_turn._overflow_compact_attempts,
                    live_turn._last_compact_reason,
                    live_turn._last_compact_tokens_before,
                    live_turn._last_compact_tokens_after,
                )
            ):
                live_turn._overflow_compact_attempts += 1
                self._set_status("Compacting conversation...")
                self._spawn_llm_worker(
                    retry_q,
                    self._active_client,
                    self._active_max_tokens,
                    self._active_tools or [],
                    self._sm_state.round_num,
                    query_text=self._active_query_text,
                    force_compact=True,
                )
                return True
        # Issue #570: llama-server died / prompt overflow. Plain sentence only —
        # never dump the format_error_payload dict into the sidebar.
        if is_local_model_server_crash(crash_blob):
            display = err_msg
            if (
                not isinstance(display, str)
                or display.lstrip()[:1] in "{["
                or "HTTP Error" in display
                or not is_local_model_server_crash(display)
            ):
                display = local_model_overflow_message()
            banner = "\n%s\n" % display
        else:
            banner = "\n[API error: %s]\n" % err_msg
        self._append_response(banner)
        # The user row is already stored. Leaving the banner widget-only made
        # a retry append a second user row with a hole where the assistant was.
        # Overflow respawn and audio fallback return before this write.
        persist_assistant_on_turn(self, content=banner.strip())
        self._terminal_status = "Error"
        self._set_status("Error")
        clear_pending_audio_wav(self)
        return None

    def _on_tool_loop_approval_required(self: ToolLoopHost, item: Any) -> None:
        """Main-thread handler: show inline Accept/Reject and unblock the tool worker."""
        query_for_engine = item[1] if len(item) > 1 else ""
        tool_name = item[2] if len(item) > 2 else ""
        event_obj = item[3] if len(item) > 3 else None
        if event_obj is not None:
            self.begin_inline_web_approval(query_for_engine, tool_name, event_obj)
        log.info("tool_loop on_approval_required: tool=%s (inline Accept/Change/Reject)", tool_name)

    def _start_tool_calling_async(self: ToolLoopHost, client: "LlmClient", model: Any, max_tokens: int, tools: list[dict[str, Any]], execute_tool_fn: Callable[..., Any], max_tool_rounds: int | None = None, query_text: str | None = None) -> None:
        """Tool-calling event loop: single queue, main-thread drain.

        Background threads push messages onto q. The main thread drains via
        ``run_stream_drain_loop``: event-driven slices return to VCL between
        batches; the blocking fallback still pumps with ``pump_ui_idle``.
        """
        if max_tool_rounds is None:
            max_tool_rounds = get_config_int("chatbot.max_tool_rounds")
        log.info("=== Tool-calling loop START (max %d rounds) ===" % max_tool_rounds)
        # Overflow retry counters live on the fresh per-send TurnController.
        self._append_response("\nAI: ")
        self._record_assistant_start = True

        try:
            from plugin.main import get_tools as _get_tools_registry

            registry = _get_tools_registry()
            async_tools = frozenset(
                tool.name
                for tool in registry.get_tools(filter_doc_type=False, exclude_tiers=())
                if tool.name is not None and getattr(tool, "is_async", lambda: False)()
            )
        except Exception as e:
            log.debug("Failed to get async tools list, falling back to defaults: %s", e)
            async_tools = frozenset({"web_research", "image_generate"})

        self._sm_state = ToolLoopState(round_num=0, pending_tools=[], max_rounds=max_tool_rounds, status="Thinking...", async_tools=async_tools)

        turn = running_turn(self)
        if turn is None:
            return
        try:
            raw_q: queue.Queue[Any] = queue.Queue()
            batched = BatchingStreamQueue(raw_q, batch_interval=CHAT_STREAM_BATCH_INTERVAL)
            # The drain attaches the queue. Workers only enqueue on it.
            turn.queue = raw_q
            turn.batcher = batched
            if turn.model is None:
                turn.model = model

            self._active_client = client
            self._active_max_tokens = max_tokens
            self._active_tools = tools
            self._active_execute_tool_fn = execute_tool_fn
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

                msg = "\n[" + _("Error: Toolkit unavailable") + "]\n"
                self._append_response(msg)
                persist_assistant_on_turn(self, content=msg.strip())
                self._terminal_status = "Error"
                self._set_status("Error")
                return

            # Check once whether execute_tool_fn accepts status_callback
            sig = inspect.signature(execute_tool_fn)
            self._active_supports_status = "status_callback" in sig.parameters or "kwargs" in sig.parameters

            # --- Thinking display state (mirrors run_stream_drain_loop behavior) ---

            # --- Kick off the first LLM stream (producer batching at 250 ms) ---
            self._refresh_active_tools_for_session()
            self._spawn_llm_worker(spawn_queue(turn), self._active_client, self._active_max_tokens, self._active_tools, self._sm_state.round_num, query_text=self._active_query_text)

            def _flush_active_batcher() -> None:
                # Stop used to clear this batcher in finally without a last
                # flush, dropping up to one batch interval of text.
                if turn.batcher is not None:
                    turn.batcher.flush()

            run_stream_drain_loop(
                turn.queue,
                toolkit,
                [False],
                self._append_response,
                on_stream_done=self._handle_stream_completion,
                on_stopped=self._handle_stream_stopped,
                on_error=self._handle_stream_error,
                on_status_fn=self._set_status,
                stop_checker=self.resolve_stop_checker(),
                show_search_thinking=show_search_thinking,
                on_approval_required=self._on_tool_loop_approval_required,
                flush_pending=_flush_active_batcher,
            )

            def _after_tool_drain() -> None:
                # Blocking drain: this runs before we return. Event-driven
                # drain: the function already returned to VCL; this runs on
                # the terminal slice, still before abort_turn below.
                from plugin.chatbot.rich_text import finalize_sidebar_assistant_response

                finalize_sidebar_assistant_response(self, allow_rerender=not self.stop_requested)

            defer_until_drain_done(_after_tool_drain)
        finally:
            def _end_tool_drain() -> None:
                # The drain has finished. Abort so a timer or a late tool cannot
                # enqueue. The outer send drain still holds ``_turn`` for the
                # spoken reply, then drops it.
                abort_turn(self)
                self._tool_loop_interpreter = None
                self.sidebar_state = dataclasses.replace(self.sidebar_state, tool_loop=None)

            defer_until_drain_done(_end_tool_drain)

    def begin_inline_web_approval(self, query: str, tool: str, event: Any) -> None:
        """Override on ``SendButtonListener`` for real UI. Default: auto-approve (tests / no panel)."""
        if event is not None:
            event.approved = True
            event.query_override = None
            event.set()
