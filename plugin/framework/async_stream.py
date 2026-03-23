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
import logging
import queue
import threading

from plugin.framework.worker_pool import run_in_background

log = logging.getLogger(__name__)


from plugin.framework.errors import format_error_payload

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

    Supported queue items (kind, *args):
    - ('chunk', text): Applied via apply_chunk_fn(text, is_thinking=False).
    - ('thinking', text): Applied via apply_chunk_fn(text, is_thinking=True).
    - ('status', text): Passed to on_status_fn(text).
    - ('stream_done', response): Calls on_stream_done(response). Returns True if job finished.
    - ('next_tool',): Internal trigger for multi-round loops.
    - ('tool_done', call_id, func_name, args_str, res): Handled by orchestration (if used).
    - ('tool_thinking', text): Thinking tokens from a tool (e.g. web search).
    - ('final_done', text): Final non-tool response.
    - ('approval_required', description, tool_name, args, request_id): HITL; call on_approval_required(item).
    - ('stopped',): Calls on_stopped().
    - ('error', exception): Calls on_error(exception).
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

                    kind = item[0] if isinstance(item, (tuple, list)) else item
                    data = item[1] if isinstance(item, (tuple, list)) and len(item) > 1 else None

                    try:
                        if kind == "chunk":
                            if current_thinking:
                                flush_buffers()
                            current_content.append(data)
                        elif kind == "thinking":
                            if current_content:
                                flush_buffers()
                            current_thinking.append(data)
                        elif kind == "status":
                            if on_status_fn:
                                on_status_fn(data)
                        elif kind == "stream_done":
                            flush_buffers()
                            close_thinking()
                            if on_stream_done(item): # Pass whole item for consistency
                                job_done[0] = True
                                break
                        elif kind == "tool_done":
                            # For unified tool loop handling, we relay back to on_stream_done
                            # with a special structure or just let the caller handle it if they passed
                            # accurate on_stream_done logic.
                            flush_buffers()
                            close_thinking()
                            if on_stream_done(item): # Pass whole item for tool_done
                                job_done[0] = True
                                break
                        elif kind == "tool_thinking":
                            if show_search_thinking:
                                if current_content:
                                    flush_buffers()
                                current_thinking.append(data)
                        elif kind == "approval_required":
                            flush_buffers()
                            close_thinking()
                            if on_approval_required:
                                try:
                                    on_approval_required(item)
                                except Exception as e:
                                    log.error("approval_required handler: %s" % e)
                        elif kind == "final_done":
                            flush_buffers()
                            close_thinking()
                            if on_stream_done(item): # Same as tool_done
                                job_done[0] = True
                                break
                        elif kind == "next_tool":
                            # Caller usually puts this back in to trigger next iteration
                            # if it's a multi-tool-round loop.
                            flush_buffers()
                            close_thinking()
                            if on_stream_done(item):
                                job_done[0] = True
                                break
                        elif kind == "stopped":
                            flush_buffers()
                            close_thinking()
                            on_stopped()
                            job_done[0] = True
                            break
                        elif kind == "error":
                            flush_buffers()
                            close_thinking()
                            on_error(data)
                            job_done[0] = True
                            break
                    except Exception as loop_e:
                        error_payload = format_error_payload(loop_e)
                        log.error("Stream processing error: %s" % error_payload)
                        q.put(("error", error_payload))

                flush_buffers()

            except Exception as e:
                error_payload = format_error_payload(e)
                log.error("run_stream_drain_loop EXCEPTION: %s" % error_payload)
                job_done[0] = True
                try:
                    on_error(error_payload)
                except:
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
    """
    High-level helper for simple non-tool streams (always chat completions).
    """
    q = queue.Queue()
    job_done = [False]

    def worker():
        try:
            client.stream_completion(
                prompt,
                system_prompt,
                max_tokens,
                append_callback=lambda t: q.put(("chunk", t)),
                append_thinking_callback=lambda t: q.put(("thinking", t)),
                status_callback=lambda t: q.put(("status", t)),
                stop_checker=stop_checker,
            )
            if stop_checker and stop_checker():
                q.put(("stopped",))
            else:
                q.put(("stream_done", None))
        except Exception as e:
            from plugin.framework.errors import format_error_payload
            q.put(("error", format_error_payload(e)))

    try:
        toolkit = ctx.getServiceManager().createInstanceWithContext(
            "com.sun.star.awt.Toolkit", ctx)
    except Exception as e:
        on_error_fn(e)
        return

    run_in_background(worker, daemon=True, name="stream-completion")

    def on_stream_done_wrapper(_response):
        on_done_fn()
        return True

    run_stream_drain_loop(
        q, toolkit, job_done, apply_chunk_fn,
        on_stream_done=on_stream_done_wrapper,
        on_stopped=on_done_fn,
        on_error=on_error_fn,
        on_status_fn=on_status_fn,
        ctx=ctx,
        stop_checker=stop_checker,
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
    """
    Compatibility helper for legacy run_stream_async calls (using messages/tools).
    """
    q = queue.Queue()
    job_done = [False]

    def worker():
        try:
            if tools:
                client.stream_request_with_tools(
                    messages,
                    max_tokens or 512,
                    tools=tools,
                    append_callback=lambda t: q.put(("chunk", t)),
                    append_thinking_callback=lambda t: q.put(("thinking", t)),
                    stop_checker=stop_checker,
                )
            else:
                client.stream_chat_response(
                    messages,
                    max_tokens or 512,
                    append_callback=lambda t: q.put(("chunk", t)),
                    append_thinking_callback=lambda t: q.put(("thinking", t)),
                    stop_checker=stop_checker,
                )
            if stop_checker and stop_checker():
                q.put(("stopped",))
            else:
                q.put(("stream_done", None))
        except Exception as e:
            from plugin.framework.errors import format_error_payload
            q.put(("error", format_error_payload(e)))

    try:
        toolkit = ctx.getServiceManager().createInstanceWithContext(
            "com.sun.star.awt.Toolkit", ctx)
    except Exception as e:
        if on_error_fn:
            on_error_fn(e)
        return

    run_in_background(worker, daemon=True, name="stream-async")

    def on_stream_done_wrapper(_response):
        if on_done_fn:
            on_done_fn()
        return True

    run_stream_drain_loop(
        q, toolkit, job_done, apply_chunk_fn,
        on_stream_done=on_stream_done_wrapper,
        on_stopped=on_done_fn if on_done_fn else (lambda: None),
        on_error=on_error_fn if on_error_fn else (lambda e: None),
        ctx=ctx,
        stop_checker=stop_checker,
    )


def run_blocking_in_thread(ctx, func, *args, **kwargs):
    """
    Run a blocking function in a background thread while pumping UNO events
    on the main thread to keep the UI responsive.
    
    Returns the result of the function or raises the exception encountered.
    """
    q = queue.Queue()
    
    def worker():
        try:
            result = func(*args, **kwargs)
            q.put(("done", result))
        except Exception as e:
            q.put(("error", e))

    try:
        toolkit = ctx.getServiceManager().createInstanceWithContext(
            "com.sun.star.awt.Toolkit", ctx)
    except Exception as e:
        # Fallback if toolkit isn't available (unlikely in UI context)
        return func(*args, **kwargs)

    run_in_background(worker, daemon=True, name="blocking-thread")

    while True:
        try:
            # Check for result without long block
            item = q.get(timeout=0.1)
            kind, data = item
            if kind == "done":
                return data
            elif kind == "error":
                raise data
        except queue.Empty:
            # Pulse MCP if enabled (similar to drain loop)
            # MCP pulse removed
            toolkit.processEventsToIdle()