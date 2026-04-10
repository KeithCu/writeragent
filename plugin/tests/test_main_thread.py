import threading
import queue
import pytest
from unittest.mock import patch, MagicMock

from plugin.tests.testing_utils import setup_uno_mocks
setup_uno_mocks()

from plugin.framework.worker_pool import run_in_background

from plugin.framework.queue_executor import (
    _WorkItem,
    execute_on_main_thread,
    post_to_main_thread,
    default_executor
)
import plugin.framework.queue_executor as mt

@pytest.fixture(autouse=True)
def empty_work_queue():
    while not default_executor._work_queue.empty():
        try:
            default_executor._work_queue.get_nowait()
        except queue.Empty:
            break

def test_work_item():
    def func(x): return x * 2
    item = _WorkItem("id", func, (5,), {})

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


@patch.object(mt.QueueExecutor, "_get_async_callback")
@patch.object(mt.QueueExecutor, "_poke_main_thread")

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

    def mock_poke_main_thread():
        # Simulate VCL event loop calling notify()
        default_executor.process_queue()

    mock_poke.side_effect = mock_poke_main_thread

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


@patch.object(mt.QueueExecutor, "_get_async_callback")
@patch.object(mt.QueueExecutor, "_poke_main_thread")

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


@patch.object(mt.QueueExecutor, "_get_async_callback")
@patch.object(mt.QueueExecutor, "_poke_main_thread")

def test_post_to_main_thread_fire_and_forget(mock_poke, mock_get_async):
    """
    Test for post_to_main_thread() that ensures it enqueues the work item
    without blocking (and still calls _poke_main_thread()).
    """
    mock_get_async.return_value = MagicMock()

    def my_func():
        pass

    post_to_main_thread(my_func)

    # Check that it enqueued the item
    item = default_executor._work_queue.get_nowait()
    assert item.fn is my_func

    # Check that it called _poke_main_thread
    mock_poke.assert_called_once()

@pytest.fixture(autouse=True)
def reset_mt_globals():
    default_executor._initialized = False
    default_executor._async_callback_service = None
    default_executor._callback_instance = None
    with default_executor._init_lock:
        pass
    while not default_executor._work_queue.empty():
        try:
            default_executor._work_queue.get_nowait()
        except queue.Empty:
            break
    yield
    default_executor._initialized = False
    default_executor._async_callback_service = None
    default_executor._callback_instance = None

def test_get_async_callback_success(monkeypatch):
    import sys
    mock_uno = MagicMock()
    mock_ctx = MagicMock()
    mock_uno.getComponentContext.return_value = mock_ctx
    mock_smgr = MagicMock()
    mock_ctx.ServiceManager = mock_smgr
    mock_service = MagicMock()
    mock_smgr.createInstanceWithContext.return_value = mock_service

    monkeypatch.setitem(sys.modules, 'uno', mock_uno)

    with patch.object(default_executor, '_make_callback_instance') as mock_make:
        mock_instance = MagicMock()
        mock_make.return_value = mock_instance
        res = default_executor._get_async_callback()

    assert res == mock_service
    assert default_executor._initialized == True
    assert default_executor._async_callback_service == mock_service
    assert default_executor._callback_instance == mock_instance

def test_get_async_callback_already_init():
    default_executor._initialized = True
    mock_svc = MagicMock()
    default_executor._async_callback_service = mock_svc
    assert default_executor._get_async_callback() == mock_svc

def test_get_async_callback_failure(monkeypatch):
    import sys
    mock_uno = MagicMock()
    mock_uno.getComponentContext.side_effect = Exception("No UNO")
    monkeypatch.setitem(sys.modules, 'uno', mock_uno)

    with patch('plugin.framework.queue_executor.log.warning') as mock_warn:
        res = default_executor._get_async_callback()

    assert res is None
    assert default_executor._initialized == True
    assert default_executor._async_callback_service is None
    mock_warn.assert_called()

def test_get_async_callback_returns_none(monkeypatch):
    import sys
    mock_uno = MagicMock()
    mock_ctx = MagicMock()
    mock_uno.getComponentContext.return_value = mock_ctx
    mock_smgr = MagicMock()
    mock_ctx.ServiceManager = mock_smgr
    mock_smgr.createInstanceWithContext.return_value = None
    monkeypatch.setitem(sys.modules, 'uno', mock_uno)

    with patch('plugin.framework.queue_executor.log.warning') as mock_warn:
        res = default_executor._get_async_callback()

    assert res is None
    assert default_executor._initialized == True
    mock_warn.assert_called()

def test_make_callback_instance():
    import sys
    mock_unohelper = MagicMock()
    class MockBase:
        pass
    mock_unohelper.Base = MockBase
    monkeypatch_modules = {
        'unohelper': mock_unohelper,
        'com': MagicMock(),
        'com.sun': MagicMock(),
        'com.sun.star': MagicMock(),
        'com.sun.star.awt': MagicMock()
    }

    with patch.dict(sys.modules, monkeypatch_modules):
        class MockXCallback:
            pass
        sys.modules['com.sun.star.awt'].XCallback = MockXCallback

        instance = default_executor._make_callback_instance()
        assert instance is not None
        assert hasattr(instance, 'notify')

def test_make_callback_instance_notify(monkeypatch):
    import sys
    mock_unohelper = MagicMock()
    class MockBase: pass
    mock_unohelper.Base = MockBase
    monkeypatch_modules = {
        'unohelper': mock_unohelper,
        'com': MagicMock(),
        'com.sun': MagicMock(),
        'com.sun.star': MagicMock(),
        'com.sun.star.awt': MagicMock()
    }

    with patch.dict(sys.modules, monkeypatch_modules):
        class MockXCallback: pass
        sys.modules['com.sun.star.awt'].XCallback = MockXCallback
        instance = default_executor._make_callback_instance()

        # Test empty queue
        instance.notify(None)

        # Test valid item
        def dummy_fn(x): return x * 2
        item = _WorkItem("id1", dummy_fn, (10,), {})
        default_executor._work_queue.put(item)

        with patch.object(default_executor, '_poke_main_thread') as mock_poke:
            instance.notify(None)
            assert item.result == 20
            assert item.exception is None
            assert item.event.is_set()
            mock_poke.assert_not_called()

        # Test item that raises exception
        def dummy_fn_exc(): raise ValueError("test error")
        item2 = _WorkItem("id2", dummy_fn_exc, (), {})
        default_executor._work_queue.put(item2)

        # Test queue not empty after popping
        item3 = _WorkItem("id3", lambda: 1, (), {})
        default_executor._work_queue.put(item3)

        with patch.object(default_executor, '_poke_main_thread') as mock_poke:
            instance.notify(None)
            assert item2.result is None
            assert isinstance(item2.exception, ValueError)
            assert item2.event.is_set()
            mock_poke.assert_called_once()

        # Clear queue
        default_executor._work_queue.get_nowait()

def test_poke_vcl(monkeypatch):
    import sys
    mock_uno = MagicMock()
    mock_uno.Any.return_value = "AnyVal"
    monkeypatch.setitem(sys.modules, 'uno', mock_uno)

    default_executor._async_callback_service = MagicMock()
    default_executor._callback_instance = MagicMock()

    # Success case
    default_executor._poke_main_thread()
    default_executor._async_callback_service.addCallback.assert_called_with(default_executor._callback_instance, "AnyVal")

    # Failure case 1: Any fails, retry without
    default_executor._async_callback_service.addCallback.side_effect = [Exception("error"), None]
    default_executor._poke_main_thread()
    default_executor._async_callback_service.addCallback.assert_called_with(default_executor._callback_instance, None)

    # Failure case 2: Both fail
    default_executor._async_callback_service.addCallback.side_effect = Exception("error")
    with patch('plugin.framework.queue_executor.log.warning') as mock_warn:
        default_executor._poke_main_thread()
        mock_warn.assert_called_once()

    default_executor._async_callback_service = None
    default_executor._poke_main_thread() # Should do nothing

def test_execute_on_main_thread_no_service():
    default_executor._async_callback_service = None
    default_executor._initialized = True # prevent initialization

    with patch.object(default_executor, '_get_async_callback') as mock_get:
        mock_get.return_value = None

        # Test fallback path when not on main thread
        def bg_thread():
            return mt.execute_on_main_thread(lambda x: x*2, 5)

        t = threading.Thread(target=bg_thread)
        t.start()
        t.join()

        # In actual test, we can mock threading.current_thread
        with patch('threading.current_thread') as mock_thread, patch('threading.main_thread') as mock_main:
            mock_thread.return_value = "Thread-1"
            mock_main.return_value = "Thread-2"

            res = mt.execute_on_main_thread(lambda x: x*2, 5)
            assert res == 10

def test_post_to_main_thread_no_service():
    with patch.object(default_executor, '_get_async_callback') as mock_get:
        mock_get.return_value = None

        # Test fallback
        called = False
        def fn(): nonlocal called; called = True

        mt.post_to_main_thread(fn)
        assert called

def test_execute_on_main_thread_success():
    with patch('threading.current_thread') as mock_thread, \
         patch('threading.main_thread') as mock_main, \
         patch.object(default_executor, '_get_async_callback') as mock_get, \
         patch.object(default_executor, '_poke_main_thread') as mock_poke:

        mock_thread.return_value = "Thread-1"
        mock_main.return_value = "Thread-2"
        mock_get.return_value = MagicMock()

        def mock_vcl():
            default_executor.process_queue()

        mock_poke.side_effect = mock_vcl

        res = mt.execute_on_main_thread(lambda x: x*2, 5)
        assert res == 10

def test_get_async_callback_already_init_with_lock():
    default_executor._initialized = False
    mock_svc = MagicMock()

    # We want to test the case where _initialized becomes true while waiting for lock.
    # To do this, we can make the lock acquisition call a side effect that sets _initialized=True
    real_lock = default_executor._init_lock
    class FakeLock:
        def __enter__(self):
            default_executor._initialized = True
            default_executor._async_callback_service = mock_svc
            return self
        def __exit__(self, exc_type, exc_val, exc_tb):
            pass

    default_executor._init_lock = FakeLock()
    try:
        assert default_executor._get_async_callback() == mock_svc
    finally:
        default_executor._init_lock = real_lock
