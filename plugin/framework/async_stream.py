"""
Unified async stream orchestration for WriterAgent.
Handles both simple streaming and complex tool-calling loops with thinking/status updates.
Runs blocking API calls on worker threads and drains logic via a main-thread loop
to keep the LibreOffice UI responsive (processEventsToIdle).
"""
import queue
import threading
import json

from plugin.framework.logging import debug_log


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
):
    """
    Main-thread drain loop: batches items from queue, manages thinking/chunk buffers,
    and dispatches to callbacks. Keeps UI responsive via processEventsToIdle().

    Supported queue items (kind, *args):
    - ('chunk', text): Applied via apply_chunk_fn(text, is_thinking=False).
    - ('thinking', text): Applied via apply_chunk_fn(text, is_thinking=True).
    - ('status', text): Passed to on_status_fn(text).
    - ('stream_done', response): Calls on_stream_done(response). Returns True if job finished.
    - ('next_tool',): Internal trigger for multi-round loops.
    - ('tool_done', call_id, func_name, args_str, res): Handled by orchestration (if used).
    - ('tool_thinking', text): Thinking tokens from a tool (e.g. web search).
    - ('final_done', text): Final non-tool response.
    - ('stopped',): Calls on_stopped().
    - ('error', exception): Calls on_error(exception).
    """
    thinking_open = [False]

    def close_thinking():
        if thinking_open[0]:
            apply_chunk_fn(" /thinking\n", is_thinking=True)
            thinking_open[0] = False

    while not job_done[0]:
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
                try:
                    from plugin.modules.http.mcp_protocol import drain_mcp_queue
                    drain_mcp_queue()
                except ImportError:
                    pass
            toolkit.processEventsToIdle()
            continue

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
                kind = item[0] if isinstance(item, (tuple, list)) else item
                data = item[1] if isinstance(item, (tuple, list)) and len(item) > 1 else None

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
                        apply_chunk_fn(data, is_thinking=True)
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

            flush_buffers()

        except Exception as e:
            debug_log("run_stream_drain_loop EXCEPTION: %s" % e, context="API")
            job_done[0] = True
            try:
                on_error(e)
            except:
                pass

        toolkit.processEventsToIdle()


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
            q.put(("error", e))

    try:
        toolkit = ctx.getServiceManager().createInstanceWithContext(
            "com.sun.star.awt.Toolkit", ctx)
    except Exception as e:
        on_error_fn(e)
        return

    t = threading.Thread(target=worker, daemon=True)
    t.start()

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
            q.put(("error", e))

    try:
        toolkit = ctx.getServiceManager().createInstanceWithContext(
            "com.sun.star.awt.Toolkit", ctx)
    except Exception as e:
        if on_error_fn:
            on_error_fn(e)
        return

    t = threading.Thread(target=worker, daemon=True)
    t.start()

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
    )
