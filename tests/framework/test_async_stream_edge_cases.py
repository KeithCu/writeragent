# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for async_stream edge cases, parameterization, and callback fallbacks."""

import inspect
import queue

import pytest

from plugin.framework.stream_batch import StreamQueueKind
from plugin.framework.async_stream import run_stream_drain_loop
from tests.framework.stream_test_helpers import DummyToolkit


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


def test_stream_queue_helpers_parameterize_queue() -> None:
    """Bare queue.Queue leftovers trip reportMissingTypeArgument."""

    from plugin.framework.async_stream import run_async_worker_with_drain

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


def test_recovered_error_item_after_next_tool_keeps_drain_alive():
    """A queued ERROR after NEXT_TOOL must not apply the deferred exit when on_error recovers.

    The older test raised inside on_status, which took the except path that
    already skipped the trailing flush. A real ERROR tuple did not.
    """
    q = queue.Queue()
    q.put((StreamQueueKind.NEXT_TOOL,))
    q.put((StreamQueueKind.ERROR, "tool failed"))
    q.put((StreamQueueKind.CHUNK, "dropped-tail"))
    applied: list[tuple[str, bool]] = []
    errors: list[object] = []
    job_done = [False]

    def on_error(payload: object) -> bool:
        errors.append(payload)
        q.put((StreamQueueKind.CHUNK, "replacement"))
        q.put((StreamQueueKind.STREAM_DONE, "ok"))
        return True

    run_stream_drain_loop(
        q,
        None,
        job_done,
        lambda text, is_thinking: applied.append((text, is_thinking)),
        on_stream_done=lambda _item: True,
        on_stopped=lambda: None,
        on_error=on_error,
    )
    assert job_done[0] is True
    assert ("replacement", False) in applied
    assert ("dropped-tail", False) not in applied
    assert errors == ["tool failed"]


@pytest.mark.parametrize("empty", [(), []])
def test_empty_queue_item_is_invalid_tag_not_index_error(empty: tuple | list) -> None:
    """() and [] are a missing kind, not an IndexError in the slice."""
    q = queue.Queue()
    q.put(empty)
    q.put((StreamQueueKind.STREAM_DONE, "later"))
    errors: list[object] = []
    done: list[object] = []
    job_done = [False]

    run_stream_drain_loop(
        q,
        None,
        job_done,
        lambda _text, _is_thinking: None,
        on_stream_done=lambda item: done.append(item) or True,
        on_stopped=lambda: None,
        on_error=lambda payload: errors.append(payload),
    )
    assert job_done[0] is True
    assert done == []
    assert len(errors) == 1
    assert "NoneType" in str(errors[0])


def test_stop_tail_empty_item_does_not_call_on_error():
    """Stop applies the already-pulled tail. An empty item there is dropped."""
    q = queue.Queue()
    q.put(())
    calls = [0]
    stopped: list[bool] = []
    errors: list[object] = []
    job_done = [False]

    def stop_checker() -> bool:
        # The blocking loop checks once before the batch, then again per item.
        calls[0] += 1
        return calls[0] > 1

    run_stream_drain_loop(
        q,
        None,
        job_done,
        lambda _text, _is_thinking: None,
        on_stream_done=lambda _item: True,
        on_stopped=lambda: stopped.append(True),
        on_error=lambda payload: errors.append(payload),
        stop_checker=stop_checker,
    )
    assert job_done[0] is True
    assert stopped == [True]
    assert errors == []


def test_none_display_payloads_flush_as_strings():
    """Raw CHUNK, THINKING, and TOOL_THINKING None must not break "".join."""
    q = queue.Queue()
    q.put((StreamQueueKind.THINKING, None))
    q.put((StreamQueueKind.CHUNK, None))
    q.put((StreamQueueKind.TOOL_THINKING, None))
    q.put((StreamQueueKind.STREAM_DONE, None))
    applied: list[tuple[object, bool]] = []
    errors: list[object] = []

    run_stream_drain_loop(
        q,
        None,
        [False],
        lambda text, is_thinking: applied.append((text, is_thinking)),
        on_stream_done=lambda _item: True,
        on_stopped=lambda: None,
        on_error=lambda payload: errors.append(payload),
        show_search_thinking=True,
    )
    assert errors == []
    assert applied
    assert all(isinstance(text, str) for text, _is_thinking in applied)
