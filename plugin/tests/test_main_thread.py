import threading
import queue
import pytest
from unittest.mock import patch, MagicMock

from plugin.framework.worker_pool import run_in_background

from plugin.framework.main_thread import (
    _WorkItem,
    execute_on_main_thread,
    post_to_main_thread,
    _work_queue
)

@pytest.fixture(autouse=True)
def empty_work_queue():
    while not _work_queue.empty():
        try:
            _work_queue.get_nowait()
        except queue.Empty:
            break

def test_work_item():
    def func(x): return x * 2
    item = _WorkItem(func, (5,), {})

    assert item.fn is func
    assert item.args == (5,)
    assert not item.event.is_set()

def test_execute_on_main_thread_direct():
    # Calling it from main thread should just execute synchronously
    def func():
        return 42

    # We are in the main thread of pytest
    assert threading.current_thread() is threading.main_thread()

    res = execute_on_main_thread(func)
    assert res == 42


@patch("plugin.framework.main_thread._get_async_callback")
@patch("plugin.framework.main_thread._poke_vcl")
def test_execute_on_main_thread_background(mock_poke, mock_get_async):
    """
    Test where caller is not threading.main_thread(), mock _get_async_callback
    to force AsyncCallback path, and validate results/exceptions are returned.
    """
    mock_get_async.return_value = MagicMock()

    def func_to_run(x):
        if x == 0:
            raise ValueError("Zero not allowed")
        return x * 10

    def mock_poke_vcl():
        # Simulate VCL event loop calling notify()
        try:
            item = _work_queue.get_nowait()
            try:
                item.result = item.fn(*item.args, **item.kwargs)
            except Exception as e:
                item.exception = e
            finally:
                item.event.set()
        except queue.Empty:
            pass

    mock_poke.side_effect = mock_poke_vcl

    results = {}
    exceptions = {}

    def bg_thread(val):
        try:
            res = execute_on_main_thread(func_to_run, val)
            results[val] = res
        except Exception as e:
            exceptions[val] = e

    t1 = run_in_background(bg_thread, 5, daemon=False)
    t2 = run_in_background(bg_thread, 0, daemon=False)

    t1.join(timeout=2.0)
    t2.join(timeout=2.0)

    assert results.get(5) == 50
    assert isinstance(exceptions.get(0), ValueError)
    assert str(exceptions[0]) == "Zero not allowed"


@patch("plugin.framework.main_thread._get_async_callback")
@patch("plugin.framework.main_thread._poke_vcl")
def test_execute_on_main_thread_timeout(mock_poke, mock_get_async):
    """
    Test that forces item.event.wait(timeout) to time out and asserts
    the raised TimeoutError message includes the function name.
    """
    mock_get_async.return_value = MagicMock()
    # mock_poke does nothing, so the work item is never executed

    def slow_func():
        pass

    exc_caught = None
    def bg_thread():
        nonlocal exc_caught
        try:
            execute_on_main_thread(slow_func, timeout=0.1)
        except Exception as e:
            exc_caught = e

    t = run_in_background(bg_thread, daemon=False)
    t.join(timeout=1.0)

    assert isinstance(exc_caught, TimeoutError)
    assert "slow_func" in str(exc_caught)
    assert "timed out after 0.1s" in str(exc_caught)


@patch("plugin.framework.main_thread._get_async_callback")
@patch("plugin.framework.main_thread._poke_vcl")
def test_post_to_main_thread_fire_and_forget(mock_poke, mock_get_async):
    """
    Test for post_to_main_thread() that ensures it enqueues the work item
    without blocking (and still calls _poke_vcl()).
    """
    mock_get_async.return_value = MagicMock()

    def my_func():
        pass

    post_to_main_thread(my_func)

    # Check that it enqueued the item
    item = _work_queue.get_nowait()
    assert item.fn is my_func

    # Check that it called _poke_vcl
    mock_poke.assert_called_once()
