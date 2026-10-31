"""ToolCallingMixin: core chat-with-tools engine for the sidebar.

Composed onto SendButtonListener in panel.py and contains the
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
import os
from typing import TYPE_CHECKING, Protocol, Any, Callable, Sequence, cast

if TYPE_CHECKING:
    import queue
    from plugin.framework.client.llm_client import LlmClient
    from plugin.chatbot.chat_session import ChatSession

from plugin.framework.json_utils import safe_json_loads
from plugin.framework.stream_batch import BatchingStreamQueue, StreamQueueKind
from plugin.framework.async_stream import StreamQueue, defer_until_drain_done, run_stream_drain_loop
from plugin.framework.logging import agent_log, update_activity_state
from plugin.framework.errors import BlockingWaitStopped, best_effort, format_error_message, is_tool_document_disposed, resolve_exception_message, suppress_disposed, UnoObjectError
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
from plugin.framework.client import model_fetcher
from plugin.framework.client.model_fetcher import (
    resolve_context_window,
    set_image_model,
)
from plugin.chatbot.config_ui_helpers import sync_sidebar_text_model
from plugin.framework.constants import CHAT_DOCUMENT_CONTEXT_MAX_CHARS
from plugin.framework.errors import format_error_payload
from plugin.framework.llm_lanes import llm_request_lane
from plugin.framework.send_cancellation import capture_send_stop, send_stopped
from plugin.framework.client.llm_client import LlmClient
from plugin.framework.config_schema import as_bool
from plugin.doc.peer_message import log_peer_tool_on_wire

from plugin.framework.worker_pool import run_in_background
from plugin.framework.uno_context import get_toolkit
from plugin.chatbot.tool_loop_actions import (
    ToolLoopEffectInterpreter,
    TurnController,
    abort_turn,
    build_tool_execute_fn,
    current_turn,
    finalize_sidebar_assistant_response,
    note_stop_partial,
    persist_assistant_on_turn,
    put_for_turn,
    running_turn,
    session_for_turn,
    spawn_queue,
    take_stop_partial,
)

from plugin.chatbot.tool_loop_state import (
    ToolLoopState,
    ToolLoopEvent,
    EventKind,
    next_state,
    object_dict_or_empty,
)
from plugin.chatbot.compaction import (
    MAX_OVERFLOW_COMPACTION_ATTEMPTS,
    compact_session,
    is_context_overflow_error,
    is_process_death_error,
    messages_for_llm,
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
    _panel: Any
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

    _record_assistant_start: bool
    _turn: Any

    def _append_response(self, text: str, is_thinking: bool = False, role: str = "assistant") -> None: ...
    def _set_status(self, text: str) -> None: ...
    def _get_document_model(self) -> Any: ...
    _events: Any
    _voice: Any
    def _get_mcp_url(self) -> str | None: ...

    @property
    def _sm_state(self) -> "ToolLoopState": ...
    @_sm_state.setter
    def _sm_state(self, value: "ToolLoopState | None") -> None:  # pyright: ignore[reportPropertyTypeMismatch]  # clear session with None
        ...

    # Mixin methods called on self
    def _start_tool_calling_async(self, client: "LlmClient", model: Any, max_tokens: int, tools: list[dict[str, Any]], execute_tool_fn: Callable[..., Any], max_tool_rounds: int, query_text: str | None = None) -> None: ...
    def _spawn_llm_worker(self, q: "queue.Queue[Any] | BatchingStreamQueue", client: "LlmClient", max_tokens: int, tools: list[dict[str, Any]], round_num: int, query_text: str | None = None, force_compact: bool = False) -> None: ...
    def _spawn_final_stream(self, q: "queue.Queue[Any] | BatchingStreamQueue", client: "LlmClient", max_tokens: int) -> None: ...
    def _create_event_from_stream_item(self, item: Any) -> ToolLoopEvent | None: ...
    def _handle_stream_completion(self, item: Any) -> bool: ...
    def _handle_stream_stopped(self) -> None: ...
    def _handle_stream_error(self, e: Any) -> bool | None: ...
    def _on_tool_loop_approval_required(self, item: Any) -> None: ...
    def _execute_effect(self, effect: Any) -> bool: ...
    def _apply(self, event: ToolLoopEvent) -> bool: ...
    def _do_send_chat_with_tools(self, query_text: str, model: Any, doc_type_str: str) -> None: ...
    def _refresh_active_tools_for_session(self) -> None: ...


def _last_user_message_with_image(session: Any) -> dict[str, Any] | None:
    """The session's last user message when it carries an image_url part."""
    for msg in reversed(getattr(session, "messages", None) or []):
        if isinstance(msg, dict) and msg.get("role") == "user":
            content = msg.get("content")
            if isinstance(content, list) and any(isinstance(p, dict) and p.get("type") == "image_url" for p in content):
                return msg
            return None
    return None


def _strip_user_image(session: Any, msg: dict[str, Any], fallback_text: str) -> None:
    """Drop msg's image_url parts. Text-only leftovers collapse to a string.

    Other parts (input_audio appended in memory for this turn) stay.
    """
    rest = [p for p in msg["content"] if not (isinstance(p, dict) and p.get("type") == "image_url")]
    if all(isinstance(p, dict) and p.get("type") == "text" for p in rest):
        clean_text = "\n".join(t for t in (str(p.get("text", "")) for p in rest) if t).strip()
        msg["content"] = clean_text or fallback_text
    else:
        msg["content"] = rest
    if getattr(session, "db", None) is not None:
        session.db.update_last_user_content(msg["content"])


def _parsed_tool_call_args(tc: object) -> dict[str, Any]:
    """Arguments for one streamed tool call. Missing, non-dict, and bad JSON are {}."""
    func = tc.get("function") if isinstance(tc, dict) else None
    raw_args = func.get("arguments") if isinstance(func, dict) else None
    if isinstance(raw_args, dict):
        return raw_args
    if isinstance(raw_args, str):
        return object_dict_or_empty(safe_json_loads(raw_args))
    return {}


def _live_text(turn: Any, callback: Callable[[str], None]) -> Callable[[str], None]:
    """Ignore deltas after this turn has been aborted."""

    def wrapped(text: str) -> None:
        if isinstance(turn, TurnController) and not turn.alive:
            return
        callback(text)

    return wrapped


def _compact_bound_session(
    session: Any,
    client: Any,
    turn: Any,
    *,
    tools: Any,
    max_tokens: int | None,
    stop_checker: Callable[[], bool] | None,
    force: bool,
    status_cb: Callable[[str], None],
) -> bool:
    """True when the caller should stream. False when the turn was aborted.

    Disabled compaction returns True without writing the turn fields.
    """
    if not get_config_bool_safe("chat_compaction_enabled"):
        return True
    result = compact_session(
        session,
        client,
        window=resolve_context_window(client),
        tools=tools,
        max_tokens=max_tokens,
        stop_checker=stop_checker,
        force=force,
        status_callback=status_cb,
        enabled=True,
    )
    if isinstance(turn, TurnController):
        turn._last_compact_reason = result.reason
        turn._last_compact_tokens_before = result.tokens_before
        turn._last_compact_tokens_after = result.tokens_after
    if result.reason == "aborted":
        # The caller emits STOPPED through turn.put, which flushes.
        return False
    return True


def try_native_audio_stt_fallback(host: Any, error: Any) -> bool | None:
    """After a native ``input_audio`` rejection, transcribe and respawn this drain.

    The chat POST already failed, so this must not re-enter
    ``_do_send_chat_with_tools`` / ``_start_tool_calling_async`` (nested drain).
    The model-catalog imports stay inside the function so importing this
    module to spawn a recording does not load the chat model fetcher.

    False: not a native-audio rejection, or STT cannot run. The caller
    continues with overflow and generic API handling.
    True: a replacement worker was spawned on this drain. The caller must
    keep draining. The drain treats only True that way.
    None: stop the drain without a second API error. Transcription threw
    (``_transcribe_audio`` already reported it and deleted the WAV), or
    empty speech already ended the turn (banner shown, no worker).
    """
    from plugin.audio.stt_service import uses_local_stt
    from plugin.framework.client.errors import is_400_input_validation, is_audio_unsupported_error
    from plugin.framework.client.model_fetcher import get_stt_model, set_native_audio_support
    from plugin.framework.i18n import _

    if not host.audio_wav_path or not (is_400_input_validation(error) or is_audio_unsupported_error(error)):
        return False

    turn = getattr(host, "_turn", None)
    # Read text_model and endpoint from the turn captured when the audio was
    # attached. Re-reading the combobox mid-turn would mark the newly chosen
    # model unsupported instead of the model the audio was sent to.
    turn_model = getattr(turn, "text_model", None)
    turn_endpoint = getattr(turn, "endpoint", None)
    if not turn_model:
        from plugin.framework.client.model_fetcher import get_text_model
        from plugin.framework.config import get_current_endpoint

        turn_model = get_text_model()
        turn_endpoint = get_current_endpoint()
    if turn_model:
        log.warning("Model %s failed native audio, caching and falling back to STT", turn_model)
        set_native_audio_support(turn_model, turn_endpoint, supported=False)

    stt_model = get_stt_model()
    # Local Whisper does not need an endpoint model id. Endpoint STT still does.
    local_stt = uses_local_stt()
    if turn is None or not getattr(turn, "alive", False):
        return False
    retry_q = getattr(turn, "batcher", None) or getattr(turn, "queue", None)
    if (stt_model or local_stt) and retry_q is not None and getattr(turn, "active_client", None) is not None:
        host._append_response("\n[Model does not support audio. Falling back to STT...]\n")
        try:
            transcript = host._voice._transcribe_audio(host.audio_wav_path, stt_model)
            if getattr(host, "_terminal_status", None) == "Stopped":
                # Stop during fallback STT. None ends the drain. Do not show
                # "No speech detected" or spawn another chat worker.
                return None
            # What was wrong: STT fallback wrote to host.session instead of the turn's session.
            # How it happened: _transcribe_audio pumps UI idle, letting the user switch modes
            # or sessions mid-transcription while the turn is aborted.
            # Why this fixes it: Bind turn.session and verify the turn is still alive and has
            # identical message list identity before modifying session state or deleting the WAV.
            turn_session = getattr(turn, "session", None)
            same_messages = getattr(turn, "same_messages", None)
            if (
                turn_session is None
                or not getattr(turn, "alive", False)
                or not (callable(same_messages) and same_messages())
            ):
                return None
            wav_path = host.audio_wav_path
            host.audio_wav_path = None
            if wav_path:
                try:
                    os.remove(wav_path)
                except OSError as rem_err:
                    log.debug("Failed to remove audio_wav_path after STT fallback: %s", rem_err)
            if not (transcript or "").strip():
                # G27: empty STT must not spawn a blank chat POST.
                # The drain keeps running only when on_error returns True,
                # which means a replacement worker was spawned. Empty speech
                # spawns nothing and posts no STREAM_DONE, so returning True
                # left the sidebar on Stop. None means this turn is finished
                # and a second API error should not be shown.
                host._append_response("\n" + _("[No speech detected.]") + "\n")
                host._terminal_status = ""
                return None
            combined = (turn.active_query_text + "\n" + transcript).strip() if turn.active_query_text else transcript
            if turn_session.messages and turn_session.messages[-1].get("role") == "user":
                turn_session.messages[-1]["content"] = combined
                if getattr(turn_session, "db", None):
                    turn_session.db.update_last_user_content(combined)
            else:
                turn_session.add_user_message(combined)
            turn.active_query_text = combined
            host._tool_calling._spawn_llm_worker(retry_q, turn.active_client, turn.active_max_tokens, turn.active_tools or [], host._tool_calling._sm_state.round_num, query_text=combined)
            return True
        except Exception:
            log.exception("STT fallback after native-audio error failed")
        return None
    return False


class _PanelDescriptor:
    """Descriptor that returns the parent panel host, or the instance itself when unset (e.g. in test subclasses)."""

    def __get__(self, instance: Any, owner: Any = None) -> Any:
        if instance is None:
            return self
        return instance.__dict__.get("_panel_obj", instance)

    def __set__(self, instance: Any, value: Any) -> None:
        instance.__dict__["_panel_obj"] = value


class ToolCallingMixin:
    """Tool-loop control state lives only in ``sidebar_state.tool_loop`` (via ``_sm_state``).

    Remaining ``_active_*`` fields on the host are the client, tool schemas, and
    query text for :class:`ToolLoopEffectInterpreter`. The queue, stripper, and
    document model live on the turn — not a second copy of round/pending/stop.
    """

    _panel: Any = _PanelDescriptor()

    def __init__(self, panel: Any = None) -> None:
        if panel is not None:
            self._panel = panel

    # Defaults satisfy basedpyright reportUninitializedInstanceVariable; panel __init__ overwrites.
    client: LlmClient | None = None
    audio_wav_path: str | None = None
    sidebar_state: Any = None
    _terminal_status: str = "Ready"
    _record_assistant_start: bool = False
    _turn: Any = None

    @property
    def _sm_state(self: ToolLoopHost) -> ToolLoopState:
        tl = self._panel.sidebar_state.tool_loop
        if tl is None:
            raise RuntimeError("Tool loop state used without active session")
        return tl

    @_sm_state.setter
    def _sm_state(self: ToolLoopHost, value: ToolLoopState | None) -> None:  # pyright: ignore[reportPropertyTypeMismatch]  # clear session with None
        self._panel.sidebar_state = dataclasses.replace(self._panel.sidebar_state, tool_loop=value)

    def _do_send_chat_with_tools(self: ToolLoopHost, query_text: str, model: Any, doc_type_str: str, skip_append_user: bool = False) -> None:
        # The send already called begin_send_turn. A mode click inside
        # pump_ui_idle aborts that turn and swaps the session. Do not start
        # another turn here: that rebind wrote this reply onto the other chat.
        if running_turn(self._panel) is None:
            return
        live = current_turn(self._panel)
        if isinstance(live, TurnController) and live.model is None:
            live.model = model
        try:
            log.debug("_do_send: importing core modules...")
            from plugin.main import get_tools

            log.debug("_do_send: core modules imported OK")
        except Exception as e:
            log.exception("_do_send: core modules import FAILED")
            self._panel._append_response("\n[Import error - core: %s]\n" % e)
            self._panel._terminal_status = "Error"
            return

        # Callback for updating active domain in the session
        bound_turn = current_turn(self._panel)

        def set_active_domain(domain: Any, python_tool_domain: Any = None) -> None:
            # The tool callback runs later. A new send must not retarget the
            # domain onto the session that replaced this one.
            if not isinstance(bound_turn, TurnController) or not bound_turn.can_write(self._panel):
                return
            assert bound_turn.session is not None
            session = bound_turn.session
            session.active_specialized_domain = domain
            session.python_tool_domain = python_tool_domain
            log.debug("_do_send: updated active specialized domain to: %s (python_tool_domain: %s)", domain, python_tool_domain)

        try:
            log.debug("_do_send: loading %s schema..." % doc_type_str)
            turn_session = session_for_turn(self._panel)
            active_domain = getattr(turn_session, "active_specialized_domain", None) if turn_session is not None else None
            python_tool_domain = getattr(turn_session, "python_tool_domain", None) if turn_session is not None else None
            from plugin.framework.queue_executor import pump_ui_idle

            toolkit = get_toolkit(self._panel.ctx)
            if toolkit:
                pump_ui_idle(toolkit, max_queue_items=4)
            active_tools = get_tools().get_schemas(
                "openai",
                doc_type=doc_type_str,
                uno_services_supported=getattr(self._panel, "cached_uno_services", None),
                active_domain=active_domain,
                ctx=self._panel.ctx,
                doc=model,
            )
            log_peer_tool_on_wire(active_tools)
            execute_fn = build_tool_execute_fn(self._panel, doc_type_str, active_domain, python_tool_domain, set_active_domain)

        except Exception as e:
            log.exception("_do_send: tool import FAILED")
            self._panel._append_response("\n[Import error - tools: %s]\n" % e)
            self._panel._terminal_status = "Error"
            return

        synced_model = sync_sidebar_text_model(self._panel.ctx, self._panel.model_selector)
        if synced_model:
            log.debug("_do_send: text model updated to %s" % synced_model)
        if self._panel.image_model_selector:
            from plugin.framework.model_lru import _sanitize_model_combobox_value

            selected_image_model = _sanitize_model_combobox_value(str(self._panel.image_model_selector.getText() or ""))
            if selected_image_model:
                set_image_model(selected_image_model)
                log.debug("_do_send: image model updated to %s" % selected_image_model)

        max_tokens = get_config_int("chat_max_tokens")
        log.debug("_do_send: config loaded: max_tokens=%d, max_context=%d" % (max_tokens, CHAT_DOCUMENT_CONTEXT_MAX_CHARS))

        api_config = get_api_config()
        ok, err_msg = validate_api_config(api_config)
        if not ok:
            self._panel._append_response("\n[%s]\n" % err_msg)
            self._panel._terminal_status = "Error"
            self._panel._set_status("Error")
            return

        # contextvars (SendCancellation) do not propagate to worker threads — LlmClient
        # picks up resolve_stop_checker() via get_current_send_cancellation when created on
        # the UI thread; spawned workers pass stop_checker= explicitly (_spawn_llm_worker).
        from plugin.framework.send_cancellation import get_current_send_cancellation

        if not self._panel.client:
            self._panel.client = LlmClient(api_config, self._panel.ctx)
        else:
            self._panel.client.config = api_config
            # New send: clear Stop latch from a prior turn (UI thread only).
            self._panel.client.clear_stop()
        assert self._panel.client is not None
        client = self._panel.client
        # A duplicate hook does not stop again. clear_stop() just cleared the
        # latch, so a scope that is already cancelled must stop here.
        scope = get_current_send_cancellation()
        if scope is not None:
            scope.register_client(client)
            if scope.is_cancelled() and not client._stopped:
                client.stop()

        self._panel._set_status("Reading document...")
        started = running_turn(self._panel)
        if isinstance(started, TurnController):
            started.text_model = str(api_config.get("model") or "") or None
            started.endpoint = client._endpoint()
        turn_session = started.session if started is not None else None
        if turn_session is None:
            # Clear replaced messages, or Stop aborted the turn, while this
            # send was still starting. The user row must not land on the wiped chat.
            return
        try:
            turn_session.refresh_document_context(model, self._panel.ctx)
            doc_text = turn_session.document_context
            log.debug("_do_send: document context length=%d" % len(doc_text))
            agent_log("tool_loop.py:doc_context", "Document context for AI", data={"doc_length": len(doc_text), "doc_prefix_first_200": (doc_text or "")[:200], "max_context": CHAT_DOCUMENT_CONTEXT_MAX_CHARS}, hypothesis_id="B")
        except UnoObjectError:
            log.exception("Document unavailable")
            self._panel._append_response("\n[Document closed or unavailable.]\n")
            self._panel._terminal_status = "Error"
            self._panel._set_status("Error")
            return
        except Exception as e:
            if is_tool_document_disposed(e, model):
                log.debug("Document likely disposed while reading context: %s", e)
                self._panel._append_response("\n[Document closed or unavailable.]\n")
            else:
                log.exception("Unexpected document error")
                err_msg = resolve_exception_message(e)
                self._panel._append_response("\n[Error reading document: %s]\n" % err_msg)
            self._panel._terminal_status = "Error"
            self._panel._set_status("Error")
            return

        # Peer extracted send already appended the envelope + body once.
        # Calling add_user_message again would double-post that turn.
        # The turn was bound before pump_ui_idle. Refresh edits that list
        # in place, so the pin still names it.
        if not skip_append_user:
            # Check for vision capability and selected image base64.
            # `model` in this function is the UNO document, not the model id.
            # get_api_config stores the chat model as "model". Reading
            # "text_model" was always empty, so has_native_vision returned
            # False and a selected image was never attached.
            # _do_send runs on the UI thread: a cold model answers None here
            # without blocking. Capture unless it is a known no; round 0 of
            # the LLM worker resolves it and drops the image unless True.
            b64_image = None
            text_model_id = str(api_config.get("model") or "")
            typed_key = str(api_config.get("api_key") or "").strip()
            vision = model_fetcher.has_native_vision(text_model_id, client._endpoint(), api_key_override=typed_key or None)
            if vision is not False:
                doc = self._panel._get_document_model()
                if doc:
                    try:
                        from plugin.writer.images.image_tools import get_selected_image_base64
                        b64_image = get_selected_image_base64(doc, self._panel.ctx)
                    except Exception:
                        log.exception("Failed to get selected image base64")

            if b64_image or self._panel.audio_wav_path:
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
                if self._panel.audio_wav_path:
                    from plugin.scripting.audio_recorder_service import append_wav_as_input_audio

                    audio_content = []
                    if append_wav_as_input_audio(audio_content, self._panel.audio_wav_path):
                        attachments.append("Audio")
                        # Capture the model/endpoint the audio is sent to, so a model
                        # switch mid-send does not mark the wrong model as audio-capable.
                        audio_turn = current_turn(self._panel)
                        if isinstance(audio_turn, TurnController):
                            audio_turn.text_model = str(api_config.get("model") or "") or None
                            audio_turn.endpoint = client._endpoint()
                    else:
                        self._panel.audio_wav_path = None

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
                self._panel._append_response(display_text, role="user")
            else:
                turn_session.add_user_message(query_text)
                self._panel._append_response(query_text, role="user")

        self._panel._append_response("\n[Using chat model.]\n")
        log.info("_do_send: using chat model")

        self._panel._set_status("Connecting to AI (tools=True)...")
        log.debug("_do_send: calling AI, use_tools=True, messages=%d" % len(turn_session.messages))

        max_tool_rounds = cast("int", api_config["chat_max_tool_rounds"])
        self._start_tool_calling_async(client, model, max_tokens, active_tools, execute_fn, max_tool_rounds, query_text=query_text)

        log.debug("=== _do_send END (async started, level=logging.INFO) ===")

    def _refresh_active_tools_for_session(self: ToolLoopHost) -> None:
        """Recompute OpenAI tool schemas from ``session.active_specialized_domain``.

        In-place specialized delegation updates the session after ``delegate`` or
        ``specialized_workflow_finished``; each LLM round must see the matching list.
        """
        turn = current_turn(self._panel)
        if not isinstance(turn, TurnController):
            return
        try:
            from plugin.main import get_tools

            turn_session = session_for_turn(self._panel)
            active_domain = getattr(turn_session, "active_specialized_domain", None) if turn_session is not None else None
            refresh_doc = turn.model
            if refresh_doc is None:
                refresh_doc = self._panel._get_document_model()
            turn.active_tools = get_tools().get_schemas(
                "openai",
                doc_type=getattr(self._panel, "cached_doc_type", None),
                uno_services_supported=getattr(self._panel, "cached_uno_services", None),
                active_domain=active_domain,
                ctx=getattr(self._panel, "ctx", None),
                doc=refresh_doc,
            )
            log_peer_tool_on_wire(turn.active_tools)
        except Exception as e:
            log.warning("Failed to refresh active tools: %s", e)

    def _spawn_llm_worker(self: ToolLoopHost, q: "queue.Queue[Any] | BatchingStreamQueue", client: "LlmClient", max_tokens: int, tools: list[dict[str, Any]], round_num: int, query_text: str | None = None, force_compact: bool = False) -> None:
        """Spawn a background thread that streams the LLM response into q (or the batcher's raw queue)."""
        batched = q if isinstance(q, BatchingStreamQueue) else None

        update_activity_state("tool_loop", round_num=round_num)
        log.debug("Tool loop round %d: sending %d messages to API..." % (round_num, len(self._panel.session.messages)))
        resume_status = "Thinking..." if round_num == 0 else "Thinking (round %d)..." % (round_num + 1)
        # Overflow retry already set "Compacting conversation..." on the drain
        # thread. Host Thinking here would flash over that ack before the
        # worker's status_callback can post Compacting again.
        if not force_compact:
            self._panel._set_status(resume_status)

        self._panel._record_assistant_start = True

        turn = current_turn(self._panel)
        # Captured at spawn. session_for_turn at run time would be the next send.
        bound_session = turn.session if isinstance(turn, TurnController) else None
        # Spawn-time scope. The body must not call resolve_stop_checker or
        # read _send_cancellation: Stop clears the field and the next send replaces it.
        bound_scope, stop_checker = capture_send_stop(self._panel)

        def emit(item: Any) -> None:
            put_for_turn(turn, item)

        def stopped_for_this_send() -> bool:
            return send_stopped(bound_scope, stop_checker, client)

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
                    emit((StreamQueueKind.STOPPED,))
                    return
                # Status via queue only — never self._set_status from this worker (UNO).
                retry_shown = False

                def status_cb(t: str) -> None:
                    nonlocal retry_shown
                    retry_shown = True
                    emit((StreamQueueKind.STATUS, t))

                raw_chunk = batched.content_cb() if batched else lambda t: emit((StreamQueueKind.CHUNK, t))

                def on_chunk(t: str) -> None:
                    nonlocal retry_shown
                    if retry_shown:
                        retry_shown = False
                        emit((StreamQueueKind.STATUS, resume_status))
                    raw_chunk(t)

                # The send attached the selection unless the UI-thread answer
                # was False. Resolve here, off the UI thread, so the first
                # request to a cold model is right; unknown does not attach.
                image_msg = _last_user_message_with_image(session) if round_num == 0 else None
                if image_msg is not None:
                    client_config = getattr(client, "config", None)
                    typed_key = str(client_config.get("api_key") or "").strip() if isinstance(client_config, dict) else ""
                    if model_fetcher.has_native_vision(turn.text_model, turn.endpoint, api_key_override=typed_key or None) is not True:
                        _strip_user_image(session, image_msg, turn.active_query_text or "")

                with llm_request_lane(status_callback=status_cb, resume_status=resume_status):
                    # Compact + stream share one lane hold. compaction.py must
                    # not take the non-reentrant lock itself.
                    if not _compact_bound_session(
                        session,
                        client,
                        turn,
                        tools=tools,
                        max_tokens=max_tokens,
                        stop_checker=stop_checker,
                        force=force_compact,
                        status_cb=status_cb,
                    ):
                        emit((StreamQueueKind.STOPPED,))
                        return
                    payload = messages_for_llm(session)
                    response = client.stream_request_with_tools(
                        payload, max_tokens, tools=tools,
                        append_callback=_live_text(turn, on_chunk),
                        append_thinking_callback=_live_text(turn, batched.thinking_cb() if batched else lambda t: emit((StreamQueueKind.THINKING, t))),
                        stop_checker=stop_checker,
                        status_callback=status_cb,
                    )
                # Stop during pre-send host-gap wait returns finish_reason "stop"
                # without raising. The captured scope stays cancelled after the
                # next send clears the panel field and calls clear_stop() on
                # this shared client, so a live stop_requested read is wrong.
                if stopped_for_this_send():
                    note_stop_partial(turn, response)
                    emit((StreamQueueKind.STOPPED,))
                else:
                    update_activity_state("tool_loop", round_num=round_num)
                    emit((StreamQueueKind.STREAM_DONE, response))
            except BlockingWaitStopped:
                emit((StreamQueueKind.STOPPED,))
            except Exception as e:
                log.exception("Tool loop round %d: API ERROR" % round_num)
                emit((StreamQueueKind.ERROR, format_error_payload(e)))

        run_in_background(run, name=f"llm-worker-{round_num}", dedicated=True)

    def _spawn_final_stream(self: ToolLoopHost, q: "queue.Queue[Any] | BatchingStreamQueue", client: "LlmClient", max_tokens: int) -> None:
        """Spawn a background thread for a final no-tools stream into q (or the batcher's raw queue)."""
        batched = q if isinstance(q, BatchingStreamQueue) else None

        update_activity_state("exhausted_rounds")
        self._panel._set_status("Finishing...")
        self._panel._append_response("\nAI: ")
        self._panel._record_assistant_start = True

        turn = current_turn(self._panel)
        bound_session = turn.session if isinstance(turn, TurnController) else None
        # Same spawn-time capture as _spawn_llm_worker. The final-stream body
        # must not resolve the panel field when it later starts.
        bound_scope, stop_checker = capture_send_stop(self._panel)

        def emit(item: Any) -> None:
            put_for_turn(turn, item)

        def stopped_for_this_send() -> bool:
            return send_stopped(bound_scope, stop_checker, client)

        def run_final() -> None:
            session = bound_session
            if not isinstance(turn, TurnController) or not turn.alive or session is None:
                emit((StreamQueueKind.STOPPED,))
                return
            last_streamed: list[str] = []
            try:
                # Status via queue only — never self._set_status from this worker (UNO).
                retry_shown = False

                def status_cb(t: str) -> None:
                    nonlocal retry_shown
                    retry_shown = True
                    emit((StreamQueueKind.STATUS, t))

                raw_chunk = batched.content_cb() if batched else lambda t: emit((StreamQueueKind.CHUNK, t))

                def on_chunk(c: str) -> None:
                    nonlocal retry_shown
                    if retry_shown:
                        retry_shown = False
                        emit((StreamQueueKind.STATUS, "Finishing..."))
                    raw_chunk(c)

                content_cb = _live_text(turn, on_chunk)
                thinking_cb = _live_text(turn, batched.thinking_cb() if batched else lambda t: emit((StreamQueueKind.THINKING, t)))

                def append_c(c: str) -> None:
                    content_cb(c)
                    last_streamed.append(c)

                if stopped_for_this_send():
                    emit((StreamQueueKind.STOPPED,))
                    return
                with llm_request_lane(status_callback=status_cb, resume_status="Finishing..."):
                    # Same compact-then-view path as _spawn_llm_worker. Final
                    # stream has no tools; still compact when the transcript
                    # is over the tiered threshold.
                    if not _compact_bound_session(
                        session,
                        client,
                        turn,
                        tools=None,
                        max_tokens=max_tokens,
                        stop_checker=stop_checker,
                        force=False,
                        status_cb=status_cb,
                    ):
                        emit((StreamQueueKind.STOPPED,))
                        return
                    client.stream_chat_response(
                        messages_for_llm(session), max_tokens, append_c, thinking_cb,
                        stop_checker=stop_checker,
                        status_callback=status_cb,
                    )
                if stopped_for_this_send():
                    note_stop_partial(turn, "".join(last_streamed))
                    emit((StreamQueueKind.STOPPED,))
                else:
                    emit((StreamQueueKind.FINAL_DONE, "".join(last_streamed)))
            except BlockingWaitStopped:
                emit((StreamQueueKind.STOPPED,))
            except Exception as e:
                log.exception("Final stream failed")
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
            response = data
            tool_calls = response.get("tool_calls") if isinstance(response, dict) else None
            parsed_tool_args = [_parsed_tool_call_args(tc) for tc in tool_calls] if isinstance(tool_calls, list) else []
            return ToolLoopEvent(
                kind=EventKind.STREAM_DONE,
                data={
                    "response": response,
                    "parsed_tool_args": parsed_tool_args,
                    "has_audio": bool(self._panel.audio_wav_path),
                },
            )
        elif kind == StreamQueueKind.NEXT_TOOL:
            return ToolLoopEvent(kind=EventKind.NEXT_TOOL)
        elif kind == StreamQueueKind.TOOL_DONE:
            raw = item if isinstance(item, (tuple, list)) else ()
            s = cast("Sequence[Any]", raw)
            if len(s) < 5:
                log.warning("Tool loop received malformed TOOL_DONE item: %r", item)
                return ToolLoopEvent(kind=EventKind.ERROR, data={"error": "Malformed TOOL_DONE item"})
            mutates = False
            with suppress_disposed("Tool loop event: mutates_document check", logger=log):
                from plugin.main import get_tools as _get_tools_registry

                tool = _get_tools_registry().get(s[2])
                if tool and tool.detects_mutation():
                    mutates = True
            call_id = s[1] if isinstance(s[1], str) and s[1].strip() else ""
            func_name = s[2] if isinstance(s[2], str) and s[2].strip() else "unknown"
            func_args_str = s[3] if isinstance(s[3], str) else "{}"
            result = s[4] if isinstance(s[4], str) else ("" if s[4] is None else str(s[4]))
            func_args = object_dict_or_empty(safe_json_loads(func_args_str) if func_args_str else {})
            result_data = object_dict_or_empty(safe_json_loads(result) if result else {})
            return ToolLoopEvent(
                kind=EventKind.TOOL_RESULT,
                data={
                    "call_id": call_id,
                    "func_name": func_name,
                    "func_args_str": func_args_str,
                    "func_args": func_args,
                    "result": result,
                    "result_data": result_data,
                    "mutates_document": mutates,
                },
            )
        elif kind == StreamQueueKind.FINAL_DONE:
            return ToolLoopEvent(kind=EventKind.FINAL_DONE, data={"content": data})
        elif kind == StreamQueueKind.ERROR:
            return ToolLoopEvent(kind=EventKind.ERROR, data={"error": data})
        return None

    def _execute_effect(self: ToolLoopHost, effect: Any) -> bool:
        """Execute a single pure effect returned by the state machine."""
        return self._panel._tool_loop_interpreter.execute(effect)

    def _apply(self: ToolLoopHost, event: ToolLoopEvent) -> bool:
        tr = next_state(self._sm_state, event)
        self._sm_state = tr.state
        exit_loop = False
        for effect in tr.effects:
            if self._execute_effect(effect):
                exit_loop = True
        return exit_loop

    def _handle_stream_completion(self: ToolLoopHost, item: Any) -> bool:
        # A sentinel after abort is not another tool round.
        live = current_turn(self._panel)
        if isinstance(live, TurnController) and not live.alive:
            return True
        raw_kind = item[0] if isinstance(item, (tuple, list)) else item
        kind = raw_kind if isinstance(raw_kind, StreamQueueKind) else None
        if kind == StreamQueueKind.NEXT_TOOL and self._panel.stop_requested and not self._sm_state.is_stopped:
            # Synchronize stop state into the state machine
            self._sm_state = dataclasses.replace(self._sm_state, is_stopped=True)

        event = self._create_event_from_stream_item(item)
        if not event:
            return False

        return self._apply(event)

    def _handle_stream_stopped(self: ToolLoopHost) -> None:
        # The turn commits the open row, closes tool calls that have no
        # result, and writes the stop line. The FSM only latches Stopped.
        turn = current_turn(self._panel)
        partial = take_stop_partial(turn)
        if isinstance(turn, TurnController):
            turn.close_stopped(self._panel, partial)
        # on_stopped ignores the exit flag from _apply.
        self._apply(ToolLoopEvent(kind=EventKind.STOP_REQUESTED, data={}))

    def _handle_stream_error(self: ToolLoopHost, e: Any) -> bool | None:
        from plugin.scripting.audio_recorder_service import clear_pending_audio_wav

        live = current_turn(self._panel)
        if isinstance(live, TurnController) and not live.alive:
            # Dead turn (e.g. after Stop): nothing will retry, so drop the WAV.
            # Not a try/finally: the overflow respawn below returns True, and a
            # native-audio rejection on that retry still needs the WAV for STT.
            clear_pending_audio_wav(self._panel)
            return None
        # Native-audio rejection retries as text on this drain. The STT retry
        # is defined in this module and the WAV attach stays in audio_recorder_service.

        fallback = try_native_audio_stt_fallback(self._panel, e)
        if fallback is not False:
            return fallback

        # If we reached here, it's either not a modality error or STT is not configured.
        # Drain ERROR items are format_error_payload dicts (see _spawn_llm_worker),
        # not BaseException.
        if isinstance(e, dict):
            err_msg = str(e.get("message") or e.get("code") or e)
            crash_blob = "%s %s" % (err_msg, e)
        elif isinstance(e, BaseException):
            err_msg = format_error_message(e)
            crash_blob = err_msg
        else:
            err_msg = resolve_exception_message(e)
            crash_blob = err_msg

        # Tip: a model that rejects images must not keep them in history, or
        # every later prompt fails too. Mark it text-only and strip the image.
        from plugin.framework.client.errors import is_image_unsupported_error

        if is_image_unsupported_error(crash_blob):
            turn = current_turn(self._panel)
            turn_model = getattr(turn, "text_model", None)
            turn_endpoint = getattr(turn, "endpoint", None)
            if not turn_model:
                from plugin.framework.client.model_fetcher import get_text_model
                turn_model = get_text_model()
            if not turn_endpoint:
                from plugin.framework.config import get_current_endpoint
                turn_endpoint = get_current_endpoint()
            if turn_model:
                from plugin.framework.client.model_fetcher import set_native_vision_support
                set_native_vision_support(turn_model, turn_endpoint, supported=False)

            # What was wrong: Stripping unsupported image modified host.session instead of the turn's session.
            # How it happened: Mode switch or session rebind points host.session to a new chat transcript.
            # Why this fixes it: Use session_for_turn so modifications only apply to the active turn's session.
            turn_session = session_for_turn(self._panel)
            image_msg = _last_user_message_with_image(turn_session)
            if image_msg is not None:
                turn = current_turn(self._panel)
                _strip_user_image(turn_session, image_msg, (turn.active_query_text if turn else "") or "")

        # Prompt-too-large: respawn the worker with force_compact. Never call
        # compact_session on this drain / UI thread (UNO + lane). Process death
        # is not overflow — compact-and-retry on a dead llama-server is worse
        # than today's sentence. Kill switch offs this path too.
        if get_config_bool_safe("chat_compaction_enabled"):
            stop_checker = self._panel.resolve_stop_checker()
            stopped = bool(self._panel.stop_requested or stop_checker())
            live_turn = running_turn(self._panel)
            retry_q = spawn_queue(live_turn) if live_turn is not None else None
            if (
                not stopped
                and retry_q is not None
                and isinstance(live_turn, TurnController)
                and live_turn.active_client is not None
                and not is_process_death_error(crash_blob)
                and is_context_overflow_error(crash_blob)
                and live_turn._overflow_compact_attempts < MAX_OVERFLOW_COMPACTION_ATTEMPTS
                and should_retry_overflow(
                    live_turn._overflow_compact_attempts,
                    live_turn._last_compact_reason,
                    live_turn._last_compact_tokens_before,
                    live_turn._last_compact_tokens_after,
                )
            ):
                live_turn._overflow_compact_attempts += 1
                self._panel._set_status("Compacting conversation...")
                self._spawn_llm_worker(
                    retry_q,
                    live_turn.active_client,
                    live_turn.active_max_tokens,
                    live_turn.active_tools or [],
                    self._sm_state.round_num,
                    query_text=live_turn.active_query_text,
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
        self._panel._append_response(banner)
        # The user row is already stored. Leaving the banner widget-only made
        # a retry append a second user row with a hole where the assistant was.
        # Overflow respawn and audio fallback return before this write.
        persist_assistant_on_turn(self._panel, content=banner.strip())
        self._panel._terminal_status = "Error"
        self._panel._set_status("Error")
        clear_pending_audio_wav(self._panel)
        return None

    def _on_tool_loop_approval_required(self: ToolLoopHost, item: Any) -> None:
        """Main-thread handler: show inline Accept/Reject and unblock the tool worker."""
        query_for_engine = item[1] if len(item) > 1 else ""
        tool_name = item[2] if len(item) > 2 else ""
        event_obj = item[3] if len(item) > 3 else None
        if event_obj is not None:
            self._panel._events.begin_inline_web_approval(query_for_engine, tool_name, event_obj)
        log.info("tool_loop on_approval_required: tool=%s (inline Accept/Change/Reject)", tool_name)

    def _start_tool_calling_async(self: ToolLoopHost, client: "LlmClient", model: Any, max_tokens: int, tools: list[dict[str, Any]], execute_tool_fn: Callable[..., Any], max_tool_rounds: int, query_text: str | None = None) -> None:
        """Tool-calling event loop: single queue, main-thread drain.

        Background threads push messages onto q. The main thread drains via
        ``run_stream_drain_loop``: event-driven slices return to VCL between
        batches; the blocking fallback still pumps with ``pump_ui_idle``.
        """
        log.info("=== Tool-calling loop START (max %d rounds) ===" % max_tool_rounds)
        # Overflow retry counters live on the fresh per-send TurnController.
        self._panel._append_response("\nAI: ")
        self._panel._record_assistant_start = True

        def _get_async_tools() -> frozenset[str]:
            from plugin.main import get_tools as _get_tools_registry

            registry = _get_tools_registry()
            return frozenset(
                tool.name
                for tool in registry.get_tools(filter_doc_type=False, exclude_tiers=())
                if tool.name is not None and getattr(tool, "is_async", lambda: False)()
            )

        async_tool_default = frozenset({"web_research", "image_generate"})
        async_tools = best_effort(_get_async_tools, "Failed to get async tools list, falling back to defaults", default=async_tool_default) or async_tool_default

        self._sm_state = ToolLoopState(round_num=0, pending_tools=[], max_rounds=max_tool_rounds, status="Thinking...", async_tools=async_tools)

        turn = running_turn(self._panel)
        if turn is None:
            return
        try:
            raw_q: StreamQueue = StreamQueue()
            batched = BatchingStreamQueue(raw_q, batch_interval=CHAT_STREAM_BATCH_INTERVAL)
            # The drain attaches the queue. Workers only enqueue on it.
            turn.queue = raw_q
            turn.batcher = batched
            if turn.model is None:
                turn.model = model

            turn.active_client = client
            turn.active_max_tokens = max_tokens
            turn.active_tools = tools
            turn.active_execute_tool_fn = execute_tool_fn
            turn.active_query_text = query_text
            self._panel._tool_loop_interpreter = ToolLoopEffectInterpreter(self._panel)

            # Read config once for web research thinking display
            show_search_thinking = as_bool(get_config("chatbot.show_search_thinking"))

            # Bugfix: toolkit None used to abort a chat that the event-driven drain does
            # not pump; the first LLM worker used to start before the drain was armed.
            toolkit = get_toolkit(self._panel.ctx)

            # Check once whether execute_tool_fn accepts status_callback
            sig = inspect.signature(execute_tool_fn)
            turn.active_supports_status = "status_callback" in sig.parameters or "kwargs" in sig.parameters

            # --- Thinking display state (mirrors run_stream_drain_loop behavior) ---

            self._refresh_active_tools_for_session()

            def _flush_active_batcher() -> None:
                # Stop used to clear this batcher in finally without a last
                # flush, dropping up to one batch interval of text.
                if turn.batcher is not None:
                    turn.batcher.flush()

            run_stream_drain_loop(
                turn.queue,
                toolkit,
                [False],
                self._panel._append_response,
                on_stream_done=self._handle_stream_completion,
                on_stopped=self._handle_stream_stopped,
                on_error=self._handle_stream_error,
                on_status_fn=self._panel._set_status,
                stop_checker=self._panel.resolve_stop_checker(),
                show_search_thinking=show_search_thinking,
                on_approval_required=self._on_tool_loop_approval_required,
                flush_pending=_flush_active_batcher,
                on_drain_started=lambda: self._spawn_llm_worker(
                    spawn_queue(turn),
                    client,
                    turn.active_max_tokens,
                    turn.active_tools or [],
                    self._sm_state.round_num,
                    query_text=turn.active_query_text,
                ),
            )

            def _after_tool_drain() -> None:
                # Blocking drain: this runs before we return. Event-driven
                # drain: the function already returned to VCL; this runs on
                # the terminal slice, still before abort_turn below.
                finalize_sidebar_assistant_response(self._panel, allow_rerender=not self._panel.stop_requested)

            defer_until_drain_done(_after_tool_drain)
        finally:
            def _end_tool_drain() -> None:
                # The drain has finished. Abort so a timer or a late tool cannot
                # enqueue. The outer send drain still holds ``_turn`` for the
                # spoken reply, then drops it.
                abort_turn(self._panel)
                self._panel._tool_loop_interpreter = None
                self._panel.sidebar_state = dataclasses.replace(self._panel.sidebar_state, tool_loop=None)

            defer_until_drain_done(_end_tool_drain)
