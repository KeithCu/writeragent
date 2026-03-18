import pytest
import queue
import time
from plugin.framework.async_stream import run_stream_drain_loop

class DummyToolkit:
    def processEventsToIdle(self):
        pass

def test_run_stream_drain_loop_basic():
    q = queue.Queue()
    q.put(("chunk", "hello"))
    q.put(("stream_done", None))

    toolkit = DummyToolkit()
    job_done = [False]

    applied = []
    def apply_chunk(t, is_thinking):
        applied.append((t, is_thinking))

    def stream_done(item):
        return True

    def noop(*args, **kwargs):
        pass

    run_stream_drain_loop(
        q, toolkit, job_done, apply_chunk,
        on_stream_done=stream_done, on_stopped=noop, on_error=noop
    )

    assert job_done[0] is True
    assert ("hello", False) in applied

def test_run_stream_drain_loop_thinking():
    q = queue.Queue()
    q.put(("thinking", "hmmm"))
    q.put(("stream_done", None))

    toolkit = DummyToolkit()
    job_done = [False]

    applied = []
    def apply_chunk(t, is_thinking):
        applied.append((t, is_thinking))

    run_stream_drain_loop(
        q, toolkit, job_done, apply_chunk,
        on_stream_done=lambda i: True, on_stopped=lambda: None, on_error=lambda e: None
    )

    assert job_done[0] is True
    assert ("[Thinking] ", True) in applied
    assert ("hmmm", True) in applied
    assert (" /thinking\n", True) in applied

def test_run_stream_drain_loop_error():
    q = queue.Queue()
    q.put(("error", ValueError("test error")))

    toolkit = DummyToolkit()
    job_done = [False]

    errors = []
    def on_error(e):
        errors.append(e)

    run_stream_drain_loop(
        q, toolkit, job_done, lambda t, is_thinking: None,
        on_stream_done=lambda i: True, on_stopped=lambda: None, on_error=on_error
    )

    assert job_done[0] is True
    assert len(errors) == 1
    assert isinstance(errors[0], ValueError)


def test_run_stream_drain_loop_stop_checker_mid_batch():
    q = queue.Queue()
    q.put(("chunk", "first "))
    q.put(("chunk", "second "))
    q.put(("chunk", "third "))

    toolkit = DummyToolkit()
    job_done = [False]
    stopped_called = [False]
    applied = []

    def apply_chunk(t, is_thinking):
        applied.append(t)

    items_seen = [0]
    def stop_checker():
        # Stop on the second call (first item in the for loop)
        items_seen[0] += 1
        return items_seen[0] > 2

    def on_stopped():
        stopped_called[0] = True

    # To prevent the while loop in run_stream_drain_loop from hanging, we need to return True for stop_checker on the first run after setting `stop_flag` to True.
    # But since there is no `stream_done` at the end of the batch, the loop would just block on `q.get()`.
    # Actually `q.put(("stream_done", None))` might not be executed when `stop_checker` flips mid stream.
    # We should add a `stream_done` to break the loop normally if `stop_checker` somehow didn't stop the loop.
    q.put(("stream_done", None))

    run_stream_drain_loop(
        q, toolkit, job_done, apply_chunk,
        on_stream_done=lambda i: True, on_stopped=on_stopped, on_error=lambda e: None, stop_checker=stop_checker
    )

    assert stopped_called[0] is True
    assert job_done[0] is True
    # The first chunk should be processed, which sets stop_flag to True.
    # The stop_checker check happens at the start of the next iteration of the `for item in items:` loop.
    # So the remaining chunks in the batch shouldn't be processed.
    assert len(applied) == 1
    assert applied[0] == "first "


def test_run_stream_drain_loop_callback_raises():
    q = queue.Queue()
    q.put(("chunk", "hello"))

    toolkit = DummyToolkit()
    job_done = [False]

    def apply_chunk(t, is_thinking):
        raise RuntimeError("apply_chunk error")

    def on_error(e):
        raise RuntimeError("on_error error")

    # It should not hang, but gracefully mark job_done as True
    # and swallow the exception in the catch-all.
    run_stream_drain_loop(
        q, toolkit, job_done, apply_chunk,
        on_stream_done=lambda i: True, on_stopped=lambda: None, on_error=on_error
    )

    assert job_done[0] is True


def test_run_stream_drain_loop_tool_done_continue():
    q = queue.Queue()
    q.put(("tool_done", "call_123", "web_search", '{"q": "answer"}', '{"status": "ok"}'))
    q.put(("chunk", "next chunk"))

    toolkit = None
    job_done = [False]
    applied = []
    tools_done = []

    def apply_chunk(t, is_thinking):
        applied.append((t, is_thinking))

    def on_stream_done(item):
        if item[0] == "tool_done":
            tools_done.append(item)
            return False # Continue the loop!
        elif item[0] == "stream_done":
            return True
        return False

    def noop(*args, **kwargs):
        pass

    q.put(("stream_done", None))

    run_stream_drain_loop(
        q, toolkit, job_done, apply_chunk,
        on_stream_done=on_stream_done, on_stopped=noop, on_error=noop
    )

    assert job_done[0] is True
    assert len(tools_done) == 1
    assert tools_done[0][1] == "call_123"
    assert ("next chunk", False) in applied


def test_run_stream_drain_loop_stopped():
    q = queue.Queue()
    q.put(("stopped",))

    toolkit = DummyToolkit()
    job_done = [False]
    stopped_called = [False]

    def on_stopped():
        stopped_called[0] = True

    run_stream_drain_loop(
        q, toolkit, job_done, lambda t, is_thinking: None,
        on_stream_done=lambda i: True, on_stopped=on_stopped, on_error=lambda e: None
    )

    assert stopped_called[0] is True
    assert job_done[0] is True


def test_run_blocking_in_thread():
    from unittest.mock import MagicMock
    from plugin.framework.async_stream import run_blocking_in_thread

    ctx = MagicMock()
    ctx.getServiceManager.return_value = MagicMock()

    def blocking_func():
        return "success"

    assert run_blocking_in_thread(ctx, blocking_func) == "success"


def test_run_blocking_in_thread_error():
    from unittest.mock import MagicMock
    from plugin.framework.async_stream import run_blocking_in_thread

    ctx = MagicMock()
    ctx.getServiceManager.return_value = MagicMock()

    def blocking_func():
        raise ValueError("failed")

    with pytest.raises(ValueError, match="failed"):
        run_blocking_in_thread(ctx, blocking_func)


def test_run_stream_drain_loop_toolkit_none():
    q = queue.Queue()
    q.put(("chunk", "hello"))
    q.put(("stream_done", None))

    job_done = [False]

    applied = []
    def apply_chunk(t, is_thinking):
        applied.append((t, is_thinking))

    def stream_done(item):
        return True

    def noop(*args, **kwargs):
        pass

    # Should run successfully without a toolkit
    run_stream_drain_loop(
        q, None, job_done, apply_chunk,
        on_stream_done=stream_done, on_stopped=noop, on_error=noop
    )

    assert job_done[0] is True
    assert ("hello", False) in applied


def test_run_stream_drain_loop_tool_thinking():
    q = queue.Queue()
    q.put(("tool_thinking", "Searching google..."))
    q.put(("stream_done", None))

    job_done = [False]

    applied = []
    def apply_chunk(t, is_thinking):
        applied.append((t, is_thinking))

    def stream_done(item):
        return True

    def noop(*args, **kwargs):
        pass

    # With show_search_thinking=True, it should apply the chunk
    run_stream_drain_loop(
        q, None, job_done, apply_chunk,
        on_stream_done=stream_done, on_stopped=noop, on_error=noop, show_search_thinking=True
    )

    assert job_done[0] is True
    assert ("Searching google...", True) in applied

    # With show_search_thinking=False, it should NOT apply the chunk
    q2 = queue.Queue()
    q2.put(("tool_thinking", "Searching bing..."))
    q2.put(("stream_done", None))

    job_done2 = [False]
    applied2 = []
    def apply_chunk2(t, is_thinking):
        applied2.append((t, is_thinking))

    run_stream_drain_loop(
        q2, None, job_done2, apply_chunk2,
        on_stream_done=stream_done, on_stopped=noop, on_error=noop, show_search_thinking=False
    )

    assert job_done2[0] is True
    assert len(applied2) == 0


def test_run_stream_drain_loop_complex_interleaving():
    # Test a realistic stream involving thinking, chunking, status, tool_done, and final_done
    q = queue.Queue()
    q.put(("status", "Searching..."))
    q.put(("thinking", "I need to check the web."))
    q.put(("thinking", " Looking up..."))
    q.put(("chunk", "Based on my research, "))
    q.put(("status", "Writing..."))
    q.put(("chunk", "the answer is 42."))
    q.put(("tool_done", "call_123", "web_search", '{"q": "answer"}', '{"status": "ok"}'))
    q.put(("final_done", " That is all."))

    toolkit = None
    job_done = [False]

    applied = []
    statuses = []
    tools_done = []

    def apply_chunk(t, is_thinking):
        applied.append((t, is_thinking))

    def on_status(s):
        statuses.append(s)

    def stream_done(item):
        kind = item[0] if isinstance(item, tuple) else item
        if kind == "tool_done":
            tools_done.append(item)
            return True # stop the loop for testing purposes
        if kind == "final_done":
            applied.append((item[1], False))
            return True
        return False

    def noop(*args, **kwargs):
        pass

    run_stream_drain_loop(
        q, toolkit, job_done, apply_chunk,
        on_stream_done=stream_done, on_stopped=noop, on_error=noop, on_status_fn=on_status
    )

    assert job_done[0] is True

    # Assert specific sequence of flushes
    assert statuses == ["Searching...", "Writing..."]

    # Check what was applied to the UI in order
    assert applied[0] == ("[Thinking] ", True)
    assert applied[1] == ("I need to check the web. Looking up...", True)
    assert applied[2] == (" /thinking\n", True)
    # The batching combines consecutive content chunks into a single flush
    assert applied[3] == ("Based on my research, the answer is 42.", False)

    assert len(tools_done) == 1
    assert tools_done[0][1] == "call_123"

    # In our mock stream_done, tool_done returns True to stop the loop,
    # so we shouldn't actually see final_done applied in the assertions above.
    # Wait, the queue items are batched and processed sequentially in one go,
    # but `tool_done` handler does:
    # if on_stream_done(item): job_done[0] = True; break
    # so if it breaks, we don't process final_done in the same batch. Let's adjust assertions.
    # We will remove the `final_done` assertion because the loop will exit early.

    # Fix: We'll assert that final_done is NOT reached because tool_done broke the loop.
    assert len(applied) == 4


def test_run_stream_drain_loop_next_tool_and_approval():
    q = queue.Queue()
    q.put(("approval_required", "Do you allow file access?", "read_file", '{"path": "test.txt"}', "req_1"))
    q.put(("next_tool",))

    toolkit = None
    job_done = [False]

    applied = []
    approvals = []
    stream_done_items = []

    def apply_chunk(t, is_thinking):
        applied.append((t, is_thinking))

    def stream_done(item):
        stream_done_items.append(item)
        if item[0] == "next_tool":
            return True
        return False

    def on_stopped():
        pass

    def on_approval(item):
        approvals.append(item)

    run_stream_drain_loop(
        q, toolkit, job_done, apply_chunk,
        on_stream_done=stream_done, on_stopped=on_stopped, on_error=lambda e: None,
        on_approval_required=on_approval
    )

    assert job_done[0] is True
    assert len(stream_done_items) == 1
    assert stream_done_items[0] == ("next_tool",)

    assert len(approvals) == 1
    assert approvals[0] == ("approval_required", "Do you allow file access?", "read_file", '{"path": "test.txt"}', "req_1")


def test_run_stream_drain_loop_connection_drop():
    import threading

    q = queue.Queue()
    job_done = [False]
    toolkit = DummyToolkit()

    chunks_received = []
    error_received = []
    status_received = []

    def apply_chunk_fn(text, is_thinking=False):
        chunks_received.append((text, is_thinking))

    def on_stream_done(response):
        return True

    def on_stopped():
        pass

    def on_error(err):
        error_received.append(err)

    def on_status_fn(text):
        status_received.append(text)

    # Simulate a background thread that yields some chunks then raises an error
    def worker():
        try:
            q.put(("chunk", "Hello "))
            time.sleep(0.01)
            q.put(("chunk", "world"))
            time.sleep(0.01)
            # Simulate a connection drop halfway
            raise ConnectionError("Connection dropped unexpectedly")
        except Exception as e:
            q.put(("error", e))

    t = threading.Thread(target=worker)
    t.start()

    # Run the drain loop in the main thread (simulated)
    # The loop should terminate when job_done[0] becomes True, which happens on error
    run_stream_drain_loop(
        q,
        toolkit,
        job_done,
        apply_chunk_fn,
        on_stream_done,
        on_stopped,
        on_error,
        on_status_fn,
        ctx=None
    )

    t.join(timeout=1.0)
    assert not t.is_alive(), "Worker thread should have finished"

    # Verify that we received the initial chunks
    assert ("Hello ", False) in chunks_received
    assert ("world", False) in chunks_received

    # Verify that the error was caught and propagated
    assert len(error_received) == 1
    assert isinstance(error_received[0], ConnectionError)
    assert str(error_received[0]) == "Connection dropped unexpectedly"

    # Verify that the job was marked as done
    assert job_done[0] is True
