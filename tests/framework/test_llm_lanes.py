# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for LLM request lane and concurrency gates."""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from plugin.framework.errors import BlockingWaitStopped
import plugin.framework.llm_lanes as lc


def test_llm_request_lane_serializes_callers() -> None:
    order: list[str] = []

    def first() -> None:
        with lc.llm_request_lane():
            order.append('first-enter')
            time.sleep(0.08)
            order.append('first-exit')

    def second() -> None:
        time.sleep(0.01)
        with lc.llm_request_lane():
            order.append('second-enter')

    t1 = threading.Thread(target=first)
    t2 = threading.Thread(target=second)
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    assert (order == ['first-enter', 'first-exit', 'second-enter'])


@pytest.fixture(autouse=True)
def _reset_grammar_inflight_count() -> None:
    with lc._GRAMMAR_INFLIGHT_LOCK:
        lc._GRAMMAR_INFLIGHT_COUNT = 0
    yield
    with lc._GRAMMAR_INFLIGHT_LOCK:
        lc._GRAMMAR_INFLIGHT_COUNT = 0


def test_grammar_llm_request_gate_limit_1_uses_global_lane() -> None:
    with patch.object(lc, "llm_request_lane") as lane:
        lane.return_value.__enter__ = MagicMock()
        lane.return_value.__exit__ = MagicMock(return_value=False)
        with lc.grammar_llm_request_gate(1):
            pass
        lane.assert_called_once()


def test_grammar_llm_request_gate_release_admits_one_waiter() -> None:
    """One release opens one slot. The other waiter stays blocked until that holder leaves."""
    first_in = threading.Event()
    release_first = threading.Event()
    second_in = threading.Event()
    third_in = threading.Event()
    release_rest = threading.Event()

    def holder() -> None:
        with lc.grammar_llm_request_gate(1):
            first_in.set()
            release_first.wait(timeout=2.0)

    def waiter(flag: threading.Event) -> None:
        with lc.grammar_llm_request_gate(1):
            flag.set()
            release_rest.wait(timeout=2.0)

    threads = (
        threading.Thread(target=holder),
        threading.Thread(target=lambda: waiter(second_in)),
        threading.Thread(target=lambda: waiter(third_in)),
    )
    threads[0].start()
    assert first_in.wait(timeout=2.0)
    threads[1].start()
    threads[2].start()
    time.sleep(0.05)
    assert second_in.is_set() is False
    assert third_in.is_set() is False
    release_first.set()
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and not (second_in.is_set() or third_in.is_set()):
        time.sleep(0.01)
    assert second_in.is_set() != third_in.is_set()
    time.sleep(0.05)
    assert second_in.is_set() != third_in.is_set()
    release_rest.set()
    for thread in threads:
        thread.join(timeout=2.0)
        assert thread.is_alive() is False


def test_grammar_llm_request_gate_limit_2_allows_parallel() -> None:
    entered = threading.Barrier(2)
    inside: list[int] = []
    lock = threading.Lock()

    def worker() -> None:
        with lc.grammar_llm_request_gate(2):
            entered.wait(timeout=2.0)
            with lock:
                inside.append(lc._GRAMMAR_INFLIGHT_COUNT)
            time.sleep(0.05)

    t1 = threading.Thread(target=worker)
    t2 = threading.Thread(target=worker)
    t1.start()
    t2.start()
    t1.join(timeout=3.0)
    t2.join(timeout=3.0)
    assert t1.is_alive() is False and t2.is_alive() is False
    assert max(inside) == 2


def test_llm_request_lane_restores_status_after_waiting():
    from plugin.framework.llm_lanes import llm_request_lane, _LLM_REQUEST_LOCK
    import threading
    import time

    # Block the lane
    _LLM_REQUEST_LOCK.acquire()

    status_calls = []
    def status_callback(msg: str):
        status_calls.append(msg)

    def worker():
        # wait a little then release so lane can be acquired
        time.sleep(0.4)
        _LLM_REQUEST_LOCK.release()

    t = threading.Thread(target=worker)
    t.start()

    with llm_request_lane(timeout=2.0, status_callback=status_callback, resume_status="Thinking..."):
        pass

    t.join()

    assert len(status_calls) == 2
    assert "Waiting for another document's reply..." in status_calls[0]
    assert status_calls[1] == "Thinking..."


def test_llm_request_lane_uncontended_does_not_call_status():
    from plugin.framework.llm_lanes import llm_request_lane, _LLM_REQUEST_LOCK

    # Ensure uncontended
    if _LLM_REQUEST_LOCK.locked():
        _LLM_REQUEST_LOCK.release()

    status_calls = []
    def status_callback(msg: str):
        status_calls.append(msg)

    with llm_request_lane(timeout=2.0, status_callback=status_callback, resume_status="Thinking..."):
        pass

    assert len(status_calls) == 0


def test_llm_request_lane_stop_while_waiting():
    from plugin.framework.llm_lanes import llm_request_lane, _LLM_REQUEST_LOCK

    # Block the lane
    _LLM_REQUEST_LOCK.acquire()
    try:
        class FakeCancellation:
            def is_cancelled(self):
                return True
        from unittest.mock import patch
        with patch('plugin.framework.llm_lanes.get_current_send_cancellation', return_value=FakeCancellation()):
            try:
                with llm_request_lane(timeout=1.0):
                    pass
                assert False, "Should have raised BlockingWaitStopped"
            except BlockingWaitStopped:
                pass
    finally:
        _LLM_REQUEST_LOCK.release()


def test_llm_request_lane_resume_callback_raise_releases_lock() -> None:
    from plugin.framework.llm_lanes import llm_request_lane, _LLM_REQUEST_LOCK
    from plugin.framework.i18n import _

    if _LLM_REQUEST_LOCK.locked():
        _LLM_REQUEST_LOCK.release()

    lock_held = False
    releaser = None
    try:
        assert _LLM_REQUEST_LOCK.acquire(timeout=1.0)
        lock_held = True

        def releaser_target() -> None:
            time.sleep(0.4)
            _LLM_REQUEST_LOCK.release()

        releaser = threading.Thread(target=releaser_target, daemon=True)
        releaser.start()
        lock_held = False

        status_calls: list[str] = []

        def cb(msg: str) -> None:
            status_calls.append(msg)
            if len(status_calls) == 2:
                raise RuntimeError("status failed")

        inside_body = False
        with pytest.raises(RuntimeError, match="status failed"):
            with llm_request_lane(timeout=2.0, status_callback=cb, resume_status="Thinking..."):
                inside_body = True

        assert not inside_body
        releaser.join(timeout=2.0)
        assert not releaser.is_alive()

        acquired = _LLM_REQUEST_LOCK.acquire(timeout=1.0)
        assert acquired
        lock_held = True
        _LLM_REQUEST_LOCK.release()
        lock_held = False

        assert len(status_calls) == 2
        assert status_calls[0] == _("Waiting for another document's reply...")
        assert status_calls[1] == "Thinking..."
    finally:
        if lock_held:
            try:
                _LLM_REQUEST_LOCK.release()
            except RuntimeError:
                pass
        if releaser is not None and releaser.is_alive():
            releaser.join(timeout=1.0)


def test_grammar_llm_request_gate_timeout_when_full() -> None:
    from plugin.framework.llm_lanes import grammar_llm_request_gate, _GRAMMAR_INFLIGHT_LOCK
    import plugin.framework.llm_lanes as lc

    with _GRAMMAR_INFLIGHT_LOCK:
        assert lc._GRAMMAR_INFLIGHT_COUNT == 0
        lc._GRAMMAR_INFLIGHT_COUNT = 2
    try:
        t0 = time.monotonic()
        with pytest.raises(TimeoutError, match=r"after 0\.2s"):
            with grammar_llm_request_gate(2, timeout=0.2):
                pass
        elapsed = time.monotonic() - t0
        assert 0.15 <= elapsed < 0.8
        with _GRAMMAR_INFLIGHT_LOCK:
            assert lc._GRAMMAR_INFLIGHT_COUNT == 2
    finally:
        with _GRAMMAR_INFLIGHT_LOCK:
            lc._GRAMMAR_INFLIGHT_COUNT = 0


def test_grammar_llm_request_gate_wakeup_does_not_restart_timeout() -> None:
    from plugin.framework.llm_lanes import grammar_llm_request_gate, _GRAMMAR_INFLIGHT_LOCK, _GRAMMAR_INFLIGHT_CV
    import plugin.framework.llm_lanes as lc

    with _GRAMMAR_INFLIGHT_LOCK:
        assert lc._GRAMMAR_INFLIGHT_COUNT == 0
        lc._GRAMMAR_INFLIGHT_COUNT = 2
    recorded_timeouts: list[float] = []
    orig_wait = _GRAMMAR_INFLIGHT_CV.wait

    def fake_wait(timeout: float | None = None) -> bool:
        assert timeout is not None
        recorded_timeouts.append(timeout)
        if len(recorded_timeouts) == 1:
            time.sleep(0.25)
            return True
        return orig_wait(timeout)

    try:
        with patch.object(_GRAMMAR_INFLIGHT_CV, "wait", side_effect=fake_wait):
            with pytest.raises(TimeoutError, match=r"after 0\.5s"):
                with grammar_llm_request_gate(2, timeout=0.5):
                    pass
        assert len(recorded_timeouts) == 2
        assert recorded_timeouts[0] > 0.45
        assert recorded_timeouts[1] < 0.40
    finally:
        with _GRAMMAR_INFLIGHT_LOCK:
            lc._GRAMMAR_INFLIGHT_COUNT = 0


def test_llm_request_lane_acquire_is_capped_by_remaining() -> None:
    from plugin.framework.llm_lanes import llm_request_lane

    recorded: list[float] = []

    def spy_acquire(blocking: bool = True, timeout: float = -1) -> bool:
        recorded.append(timeout)
        if timeout > 0:
            time.sleep(timeout)
        return False

    mock_lock = MagicMock()
    mock_lock.acquire = spy_acquire

    with patch("plugin.framework.llm_lanes._LLM_REQUEST_LOCK", mock_lock):
        with pytest.raises(TimeoutError):
            with llm_request_lane(timeout=0.40):
                pass

    assert all(t <= 0.25 for t in recorded)
    assert sum(recorded) <= 0.42
    assert recorded[-1] < 0.25


def test_llm_request_lane_stop_is_polled_during_acquire() -> None:
    from plugin.framework.llm_lanes import _LLM_REQUEST_LOCK, llm_request_lane

    stop_event = threading.Event()

    class _FakeCancellation:
        def is_cancelled(self) -> bool:
            return stop_event.is_set()

    fake_cancel = _FakeCancellation()
    thread_exc: list[Exception] = []

    def worker() -> None:
        try:
            with llm_request_lane(timeout=5.0):
                pass
        except Exception as e:
            thread_exc.append(e)

    if _LLM_REQUEST_LOCK.locked():
        try:
            _LLM_REQUEST_LOCK.release()
        except RuntimeError:
            pass

    assert _LLM_REQUEST_LOCK.acquire(timeout=2.0)
    lock_held = True
    worker_thread = None
    try:
        with patch("plugin.framework.llm_lanes.get_current_send_cancellation", return_value=fake_cancel):
            worker_thread = threading.Thread(target=worker, daemon=True)
            worker_thread.start()
            time.sleep(0.3)
            stop_event.set()
            worker_thread.join(timeout=1.0)
            assert not worker_thread.is_alive()
            assert len(thread_exc) == 1
            assert isinstance(thread_exc[0], BlockingWaitStopped)
    finally:
        if lock_held:
            _LLM_REQUEST_LOCK.release()
            lock_held = False
        if worker_thread is not None:
            worker_thread.join(timeout=2.0)
