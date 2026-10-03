"""Effect interpreter for the sidebar tool-calling loop.

``tool_loop_state.next_state`` stays pure: it returns effect descriptions.
This module is the command boundary where those descriptions touch UI,
session history, workers, tools, and document context.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import traceback
from typing import Any, Callable, Protocol

from plugin.chatbot.tool_loop_state import (
    DELEGATE_GATEWAY_TOOL_NAMES,
    AddMessageEffect,
    ExitLoopEffect,
    LogAgentEffect,
    SpawnFinalStreamEffect,
    SpawnLLMWorkerEffect,
    SpawnToolWorkerEffect,
    ToolLoopUIEffect,
    TriggerNextToolEffect,
    UpdateActivityStateEffect,
    UpdateDocumentContextEffect,
)
from plugin.framework.async_stream import StreamQueueKind
from plugin.framework.client.model_fetcher import get_text_model, set_native_audio_support
from plugin.framework.config import get_config_bool, get_current_endpoint
from plugin.framework.errors import ToolExecutionError, UnoObjectError, format_error_payload, is_disposed_exception, is_tool_document_disposed
from plugin.framework.logging import agent_log, update_activity_state
from plugin.framework.queue_executor import execute_on_main_thread
from plugin.framework.tool import ToolContext
from plugin.framework.worker_pool import run_in_background

log = logging.getLogger(__name__)


class TurnController:
    """One sidebar send. Stop or the next send aborts it and drops the reference.

    What was wrong: a generation counter, a pinned ``_apply_turn``, and each
    worker's queue were three copies of "which send is this". Stop bumped the
    counter and left the old object alive so the banner could still paint.
    A newer send did the same. Callbacks kept enqueueing onto those queues.

    This object is the turn. Workers and the drain close over it. ``abort``
    makes later ``put`` calls no-ops and drops any text still in the batcher.
    The host holds at most one (``_turn``). The sidebar paints ``session.messages``;
    streamed tokens are the open row on that list, not a second buffer.
    """

    mode: str
    session: Any
    messages: Any
    queue: Any
    batcher: Any
    _alive: bool

    def __init__(self, session: Any, mode: str) -> None:
        self.mode = str(mode or "")
        self.session = session
        self.messages = getattr(session, "messages", None) if session is not None else None
        self.queue = None
        self.batcher = None
        self._alive = True

    @property
    def alive(self) -> bool:
        return self._alive

    def same_messages(self) -> bool:
        """Clear replaces the list. A write against the old list must not land."""
        if self.messages is None:
            return True
        return getattr(self.session, "messages", None) is self.messages

    def abort(self) -> None:
        """Refuse later callbacks. Idempotent."""
        self._alive = False
        _discard_batcher(self.batcher)

    def open_text(self) -> str:
        """Assistant bytes already folded into this turn's message list."""
        if not self.same_messages():
            return ""
        messages = self.messages
        if not isinstance(messages, list) or not messages:
            return ""
        last = messages[-1]
        if not isinstance(last, dict) or not last.get("_open_transcript"):
            return ""
        content = last.get("content") or ""
        return content.strip() if isinstance(content, str) else ""

    def put(self, item: Any) -> bool:
        """Enqueue only while this turn is the one the host still holds and it is alive.

        After abort, or after a newer send replaced it, the item is dropped.
        It is not parked on a queue for a later drain.
        """
        if not self._alive or self.queue is None:
            return False
        self.queue.put(item)
        return True

    def accepts_history(self, host: Any) -> bool:
        """The close may still store on this list.

        Stop has already aborted the turn. The partial and the stop line
        still belong here until a newer send replaces ``_turn`` or Clear
        replaces the list. A mode change that swapped the visible session
        does not store: the caller checks ``host.session``.
        """
        if current_turn(host) is not self:
            return False
        return self.same_messages()

    def accepts_display(self, host: Any, text: str) -> bool:
        """A chunk paints only while this turn is alive and on screen.

        The stop line is the close, so it still paints after ``abort`` when
        this object is the host's turn and the sidebar is still showing its
        session. Any other text after abort is dropped.
        """
        if not self.accepts_history(host):
            return False
        if getattr(host, "session", None) is not self.session:
            return False
        if self._alive:
            return True
        return _is_stop_banner(text)


def _discard_batcher(batcher: Any) -> None:
    """Drop a producer batch so its timer cannot emit after the turn is gone."""
    discard = getattr(batcher, "discard", None)
    if callable(discard):
        discard()


def current_turn(host: Any) -> TurnController | None:
    turn = getattr(host, "_turn", None)
    if isinstance(turn, TurnController):
        return turn
    return None


def abort_turn(host: Any) -> None:
    """Stop, a mode change, or dispose. The host still names this turn until ``drop_turn``."""
    turn = current_turn(host)
    if turn is not None:
        turn.abort()
        return
    _discard_batcher(getattr(host, "_active_batched_q", None))


def drop_turn(host: Any) -> None:
    """The drain has finished. Forget the turn so a late callback cannot find it."""
    turn = current_turn(host)
    if turn is None:
        return
    turn.abort()
    if current_turn(host) is turn:
        host._turn = None


def begin_send_turn(host: Any, mode: str) -> TurnController:
    """Start a send. The previous turn, if any, is aborted and replaced."""
    previous = current_turn(host)
    if previous is not None:
        previous.abort()
    session = getattr(host, "session", None)
    turn = TurnController(session, mode)
    host._turn = turn
    return turn


def bind_turn_session(host: Any, mode: str | None = None) -> TurnController:
    """Use the turn already started for this session, or start one.

    What was wrong: Clear replaces ``messages`` while the drain is inside
    ``processEventsToIdle``, and ``set_session`` swaps ``host.session`` while
    a worker still reads that attribute. The reply was stored on the wiped
    chat, or on a mode that never got the user row.

    Calling this again for the same list must not abort the turn that just
    started, or its own chunks would miss.
    """
    session = getattr(host, "session", None)
    messages = getattr(session, "messages", None) if session is not None else None
    active = current_turn(host)
    if (
        active is not None
        and active.alive
        and active.session is session
        and active.messages is messages
    ):
        if mode:
            active.mode = str(mode)
        return active
    return begin_send_turn(host, mode or "")


def session_for_turn(host: Any) -> Any:
    """The session this send bound, or the live one when no turn is current."""
    turn = current_turn(host)
    if turn is not None and turn.session is not None:
        return turn.session
    return getattr(host, "session", None)


def _turn_accepts_write(host: Any, session: Any) -> bool:
    """True when this send may still change ``session``.

    No turn yet: the caller is before bind (tests, a pre-send error).
    A turn that is no longer current, or whose list Clear replaced, rejects.
    """
    turn = current_turn(host)
    if turn is None:
        return True
    if session is not turn.session:
        return False
    return turn.accepts_history(host)


_STOP_BANNER = "[Stopped by user]"


def _is_stop_banner(text: str) -> bool:
    return bool(text) and text.strip() == _STOP_BANNER


def stopped_assistant_text(host: Any, partial: str | None) -> str:
    """Text to store when the user hits Stop.

    What was wrong: Stop stored ``No response.`` after the sidebar had already
    shown streamed tokens. Those tokens are the open row on the session.
    Prefer the worker partial, then that row.
    """
    turn = current_turn(host)
    emitted = turn.open_text() if isinstance(turn, TurnController) else ""
    text = partial.strip() if isinstance(partial, str) else ""
    if text and text != "No response.":
        return text
    return emitted


def take_stripper_tail(host: Any) -> str:
    """Finish the HTML stripper and return any held fragment.

    Stop aborts the turn before the drain's finalize runs. The fragment was
    still in the stripper, so the stored answer dropped the tail of an
    unclosed tag. Folding it into the open row here puts it in the session
    the sidebar paints.
    """
    stripper = getattr(host, "_plain_text_stripper", None)
    if stripper is None:
        return ""
    host._plain_text_stripper = None
    try:
        leftover = stripper.finalize()
    except Exception:
        log.debug("stripper finalize on stop failed", exc_info=True)
        return ""
    return leftover if isinstance(leftover, str) else ""


def fold_stop_tail(host: Any, text: str) -> None:
    """Put the stripper tail on this turn's open row, then the caller stores it."""
    if not text:
        return
    turn = current_turn(host)
    if turn is None or not turn.accepts_history(host) or turn.session is None:
        return
    from plugin.chatbot.rich_text_paste import fold_transcript_chunk

    fold_transcript_chunk(turn.session, text, "assistant")


def emit_for_host(host: Any, item: Any) -> bool:
    """Enqueue on the current turn. A dead turn drops the item."""
    turn = current_turn(host)
    q = turn.queue if isinstance(turn, TurnController) else None
    if q is None:
        q = getattr(host, "_active_q", None)
    return put_for_turn(host, turn, q, item)


def put_for_turn(host: Any, turn: Any, q: Any, item: Any) -> bool:
    """Workers enqueue on the controller they captured. They do not touch the transcript.

    ``host`` is unused. Callers still pass it so a worker closure does not
    grow a second way to find the queue.
    """
    del host
    if isinstance(turn, TurnController):
        if turn.queue is None and q is not None:
            turn.queue = q
        return turn.put(item)
    if q is None:
        return False
    q.put(item)
    return True


def persist_assistant_on_turn(
    host: Any,
    content: Any = None,
    tool_calls: Any = None,
    reasoning_replay: Any = None,
) -> None:
    turn = current_turn(host)
    if isinstance(turn, TurnController):
        if not turn.accepts_history(host):
            return
        session = turn.session
    else:
        session = session_for_turn(host)
        if session is None or not _turn_accepts_write(host, session):
            return
    if session is None:
        return
    kwargs: dict[str, Any] = {}
    if tool_calls is not None:
        kwargs["tool_calls"] = tool_calls
    if reasoning_replay is not None:
        kwargs["reasoning_replay"] = reasoning_replay
    session.add_assistant_message(content=content, **kwargs)


def _queue_tool_failure(host: Any, call_id: str, func_name: str, func_args_str: str, exc: BaseException, turn: Any = None, q: Any = None, *, model: Any) -> None:
    """Queue a tool failure. A disposed document ends the loop.

    What was wrong: both workers turned every exception into a JSON tool
    payload and queued ``TOOL_DONE``, so a closed document looked like a
    normal tool error and the loop continued. ``is_tool_document_disposed``
    is the tool-boundary check; ``is_disposed_exception`` also matches a
    bare ``RuntimeException`` from a live document.

    What was wrong: the check read ``host._active_model`` when the worker
    finished. A later send had already stored the new turn's document
    there, so a disposed-document failure from the tool that already
    started was scored against that live model. How: a bare
    ``RuntimeException`` then failed ``is_tool_document_disposed`` and was
    queued as ``TOOL_DONE``. Why: ``model`` is the document closed over at
    spawn, the same capture that keeps the tool from running on the new send.
    """
    if turn is None:
        turn = current_turn(host)
    if q is None:
        q = getattr(turn, "queue", None) if isinstance(turn, TurnController) else None
    if q is None:
        q = getattr(host, "_active_q", None)
    payload_error = (StreamQueueKind.ERROR, format_error_payload(exc))
    payload_done = (StreamQueueKind.TOOL_DONE, call_id, func_name, func_args_str, json.dumps(format_error_payload(exc)))
    if is_tool_document_disposed(exc, model):
        put_for_turn(host, turn, q, payload_error)
        return
    put_for_turn(host, turn, q, payload_done)


def persist_tool_on_turn(host: Any, call_id: str | None, content: Any) -> None:
    turn = current_turn(host)
    if isinstance(turn, TurnController):
        if not turn.accepts_history(host):
            return
        session = turn.session
    else:
        session = session_for_turn(host)
        if session is None or not _turn_accepts_write(host, session):
            return
    if session is None:
        return
    session.add_tool_result(call_id, content)


class ToolLoopActionHost(Protocol):
    ctx: Any
    session: Any
    image_model_selector: Any
    audio_wav_path: str | None
    _active_q: Any
    _active_batched_q: Any
    _active_client: Any
    _active_max_tokens: int
    _active_tools: list[dict[str, Any]]
    _active_execute_tool_fn: Callable[..., Any]
    _active_model: Any
    _active_query_text: str | None
    _active_supports_status: bool
    _current_tool_call_id: str | None
    _terminal_status: str

    def _append_response(self, text: str, is_thinking: bool = False, role: str = "assistant") -> None: ...
    def _set_status(self, text: str) -> None: ...
    def _get_document_model(self) -> Any: ...
    def _refresh_active_tools_for_session(self) -> None: ...
    def _spawn_llm_worker(self, q: Any, client: Any, max_tokens: int, tools: list[dict[str, Any]], round_num: int, query_text: str | None = None) -> None: ...
    def _spawn_final_stream(self, q: Any, client: Any, max_tokens: int) -> None: ...
    def resolve_stop_checker(self) -> Callable[[], bool]: ...


def build_tool_execute_fn(
    host: Any,
    doc_type_str: str,
    active_domain: Any,
    python_tool_domain: Any,
    set_active_domain: Callable[..., None],
) -> Callable[..., str]:
    """Build the tool executor used by SpawnToolWorkerEffect.

    The returned callable is intentionally independent from the send setup code
    so tests can verify ToolContext wiring and error serialization without
    starting a full sidebar send.
    """

    def execute_fn(
        name: str,
        args: Any,
        doc: Any,
        ctx: Any,
        status_callback: Callable[[str], None] | None = None,
        append_thinking_callback: Callable[[str], None] | None = None,
        stop_checker: Callable[[], bool] | None = None,
        *,
        captured_turn: Any = None,
        captured_q: Any = None,
        captured_call_id: str | None = None,
    ) -> str:
        from plugin.main import get_tools as _get_tools

        # NOTE: Experimental planning/TodoStore wiring is intentionally
        # commented out. When enabling the hermes-style todo tool,
        # you can attach a session-scoped TodoStore here and expose it
        # via ToolContext.services, e.g.:
        #
        # from plugin.contrib.todo_store import TodoStore
        # if not hasattr(host, "_todo_store"):
        #     host._todo_store = TodoStore()
        # services = dict(_get_tools()._services)
        # services["todo_store"] = host._todo_store
        #
        # and then pass `services=services` into ToolContext below.

        approval_cb: Any = None
        chat_append_cb: Any = None
        safe_args = args if isinstance(args, dict) else {}

        delegate_domain = str(safe_args.get("domain") or "") if name in DELEGATE_GATEWAY_TOOL_NAMES else ""
        # Delegate gateways forward domain=web_research to WebResearchTool with the same ctx;
        # they must receive the same HITL wiring as the outer web_research tool.
        needs_web_research_ui = name == "web_research" or delegate_domain == "web_research"
        needs_document_research_ui = delegate_domain == "document_research"
        if needs_web_research_ui or needs_document_research_ui:

            def _subagent_target() -> tuple[Any, Any]:
                # What was wrong: chat lines and the approval dialog were put
                # on host._active_q when the callback ran. Stop or a new send
                # had replaced that queue, so the text or the dialog landed
                # on the next turn. Why: the tool worker already captured
                # this turn at spawn. A dead turn drops the item.
                if captured_turn is not None or captured_q is not None:
                    return captured_turn, captured_q
                live = current_turn(host)
                live_q = live.queue if isinstance(live, TurnController) else None
                if live_q is None:
                    live_q = getattr(host, "_active_q", None)
                return live, live_q

            def _sub_agent_chat_append(text: str) -> None:
                emit_turn, emit_q = _subagent_target()
                if not put_for_turn(host, emit_turn, emit_q, (StreamQueueKind.CHUNK, text)):
                    return
                cid = captured_call_id if captured_call_id is not None else getattr(host, "_current_tool_call_id", None)
                if isinstance(emit_turn, TurnController) and emit_turn.session is not None:
                    streamed_session = emit_turn.session
                else:
                    streamed_session = session_for_turn(host)
                if cid and streamed_session is not None:
                    if not hasattr(streamed_session, "tool_streamed_texts"):
                        streamed_session.tool_streamed_texts = {}
                    if cid not in streamed_session.tool_streamed_texts:
                        streamed_session.tool_streamed_texts[cid] = []
                    streamed_session.tool_streamed_texts[cid].append(text)

            chat_append_cb = _sub_agent_chat_append

            try:
                if needs_web_research_ui and get_config_bool("chatbot.prompt_for_web_research"):

                    def _web_approval(query_for_engine: str, tool_name: str, args: Any) -> Any:
                        emit_turn, q = _subagent_target()
                        if q is None:
                            log.warning("tool_loop: web_research approval skipped (queue missing)")
                            return True
                        event = threading.Event()
                        # Use setattr/getattr to avoid static attribute errors on Event.
                        setattr(event, "approved", False)
                        setattr(event, "query_override", None)
                        from plugin.framework.queue_executor import wait_for_approval

                        if not put_for_turn(host, emit_turn, q, (StreamQueueKind.APPROVAL_REQUIRED, query_for_engine, tool_name, event)):
                            return (False, None)
                        checker = stop_checker if stop_checker is not None else host.resolve_stop_checker()
                        # event.wait() ignored Stop. Sidebar close latches the
                        # checker and never sets the event, so this worker parked.
                        if not wait_for_approval(event, checker):
                            put_for_turn(host, emit_turn, q, (StreamQueueKind.STOPPED,))
                            return (False, None)
                        if not getattr(event, "approved", False):
                            put_for_turn(host, emit_turn, q, (StreamQueueKind.STOPPED,))
                        return (bool(getattr(event, "approved", False)), getattr(event, "query_override", None))

                    approval_cb = _web_approval
            except Exception as ex:
                # What was wrong: this logged and left approval_cb as None.
                # web_research prompts only when both the config flag and the
                # callback are set, so a config error skipped Accept/Change/Reject
                # and the search ran. Fail closed instead.
                log.warning("tool_loop: web_research approval setup failed: %s", ex)
                err = ToolExecutionError(
                    "Web research approval could not be shown. The search was not started.",
                    code="WEB_RESEARCH_APPROVAL_UNAVAILABLE",
                )
                return json.dumps(format_error_payload(err))

        active_page_idx = None
        if doc_type_str in ("draw", "impress"):
            try:
                from plugin.draw.bridge import DrawBridge

                # Async gateways (delegate_to_specialized_draw_toolset) run
                # execute_fn on the worker; hasattr(doc, "getDrawPages") is UNO.
                active_page_idx = execute_on_main_thread(
                    lambda: DrawBridge(doc).get_active_page_index()
                )
            except Exception:
                log.debug("execute_fn: failed to get active page index for %s", doc_type_str)

        cancel_scope = getattr(host, "_send_cancellation", None)

        tctx = ToolContext(
            doc=doc,
            ctx=ctx,
            doc_type=doc_type_str,
            services=_get_tools()._services,
            caller="chat",
            active_page_index=active_page_idx,
            status_callback=status_callback,
            append_thinking_callback=append_thinking_callback,
            stop_checker=stop_checker if stop_checker is not None else host.resolve_stop_checker(),
            approval_callback=approval_cb,
            chat_append_callback=chat_append_cb if (needs_web_research_ui or needs_document_research_ui) else None,
            set_active_domain_callback=set_active_domain,
            active_domain=active_domain,
            python_tool_domain=python_tool_domain,
            send_cancellation=cancel_scope,
            uno_services_supported=getattr(host, "cached_uno_services", None),
        )
        try:
            res = _get_tools().execute(name, tctx, **safe_args)
            return json.dumps(res) if isinstance(res, dict) else str(res)
        except (ToolExecutionError, UnoObjectError) as e:
            if is_tool_document_disposed(e, doc):
                raise
            tb = traceback.format_exc()
            log.exception("Tool execution failed")
            agent_log("tool_loop.py:execute_fn", "Tool execution failed", data={"type": type(e).__name__, "message": str(e)})
            err_payload = format_error_payload(e)
            if "details" not in err_payload:
                err_payload["details"] = {}
            err_payload["details"]["traceback"] = tb
            return json.dumps(err_payload)
        except Exception as e:
            if is_tool_document_disposed(e, doc):
                raise
            log.exception("Unexpected tool error")
            tb = traceback.format_exc()
            wrapped_error = ToolExecutionError("Unexpected error executing tool '%s'" % name, code="TOOL_UNEXPECTED_ERROR", details={"tool_name": name, "original_error": str(e), "type": type(e).__name__, "traceback": tb})
            return json.dumps(format_error_payload(wrapped_error))

    return execute_fn


class ToolLoopEffectInterpreter:
    """Execute tool-loop effects against a concrete sidebar host."""

    host: ToolLoopActionHost

    def __init__(self, host: ToolLoopActionHost) -> None:
        self.host = host

    def execute(self, effect: Any) -> bool:
        """Run one effect and return True when the drain loop should exit."""

        host = self.host
        if isinstance(effect, ExitLoopEffect):
            return True
        if isinstance(effect, TriggerNextToolEffect):
            emit_for_host(host, (StreamQueueKind.NEXT_TOOL,))
        elif isinstance(effect, SpawnFinalStreamEffect):
            host._spawn_final_stream(host._active_batched_q or host._active_q, host._active_client, host._active_max_tokens)
        elif isinstance(effect, UpdateDocumentContextEffect):
            if self._refresh_document_context():
                return True
        elif isinstance(effect, ToolLoopUIEffect):
            self._execute_ui_effect(effect)
        elif isinstance(effect, LogAgentEffect):
            agent_log(effect.location, effect.message, data=effect.data, hypothesis_id=effect.hypothesis_id)
        elif isinstance(effect, AddMessageEffect):
            self._add_message(effect)
        elif isinstance(effect, SpawnLLMWorkerEffect):
            host._refresh_active_tools_for_session()
            host._spawn_llm_worker(host._active_batched_q or host._active_q, host._active_client, host._active_max_tokens, host._active_tools, effect.round_num, query_text=host._active_query_text)
        elif isinstance(effect, UpdateActivityStateEffect):
            self._update_activity_state(effect)
        elif effect.__class__.__name__ == "CleanupAudioEffect":
            self._cleanup_audio()
        elif isinstance(effect, SpawnToolWorkerEffect):
            self._spawn_tool_worker(effect)
        return False

    def _refresh_document_context(self) -> bool:
        """Refresh the document snapshot. Return True when the drain should stop.

        A missing or disposed document used to be logged at debug and the
        tool loop kept going with the previous snapshot. That is the same
        failure _do_send already ends on.
        """
        host = self.host
        session = session_for_turn(host)
        if session is None or not _turn_accepts_write(host, session):
            # Clear replaced the message list. Do not write [DOCUMENT CONTENT]
            # onto the wiped chat, and do not keep the tool loop going.
            return True
        try:
            doc = host._get_document_model() if hasattr(host, "_get_document_model") else None
            if not doc:
                raise UnoObjectError("Document closed or unavailable.", code="DOCUMENT_UNAVAILABLE")
            session.refresh_document_context(doc, host.ctx)
            return False
        except Exception as exc:
            if is_disposed_exception(exc):
                log.debug("Tool loop: document disposed during context refresh", exc_info=True)
            else:
                log.exception("Tool loop: failed to refresh document context after mutating tool")
            host._append_response("\n[Document closed or unavailable.]\n")
            host._terminal_status = "Error"
            host._set_status("Error")
            return True

    def _execute_ui_effect(self, effect: ToolLoopUIEffect) -> None:
        host = self.host
        if effect.kind == "append":
            host._append_response(effect.text)
            if effect.text.startswith("\n[Debug: round="):
                log.warning("Tool loop: no assistant text from model: %s", effect.text.strip())
        elif effect.kind == "status":
            host._set_status(effect.text)
            if effect.text in ("Stopped", "Ready", "Error"):
                host._terminal_status = effect.text
        elif effect.kind == "debug":
            log.debug(effect.text)
        elif effect.kind == "info":
            log.info(effect.text)

    def _add_message(self, effect: AddMessageEffect) -> None:
        if effect.role == "assistant":
            persist_assistant_on_turn(
                self.host,
                content=effect.content,
                tool_calls=effect.tool_calls,
                reasoning_replay=effect.reasoning_replay,
            )
        elif effect.role == "tool":
            persist_tool_on_turn(self.host, effect.call_id, effect.content)

    def _update_activity_state(self, effect: UpdateActivityStateEffect) -> None:
        if effect.action == "tool_execute":
            update_activity_state("tool_execute", round_num=effect.round_num, tool_name=effect.tool_name)
        elif effect.action == "exhausted_rounds":
            update_activity_state("exhausted_rounds")

    def _cleanup_audio(self) -> None:
        host = self.host
        current_model = get_text_model()
        current_endpoint = get_current_endpoint()
        set_native_audio_support(current_model, current_endpoint, supported=True)

        try:
            if host.audio_wav_path:
                os.remove(host.audio_wav_path)
        except Exception:
            pass
        host.audio_wav_path = None

    def _spawn_tool_worker(self, effect: SpawnToolWorkerEffect) -> None:
        host = self.host
        func_name = effect.func_name
        func_args_str = effect.func_args_str
        func_args = effect.func_args
        call_id = effect.call_id
        host._current_tool_call_id = call_id
        # Capture the queue at spawn. A later send replaces host._active_q;
        # this worker must not enqueue onto that turn.
        turn = current_turn(host)
        worker_q = turn.queue if isinstance(turn, TurnController) else None
        if worker_q is None:
            worker_q = host._active_q

        def emit(item: Any) -> None:
            put_for_turn(host, turn, worker_q, item)

        # What was wrong: the async body read host._active_execute_tool_fn
        # and host._active_model when the thread ran. A new send replaced
        # both while this tool was still in flight, so the old call ran
        # against the new send. The failure path had the same hole: it
        # asked is_tool_document_disposed about the live host model.
        # Why: close over the values this spawn already had, the same way
        # worker_q is captured above, and pass that model into the failure.
        execute_tool_fn = host._active_execute_tool_fn
        model = host._active_model
        supports_status = host._active_supports_status
        bound_stop = host.resolve_stop_checker()

        image_model_override = host.image_model_selector.getText() if host.image_model_selector else None
        if image_model_override and func_name == "image_generate":
            func_args["image_model"] = image_model_override

        def tool_status_callback(msg: str) -> None:
            emit((StreamQueueKind.STATUS, msg))

        if effect.is_async:

            def run_async() -> None:
                try:

                    def tool_thinking_callback(msg: str) -> None:
                        emit((StreamQueueKind.TOOL_THINKING, msg))

                    if supports_status:
                        res = execute_tool_fn(func_name, func_args, model, host.ctx, status_callback=tool_status_callback, append_thinking_callback=tool_thinking_callback, stop_checker=bound_stop, captured_turn=turn, captured_q=worker_q, captured_call_id=call_id)
                    else:
                        res = execute_tool_fn(func_name, func_args, model, host.ctx, stop_checker=bound_stop, captured_turn=turn, captured_q=worker_q, captured_call_id=call_id)
                    emit((StreamQueueKind.TOOL_DONE, call_id, func_name, func_args_str, res))
                except Exception as e:
                    _queue_tool_failure(host, call_id, func_name, func_args_str, e, turn, worker_q, model=model)

            run_in_background(run_async, name=f"tool-async-{func_name}", dedicated=True)
        else:
            # Sync tools run inline on the drain thread — if Stop appears broken, check these
            # enter/exit timings against document_to_content phase logs for the stuck step.
            t0 = time.perf_counter()
            log.debug("sync tool start name=%s", func_name)
            try:
                if supports_status:
                    res = execute_tool_fn(func_name, func_args, model, host.ctx, status_callback=tool_status_callback)
                else:
                    res = execute_tool_fn(func_name, func_args, model, host.ctx)
                log.debug("sync tool done name=%s elapsed_ms=%.1f", func_name, (time.perf_counter() - t0) * 1000.0)
                emit((StreamQueueKind.TOOL_DONE, call_id, func_name, func_args_str, res))
            except Exception as e:
                log.debug("sync tool failed name=%s elapsed_ms=%.1f", func_name, (time.perf_counter() - t0) * 1000.0)
                _queue_tool_failure(host, call_id, func_name, func_args_str, e, turn, worker_q, model=model)
