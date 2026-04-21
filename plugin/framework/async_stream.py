# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2024 John Balis
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
"""
Unified async stream orchestration for WriterAgent.
Handles both simple streaming and complex tool-calling loops with thinking/status updates.
Runs blocking API calls on worker threads and drains logic via a main-thread loop
to keep the LibreOffice UI responsive (processEventsToIdle).
"""
from __future__ import annotations

import json
import logging
import queue
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, TypeAlias, Callable

from plugin.framework.worker_pool import run_in_background

log = logging.getLogger(__name__)


from plugin.framework.errors import format_error_payload


class StreamQueueKind(StrEnum):
    """First element of stream queue tuples (producers must use these enum members)."""

    CHUNK = "chunk"
    THINKING = "thinking"
    STATUS = "status"
    STREAM_DONE = "stream_done"
    NEXT_TOOL = "next_tool"
    TOOL_DONE = "tool_done"
    TOOL_THINKING = "tool_thinking"
    APPROVAL_REQUIRED = "approval_required"
    FINAL_DONE = "final_done"
    STOPPED = "stopped"
    ERROR = "error"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"


class BlockingPumpKind(StrEnum):
    """Tags for :func:`run_blocking_in_thread` queue (not the stream drain protocol)."""

    DONE = "done"
    ERROR = "error"


def _format_agent_tool_stream_line(prefix: str, data: Any) -> str:
    """Serialize ACP tool_call / tool_result payloads for chat display."""
    try:
        if isinstance(data, (dict, list)):
            body = json.dumps(data, ensure_ascii=False)
        else:
            body = str(data) if data is not None else ""
    except Exception:
        body = str(data)
    return "\n%s %s\n" % (prefix, body)


StreamQueueItem: TypeAlias = tuple[StreamQueueKind, ...]
BlockingPumpQueueItem: TypeAlias = tuple[BlockingPumpKind, Any]


def put_stream_queue_stopped(q: queue.Queue) -> None:
    """Enqueue a user-stopped signal. Always uses (kind, payload); do not use a 1-tuple."""
    q.put((StreamQueueKind.STOPPED, None))


@dataclass(slots=True)
class _DrainState:
    """Mutable state for :func:`run_stream_drain_loop` (main thread only)."""

    q: queue.Queue[Any]
    apply_chunk_fn: Callable[[str, bool], None]
    on_stream_done: Callable[..., Any]
    on_stopped: Callable[[], None]
    on_error: Callable[[Any], None]
    on_status_fn: Callable[[str], None] | None
    on_approval_required: Callable[..., None] | None
    show_search_thinking: bool
    job_done: list[bool]
    current_content: list[Any] = field(default_factory=list)
    current_thinking: list[Any] = field(default_factory=list)
    thinking_open: list[bool] = field(default_factory=lambda: [False])

    def close_thinking(self) -> None:
        if self.thinking_open[0]:
            self.apply_chunk_fn(" /thinking\n", True)
            self.thinking_open[0] = False

    def flush_buffers(self) -> None:
        if self.current_thinking:
            if not self.thinking_open[0]:
                self.apply_chunk_fn("[Thinking] ", True)
                self.thinking_open[0] = True
            self.apply_chunk_fn("".join(self.current_thinking), True)
            self.current_thinking.clear()
        if self.current_content:
            self.close_thinking()
            self.apply_chunk_fn("".join(self.current_content), False)
            self.current_content.clear()


def _drain_batch(q: queue.Queue[Any], timeout: float) -> list[Any]:
    """Block up to *timeout* for one item, then drain any immediately available extras."""
    items: list[Any] = []
    try:
        items.append(q.get(timeout=timeout))
    except queue.Empty:
        return items
    try:
        while True:
            items.append(q.get_nowait())
    except queue.Empty:
        pass
    return items


def _handle_chunk(state: _DrainState, data: Any, _item: Any) -> None:
    if state.current_thinking:
        state.flush_buffers()
    state.current_content.append(data)


def _handle_thinking(state: _DrainState, data: Any, _item: Any) -> None:
    if state.current_content:
        state.flush_buffers()
    state.current_thinking.append(data)


def _handle_status(state: _DrainState, data: Any, _item: Any) -> None:
    if state.on_status_fn:
        state.on_status_fn(data)


def _handle_stream_done_like(state: _DrainState, _data: Any, item: Any) -> None:
    state.flush_buffers()
    state.close_thinking()
    if state.on_stream_done(item):
        state.job_done[0] = True


def _handle_tool_thinking(state: _DrainState, data: Any, _item: Any) -> None:
    if state.show_search_thinking:
        if state.current_content:
            state.flush_buffers()
        state.current_thinking.append(data)


def _handle_tool_call_line(state: _DrainState, data: Any, _item: Any) -> None:
    state.flush_buffers()
    state.close_thinking()
    state.apply_chunk_fn(
        _format_agent_tool_stream_line("[Tool call]", data),
        False,
    )


def _handle_tool_result_line(state: _DrainState, data: Any, _item: Any) -> None:
    state.flush_buffers()
    state.close_thinking()
    state.apply_chunk_fn(
        _format_agent_tool_stream_line("[Tool result]", data),
        False,
    )


def _handle_approval_required(state: _DrainState, _data: Any, item: Any) -> None:
    state.flush_buffers()
    state.close_thinking()
    if state.on_approval_required:
        try:
            state.on_approval_required(item)
        except Exception as e:
            log.error("approval_required handler: %s" % e)


def _handle_stopped(state: _DrainState, _data: Any, _item: Any) -> None:
    state.flush_buffers()
    state.close_thinking()
    state.on_stopped()
    state.job_done[0] = True


def _handle_error(state: _DrainState, data: Any, _item: Any) -> None:
    state.flush_buffers()
    state.close_thinking()
    state.on_error(data)
    state.job_done[0] = True


_DISPATCH: dict[StreamQueueKind, Callable[[_DrainState, Any, Any], None]] = {
    StreamQueueKind.CHUNK: _handle_chunk,
    StreamQueueKind.THINKING: _handle_thinking,
    StreamQueueKind.STATUS: _handle_status,
    StreamQueueKind.STREAM_DONE: _handle_stream_done_like,
    StreamQueueKind.TOOL_DONE: _handle_stream_done_like,
    StreamQueueKind.FINAL_DONE: _handle_stream_done_like,
    StreamQueueKind.NEXT_TOOL: _handle_stream_done_like,
    StreamQueueKind.TOOL_THINKING: _handle_tool_thinking,
    StreamQueueKind.TOOL_CALL: _handle_tool_call_line,
    StreamQueueKind.TOOL_RESULT: _handle_tool_result_line,
    StreamQueueKind.APPROVAL_REQUIRED: _handle_approval_required,
    StreamQueueKind.STOPPED: _handle_stopped,
    StreamQueueKind.ERROR: _handle_error,
}


def _process_batch(
    state: _DrainState,
    items: list[Any],
    stop_checker: Callable[[], bool] | None,
) -> None:
    for item in items:
        if stop_checker and stop_checker():
            log.info("run_stream_drain_loop: Stop requested via checker.")
            state.flush_buffers()
            state.close_thinking()
            state.on_stopped()
            state.job_done[0] = True
            break

        raw_kind = item[0] if isinstance(item, (tuple, list)) else item
        data = item[1] if isinstance(item, (tuple, list)) and len(item) > 1 else None

        try:
            if not isinstance(raw_kind, StreamQueueKind):
                ek = TypeError(
                    "stream queue item kind must be StreamQueueKind, got %s"
                    % (type(raw_kind).__name__,)
                )
                log.error("Invalid stream queue tag: %s", ek)
                state.flush_buffers()
                state.close_thinking()
                state.on_error(format_error_payload(ek))
                state.job_done[0] = True
                break

            _DISPATCH[raw_kind](state, data, item)
        except Exception as loop_e:
            error_payload = format_error_payload(loop_e)
            log.error("Stream processing error: %s" % error_payload)
            state.q.put((StreamQueueKind.ERROR, error_payload))

        if state.job_done[0]:
            break

    state.flush_buffers()


def run_stream_drain_loop(
    q,
    toolkit,
    job_done,
    apply_chunk_fn,
    on_stream_done,
    on_stopped,
    on_error,
    on_status_fn=None,
    ctx=None,
    show_search_thinking=False,
    on_approval_required=None,
    stop_checker=None,
):
    """
    Main-thread drain loop: batches items from queue, manages thinking/chunk buffers,
    and dispatches to callbacks. Keeps UI responsive via processEventsToIdle().
    Includes comprehensive error handling to prevent UI thread crashes.

    Supported queue items (kind, *args); kind must be :class:`StreamQueueKind`:
    - (CHUNK, text): Applied via apply_chunk_fn(text, is_thinking=False).
    - (THINKING, text): Applied via apply_chunk_fn(text, is_thinking=True).
    - (STATUS, text): Passed to on_status_fn(text).
    - (STREAM_DONE, response): Calls on_stream_done(item). Returns True if job finished.
    - (NEXT_TOOL,): Internal trigger for multi-round loops.
    - (TOOL_DONE, call_id, func_name, args_str, res): Handled by orchestration (if used).
    - (TOOL_THINKING, text): Thinking tokens from a tool (e.g. web search).
    - (FINAL_DONE, text): Final non-tool response.
    - (APPROVAL_REQUIRED, ...): HITL; call on_approval_required(item).
    - (STOPPED, ignored): Calls on_stopped() (second element unused).
    - (ERROR, payload): Calls on_error(payload).
    - (TOOL_CALL, payload): Agent-backend tool block; shown as text via apply_chunk_fn.
    - (TOOL_RESULT, payload): Agent-backend tool result block; shown as text via apply_chunk_fn.
    """
    state = _DrainState(
        q=q,
        apply_chunk_fn=apply_chunk_fn,
        on_stream_done=on_stream_done,
        on_stopped=on_stopped,
        on_error=on_error,
        on_status_fn=on_status_fn,
        on_approval_required=on_approval_required,
        show_search_thinking=show_search_thinking,
        job_done=job_done,
    )
    try:
        while not job_done[0]:
            if stop_checker and stop_checker():
                log.info("run_stream_drain_loop: Stop requested via checker.")
                on_stopped()
                job_done[0] = True
                break

            try:
                items = _drain_batch(q, 0.1)
            except Exception as e:
                error_payload = format_error_payload(e)
                log.error("Stream queue error: %s" % error_payload)
                on_error(error_payload)
                job_done[0] = True
                break

            if not items:
                if ctx:
                    pass
                if toolkit:
                    toolkit.processEventsToIdle()
                continue

            try:
                _process_batch(state, items, stop_checker)
            except Exception as e:
                error_payload = format_error_payload(e)
                log.error("run_stream_drain_loop EXCEPTION: %s" % error_payload)
                job_done[0] = True
                try:
                    on_error(error_payload)
                except Exception:
                    pass

            if toolkit:
                toolkit.processEventsToIdle()

        if toolkit:
            toolkit.processEventsToIdle()

    except Exception as e:
        error_payload = format_error_payload(e)
        log.error("Stream drain loop crashed: %s" % error_payload)

        try:
            on_error(error_payload)
        except Exception:
            log.error("Failed to notify error handler")

        job_done[0] = True


def run_async_worker_with_drain(
    ctx: Any,
    worker_fn: Callable[[queue.Queue], None],
    apply_chunk_fn: Callable[[str, bool], None] | None,
    on_done_fn: Callable[..., None] | None,
    on_error_fn: Callable[[Any], None] | None,
    on_status_fn: Callable[[str], None] | None = None,
    stop_checker: Callable[[], bool] | None = None,
    on_stopped_fn: Callable[[], None] | None = None,
    name: str = "async-worker",
    q: queue.Queue | None = None,
):
    """Run a background worker and drain its queue on the main thread.

    ``worker_fn`` is a callable that accepts the queue and produces
    :class:`StreamQueueKind` tuples. It does not need to post a terminal
    ``STREAM_DONE`` — the wrapper does so in ``finally`` so the drain loop
    always unblocks. Any exception raised by ``worker_fn`` is converted
    into an ``ERROR`` payload.

    Callback defaults: ``on_error_fn`` and ``on_stopped_fn`` fall back to
    ``on_done_fn`` or a no-op so the drain loop never fails on a missing
    handler.
    """
    if q is None:
        q = queue.Queue()
    job_done = [False]

    def worker_wrapper():
        try:
            worker_fn(q)
        except Exception as e:
            from plugin.framework.errors import format_error_payload
            q.put((StreamQueueKind.ERROR, format_error_payload(e)))
        finally:
            # Terminal sentinel so the drain loop always unblocks, even if
            # the worker forgot. A late sentinel after STOPPED/ERROR is
            # harmless because the loop has already exited.
            q.put((StreamQueueKind.STREAM_DONE, None))
            job_done[0] = True

    from plugin.framework.uno_context import get_toolkit
    toolkit = get_toolkit(ctx)
    if toolkit is None:
        from plugin.framework.errors import UnoObjectError
        err = UnoObjectError(f"Failed to create toolkit for {name}")
        if on_error_fn:
            on_error_fn(err)
        return

    run_in_background(worker_wrapper, daemon=True, name=name)

    def on_stream_done_wrapper(item):
        if on_done_fn:
            try:
                on_done_fn(item)
            except TypeError:
                # Fallback for callbacks that don't take any arguments.
                on_done_fn()

    def _noop_error(_payload: Any) -> None:
        return None

    def _noop_stopped() -> None:
        return None

    resolved_on_error = on_error_fn or _noop_error
    resolved_on_stopped = on_stopped_fn or (
        (lambda: on_done_fn()) if on_done_fn else _noop_stopped
    )

    run_stream_drain_loop(
        q,
        toolkit,
        job_done,
        apply_chunk_fn,
        on_stream_done=on_stream_done_wrapper,
        on_stopped=resolved_on_stopped,
        on_error=resolved_on_error,
        on_status_fn=on_status_fn,
        ctx=ctx,
        stop_checker=stop_checker,
    )


def _run_client_stream(
    ctx: Any,
    client_call: Callable[..., None],
    apply_chunk_fn: Callable[[str, bool], None] | None,
    on_done_fn: Callable[..., None] | None,
    on_error_fn: Callable[[Any], None] | None,
    on_status_fn: Callable[[str], None] | None = None,
    stop_checker: Callable[[], bool] | None = None,
    name: str = "stream-client",
    include_status: bool = False,
) -> None:
    """Shared adapter: run *client_call* in a worker streaming into the queue.

    ``client_call`` is a client method pre-bound with all positional args;
    it receives the standard streaming callback kwargs
    (``append_callback``, ``append_thinking_callback``, optional
    ``status_callback``, and ``stop_checker``).
    """

    def worker(q: queue.Queue) -> None:
        kwargs: dict[str, Any] = {
            "append_callback": lambda t: q.put((StreamQueueKind.CHUNK, t)),
            "append_thinking_callback": lambda t: q.put((StreamQueueKind.THINKING, t)),
            "stop_checker": stop_checker,
        }
        if include_status:
            kwargs["status_callback"] = lambda t: q.put((StreamQueueKind.STATUS, t))
        client_call(**kwargs)
        if stop_checker and stop_checker():
            put_stream_queue_stopped(q)

    run_async_worker_with_drain(
        ctx,
        worker,
        apply_chunk_fn=apply_chunk_fn,
        on_done_fn=on_done_fn,
        on_error_fn=on_error_fn,
        on_status_fn=on_status_fn,
        stop_checker=stop_checker,
        name=name,
    )


def run_stream_completion_async(
    ctx,
    client,
    prompt,
    system_prompt,
    max_tokens,
    apply_chunk_fn,
    on_done_fn,
    on_error_fn,
    on_status_fn=None,
    stop_checker=None,
):
    """High-level helper for simple non-tool streams (always chat completions)."""

    def client_call(**cb_kwargs):
        client.stream_completion(prompt, system_prompt, max_tokens, **cb_kwargs)

    _run_client_stream(
        ctx, client_call,
        apply_chunk_fn=apply_chunk_fn,
        on_done_fn=on_done_fn,
        on_error_fn=on_error_fn,
        on_status_fn=on_status_fn,
        stop_checker=stop_checker,
        name="stream-completion",
        include_status=True,
    )


def run_stream_async(
    ctx,
    client,
    messages,
    tools=None,
    apply_chunk_fn=None,
    on_done_fn=None,
    on_error_fn=None,
    max_tokens=None,
    stop_checker=None,
):
    """Compatibility helper for legacy run_stream_async calls (using messages/tools)."""

    effective_max = max_tokens or 512

    def client_call(**cb_kwargs):
        if tools:
            client.stream_request_with_tools(
                messages, effective_max, tools=tools, **cb_kwargs)
        else:
            client.stream_chat_response(messages, effective_max, **cb_kwargs)

    _run_client_stream(
        ctx, client_call,
        apply_chunk_fn=apply_chunk_fn,
        on_done_fn=on_done_fn,
        on_error_fn=on_error_fn,
        stop_checker=stop_checker,
        name="stream-async",
        include_status=False,
    )


def run_blocking_in_thread(ctx, func, *args, **kwargs):
    """
    Run a blocking function in a background thread while pumping UNO events
    on the main thread to keep the UI responsive.

    The internal queue uses :class:`BlockingPumpKind` as the first tuple
    element only (same contract as :class:`StreamQueueKind` for the stream drain).

    Returns the result of the function or raises the exception encountered.
    """
    q: "queue.Queue[BlockingPumpQueueItem]" = queue.Queue()

    def worker():
        try:
            result = func(*args, **kwargs)
            q.put((BlockingPumpKind.DONE, result))
        except Exception as e:
            q.put((BlockingPumpKind.ERROR, e))

    try:
        toolkit = ctx.getServiceManager().createInstanceWithContext(
            "com.sun.star.awt.Toolkit", ctx)
    except Exception as e:
        log.warning("run_blocking_with_pump: Failed to create toolkit, running synchronously. %s", e)
        # Fallback if toolkit isn't available (unlikely in UI context)
        return func(*args, **kwargs)

    run_in_background(worker, daemon=True, name="blocking-thread")

    while True:
        try:
            # Check for result without long block
            item = q.get(timeout=0.1)
            kind, data = item
            if not isinstance(kind, BlockingPumpKind):
                ek = TypeError(
                    "blocking pump queue item kind must be BlockingPumpKind, got %s"
                    % (type(kind).__name__,)
                )
                log.error("Invalid blocking pump tag: %s", ek)
                raise ek
            if kind == BlockingPumpKind.DONE:
                return data
            if kind == BlockingPumpKind.ERROR:
                raise data
        except queue.Empty:
            # Pulse MCP if enabled (similar to drain loop)
            # MCP pulse removed
            toolkit.processEventsToIdle()