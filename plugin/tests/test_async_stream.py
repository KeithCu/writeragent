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
