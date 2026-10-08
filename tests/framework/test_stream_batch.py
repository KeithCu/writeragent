import queue
import threading

from plugin.framework.async_stream import StreamQueueKind
from plugin.framework.stream_batch import BatchingStreamQueue


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


def test_discard_then_control_put_leaves_queue_empty():
    """discard() wins the lock before a later control put, so the item is dropped."""
    raw = queue.Queue()
    bq = BatchingStreamQueue(raw, batch_interval=10.0)
    bq.put((StreamQueueKind.CHUNK, "dropped"))
    bq.discard()
    bq.put((StreamQueueKind.STREAM_DONE, None))
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
