from __future__ import annotations

import threading

import pytest
from plugin.framework.async_drain_guard import (
    NestedDrainOwnerError,
    acquire_drain_owner,
    add_drain_idle_callback,
    drain_owner_scope,
    get_drain_depth,
    get_drain_owner,
    get_suppressed_vcl_pump_count,
    is_vcl_pump_allowed,
    note_suppressed_vcl_pump,
    release_drain_owner,
    reset_sentry_state,
    reset_suppressed_vcl_pump_count,
)
import plugin.framework.async_drain_guard as guard
import plugin.framework.queue_executor as qe


@pytest.fixture(autouse=True)
def _clean_sentry_state():
    reset_sentry_state()
    yield
    reset_sentry_state()


def test_async_drain_guard_single_owner():
    assert get_drain_owner() is None
    assert get_drain_depth() == 0
    assert is_vcl_pump_allowed() is True

    with drain_owner_scope("chat_stream"):
        assert get_drain_owner() == "chat_stream"
        assert get_drain_depth() == 1
        assert is_vcl_pump_allowed() is True

    assert get_drain_owner() is None
    assert get_drain_depth() == 0


def test_async_drain_guard_prevents_nested_different_owners():
    with drain_owner_scope("chat_stream"):
        with pytest.raises(NestedDrainOwnerError, match="Nested UI drain attempted by 'mcp_stream'"):
            with drain_owner_scope("mcp_stream"):
                pass


def test_async_drain_guard_reentrant_same_owner_updates_depth():
    with drain_owner_scope("chat_stream"):
        assert get_drain_depth() == 1
        assert is_vcl_pump_allowed() is True
        with drain_owner_scope("chat_stream"):
            assert get_drain_depth() == 2
            assert is_vcl_pump_allowed() is False

        assert get_drain_depth() == 1
        assert is_vcl_pump_allowed() is True


def test_acquire_release_drain_owner_overlap_and_depth():
    acquire_drain_owner("stream")
    acquire_drain_owner("stream")
    assert get_drain_owner() == "stream"
    assert get_drain_depth() == 2

    release_drain_owner()
    assert get_drain_owner() == "stream"
    assert get_drain_depth() == 1

    with pytest.raises(NestedDrainOwnerError, match="Nested UI drain attempted by 'mcp'"):
        acquire_drain_owner("mcp")

    release_drain_owner()
    assert get_drain_owner() is None
    assert get_drain_depth() == 0


def test_async_drain_guard_suppressed_vcl_counter():
    assert get_suppressed_vcl_pump_count() == 0
    note_suppressed_vcl_pump("chat_stream")
    note_suppressed_vcl_pump()
    assert get_suppressed_vcl_pump_count() == 2

    reset_suppressed_vcl_pump_count()
    assert get_suppressed_vcl_pump_count() == 0


@pytest.fixture
def _isolated_idle_callbacks():
    """Peer messaging registers a process-wide idle callback at import.

    ``reset_sentry_state`` must not drop it. These tests swap the list and put it back.
    """
    with guard._drain_lock:
        saved = list(guard._drain_idle_callbacks)
        guard._drain_idle_callbacks.clear()
    try:
        yield
    finally:
        with guard._drain_lock:
            guard._drain_idle_callbacks[:] = saved


def test_add_drain_idle_callback_is_idempotent(_isolated_idle_callbacks):
    ran: list[int] = []

    def cb() -> None:
        ran.append(1)

    add_drain_idle_callback(cb)
    add_drain_idle_callback(cb)
    with drain_owner_scope("chat_stream"):
        pass
    assert ran == [1]


def test_concurrent_add_drain_idle_callback_registers_once(_isolated_idle_callbacks):
    def cb() -> None:
        return None

    barrier = threading.Barrier(8)

    def worker() -> None:
        barrier.wait()
        add_drain_idle_callback(cb)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    with guard._drain_lock:
        assert guard._drain_idle_callbacks.count(cb) == 1


def test_callback_added_during_notify_runs_on_next_idle(_isolated_idle_callbacks):
    ran: list[str] = []

    def second() -> None:
        ran.append("second")

    def first() -> None:
        ran.append("first")
        # Must not deadlock: notify copies under ``_drain_lock`` then releases
        # before calling, and ``get_drain_owner`` takes that lock.
        assert get_drain_owner() is None
        add_drain_idle_callback(second)

    add_drain_idle_callback(first)
    with drain_owner_scope("chat_stream"):
        pass
    assert ran == ["first"]
    with drain_owner_scope("chat_stream"):
        pass
    assert ran == ["first", "first", "second"]


def test_queue_executor_reexports_async_drain_guard():
    assert qe.NestedDrainOwnerError is NestedDrainOwnerError
    assert qe.drain_owner_scope is drain_owner_scope
    assert qe.get_drain_owner is get_drain_owner
    assert qe.get_suppressed_vcl_pump_count is get_suppressed_vcl_pump_count
    assert qe.reset_suppressed_vcl_pump_count is reset_suppressed_vcl_pump_count
    assert qe.note_suppressed_vcl_pump is note_suppressed_vcl_pump


def test_release_underflow_logs_and_skips_idle_callbacks(_isolated_idle_callbacks, caplog):
    ran: list[int] = []

    def cb() -> None:
        ran.append(1)

    add_drain_idle_callback(cb)
    assert get_drain_depth() == 0
    with caplog.at_level("WARNING"):
        release_drain_owner()
    assert ran == []
    assert get_drain_depth() == 0
    assert get_drain_owner() is None
    assert any("underflow" in r.message for r in caplog.records)


def test_idle_callback_recursion_error_does_not_escape(_isolated_idle_callbacks):
    ran: list[str] = []

    def bad_cb() -> None:
        ran.append("bad")
        raise RecursionError("maximum recursion depth exceeded")

    def good_cb() -> None:
        ran.append("good")

    add_drain_idle_callback(bad_cb)
    add_drain_idle_callback(good_cb)

    acquire_drain_owner("stream")
    release_drain_owner()

    assert ran == ["bad", "good"]
    assert get_drain_depth() == 0
    assert get_drain_owner() is None


def test_nested_idle_notify_reruns_registered_callback(_isolated_idle_callbacks, caplog):
    from plugin.framework.async_drain_guard import remove_drain_idle_callback

    ran: list[str] = []

    def second() -> None:
        ran.append("second")

    def first() -> None:
        ran.append("first")
        remove_drain_idle_callback(first)
        acquire_drain_owner("stream")
        add_drain_idle_callback(second)
        release_drain_owner()

    add_drain_idle_callback(first)
    with caplog.at_level("WARNING"):
        acquire_drain_owner("stream")
        release_drain_owner()

    assert ran == ["first", "second"]
    assert get_drain_depth() == 0
    assert get_drain_owner() is None
    assert not any("stopped after" in r.message for r in caplog.records)


def test_nested_idle_notify_stops_after_eight_passes(_isolated_idle_callbacks, caplog):
    ran: list[int] = []

    def looper() -> None:
        ran.append(len(ran) + 1)
        acquire_drain_owner("stream")
        release_drain_owner()

    add_drain_idle_callback(looper)
    with caplog.at_level("WARNING"):
        acquire_drain_owner("stream")
        release_drain_owner()

    assert len(ran) == 8
    assert get_drain_depth() == 0
    assert get_drain_owner() is None
    assert any("stopped after 8 passes" in r.message for r in caplog.records)


def test_same_name_other_thread_acquire_raises():
    acquire_drain_owner("stream")
    caller_ident = threading.get_ident()
    other_ident: list[int] = []
    errors: list[Exception] = []

    def bg():
        other_ident.append(threading.get_ident())
        try:
            acquire_drain_owner("stream")
        except Exception as exc:
            errors.append(exc)

    t = threading.Thread(target=bg)
    t.start()
    t.join()

    assert len(errors) == 1
    assert isinstance(errors[0], NestedDrainOwnerError)
    err_msg = str(errors[0])
    assert f"thread {other_ident[0]}" in err_msg
    assert f"thread {caller_ident}" in err_msg
    assert "[SENTRY VIOLATION] Drain owner 'stream' re-entered" in err_msg
    assert get_drain_depth() == 1
    assert get_drain_owner() == "stream"

    release_drain_owner()
    assert get_drain_depth() == 0
    assert get_drain_owner() is None


def test_other_thread_release_does_not_drop_owner(_isolated_idle_callbacks, caplog):
    ran: list[int] = []

    def cb() -> None:
        ran.append(1)

    add_drain_idle_callback(cb)
    acquire_drain_owner("stream")
    assert get_drain_depth() == 1

    with caplog.at_level("WARNING"):
        t = threading.Thread(target=release_drain_owner)
        t.start()
        t.join()

    assert any("release_drain_owner ignored: caller thread" in r.message for r in caplog.records)
    assert ran == []
    assert get_drain_depth() == 1
    assert get_drain_owner() == "stream"

    release_drain_owner()
    assert ran == [1]
    assert get_drain_depth() == 0
    assert get_drain_owner() is None
