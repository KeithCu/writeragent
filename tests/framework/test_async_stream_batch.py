# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for async_stream batching, flushing, error handling, and drain guards."""

import queue
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from plugin.framework.stream_batch import BatchingStreamQueue, StreamQueueKind
from plugin.framework.async_stream import StreamQueue, run_stream_drain_loop
from plugin.framework.worker_pool import run_in_background
from tests.framework.stream_test_helpers import DummyToolkit


class TestAsyncStreamErrorHandling:

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
    orig = async_stream._DISPATCH[StreamQueueKind.CHUNK]
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
    orig = async_stream._DISPATCH[StreamQueueKind.THINKING]
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
    orig = async_stream._DISPATCH[StreamQueueKind.CHUNK]
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
    orig = async_stream._DISPATCH[StreamQueueKind.CHUNK]
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


def test_async_worker_flushes_queue_with_raw_and_flush():
    """A queue with raw and flush is flushed before the terminal item.

    The drain must not require a BatchingStreamQueue instance. Text stays
    off the raw queue until flush, so a missed flush leaves the drain empty.
    """
    from plugin.framework.async_stream import run_async_worker_with_drain

    ctx = MagicMock()
    toolkit = DummyToolkit()
    raw: StreamQueue = StreamQueue()

    class _HoldingQueue:
        def __init__(self) -> None:
            self.raw = raw
            self._held: list[object] = []

        def put(self, item: object) -> None:
            self._held.append(item)

        def flush(self) -> None:
            while self._held:
                self.raw.put(self._held.pop(0))

    applied: list[str] = []
    holder = _HoldingQueue()

    def worker(worker_q):
        worker_q.put((StreamQueueKind.CHUNK, "kept"))

    with patch("plugin.framework.uno_context.get_toolkit", return_value=toolkit):
        run_async_worker_with_drain(
            ctx,
            worker,
            lambda text, _is_thinking: applied.append(text),
            lambda _item: None,
            lambda _e: None,
            q=holder,
        )

    assert applied == ["kept"]
    assert holder._held == []


def test_async_worker_stop_flushes_batcher_before_worker_returns():
    """Stop must flush a BatchingStreamQueue before the worker's finally."""
    from plugin.framework.async_stream import run_async_worker_with_drain

    ctx = MagicMock()
    toolkit = DummyToolkit()
    batched = BatchingStreamQueue(StreamQueue(), batch_interval=30.0)
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
    """A raising flush must not swallow ERROR."""
    from plugin.framework.async_stream import run_async_worker_with_drain

    ctx = MagicMock()
    toolkit = DummyToolkit()
    raw: StreamQueue = StreamQueue()
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
    assert raw._terminal_flags == []
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


def test_stop_tail_drops_string_kind():
    """A bare "chunk" string is not display text. Stop must not apply it or error."""
    q = queue.Queue()
    q.put((StreamQueueKind.CHUNK, "hello"))
    q.put(("chunk", "bad"))
    q.put((StreamQueueKind.STREAM_DONE, "nope"))
    applied: list[str] = []
    errors: list[object] = []
    stopped: list[bool] = []
    checks = [0]

    def stop_checker() -> bool:
        checks[0] += 1
        return checks[0] > 1

    job_done = [False]
    run_stream_drain_loop(
        q,
        None,
        job_done,
        lambda text, _is_thinking: applied.append(text),
        on_stream_done=lambda _item: True,
        on_stopped=lambda: stopped.append(True),
        on_error=errors.append,
        stop_checker=stop_checker,
    )
    assert job_done[0] is True
    assert stopped == [True]
    assert errors == []
    assert "hello" in applied
    assert "bad" not in applied


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
    from plugin.framework.async_drain_guard import NestedDrainOwnerError, drain_owner_scope, get_drain_owner

    with drain_owner_scope("stream"):
        assert get_drain_owner() == "stream"
        with pytest.raises(NestedDrainOwnerError):
            with drain_owner_scope("nested"):
                pass
    assert get_drain_owner() is None


def test_pump_ui_idle_still_pumps_vcl_under_drain_owner():
    """Owner path must keep pumping so Send stays responsive / Stop works."""
    from plugin.framework import queue_executor as qe
    from plugin.framework.async_drain_guard import drain_owner_scope

    toolkit = DummyToolkit()
    with drain_owner_scope("stream"):
        qe.pump_ui_idle(toolkit)
    assert toolkit.idle_calls >= 1


def test_pump_ui_idle_skips_vcl_when_drain_depth_gt_1_but_drains_queue():
    """Same-owner re-entry must not pump VCL again, and must still run queued work."""
    from plugin.framework import queue_executor as qe
    from plugin.framework.async_drain_guard import (
        drain_owner_scope,
        get_drain_depth,
        get_suppressed_vcl_pump_count,
        reset_sentry_state,
    )

    reset_sentry_state()
    toolkit = DummyToolkit()
    ran: list[str] = []
    ex = qe.QueueExecutor()
    ex._enqueue_work(lambda: ran.append("q"), (), {}, blocking=False)
    with drain_owner_scope("stream"):
        assert get_drain_depth() == 1
        with drain_owner_scope("stream"):
            assert get_drain_depth() == 2
            qe.pump_ui_idle(toolkit, executor=ex)
    assert toolkit.idle_calls == 0
    assert ran == ["q"]
    assert get_suppressed_vcl_pump_count() >= 1
    reset_sentry_state()


def test_nested_stream_drain_rejected():
    """A second run_stream_drain_loop under an active owner must not hang forever."""
    from plugin.framework.async_drain_guard import drain_owner_scope

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


@pytest.mark.parametrize("owner", ["outer", "stream"])
def test_nested_async_worker_does_not_start_before_drain_check(owner: str):
    """A second drain must not spawn a worker; both outer and same-name stream owners are refused."""
    from plugin.framework.async_stream import run_async_worker_with_drain
    from plugin.framework.async_drain_guard import drain_owner_scope

    started: list[int] = []
    errors: list[object] = []

    def worker(_q: queue.Queue) -> None:
        started.append(1)

    with patch("plugin.framework.uno_context.get_toolkit", return_value=DummyToolkit()):
        with drain_owner_scope(owner):
            run_async_worker_with_drain(MagicMock(), worker, None, None, errors.append)
    assert started == []
    assert len(errors) == 1


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


def test_approval_without_handler_sets_event_and_keeps_draining():
    """No dialog handler must still unblock the worker and keep the drain going."""
    event = threading.Event()
    q: queue.Queue = queue.Queue()
    q.put((StreamQueueKind.APPROVAL_REQUIRED, "allow?", "read_file", event))
    q.put((StreamQueueKind.STREAM_DONE, "after"))
    done = []
    job_done = [False]

    def on_stream_done(item):
        done.append(item)
        return True

    run_stream_drain_loop(
        q,
        None,
        job_done,
        lambda _t, _th: None,
        on_stream_done=on_stream_done,
        on_stopped=lambda: None,
        on_error=lambda _e: None,
        on_approval_required=None,
    )
    assert event.is_set()
    assert job_done[0] is True
    assert done and done[0][1] == "after"


def test_stop_sets_approval_event_without_dialog():
    """Stop sets an approval event already pulled and does not open the dialog."""
    event = threading.Event()
    q: queue.Queue = queue.Queue()
    q.put((StreamQueueKind.CHUNK, "hello"))
    q.put((StreamQueueKind.APPROVAL_REQUIRED, "allow?", "read_file", event))
    q.put((StreamQueueKind.STREAM_DONE, "nope"))
    dialogs: list[object] = []
    stopped: list[bool] = []
    checks = [0]

    def stop_checker() -> bool:
        checks[0] += 1
        return checks[0] > 1

    job_done = [False]
    run_stream_drain_loop(
        q,
        None,
        job_done,
        lambda _text, _th: None,
        on_stream_done=lambda _item: True,
        on_stopped=lambda: stopped.append(True),
        on_error=lambda _e: None,
        on_approval_required=dialogs.append,
        stop_checker=stop_checker,
    )
    assert job_done[0] is True
    assert stopped == [True]
    assert event.is_set()
    assert dialogs == []


def test_error_item_on_error_raise_is_not_called_twice():
    """A raising on_error for an ERROR item is invoked once, with the original payload."""
    q: queue.Queue = queue.Queue()
    q.put((StreamQueueKind.ERROR, {"message": "boom"}))
    q.put((StreamQueueKind.CHUNK, "later"))
    errors: list[object] = []
    applied: list[str] = []

    def on_error(payload: object) -> None:
        errors.append(payload)
        raise RuntimeError("on_error error")

    job_done = [False]
    run_stream_drain_loop(
        q,
        None,
        job_done,
        lambda text, _th: applied.append(text),
        on_stream_done=lambda _item: True,
        on_stopped=lambda: None,
        on_error=on_error,
    )
    assert job_done[0] is True
    assert errors == [{"message": "boom"}]
    assert applied == []


def test_idle_rearm_cancel_resets_failures():
    from plugin.framework.async_stream import _IdleRearmThread

    timer = _IdleRearmThread()
    timer._failures = 4
    timer.cancel()
    assert timer._failures == 0


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
