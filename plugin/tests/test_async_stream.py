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
