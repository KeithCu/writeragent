# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for async_stream edge cases, parameterization, and callback fallbacks."""

import queue
from unittest.mock import MagicMock, patch

import pytest

from plugin.framework.stream_batch import StreamQueueKind
from plugin.framework.async_stream import DrainOutcome, run_stream_drain_loop
from plugin.framework.send_cancellation import SendCancellation
from tests.framework.stream_test_helpers import DummyToolkit, _drain


def test_run_async_worker_with_drain_stop_fallback_calls_one_arg_on_done() -> None:
    """When on_stopped_fn is None, STOPPED invokes a one-arg on_done with None."""
    from plugin.framework.async_stream import run_async_worker_with_drain

    done_calls: list[object] = []
    errors: list[object] = []

    def on_done(item: object) -> None:
        done_calls.append(item)

    def worker(worker_q) -> None:
        worker_q.put((StreamQueueKind.STOPPED, None))

    with patch("plugin.framework.uno_context.get_toolkit", return_value=DummyToolkit()):
        run_async_worker_with_drain(
            MagicMock(),
            worker,
            lambda _text, _is_thinking: None,
            on_done,
            errors.append,
            on_stopped_fn=None,
        )

    assert done_calls == [None]
    assert errors == []


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
    outcome, _applied, errors, _stopped, done = _drain([empty, (StreamQueueKind.STREAM_DONE, "later")])
    assert outcome.done is True
    assert done == []
    assert len(errors) == 1
    assert "NoneType" in str(errors[0])


def test_stop_tail_empty_item_does_not_call_on_error():
    """Stop applies the already-pulled tail. An empty item there is dropped."""
    calls = [0]
    cancel = SendCancellation()

    def is_cancelled() -> bool:
        # The blocking loop checks once before the batch, then again per item.
        calls[0] += 1
        return calls[0] > 1

    cancel.is_cancelled = is_cancelled  # type: ignore

    outcome, _applied, errors, stopped, _done = _drain([()], cancel=cancel)
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
