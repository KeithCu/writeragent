import queue
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from plugin.framework.blocking_wait import BlockingWaitStopped, run_blocking_in_thread


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
        with patch("plugin.framework.queue_executor.pump_main_thread_work_queue") as pump_queue:
            def slow_worker():
                time.sleep(0.3)
                return "ok"
            assert run_blocking_in_thread(ctx, slow_worker, pump_idle=False) == "ok"
    pump.assert_not_called()
    pump_queue.assert_called()


def test_run_blocking_in_thread_stop_checker_returns_without_pump():
    ctx = MagicMock()
    stop = threading.Event()

    def _slow():
        time.sleep(2)
        return "done"

    def _checker():
        return stop.is_set()

    stop.set()
    with patch("plugin.framework.blocking_wait.pump_ui_idle") as pump:
        with pytest.raises(BlockingWaitStopped):
            run_blocking_in_thread(ctx, _slow, pump_idle=False, stop_checker=_checker)
    pump.assert_not_called()


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
