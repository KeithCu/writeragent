# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for async_stream edge cases, parameterization, and callback fallbacks."""

import inspect
import queue

import pytest

from plugin.framework.stream_batch import StreamQueueKind
from plugin.framework.async_stream import DrainOutcome, run_stream_drain_loop
from plugin.framework.send_cancellation import SendCancellation
from tests.framework.stream_test_helpers import DummyToolkit


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
    outcome = DrainOutcome()
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
        outcome,
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
    outcome = DrainOutcome()

    def on_error(payload: object) -> bool:
        errors.append(payload)
        q.put((StreamQueueKind.CHUNK, "replacement"))
        q.put((StreamQueueKind.STREAM_DONE, "ok"))
        return True

    run_stream_drain_loop(
        q,
        None,
        outcome,
        lambda text, is_thinking: applied.append((text, is_thinking)),
        on_stream_done=lambda _item: True,
        on_stopped=lambda: None,
        on_error=on_error,
    )
    assert outcome.done is True
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
    outcome = DrainOutcome()

    run_stream_drain_loop(
        q,
        None,
        outcome,
        lambda _text, _is_thinking: None,
        on_stream_done=lambda item: done.append(item) or True,
        on_stopped=lambda: None,
        on_error=lambda payload: errors.append(payload),
    )
    assert outcome.done is True
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
    outcome = DrainOutcome()
    cancel = SendCancellation()

    def is_cancelled() -> bool:
        # The blocking loop checks once before the batch, then again per item.
        calls[0] += 1
        return calls[0] > 1

    cancel.is_cancelled = is_cancelled  # type: ignore

    run_stream_drain_loop(
        q,
        None,
        outcome,
        lambda _text, _is_thinking: None,
        on_stream_done=lambda _item: True,
        on_stopped=lambda: stopped.append(True),
        on_error=lambda payload: errors.append(payload),
        cancel=cancel,
    )
    assert outcome.done is True
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
        DrainOutcome(),
        lambda text, is_thinking: applied.append((text, is_thinking)),
        on_stream_done=lambda _item: True,
        on_stopped=lambda: None,
        on_error=lambda payload: errors.append(payload),
        show_search_thinking=True,
    )
    assert errors == []
    assert applied
    assert all(isinstance(text, str) for text, _is_thinking in applied)
