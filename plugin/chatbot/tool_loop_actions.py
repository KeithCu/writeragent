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
from typing import TYPE_CHECKING, Any, Callable, Mapping, Protocol

if TYPE_CHECKING:
    from plugin.framework.client.llm_client import LlmClient

from plugin.chatbot.memory import format_upsert_memory_chat_line
from plugin.chatbot.tool_loop_state import (
    AddMessageEffect,
    CleanupAudioEffect,
    ExitLoopEffect,
    LogAgentEffect,
    MessageRole,
    SpawnFinalStreamEffect,
    SpawnLLMWorkerEffect,
    SpawnToolWorkerEffect,
    ToolLoopUIEffect,
    TriggerNextToolEffect,
    UIEffectKind,
    UpdateActivityStateEffect,
    UpdateDocumentContextEffect,
)
from plugin.chatbot.web_research_chat import format_research_cache_result_chat
from plugin.framework.stream_batch import StreamQueueKind
from plugin.framework.client.model_fetcher import set_native_audio_support
from plugin.framework.html_stripper import StreamingHTMLStripper
from plugin.framework.config import get_config_bool
from plugin.framework.errors import (
    DocumentDisposedError,
    ToolExecutionError,
    UnoObjectError,
    best_effort,
    format_error_payload,
    is_tool_document_disposed,
    resolve_exception_message,
)
from plugin.framework.logging import agent_log, update_activity_state
from plugin.framework.queue_executor import execute_on_main_thread
from plugin.framework.send_cancellation import capture_send_stop
from plugin.framework.tool import ToolContext
from plugin.framework.worker_pool import run_in_background

log = logging.getLogger(__name__)

# Short sidebar chat labels for delegate_to_specialized_*_toolset gateway tools.
DELEGATE_GATEWAY_TOOL_NAMES = frozenset(
    {
        "delegate_to_specialized_writer_toolset",
        "delegate_to_specialized_calc_toolset",
        "delegate_to_specialized_draw_toolset",
    }
)
DELEGATE_TASK_CHAT_MAX = 120
_EMPTY_MODEL_DEBUG_CONTENT_PREVIEW_MAX = 120


def _describe_empty_response_content(content: Any) -> str:
    # crosshair: off
    if content is None:
        return "None"
    if content == "":
        return "empty"
    if not isinstance(content, str):
        content = str(content)
    if len(content) <= _EMPTY_MODEL_DEBUG_CONTENT_PREVIEW_MAX:
        return f"{len(content)} chars: {content!r}"
    preview = content[: _EMPTY_MODEL_DEBUG_CONTENT_PREVIEW_MAX - 3]
    return f"{len(content)} chars: {preview!r}..."


def _describe_empty_response_tool_calls(tool_calls: Any) -> str:
    # crosshair: off
    if tool_calls is None:
        return "none"
    if isinstance(tool_calls, list):
        return str(len(tool_calls))
    return "present"


def format_empty_model_response_debug(round_num: int, response: Mapping[str, Any]) -> str:
    """Compact API summary for sidebar when STREAM_DONE has no content and no tools."""
    # crosshair: off
    calls_note = _describe_empty_response_tool_calls(response.get("tool_calls"))
    parts = [
        f"round={round_num}",
        f"finish_reason={response.get('finish_reason')!r}",
        f"content={_describe_empty_response_content(response.get('content'))}",
        f"tool_calls={calls_note}",
    ]
    usage = response.get("usage")
    if isinstance(usage, dict) and usage:
        try:
            parts.append(f"usage={json.dumps(usage, separators=(',', ':'))}")
        except Exception:
            parts.append(f"usage={len(usage)} entries")
    images = response.get("images")
    if isinstance(images, list) and images:
        parts.append(f"images={len(images)}")
    return ", ".join(parts)


def is_delegate_gateway(func_name: str) -> bool:
    return func_name in DELEGATE_GATEWAY_TOOL_NAMES


def domain_from_delegate_args(func_args: Mapping[str, Any]) -> str:
    # crosshair: off
    domain = func_args.get("domain")
    if isinstance(domain, str) and domain.strip():
        return domain.strip()
    return "?"


def delegate_status_label(func_args: Mapping[str, Any]) -> str:
    # crosshair: off
    return f"delegate ({domain_from_delegate_args(func_args)})"


def _truncate_delegate_task(task: str, max_len: int = DELEGATE_TASK_CHAT_MAX) -> str:
    # crosshair: off
    # Display-only helper. Chat may show 120 chars; specialize still gets
    # kwargs["task"] intact (specialized_base.execute).
    one_line = task.replace("\n", " ").replace("\r", " ").strip()
    if len(one_line) <= max_len:
        return one_line
    return one_line[: max_len - 3] + "..."


def format_delegate_running_chat_line(func_args: Mapping[str, Any]) -> str:
    """One-line chat preview when a delegate gateway tool starts."""
    # crosshair: off
    domain = domain_from_delegate_args(func_args)
    raw_task = func_args.get("task")
    if raw_task is None:
        task_preview = ""
    elif isinstance(raw_task, str):
        task_preview = _truncate_delegate_task(raw_task)
    else:
        task_preview = _truncate_delegate_task(str(raw_task))
    if task_preview:
        return f"[Running delegate ({domain}): {task_preview}]\n"
    return f"[Running delegate ({domain})...]\n"


def _result_note(result_data: Mapping[str, Any]) -> str:
    """Chat label for a tool result.

    One label. ``dict.get("message", fallback)`` returns None when the
    key is present and null. A null or empty message falls through to
    Unknown error, status, or done.
    """
    # crosshair: off
    if result_data.get("status") == "error":
        return result_data.get("message") or "Unknown error"
    return result_data.get("message") or result_data.get("status") or "done"


def format_delegate_result_chat_line(func_args: Mapping[str, Any], result_data: Mapping[str, Any]) -> str:
    # crosshair: off
    """Completion line for delegate gateway tools (domain shown; success is short)."""
    domain = domain_from_delegate_args(func_args)
    if result_data.get("status") == "error":
        return f"[delegate ({domain}) failed: {_result_note(result_data)}]\n"
    if result_data.get("status") == "incomplete":
        return f"[delegate ({domain}) incomplete: {_result_note(result_data)}]\n"
    cache_block = format_research_cache_result_chat(result_data) if domain == "web_research" else ""
    return cache_block + f"[delegate ({domain}): done]\n"


def format_tool_running_ui(func_name: str, func_args: Mapping[str, Any]) -> tuple[str, str]:
    """Status bar text and chat run-line when a tool starts executing."""
    # crosshair: off
    if is_delegate_gateway(func_name):
        return f"Running: {delegate_status_label(func_args)}", format_delegate_running_chat_line(func_args)
    if func_name == "upsert_memory":
        return f"Running: {func_name}", format_upsert_memory_chat_line(func_args)
    return f"Running: {func_name}", f"[Running tool: {func_name}...]\n"


def format_tool_result_chat_text(func_name: str, func_args: Mapping[str, Any], result_data: Mapping[str, Any]) -> str:
    """Chat append body for a tool result (error or success); does not mutate *result_data*."""
    # crosshair: off
    note = _result_note(result_data)
    if result_data.get("status") == "error":
        if is_delegate_gateway(func_name):
            detailed_text = format_delegate_result_chat_line(func_args, result_data)
        else:
            detailed_text = f"[{func_name} failed: {note}]\n"
        raw_details = result_data.get("details", {})
        # Copy before popping traceback so callers' result_data is not mutated.
        details = dict(raw_details) if isinstance(raw_details, dict) else {}
        if details:
            tb = details.pop("traceback", None)
            if details:
                detailed_text += f"Details: {json.dumps(details, indent=2)}\n"
            if isinstance(tb, str) and tb.strip() and tb.strip() != "NoneType: None":
                detailed_text += f"Traceback:\n{tb}\n"
        return detailed_text

    if is_delegate_gateway(func_name):
        return format_delegate_result_chat_line(func_args, result_data)
    if func_name == "web_research":
        cache_block = format_research_cache_result_chat(result_data)
        return cache_block + f"[{func_name}: {note}]\n"
    return f"[{func_name}: {note}]\n"


def format_replaced_zero_debug_line(func_args_str: str) -> str:
    """Preview parameters when apply_document_content finds 0 occurrences."""
    # crosshair: off
    params_display = func_args_str if len(func_args_str) <= 800 else func_args_str[:800] + "..."
    return f"[Debug: params {params_display}]\n"


def is_replaced_zero_result(result_data: Mapping[str, Any]) -> bool:
    """True when apply_document_content reported zero replacements (structured or legacy message)."""
    # crosshair: off
    # Plain dict/str only — isinstance(str) is true for CrossHair LazyIntSymbolicStr.
    # Only a real int zero counts. ``replaced_count == 0`` is also true
    # for False, which would append the debug params line.
    if type(result_data) is not dict:
        return False
    count = result_data.get("replaced_count")
    if type(count) is int and count == 0:
        return True
    return False

# Direct execute_fn callers omit the spawn capture. Workers always pass the scope.
_SEND_SCOPE_UNSET = object()


# Stop writes this as an ordinary assistant row. It is not matched by text
# to decide whether a dead turn may still paint.
_STOP_LINE = "\n[Stopped by user]\n"
# One row per tool call that never received a result. Not a second controller.
_CANCELLED_TOOL = "Cancelled by user. The call may have completed."


class TurnController:
    """One sidebar send. Stop or the next send aborts it and drops the reference.

    This object is the turn. A generation counter, a pinned ``_apply_turn``,
    and a separate worker queue would be three copies of "which send is
    this": Stop and a newer send would leave the old object alive so the
    banner could still paint, and callbacks would keep enqueueing onto
    those queues. The queue, the HTML stripper, and the document
    model captured at spawn live here. The host does not keep a second copy.
    Workers enqueue on this object and do not append to ``session.messages``.
    ``abort`` makes later ``put`` calls no-ops and drops any text still in
    the batcher. The host holds at most one (``_turn``). The sidebar paints
    ``session.messages``; streamed tokens are the open row on that list.
    """

    mode: str
    session: Any
    messages: Any
    queue: Any
    batcher: Any
    model: Any
    text_model: str | None
    endpoint: str | None
    stripper: StreamingHTMLStripper | None
    _alive: bool
    _stop_banner_appended: bool
    _stop_committed: bool
    _stop_partial_text: str | None
    closed_by_document: bool
    send_start_msg_count: int
    _overflow_compact_attempts: int
    _last_compact_reason: str | None
    _last_compact_tokens_before: int | None
    _last_compact_tokens_after: int | None
    active_client: "LlmClient | None"
    active_max_tokens: int
    active_tools: list[dict[str, Any]] | None
    active_execute_tool_fn: Callable[..., Any] | None
    active_query_text: str | None
    active_supports_status: bool

    def __init__(
        self,
        session: Any,
        mode: str,
        model: Any = None,
        text_model: str | None = None,
        endpoint: str | None = None,
    ) -> None:
        self.mode = str(mode or "")
        self.session = session
        self.messages = getattr(session, "messages", None) if session is not None else None
        self.send_start_msg_count = len(self.messages) if isinstance(self.messages, list) else 0
        self.queue = None
        self.batcher = None
        self.model = model
        self.text_model = text_model
        self.endpoint = endpoint
        self.stripper = StreamingHTMLStripper()
        self._alive = True
        self._stop_banner_appended = False
        self._stop_committed = False
        self._stop_partial_text = None
        self.closed_by_document = False
        self._overflow_compact_attempts = 0
        self._last_compact_reason = None
        self._last_compact_tokens_before = None
        self._last_compact_tokens_after = None
        self.active_client = None
        self.active_max_tokens = 0
        self.active_tools = None
        self.active_execute_tool_fn = None
        self.active_query_text = None
        self.active_supports_status = False

    @property
    def alive(self) -> bool:
        return self._alive

    def same_messages(self) -> bool:
        """Clear replaces the list. A write against the old list must not land."""
        if self.session is None:
            return True
        return getattr(self.session, "messages", None) is self.messages

    def stored_user_row(self) -> bool:
        """True when this turn appended a user row after the snapshot taken in __init__.

        Rows already on the list at begin_send_turn do not count.
        """
        if not self.same_messages() or not isinstance(self.messages, list):
            return False
        return any(
            isinstance(msg, dict) and msg.get("role") == "user"
            for msg in self.messages[self.send_start_msg_count :]
        )

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
        """Enqueue only while this turn is alive.

        After abort the item is dropped. It is not parked on a queue for a
        later drain. This used to write the raw queue, so a control item
        could pass text still in the batcher, and callers had to flush by hand.
        """
        if not self._alive:
            return False
        # This used to write the raw queue, so a control item could pass text
        # still in the batcher, and callers had to flush by hand.
        target = spawn_queue(self)
        if target is None:
            return False
        target.put(item)
        return True

    def can_write(self, host: Any) -> bool:
        """A transcript write is allowed only for the host's current turn when open and not closed by document.

        Stop has already aborted the turn. The close still stores here until
        a newer send replaces ``_turn`` or Clear replaces the list. A dead
        turn that is no longer current, or whose list identity changed, refuses.
        """
        if self.closed_by_document or self.session is None:
            return False
        if current_turn(host) is not self:
            return False
        return self.same_messages()

    def fold_chunk(self, host: Any, text: str, role: str = "assistant") -> bool:
        """Append *text* to this turn's list. Workers must not call this."""
        if not text or not self.can_write(host):
            return False
        assert self.session is not None
        from plugin.chatbot.rich_text_paste import fold_transcript_chunk

        return fold_transcript_chunk(self.session, text, role)

    def persist_assistant(
        self,
        host: Any,
        content: Any = None,
        tool_calls: Any = None,
        reasoning_replay: Any = None,
    ) -> None:
        if not self.can_write(host):
            return
        assert self.session is not None
        kwargs: dict[str, Any] = {}
        if tool_calls is not None:
            kwargs["tool_calls"] = tool_calls
        if reasoning_replay is not None:
            kwargs["reasoning_replay"] = reasoning_replay
        self.session.add_assistant_message(content=content, **kwargs)

    def persist_tool(self, host: Any, call_id: str | None, content: Any) -> None:
        if not self.can_write(host):
            return
        assert self.session is not None
        self.session.add_tool_result(call_id, content)

    def take_stripper_tail(self) -> str:
        """Finish the HTML stripper and return any held fragment.

        Stop aborts the turn before the drain's finalize runs. The fragment
        was still in the stripper, so the stored answer dropped the tail of
        an unclosed tag.
        """
        stripper = self.stripper
        self.stripper = None
        if stripper is None:
            return ""
        leftover = best_effort(stripper.finalize, "stripper finalize on stop failed", default="")
        return leftover if isinstance(leftover, str) else ""

    def close_stopped(self, host: Any, partial: str | None = None) -> None:
        """Finalise this turn after Stop.

        A running tool cannot be killed. Enqueue is already a no-op, so its
        later result is discarded. Document side effects may still land.
        Rows already written stay. The open assistant row is committed, each
        tool call with no tool row gets one cancelled row, and the stop line
        is an ordinary assistant message. ``tool_calls`` are not removed.
        """
        if not self.can_write(host):
            return
        assert self.session is not None
        # A second call sees a finished stripper and a last row that is not
        # _open_transcript, so open_text() is empty and it stores another
        # "No response." plus another _STOP_LINE (including when the first
        # call had already stored the open row). send_handlers.on_stopped and
        # tool_loop._handle_stream_stopped both call this; the drain invokes
        # on_stopped once, and the turn did not. _stop_committed is not
        # _stop_banner_appended.
        if self._stop_committed:
            return
        self._stop_committed = True
        tail = self.take_stripper_tail()
        if tail:
            self.fold_chunk(host, tail, "assistant")
        emitted = self.open_text()
        fallback = partial.strip() if isinstance(partial, str) else ""
        if emitted:
            chosen = emitted
        elif fallback and fallback != "No response.":
            chosen = fallback
        else:
            chosen = ""
        if chosen:
            self.persist_assistant(host, content=chosen)
        messages = self.messages if isinstance(self.messages, list) and self.same_messages() else None
        closed = _append_cancelled_tool_rows(messages) if messages is not None else 0
        if not chosen and closed == 0:
            self.persist_assistant(host, content="No response.")
        self.persist_assistant(host, content=_STOP_LINE)


def _discard_batcher(batcher: Any) -> None:
    """Drop a producer batch so its timer cannot emit after the turn is gone."""
    discard = getattr(batcher, "discard", None)
    if callable(discard):
        discard()


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


# current_turn: host._turn while it is a TurnController, including after
# abort, until drop_turn. Not a liveness check.
# running_turn: that turn when it is still alive and can write.
# Spawn and in-flight work. None after Stop, Clear, or a newer send.
# session_for_turn: the session when this turn is still current and can write,
# including an aborted turn. Close, TTS, and other transcript reads. None after
# Clear or a newer send.
def current_turn(host: Any) -> TurnController | None:
    turn = getattr(host, "_turn", None)
    if isinstance(turn, TurnController):
        return turn
    return None


def finalize_sidebar_assistant_response(listener: Any, *, allow_rerender: bool = True) -> None:
    """Re-import the last assistant message as HTML when rich sidebar is active.

    Skip HTML rerender after an API error. Drain ERROR appends ``[API error: …]``
    after ``_assistant_stream_start_len``; rerender looks up the previous HTML
    assistant message and ``truncate_control_from`` would delete that error line
    (Packet F HTTP 429/500 looked like a leftover hello).

    Skip rerender after Stop. The Stop path already stored the partial answer
    and the ``[Stopped by user]`` row, then painted that list. A second paint
    is not required. The old splice truncated the tail and pasted the answer
    without the marker (Packet B1).

    Empty / truncated STREAM_DONE must AddMessageEffect the banner (see
    ``tool_loop_state``) so this path does not paste the previous HTML assistant
    over ``[Response truncated]`` / ``[No text from model]`` (Packet C).
    """
    # Append a leftover unclosed tag when the rerender did not replace
    # the control. The Error path clears the stripper without finalize(),
    # and the default sidebar has the rich widget, so Stop and error would
    # drop that tail. A successful rerender already replaced it from the
    # session.
    replaced = False
    if getattr(listener, "_terminal_status", None) != "Error" and allow_rerender:
        rerender = getattr(listener, "rerender_rich_text_session", None)
        if callable(rerender):
            replaced = rerender() is True

    turn = current_turn(listener)
    if isinstance(turn, TurnController) and not turn.same_messages():
        return
    stripper = turn.stripper if isinstance(turn, TurnController) else None
    if stripper is not None and isinstance(turn, TurnController):
        leftover = turn.take_stripper_tail()
        if leftover and not replaced:
            listener._append_response(leftover, role="assistant")

    # Append _STOP_LINE to the control for this stopped turn. Skipping
    # rerender keeps the ramble from painting over the banner, and
    # close_stopped only persisted the line on session.messages, so the
    # control never drew [Stopped by user].
    stop_requested = getattr(listener, "stop_requested", False) or not allow_rerender
    if stop_requested and getattr(listener, "_terminal_status", None) != "Error":
        if isinstance(turn, TurnController):
            if getattr(turn, "_stop_banner_appended", False):
                return
            turn._stop_banner_appended = True
        from plugin.chatbot.dialogs import get_control_text, set_control_text

        widget = getattr(listener, "rich_text_widget", None)
        if widget is not None:
            run_rich = getattr(listener, "_run_rich_ui", None)
            if callable(run_rich):
                run_rich(lambda: widget.append_chunk(_STOP_LINE))
            else:
                widget.append_chunk(_STOP_LINE)
        else:
            control = getattr(listener, "response_control", None)
            if control is not None and control.getModel():
                cur = get_control_text(control, default="") or ""
                set_control_text(control, cur + _STOP_LINE)


def abort_turn(host: Any) -> None:
    """Stop, a mode change, or dispose. The host still names this turn until ``drop_turn``."""
    turn = current_turn(host)
    if turn is not None:
        turn.abort()


def drop_turn(host: Any) -> None:
    """The drain has finished. Forget the turn so a late callback cannot find it."""
    turn = current_turn(host)
    if turn is None:
        return
    turn.abort()
    if current_turn(host) is turn:
        host._turn = None


def begin_send_turn(host: Any, mode: str, model: Any = None) -> TurnController:
    """Start a send. The previous turn, if any, is aborted and replaced.

    Called before any worker spawns. There is no later rebind: Clear and a
    mode change abort this turn, then replace the list or the session.
    """
    previous = current_turn(host)
    if previous is not None:
        previous.abort()
    session = getattr(host, "session", None)
    turn = TurnController(session, mode, model)
    host._turn = turn
    return turn


def running_turn(host: Any) -> TurnController | None:
    """The live turn, or None once Stop, Clear, or a new send has ended it."""
    turn = current_turn(host)
    if isinstance(turn, TurnController) and turn.alive and turn.can_write(host):
        return turn
    return None


def session_for_turn(host: Any) -> Any:
    """The session this send bound. None when the turn is gone or the list changed."""
    turn = current_turn(host)
    if isinstance(turn, TurnController) and turn.can_write(host):
        assert turn.session is not None
        return turn.session
    return None


def turn_session_or_stop(host: Any) -> Any:
    """The session for this send. None when the turn is already over.

    ``_do_send`` starts the turn before this runs. A direct call with no
    turn yet starts one. An aborted turn is not replaced: that rebind wrote
    the reply onto a session the turn did not start with.
    """
    if current_turn(host) is None:
        begin_send_turn(host, "")
    turn = running_turn(host)
    if turn is None:
        return None
    return turn.session


def spawn_queue(turn: TurnController) -> Any:
    """The queue the drain attached. The batcher wraps it when batching is on."""
    return turn.batcher or turn.queue


def _tool_call_id(call: Any) -> str:
    if not isinstance(call, dict):
        return ""
    call_id = call.get("id")
    return call_id if isinstance(call_id, str) else ""


def _append_cancelled_tool_rows(messages: list[Any]) -> int:
    """Append one cancelled tool row for each call that has none.

    The row is inserted with that assistant message's other tool rows so
    the pair stays adjacent. ``tool_calls`` on the assistant message stay.

    ``answered`` is scoped to one assistant message. A global set treats a
    reused tool-call id (providers emit ``call_0`` again on the next turn)
    as already answered, skips the synthetic cancelled row, and the next
    request comes back 400. Scan only the contiguous tool rows that follow
    that assistant message.
    """
    added = 0
    index = 0
    while index < len(messages):
        msg = messages[index]
        calls = msg.get("tool_calls") if isinstance(msg, dict) and msg.get("role") == "assistant" else None
        if not isinstance(calls, list) or not calls:
            index += 1
            continue
        answered: set[str] = set()
        insert_at = index + 1
        while (
            insert_at < len(messages)
            and isinstance(messages[insert_at], dict)
            and messages[insert_at].get("role") == "tool"
        ):
            tid = messages[insert_at].get("tool_call_id")
            if isinstance(tid, str) and tid:
                answered.add(tid)
            insert_at += 1
        for call in calls:
            call_id = _tool_call_id(call)
            if not call_id or call_id in answered:
                continue
            messages.insert(
                insert_at,
                {"role": "tool", "tool_call_id": call_id, "content": _CANCELLED_TOOL},
            )
            answered.add(call_id)
            insert_at += 1
            added += 1
        index = insert_at
    return added


def emit_for_host(host: Any, item: Any) -> bool:
    """Enqueue on the current turn. A dead turn drops the item."""
    turn = current_turn(host)
    if not isinstance(turn, TurnController):
        return False
    return turn.put(item)


def put_for_turn(turn: Any, item: Any) -> bool:
    """Workers enqueue on the controller they captured. They do not touch the transcript.

    Callers enqueue on the turn they captured. There is no ``host`` or
    ``q`` parameter to write through.
    A worker must not assign ``turn.queue``; the drain attached the queue before spawn.
    After abort, ``put`` is a no-op.
    """
    if not isinstance(turn, TurnController):
        return False
    return turn.put(item)


def persist_assistant_on_turn(
    host: Any,
    content: Any = None,
    tool_calls: Any = None,
    reasoning_replay: Any = None,
) -> None:
    turn = current_turn(host)
    if not isinstance(turn, TurnController):
        return
    turn.persist_assistant(host, content=content, tool_calls=tool_calls, reasoning_replay=reasoning_replay)


def _queue_tool_failure(host: Any, call_id: str, func_name: str, func_args_str: str, exc: BaseException, turn: Any = None, *, model: Any) -> None:
    """Queue a tool failure. A disposed document ends the loop.

    A closed document is not a normal tool error. Turning every exception
    into a JSON payload and queuing ``TOOL_DONE`` lets the loop continue.
    ``is_tool_document_disposed`` is the tool-boundary check;
    ``is_disposed_exception`` also matches a bare ``RuntimeException``
    from a live document.

    Classify against ``model``, the document closed over at spawn. Reading
    ``host._active_model`` when the worker finishes scores the failure
    against a later send's live document, so a bare ``RuntimeException``
    fails ``is_tool_document_disposed`` and is queued as ``TOOL_DONE``.
    The same capture keeps the tool from running on the new send.
    """
    if turn is None:
        turn = current_turn(host)
    formatted = format_error_payload(exc)
    if is_tool_document_disposed(exc, model):
        put_for_turn(turn, (StreamQueueKind.ERROR, formatted))
        return
    put_for_turn(turn, (StreamQueueKind.TOOL_DONE, call_id, func_name, func_args_str, json.dumps(formatted, default=str)))


def persist_tool_on_turn(host: Any, call_id: str | None, content: Any) -> None:
    turn = current_turn(host)
    if not isinstance(turn, TurnController):
        return
    turn.persist_tool(host, call_id, content)


class ToolLoopActionHost(Protocol):
    ctx: Any
    session: Any
    image_model_selector: Any
    audio_wav_path: str | None
    _terminal_status: str

    def _append_response(self, text: str, is_thinking: bool = False, role: str = "assistant") -> None: ...
    def _set_status(self, text: str) -> None: ...
    def _get_document_model(self) -> Any: ...
    _tool_calling: Any
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
        send_cancellation: Any = _SEND_SCOPE_UNSET,
    ) -> str:
        from plugin.main import get_tools as _get_tools

        approval_cb: Any = None
        chat_append_cb: Any = None
        if not isinstance(args, dict) and args is not None:
            err = ToolExecutionError("Tool arguments must be a dictionary", code="TOOL_ARGS_INVALID")
            return json.dumps(format_error_payload(err), default=str)
        safe_args = args if isinstance(args, dict) else {}

        delegate_domain = str(safe_args.get("domain") or "") if name in DELEGATE_GATEWAY_TOOL_NAMES else ""
        # Delegate gateways forward domain=web_research to WebResearchTool with the same ctx;
        # they must receive the same HITL wiring as the outer web_research tool.
        needs_web_research_ui = name == "web_research" or delegate_domain == "web_research"
        needs_document_research_ui = delegate_domain == "document_research"
        if needs_web_research_ui or needs_document_research_ui:

            def _subagent_target() -> TurnController | None:
                # Enqueue on the turn this worker captured at spawn.
                # Putting chat lines or the approval dialog on the host queue
                # when the callback runs lands them on the next turn after
                # Stop or a new send replaces that queue. A dead turn drops
                # the item.
                if isinstance(captured_turn, TurnController):
                    return captured_turn
                return None

            def _sub_agent_chat_append(text: str) -> None:
                emit_turn = _subagent_target()
                if not put_for_turn(emit_turn, (StreamQueueKind.CHUNK, text)):
                    return

            chat_append_cb = _sub_agent_chat_append

            try:
                if needs_web_research_ui and get_config_bool("chatbot.prompt_for_web_research"):

                    def _web_approval(query_for_engine: str, tool_name: str, args: Any) -> Any:
                        emit_turn = _subagent_target()
                        if not isinstance(emit_turn, TurnController) or emit_turn.queue is None:
                            log.warning("tool_loop: web_research approval skipped (queue missing)")
                            return (False, None)
                        event = threading.Event()
                        # Use setattr/getattr to avoid static attribute errors on Event.
                        setattr(event, "approved", False)
                        setattr(event, "query_override", None)
                        from plugin.framework.send_cancellation import wait_for_approval

                        if not put_for_turn(emit_turn, (StreamQueueKind.APPROVAL_REQUIRED, query_for_engine, tool_name, event)):
                            return (False, None)
                        # Workers pass the checker captured at spawn. Direct
                        # callers (tests) omit it and still run on that thread.
                        checker = stop_checker if stop_checker is not None else host.resolve_stop_checker()
                        # event.wait() ignored Stop. Sidebar close latches the
                        # checker and never sets the event, so this worker parked.
                        if not wait_for_approval(event, checker):
                            put_for_turn(emit_turn, (StreamQueueKind.STOPPED,))
                            return (False, None)
                        if not getattr(event, "approved", False):
                            put_for_turn(emit_turn, (StreamQueueKind.STOPPED,))
                        return (bool(getattr(event, "approved", False)), getattr(event, "query_override", None))

                    approval_cb = _web_approval
            except Exception as ex:
                # Fail closed. Leaving approval_cb as None after a config
                # error skips Accept/Change/Reject: web_research prompts only
                # when both the config flag and the callback are set, so the
                # search would run without approval.
                log.warning("tool_loop: web_research approval setup failed: %s", ex)
                err = ToolExecutionError(
                    "Web research approval could not be shown. The search was not started.",
                    code="WEB_RESEARCH_APPROVAL_UNAVAILABLE",
                )
                return json.dumps(format_error_payload(err), default=str)

        active_page_idx = None
        if doc_type_str in ("draw", "impress"):
            from plugin.draw.bridge import DrawBridge

            # Async gateways (delegate_to_specialized_draw_toolset) run
            # execute_fn on the worker; hasattr(doc, "getDrawPages") is UNO.
            active_page_idx = best_effort(
                lambda: execute_on_main_thread(lambda: DrawBridge(doc).get_active_page_index()),
                f"execute_fn: failed to get active page index for {doc_type_str}",
            )

        # The spawn passes the scope it captured. Reading
        # host._send_cancellation when the tool runs registers on the next
        # send: Stop clears the field and the next send stores a new scope.
        # Omitted means a direct caller, not a delayed worker, and there
        # is no scope.
        cancel_scope = None if send_cancellation is _SEND_SCOPE_UNSET else send_cancellation

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
        # ToolRegistry.execute binds keyword-only bypass_thread_guard (and
        # ctx/tool_name) from **safe_args, so a model argument can skip
        # execute_safe or collide with those parameters. Copy so the stored
        # tool-call dict stays intact, drop the keys, and pass False. A
        # chat argument must not set the eval-harness switch.
        call_args = safe_args
        if "bypass_thread_guard" in call_args or "ctx" in call_args or "tool_name" in call_args:
            call_args = {key: value for key, value in call_args.items() if key not in ("bypass_thread_guard", "ctx", "tool_name")}
        res = _get_tools().execute(name, tctx, bypass_thread_guard=False, **call_args)
        # execute_safe turns a disposed document into a DOCUMENT_DISPOSED
        # dict. Returning that JSON queues TOOL_DONE and the loop keeps
        # going on a dead document. Raise instead. The raise below is
        # classified with this same ``doc`` (the document passed in at
        # spawn) in ``_queue_tool_failure``.
        if isinstance(res, dict) and res.get("code") == "DOCUMENT_DISPOSED":
            message = res.get("message")
            text = message.strip() if isinstance(message, str) and message.strip() else "Document was closed or disposed by LibreOffice"
            raise DocumentDisposedError(text)
        return json.dumps(res, default=str) if isinstance(res, dict) else str(res)

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
            turn = running_turn(host)
            q = spawn_queue(turn) if turn is not None else None
            if turn is None or q is None or turn.active_client is None:
                return True
            host._tool_calling._spawn_final_stream(q, turn.active_client, turn.active_max_tokens)
        elif isinstance(effect, UpdateDocumentContextEffect):
            if self._refresh_document_context():
                return True
        elif isinstance(effect, ToolLoopUIEffect):
            self._execute_ui_effect(effect)
        elif isinstance(effect, LogAgentEffect):
            agent_log(effect.location, effect.message, data=effect.data)
        elif isinstance(effect, AddMessageEffect):
            self._add_message(effect)
        elif isinstance(effect, SpawnLLMWorkerEffect):
            turn = running_turn(host)
            q = spawn_queue(turn) if turn is not None else None
            if turn is None or q is None or turn.active_client is None:
                return True
            host._tool_calling._refresh_active_tools_for_session()
            host._tool_calling._spawn_llm_worker(q, turn.active_client, turn.active_max_tokens, turn.active_tools or [], effect.round_num, query_text=turn.active_query_text)
        elif isinstance(effect, UpdateActivityStateEffect):
            self._update_activity_state(effect)
        elif isinstance(effect, CleanupAudioEffect):
            self._cleanup_audio()
        elif isinstance(effect, SpawnToolWorkerEffect):
            if self._spawn_tool_worker(effect):
                return True
        return False

    def _refresh_document_context(self) -> bool:
        """Refresh the document snapshot. Return True when the drain should stop.

        A missing or disposed document used to be logged at debug and the
        tool loop kept going with the previous snapshot. That is the same
        failure _do_send already ends on.
        """
        host = self.host
        turn = running_turn(host)
        session = turn.session if turn is not None else None
        if session is None:
            # Clear replaced the message list, or the turn was aborted.
            # Do not write [DOCUMENT CONTENT] onto the wiped chat.
            return True
        doc = None
        try:
            # The frame document is the live snapshot. The model captured on
            # the turn is what tool workers run against. Every action host has
            # the method; falling back to turn.model would refresh from the spawn
            # capture.
            doc = host._get_document_model()
            if not doc:
                raise UnoObjectError("Document closed or unavailable.", code="DOCUMENT_UNAVAILABLE")
            session.refresh_document_context(doc, host.ctx)
            return False
        except Exception as exc:
            if isinstance(exc, UnoObjectError) or is_tool_document_disposed(exc, doc):
                log.debug("Tool loop: document disposed during context refresh", exc_info=True)
                host._append_response("\n[Document closed or unavailable.]\n")
            else:
                log.exception("Tool loop: failed to refresh document context after mutating tool")
                host._append_response(f"\n[Error reading document: {resolve_exception_message(exc)}]\n")
            host._terminal_status = "Error"
            host._set_status("Error")
            return True

    def _execute_ui_effect(self, effect: ToolLoopUIEffect) -> None:
        host = self.host
        if effect.kind == UIEffectKind.APPEND:
            host._append_response(effect.text)
        elif effect.kind == UIEffectKind.STATUS:
            host._set_status(effect.text)
            if effect.text in ("Stopped", "Ready", "Error"):
                host._terminal_status = effect.text
        elif effect.kind == UIEffectKind.TOOL_RUNNING:
            status_text, run_line = format_tool_running_ui(effect.func_name, effect.func_args or {})
            host._set_status(status_text)
            host._append_response(run_line)
        elif effect.kind == UIEffectKind.TOOL_RESULT:
            func_args = effect.func_args or {}
            result_data = effect.result_data or {}
            chat_text = format_tool_result_chat_text(effect.func_name, func_args, result_data)
            host._append_response(chat_text)
            if effect.func_name == "apply_document_content" and is_replaced_zero_result(result_data):
                host._append_response(format_replaced_zero_debug_line(effect.func_args_str))
        elif effect.kind == UIEffectKind.EMPTY_MODEL_DEBUG:
            debug = "[Debug: %s]" % format_empty_model_response_debug(effect.round_num, effect.response or {})
            host._append_response("\n" + debug + "\n")
            log.warning("Tool loop: no assistant text from model: %s", debug.strip())
        elif effect.kind == UIEffectKind.DEBUG:
            log.debug(effect.text)

    def _add_message(self, effect: AddMessageEffect) -> None:
        content = effect.content
        if effect.role == MessageRole.ASSISTANT and content is None and effect.response is not None:
            banner = "[No text from model; any tool changes were still applied.]"
            debug = "[Debug: %s]" % format_empty_model_response_debug(effect.round_num, effect.response)
            content = banner + "\n" + debug

        if effect.role == MessageRole.ASSISTANT:
            persist_assistant_on_turn(
                self.host,
                content=content,
                tool_calls=effect.tool_calls,
                reasoning_replay=effect.reasoning_replay,
            )
        elif effect.role == MessageRole.TOOL:
            persist_tool_on_turn(self.host, effect.call_id, content)

    def _update_activity_state(self, effect: UpdateActivityStateEffect) -> None:
        if effect.action == "tool_execute":
            update_activity_state("tool_execute", round_num=effect.round_num, tool_name=effect.tool_name)
        elif effect.action == "exhausted_rounds":
            update_activity_state("exhausted_rounds")

    def _cleanup_audio(self) -> None:
        """Clean up recording file and record audio support for the captured model.

        current_turn, not running_turn: Stop has already aborted the turn when
        this effect runs, and the model that streamed audio is still that turn
        until drop_turn.

        Read model and endpoint from the turn captured at spawn.
        ``get_text_model()`` and ``get_current_endpoint()`` at cleanup time
        follow the combobox, so a mid-send model change marks the new model
        as supporting native audio.
        """
        host = self.host
        turn = current_turn(host)
        if turn is not None and turn.text_model:
            set_native_audio_support(turn.text_model, turn.endpoint, supported=True)

        try:
            if host.audio_wav_path:
                os.remove(host.audio_wav_path)
        except Exception:
            log.exception("Tool loop: failed to remove audio_wav_path")
        host.audio_wav_path = None

    def _spawn_tool_worker(self, effect: SpawnToolWorkerEffect) -> bool:
        host = self.host
        func_name = effect.func_name
        func_args_str = effect.func_args_str
        func_args = effect.func_args
        call_id = effect.call_id
        # The drain attached the queue and the document before this spawn.
        # A later send replaces the host's turn. This worker keeps the one
        # it captured and must not assign that turn's queue.
        turn = running_turn(host)
        if not isinstance(turn, TurnController):
            return True
        model = turn.model

        def emit(item: Any) -> None:
            put_for_turn(turn, item)

        # Close over the execute function and the document this spawn
        # already had. Reading them when the thread runs lets a new send
        # replace both while this tool is still in flight, so the old call
        # runs against the new send. The failure path closes over the same
        # values.
        execute_tool_fn = turn.active_execute_tool_fn
        if execute_tool_fn is None:
            return True
        # Same object execute_fn receives as ``doc``. It was captured on the
        # turn at spawn. A later send must not retarget this call or the
        # disposed-document check.
        spawn_doc = model
        supports_status = turn.active_supports_status
        # Scope and checker from this send. execute_fn must not read the
        # panel field when the tool later runs.
        bound_scope, bound_stop = capture_send_stop(host)

        # Bugfix: getText ran for every tool whenever the sidebar combobox
        # existed, and execute_on_main_thread re-raises, so a disposed or
        # unwired control left _spawn_tool_worker and the drain treated it as
        # a stream error. Only image_generate uses the override; a widget
        # failure leaves that call's arguments unchanged.
        if func_name == "image_generate" and isinstance(func_args, dict) and host.image_model_selector:
            image_model_override = best_effort(
                lambda: execute_on_main_thread(lambda: host.image_model_selector.getText()),
                "image model selector unusable; image_generate keeps its arguments",
            )
            if image_model_override:
                # Execution uses the sidebar override; func_args_str stays the
                # model arguments so TOOL_DONE and history do not record the UI
                # override.
                func_args = dict(func_args)
                func_args["image_model"] = image_model_override

        def tool_status_callback(msg: str) -> None:
            emit((StreamQueueKind.STATUS, msg))

        def run_tool() -> None:
            t0 = time.perf_counter()
            sync = not effect.is_async
            if sync:
                log.debug("sync tool start name=%s", func_name)
            try:
                # Stop can land after spawn and before this body. Do not start
                # the tool; the drain's on_stopped path closes the turn.
                if bound_stop() or not turn.alive:
                    emit((StreamQueueKind.STOPPED,))
                    return

                call_kwargs: dict[str, Any] = {
                    "stop_checker": bound_stop,
                    "send_cancellation": bound_scope,
                    "captured_turn": turn,
                }
                if supports_status:
                    call_kwargs["status_callback"] = tool_status_callback
                    if effect.is_async:

                        def tool_thinking_callback(msg: str) -> None:
                            emit((StreamQueueKind.TOOL_THINKING, msg))

                        call_kwargs["append_thinking_callback"] = tool_thinking_callback
                res = execute_tool_fn(func_name, func_args, spawn_doc, host.ctx, **call_kwargs)
                if sync:
                    log.debug("sync tool done name=%s elapsed_ms=%.1f", func_name, (time.perf_counter() - t0) * 1000.0)
                emit((StreamQueueKind.TOOL_DONE, call_id, func_name, func_args_str, res))
            except Exception as e:
                if sync:
                    log.debug("sync tool failed name=%s elapsed_ms=%.1f", func_name, (time.perf_counter() - t0) * 1000.0)
                _queue_tool_failure(host, call_id, func_name, func_args_str, e, turn, model=spawn_doc)

        # Use the same dedicated worker as async tools. Calling the tool
        # on the drain thread skips pump_ui_idle, so Stop is not delivered
        # until the tool returns and the next round can start. Sync UNO
        # still runs on the main thread: ToolRegistry.execute marshals it,
        # and the drain pumps that queue.
        worker_name = f"tool-async-{func_name}" if effect.is_async else f"tool-sync-{func_name}"
        run_in_background(run_tool, name=worker_name, dedicated=True)
        return False
