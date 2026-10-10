"""SendHandlersMixin: specialized send paths for the chat sidebar.

This mixin is used by SendButtonListener in panel.py and contains
alternate send flows that would otherwise bloat that class:

- Audio transcription fallback
- Direct image generation / img2img (sidebar Image mode)
- External agent backends (Aider, Hermes)
- Web research sub-agent
"""

from __future__ import annotations

import os
import json
import threading
import queue
import logging
from typing import TYPE_CHECKING, Protocol, Any, Callable, TypeVar

from plugin.framework.i18n import _
from plugin.framework.async_stream import StreamQueueKind, defer_until_drain_done, run_async_worker_with_drain
from plugin.framework.blocking_wait import run_blocking_in_thread
from plugin.framework.errors import (
    AgentParsingError,
    NetworkError,
    format_error_payload,
    safe_json_loads,
    suppress_disposed,
)
from plugin.framework.config import get_api_config, get_config, get_config_int_safe
from plugin.framework.config_schema import DEFAULT_IMAGE_BASE_SIZE, as_bool
from plugin.framework.client.errors import format_error_for_display
from plugin.framework.client.llm_client import LlmClient
from plugin.framework.prompts import get_core_directives_for_type
from plugin.chatbot.agent_manual import full_manual
from plugin.framework.queue_executor import capture_send_stop, llm_request_lane
from plugin.acp import get_backend
from plugin.acp.registry import normalize_backend_id
from plugin.chatbot.state_machine import SendHandlerEvent, SendHandlerState, StartEvent, StreamChunkEvent, StreamDoneEvent, ErrorEvent, StopRequestedEvent, next_state, EffectInterpreter, ui_lines_for_handler_error
from plugin.chatbot.tool_loop_actions import (
    TurnController,
    abort_turn,
    current_turn,
    persist_assistant_on_turn,
    running_turn,
)
from plugin.chatbot.dialogs import get_control_text, show_approval_dialog
from plugin.chatbot.config_ui_helpers import update_lru_history
from plugin.framework.tool import ToolContext
from plugin.doc.visual_helpers import selected_graphic_object

log = logging.getLogger(__name__)


def _direct_image_source_arg(model: Any) -> str | None:
    """Return ``'selection'`` when a document graphic is selected, else ``None``.

    Sidebar Image mode calls ``image_generate`` with no LLM. The tool only
    runs img2img and replaces in place when ``source_image='selection'``.
    The old worker always omitted that arg, so an edit prompt with a
    selected image still text-to-image inserted a new graphic.
    """
    try:
        if selected_graphic_object(model) is not None:
            return "selection"
    except Exception:
        log.debug("Direct image: selection probe failed", exc_info=True)
    return None


class _SendWorkerQueue:
    """Worker-facing queue bound to the turn that created it.

    ``put`` goes through the controller. Once that turn is aborted, the
    item is dropped. Image, agent, and web workers that call ``Queue.put``
    on the drain queue still deliver after Stop or a newer send, and the
    drain then keeps a second queue alive to filter them. The worker does
    not assign the queue; the drain attached it.
    """

    _turn: TurnController | None
    raw: "queue.Queue[Any]"

    def __init__(self, turn: TurnController | None, raw: "queue.Queue[Any]") -> None:
        self._turn = turn
        self.raw = raw

    def put(self, item: Any, *_args: Any, **_kwargs: Any) -> None:
        turn = self._turn
        if isinstance(turn, TurnController):
            turn.put(item)


def _send_worker_queues(host: Any) -> tuple["queue.Queue[Any]", _SendWorkerQueue]:
    from plugin.chatbot.tool_loop_actions import begin_send_turn

    # ``_do_send`` starts the turn. A direct handler call with no turn yet
    # starts one before the worker. An aborted turn is not replaced.
    if current_turn(host) is None:
        begin_send_turn(host, "")
    raw: queue.Queue[Any] = queue.Queue()
    turn = current_turn(host)
    if isinstance(turn, TurnController) and turn.alive:
        turn.queue = raw
        turn.batcher = None
    return raw, _SendWorkerQueue(turn, raw)


def _turn_session_or_stop(host: Any) -> Any:
    """The session for this send. None when the turn is already over.

    ``_do_send`` starts the turn before this runs. A direct call with no
    turn yet starts one. An aborted turn is not replaced: that rebind wrote
    the reply onto a session the turn did not start with.
    """
    from plugin.chatbot.tool_loop_actions import begin_send_turn

    if current_turn(host) is None:
        begin_send_turn(host, "")
    turn = running_turn(host)
    if turn is None:
        return None
    return turn.session


def _specialized_tool_error_payload(note: str) -> dict[str, str]:
    """Assistant row for a specialized tool that returned status=error.

    The painted note is the assistant message, same as a stream error.
    Librarian, brainstorm, writing-plan, PPT, deep research, and shallow
    research that paint the error and finish with an empty STREAM_DONE
    leave the user turn unanswered: on_stream_done stores a non-agent row
    only when assistant_content is non-empty, and the FSM still ends Ready.
    The open transcript is not a history write.
    """
    return {"assistant_content": note.strip()}


_ACTIVE_RUN_FLAGS = (
    "_active_run_librarian",
    "_active_run_brainstorming",
    "_active_run_writing_plan",
    "_active_run_ppt_master",
    "_active_run_deep_research",
)


def _reset_active_run_flags(host: Any) -> None:
    """Clear temporary active run markers so stale flags do not leak between sends.

    Reset flags at both the start and end of each run. Deleting them only
    inside _execute_web_research_effect leaves the flag set when an error
    happens before that effect, and the next plain send routes to the
    wrong specialized tool.
    """
    for flag in _ACTIVE_RUN_FLAGS:
        setattr(host, flag, False)


if TYPE_CHECKING:
    from plugin.chatbot.panel import ChatSession


def _agent_backend_label(adapter: Any, backend_id: str) -> str:
    """Human-readable backend name for errors (ACP backends implement get_display_name())."""
    getter = getattr(adapter, "get_display_name", None)
    if callable(getter):
        try:
            return str(getter())
        except NotImplementedError:
            pass
    return getattr(adapter, "display_name", backend_id)


class SendHandlerHost(Protocol):
    ctx: Any
    client: "LlmClient | None"

    @property
    def stop_requested(self) -> bool: ...

    def resolve_stop_checker(self) -> Callable[[], bool]: ...
    _in_librarian_mode: bool
    _librarian_suggested_user_name: str | None
    _in_brainstorming_mode: bool
    _brainstorming_topic: str
    _in_writing_plan_mode: bool
    _writing_plan_topic: str
    _in_ppt_master_mode: bool
    _active_run_librarian: bool
    _active_run_brainstorming: bool
    _active_run_writing_plan: bool
    _active_run_ppt_master: bool
    _active_run_deep_research: bool
    session: "ChatSession"
    response_control: Any
    status_control: Any
    image_model_selector: Any
    aspect_ratio_selector: Any
    base_size_input: Any
    frame: Any
    audio_wav_path: str | None
    _terminal_status: str
    _stt_inflight: bool
    _stt_kill: Any
    _current_agent_backend: Any
    _turn: Any

    def _set_status(self, text: str) -> None: ...
    def _append_response(self, text: str, is_thinking: bool = False, role: str = "assistant") -> None: ...
    def _get_doc_type_str(self, model: Any) -> str: ...
    def begin_inline_web_approval(self, query: str, tool: str, event: Any) -> None: ...
    def rerender_rich_text_session(self) -> bool: ...
    _record_assistant_start: bool
    def _run_unified_worker_drain_loop(
        self, q: "queue.Queue[Any]", worker_fn: Callable[[], None], current_state: "SendHandlerState", interpreter: "EffectInterpreter", show_thinking: bool = True, on_stopped_callback: Callable[[], None] | None = None, on_approval_callback: Callable[[Any], None] | None = None
    ) -> None: ...
    def _get_mcp_url(self) -> str | None: ...
    def _do_send_direct_image(self, query_text: str, model: Any) -> None: ...
    def _do_send_via_agent_backend(self, query_text: str, model: Any, doc_type_str: str) -> None: ...
    def on_librarian_session_finished(self) -> None: ...
    def on_brainstorming_session_finished(self, spec_saved: bool = False) -> None: ...
    def on_writing_plan_session_finished(self) -> None: ...
    def on_ppt_master_session_finished(self, exported: bool = False) -> None: ...
    def _run_librarian(self, query_text: str, model: Any) -> None: ...
    def _run_brainstorming(self, query_text: str, model: Any) -> None: ...
    def _run_writing_plan(self, query_text: str, model: Any) -> None: ...
    def _run_ppt_master(self, query_text: str, model: Any) -> None: ...
    def _run_web_research(self, query_text: str, model: Any, is_deep_research: bool = False) -> None: ...


class TypedEvent(Protocol):
    approved: bool
    query_override: str | None

    def wait(self, timeout: float | None = None) -> bool: ...
    def set(self) -> None: ...
    def is_set(self) -> bool: ...


T = TypeVar("T", bound="SendHandlersMixin")


class SendHandlersMixin:
    # Defaults satisfy basedpyright reportUninitializedInstanceVariable; panel __init__ overwrites.
    client: LlmClient | None = None
    audio_wav_path: str | None = None
    _terminal_status: str = "Ready"
    _stt_inflight: bool = False
    _stt_kill: Any = None
    _current_agent_backend: Any = None
    _librarian_suggested_user_name: str | None = None
    _in_librarian_mode: bool = False
    _in_brainstorming_mode: bool = False
    _in_writing_plan_mode: bool = False
    _in_ppt_master_mode: bool = False
    _active_run_librarian: bool = False
    _active_run_brainstorming: bool = False
    _active_run_writing_plan: bool = False
    _active_run_ppt_master: bool = False
    _active_run_deep_research: bool = False
    _turn: Any = None

    def _transcribe_audio(self: SendHandlerHost, wav_path: str, stt_model: str) -> str:
        """Transcribe audio synchronously using event pumping on the main thread.

        ``capture_send_stop`` freezes the scope from this send, and the
        checker is read once more after the transcript returns. Without
        that checker, ``run_blocking_in_thread`` pumps the UI, so Stop can
        cancel the send scope and a nested send can replace
        ``_send_cancellation``, while Whisper's ``subprocess.run`` still
        waits out 900s and the transcript is posted anyway. Stop does
        not kill local Whisper or close the endpoint socket: transcription
        finishes and ``_do_send`` puts the words back in the Ask box instead
        of sending them (see the StopSendEffect invariant in ``panel``).
        Only sidebar teardown kills the child, through ``_stt_kill``. A
        re-entrant call returns without deleting the first WAV or clearing
        the first client's stop latch. The STT client is not registered with
        the send scope for the same reason.
        """
        from plugin.audio.stt_service import SttStopped, status_for_transcription, terminate_stt_process, transcribe
        from plugin.framework.queue_executor import post_to_main_thread

        # getattr: a duck-typed host (the smol _do_send double) has no field
        # until this call sets it. Missing means nothing is in flight.
        if getattr(self, "_stt_inflight", False):
            log.info("Ignoring re-entrant STT; the in-flight transcription keeps the first Stop")
            return ""

        self._stt_inflight = True
        try:
            # Spawn-time scope. Do not call resolve_stop_checker inside the
            # worker: Stop's drain clears the field and the next send replaces it.
            cancel_scope, stop_checker = capture_send_stop(self)
            if stop_checker():
                self._terminal_status = "Stopped"
                return ""

            # Always create a fresh client to avoid reusing previous STT endpoint/key
            api_config = get_api_config()
            local_client = LlmClient(api_config, self.ctx, cancellation_scope=cancel_scope, register_with_send=False)

            cl = local_client
            assert cl is not None
            if cancel_scope is not None and not cancel_scope.is_cancelled():
                clearer = getattr(cl, "clear_stop", None)
                if callable(clearer):
                    clearer()

            transcribing = status_for_transcription()
            self._set_status(transcribing)
            self._append_response("\n[" + transcribing + "]\n")

            def on_status(message: str) -> None:
                # Worker thread: the status control is UNO. run_blocking_in_thread
                # pumps processEventsToIdle, which runs this posted callback.
                post_to_main_thread(self._set_status, message)

            def _on_spawn(proc: Any) -> None:
                def _kill() -> None:
                    terminate_stt_process(proc)

                # Sidebar teardown (_kill_inflight_stt) calls this. Stop does
                # not: the transcript is kept for the Ask box.
                self._stt_kill = _kill

            def _call() -> str:
                return transcribe(
                    wav_path,
                    client=cl,
                    model=stt_model,
                    on_status=on_status,
                    on_spawn=_on_spawn,
                )

            try:
                # Do not pass stop_checker here. That raises BlockingWaitStopped
                # and returns before the worker reaps the child, and Stop is
                # meant to keep the transcript, so the worker runs to the end.
                transcript_text = run_blocking_in_thread(self.ctx, _call)
            except SttStopped:
                log.info("Speech-to-text stopped")
                self._terminal_status = "Stopped"
                return ""
            except Exception as e:
                log.exception("Transcription error in _transcribe_audio")
                self._append_response("\n" + _("[Transcription error: {0}]").format(str(e)) + "\n")
                raise e
            if stop_checker():
                log.info("Speech-to-text finished after Stop; preserving transcript for query box")
                self._terminal_status = "Stopped"
                return transcript_text
            return transcript_text
        finally:
            self._stt_inflight = False
            self._stt_kill = None
            try:
                os.remove(wav_path)
            except Exception:
                pass
            self.audio_wav_path = None

    def _run_unified_worker_drain_loop(
        self: SendHandlerHost, q: "queue.Queue[Any]", worker_fn: Callable[[], None], current_state: "SendHandlerState", interpreter: "EffectInterpreter", show_thinking: bool = True, on_stopped_callback: Callable[[], None] | None = None, on_approval_callback: Callable[[Any], None] | None = None
    ) -> None:


        def dispatch_event(event: SendHandlerEvent) -> None:
            nonlocal current_state
            step = next_state(current_state, event)
            current_state = step.state
            interpreter.current_state = current_state
            for eff in step.effects:
                interpreter.interpret(eff)

        agent_parts: list[str] = []

        def apply_chunk(chunk_text: str, is_thinking: bool = False) -> None:
            if is_thinking and not show_thinking:
                return
            # External-agent text is persisted once on STREAM_DONE / stop.
            # Writing each chunk from the worker raced the UI drain.
            if current_state.handler_type == "agent" and not is_thinking and chunk_text:
                agent_parts.append(chunk_text)
            dispatch_event(StreamChunkEvent(chunk_text, is_thinking))

        def _finish_specialized_session(payload: dict[str, Any]) -> None:
            # Brainstorm / writing-plan / PPT used to call these on the send
            # worker. They touch the mode combo (UNO). Librarian already
            # rides STREAM_DONE; these three do the same.
            #
            # When in_*_mode is False, run the same session-finished
            # callback as success. That resets the combo and applies Chat
            # mode. Error branches set in_*_mode=False in the STREAM_DONE
            # payload; looking only for success keys
            # (librarian_switch_to_chat, brainstorming_finished) assigns the
            # host attributes and leaves the combo on the old mode.
            if payload.get("librarian_switch_to_chat") or payload.get("in_librarian_mode") is False:
                finished_cb = getattr(self, "on_librarian_session_finished", None)
                if callable(finished_cb):
                    finished_cb()
                else:
                    self._in_librarian_mode = False
            if payload.get("brainstorming_finished") or payload.get("in_brainstorming_mode") is False:
                spec_saved = bool(payload.get("spec_saved", False))
                finished_cb = getattr(self, "on_brainstorming_session_finished", None)
                if callable(finished_cb):
                    finished_cb(spec_saved=spec_saved)
                else:
                    self._in_brainstorming_mode = False
            if payload.get("writing_plan_finished") or payload.get("in_writing_plan_mode") is False:
                finished_cb = getattr(self, "on_writing_plan_session_finished", None)
                if callable(finished_cb):
                    finished_cb()
                else:
                    self._in_writing_plan_mode = False
            if payload.get("ppt_master_finished") or payload.get("in_ppt_master_mode") is False:
                finished_cb = getattr(self, "on_ppt_master_session_finished", None)
                if callable(finished_cb):
                    finished_cb(exported=bool(payload.get("exported", False)))
                else:
                    self._in_ppt_master_mode = False


            if "in_librarian_mode" in payload:
                self._in_librarian_mode = payload["in_librarian_mode"]
            if "in_brainstorming_mode" in payload:
                self._in_brainstorming_mode = payload["in_brainstorming_mode"]
            if "in_writing_plan_mode" in payload:
                self._in_writing_plan_mode = payload["in_writing_plan_mode"]
            if "in_ppt_master_mode" in payload:
                self._in_ppt_master_mode = payload["in_ppt_master_mode"]

        def on_stream_done(item: Any) -> None:
            # Stop or a new send already aborted this turn. The worker
            # wrapper may still post STREAM_DONE so the drain unblocks.
            # That sentinel is not a successful answer.
            live = current_turn(self)
            if isinstance(live, TurnController) and not live.alive:
                return
            payload = item[1] if isinstance(item, tuple) and len(item) > 1 else item
            if isinstance(payload, dict):
                # Store the answer before a mode handoff aborts this turn.
                # Web, librarian, brainstorm, writing, PPT, and deep research
                # used to persist on the worker. That write raced Clear.
                if current_state.handler_type != "agent":
                    answer = payload.get("assistant_content")
                    if isinstance(answer, str) and answer:
                        persist_assistant_on_turn(self, content=answer)
                _finish_specialized_session(payload)
            if current_state.handler_type == "agent":
                text = "".join(agent_parts).strip()
                if text:
                    persist_assistant_on_turn(self, content=text)
            dispatch_event(StreamDoneEvent(payload))

        def on_stopped() -> None:
            # The turn commits the open row and writes the stop line.
            # Agent still prefers the non-thinking chunks it accumulated.
            # Only agent Stop used to store a row. Web and image pass no
            # on_stopped_callback, and finalize skips rerender after Stop,
            # so the painted partial never lands in session.messages.
            turn = current_turn(self)
            partial = "".join(agent_parts).strip() if current_state.handler_type == "agent" else None
            if isinstance(turn, TurnController):
                turn.close_stopped(self, partial)
            if on_stopped_callback:
                on_stopped_callback()
            dispatch_event(StopRequestedEvent())

        def on_error(e: Exception) -> None:
            live = current_turn(self)
            if isinstance(live, TurnController) and not live.alive:
                return
            dispatch_event(ErrorEvent(e))
            # Same write for web, agent, and image, using the lines
            # handle_error already shows. The tool loop stores that banner
            # with persist_assistant_on_turn. Painting the banner and storing
            # the user row without this write leaves the next send a hole
            # where the assistant row should be.
            err_msg = format_error_for_display(e)
            append_text = ui_lines_for_handler_error(current_state.handler_type, err_msg)[1]
            persist_assistant_on_turn(self, content=append_text.strip())

        def worker_wrapper(worker_q: queue.Queue[Any]) -> None:
            # The worker_fn in this mixin expects to put things directly into q.
            # We already have q, so we just run worker_fn.
            # However, run_async_worker_with_drain creates its own q.
            # We can just ignore the worker_q and use the outer q.
            # BUT, it's better to refactor the workers to use the passed queue.
            worker_fn()

        turn = current_turn(self)
        if isinstance(turn, TurnController) and turn.queue is None:
            # The drain owns the queue. The worker wrapper only calls put.
            turn.queue = q
        try:
            run_async_worker_with_drain(
                self.ctx,
                worker_wrapper,
                apply_chunk,
                on_stream_done,
                on_error,
                on_status_fn=self._set_status,
                stop_checker=self.resolve_stop_checker(),
                on_stopped_fn=on_stopped,
                name="chatbot-send-handler",
                q=q,
                on_approval_required=on_approval_callback,
            )
        finally:
            def _abort_after_drain() -> None:
                # Workers that outlive the drain must not enqueue. The send
                # drain drops ``_turn`` after it has read the reply to speak.
                # Event-driven drains return before the stream ends; aborting
                # here on the way out would drop the turn while chunks are
                # still landing.
                abort_turn(self)

            defer_until_drain_done(_abort_after_drain)

    def _do_send_direct_image(self: SendHandlerHost, query_text: str, model: Any) -> None:
        interpreter = EffectInterpreter(self)
        current_state = SendHandlerState(handler_type="image", status="ready")

        turn_session = _turn_session_or_stop(self)
        # Store the user row once here, before the append, like web
        # research; the fold then sees it and only paints. The StartEvent
        # user append already folds the prompt into the session, and the
        # spawn effect calling add_user_message again paints 'You: <prompt>'
        # twice. An aborted turn (None) must not spawn the image worker.
        if turn_session is None:
            return
        turn_session.add_user_message(query_text)

        # 1. State machine transition: start
        step = next_state(current_state, StartEvent(query_text, model, "image"))
        current_state = step.state
        interpreter.current_state = step.state
        for effect in step.effects:
            interpreter.interpret(effect)

    def _execute_direct_image_effect(self: SendHandlerHost, query_text: str, model: Any, current_state: "SendHandlerState", interpreter: "EffectInterpreter") -> None:
        turn_session = _turn_session_or_stop(self)
        if turn_session is None:
            return
        # The user row was stored by _do_send_direct_image before StartEvent.
        drain_q, q = _send_worker_queues(self)
        # Probe on the UI thread. The tool re-reads the selection when it
        # executes; this flag only decides whether to request img2img.
        source_image = _direct_image_source_arg(model)
        # getText on these controls, and update_lru_history, used to run inside
        # the image worker. getText is UNO; the LRU write emits config:changed
        # and refreshes sidebar controls, which must stay on the UI thread.
        aspect_ratio_str = "Square"
        if self.aspect_ratio_selector and hasattr(self.aspect_ratio_selector, "getText"):
            aspect_ratio_str = self.aspect_ratio_selector.getText()
        from plugin.chatbot.settings_dialog import canonical_aspect_label

        aspect_ratio_str = canonical_aspect_label(str(aspect_ratio_str or ""))
        aspect_map = {"Square": "square", "Landscape (16:9)": "landscape_16_9", "Portrait (9:16)": "portrait_9_16", "Landscape (3:2)": "landscape_3_2", "Portrait (2:3)": "portrait_2_3"}
        mapped_aspect = aspect_map.get(aspect_ratio_str, "square")
        image_model_text = ""
        if self.image_model_selector and hasattr(self.image_model_selector, "getText"):
            image_model_text = self.image_model_selector.getText()
        base_size_val: int | str = DEFAULT_IMAGE_BASE_SIZE
        if self.base_size_input:
            if hasattr(self.base_size_input, "getText"):
                base_size_val = self.base_size_input.getText()
            elif hasattr(self.base_size_input.getModel(), "Text"):
                base_size_val = get_control_text(self.base_size_input)
        try:
            base_size_int = int(base_size_val)
        except (ValueError, TypeError):
            base_size_int = DEFAULT_IMAGE_BASE_SIZE
        with suppress_disposed("LRU update", logger=log, exc_info=True):
            update_lru_history(base_size_int, "image_base_size_lru", "")

        # Spawn-time scope. The body must not call resolve_stop_checker or
        # read _send_cancellation: Stop clears the field and the next send replaces it.
        cancel_scope, stop_checker = capture_send_stop(self)

        def run_direct_image() -> None:
            try:
                from plugin.main import get_tools

                tctx = ToolContext(doc=model, ctx=self.ctx, stop_checker=stop_checker, doc_type=getattr(self, "cached_doc_type", None) or "writer", services=get_tools()._services, caller="chat", status_callback=lambda t: q.put((StreamQueueKind.STATUS, t)), send_cancellation=cancel_scope, uno_services_supported=getattr(self, "cached_uno_services", None))

                # generate_image is async; UNO is marshalled inside the tool (worker runs HTTP).
                image_args: dict[str, Any] = {"prompt": query_text, "aspect_ratio": mapped_aspect, "base_size": base_size_int, "image_model": image_model_text}
                if source_image:
                    image_args["source_image"] = source_image
                res = get_tools().execute("image_generate", tctx, bypass_thread_guard=False, **image_args)
                if isinstance(res, dict) and res.get("status") == "error":
                    # An ordinary user Stop is not a failure; keep it out of ERROR.
                    if stop_checker():
                        log.debug("generate_image (direct) stopped by user: %s", res.get("message"))
                    else:
                        log.error("generate_image (direct) failed: %s details=%s", res.get("message"), res.get("details"))
                result = json.dumps(res, default=str) if isinstance(res, dict) else str(res)
                data = safe_json_loads(result, default={})
                if isinstance(data, dict):
                    note = data.get("message", data.get("status", "done"))
                else:
                    log.error("Failed to parse generate_image result in _do_send_direct_image")
                    note = "done"
                # This note is the assistant message. Success (and a tool
                # status=error) that puts STREAM_DONE {} never reaches
                # history: on_stream_done stores a non-agent row only when
                # assistant_content is set. Stop and raised errors already
                # store a row.
                note_line = "[image_generate: %s]\n" % note
                q.put((StreamQueueKind.CHUNK, note_line))
                q.put((StreamQueueKind.STREAM_DONE, {"assistant_content": note_line.strip()}))
            except Exception as e:
                doc_type = getattr(self, "cached_doc_type", None) or "unknown"
                if stop_checker():
                    log.debug("Direct image path cancelled by Stop [doc: %s]: %s", doc_type, e)
                else:
                    log.exception("Direct image path failed in _do_send_direct_image [doc: %s]", doc_type)

                q.put((StreamQueueKind.ERROR, format_error_payload(e)))

        self._run_unified_worker_drain_loop(drain_q, run_direct_image, current_state, interpreter)

        def _image_ready() -> None:
            # Runs when the drain is really done. The event-driven drain returns
            # to the VCL loop at once, so code placed after the drain call would
            # run before the queued text and terminal slice are flushed.
            #
            # The stop line must follow the final flush, so finalize runs
            # inside this deferred callback. Chat and web call finalize after
            # their drain, which draws the stop line; the image path has to
            # do the same or Image-mode Stop shows no '[Stopped by user]'
            # until a later repaint.
            if self.stop_requested:
                from plugin.chatbot.rich_text import finalize_sidebar_assistant_response

                finalize_sidebar_assistant_response(self, allow_rerender=False)
            # Stop already stored "Stopped" via CompleteJobEffect. Forcing Ready
            # here made image Stop look like a normal finish. The agent path
            # below keeps both Error and Stopped.
            if self._terminal_status not in ("Error", "Stopped"):
                self._terminal_status = "Ready"

        defer_until_drain_done(_image_ready)

    def _do_send_via_agent_backend(self: SendHandlerHost, query_text: str, model: Any, doc_type_str: str) -> None:
        """Send via external agent backend (Aider, Hermes). No fallback to built-in on failure."""
        interpreter = EffectInterpreter(self)
        current_state = SendHandlerState(handler_type="agent", status="ready")

        # User row is stored after the backend exists. Writing it here left
        # a user turn with no assistant row when the adapter was missing.

        # 1. State machine transition: start
        step = next_state(current_state, StartEvent(query_text, model, doc_type_str))
        current_state = step.state
        interpreter.current_state = current_state
        for effect in step.effects:
            interpreter.interpret(effect)

    def _execute_agent_backend_effect(self: SendHandlerHost, query_text: str, model: Any, doc_type_str: str, current_state: "SendHandlerState", interpreter: "EffectInterpreter") -> None:


        document_url = ""
        with suppress_disposed("get document URL for agent backend", logger=log):
            if model and hasattr(model, "getURL"):
                document_url = str(model.getURL() or "")

        turn_session = _turn_session_or_stop(self)
        if turn_session is None:
            return

        def _handle_early_error(exception_instance: Exception) -> None:
            self._terminal_status = "Error"
            self._set_status(_("Error"))

            # Step 1: dispatch ErrorEvent to current state machine (this will persist the banner)
            step = next_state(current_state, ErrorEvent(exception_instance))
            interpreter.current_state = step.state
            for eff in step.effects:
                interpreter.interpret(eff)

            # Step 2: Ensure turn is closed out properly, just as _do_send_direct_image does
            defer_until_drain_done(lambda: abort_turn(self))

        try:
            turn_session.refresh_document_context(model, self.ctx)
            doc_context = turn_session.document_context
        except Exception as e:
            from plugin.framework.errors import is_disposed_exception

            if is_disposed_exception(e):
                log.debug("Failed to build document context for agent backend (likely disposed): %s", e)
            else:
                log.exception("Failed to build document context for agent backend")
            _handle_early_error(Exception(_("[Document context error: {0}]").format(str(e))))
            return



        backend_id = normalize_backend_id(get_config("agent_backend.backend_id"))
        adapter = get_backend(backend_id, ctx=self.ctx)
        if not adapter:


            _handle_early_error(ValueError(_("[Agent backend '{0}' not found.]").format(backend_id)))
            return
        if not adapter.is_available(self.ctx):


            _handle_early_error(ValueError(_("[Agent backend '{0}' is not available. Check Settings (path, install).]").format(_agent_backend_label(adapter, backend_id))))
            return

        turn_session.add_user_message(query_text)
        self._append_response(query_text, role="user")

        drain_q, q = _send_worker_queues(self)
        self._current_agent_backend = adapter
        # Spawn-time scope. run_agent must not call resolve_stop_checker:
        # Stop clears the field and the next send replaces it.
        cancel_scope, stop_checker = capture_send_stop(self)
        if cancel_scope is not None and hasattr(adapter, "stop"):
            cancel_scope.register_on_cancel(adapter.stop)

        # String-only: classifying the live model hits get_document_type (UNO).
        # run_agent is chatbot-send-handler; do not classify the document here.
        core_dirs = get_core_directives_for_type(doc_type_str or "writer")

        def run_agent() -> None:
            try:


                # Lean system prompt for external agents: instructions + MCP connection info
                mcp_url = self._get_mcp_url()

                # Check if MCP is enabled and running; if so, tell the agent about it.
                # Gate advertisement on the MCP server actually running.
                # After "Stop MCP Server", config (mcp.mcp_enabled) still
                # says enabled and would tell the agent the dead endpoint
                # is live.
                mcp_instructions = ""
                from plugin.mcp import is_mcp_server_running

                if mcp_url and is_mcp_server_running():
                    mcp_instructions = (
                        f"\n\n[MCP SERVER AVAILABLE]\nA Model Context Protocol (MCP) server is running at: {mcp_url}\nYou can discover and use all LibreOffice tools (Writer, Calc, Draw) via this server.\nTarget the current document by passing the 'X-Document-URL' header: {document_url}\n"
                    )

                # Inject the FULL shared manual: the same prompt pieces (constants) that feed the
                # sidebar's hybrid prompt and get_guidance's topics, concatenated by agent_manual
                # WITH the MCP extras (this backend talks to the HTTP server, so e.g. the 429
                # concurrency contract applies here, unlike the in-process sidebar).
                # full_manual(doc_type_str) is string-only — do not classify the document here.
                lean_system_prompt = f"{core_dirs}\n\n{full_manual(doc_type_str or 'writer')}\n\nYou are currently interacting with a LibreOffice document.\n{mcp_instructions}\nPlease proceed with the user's request."

                # Add optional instructions from settings
                extra = str(get_config("additional_instructions") or "").strip()
                if extra:
                    lean_system_prompt += "\n\n" + extra

                def status_cb(t: str) -> None:
                    q.put((StreamQueueKind.STATUS, t))
                with llm_request_lane(status_callback=status_cb, resume_status=_("Thinking...")):
                    adapter.send(queue=q, user_message=query_text, document_context=doc_context, document_url=document_url, system_prompt=lean_system_prompt, mcp_url=mcp_url, stop_checker=stop_checker)
            except Exception as e:
                from plugin.framework.async_stream import BlockingWaitStopped
                if isinstance(e, BlockingWaitStopped):
                    q.put((StreamQueueKind.STOPPED,))
                else:
                    log.exception("Agent backend ERROR in _do_send_via_agent_backend [backend: %s, doc: %s]", backend_id, doc_type_str)
                    q.put((StreamQueueKind.ERROR, format_error_payload(e)))
            finally:
                # Clear _current_agent_backend only if it is still the
                # backend this worker set. A newer send can install its own
                # backend before this worker finishes; an unconditional
                # clear in finally clobbers that one.
                if getattr(self, "_current_agent_backend", None) is adapter:
                    self._current_agent_backend = None

        def on_approval_required(item: Any) -> None:
            # item = ("approval_required", description, tool_name, args, request_id)


            description = item[1] if len(item) > 1 else ""
            tool_name = item[2] if len(item) > 2 else ""
            request_id = item[4] if len(item) > 4 else None

            # Option to auto-approve tools from external agents
            try:
                prompt_for_permission = as_bool(get_config("agent_backend.prompt_for_permission"))
            except Exception:
                prompt_for_permission = True

            if self.stop_requested:
                approved = False
            elif not prompt_for_permission:
                approved = True
            else:
                try:
                    approved = show_approval_dialog(self.ctx, description, tool_name, parent_frame=getattr(self, "frame", None))
                except Exception as e:
                    log.error("Error showing approval dialog: %s", e)
                    approved = False

            if request_id is not None and hasattr(adapter, "submit_approval"):
                try:
                    adapter.submit_approval(request_id, approved)
                except Exception as e:
                    if isinstance(e, NetworkError):
                        log.debug("NetworkError submitting agent backend approval: %s", e)
                    else:
                        log.debug("Error submitting agent backend approval: %s", e)

        self._run_unified_worker_drain_loop(drain_q, run_agent, current_state, interpreter, on_approval_callback=on_approval_required)

        def _agent_ready() -> None:
            if self._terminal_status not in ("Error", "Stopped"):
                self._terminal_status = "Ready"
            if self._current_agent_backend is adapter:
                self._current_agent_backend = None

        defer_until_drain_done(_agent_ready)

    def _run_librarian(self: SendHandlerHost, query_text: str, model: Any) -> None:
        """Run the librarian onboarding tool via the sub-agent and stream its result into the response area."""
        from plugin.chatbot.librarian import get_suggested_user_name

        _reset_active_run_flags(self)
        interpreter = EffectInterpreter(self)
        current_state = SendHandlerState(handler_type="web", status="ready")  # We can reuse 'web' handler_type or create a new one, but for simplicity, 'web' will dispatch StartEvent

        # Resolve on the UI thread so UNO UserProfile reads stay on the main thread.
        self._librarian_suggested_user_name = get_suggested_user_name(self.ctx)

        turn_session = _turn_session_or_stop(self)
        if turn_session is None:
            return
        self._in_librarian_mode = True
        turn_session.add_user_message(query_text)

        # 1. State machine transition: start
        step = next_state(current_state, StartEvent(query_text, model, "web"))
        current_state = step.state
        interpreter.current_state = current_state

        # Manually set the run_librarian flag to distinguish from web research in effect execution
        setattr(self, "_active_run_librarian", True)

        try:
            for effect in step.effects:
                interpreter.interpret(effect)
        finally:
            _reset_active_run_flags(self)

    def _run_brainstorming(self: SendHandlerHost, query_text: str, model: Any) -> None:
        """Run the brainstorming sub-agent and stream its result into the response area."""
        _reset_active_run_flags(self)
        interpreter = EffectInterpreter(self)
        current_state = SendHandlerState(handler_type="web", status="ready")

        turn_session = _turn_session_or_stop(self)
        if turn_session is None:
            return
        self._in_brainstorming_mode = True
        turn_session.add_user_message(query_text)

        step = next_state(current_state, StartEvent(query_text, model, "web"))
        current_state = step.state
        interpreter.current_state = current_state

        setattr(self, "_active_run_brainstorming", True)

        try:
            for effect in step.effects:
                interpreter.interpret(effect)
        finally:
            _reset_active_run_flags(self)

    def _run_writing_plan(self: SendHandlerHost, query_text: str, model: Any) -> None:
        """Run the writing plan sub-agent and stream its result into the response area."""
        _reset_active_run_flags(self)
        interpreter = EffectInterpreter(self)
        current_state = SendHandlerState(handler_type="web", status="ready")

        turn_session = _turn_session_or_stop(self)
        if turn_session is None:
            return
        self._in_writing_plan_mode = True
        turn_session.add_user_message(query_text)

        step = next_state(current_state, StartEvent(query_text, model, "web"))
        current_state = step.state
        interpreter.current_state = current_state

        setattr(self, "_active_run_writing_plan", True)

        try:
            for effect in step.effects:
                interpreter.interpret(effect)
        finally:
            _reset_active_run_flags(self)

    def _run_ppt_master(self: SendHandlerHost, query_text: str, model: Any) -> None:
        """Run the PPT-Master sub-agent (Impress/Draw sidebar mode)."""
        _reset_active_run_flags(self)
        interpreter = EffectInterpreter(self)
        current_state = SendHandlerState(handler_type="web", status="ready")

        turn_session = _turn_session_or_stop(self)
        if turn_session is None:
            return
        self._in_ppt_master_mode = True
        turn_session.add_user_message(query_text)

        step = next_state(current_state, StartEvent(query_text, model, "web"))
        current_state = step.state
        interpreter.current_state = current_state

        setattr(self, "_active_run_ppt_master", True)

        try:
            for effect in step.effects:
                interpreter.interpret(effect)
        finally:
            _reset_active_run_flags(self)

    def _run_web_research(self: SendHandlerHost, query_text: str, model: Any, is_deep_research: bool = False) -> None:
        """Run the web_research tool via the sub-agent and stream its result into the response area."""
        _reset_active_run_flags(self)
        interpreter = EffectInterpreter(self)
        current_state = SendHandlerState(handler_type="web", status="ready")

        turn_session = _turn_session_or_stop(self)
        if turn_session is None:
            return

        if is_deep_research:
            setattr(self, "_active_run_deep_research", True)

        turn_session.add_user_message(query_text)

        # 1. State machine transition: start
        step = next_state(current_state, StartEvent(query_text, model, "web"))
        current_state = step.state
        interpreter.current_state = current_state
        try:
            for effect in step.effects:
                interpreter.interpret(effect)
        finally:
            _reset_active_run_flags(self)

    def _run_deep_web_research(self: SendHandlerHost, query_text: str, model: Any) -> None:
        """Run Deep Research sidebar session (sub-agent with apply_document_content)."""
        self._run_web_research(query_text, model, is_deep_research=True)

    def _execute_web_research_effect(self: SendHandlerHost, query_text: str, model: Any, current_state: "SendHandlerState", interpreter: "EffectInterpreter") -> None:
        from plugin.main import get_tools
        is_librarian = bool(getattr(self, "_active_run_librarian", False))
        is_brainstorming = bool(getattr(self, "_active_run_brainstorming", False))
        is_writing_plan = bool(getattr(self, "_active_run_writing_plan", False))
        is_ppt_master = bool(getattr(self, "_active_run_ppt_master", False))
        is_deep_research = bool(getattr(self, "_active_run_deep_research", False))
        _reset_active_run_flags(self)



        drain_q, q = _send_worker_queues(self)
        # Read show_thinking before spawning the thread so apply_chunk can use it
        try:


            show_thinking = as_bool(get_config("chatbot.show_search_thinking"))
        except (ValueError, TypeError) as e:
            log.debug("Failed to read 'chatbot.show_search_thinking' from config: %s", e)
            show_thinking = False



        from plugin.chatbot.web_research_chat import format_sub_agent_conversation_history

        history_text = format_sub_agent_conversation_history(self.session, current_query=query_text)
        # Spawn-time scope. run_search must not call resolve_stop_checker or
        # read _send_cancellation: Stop clears the field and the next send replaces it.
        cancel_scope, stop_checker = capture_send_stop(self)

        def run_search() -> None:
            doc_type = getattr(self, "cached_doc_type", None) or "writer"
            try:
                # If librarian mode, clear active_run_librarian and run librarian

                def status_cb(msg: str) -> None:
                    q.put((StreamQueueKind.STATUS, msg))

                # Always push thinking to the queue so the drain stays fed.
                # Display is controlled by show_thinking in apply_chunk below.
                def thinking_cb(msg: str) -> None:
                    q.put((StreamQueueKind.THINKING, msg))

                def chat_append_cb(text: str) -> None:
                    q.put((StreamQueueKind.CHUNK, text))

                def approval_cb(query_for_engine: str, tool_name: str, args: Any) -> Any:


                    from plugin.framework.queue_executor import wait_for_approval

                    event = threading.Event()
                    # Use setattr/getattr to avoid static attribute errors on Event
                    setattr(event, "approved", False)
                    setattr(event, "query_override", None)
                    q.put((StreamQueueKind.APPROVAL_REQUIRED, query_for_engine, tool_name, event))
                    # event.wait() ignored Stop. Sidebar close latches the
                    # checker and never sets the event, so this worker parked.
                    if not wait_for_approval(event, stop_checker):
                        q.put((StreamQueueKind.STOPPED,))
                        return (False, None)
                    if not getattr(event, "approved", False):
                        # If the user rejects the search query, do not let the LLM
                        # keep going without the data it requested. Instead, immediately
                        # halt the entire tool call loop, acting exactly as if the
                        # user clicked the explicit 'Stop' button in the UI.
                        q.put((StreamQueueKind.STOPPED,))
                    return (bool(getattr(event, "approved", False)), getattr(event, "query_override", None))

                # run_search runs on the chatbot-send-handler worker;
                # get_ctx() is main-thread-only. Panel bootstrap ctx matches
                # tool_loop.execute_fn (async tools marshal UNO reads themselves).
                tctx = ToolContext(doc=model, ctx=self.ctx, stop_checker=stop_checker, doc_type=doc_type, services=get_tools()._services, caller="chat", status_callback=status_cb, append_thinking_callback=thinking_cb, approval_callback=approval_cb, chat_append_callback=chat_append_cb, send_cancellation=cancel_scope, uno_services_supported=getattr(self, "cached_uno_services", None))

                if is_librarian:
                    res = get_tools().execute(
                        "librarian_onboarding",
                        tctx,
                        bypass_thread_guard=False,
                        **{
                            "query": query_text,
                            "history_text": history_text,
                            "suggested_user_name": getattr(self, "_librarian_suggested_user_name", None),
                        },
                    )
                    result = json.dumps(res, default=str) if isinstance(res, dict) else str(res)

                    data = safe_json_loads(result)
                    if not isinstance(data, dict):
                        log.error("Failed to parse librarian result in _run_librarian [doc: %s]", doc_type)
                        parsed_err = AgentParsingError("Invalid JSON from librarian tool.", details={"raw_result": result})
                        data = format_error_payload(parsed_err)

                    if data.get("status") == "ok":
                        answer = data.get("result", "")
                        if not isinstance(answer, str):
                            answer = str(answer)
                        self._record_assistant_start = True
                        q.put((StreamQueueKind.CHUNK, answer + "\n"))
                        q.put((StreamQueueKind.STREAM_DONE, {"assistant_content": answer}))
                    elif data.get("status") == "switch_mode":
                        # Exit librarian on the UI thread via STREAM_DONE (combobox is UNO).
                        answer = data.get("result", _("Perfect! I'm switching you to the main assistant now."))
                        self._record_assistant_start = True
                        q.put((StreamQueueKind.CHUNK, answer + "\n"))
                        q.put((StreamQueueKind.STREAM_DONE, {"librarian_switch_to_chat": True, "assistant_content": answer}))
                    else:
                        msg = data.get("message", _("Unknown librarian error."))
                        note = "\n" + _("[Librarian error: {0}]").format(msg) + "\n"
                        q.put((StreamQueueKind.CHUNK, note))
                        # Mode flag rides the one STREAM_DONE so the UI thread applies it.
                        q.put((StreamQueueKind.STREAM_DONE, {**_specialized_tool_error_payload(note), "in_librarian_mode": False}))
                elif is_brainstorming:
                    topic = getattr(self, "_brainstorming_topic", "") or ""
                    res = get_tools().execute(
                        "brainstorming_session",
                        tctx,
                        bypass_thread_guard=False,
                        **{"query": query_text, "history_text": history_text, "topic": topic},
                    )
                    result = json.dumps(res, default=str) if isinstance(res, dict) else str(res)

                    data = safe_json_loads(result)
                    if not isinstance(data, dict):
                        log.error("Failed to parse brainstorming result [doc: %s]", doc_type)
                        parsed_err = AgentParsingError("Invalid JSON from brainstorming tool.", details={"raw_result": result})
                        data = format_error_payload(parsed_err)

                    done_payload: dict[str, Any] = {}
                    if data.get("status") == "ok":
                        answer = data.get("result", "")
                        if not isinstance(answer, str):
                            answer = str(answer)
                        self._record_assistant_start = True
                        q.put((StreamQueueKind.CHUNK, answer + "\n"))
                        done_payload["assistant_content"] = answer
                    elif data.get("status") == "finished":
                        done_payload = {"brainstorming_finished": True, "spec_saved": bool(data.get("spec_saved", False))}
                        answer = data.get("result", _("Brainstorming complete."))
                        if not isinstance(answer, str):
                            answer = str(answer)
                        self._record_assistant_start = True
                        q.put((StreamQueueKind.CHUNK, answer + "\n"))
                        done_payload["assistant_content"] = answer
                    else:
                        done_payload["in_brainstorming_mode"] = False
                        msg = data.get("message", _("Unknown brainstorming error."))
                        note = "\n" + _("[Brainstorming error: {0}]").format(msg) + "\n"
                        q.put((StreamQueueKind.CHUNK, note))
                        done_payload.update(_specialized_tool_error_payload(note))

                    q.put((StreamQueueKind.STREAM_DONE, done_payload))
                elif is_writing_plan:
                    topic = getattr(self, "_writing_plan_topic", "") or ""
                    res = get_tools().execute(
                        "writing_plan_session",
                        tctx,
                        bypass_thread_guard=False,
                        **{"query": query_text, "history_text": history_text, "topic": topic},
                    )
                    result = json.dumps(res, default=str) if isinstance(res, dict) else str(res)

                    data = safe_json_loads(result)
                    if not isinstance(data, dict):
                        log.error("Failed to parse writing plan result [doc: %s]", doc_type)
                        parsed_err = AgentParsingError("Invalid JSON from writing plan tool.", details={"raw_result": result})
                        data = format_error_payload(parsed_err)

                    done_payload = {}
                    if data.get("status") == "ok":
                        answer = data.get("result", "")
                        if not isinstance(answer, str):
                            answer = str(answer)
                        self._record_assistant_start = True
                        q.put((StreamQueueKind.CHUNK, answer + "\n"))
                        done_payload["assistant_content"] = answer
                    elif data.get("status") == "finished":
                        done_payload = {"writing_plan_finished": True}
                        answer = data.get("result", _("Writing plan complete."))
                        if not isinstance(answer, str):
                            answer = str(answer)
                        self._record_assistant_start = True
                        q.put((StreamQueueKind.CHUNK, answer + "\n"))
                        done_payload["assistant_content"] = answer
                    else:
                        done_payload["in_writing_plan_mode"] = False
                        msg = data.get("message", _("Unknown writing plan error."))
                        note = "\n" + _("[Writing plan error: {0}]").format(msg) + "\n"
                        q.put((StreamQueueKind.CHUNK, note))
                        done_payload.update(_specialized_tool_error_payload(note))

                    q.put((StreamQueueKind.STREAM_DONE, done_payload))
                elif is_ppt_master:
                    topic = getattr(self, "_ppt_master_topic", "") or ""
                    res = get_tools().execute(
                        "ppt_master_session",
                        tctx,
                        bypass_thread_guard=False,
                        **{"query": query_text, "history_text": history_text, "topic": topic},
                    )
                    result = json.dumps(res, default=str) if isinstance(res, dict) else str(res)

                    data = safe_json_loads(result)
                    if not isinstance(data, dict):
                        log.error("Failed to parse PPT-Master result [doc: %s]", doc_type)
                        parsed_err = AgentParsingError("Invalid JSON from PPT-Master tool.", details={"raw_result": result})
                        data = format_error_payload(parsed_err)

                    done_payload = {}
                    if data.get("status") == "ok":
                        answer = data.get("result", "")
                        if not isinstance(answer, str):
                            answer = str(answer)
                        self._record_assistant_start = True
                        q.put((StreamQueueKind.CHUNK, answer + "\n"))
                        done_payload["assistant_content"] = answer
                    elif data.get("status") == "finished":
                        done_payload = {"ppt_master_finished": True, "exported": bool(data.get("exported", False))}
                        answer = data.get("result", _("PPT-Master session complete."))
                        if not isinstance(answer, str):
                            answer = str(answer)
                        self._record_assistant_start = True
                        q.put((StreamQueueKind.CHUNK, answer + "\n"))
                        done_payload["assistant_content"] = answer
                    else:
                        done_payload["in_ppt_master_mode"] = False
                        msg = data.get("message", _("Unknown PPT-Master error."))
                        note = "\n" + _("[PPT-Master error: {0}]").format(msg) + "\n"
                        q.put((StreamQueueKind.CHUNK, note))
                        done_payload.update(_specialized_tool_error_payload(note))

                    q.put((StreamQueueKind.STREAM_DONE, done_payload))
                elif is_deep_research:
                    res = get_tools().execute(
                        "deep_research_session",
                        tctx,
                        bypass_thread_guard=False,
                        **{"query": query_text, "history_text": history_text},
                    )
                    result = json.dumps(res, default=str) if isinstance(res, dict) else str(res)

                    data = safe_json_loads(result)
                    if not isinstance(data, dict):
                        log.error("Failed to parse deep research result [doc: %s]", doc_type)
                        parsed_err = AgentParsingError("Invalid JSON from deep research tool.", details={"raw_result": result})
                        data = format_error_payload(parsed_err)

                    done_payload = {}
                    if data.get("status") == "ok":
                        answer = data.get("result", "")
                        if not isinstance(answer, str):
                            answer = str(answer)
                        self._record_assistant_start = True
                        q.put((StreamQueueKind.CHUNK, answer + "\n"))
                        done_payload["assistant_content"] = answer
                    else:
                        msg = data.get("message", _("Unknown deep research error."))
                        note = "\n" + _("[Deep research error: {0}]").format(msg) + "\n"
                        q.put((StreamQueueKind.CHUNK, note))
                        done_payload.update(_specialized_tool_error_payload(note))

                    q.put((StreamQueueKind.STREAM_DONE, done_payload))
                else:
                    res = get_tools().execute("web_research", tctx, bypass_thread_guard=False, **{"query": query_text, "history_text": history_text})
                    result = json.dumps(res, default=str) if isinstance(res, dict) else str(res)

                    data = safe_json_loads(result)
                    if not isinstance(data, dict):
                        log.error("Failed to parse web_research result in _run_web_research [doc: %s]", doc_type)
                        parsed_err = AgentParsingError("Invalid JSON from web search tool.", details={"raw_result": result})
                        data = format_error_payload(parsed_err)

                    done_payload = {}
                    if data.get("status") == "ok":
                        from plugin.chatbot.web_research_chat import format_research_cache_result_chat

                        answer = data.get("result", "")
                        if not isinstance(answer, str):
                            answer = str(answer)
                        cache_block = format_research_cache_result_chat(data)
                        self._record_assistant_start = True
                        q.put((StreamQueueKind.CHUNK, cache_block + answer + "\n"))
                        done_payload["assistant_content"] = cache_block + answer
                    else:
                        msg = data.get("message", _("Unknown research error."))
                        note = "\n" + _("[Research error: {0}]").format(msg) + "\n"
                        q.put((StreamQueueKind.CHUNK, note))
                        done_payload.update(_specialized_tool_error_payload(note))

                    q.put((StreamQueueKind.STREAM_DONE, done_payload))
            except Exception as e:
                log.exception("Web/Librarian path ERROR in _run_web_research [doc: %s]", doc_type)

                q.put((StreamQueueKind.ERROR, format_error_payload(e)))

        def on_approval_required(item: Any) -> None:
            # item = ("approval_required", query_for_engine, tool_name, event_obj)
            query_for_engine = item[1] if len(item) > 1 else ""
            tool_name = item[2] if len(item) > 2 else ""
            event_obj = item[3] if len(item) > 3 else None
            if event_obj is not None:
                self.begin_inline_web_approval(query_for_engine, tool_name, event_obj)
            log.info("web_research on_approval_required: tool=%s (inline Accept/Change/Reject)", tool_name)

        self._run_unified_worker_drain_loop(drain_q, run_search, current_state, interpreter, show_thinking=show_thinking, on_approval_callback=on_approval_required)

        def _finalize_research() -> None:
            _reset_active_run_flags(self)
            from plugin.chatbot.rich_text import finalize_sidebar_assistant_response

            finalize_sidebar_assistant_response(self, allow_rerender=not self.stop_requested)

        defer_until_drain_done(_finalize_research)

    def _get_mcp_url(self: SendHandlerHost) -> str | None:
        """Construct the local MCP streamable-HTTP endpoint URL from config."""
        try:
            if not as_bool(get_config("mcp.mcp_enabled")):
                return None
            from plugin.mcp.server import mcp_endpoint_url

            port = get_config_int_safe("mcp.mcp_port")
            # MCP binds localhost only (mcp/module.yaml); host/ssl are not user settings.
            return mcp_endpoint_url("localhost", port, False)
        except (ValueError, TypeError) as e:
            log.debug("Failed to read MCP config: %s", e)
            return None
