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
    thinking_open = [False]

    def close_thinking():
        if thinking_open[0]:
            apply_chunk_fn(" /thinking\n", is_thinking=True)
            thinking_open[0] = False

    try:
        while not job_done[0]:
            if stop_checker and stop_checker():
                log.info("run_stream_drain_loop: Stop requested via checker.")
                on_stopped()
                job_done[0] = True
                break

            items = []
            try:
                # Wait for at least one item
                items.append(q.get(timeout=0.1))
                # Batch any additional items that arrived immediately
                try:
                    while True:
                        items.append(q.get_nowait())
                except queue.Empty:
                    pass
            except queue.Empty:
                # Pulse MCP if enabled
                if ctx:
                    pass # MC P pulse removed, executes in LibreOffice event loop natively.
                if toolkit:
                    toolkit.processEventsToIdle()
                continue
            except Exception as e:
                # Queue operation error
                error_payload = format_error_payload(e)
                log.error("Stream queue error: %s" % error_payload)
                on_error(error_payload)
                job_done[0] = True
                break

            try:
                current_content = []
                current_thinking = []

                def flush_buffers():
                    if current_thinking:
                        if not thinking_open[0]:
                            apply_chunk_fn("[Thinking] ", is_thinking=True)
                            thinking_open[0] = True
                        apply_chunk_fn("".join(current_thinking), is_thinking=True)
                        current_thinking.clear()
                    if current_content:
                        close_thinking()
                        apply_chunk_fn("".join(current_content), is_thinking=False)
                        current_content.clear()

                for item in items:
                    if stop_checker and stop_checker():
                        log.info("run_stream_drain_loop: Stop requested via checker.")
                        flush_buffers()
                        close_thinking()
                        on_stopped()
                        job_done[0] = True
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
                            flush_buffers()
                            close_thinking()
                            on_error(format_error_payload(ek))
                            job_done[0] = True
                            break
                        kind = raw_kind

                        if kind == StreamQueueKind.CHUNK:
                            if current_thinking:
                                flush_buffers()
                            current_content.append(data)
                        elif kind == StreamQueueKind.THINKING:
                            if current_content:
                                flush_buffers()
                            current_thinking.append(data)
                        elif kind == StreamQueueKind.STATUS:
                            if on_status_fn:
                                on_status_fn(data)
                        elif kind == StreamQueueKind.STREAM_DONE:
                            flush_buffers()
                            close_thinking()
                            if on_stream_done(item): # Pass whole item for consistency
                                job_done[0] = True
                                break
                        elif kind == StreamQueueKind.TOOL_DONE:
                            # For unified tool loop handling, we relay back to on_stream_done
                            # with a special structure or just let the caller handle it if they passed
                            # accurate on_stream_done logic.
                            flush_buffers()
                            close_thinking()
                            if on_stream_done(item): # Pass whole item for tool_done
                                job_done[0] = True
                                break
                        elif kind == StreamQueueKind.TOOL_THINKING:
                            if show_search_thinking:
                                if current_content:
                                    flush_buffers()
                                current_thinking.append(data)
                        elif kind == StreamQueueKind.TOOL_CALL:
                            flush_buffers()
                            close_thinking()
                            apply_chunk_fn(
                                _format_agent_tool_stream_line("[Tool call]", data),
                                is_thinking=False,
                            )
                        elif kind == StreamQueueKind.TOOL_RESULT:
                            flush_buffers()
                            close_thinking()
                            apply_chunk_fn(
                                _format_agent_tool_stream_line("[Tool result]", data),
                                is_thinking=False,
                            )
                        elif kind == StreamQueueKind.APPROVAL_REQUIRED:
                            flush_buffers()
                            close_thinking()
                            if on_approval_required:
                                try:
                                    on_approval_required(item)
                                except Exception as e:
                                    log.error("approval_required handler: %s" % e)
                        elif kind == StreamQueueKind.FINAL_DONE:
                            flush_buffers()
                            close_thinking()
                            if on_stream_done(item): # Same as tool_done
                                job_done[0] = True
                                break
                        elif kind == StreamQueueKind.NEXT_TOOL:
                            # Caller usually puts this back in to trigger next iteration
                            # if it's a multi-tool-round loop.
                            flush_buffers()
                            close_thinking()
                            if on_stream_done(item):
                                job_done[0] = True
                                break
                        elif kind == StreamQueueKind.STOPPED:
                            flush_buffers()
                            close_thinking()
                            on_stopped()
                            job_done[0] = True
                            break
                        elif kind == StreamQueueKind.ERROR:
                            flush_buffers()
                            close_thinking()
                            on_error(data)
                            job_done[0] = True
                            break
                    except Exception as loop_e:
                        error_payload = format_error_payload(loop_e)
                        log.error("Stream processing error: %s" % error_payload)
                        q.put((StreamQueueKind.ERROR, error_payload))

                flush_buffers()

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

        # Final event pump
        if toolkit:
            toolkit.processEventsToIdle()

    except Exception as e:
        # Catch-all for drain loop errors
        error_payload = format_error_payload(e)
        log.error("Stream drain loop crashed: %s" % error_payload)

        try:
            on_error(error_payload)
        except Exception:
            # Final fallback
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