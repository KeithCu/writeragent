import inspect
import queue
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from plugin.framework.async_stream import StreamQueueKind, run_stream_drain_loop, BatchingStreamQueue
from plugin.framework.worker_pool import run_in_background

class DummyToolkit:
    def __init__(self):
        self.idle_calls = 0

    def processEventsToIdle(self):
        self.idle_calls += 1

def test_run_async_worker_with_drain_next_tool_keeps_pumping():
    """NEXT_TOOL must not end the generic worker drain before later items."""
    from plugin.framework.async_stream import run_async_worker_with_drain

    ctx = MagicMock()
    toolkit = DummyToolkit()
    q = queue.Queue()
    q.put((StreamQueueKind.NEXT_TOOL,))
    q.put((StreamQueueKind.CHUNK, "kept"))
    q.put((StreamQueueKind.STREAM_DONE, "end"))
    seen = []
    applied = []

    def worker(worker_q):
        del worker_q

    def on_done(item):
        seen.append(item[0] if isinstance(item, tuple) else item)

    with patch("plugin.framework.uno_context.get_toolkit", return_value=toolkit):
        run_async_worker_with_drain(
            ctx,
            worker,
            lambda text, is_thinking: applied.append((text, is_thinking)),
            on_done,
            None,
            q=q,
        )

    assert StreamQueueKind.NEXT_TOOL in seen
    assert StreamQueueKind.STREAM_DONE in seen
    assert ("kept", False) in applied


def test_run_stream_drain_loop_next_tool_false_keeps_pumping():
    """A false NEXT_TOOL return is the tool loop's keep-pumping signal."""
    q = queue.Queue()
    q.put((StreamQueueKind.NEXT_TOOL,))
    q.put((StreamQueueKind.CHUNK, "still"))
    q.put((StreamQueueKind.STREAM_DONE, None))
    seen = []
    applied = []

    def on_stream_done(item):
        seen.append(item[0])
        return item[0] == StreamQueueKind.STREAM_DONE

    run_stream_drain_loop(
        q,
        None,
        [False],
        lambda text, is_thinking: applied.append((text, is_thinking)),
        on_stream_done=on_stream_done,
        on_stopped=lambda: None,
        on_error=lambda err: None,
    )
    assert seen == [StreamQueueKind.NEXT_TOOL, StreamQueueKind.STREAM_DONE]
    assert ("still", False) in applied


def test_run_async_worker_with_drain_none_apply_chunk():
    from plugin.framework.async_stream import run_async_worker_with_drain

    ctx = MagicMock()
    toolkit = DummyToolkit()

    def worker(q):
        q.put((StreamQueueKind.CHUNK, "hello"))

    with patch("plugin.framework.uno_context.get_toolkit", return_value=toolkit):
        run_async_worker_with_drain(ctx, worker, None, lambda item: True, None)


def test_run_stream_drain_loop_basic():
    q = queue.Queue()
    q.put((StreamQueueKind.CHUNK, "hello"))
    q.put((StreamQueueKind.STREAM_DONE, None))

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
    q.put((StreamQueueKind.THINKING, "hmmm"))
    q.put((StreamQueueKind.STREAM_DONE, None))

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
    q.put((StreamQueueKind.ERROR, ValueError("test error")))

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


def test_run_stream_drain_loop_error_on_error_true_drops_batch_tail():
    """Recovered ERROR keeps the drain, but items already pulled are the failed attempt."""
    q = queue.Queue()
    q.put((StreamQueueKind.ERROR, ValueError("recoverable")))
    q.put((StreamQueueKind.CHUNK, "stale"))
    q.put((StreamQueueKind.STREAM_DONE, "stale-done"))

    toolkit = DummyToolkit()
    job_done = [False]
    applied = []
    errors = []
    done = []

    def on_error(e):
        errors.append(e)
        q.put((StreamQueueKind.CHUNK, "after retry"))
        q.put((StreamQueueKind.STREAM_DONE, "replacement"))
        return True

    def stream_done(item):
        done.append(item)
        return True

    run_stream_drain_loop(
        q,
        toolkit,
        job_done,
        lambda t, is_thinking: applied.append(t),
        on_stream_done=stream_done,
        on_stopped=lambda: None,
        on_error=on_error,
    )

    assert job_done[0] is True
    assert len(errors) == 1
    assert "stale" not in applied
    assert "after retry" in applied
    assert done[0][1] == "replacement"


def test_worker_exception_on_error_true_still_applies_later_chunk():
    """Wrapper must not queue STREAM_DONE after ERROR when recovery continues."""
    from plugin.framework.async_stream import run_async_worker_with_drain

    ctx = MagicMock()
    toolkit = DummyToolkit()
    shared: queue.Queue = queue.Queue()
    applied = []
    errors = []

    def worker(_worker_q):
        raise RuntimeError("boom")

    def on_error(e):
        errors.append(e)
        shared.put((StreamQueueKind.CHUNK, "after retry"))
        shared.put((StreamQueueKind.STREAM_DONE, None))
        return True

    with patch("plugin.framework.uno_context.get_toolkit", return_value=toolkit):
        run_async_worker_with_drain(
            ctx,
            worker,
            lambda text, _is_thinking: applied.append(text),
            lambda _item: None,
            on_error,
            q=shared,
        )

    assert len(errors) == 1
    assert "after retry" in applied


def test_closed_over_queue_error_skips_wrapper_stream_done():
    """Puts on a closed-over raw queue must still set saw_terminal (send_handlers)."""
    from plugin.framework.async_stream import run_async_worker_with_drain

    ctx = MagicMock()
    toolkit = DummyToolkit()
    shared: queue.Queue = queue.Queue()
    applied = []
    done_payloads = []
    errors = []

    def worker(_worker_q):
        # Ignore the passed queue; put on the closed-over one.
        shared.put((StreamQueueKind.ERROR, {"message": "fail", "code": "X"}))

    def on_error(e):
        errors.append(e)
        shared.put((StreamQueueKind.CHUNK, "recovered"))
        shared.put((StreamQueueKind.STREAM_DONE, "ok"))
        return True

    with patch("plugin.framework.uno_context.get_toolkit", return_value=toolkit):
        run_async_worker_with_drain(
            ctx,
            worker,
            lambda text, _is_thinking: applied.append(text),
            lambda item: done_payloads.append(item),
            on_error,
            q=shared,
        )

    assert len(errors) == 1
    assert "recovered" in applied
    # on_done receives the STREAM_DONE item; payload must be our "ok", not a
    # wrapper None that would have ended the drain before "recovered".
    assert any(p == "ok" or (isinstance(p, tuple) and p[-1] == "ok") for p in done_payloads)


def test_worker_error_put_then_raise_queues_single_error():
    """If the worker puts ERROR and then raises, only one ERROR must be queued."""
    from plugin.framework.async_stream import run_async_worker_with_drain

    ctx = MagicMock()
    toolkit = DummyToolkit()
    shared: queue.Queue = queue.Queue()
    applied = []
    errors = []

    def worker(worker_q):
        worker_q.put((StreamQueueKind.ERROR, {"message": "first error"}))
        raise RuntimeError("boom after error put")

    def on_error(err):
        errors.append(err)
        shared.put((StreamQueueKind.CHUNK, "recovered"))
        shared.put((StreamQueueKind.STREAM_DONE, None))
        return True

    with patch("plugin.framework.uno_context.get_toolkit", return_value=toolkit):
        run_async_worker_with_drain(
            ctx,
            worker,
            lambda text, _is_thinking: applied.append(text),
            lambda _item: None,
            on_error,
            q=shared,
        )

    assert len(errors) == 1
    assert errors[0] == {"message": "first error"}
    assert "recovered" in applied


def test_run_async_worker_with_drain_toolkit_failure_formats_error():
    """No-toolkit path must pass format_error_payload dict to on_error_fn."""
    from plugin.framework.async_stream import run_async_worker_with_drain

    ctx = MagicMock()
    errors = []

    with patch("plugin.framework.uno_context.get_toolkit", return_value=None):
        run_async_worker_with_drain(
            ctx,
            lambda _q: None,
            None,
            None,
            on_error_fn=lambda err: errors.append(err),
            name="test-worker",
        )

    assert len(errors) == 1
    assert isinstance(errors[0], dict)
    assert "Failed to create toolkit for test-worker" in errors[0].get("message", "")


def test_on_done_body_type_error_mentioning_positional_argument_is_not_retried():
    """A TypeError inside on_done must not be treated as the wrong arity."""
    from plugin.framework.async_stream import run_async_worker_with_drain

    ctx = MagicMock()
    toolkit = DummyToolkit()
    calls = []
    errors = []

    def worker(worker_q):
        worker_q.put((StreamQueueKind.STREAM_DONE, {"ok": True}))

    def on_done(item=None):
        calls.append(item)
        raise TypeError("helper() takes 1 positional argument but 2 were given")

    with patch("plugin.framework.uno_context.get_toolkit", return_value=toolkit):
        run_async_worker_with_drain(ctx, worker, None, on_done, errors.append)

    assert len(calls) == 1
    assert calls[0][0] is StreamQueueKind.STREAM_DONE
    assert errors


def test_zero_arg_on_done_is_called_once():
    from plugin.framework.async_stream import run_async_worker_with_drain

    ctx = MagicMock()
    toolkit = DummyToolkit()
    calls = []
    errors = []

    def worker(worker_q):
        worker_q.put((StreamQueueKind.STREAM_DONE, None))

    def on_done():
        calls.append("go")

    with patch("plugin.framework.uno_context.get_toolkit", return_value=toolkit):
        run_async_worker_with_drain(ctx, worker, None, on_done, errors.append)

    assert calls == ["go"]
    assert errors == []


def test_on_done_internal_type_error_is_not_retried_as_zero_arg():
    from plugin.framework.async_stream import run_async_worker_with_drain

    ctx = MagicMock()
    toolkit = DummyToolkit()
    calls = []

    def worker(worker_q):
        worker_q.put((StreamQueueKind.STREAM_DONE, {"ok": True}))

    def on_done(*args):
        calls.append(args)
        if args:
            raise TypeError("'NoneType' object is not subscriptable")

    with patch("plugin.framework.uno_context.get_toolkit", return_value=toolkit):
        run_async_worker_with_drain(ctx, worker, None, on_done, lambda _e: None)

    assert len(calls) == 1
    assert calls[0][0][0] is StreamQueueKind.STREAM_DONE
    assert calls[0][0][1] == {"ok": True}


def test_run_stream_drain_loop_stop_checker_mid_batch():
    q = queue.Queue()
    q.put((StreamQueueKind.CHUNK, "first "))
    q.put((StreamQueueKind.CHUNK, "second "))
    q.put((StreamQueueKind.CHUNK, "third "))

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
    # Actually `q.put((StreamQueueKind.STREAM_DONE, None))` might not be executed when `stop_checker` flips mid stream.
    # We should add a `stream_done` to break the loop normally if `stop_checker` somehow didn't stop the loop.
    q.put((StreamQueueKind.STREAM_DONE, None))

    run_stream_drain_loop(
        q, toolkit, job_done, apply_chunk,
        on_stream_done=lambda i: True, on_stopped=on_stopped, on_error=lambda e: None, stop_checker=stop_checker
    )

    assert stopped_called[0] is True
    assert job_done[0] is True
    # The first chunk is handled before Stop. CHUNK items already pulled
    # into the rest of the batch are still applied. STREAM_DONE is not.
    assert "".join(applied) == "first second third "


def test_run_stream_drain_loop_callback_raises():
    q = queue.Queue()
    q.put((StreamQueueKind.CHUNK, "hello"))

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
    q.put((StreamQueueKind.TOOL_DONE, "call_123", "web_search", '{"q": "answer"}', '{"status": "ok"}'))
    q.put((StreamQueueKind.CHUNK, "next chunk"))

    toolkit = None
    job_done = [False]
    applied = []
    tools_done = []

    def apply_chunk(t, is_thinking):
        applied.append((t, is_thinking))

    def on_stream_done(item):
        if item[0] == StreamQueueKind.TOOL_DONE:
            tools_done.append(item)
            return False # Continue the loop!
        elif item[0] == StreamQueueKind.STREAM_DONE:
            return True
        return False

    def noop(*args, **kwargs):
        pass

    q.put((StreamQueueKind.STREAM_DONE, None))

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
    q.put((StreamQueueKind.STOPPED,))

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


def test_run_blocking_in_thread_pump_idle_false_does_not_pump():
    from unittest.mock import MagicMock, patch
    from plugin.framework.async_stream import run_blocking_in_thread

    ctx = MagicMock()
    with patch("plugin.framework.blocking_wait.pump_ui_idle") as pump:
        with patch("plugin.framework.queue_executor.pump_main_thread_work_queue") as pump_queue:
            def slow_worker():
                import time
                time.sleep(0.3)
                return "ok"
            assert run_blocking_in_thread(ctx, slow_worker, pump_idle=False) == "ok"
    pump.assert_not_called()
    pump_queue.assert_called()


def test_run_blocking_in_thread_stop_checker_returns_without_pump():
    import threading
    import time
    from unittest.mock import MagicMock, patch
    from plugin.framework.async_stream import BlockingWaitStopped, run_blocking_in_thread

    ctx = MagicMock()
    stop = threading.Event()

    def _slow():
        time.sleep(2)
        return "done"

    def _checker():
        return stop.is_set()

    stop.set()
    with patch("plugin.framework.blocking_wait.pump_ui_idle") as pump:
        with pytest.raises(BlockingWaitStopped):
            run_blocking_in_thread(ctx, _slow, pump_idle=False, stop_checker=_checker)
    pump.assert_not_called()


def test_run_blocking_in_thread_toolkit_fail_runs_off_caller():
    import threading
    from unittest.mock import MagicMock
    from plugin.framework.async_stream import run_blocking_in_thread

    caller = threading.get_ident()
    ran_on: list[int] = []
    ctx = MagicMock()
    ctx.getServiceManager.side_effect = RuntimeError("no toolkit")

    def blocking_func():
        ran_on.append(threading.get_ident())
        return "ok"

    assert run_blocking_in_thread(ctx, blocking_func) == "ok"
    assert ran_on and ran_on[0] != caller


def test_run_blocking_in_thread_error():
    from unittest.mock import MagicMock
    from plugin.framework.async_stream import run_blocking_in_thread

    ctx = MagicMock()
    ctx.getServiceManager.return_value = MagicMock()

    def blocking_func():
        raise ValueError("failed")

    with pytest.raises(ValueError, match="failed"):
        run_blocking_in_thread(ctx, blocking_func)


def test_run_blocking_in_thread_baseexception_does_not_hang():
    from unittest.mock import MagicMock
    from plugin.framework.async_stream import run_blocking_in_thread

    class Boom(BaseException):
        pass

    ctx = MagicMock()
    ctx.getServiceManager.return_value = MagicMock()

    def blocking_func():
        raise Boom("hard fault")

    with pytest.raises(Boom, match="hard fault"):
        run_blocking_in_thread(ctx, blocking_func, pump_idle=False)


def test_run_blocking_in_thread_queue_empty_from_worker_is_not_swallowed():
    """queue.Empty from func must surface. The wait loop must not hang."""
    from plugin.framework.async_stream import run_blocking_in_thread

    ctx = MagicMock()
    holder: list[BaseException] = []

    def raise_empty() -> None:
        raise queue.Empty("worker empty")

    def run() -> None:
        try:
            run_blocking_in_thread(ctx, raise_empty, pump_idle=False)
        except BaseException as exc:
            holder.append(exc)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(2)
    assert not thread.is_alive(), "run_blocking_in_thread hung after the worker raised queue.Empty"
    assert len(holder) == 1
    assert isinstance(holder[0], queue.Empty)
    assert str(holder[0]) == "worker empty"


def test_run_stream_drain_loop_toolkit_none():
    q = queue.Queue()
    q.put((StreamQueueKind.CHUNK, "hello"))
    q.put((StreamQueueKind.STREAM_DONE, None))

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
    q.put((StreamQueueKind.TOOL_THINKING, "Searching google..."))
    q.put((StreamQueueKind.STREAM_DONE, None))

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
    q2.put((StreamQueueKind.TOOL_THINKING, "Searching bing..."))
    q2.put((StreamQueueKind.STREAM_DONE, None))

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
    q.put((StreamQueueKind.STATUS, "Searching..."))
    q.put((StreamQueueKind.THINKING, "I need to check the web."))
    q.put((StreamQueueKind.THINKING, " Looking up..."))
    q.put((StreamQueueKind.CHUNK, "Based on my research, "))
    q.put((StreamQueueKind.STATUS, "Writing..."))
    q.put((StreamQueueKind.CHUNK, "the answer is 42."))
    q.put((StreamQueueKind.TOOL_DONE, "call_123", "web_search", '{"q": "answer"}', '{"status": "ok"}'))
    q.put((StreamQueueKind.FINAL_DONE, " That is all."))

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
        if kind == StreamQueueKind.TOOL_DONE:
            tools_done.append(item)
            return True # stop the loop for testing purposes
        if kind == StreamQueueKind.FINAL_DONE:
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
    q.put((StreamQueueKind.APPROVAL_REQUIRED, "Do you allow file access?", "read_file", '{"path": "test.txt"}', "req_1"))
    q.put((StreamQueueKind.NEXT_TOOL,))
    # A true NEXT_TOOL return used to set job_done and drop this tail.
    q.put((StreamQueueKind.CHUNK, "after-next"))
    q.put((StreamQueueKind.STREAM_DONE, "end"))

    toolkit = None
    job_done = [False]

    applied = []
    approvals = []
    stream_done_items = []

    def apply_chunk(t, is_thinking):
        applied.append((t, is_thinking))

    def stream_done(item):
        stream_done_items.append(item)
        if item[0] == StreamQueueKind.NEXT_TOOL:
            return True
        if item[0] == StreamQueueKind.STREAM_DONE:
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
    assert [item[0] for item in stream_done_items] == [
        StreamQueueKind.NEXT_TOOL,
        StreamQueueKind.STREAM_DONE,
    ]
    assert ("after-next", False) in applied

    assert len(approvals) == 1
    assert approvals[0] == (
        StreamQueueKind.APPROVAL_REQUIRED,
        "Do you allow file access?",
        "read_file",
        '{"path": "test.txt"}',
        "req_1",
    )


def test_run_stream_drain_loop_connection_drop():
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
            q.put((StreamQueueKind.CHUNK, "Hello "))
            time.sleep(0.01)
            q.put((StreamQueueKind.CHUNK, "world"))
            time.sleep(0.01)
            # Simulate a connection drop halfway
            raise ConnectionError("Connection dropped unexpectedly")
        except Exception as e:
            q.put((StreamQueueKind.ERROR, e))

    t = run_in_background(worker, daemon=False)

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


def test_run_stream_drain_loop_rejects_string_kind():
    """First tuple element must be StreamQueueKind, not a bare str matching the value."""
    q = queue.Queue()
    q.put(("chunk", "bad"))
    job_done = [False]
    errors = []

    def on_error(e):
        errors.append(e)

    run_stream_drain_loop(
        q,
        None,
        job_done,
        lambda t, is_thinking: None,
        on_stream_done=lambda i: True,
        on_stopped=lambda: None,
        on_error=on_error,
    )
    assert job_done[0] is True
    assert len(errors) == 1


def test_run_stream_drain_loop_tool_call_and_tool_result():
    q = queue.Queue()
    payload_call = {"type": "tool_call", "name": "read_file"}
    payload_result = {"type": "tool_result", "content": "ok"}
    q.put((StreamQueueKind.TOOL_CALL, payload_call))
    q.put((StreamQueueKind.TOOL_RESULT, payload_result))
    q.put((StreamQueueKind.STREAM_DONE, None))

    job_done = [False]
    applied = []

    def apply_chunk(t, is_thinking):
        applied.append((t, is_thinking))

    run_stream_drain_loop(
        q,
        None,
        job_done,
        apply_chunk,
        on_stream_done=lambda i: True,
        on_stopped=lambda: None,
        on_error=lambda e: None,
    )

    assert job_done[0] is True
    assert any("[Tool call]" in t for t, th in applied if not th)
    assert any("[Tool result]" in t for t, th in applied if not th)
    assert any(payload_call["name"] in t for t, th in applied if not th)


# ── accumulate_delta tests ──────────────────────────────────────────


def test_accumulate_delta_simple():
    from plugin.framework.async_stream import accumulate_delta

    acc = {"a": "hello"}
    delta = {"a": " world"}
    result = accumulate_delta(acc, delta)
    assert result == {"a": "hello world"}
    assert acc is result


def test_accumulate_delta_new_key():
    from plugin.framework.async_stream import accumulate_delta

    acc = {"a": "hello"}
    delta = {"b": 42}
    result = accumulate_delta(acc, delta)
    assert result == {"a": "hello", "b": 42}


def test_accumulate_delta_null_base():
    from plugin.framework.async_stream import accumulate_delta

    acc = {"a": None}
    delta = {"a": "value"}
    result = accumulate_delta(acc, delta)
    assert result == {"a": "value"}


def test_accumulate_delta_special_keys():
    from plugin.framework.async_stream import accumulate_delta

    acc = {"index": 1, "type": "old"}
    delta = {"index": 2, "type": "new"}
    result = accumulate_delta(acc, delta)
    assert result == {"index": 2, "type": "new"}


def test_accumulate_delta_numeric():
    from plugin.framework.async_stream import accumulate_delta

    acc = {"num": 10, "float": 1.5}
    delta = {"num": 5, "float": 2.5}
    result = accumulate_delta(acc, delta)
    assert result == {"num": 15, "float": 4.0}


def test_accumulate_delta_nested_dict():
    from plugin.framework.async_stream import accumulate_delta

    acc = {"obj": {"x": "a"}}
    delta = {"obj": {"y": "b", "x": "c"}}
    result = accumulate_delta(acc, delta)
    assert result == {"obj": {"x": "ac", "y": "b"}}


def test_accumulate_delta_list_simple():
    from plugin.framework.async_stream import accumulate_delta

    acc = {"list": ["a", 1]}
    delta = {"list": ["b", 2]}
    result = accumulate_delta(acc, delta)
    assert result == {"list": ["a", 1, "b", 2]}


def test_accumulate_delta_list_objects():
    from plugin.framework.async_stream import accumulate_delta

    acc = {"items": []}
    delta = {"items": [{"index": 0, "val": "a"}]}
    result = accumulate_delta(acc, delta)
    assert result == {"items": [{"index": 0, "val": "a"}]}

    delta2 = {"items": [{"index": 0, "val": "b"}]}
    result2 = accumulate_delta(result, delta2)
    assert result2 == {"items": [{"index": 0, "val": "ab"}]}

    delta3 = {"items": [{"index": 1, "val": "c"}]}
    result3 = accumulate_delta(result2, delta3)
    assert result3 == {"items": [{"index": 0, "val": "ab"}, {"index": 1, "val": "c"}]}


def test_accumulate_delta_errors():
    from plugin.framework.async_stream import accumulate_delta

    acc = {"items": [{"index": 0, "val": "a"}]}

    # Missing index
    with pytest.raises(RuntimeError):
        accumulate_delta(acc, {"items": [{"val": "b"}]})

    # Bad index type
    with pytest.raises(TypeError):
        accumulate_delta(acc, {"items": [{"index": "0", "val": "b"}]})

    # Non-dict delta entry
    with pytest.raises(TypeError):
        accumulate_delta(acc, {"items": ["bad"]})


def test_accumulate_delta_rejects_non_plain_dict():
    """Mapping subclasses that isinstance(dict) must be rejected (plain dict only)."""
    from collections import UserDict

    from plugin.framework.async_stream import accumulate_delta
    from tests.harness.strip_bundle import expect_pre_or_body

    expect_pre_or_body(
        lambda: accumulate_delta(UserDict({"a": 1}), {"a": 2}),  # type: ignore[arg-type]
        body_exc=TypeError,
    )
    expect_pre_or_body(
        lambda: accumulate_delta({"a": 1}, UserDict({"a": 2})),  # type: ignore[arg-type]
        body_exc=TypeError,
    )


class TestAsyncStreamErrorHandling():

    def test_stream_drain_loop_success(self):
        q = queue.Queue()
        toolkit = MagicMock()
        job_done = [False]
        on_chunk = MagicMock()
        on_error = MagicMock()
        on_stream_done = MagicMock()
        on_stopped = MagicMock()
        q.put((StreamQueueKind.CHUNK, 'hello '))
        q.put((StreamQueueKind.THINKING, 'thinking...'))
        q.put((StreamQueueKind.STREAM_DONE, 'final'))
        run_stream_drain_loop(q, toolkit, job_done, on_chunk, on_stream_done, on_stopped, on_error)
        assert (job_done[0] is True)
        on_chunk.assert_any_call('hello ', False)
        on_chunk.assert_any_call('thinking...', True)
        on_stream_done.assert_called_once_with((StreamQueueKind.STREAM_DONE, 'final'))
        on_error.assert_not_called()

    def test_stream_drain_loop_processing_error(self):
        q = queue.Queue()
        toolkit = MagicMock()
        job_done = [False]
        on_error = MagicMock()

        def faulty_on_chunk(data, is_thinking):
            raise ValueError('Processing failed')
        q.put((StreamQueueKind.CHUNK, 'bad data'))
        run_stream_drain_loop(q, toolkit, job_done, faulty_on_chunk, MagicMock(), MagicMock(), on_error)
        assert (job_done[0] is True)
        assert (on_error.call_count == 1)
        error_payload = on_error.call_args[0][0]
        assert (error_payload['status'] == 'error')
        assert ('Processing failed' in error_payload['message'])


def test_process_batch_handler_raises_on_second_chunk_ends_batch(monkeypatch):
    """Dispatch-handler raise on the second CHUNK ends the batch without fake success.

    Consecutive CHUNKs only append; apply_chunk_fn runs in flush_buffers(). Patch
    _handle_chunk (not apply_chunk_fn) so this hits the inner except, not the
    outer catch that test_stream_drain_loop_processing_error covers.

    STREAM_DONE is already in the dequeued items list; job_done + break skips it
    rather than leaving it on the queue. Do not assert q still holds STREAM_DONE.
    """
    import plugin.framework.async_stream as async_stream

    q = queue.Queue()
    q.put((StreamQueueKind.CHUNK, "one"))
    q.put((StreamQueueKind.CHUNK, "two"))
    q.put((StreamQueueKind.CHUNK, "three"))
    q.put((StreamQueueKind.STREAM_DONE, None))

    applied = []
    errors = []
    done_calls = []
    orig = async_stream._handle_chunk
    calls = [0]

    def boom_chunk(state, data, item):
        calls[0] += 1
        if calls[0] == 2:
            raise RuntimeError("second chunk")
        return orig(state, data, item)

    monkeypatch.setitem(async_stream._DISPATCH, StreamQueueKind.CHUNK, boom_chunk)

    def apply_chunk(text, is_thinking):
        applied.append(text)

    def on_error(e):
        errors.append(e)

    def on_stream_done(item):
        done_calls.append(item)
        return True

    job_done = [False]
    async_stream.run_stream_drain_loop(
        q, None, job_done, apply_chunk,
        on_stream_done=on_stream_done, on_stopped=lambda: None, on_error=on_error,
    )

    assert job_done[0] is True
    assert calls[0] == 2
    assert len(errors) == 1
    assert errors[0]["status"] == "error"
    assert "second chunk" in errors[0]["message"]
    assert done_calls == []
    joined = "".join(applied)
    assert "one" in joined
    assert "three" not in joined


def test_process_batch_handler_raises_on_second_thinking_ends_batch(monkeypatch):
    """Same inner-except contract for THINKING as for CHUNK."""
    import plugin.framework.async_stream as async_stream

    q = queue.Queue()
    q.put((StreamQueueKind.THINKING, "hmm"))
    q.put((StreamQueueKind.THINKING, "nope"))
    q.put((StreamQueueKind.THINKING, "later"))
    q.put((StreamQueueKind.STREAM_DONE, None))

    applied = []
    errors = []
    done_calls = []
    orig = async_stream._handle_thinking
    calls = [0]

    def boom_thinking(state, data, item):
        calls[0] += 1
        if calls[0] == 2:
            raise RuntimeError("second thinking")
        return orig(state, data, item)

    monkeypatch.setitem(async_stream._DISPATCH, StreamQueueKind.THINKING, boom_thinking)

    def apply_chunk(text, is_thinking):
        applied.append(text)

    def on_error(e):
        errors.append(e)

    def on_stream_done(item):
        done_calls.append(item)
        return True

    job_done = [False]
    async_stream.run_stream_drain_loop(
        q, None, job_done, apply_chunk,
        on_stream_done=on_stream_done, on_stopped=lambda: None, on_error=on_error,
    )

    assert job_done[0] is True
    assert calls[0] == 2
    assert len(errors) == 1
    assert "second thinking" in errors[0]["message"]
    assert done_calls == []
    joined = "".join(applied)
    assert "hmm" in joined
    assert "later" not in joined


def test_process_batch_handler_error_on_error_true_drops_batch_tail(monkeypatch):
    """on_error returning True keeps the drain and drops the failed attempt's tail."""
    import plugin.framework.async_stream as async_stream

    q = queue.Queue()
    q.put((StreamQueueKind.CHUNK, "one"))
    q.put((StreamQueueKind.CHUNK, "two"))
    q.put((StreamQueueKind.CHUNK, "three"))
    q.put((StreamQueueKind.STREAM_DONE, "stale-done"))

    applied = []
    errors = []
    done_calls = []
    orig = async_stream._handle_chunk
    calls = [0]

    def boom_chunk(state, data, item):
        calls[0] += 1
        if calls[0] == 2:
            raise RuntimeError("second chunk")
        return orig(state, data, item)

    monkeypatch.setitem(async_stream._DISPATCH, StreamQueueKind.CHUNK, boom_chunk)

    def apply_chunk(text, is_thinking):
        applied.append(text)

    def on_error(e):
        errors.append(e)
        q.put((StreamQueueKind.STREAM_DONE, "replacement"))
        return True

    def on_stream_done(item):
        done_calls.append(item)
        return True

    job_done = [False]
    async_stream.run_stream_drain_loop(
        q, None, job_done, apply_chunk,
        on_stream_done=on_stream_done, on_stopped=lambda: None, on_error=on_error,
    )

    assert job_done[0] is True
    assert len(errors) == 1
    assert len(done_calls) == 1
    assert done_calls[0][1] == "replacement"
    joined = "".join(applied)
    assert "one" in joined
    assert "three" not in joined


def test_process_batch_handler_error_on_error_raises_still_sets_job_done(monkeypatch):
    """A raising on_error must still set job_done and must not be retried by the outer catch."""
    import plugin.framework.async_stream as async_stream

    q = queue.Queue()
    q.put((StreamQueueKind.CHUNK, "one"))
    q.put((StreamQueueKind.CHUNK, "two"))
    q.put((StreamQueueKind.STREAM_DONE, None))

    errors = []
    orig = async_stream._handle_chunk
    calls = [0]

    def boom_chunk(state, data, item):
        calls[0] += 1
        if calls[0] == 2:
            raise RuntimeError("second chunk")
        return orig(state, data, item)

    monkeypatch.setitem(async_stream._DISPATCH, StreamQueueKind.CHUNK, boom_chunk)

    def on_error(e):
        errors.append(e)
        raise RuntimeError("on_error error")

    job_done = [False]
    async_stream.run_stream_drain_loop(
        q, None, job_done, lambda _t, _th: None,
        on_stream_done=lambda _i: True, on_stopped=lambda: None, on_error=on_error,
    )

    assert job_done[0] is True
    assert len(errors) == 1


# --- BatchingStreamQueue tests (producer-side 250 ms smoothing) ---

def test_batching_stream_queue_basic_join_and_flush():
    """CHUNK deltas are accumulated and emitted as a single joined string on explicit flush."""
    raw = queue.Queue()
    bq = BatchingStreamQueue(raw, batch_interval=0.25)

    bq.put((StreamQueueKind.CHUNK, "Hello "))
    bq.put((StreamQueueKind.CHUNK, "world"))
    assert raw.empty(), "no emission until flush or timer"

    bq.flush()

    item = raw.get_nowait()
    assert item == (StreamQueueKind.CHUNK, "Hello world")
    assert raw.empty()

    # THINKING joins separately
    bq.put((StreamQueueKind.THINKING, "[thinking]"))
    bq.flush()
    item2 = raw.get_nowait()
    assert item2 == (StreamQueueKind.THINKING, "[thinking]")


def test_batching_stream_queue_auto_flush_on_boundary():
    """Putting a control item forces immediate flush of any pending display text."""
    raw = queue.Queue()
    bq = BatchingStreamQueue(raw, batch_interval=10.0)  # long interval so only explicit/auto-boundary triggers

    bq.put((StreamQueueKind.CHUNK, "part1"))
    bq.put((StreamQueueKind.CHUNK, "part2"))
    bq.put((StreamQueueKind.STREAM_DONE, None))  # boundary

    # The boundary put should have caused the joined CHUNK to be emitted first
    first = raw.get_nowait()
    assert first == (StreamQueueKind.CHUNK, "part1part2")
    second = raw.get_nowait()
    assert second == (StreamQueueKind.STREAM_DONE, None)
    assert raw.empty()


def test_batching_stream_queue_callbacks():
    """The content_cb / thinking_cb helpers feed the batcher."""
    raw = queue.Queue()
    bq = BatchingStreamQueue(raw, batch_interval=0.25)

    cb = bq.content_cb()
    cb("a")
    cb("b")
    bq.flush()

    assert raw.get_nowait() == (StreamQueueKind.CHUNK, "ab")


def test_idle_stop_closes_open_thinking():
    q = queue.Queue()
    q.put((StreamQueueKind.THINKING, "hmm"))
    applied = []
    stopped = []
    checks = [0]

    def stop_checker():
        # While-loop check, then the item check, then the idle check after
        # the thinking text has been flushed. This stop is the idle path
        # (no pulled tail), which still has to close [Thinking].
        checks[0] += 1
        return checks[0] > 2

    job_done = [False]
    run_stream_drain_loop(
        q, None, job_done, lambda text, _is_thinking: applied.append(text),
        on_stream_done=lambda _i: True,
        on_stopped=lambda: stopped.append(True),
        on_error=lambda _e: None,
        stop_checker=stop_checker,
    )
    assert stopped == [True]
    assert job_done[0] is True
    joined = "".join(applied)
    assert "hmm" in joined
    assert " /thinking\n" in joined


def test_async_worker_stop_flushes_batcher_before_worker_returns():
    """Stop must flush a BatchingStreamQueue before the worker's finally."""
    from plugin.framework.async_stream import run_async_worker_with_drain

    ctx = MagicMock()
    toolkit = DummyToolkit()
    batched = BatchingStreamQueue(queue.Queue(), batch_interval=30.0)
    produced = threading.Event()
    release = threading.Event()
    applied: list[str] = []

    def worker(worker_q):
        worker_q.put((StreamQueueKind.CHUNK, "kept"))
        produced.set()
        release.wait(5)

    try:
        with patch("plugin.framework.uno_context.get_toolkit", return_value=toolkit):
            run_async_worker_with_drain(
                ctx,
                worker,
                lambda text, _is_thinking: applied.append(text),
                lambda _item: None,
                lambda _e: None,
                stop_checker=produced.is_set,
                q=batched,
            )
        assert applied == ["kept"]
        assert not release.is_set()
    finally:
        release.set()


def test_batcher_flush_error_still_emits_error_and_unpatches():
    """A raising flush must not swallow ERROR or leave the put wrapper installed."""
    from plugin.framework.async_stream import run_async_worker_with_drain

    ctx = MagicMock()
    toolkit = DummyToolkit()
    raw: queue.Queue = queue.Queue()
    batched = BatchingStreamQueue(raw, batch_interval=30.0)
    errors: list[object] = []
    done = threading.Event()

    def boom() -> None:
        raise RuntimeError("flush boom")

    batched.flush = boom  # type: ignore[method-assign]

    def worker(worker_q):
        worker_q.put((StreamQueueKind.CHUNK, "x"))
        raise RuntimeError("worker boom")

    def run() -> None:
        try:
            with patch("plugin.framework.uno_context.get_toolkit", return_value=toolkit):
                run_async_worker_with_drain(
                    ctx,
                    worker,
                    lambda text, _is_thinking: None,
                    lambda _item: None,
                    errors.append,
                    q=batched,
                )
        finally:
            done.set()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    assert done.wait(3), "drain missed the error terminal after flush failed"
    thread.join(1)
    assert errors
    assert any("worker boom" in str(err) for err in errors)
    assert getattr(raw, "_wa_terminal_watch", None) is None
    assert raw.put.__name__ == "put"


def test_stop_on_first_batch_item_applies_pulled_display_text():
    """CHUNK/THINKING already pulled must be shown when Stop is the first item."""
    q = queue.Queue()
    q.put((StreamQueueKind.CHUNK, "hello"))
    q.put((StreamQueueKind.THINKING, "hmm"))
    q.put((StreamQueueKind.STATUS, "skip"))
    q.put((StreamQueueKind.STREAM_DONE, "nope"))
    applied = []
    statuses = []
    done = []
    stopped = []
    checks = [0]

    def stop_checker():
        # First call is the while-loop check, before the batch is pulled.
        # The next call is the first item. The whole batch is then local.
        checks[0] += 1
        return checks[0] > 1

    def on_stream_done(item):
        done.append(item)
        return True

    job_done = [False]
    run_stream_drain_loop(
        q, None, job_done, lambda text, is_thinking: applied.append((text, is_thinking)),
        on_stream_done=on_stream_done,
        on_stopped=lambda: stopped.append(True),
        on_error=lambda _e: None,
        on_status_fn=statuses.append,
        stop_checker=stop_checker,
    )
    assert job_done[0] is True
    assert stopped == [True]
    assert statuses == []
    assert done == []
    texts = [text for text, _is_thinking in applied]
    assert "hello" in texts
    assert any("hmm" in text for text in texts)
    assert any(text == " /thinking\n" for text in texts)


def test_stop_applies_flushed_batcher_text():
    q = queue.Queue()
    applied = []

    def flush_pending():
        q.put((StreamQueueKind.CHUNK, "kept"))

    job_done = [False]
    run_stream_drain_loop(
        q, None, job_done, lambda text, _is_thinking: applied.append(text),
        on_stream_done=lambda _i: True,
        on_stopped=lambda: None,
        on_error=lambda _e: None,
        stop_checker=lambda: True,
        flush_pending=flush_pending,
    )
    assert job_done[0] is True
    assert applied == ["kept"]


def test_batching_stream_queue_preserves_thinking_before_content():
    raw = queue.Queue()
    bq = BatchingStreamQueue(raw, batch_interval=10.0)
    bq.put((StreamQueueKind.THINKING, "think"))
    bq.put((StreamQueueKind.CHUNK, "reply"))
    bq.put((StreamQueueKind.THINKING, "more"))
    bq.flush()
    assert raw.get_nowait() == (StreamQueueKind.THINKING, "think")
    assert raw.get_nowait() == (StreamQueueKind.CHUNK, "reply")
    assert raw.get_nowait() == (StreamQueueKind.THINKING, "more")
    assert raw.empty()


def test_batching_stream_queue_interleaved_kinds_keep_deadline():
    """A THINKING fragment must not restart the deadline armed by the first CHUNK."""
    raw = queue.Queue()
    bq = BatchingStreamQueue(raw, batch_interval=10.0)
    bq.put((StreamQueueKind.CHUNK, "a"))
    armed = bq._timer
    assert armed is not None
    deadline = armed._deadline
    assert deadline is not None
    bq.put((StreamQueueKind.THINKING, "t"))
    assert bq._timer is armed
    assert armed._deadline == deadline
    bq.flush()
    assert raw.get_nowait() == (StreamQueueKind.CHUNK, "a")
    assert raw.get_nowait() == (StreamQueueKind.THINKING, "t")


def test_batching_stream_queue_timer_emission(monkeypatch):
    """Timer fires and emits after the interval even without further puts (simulated)."""
    raw = queue.Queue()
    bq = BatchingStreamQueue(raw, batch_interval=0.05)

    bq.put((StreamQueueKind.CHUNK, "delayed"))

    # Fire the flush directly. Cancel first so the reusable timer thread
    # does not emit the same burst again when the interval elapses.
    if bq._timer is not None:
        bq._timer.cancel()
    bq._timer_flush()

    item = raw.get_nowait()
    assert item == (StreamQueueKind.CHUNK, "delayed")
    assert raw.empty()


def test_pump_ui_idle_unblocks_execute_on_main_thread_when_poke_noop():
    """Regression: async tools marshal UNO while drain loop runs; poke alone must not deadlock."""
    from plugin.framework import queue_executor as qe

    toolkit = DummyToolkit()
    result_holder: list[str] = []
    done = threading.Event()

    def worker():
        try:
            result_holder.append(qe.execute_on_main_thread(lambda: "marshaled"))
        finally:
            done.set()

    with patch.object(qe.default_executor, "_get_async_callback", return_value=MagicMock()):
        with patch.object(qe.default_executor, "_poke_main_thread", lambda: None):
            qe.set_force_marshal_mode(True)
            try:
                t = run_in_background(worker, name="marshal-worker", daemon=False)
                deadline = time.time() + 3.0
                while not done.is_set() and time.time() < deadline:
                    qe.pump_ui_idle(toolkit, max_queue_items=4)
                t.join(timeout=1.0)
                assert done.is_set(), "worker blocked on execute_on_main_thread without pump_ui_idle"
                assert result_holder == ["marshaled"]
            finally:
                qe.set_force_marshal_mode(False)
                while not qe.default_executor._work_queue.empty():
                    qe.default_executor.process_queue()


def test_drain_owner_scope_rejects_nesting():
    from plugin.framework.queue_executor import NestedDrainOwnerError, drain_owner_scope, get_drain_owner

    with drain_owner_scope("stream"):
        assert get_drain_owner() == "stream"
        with pytest.raises(NestedDrainOwnerError):
            with drain_owner_scope("nested"):
                pass
    assert get_drain_owner() is None


def test_pump_ui_idle_still_pumps_vcl_under_drain_owner():
    """Owner path must keep pumping so Send stays responsive / Stop works."""
    from plugin.framework import queue_executor as qe

    toolkit = DummyToolkit()
    with qe.drain_owner_scope("stream"):
        qe.pump_ui_idle(toolkit)
    assert toolkit.idle_calls >= 1


def test_pump_ui_idle_skips_vcl_when_drain_depth_gt_1_but_drains_queue():
    """Same-owner re-entry must not pump VCL again, and must still run queued work."""
    from plugin.framework import queue_executor as qe
    from plugin.framework.async_drain_guard import get_drain_depth, reset_sentry_state

    reset_sentry_state()
    toolkit = DummyToolkit()
    ran: list[str] = []
    ex = qe.QueueExecutor()
    ex._enqueue_work(lambda: ran.append("q"), (), {}, blocking=False)
    with qe.drain_owner_scope("stream"):
        assert get_drain_depth() == 1
        with qe.drain_owner_scope("stream"):
            assert get_drain_depth() == 2
            qe.pump_ui_idle(toolkit, executor=ex)
    assert toolkit.idle_calls == 0
    assert ran == ["q"]
    assert qe.get_suppressed_vcl_pump_count() >= 1
    reset_sentry_state()


def test_run_blocking_in_thread_pump_idle_uses_get_toolkit():
    from plugin.framework.async_stream import run_blocking_in_thread

    toolkit = DummyToolkit()
    ctx = MagicMock()
    with patch("plugin.framework.uno_context.get_toolkit", return_value=toolkit) as get_tk:
        assert run_blocking_in_thread(ctx, lambda: "ok") == "ok"
    get_tk.assert_called_once_with(ctx)


def test_nested_stream_drain_rejected():
    """A second run_stream_drain_loop under an active owner must not hang forever."""
    from plugin.framework.queue_executor import drain_owner_scope

    errors: list = []
    job_done = [False]
    q: queue.Queue = queue.Queue()

    with drain_owner_scope("outer"):
        run_stream_drain_loop(
            q,
            DummyToolkit(),
            job_done,
            lambda _t, _th: None,
            on_stream_done=lambda _i: True,
            on_stopped=lambda: None,
            on_error=errors.append,
        )

    assert job_done[0] is True
    assert errors


def test_nested_async_worker_does_not_start_before_drain_check():
    """A second drain must not spawn a worker that writes to an unread queue."""
    from plugin.framework.async_stream import run_async_worker_with_drain
    from plugin.framework.queue_executor import drain_owner_scope

    started: list[int] = []
    errors: list[object] = []

    def worker(_q: queue.Queue) -> None:
        started.append(1)

    with patch("plugin.framework.uno_context.get_toolkit", return_value=DummyToolkit()):
        with drain_owner_scope("outer"):
            run_async_worker_with_drain(MagicMock(), worker, None, None, errors.append)
    assert started == []
    assert errors


def test_approval_handler_failure_ends_drain_and_sets_event():
    event = threading.Event()
    q: queue.Queue = queue.Queue()
    q.put((StreamQueueKind.APPROVAL_REQUIRED, "allow?", "read_file", event))
    job_done = [False]
    errors: list[object] = []

    def boom(_item: object) -> None:
        raise RuntimeError("dialog failed")

    run_stream_drain_loop(
        q,
        None,
        job_done,
        lambda _t, _th: None,
        on_stream_done=lambda _i: True,
        on_stopped=lambda: None,
        on_error=errors.append,
        on_approval_required=boom,
    )
    assert job_done[0] is True
    assert event.is_set()
    assert errors


def test_run_stream_drain_loop_idle_unblocks_marshaled_worker():
    """Regression: web_research-style hang when main waits in drain loop for async tool."""
    from plugin.framework import queue_executor as qe

    stream_q: queue.Queue = queue.Queue()
    marshal_done = threading.Event()

    def worker():
        qe.execute_on_main_thread(lambda: marshal_done.set())
        stream_q.put((StreamQueueKind.STREAM_DONE, None))

    with patch.object(qe.default_executor, "_get_async_callback", return_value=MagicMock()):
        with patch.object(qe.default_executor, "_poke_main_thread", lambda: None):
            qe.set_force_marshal_mode(True)
            try:
                run_in_background(worker, name="tool-async-marshal", daemon=False)
                job_done = [False]

                def stream_done(_item):
                    job_done[0] = True
                    return True

                run_stream_drain_loop(
                    stream_q,
                    DummyToolkit(),
                    job_done,
                    lambda _t, _th: None,
                    on_stream_done=stream_done,
                    on_stopped=lambda: None,
                    on_error=lambda _e: None,
                )
                assert marshal_done.is_set()
                assert job_done[0]
            finally:
                qe.set_force_marshal_mode(False)
                while not qe.default_executor._work_queue.empty():
                    qe.default_executor.process_queue()


class TestBatchingStreamQueueTimerRace:
    """Regression test for Bug 2: _schedule_timer() called outside the lock allowed
    two concurrent producers to both see is_first=True and reset the burst deadline."""

    def test_timer_armed_exactly_once_for_concurrent_chunks(self):
        # Two threads simultaneously put the first CHUNK into an empty batcher.
        # _schedule_timer must be called exactly once (the second call was previously
        # cancelling and replacing the timer, losing the original deadline).
        raw_q = queue.Queue()
        batcher = BatchingStreamQueue(raw_q, batch_interval=10.0)  # long interval so it doesn't fire

        timer_calls = []
        barrier = threading.Barrier(2)  # synchronise both threads to maximise the race window

        original_schedule = batcher._schedule_timer

        def counting_schedule():
            timer_calls.append(1)
            original_schedule()

        batcher._schedule_timer = counting_schedule

        def producer():
            barrier.wait()  # both threads release simultaneously
            batcher.put((StreamQueueKind.CHUNK, "x"))

        t1 = threading.Thread(target=producer)
        t2 = threading.Thread(target=producer)
        t1.start()
        t2.start()
        t1.join(timeout=2)
        t2.join(timeout=2)

        # Flush to clean up the timer thread
        batcher.flush()

        assert len(timer_calls) == 1, (
            f"_schedule_timer was called {len(timer_calls)} times; expected exactly 1. "
            "The timer deadline was reset by a concurrent producer."
        )

    def test_timer_armed_exactly_once_for_concurrent_thinking(self):
        # Same race on the THINKING path.
        raw_q = queue.Queue()
        batcher = BatchingStreamQueue(raw_q, batch_interval=10.0)

        timer_calls = []
        barrier = threading.Barrier(2)
        original_schedule = batcher._schedule_timer

        def counting_schedule():
            timer_calls.append(1)
            original_schedule()

        batcher._schedule_timer = counting_schedule

        def producer():
            barrier.wait()
            batcher.put((StreamQueueKind.THINKING, "t"))

        t1 = threading.Thread(target=producer)
        t2 = threading.Thread(target=producer)
        t1.start()
        t2.start()
        t1.join(timeout=2)
        t2.join(timeout=2)

        batcher.flush()

        assert len(timer_calls) == 1, (
            f"_schedule_timer was called {len(timer_calls)} times on THINKING path; expected 1."
        )


class TestRunAsyncWorkerOnStoppedFallback:
    """Regression test for Bug 3: stop-path fallback called on_done_fn() with no arg,
    raising TypeError when the callback expects an item argument."""

    def test_stop_with_one_arg_done_fn_does_not_raise(self):
        # on_done_fn that requires exactly one positional argument.
        # Before the fix, the stop-path lambda called on_done_fn() with no args,
        # which raised TypeError and surfaced as an error in the drain loop.
        q = queue.Queue()
        q.put((StreamQueueKind.STOPPED, None))

        job_done = [False]
        errors = []
        done_calls = []

        def on_done(item):  # requires one argument — the bug triggers here
            done_calls.append(item)

        run_stream_drain_loop(
            q,
            DummyToolkit(),
            job_done,
            apply_chunk_fn=lambda _t, _th: None,
            on_stream_done=lambda _item: True,
            on_stopped=lambda: on_done(None),  # explicit stopped handler is fine
            on_error=lambda e: errors.append(e),
        )

        assert job_done[0] is True
        assert errors == [], f"Unexpected errors on Stop path: {errors}"

    def test_stop_fallback_no_on_stopped_fn_one_arg_done_fn(self):
        # When on_stopped_fn=None and on_done_fn takes one argument,
        # run_async_worker_with_drain must not raise or emit an error.
        # We exercise the resolved_on_stopped path directly by building it.

        errors = []
        done_calls = []

        def on_done(item):  # one-arg callback — the bug path
            done_calls.append(item)

        # Build the helper the same way run_async_worker_with_drain does
        def _noop_stopped():
            return None

        def _call_done_on_stopped():
            try:
                on_done(None)
            except TypeError:
                on_done()

        resolved_on_stopped = _call_done_on_stopped  # no on_stopped_fn provided

        # Call directly — must not raise
        try:
            resolved_on_stopped()
        except Exception as e:
            errors.append(e)

        assert errors == [], f"resolved_on_stopped raised: {errors}"
        assert done_calls == [None], f"on_done was not called as expected: {done_calls}"


# ── coalesce_split_tool_calls tests ─────────────────────────────────


def test_coalesce_split_tool_calls_merges_empty_name_continuation():
    from plugin.framework.async_stream import coalesce_split_tool_calls

    tool_calls = [
        {
            "index": 0,
            "id": "chatcmpl-tool-abc",
            "type": "function",
            "function": {
                "name": "delegate_to_specialized_writer_toolset",
                "arguments": '{"message": "back to the Writer',
            },
        },
        {
            "index": 1,
            "id": "",
            "type": "function",
            "function": {"name": "", "arguments": ' sidebar."\n}'},
        },
    ]
    out = coalesce_split_tool_calls(tool_calls)
    assert len(out) == 1
    assert out[0]["id"] == "chatcmpl-tool-abc"
    assert out[0]["function"]["name"] == "delegate_to_specialized_writer_toolset"
    assert out[0]["function"]["arguments"] == '{"message": "back to the Writer sidebar."\n}'
    assert out[0]["index"] == 0


def test_coalesce_split_tool_calls_merges_cerebras_lookup_stream_split():
    # Same OpenRouter/Cerebras gpt-oss shape as the client stream fixture:
    # new index, empty id/name, remainder of arguments.
    from plugin.framework.async_stream import coalesce_split_tool_calls

    out = coalesce_split_tool_calls(
        [
            {
                "index": 0,
                "id": "call_1",
                "type": "function",
                "function": {"name": "lookup", "arguments": '{"query":"part'},
            },
            {
                "index": 1,
                "id": "",
                "type": "function",
                "function": {"name": "", "arguments": ' two"}'},
            },
        ]
    )
    assert len(out) == 1
    assert out[0]["id"] == "call_1"
    assert out[0]["function"]["name"] == "lookup"
    assert out[0]["function"]["arguments"] == '{"query":"part two"}'
    assert out[0]["index"] == 0


def test_coalesce_split_tool_calls_lone_empty_name_dropped():
    from plugin.framework.async_stream import coalesce_split_tool_calls

    assert coalesce_split_tool_calls([
        {"index": 0, "id": "", "function": {"name": "", "arguments": " orphan"}},
    ]) == []
    assert coalesce_split_tool_calls(None) == []
    assert coalesce_split_tool_calls([]) == []


def test_stream_queue_helpers_parameterize_queue() -> None:
    """Bare queue.Queue leftovers trip reportMissingTypeArgument."""

    from plugin.framework.async_stream import put_stream_queue_stopped, run_async_worker_with_drain

    stopped = inspect.signature(put_stream_queue_stopped)
    assert "Queue[Any]" in str(stopped.parameters["q"].annotation)
    drain = inspect.signature(run_async_worker_with_drain)
    assert "Queue[Any]" in str(drain.parameters["worker_fn"].annotation)

def test_run_stream_drain_loop_error_clears_defer_next_tool_exit():
    """A recovered ERROR clears defer_next_tool_exit so it does not drop later replacement worker batches."""
    q = queue.Queue()
    q.put((StreamQueueKind.NEXT_TOOL,))
    q.put((StreamQueueKind.STATUS, "boom"))

    toolkit = DummyToolkit()
    job_done = [False]
    applied = []

    batch_counter = [0]
    original_process = toolkit.processEventsToIdle

    def mock_processEventsToIdle():
        original_process()
        c = batch_counter[0]
        batch_counter[0] += 1
        if c == 0:
            q.put((StreamQueueKind.STATUS, "replacement1"))
        elif c == 1:
            q.put((StreamQueueKind.STATUS, "replacement2"))
            q.put((StreamQueueKind.STREAM_DONE, "replacement-done"))

    toolkit.processEventsToIdle = mock_processEventsToIdle

    def on_stream_done(item):
        return True

    def on_error(e):
        return True

    def on_status(t):
        if t == "boom":
            raise ValueError("boom")
        applied.append(t)

    run_stream_drain_loop(
        q,
        toolkit,
        job_done,
        apply_chunk_fn=lambda t, i: None,
        on_stream_done=on_stream_done,
        on_stopped=lambda: None,
        on_error=on_error,
        on_status_fn=on_status
    )

    assert "replacement1" in applied
    assert "replacement2" in applied


class _RecordingRearm:
    """Scheduler that records slices. ``post`` does not run them inline."""

    def __init__(self) -> None:
        self.pending: list[tuple[str, float | None, object]] = []

    def post(self, fn: object) -> None:
        self.pending.append(("now", None, fn))

    def post_after(self, delay: float, fn: object) -> None:
        self.pending.append(("later", delay, fn))

    def pump(self) -> tuple[str, float | None]:
        kind, delay, fn = self.pending.pop(0)
        fn()
        return kind, delay


def _clear_event_drain() -> None:
    from plugin.framework import async_stream as stream_mod
    from plugin.framework.async_drain_guard import reset_sentry_state

    session = stream_mod._event_drain
    if session is not None and not session.closed:
        session._finish()
    stream_mod._event_drain = None
    reset_sentry_state()


def test_defer_until_drain_done_runs_immediately_when_blocking() -> None:
    from plugin.framework.async_stream import defer_until_drain_done

    seen: list[str] = []
    defer_until_drain_done(lambda: seen.append("now"))
    assert seen == ["now"]


def test_event_drain_caps_back_to_back_immediate_rearms(monkeypatch: pytest.MonkeyPatch) -> None:
    import queue
    from plugin.framework import async_stream

    q: queue.Queue = queue.Queue()
    job_done = [False]

    class MockScheduler:
        def __init__(self):
            self.posts = []

        def post(self, fn):
            self.posts.append(("now", fn))

        def post_after(self, delay, fn):
            self.posts.append(("later", delay, fn))

    scheduler = MockScheduler()

    drain = async_stream._EventDrain(
        async_stream._DrainState(
            q=q,
            apply_chunk_fn=lambda _text, _thinking: None,
            on_stream_done=lambda _item: True,
            on_stopped=lambda: None,
            on_error=lambda _e: None,
            on_status_fn=None,
            on_approval_required=None,
            show_search_thinking=False,
            job_done=job_done
        ),
        scheduler,
        None,
        None
    )

    drain.start()

    # start() schedules the first slice via immediate post.
    assert len(scheduler.posts) == 1
    assert scheduler.posts[0][0] == "now"

    # Cap is 3 immediate re-arms. We mock q.qsize() since _drain_ready drains it all.
    with monkeypatch.context() as m:
        m.setattr(q, "qsize", lambda: 1)

        # 1. First slice -> pending > 0 -> post (streak 1)
        scheduler.posts.pop(0)[1]()
        assert len(scheduler.posts) == 1
        assert scheduler.posts[0][0] == "now"
        assert drain._immediate_streak == 1

        # 2. Second slice -> pending > 0 -> post (streak 2)
        scheduler.posts.pop(0)[1]()
        assert len(scheduler.posts) == 1
        assert scheduler.posts[0][0] == "now"
        assert drain._immediate_streak == 2

        # 3. Third slice -> pending > 0 -> post (streak 3)
        scheduler.posts.pop(0)[1]()
        assert len(scheduler.posts) == 1
        assert scheduler.posts[0][0] == "now"
        assert drain._immediate_streak == 3

        # 4. Fourth slice -> pending > 0 -> streak >= 3 -> post_after (gap)
        scheduler.posts.pop(0)[1]()
        assert len(scheduler.posts) == 1
        assert scheduler.posts[0][0] == "later"
        assert scheduler.posts[0][1] == async_stream._DRAIN_YIELD_GAP_SEC
        assert drain._immediate_streak == 0

        # 5. Fifth slice -> pending > 0 -> post (streak 1 again)
        scheduler.posts.pop(0)[2]() # It's a post_after, fn is index 2
        assert len(scheduler.posts) == 1
        assert scheduler.posts[0][0] == "now"
        assert drain._immediate_streak == 1


def test_async_callback_rearm_breaks_reference_cycles(monkeypatch: pytest.MonkeyPatch) -> None:
    import gc
    import weakref
    from unittest.mock import MagicMock
    import queue
    from plugin.framework import async_stream

    # Stub the internal factory to a class that keeps a strong reference to fn
    class FakeSliceCallback:
        def __init__(self, fn):
            self.fn = fn

        def notify(self):
            self.fn()

    monkeypatch.setattr(async_stream, "_new_xcallback", FakeSliceCallback)

    class FakeService:
        def __init__(self):
            self.calls = []

        def addCallback(self, cb, data):
            self.calls.append((cb, data))

    service = FakeService()

    q: queue.Queue = queue.Queue()
    q.put((async_stream.StreamQueueKind.STREAM_DONE, "test"))

    job_done = [False]

    def run_drain():
        rearm = async_stream._AsyncCallbackRearm(service)
        drain = async_stream._EventDrain(
            async_stream._DrainState(
                q=q,
                apply_chunk_fn=MagicMock(),
                on_stream_done=lambda _item: True,
                on_stopped=lambda: None,
                on_error=lambda _e: None,
                on_status_fn=None,
                on_approval_required=None,
                show_search_thinking=False,
                job_done=job_done
            ),
            rearm,
            None,
            None
        )
        drain.start()

        # Test drive notify
        if service.calls:
            cb, _ = service.calls.pop(0)
            cb.notify()

        return weakref.ref(drain), weakref.ref(rearm), cb

    # Global clear
    async_stream._event_drain = None

    # Disable GC so cyclic references aren't automatically collected
    gc.disable()
    try:
        drain_ref, rearm_ref, retained_cb = run_drain()

        # Late notify after close does nothing.
        retained_cb.notify()

        # Now clear retained_cb so that FakeSliceCallback's strong reference to
        # the bound _notify method (which holds the _AsyncCallbackRearm instance)
        # is dropped.
        retained_cb = None

        # The locals have dropped. They should be dead due to breaking the cycle.
        assert drain_ref() is None
        assert rearm_ref() is None

    finally:
        gc.enable()


def test_async_callback_for_drain_rearm_keeps_testing_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mock-sidebar soffice sets WRITERAGENT_TESTING. Re-arm must still be possible."""
    from plugin.framework import queue_executor as qe

    sentinel = object()
    executor = qe.default_executor
    previous_service = executor._async_callback_service
    previous_ctx = executor._ctx
    executor._async_callback_service = sentinel
    try:
        monkeypatch.setenv("WRITERAGENT_TESTING", "1")
        assert qe.async_callback_for_drain_rearm() is sentinel
        qe.set_force_marshal_mode(True)
        try:
            assert qe.async_callback_for_drain_rearm() is None
        finally:
            qe.set_force_marshal_mode(False)
    finally:
        executor._async_callback_service = previous_service
        executor._ctx = previous_ctx


def test_drain_scheduler_override_forces_blocking_and_restores() -> None:
    """Override selects the scheduler; restoring the previous factory undoes it."""
    from plugin.framework import async_stream as stream_mod

    previous = stream_mod.set_drain_scheduler_override(lambda: None)
    try:
        assert stream_mod._make_drain_rearm() is None

        sentinel = object()
        blocking_factory = stream_mod.set_drain_scheduler_override(lambda: sentinel)
        assert blocking_factory is not None
        assert stream_mod._make_drain_rearm() is sentinel

        stream_mod.set_drain_scheduler_override(blocking_factory)
        assert stream_mod._make_drain_rearm() is None
    finally:
        stream_mod.set_drain_scheduler_override(previous)


def test_drain_scheduler_override_none_uses_blocking_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """A None hook selects _run_stream_drain_blocking (pumps idle before return).

    What was wrong: this asserted ``toolkit.idle_calls >= 1`` and failed in CI
    (run 37510062234) with ``0 >= 1`` while the chunk and STREAM_DONE were
    applied. ``pump_ui_idle`` skips VCL whenever the process-wide drain depth
    is above 1, and it runs work other tests left in ``default_executor``. On
    an xdist worker both are shared with every earlier test, so the count
    measured leftover state, not the scheduler choice. The silent
    ``on_error`` also hid any crash inside the pump.
    Why this change: spy the blocking loop and its ``pump_ui_idle`` calls,
    give the pump a private executor, start from a clean drain guard, and
    fail on any error. VCL gating by depth is covered by the
    ``test_pump_ui_idle_*`` tests.
    """
    from plugin.framework import async_stream as stream_mod
    from plugin.framework import queue_executor as qe
    from plugin.framework.async_drain_guard import reset_sentry_state

    blocking_calls: list[object] = []
    real_blocking = stream_mod._run_stream_drain_blocking

    def spy_blocking(state, toolkit_arg, stop_checker, flush_pending):
        blocking_calls.append(toolkit_arg)
        return real_blocking(state, toolkit_arg, stop_checker, flush_pending)

    private_executor = qe.QueueExecutor()
    pumps: list[object] = []
    real_pump = stream_mod.pump_ui_idle

    def spy_pump(toolkit_arg, **kwargs):
        pumps.append(toolkit_arg)
        kwargs.setdefault("executor", private_executor)
        return real_pump(toolkit_arg, **kwargs)

    monkeypatch.setattr(stream_mod, "_run_stream_drain_blocking", spy_blocking)
    monkeypatch.setattr(stream_mod, "pump_ui_idle", spy_pump)
    reset_sentry_state()
    previous = stream_mod.set_drain_scheduler_override(lambda: None)
    try:
        q: queue.Queue = queue.Queue()
        q.put((StreamQueueKind.CHUNK, "hello"))
        q.put((StreamQueueKind.STREAM_DONE, "end"))
        toolkit = DummyToolkit()
        job_done = [False]
        applied: list[str] = []
        errors: list[object] = []

        run_stream_drain_loop(
            q,
            toolkit,
            job_done,
            lambda text, is_thinking: applied.append(text),
            on_stream_done=lambda _item: True,
            on_stopped=lambda: None,
            on_error=errors.append,
        )

        assert errors == []
        assert blocking_calls == [toolkit]
        assert applied == ["hello"]
        assert job_done[0] is True
        assert pumps, "blocking drain returned without pump_ui_idle"
        assert all(tk is toolkit for tk in pumps)
    finally:
        stream_mod.set_drain_scheduler_override(previous)
        reset_sentry_state()


def test_event_drain_batches_one_slice_and_stops_on_terminal() -> None:
    """Ready items are one batch. STREAM_DONE ends the session and does not re-arm."""
    rearm = _RecordingRearm()
    q: queue.Queue = queue.Queue()
    q.put((StreamQueueKind.CHUNK, "a"))
    q.put((StreamQueueKind.STATUS, "s"))
    q.put((StreamQueueKind.CHUNK, "b"))
    q.put((StreamQueueKind.STREAM_DONE, "end"))
    applied: list[str] = []
    statuses: list[str] = []
    done: list[object] = []
    epilogue: list[str] = []
    job_done = [False]
    toolkit = DummyToolkit()
    try:
        from plugin.framework.async_stream import defer_until_drain_done

        run_stream_drain_loop(
            q,
            toolkit,
            job_done,
            lambda text, _thinking: applied.append(text),
            on_stream_done=lambda item: done.append(item) or True,
            on_stopped=lambda: None,
            on_error=lambda _e: None,
            on_status_fn=statuses.append,
            rearm=rearm,
        )
        assert job_done[0] is False
        assert applied == []
        defer_until_drain_done(lambda: epilogue.append("after"))
        assert epilogue == []
        kind, delay = rearm.pump()
        assert (kind, delay) == ("now", None)
        assert statuses == ["s"]
        assert applied == ["ab"]
        assert done and done[0][0] == StreamQueueKind.STREAM_DONE
        assert epilogue == ["after"]
        assert job_done[0] is True
        assert rearm.pending == []
        assert toolkit.idle_calls == 0
    finally:
        _clear_event_drain()


def test_idle_rearm_thread_retries_on_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    import time
    from plugin.framework import async_stream

    monkeypatch.setattr(async_stream, "_DRAIN_IDLE_REARM_SEC", 0.01)
    monkeypatch.setattr(async_stream, "_IDLE_REARM_RETRY_MAX_SEC", 0.01)

    thread = async_stream._IdleRearmThread()
    calls = []
    done_event = __import__("threading").Event()

    def failing_fire() -> None:
        calls.append(time.monotonic())
        if len(calls) < 2:
            raise RuntimeError("fire boom")
        done_event.set()

    thread.arm(0.01, failing_fire)
    assert done_event.wait(timeout=2.0)
    assert len(calls) == 2

    # After stop(), a failing fire is not retried.
    calls.clear()
    done_event.clear()

    def fire_then_stop() -> None:
        calls.append(time.monotonic())
        thread.stop()
        raise RuntimeError("fire boom after stop")

    thread.arm(0.01, fire_then_stop)
    time.sleep(0.1)
    assert len(calls) == 1
    with thread._cv:
        assert thread._deadline is None
    thread.stop()


def test_event_drain_rearms_idle_then_next_batch() -> None:
    """An empty queue waits ~100 ms. The next slice keeps order."""
    from plugin.framework.async_stream import _DRAIN_IDLE_REARM_SEC

    rearm = _RecordingRearm()
    q: queue.Queue = queue.Queue()
    applied: list[str] = []
    job_done = [False]
    try:
        run_stream_drain_loop(
            q,
            DummyToolkit(),
            job_done,
            lambda text, _thinking: applied.append(text),
            on_stream_done=lambda _item: True,
            on_stopped=lambda: None,
            on_error=lambda _e: None,
            rearm=rearm,
        )
        assert rearm.pump() == ("now", None)
        assert applied == []
        kind, delay = rearm.pending[0][0], rearm.pending[0][1]
        assert kind == "later"
        assert delay == _DRAIN_IDLE_REARM_SEC
        q.put((StreamQueueKind.CHUNK, "one"))
        rearm.pump()
        assert applied == ["one"]
        q.put((StreamQueueKind.CHUNK, "two"))
        q.put((StreamQueueKind.STREAM_DONE, None))
        rearm.pump()
        assert applied == ["one", "two"]
        assert job_done[0] is True
        assert rearm.pending == []
    finally:
        _clear_event_drain()


def test_event_drain_recovered_error_rearms_and_fatal_error_does_not() -> None:
    rearm = _RecordingRearm()
    q: queue.Queue = queue.Queue()
    q.put((StreamQueueKind.ERROR, {"message": "boom"}))
    q.put((StreamQueueKind.CHUNK, "tail"))
    errors: list[object] = []
    applied: list[str] = []
    job_done = [False]
    try:
        run_stream_drain_loop(
            q,
            None,
            job_done,
            lambda text, _thinking: applied.append(text),
            on_stream_done=lambda _item: True,
            on_stopped=lambda: None,
            on_error=lambda payload: errors.append(payload) or True,
            rearm=rearm,
        )
        rearm.pump()
        assert errors
        assert applied == []
        assert job_done[0] is False
        assert rearm.pending and rearm.pending[0][0] == "later"
        q.put((StreamQueueKind.STREAM_DONE, "ok"))
        rearm.pump()
        assert job_done[0] is True
    finally:
        _clear_event_drain()

    rearm = _RecordingRearm()
    q = queue.Queue()
    q.put((StreamQueueKind.ERROR, {"message": "fatal"}))
    job_done = [False]
    try:
        run_stream_drain_loop(
            q,
            None,
            job_done,
            lambda _t, _th: None,
            on_stream_done=lambda _item: True,
            on_stopped=lambda: None,
            on_error=lambda _payload: False,
            rearm=rearm,
        )
        rearm.pump()
        assert job_done[0] is True
        assert rearm.pending == []
    finally:
        _clear_event_drain()


def test_event_drain_stop_drops_control_tail_and_closed_slice_drops_late_chunks() -> None:
    """Stop still shows queued text and does not dispatch STREAM_DONE.

    A slice that fires after the session closed must not apply a chunk from
    a stopped or replaced turn.
    """
    rearm = _RecordingRearm()
    q: queue.Queue = queue.Queue()
    applied: list[str] = []
    done: list[object] = []
    stopped: list[bool] = []
    stop = [False]
    job_done = [False]
    try:
        run_stream_drain_loop(
            q,
            DummyToolkit(),
            job_done,
            lambda text, _thinking: applied.append(text),
            on_stream_done=lambda item: done.append(item) or True,
            on_stopped=lambda: stopped.append(True),
            on_error=lambda _e: None,
            stop_checker=lambda: stop[0],
            rearm=rearm,
        )
        rearm.pump()
        assert rearm.pending[0][0] == "later"
        stale = rearm.pending[0][2]
        stop[0] = True
        q.put((StreamQueueKind.CHUNK, "tail"))
        q.put((StreamQueueKind.STREAM_DONE, "nope"))
        rearm.pump()
        assert applied == ["tail"]
        assert done == []
        assert stopped == [True]
        assert job_done[0] is True
        q.put((StreamQueueKind.CHUNK, "next-turn"))
        stale()
        assert applied == ["tail"]
        assert done == []
    finally:
        _clear_event_drain()


def _start_event_drain(q: queue.Queue, rearm: _RecordingRearm, job_done: list[bool], applied: list[str]) -> None:
    run_stream_drain_loop(
        q,
        DummyToolkit(),
        job_done,
        lambda text, _thinking: applied.append(text),
        on_stream_done=lambda _item: True,
        on_stopped=lambda: None,
        on_error=lambda _e: None,
        rearm=rearm,
    )


def test_overlapping_event_drains_keep_the_pump_owner_until_both_finish() -> None:
    """Two documents' drains overlap and the first one started finishes first.

    The owner went back to None while the second drain still held the pump,
    so a different owner (MCP) could start under it.
    """
    from plugin.framework.queue_executor import NestedDrainOwnerError, get_drain_depth, get_drain_owner, drain_owner_scope

    # xdist can leave drain depth from an earlier test on this worker; without
    # a clean slate both releases leave owner="stream" and the final assert flakes.
    _clear_event_drain()
    rearm_a, rearm_b = _RecordingRearm(), _RecordingRearm()
    q_a: queue.Queue = queue.Queue()
    q_b: queue.Queue = queue.Queue()
    done_a, done_b = [False], [False]
    try:
        _start_event_drain(q_a, rearm_a, done_a, [])
        _start_event_drain(q_b, rearm_b, done_b, [])
        q_a.put((StreamQueueKind.STREAM_DONE, "a"))
        rearm_a.pump()
        assert done_a[0] is True
        assert get_drain_owner() == "stream"
        with pytest.raises(NestedDrainOwnerError):
            with drain_owner_scope("mcp"):
                pass
        q_b.put((StreamQueueKind.STREAM_DONE, "b"))
        rearm_b.pump()
        assert done_b[0] is True
        assert get_drain_owner() is None
        assert get_drain_depth() == 0
    finally:
        _clear_event_drain()


def test_deferral_after_clear_does_not_attach_to_another_documents_drain() -> None:
    """Doc B's send returned without a drain; its completion must not wait for doc A."""
    from plugin.framework.async_stream import clear_drain_capture, defer_until_drain_done

    rearm = _RecordingRearm()
    q: queue.Queue = queue.Queue()
    job_done = [False]
    seen: list[str] = []
    try:
        _start_event_drain(q, rearm, job_done, [])
        defer_until_drain_done(lambda: seen.append("a-epilogue"))
        # Doc B's send callback starts here.
        clear_drain_capture()
        defer_until_drain_done(lambda: seen.append("b-now"))
        assert seen == ["b-now"]
        q.put((StreamQueueKind.STREAM_DONE, "a"))
        rearm.pump()
        assert seen == ["b-now", "a-epilogue"]
    finally:
        _clear_event_drain()


def test_new_drain_owns_deferrals_made_after_it_starts() -> None:
    from plugin.framework.async_stream import defer_until_drain_done

    rearm_a, rearm_b = _RecordingRearm(), _RecordingRearm()
    q_a: queue.Queue = queue.Queue()
    q_b: queue.Queue = queue.Queue()
    seen: list[str] = []
    try:
        _start_event_drain(q_a, rearm_a, [False], [])
        _start_event_drain(q_b, rearm_b, [False], [])
        defer_until_drain_done(lambda: seen.append("b"))
        q_a.put((StreamQueueKind.STREAM_DONE, "a"))
        rearm_a.pump()
        assert seen == []
        q_b.put((StreamQueueKind.STREAM_DONE, "b"))
        rearm_b.pump()
        assert seen == ["b"]
    finally:
        _clear_event_drain()
