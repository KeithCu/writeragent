# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for async_stream core drain loop and basic worker."""

import logging
import queue
import time
from unittest.mock import MagicMock, patch


from plugin.framework.async_stream import (
    StreamQueueKind,
    run_stream_drain_loop,
)
from plugin.framework.worker_pool import run_in_background
from tests.framework.stream_test_helpers import DummyToolkit


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


def test_worker_exception_on_error_true_still_applies_later_chunk(caplog):
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

    with caplog.at_level(logging.ERROR, logger="plugin.framework.async_stream"):
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
    assert not any("Worker raised after terminal item was already queued" in r.message for r in caplog.records)


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


def test_worker_error_put_then_raise_queues_single_error(caplog):
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

    with caplog.at_level(logging.ERROR, logger="plugin.framework.async_stream"):
        with patch("plugin.framework.uno_context.get_toolkit", return_value=toolkit):
            run_async_worker_with_drain(
                ctx,
                worker,
                lambda text, _is_thinking: applied.append(text),
                lambda _item: None,
                on_error,
                q=shared,
            )
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if any("Worker raised after terminal item was already queued" in r.message for r in caplog.records):
                break
            time.sleep(0.01)

    assert len(errors) == 1
    assert errors[0] == {"message": "first error"}
    assert "recovered" in applied
    assert any("Worker raised after terminal item was already queued" in r.message for r in caplog.records)


def test_non_tuple_terminal_skips_wrapper_stream_done():
    """A list or bare kind is terminal, so the wrapper must not add STREAM_DONE."""
    from plugin.framework.async_stream import run_async_worker_with_drain

    def _run(terminal):
        ctx = MagicMock()
        toolkit = DummyToolkit()
        shared: queue.Queue = queue.Queue()
        done = []

        def worker(worker_q):
            worker_q.put(terminal)

        with patch("plugin.framework.uno_context.get_toolkit", return_value=toolkit):
            run_async_worker_with_drain(
                ctx,
                worker,
                lambda _text, _is_thinking: None,
                lambda item: done.append(item),
                lambda _err: None,
                q=shared,
            )

        assert shared.empty()
        assert done == [terminal]

    _run([StreamQueueKind.STREAM_DONE, "from-list"])
    _run(StreamQueueKind.STREAM_DONE)


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


def test_invalid_tag_on_error_raise_is_not_called_twice():
    """A raising on_error for a bare string tag is invoked once."""
    q = queue.Queue()
    q.put(("chunk", "bad"))
    errors: list[object] = []

    def on_error(payload: object) -> None:
        errors.append(payload)
        raise RuntimeError("on_error error")

    job_done = [False]
    run_stream_drain_loop(
        q,
        None,
        job_done,
        lambda _text, _th: None,
        on_stream_done=lambda _item: True,
        on_stopped=lambda: None,
        on_error=on_error,
    )
    assert job_done[0] is True
    assert len(errors) == 1


def test_stopped_item_on_stopped_raise_is_not_an_error():
    """A raising on_stopped for a STOPPED item does not call on_error."""
    q = queue.Queue()
    q.put((StreamQueueKind.STOPPED, None))
    stopped: list[bool] = []
    errors: list[object] = []

    def on_stopped() -> None:
        stopped.append(True)
        raise RuntimeError("stop handler failed")

    job_done = [False]
    run_stream_drain_loop(
        q,
        None,
        job_done,
        lambda _text, _th: None,
        on_stream_done=lambda _item: True,
        on_stopped=on_stopped,
        on_error=errors.append,
    )
    assert job_done[0] is True
    assert stopped == [True]
    assert errors == []


def test_stop_checker_on_stopped_raise_is_not_an_error():
    """A raising on_stopped from the stop checker does not call on_error."""
    q = queue.Queue()
    q.put((StreamQueueKind.CHUNK, "hello"))
    stopped: list[bool] = []
    errors: list[object] = []

    def on_stopped() -> None:
        stopped.append(True)
        raise RuntimeError("stop handler failed")

    job_done = [False]
    run_stream_drain_loop(
        q,
        None,
        job_done,
        lambda _text, _th: None,
        on_stream_done=lambda _item: True,
        on_stopped=on_stopped,
        on_error=errors.append,
        stop_checker=lambda: True,
    )
    assert job_done[0] is True
    assert stopped == [True]
    assert errors == []


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
