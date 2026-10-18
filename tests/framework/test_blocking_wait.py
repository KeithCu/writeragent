import gc
import queue
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from plugin.framework.blocking_wait import automatic_gc_suspended, run_blocking_in_thread


class DummyToolkit:
    def __init__(self):
        self.idle_calls = 0

    def processEventsToIdle(self):
        self.idle_calls += 1


def test_run_blocking_in_thread():
    ctx = MagicMock()
    ctx.getServiceManager.return_value = MagicMock()

    def blocking_func():
        return "success"

    assert run_blocking_in_thread(ctx, blocking_func) == "success"


def test_run_blocking_in_thread_pump_idle_false_does_not_pump():
    ctx = MagicMock()
    with patch("plugin.framework.blocking_wait.pump_ui_idle") as pump:
        with patch("plugin.framework.blocking_wait.pump_main_thread_work_queue") as pump_queue:
            def slow_worker():
                time.sleep(0.3)
                return "ok"
            assert run_blocking_in_thread(ctx, slow_worker, pump_idle=False) == "ok"
    pump.assert_not_called()
    pump_queue.assert_called()


def test_off_main_no_pump_does_not_poll_with_timeout():
    ctx = MagicMock()
    timeouts: list[float | None] = []
    real_get = queue.Queue.get

    def spy_get(self, block=True, timeout=None):
        timeouts.append(timeout)
        return real_get(self, block=block, timeout=timeout)

    with patch("plugin.framework.blocking_wait.on_main_thread", return_value=False):
        with patch("plugin.framework.blocking_wait.pump_main_thread_work_queue") as pump_queue:
            with patch("plugin.framework.blocking_wait.pump_ui_idle") as pump_ui:
                with patch.object(queue.Queue, "get", spy_get):
                    assert run_blocking_in_thread(ctx, lambda: "ok", pump_idle=False) == "ok"
    assert timeouts == [None]
    pump_queue.assert_not_called()
    pump_ui.assert_not_called()


def test_run_blocking_in_thread_toolkit_fail_runs_off_caller():
    caller = threading.get_ident()
    ran_on: list[int] = []
    ctx = MagicMock()
    ctx.getServiceManager.side_effect = RuntimeError("no toolkit")

    def blocking_func():
        ran_on.append(threading.get_ident())
        return "ok"

    assert run_blocking_in_thread(ctx, blocking_func) == "ok"
    assert ran_on and ran_on[0] != caller


def test_run_blocking_in_thread_error():
    ctx = MagicMock()
    ctx.getServiceManager.return_value = MagicMock()

    def blocking_func():
        raise ValueError("failed")

    with pytest.raises(ValueError, match="failed"):
        run_blocking_in_thread(ctx, blocking_func)


def test_run_blocking_in_thread_baseexception_does_not_hang():
    class Boom(BaseException):
        pass

    ctx = MagicMock()
    ctx.getServiceManager.return_value = MagicMock()

    def blocking_func():
        raise Boom("hard fault")

    with pytest.raises(Boom, match="hard fault"):
        run_blocking_in_thread(ctx, blocking_func, pump_idle=False)


def test_run_blocking_in_thread_queue_empty_from_worker_is_not_swallowed():
    """queue.Empty from func must surface. The wait loop must not hang."""
    ctx = MagicMock()
    holder: list[BaseException] = []

    def raise_empty() -> None:
        raise queue.Empty("worker empty")

    def run() -> None:
        try:
            run_blocking_in_thread(ctx, raise_empty, pump_idle=False)
        except BaseException as exc:
            holder.append(exc)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(2)
    assert not thread.is_alive(), "run_blocking_in_thread hung after the worker raised queue.Empty"
    assert len(holder) == 1
    assert isinstance(holder[0], queue.Empty)
    assert str(holder[0]) == "worker empty"


def test_run_blocking_in_thread_pump_idle_uses_get_toolkit():
    toolkit = DummyToolkit()
    ctx = MagicMock()
    with patch("plugin.framework.uno_context.get_toolkit", return_value=toolkit) as get_tk:
        assert run_blocking_in_thread(ctx, lambda: "ok") == "ok"
    get_tk.assert_called_once_with(ctx)


def test_run_blocking_in_thread_pump_idle_true_toolkit_none_drains_marshal_queue():
    """Missing toolkit when pump_idle=True must poll and call pump_main_thread_work_queue instead of hanging."""
    ctx = MagicMock()
    unblock_worker = threading.Event()
    result_holder: list[str] = []

    def worker_func() -> str:
        # Simulate worker waiting for main thread marshal queue to be drained
        if not unblock_worker.wait(timeout=2):
            raise TimeoutError("worker timed out waiting for marshal queue pump")
        return "unblocked_result"

    def fake_pump_main_thread_work_queue() -> None:
        unblock_worker.set()

    with patch("plugin.framework.uno_context.get_toolkit", return_value=None):
        with patch("plugin.framework.blocking_wait.pump_ui_idle") as mock_pump_ui:
            with patch("plugin.framework.blocking_wait.pump_main_thread_work_queue", side_effect=fake_pump_main_thread_work_queue) as mock_pump_marshal:
                def waiter() -> None:
                    with patch("plugin.framework.blocking_wait.on_main_thread", return_value=True):
                        res = run_blocking_in_thread(ctx, worker_func, pump_idle=True)
                        result_holder.append(res)

                waiter_thread = threading.Thread(target=waiter, daemon=True)
                waiter_thread.start()
                waiter_thread.join(timeout=2)
                assert not waiter_thread.is_alive(), "waiter thread hung with toolkit=None"

    assert result_holder == ["unblocked_result"]
    mock_pump_ui.assert_not_called()
    mock_pump_marshal.assert_called()


def test_run_blocking_in_thread_pump_idle_true_toolkit_raises_drains_marshal_queue():
    """get_toolkit raising when pump_idle=True must poll and call pump_main_thread_work_queue instead of hanging."""
    ctx = MagicMock()
    unblock_worker = threading.Event()
    result_holder: list[str] = []

    def worker_func() -> str:
        if not unblock_worker.wait(timeout=2):
            raise TimeoutError("worker timed out waiting for marshal queue pump")
        return "unblocked_from_raise"

    def fake_pump_main_thread_work_queue() -> None:
        unblock_worker.set()

    with patch("plugin.framework.uno_context.get_toolkit", side_effect=RuntimeError("toolkit failed")):
        with patch("plugin.framework.blocking_wait.pump_ui_idle") as mock_pump_ui:
            with patch("plugin.framework.blocking_wait.pump_main_thread_work_queue", side_effect=fake_pump_main_thread_work_queue) as mock_pump_marshal:
                def waiter() -> None:
                    with patch("plugin.framework.blocking_wait.on_main_thread", return_value=True):
                        res = run_blocking_in_thread(ctx, worker_func, pump_idle=True)
                        result_holder.append(res)

                waiter_thread = threading.Thread(target=waiter, daemon=True)
                waiter_thread.start()
                waiter_thread.join(timeout=2)
                assert not waiter_thread.is_alive(), "waiter thread hung when get_toolkit raised"

    assert result_holder == ["unblocked_from_raise"]
    mock_pump_ui.assert_not_called()
    mock_pump_marshal.assert_called()


def test_run_blocking_in_thread_pump_idle_true_toolkit_present_calls_pump_ui_idle():
    """Present toolkit when pump_idle=True calls pump_ui_idle and not pump_main_thread_work_queue."""
    toolkit = DummyToolkit()
    ctx = MagicMock()
    unblock_worker = threading.Event()
    result_holder: list[str] = []

    def worker_func() -> str:
        if not unblock_worker.wait(timeout=2):
            raise TimeoutError("worker timed out waiting for pump_ui_idle")
        return "unblocked_by_ui_idle"

    def fake_pump_ui_idle(_tk: object) -> None:
        unblock_worker.set()

    with patch("plugin.framework.uno_context.get_toolkit", return_value=toolkit):
        with patch("plugin.framework.blocking_wait.pump_ui_idle", side_effect=fake_pump_ui_idle) as mock_pump_ui:
            with patch("plugin.framework.blocking_wait.pump_main_thread_work_queue") as mock_pump_marshal:
                def waiter() -> None:
                    res = run_blocking_in_thread(ctx, worker_func, pump_idle=True)
                    result_holder.append(res)

                waiter_thread = threading.Thread(target=waiter, daemon=True)
                waiter_thread.start()
                waiter_thread.join(timeout=2)
                assert not waiter_thread.is_alive(), "waiter thread hung"

    assert result_holder == ["unblocked_by_ui_idle"]
    mock_pump_ui.assert_called()
    mock_pump_marshal.assert_not_called()



# ---- automatic GC stays off while a pump_idle=False wait holds SolarMutex ----


@pytest.fixture
def gc_enabled_restored():
    was = gc.isenabled()
    gc.enable()
    try:
        yield
    finally:
        if was:
            gc.enable()
        else:
            gc.disable()


def test_automatic_gc_suspended_nested_keeps_gc_off_until_outer_exit(gc_enabled_restored):
    with automatic_gc_suspended():
        assert not gc.isenabled()
        with automatic_gc_suspended():
            assert not gc.isenabled()
        # The inner call found GC already off, so it must not turn it back on.
        assert not gc.isenabled()
    assert gc.isenabled()


def test_automatic_gc_suspended_leaves_disabled_gc_disabled(gc_enabled_restored):
    gc.disable()
    with automatic_gc_suspended():
        assert not gc.isenabled()
    assert not gc.isenabled()


def test_automatic_gc_suspended_restores_on_error(gc_enabled_restored):
    with pytest.raises(RuntimeError):
        with automatic_gc_suspended():
            raise RuntimeError("boom")
    assert gc.isenabled()


def test_pump_idle_false_worker_runs_with_gc_off_and_nested_call_does_not_reenable(gc_enabled_restored):
    ctx = MagicMock()
    seen: list[tuple[str, bool]] = []

    def inner_worker():
        seen.append(("inner", gc.isenabled()))
        return "inner"

    def outer_worker():
        seen.append(("outer", gc.isenabled()))
        return "outer"

    with patch("plugin.framework.blocking_wait.pump_main_thread_work_queue"):
        with automatic_gc_suspended():
            # Caller already suspended GC (for example an outer blocking wait).
            assert run_blocking_in_thread(ctx, inner_worker, pump_idle=False) == "inner"
            assert not gc.isenabled()
        assert gc.isenabled()
        assert run_blocking_in_thread(ctx, outer_worker, pump_idle=False) == "outer"
    assert seen == [("inner", False), ("outer", False)]
    assert gc.isenabled()


def test_pump_idle_true_leaves_gc_alone(gc_enabled_restored):
    ctx = MagicMock()
    seen: list[bool] = []

    def worker():
        seen.append(gc.isenabled())
        return "ok"

    with patch("plugin.framework.blocking_wait.pump_ui_idle"):
        assert run_blocking_in_thread(ctx, worker) == "ok"
    assert seen == [True]
