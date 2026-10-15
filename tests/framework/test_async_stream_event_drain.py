# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for async_stream event drain, re-arm schedulers, and idle thread."""

import gc
import queue
import time
import weakref
from unittest.mock import MagicMock

import pytest

from plugin.framework.stream_batch import StreamQueueKind
from plugin.framework.async_stream import run_stream_drain_loop
from tests.framework.stream_test_helpers import (
    DummyToolkit,
    _RecordingRearm,
    _clear_event_drain,
    _start_event_drain,
)


def test_nested_drain_closes_rearm_scheduler():
    """A refused start closes a re-arm that _finish will never see."""
    from plugin.framework.queue_executor import drain_owner_scope

    class _ClosableRearm:
        def __init__(self) -> None:
            self.closed = 0

        def post(self, fn: object) -> None:
            del fn

        def post_after(self, delay: float, fn: object) -> None:
            del delay, fn

        def close(self) -> None:
            self.closed += 1

    class _BareRearm:
        def post(self, fn: object) -> None:
            del fn

        def post_after(self, delay: float, fn: object) -> None:
            del delay, fn

    _clear_event_drain()
    closable = _ClosableRearm()
    bare = _BareRearm()
    try:
        with drain_owner_scope("mcp"):
            errors: list[object] = []
            job_done = [False]
            run_stream_drain_loop(
                queue.Queue(),
                None,
                job_done,
                lambda _text, _is_thinking: None,
                on_stream_done=lambda _item: True,
                on_stopped=lambda: None,
                on_error=lambda payload: errors.append(payload),
                rearm=closable,
            )
            assert job_done[0] is True
            assert closable.closed == 1
            assert len(errors) == 1

            bare_errors: list[object] = []
            bare_done = [False]
            run_stream_drain_loop(
                queue.Queue(),
                None,
                bare_done,
                lambda _text, _is_thinking: None,
                on_stream_done=lambda _item: True,
                on_stopped=lambda: None,
                on_error=lambda payload: bare_errors.append(payload),
                rearm=bare,
            )
            assert bare_done[0] is True
            assert len(bare_errors) == 1
    finally:
        _clear_event_drain()


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


def test_async_callback_rearm_runs_both_queued_slices(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two slices queued before either notify both run. A fire after close does not."""
    from plugin.framework import async_stream

    class FakeSliceCallback:
        def __init__(self, fn):
            self.fn = fn

        def notify(self, _aData=None):
            self.fn()

    monkeypatch.setattr(async_stream, "_new_xcallback", FakeSliceCallback)

    class FakeService:
        def __init__(self) -> None:
            self.calls: list[FakeSliceCallback] = []

        def addCallback(self, cb, _data):
            self.calls.append(cb)

    service = FakeService()
    rearm = async_stream._AsyncCallbackRearm(service)
    ran: list[str] = []
    try:
        rearm.post(lambda: ran.append("a"))
        rearm.post(lambda: ran.append("b"))
        for _step in range(8):
            if not service.calls:
                break
            service.calls.pop(0).notify()
        else:
            raise AssertionError("rearm kept scheduling")
        assert ran == ["a", "b"]
        rearm.close()
        rearm._enqueue(lambda: ran.append("late"))
        assert ran == ["a", "b"]
        assert service.calls == []
    finally:
        rearm.close()


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

    def spy_blocking(state, toolkit_arg, stop_checker, flush_pending, *args, **kwargs):
        blocking_calls.append(toolkit_arg)
        return real_blocking(state, toolkit_arg, stop_checker, flush_pending, *args, **kwargs)

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


def test_on_drain_started_runs_only_after_first_slice_is_armed() -> None:
    rearm = _RecordingRearm()
    q: queue.Queue = queue.Queue()
    started = []
    job_done = [False]
    applied: list[str] = []
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
            on_drain_started=lambda: started.append(1),
        )
        assert started == [1]
        assert len(rearm.pending) == 1
        assert rearm.pending[0][0] == "now"
        q.put((StreamQueueKind.STREAM_DONE, None))
        rearm.pump()
        assert job_done[0] is True
    finally:
        _clear_event_drain()


def test_failed_arm_does_not_call_on_drain_started() -> None:
    class _FailingPostRearm:
        def __init__(self) -> None:
            self.closed = 0

        def post(self, fn: object) -> None:
            raise RuntimeError("arm boom")

        def post_after(self, delay: float, fn: object) -> None:
            del delay, fn

        def close(self) -> None:
            self.closed += 1

    rearm = _FailingPostRearm()
    q: queue.Queue = queue.Queue()
    started = []
    job_done = [False]
    errors: list[object] = []
    try:
        run_stream_drain_loop(
            q,
            None,
            job_done,
            lambda _text, _thinking: None,
            on_stream_done=lambda _item: True,
            on_stopped=lambda: None,
            on_error=lambda payload: errors.append(payload),
            rearm=rearm,
            on_drain_started=lambda: started.append(1),
        )
        assert started == []
        assert job_done[0] is True
        assert len(errors) == 1
        assert rearm.closed == 1
    finally:
        _clear_event_drain()


def test_idle_rearm_gives_up_after_max_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    from plugin.framework import async_stream

    monkeypatch.setattr(async_stream, "_IDLE_REARM_MAX_FAILURES", 3)
    monkeypatch.setattr(async_stream, "_DRAIN_IDLE_REARM_SEC", 0.01)
    monkeypatch.setattr(async_stream, "_IDLE_REARM_RETRY_MAX_SEC", 0.01)

    thread = async_stream._IdleRearmThread()
    calls = []
    give_up_event = __import__("threading").Event()

    def failing_fire() -> None:
        calls.append(time.monotonic())
        raise RuntimeError("always boom")

    try:
        thread.arm(0.01, failing_fire, on_give_up=give_up_event.set)
        assert give_up_event.wait(timeout=2.0)
        assert len(calls) == 3
        time.sleep(0.1)
        assert len(calls) == 3
    finally:
        thread.stop()


def test_finish_runs_epilogues_when_idle_callback_raises_recursion_error() -> None:
    from plugin.framework import async_stream
    from plugin.framework.async_drain_guard import (
        add_drain_idle_callback,
        remove_drain_idle_callback,
        get_drain_owner,
        reset_sentry_state,
    )

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
            job_done=job_done,
        ),
        scheduler,
        None,
        None,
    )

    def failing_cb() -> None:
        raise RecursionError("failing idle cb")

    add_drain_idle_callback(failing_cb)
    epilogues_ran: list[int] = []

    try:
        drain.start()
        drain.epilogues.append(lambda: epilogues_ran.append(1))
        drain._finish()

        assert epilogues_ran == [1]
        assert get_drain_owner() is None
        assert drain.closed is True
    finally:
        remove_drain_idle_callback(failing_cb)
        _clear_event_drain()
        reset_sentry_state()

