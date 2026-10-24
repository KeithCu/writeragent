from __future__ import annotations

import queue as queue_mod
import threading
import time
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _reset_secondary_idle_in_flight():
    import plugin.framework.vcl_pumping as vp
    from plugin.framework.vcl_pumping import _secondary_idle_lock

    with _secondary_idle_lock:
        vp._secondary_idle_in_flight = False
    yield
    with _secondary_idle_lock:
        vp._secondary_idle_in_flight = False


def _park_default_work_queue() -> list[object]:
    """Take every item off the process-wide marshal queue."""
    from plugin.framework.queue_executor import default_executor

    saved: list[object] = []
    while True:
        try:
            saved.append(default_executor._work_queue.get_nowait())
        except queue_mod.Empty:
            return saved


def _restore_default_work_queue(saved: list[object]) -> None:
    """Drop anything this test queued, then put *saved* back in order."""
    from plugin.framework.queue_executor import default_executor

    _park_default_work_queue()
    for item in saved:
        default_executor._work_queue.put(item)


def test_focus_preserved_restores_focus_window():
    from plugin.framework.vcl_pumping import focus_preserved

    focus_window = MagicMock()
    toolkit = MagicMock()
    toolkit.getFocusWindow.return_value = focus_window

    with patch("plugin.framework.uno_context.get_toolkit", return_value=toolkit):
        with focus_preserved(MagicMock()):
            pass

    focus_window.setFocus.assert_called_once()


def test_focus_preserved_prefers_explicit_query_over_toolkit():
    from plugin.framework.vcl_pumping import focus_preserved

    query = MagicMock()
    send_btn = MagicMock()
    toolkit = MagicMock()
    toolkit.getFocusWindow.return_value = send_btn
    with patch("plugin.framework.uno_context.get_toolkit", return_value=toolkit):
        with focus_preserved(MagicMock(), query):
            pass

    query.setFocus.assert_called_once()
    send_btn.setFocus.assert_not_called()


def test_focus_preserved_with_restore_focus():
    from plugin.framework.vcl_pumping import focus_preserved

    callback = MagicMock()
    with focus_preserved(MagicMock(), restore_focus=callback):
        pass
    callback.assert_called_once()


def test_focus_preserved_with_restore_object():
    from plugin.framework.vcl_pumping import focus_preserved

    field = MagicMock()
    with focus_preserved(MagicMock(), restore=field):
        pass
    field.setFocus.assert_called_once()


def test_focus_preserved_swallows_restore_exceptions():
    from plugin.framework.vcl_pumping import focus_preserved

    failing_callback = MagicMock(side_effect=RuntimeError("Focus restore failed"))
    with focus_preserved(MagicMock(), restore_focus=failing_callback):
        pass  # must not raise
    failing_callback.assert_called_once()


def test_pump_vcl_events():
    from plugin.framework.vcl_pumping import pump_vcl_events

    assert pump_vcl_events(None) is False

    no_pe2i = object()
    assert pump_vcl_events(no_pe2i) is False

    toolkit = MagicMock()
    assert pump_vcl_events(toolkit) is True
    toolkit.processEventsToIdle.assert_called_once()

    failing_toolkit = MagicMock()
    failing_toolkit.processEventsToIdle.side_effect = RuntimeError("VCL error")
    assert pump_vcl_events(failing_toolkit) is False


def test_process_events_to_idle_calls_toolkit():
    from plugin.framework.async_drain_guard import reset_suppressed_vcl_pump_count
    from plugin.framework.vcl_pumping import process_events_to_idle

    reset_suppressed_vcl_pump_count()
    toolkit = MagicMock()
    with patch("plugin.framework.uno_context.get_toolkit", return_value=toolkit):
        assert process_events_to_idle(MagicMock(), rounds=3) is True

    assert toolkit.processEventsToIdle.call_count == 3


def test_process_events_to_idle_suppressed_under_drain_owner():
    from plugin.framework.async_drain_guard import (
        drain_owner_scope,
        get_suppressed_vcl_pump_count,
        reset_suppressed_vcl_pump_count,
    )
    from plugin.framework.vcl_pumping import process_events_to_idle

    reset_suppressed_vcl_pump_count()
    toolkit = MagicMock()
    with patch("plugin.framework.uno_context.get_toolkit", return_value=toolkit):
        with drain_owner_scope("stream"):
            assert process_events_to_idle(MagicMock(), rounds=2) is False

    assert toolkit.processEventsToIdle.call_count == 0
    assert get_suppressed_vcl_pump_count() >= 1


def test_process_events_to_idle_force_under_drain_owner():
    from plugin.framework.async_drain_guard import drain_owner_scope, reset_suppressed_vcl_pump_count
    from plugin.framework.vcl_pumping import process_events_to_idle

    reset_suppressed_vcl_pump_count()
    toolkit = MagicMock()
    with patch("plugin.framework.uno_context.get_toolkit", return_value=toolkit):
        with drain_owner_scope("stream"):
            assert process_events_to_idle(MagicMock(), rounds=2, force=True) is True

    assert toolkit.processEventsToIdle.call_count == 2


def test_wait_while_pumping_returns_true_when_event_set():
    from plugin.framework.vcl_pumping import wait_while_pumping

    done = threading.Event()
    pumps: list[bool] = []

    def _pe2i(_ctx: object, rounds: int = 1, force: bool = False) -> bool:
        del rounds
        pumps.append(force)
        done.set()
        return True

    with patch("plugin.framework.vcl_pumping.process_events_to_idle", side_effect=_pe2i):
        assert wait_while_pumping(done, MagicMock(), timeout=1.0) is True
    assert pumps
    assert all(force is False for force in pumps)


def test_wait_while_pumping_timeout_returns_false():
    from plugin.framework.vcl_pumping import wait_while_pumping

    done = threading.Event()
    with patch("plugin.framework.vcl_pumping.process_events_to_idle", return_value=False):
        assert wait_while_pumping(done, MagicMock(), timeout=0.05, poll_sec=0.01) is False
    assert not done.is_set()


def test_wait_while_pumping_swallows_pe2i_errors():
    from plugin.framework.vcl_pumping import wait_while_pumping

    done = threading.Event()
    n = {"i": 0}

    def _pe2i(_ctx: object, rounds: int = 1, force: bool = False) -> bool:
        del rounds, force
        n["i"] += 1
        if n["i"] == 1:
            raise RuntimeError("no toolkit")
        done.set()
        return True

    with patch("plugin.framework.vcl_pumping.process_events_to_idle", side_effect=_pe2i):
        assert wait_while_pumping(done, MagicMock(), timeout=1.0) is True


def test_wait_while_pumping_under_drain_owner_still_waits():
    from plugin.framework.async_drain_guard import drain_owner_scope, reset_suppressed_vcl_pump_count
    from plugin.framework.vcl_pumping import wait_while_pumping

    reset_suppressed_vcl_pump_count()
    done = threading.Event()
    toolkit = MagicMock()

    def _set_done() -> None:
        time.sleep(0.02)
        done.set()

    worker = threading.Thread(target=_set_done)
    with patch("plugin.framework.uno_context.get_toolkit", return_value=toolkit):
        with drain_owner_scope("stream"):
            worker.start()
            assert wait_while_pumping(done, MagicMock(), timeout=1.0, poll_sec=0.01) is True
            worker.join()
    toolkit.processEventsToIdle.assert_not_called()


def test_wait_while_pumping_off_main_posts_instead_of_pe2i():
    """Writer doProofreading is Dummy-*; PE2I on that stack is a thread violation.

    The marshal queue is process-wide. A leftover item must not skip the post:
    that left the event unset (and harper_try_lint with posts=0 under xdist).
    """
    from plugin.framework.queue_executor import default_executor
    from plugin.framework.vcl_pumping import wait_while_pumping

    saved = _park_default_work_queue()
    try:
        default_executor._work_queue.put(object())
        assert default_executor.pending_work_count() >= 1
        done = threading.Event()
        posts: list[object] = []
        pe2i_threads: list[str] = []
        result: dict[str, bool] = {}

        def _pe2i(_ctx: object, rounds: int = 1, force: bool = False) -> bool:
            del rounds, force
            pe2i_threads.append(threading.current_thread().name)
            return True

        def _post(fn: object, *args: object, **kwargs: object) -> None:
            del args, kwargs
            posts.append(fn)
            done.set()

        def _waiter() -> None:
            with (
                patch("plugin.framework.vcl_pumping.process_events_to_idle", side_effect=_pe2i),
                patch("plugin.framework.queue_executor.post_to_main_thread", side_effect=_post),
            ):
                result["ok"] = wait_while_pumping(done, MagicMock(), timeout=1.0, poll_sec=0.01)

        worker = threading.Thread(target=_waiter, name="Dummy-21")
        worker.start()
        worker.join(timeout=2.0)
        assert result.get("ok") is True
        assert posts
        assert pe2i_threads == []
    finally:
        _restore_default_work_queue(saved)


def test_wait_while_pumping_off_main_post_fallback_skips_pe2i():
    """QueueExecutor.post can run the callback on the waiter; still no PE2I off-main.

    A leftover marshal item must not skip that post. The inlined pump still
    must not call PE2I on Dummy-*.
    """
    from plugin.framework.queue_executor import default_executor
    from plugin.framework.vcl_pumping import wait_while_pumping

    saved = _park_default_work_queue()
    try:
        default_executor._work_queue.put(object())
        assert default_executor.pending_work_count() >= 1
        done = threading.Event()
        pe2i_threads: list[str] = []
        result: dict[str, bool] = {}

        def _pe2i(_ctx: object, rounds: int = 1, force: bool = False) -> bool:
            del rounds, force
            pe2i_threads.append(threading.current_thread().name)
            return True

        def _post(fn: object, *args: object, **kwargs: object) -> None:
            del args, kwargs
            assert callable(fn)
            fn()
            done.set()

        def _waiter() -> None:
            with (
                patch("plugin.framework.vcl_pumping.process_events_to_idle", side_effect=_pe2i),
                patch("plugin.framework.queue_executor.post_to_main_thread", side_effect=_post),
            ):
                result["ok"] = wait_while_pumping(done, MagicMock(), timeout=1.0, poll_sec=0.01)

        worker = threading.Thread(target=_waiter, name="Dummy-21")
        worker.start()
        worker.join(timeout=2.0)
        assert result.get("ok") is True
        assert pe2i_threads == []
    finally:
        _restore_default_work_queue(saved)


def test_post_secondary_idle_coalesces_and_passes_none_scope():
    """Two off-main posts coalesce to one post with bound_scope=None; PE2I not called on Dummy-*."""
    from plugin.framework.queue_executor import default_executor
    from plugin.framework.vcl_pumping import _post_secondary_idle

    saved = _park_default_work_queue()
    try:
        default_executor._work_queue.put(object())
        assert default_executor.pending_work_count() >= 1

        posts: list[tuple[object, dict[str, object]]] = []
        pe2i_called: list[str] = []

        def _post(fn: object, *args: object, **kwargs: object) -> None:
            del args
            posts.append((fn, kwargs))

        def _pe2i(_ctx: object, force: bool = False) -> bool:
            del force
            pe2i_called.append(threading.current_thread().name)
            return True

        def _worker() -> None:
            with (
                patch("plugin.framework.vcl_pumping.process_events_to_idle", side_effect=_pe2i),
                patch("plugin.framework.queue_executor.post_to_main_thread", side_effect=_post),
            ):
                _post_secondary_idle(MagicMock())
                _post_secondary_idle(MagicMock())

        t = threading.Thread(target=_worker, name="Dummy-21")
        t.start()
        t.join(timeout=2.0)
        assert not t.is_alive()
        assert len(posts) == 1
        _fn, kwargs = posts[0]
        assert kwargs.get("bound_scope") is None
        assert pe2i_called == []
    finally:
        _restore_default_work_queue(saved)


def test_post_secondary_idle_runs_pump_and_allows_next_post():
    """After the posted callable runs, the next call posts again."""
    from plugin.framework.vcl_pumping import _post_secondary_idle

    posts: list[object] = []

    def _post(fn: object, *args: object, **kwargs: object) -> None:
        del args, kwargs
        posts.append(fn)

    def _worker() -> None:
        with patch("plugin.framework.queue_executor.post_to_main_thread", side_effect=_post):
            _post_secondary_idle(MagicMock())
            assert len(posts) == 1
            # Second call while in-flight does not post
            _post_secondary_idle(MagicMock())
            assert len(posts) == 1

            # Run the posted callable (simulating execution on main thread)
            callable_fn = posts[0]
            assert callable(callable_fn)
            callable_fn()

            # Now the next call posts again
            _post_secondary_idle(MagicMock())
            assert len(posts) == 2

    t = threading.Thread(target=_worker, name="Dummy-21")
    t.start()
    t.join(timeout=2.0)
    assert not t.is_alive()


def test_post_secondary_idle_unrun_callable_leaves_flag_set():
    """A mock returning without calling fn does not allow a second post."""
    from plugin.framework.vcl_pumping import _post_secondary_idle

    posts: list[object] = []

    def _post(fn: object, *args: object, **kwargs: object) -> None:
        del args, kwargs
        posts.append(fn)

    def _worker() -> None:
        with patch("plugin.framework.queue_executor.post_to_main_thread", side_effect=_post):
            _post_secondary_idle(MagicMock())
            _post_secondary_idle(MagicMock())
            _post_secondary_idle(MagicMock())

    t = threading.Thread(target=_worker, name="Dummy-21")
    t.start()
    t.join(timeout=2.0)
    assert not t.is_alive()
    assert len(posts) == 1


def test_post_secondary_idle_post_raises_clears_flag():
    """When post_to_main_thread raises, the flag is cleared and the next call posts."""
    from plugin.framework.vcl_pumping import _post_secondary_idle, wait_while_pumping

    call_count = 0
    posts: list[object] = []

    def _post(fn: object, *args: object, **kwargs: object) -> None:
        del args, kwargs
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("queue failed")
        posts.append(fn)

    def _worker() -> None:
        with patch("plugin.framework.queue_executor.post_to_main_thread", side_effect=_post):
            with pytest.raises(RuntimeError, match="queue failed"):
                _post_secondary_idle(MagicMock())

            # Flag was cleared by except block, so next post proceeds
            _post_secondary_idle(MagicMock())
            assert len(posts) == 1
            callable_fn = posts[0]
            assert callable(callable_fn)
            callable_fn()

    t = threading.Thread(target=_worker, name="Dummy-21")
    t.start()
    t.join(timeout=2.0)
    assert not t.is_alive()

    # Also verify exception propagates into wait_while_pumping and is swallowed
    done = threading.Event()
    call_count = 0

    def _post_then_set(fn: object, *args: object, **kwargs: object) -> None:
        del args, kwargs
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("queue failed")
        done.set()

    def _waiter() -> None:
        with patch("plugin.framework.queue_executor.post_to_main_thread", side_effect=_post_then_set):
            assert wait_while_pumping(done, MagicMock(), timeout=1.0, poll_sec=0.01) is True

    t2 = threading.Thread(target=_waiter, name="Dummy-22")
    t2.start()
    t2.join(timeout=2.0)
    assert not t2.is_alive()
    assert done.is_set()


def test_post_secondary_idle_pump_raises_clears_flag():
    """When _pump raises inside process_events_to_idle, the flag is cleared in finally."""
    from plugin.framework.vcl_pumping import _post_secondary_idle

    posts: list[object] = []

    def _post(fn: object, *args: object, **kwargs: object) -> None:
        del args, kwargs
        posts.append(fn)

    def _worker() -> None:
        with (
            patch("plugin.framework.vcl_pumping.on_main_thread", return_value=True),
            patch("plugin.framework.vcl_pumping.process_events_to_idle", side_effect=RuntimeError("PE2I error")),
            patch("plugin.framework.queue_executor.post_to_main_thread", side_effect=_post),
        ):
            _post_secondary_idle(MagicMock())
            assert len(posts) == 1
            pump_fn = posts[0]
            assert callable(pump_fn)
            with pytest.raises(RuntimeError, match="PE2I error"):
                pump_fn()

            # Flag was cleared in finally, next call posts
            _post_secondary_idle(MagicMock())
            assert len(posts) == 2

    t = threading.Thread(target=_worker, name="Dummy-21")
    t.start()
    t.join(timeout=2.0)
    assert not t.is_alive()
