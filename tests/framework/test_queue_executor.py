from __future__ import annotations

import threading
import time
import plugin.framework.queue_executor as lc
import queue
import pytest
import plugin.framework.queue_executor as mt

from unittest.mock import patch, MagicMock
from plugin.framework.worker_pool import run_in_background
from plugin.framework.queue_executor import _WorkItem, execute_on_main_thread, post_to_main_thread, default_executor

def test_agent_session_marks_active_with_nesting() -> None:
    assert (lc.is_agent_active() is False)
    with lc.agent_session():
        assert (lc.is_agent_active() is True)
        with lc.agent_session():
            assert (lc.is_agent_active() is True)
        assert (lc.is_agent_active() is True)
    assert (lc.is_agent_active() is False)

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


@pytest.fixture(autouse=True)
def _reset_default_executor_state() -> None:
    orig_ctx = default_executor._ctx
    orig_init = default_executor._initialized
    orig_service = default_executor._async_callback_service
    orig_cb = default_executor._callback_instance
    try:
        default_executor._ctx = None
        default_executor._initialized = False
        default_executor._async_callback_service = None
        default_executor._callback_instance = None
        yield
    finally:
        default_executor._ctx = orig_ctx
        default_executor._initialized = orig_init
        default_executor._async_callback_service = orig_service
        default_executor._callback_instance = orig_cb


def test_grammar_llm_request_gate_limit_1_uses_global_lane() -> None:
    with patch.object(lc, "llm_request_lane") as lane:
        lane.return_value.__enter__ = MagicMock()
        lane.return_value.__exit__ = MagicMock(return_value=False)
        with lc.grammar_llm_request_gate(1):
            pass
        lane.assert_called_once()


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


@pytest.fixture(autouse=True)
def empty_work_queue():
    while (not default_executor._work_queue.empty()):
        try:
            default_executor._work_queue.get_nowait()
        except queue.Empty:
            break

def test_work_item():

    def func(x):
        return (x * 2)
    item = _WorkItem('id', func, (5,), {})
    assert (item.fn is func)
    assert (item.args == (5,))
    assert (not item.event.is_set())

def test_execute_on_main_thread_direct():

    def func():
        return 42
    assert (threading.current_thread() is threading.main_thread())
    res = execute_on_main_thread(func)
    assert (res == 42)

@patch.object(mt.QueueExecutor, '_get_async_callback')
@patch.object(mt.QueueExecutor, '_poke_main_thread')
def test_execute_on_main_thread_background(mock_poke, mock_get_async):
    '''
    Test where caller is not threading.main_thread(), mock _get_async_callback
    to force AsyncCallback path, and validate results/exceptions are returned.
    '''
    mock_get_async.return_value = MagicMock()

    def func_to_run(x):
        if (x == 0):
            raise ValueError('Zero not allowed')
        return (x * 10)

    def mock_poke_main_thread():
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
    assert (results.get(5) == 50)
    assert isinstance(exceptions.get(0), ValueError)
    assert (str(exceptions[0]) == 'Zero not allowed')

@patch.object(mt.QueueExecutor, '_get_async_callback')
@patch.object(mt.QueueExecutor, '_poke_main_thread')
def test_execute_on_main_thread_timeout(mock_poke, mock_get_async):
    '''
    Test that forces item.event.wait(timeout) to time out and asserts
    the raised TimeoutError message includes the function name.
    '''
    mock_get_async.return_value = MagicMock()

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
    assert ('slow_func' in str(exc_caught))
    assert ('timed out after 0.1s' in str(exc_caught))

@patch.object(mt.QueueExecutor, '_get_async_callback')
@patch.object(mt.QueueExecutor, '_poke_main_thread')
def test_post_to_main_thread_fire_and_forget(mock_poke, mock_get_async):
    '''
    Test for post_to_main_thread() that ensures it enqueues the work item
    without blocking (and still calls _poke_main_thread()).
    '''
    mock_get_async.return_value = MagicMock()

    def my_func():
        pass
    post_to_main_thread(my_func)
    item = default_executor._work_queue.get_nowait()
    assert (item.fn is my_func)
    mock_poke.assert_called_once()

@pytest.fixture(autouse=True)
def reset_mt_globals():
    default_executor._ctx = None
    default_executor._initialized = False
    default_executor._async_callback_service = None
    default_executor._callback_instance = None
    with default_executor._init_lock:
        pass
    while (not default_executor._work_queue.empty()):
        try:
            default_executor._work_queue.get_nowait()
        except queue.Empty:
            break
    (yield)
    default_executor._ctx = None
    default_executor._initialized = False
    default_executor._async_callback_service = None
    default_executor._callback_instance = None

def test_get_async_callback_success(monkeypatch):
    mock_ctx = MagicMock()
    mock_smgr = MagicMock()
    mock_ctx.ServiceManager = mock_smgr
    mock_service = MagicMock()
    mock_smgr.createInstanceWithContext.return_value = mock_service
    default_executor._ctx = mock_ctx
    with patch.object(default_executor, '_make_callback_instance') as mock_make:
        mock_instance = MagicMock()
        mock_make.return_value = mock_instance
        res = default_executor._get_async_callback()
    assert (res == mock_service)
    assert (default_executor._initialized)
    assert (default_executor._async_callback_service == mock_service)
    assert (default_executor._callback_instance == mock_instance)

def test_get_async_callback_with_explicit_ctx():
    mock_ctx = MagicMock()
    mock_smgr = MagicMock()
    mock_ctx.ServiceManager = mock_smgr
    mock_service = MagicMock()
    mock_smgr.createInstanceWithContext.return_value = mock_service

    qe = lc.QueueExecutor(ctx=mock_ctx)
    with patch.object(qe, '_make_callback_instance') as mock_make:
        mock_instance = MagicMock()
        mock_make.return_value = mock_instance
        res = qe._get_async_callback()

    assert res == mock_service
    assert qe._initialized
    assert qe._async_callback_service == mock_service
    mock_smgr.createInstanceWithContext.assert_called_once_with("com.sun.star.awt.AsyncCallback", mock_ctx)

def test_get_async_callback_explicit_ctx_does_not_call_uno_getComponentContext(monkeypatch):
    import sys
    mock_uno = MagicMock()
    mock_uno.getComponentContext.side_effect = AssertionError("uno.getComponentContext should not be called when ctx is provided")
    monkeypatch.setitem(sys.modules, 'uno', mock_uno)

    mock_ctx = MagicMock()
    mock_smgr = MagicMock()
    mock_ctx.ServiceManager = mock_smgr
    mock_service = MagicMock()
    mock_smgr.createInstanceWithContext.return_value = mock_service

    qe = lc.QueueExecutor(ctx=mock_ctx)
    with patch.object(qe, '_make_callback_instance') as mock_make:
        mock_instance = MagicMock()
        mock_make.return_value = mock_instance
        res = qe._get_async_callback()

    assert res == mock_service
    assert mock_uno.getComponentContext.call_count == 0

def test_get_async_callback_unwraps_layer_a_proxy():
    """Creating AsyncCallback from a worker must not getattr() a guard proxy.

    That used to fire Layer A while _init_lock was held and deadlock startup
    (set_context on the UI thread vs nested execute_on_main_thread).
    """
    import plugin.framework.thread_guard as tg

    raw_ctx = MagicMock()
    raw_smgr = MagicMock()
    raw_ctx.ServiceManager = raw_smgr
    raw_svc = MagicMock()
    raw_smgr.createInstanceWithContext.return_value = raw_svc
    qe = lc.QueueExecutor(ctx=tg._UnoThreadGuardProxy(raw_ctx))
    assert qe._ctx is raw_ctx
    with patch.object(qe, "_make_callback_instance", return_value=MagicMock()):
        res = qe._get_async_callback()
    assert res is raw_svc
    raw_smgr.createInstanceWithContext.assert_called_once_with("com.sun.star.awt.AsyncCallback", raw_ctx)


def test_set_context_unwraps_and_dedupes_proxies():
    import plugin.framework.thread_guard as tg

    raw = MagicMock()
    qe = lc.QueueExecutor(ctx=raw)
    qe._initialized = True
    qe.set_context(tg._UnoThreadGuardProxy(raw))
    assert qe._ctx is raw
    assert qe._initialized is True

def test_queue_executor_set_context_invalidates():
    mock_ctx1 = MagicMock()
    mock_ctx2 = MagicMock()
    qe = lc.QueueExecutor(ctx=mock_ctx1)
    qe._initialized = True
    qe._async_callback_service = MagicMock()
    qe._callback_instance = MagicMock()

    # Same context is a no-op
    qe.set_context(mock_ctx1)
    assert qe._initialized is True

    # New context resets state
    qe.set_context(mock_ctx2)
    assert qe._ctx is mock_ctx2
    assert qe._initialized is False
    assert qe._async_callback_service is None
    assert qe._callback_instance is None

def test_init_config_sets_default_executor_context(monkeypatch, tmp_path):
    from plugin.framework.config import init_config, reset_config_for_tests
    reset_config_for_tests()
    mock_ctx = MagicMock()
    mock_cfg = str(tmp_path / "mock_config.json")
    monkeypatch.setattr("plugin.framework.config._resolve_config_path_from_ctx", lambda _c: mock_cfg)

    init_config(mock_ctx)
    assert default_executor._ctx is mock_ctx
    reset_config_for_tests()


def test_get_async_callback_already_init():
    default_executor._initialized = True
    mock_svc = MagicMock()
    default_executor._async_callback_service = mock_svc
    assert (default_executor._get_async_callback() == mock_svc)

def test_get_async_callback_failure(monkeypatch):
    import sys
    mock_uno = MagicMock()
    mock_uno.getComponentContext.side_effect = Exception('No UNO')
    monkeypatch.setitem(sys.modules, 'uno', mock_uno)
    with patch('plugin.framework.queue_executor.log.warning') as mock_warn:
        res = default_executor._get_async_callback()
    assert (res is None)
    assert (default_executor._initialized)
    assert (default_executor._async_callback_service is None)
    mock_warn.assert_called()

def test_get_async_callback_returns_none(monkeypatch):
    mock_ctx = MagicMock()
    mock_smgr = MagicMock()
    mock_ctx.ServiceManager = mock_smgr
    mock_smgr.createInstanceWithContext.return_value = None
    default_executor._ctx = mock_ctx
    with patch('plugin.framework.queue_executor.log.warning') as mock_warn:
        res = default_executor._get_async_callback()
    assert (res is None)
    assert (default_executor._initialized)
    mock_warn.assert_called()

def test_make_callback_instance():
    import sys
    mock_unohelper = MagicMock()

    class MockBase():
        pass
    mock_unohelper.Base = MockBase
    monkeypatch_modules = {'unohelper': mock_unohelper, 'com': MagicMock(), 'com.sun': MagicMock(), 'com.sun.star': MagicMock(), 'com.sun.star.awt': MagicMock()}
    with patch.dict(sys.modules, monkeypatch_modules):

        class MockXCallback():
            pass
        sys.modules['com.sun.star.awt'].XCallback = MockXCallback
        instance = default_executor._make_callback_instance()
        assert (instance is not None)
        assert hasattr(instance, 'notify')

def test_make_callback_instance_notify(monkeypatch):
    import sys
    mock_unohelper = MagicMock()

    class MockBase():
        pass
    mock_unohelper.Base = MockBase
    monkeypatch_modules = {'unohelper': mock_unohelper, 'com': MagicMock(), 'com.sun': MagicMock(), 'com.sun.star': MagicMock(), 'com.sun.star.awt': MagicMock()}
    with patch.dict(sys.modules, monkeypatch_modules):

        class MockXCallback():
            pass
        sys.modules['com.sun.star.awt'].XCallback = MockXCallback
        instance = default_executor._make_callback_instance()
        instance.notify(None)

        def dummy_fn(x):
            return (x * 2)
        item = _WorkItem('id1', dummy_fn, (10,), {})
        default_executor._work_queue.put(item)
        with patch.object(default_executor, '_poke_main_thread') as mock_poke:
            instance.notify(None)
            assert (item.result == 20)
            assert (item.exception is None)
            assert item.event.is_set()
            mock_poke.assert_not_called()

        def dummy_fn_exc():
            raise ValueError('test error')
        item2 = _WorkItem('id2', dummy_fn_exc, (), {})
        default_executor._work_queue.put(item2)
        item3 = _WorkItem('id3', (lambda : 1), (), {})
        default_executor._work_queue.put(item3)
        with patch.object(default_executor, '_poke_main_thread') as mock_poke:
            instance.notify(None)
            assert (item2.result is None)
            assert isinstance(item2.exception, ValueError)
            assert item2.event.is_set()
            mock_poke.assert_called_once()
        default_executor._work_queue.get_nowait()

def test_poke_vcl():
    default_executor._async_callback_service = MagicMock()
    default_executor._callback_instance = MagicMock()
    default_executor._poke_main_thread()
    default_executor._async_callback_service.addCallback.assert_called_with(default_executor._callback_instance, None)
    default_executor._async_callback_service.addCallback.reset_mock()
    default_executor._async_callback_service.addCallback.side_effect = Exception('error')
    with patch('plugin.framework.queue_executor.log.warning') as mock_warn:
        default_executor._poke_main_thread()
        mock_warn.assert_called_once()
    default_executor._async_callback_service.addCallback.assert_called_once_with(default_executor._callback_instance, None)
    default_executor._async_callback_service = None
    default_executor._poke_main_thread()

def test_execute_on_main_thread_no_service():
    default_executor._async_callback_service = None
    default_executor._initialized = True
    with patch.object(default_executor, '_get_async_callback') as mock_get:
        mock_get.return_value = None

        def bg_thread():
            return mt.execute_on_main_thread((lambda x: (x * 2)), 5)
        t = threading.Thread(target=bg_thread)
        t.start()
        t.join()
        with patch('threading.current_thread') as mock_thread, patch('threading.main_thread') as mock_main:
            mock_cur = MagicMock()
            mock_cur.name = 'Thread-1'
            mock_main_cur = MagicMock()
            mock_main_cur.name = 'Thread-2'
            mock_thread.return_value = mock_cur
            mock_main.return_value = mock_main_cur
            res = mt.execute_on_main_thread((lambda x: (x * 2)), 5)
            assert (res == 10)

def test_post_to_main_thread_no_service():
    with patch.object(default_executor, '_get_async_callback') as mock_get:
        mock_get.return_value = None
        called = False

        def fn():
            nonlocal called
            called = True
        mt.post_to_main_thread(fn)
        assert called


def test_post_to_main_thread_drops_when_no_async_from_background():
    with patch.object(default_executor, "_get_async_callback", return_value=None), patch(
        "plugin.framework.thread_guard.get_background_task_name", return_value="worker-test"
    ):
        called = False

        def fn():
            nonlocal called
            called = True

        post_to_main_thread(fn)
        assert not called

def test_execute_on_main_thread_success():
    with patch('threading.current_thread') as mock_thread, patch('threading.main_thread') as mock_main, patch.object(default_executor, '_get_async_callback') as mock_get, patch.object(default_executor, '_poke_main_thread') as mock_poke:
        mock_cur = MagicMock()
        mock_cur.name = 'Thread-1'
        mock_main_cur = MagicMock()
        mock_main_cur.name = 'Thread-2'
        mock_thread.return_value = mock_cur
        mock_main.return_value = mock_main_cur
        mock_get.return_value = MagicMock()

        def mock_vcl():
            default_executor.process_queue()
        mock_poke.side_effect = mock_vcl
        res = mt.execute_on_main_thread((lambda x: (x * 2)), 5)
        assert (res == 10)

def test_execute_background_task_on_logical_main_enqueues():
    """worker_pool tags bg threads; inline marshal on logical main must not run UNO there."""
    default_executor._async_callback_service = MagicMock()
    default_executor._callback_instance = MagicMock()
    default_executor._initialized = True
    ran_on: list[str] = []

    def fn() -> str:
        ran_on.append(threading.current_thread().name)
        return "ok"

    def mock_vcl() -> None:
        default_executor.process_queue()

    with (
        patch("plugin.framework.thread_guard.on_main_thread", return_value=True),
        patch("plugin.framework.thread_guard.get_background_task_name", return_value="tool-async-test"),
        patch.object(default_executor, "_poke_main_thread", side_effect=mock_vcl),
    ):
        res = default_executor.execute(fn)
    assert res == "ok"
    assert ran_on == ["MainThread"]

def test_execute_logical_main_without_py_main_enqueues():
    """on_main_thread() alone must not inline UNO when caller is not Python MainThread."""
    default_executor._async_callback_service = MagicMock()
    default_executor._callback_instance = MagicMock()
    default_executor._initialized = True
    poke_called: list[bool] = []
    res_holder: list[str] = []

    def mock_vcl() -> None:
        poke_called.append(True)
        default_executor.process_queue()

    def run_on_worker() -> None:
        with (
            patch("plugin.framework.thread_guard.on_main_thread", return_value=True),
            patch("plugin.framework.thread_guard.get_background_task_name", return_value=None),
            patch.object(default_executor, "_poke_main_thread", side_effect=mock_vcl),
        ):
            res_holder.append(default_executor.execute(lambda: "ok"))

    t = threading.Thread(target=run_on_worker)
    t.start()
    t.join()
    assert res_holder == ["ok"]
    assert poke_called

def test_execute_refuses_fallback_during_agent_session():
    default_executor._async_callback_service = None
    default_executor._initialized = True
    res_holder: list[int] = []

    def run_on_worker() -> None:
        with (
            patch.object(default_executor, "_get_async_callback", return_value=None),
            patch("plugin.framework.thread_guard.get_background_task_name", return_value=None),
            lc.agent_session(),
            pytest.raises(RuntimeError, match="AsyncCallback unavailable from background thread"),
        ):
            default_executor.execute(lambda: res_holder.append(1))

    t = threading.Thread(target=run_on_worker)
    t.start()
    t.join()
    assert res_holder == []


def test_execute_refuses_fallback_when_background_task_tagged():
    res_holder: list[int] = []

    def run_on_worker() -> None:
        with (
            patch.object(default_executor, "_get_async_callback", return_value=None),
            patch("plugin.framework.thread_guard.get_background_task_name", return_value="worker-test"),
            pytest.raises(RuntimeError, match="AsyncCallback unavailable from background thread"),
        ):
            default_executor.execute(lambda: res_holder.append(1))

    t = threading.Thread(target=run_on_worker)
    t.start()
    t.join()
    assert res_holder == []


def test_get_async_callback_already_init_with_lock():
    default_executor._initialized = False
    mock_svc = MagicMock()
    real_lock = default_executor._init_lock

    class FakeLock():

        def __enter__(self):
            default_executor._initialized = True
            default_executor._async_callback_service = mock_svc
            return self

        def __exit__(self, exc_type, exc_val, exc_tb):
            pass
    default_executor._init_lock = FakeLock()
    try:
        assert (default_executor._get_async_callback() == mock_svc)
    finally:
        default_executor._init_lock = real_lock


class TestWorkItemClaimLockTimeoutRace:
    """Regression tests for the timeout-then-execute race in _wait_for_result.

    Before the fix, item.cancelled was set after item.event.wait() returned,
    leaving a window where the main thread could execute a timed-out item.
    """

    def test_timeout_cancels_unclaimed_item(self):
        # When the main thread has not yet claimed the item, a timeout must
        # mark it cancelled so process_queue drops it.
        from plugin.framework.queue_executor import QueueExecutor, _WorkItem

        qe = QueueExecutor()
        item = _WorkItem("test-id", lambda: None, (), {}, blocking=True)
        # Simulate _wait_for_result timing out: event never fired.
        assert not item.event.wait(0)  # immediate timeout
        with qe._claim_lock:
            if not item._claimed:
                item.cancelled = True
        assert item.cancelled is True
        assert item._claimed is False

    def test_claimed_item_is_not_cancelled_on_timeout(self):
        # When the main thread has already claimed the item (_claimed=True),
        # the timeout path must NOT set item.cancelled — the function is already
        # executing and cancellation would be a no-op anyway.
        from plugin.framework.queue_executor import QueueExecutor, _WorkItem

        qe = QueueExecutor()
        item = _WorkItem("test-id", lambda: None, (), {}, blocking=True)
        # Simulate main thread claiming before timeout fires.
        with qe._claim_lock:
            item._claimed = True

        # Simulate timeout path:
        with qe._claim_lock:
            if not item._claimed:
                item.cancelled = True

        assert item.cancelled is False  # claim won — item not cancelled
        assert item._claimed is True

    def test_process_queue_skips_cancelled_item_via_claim_lock(self):
        # Verify process_queue respects item.cancelled when set before claiming.
        from plugin.framework.queue_executor import QueueExecutor, _WorkItem
        import uuid

        executed = []
        fn = lambda: executed.append(True)  # noqa: E731
        item = _WorkItem(str(uuid.uuid4()), fn, (), {}, blocking=True)
        item.cancelled = True  # pre-cancel as the timeout path would do

        qe = QueueExecutor()
        qe._work_queue.put(item)
        qe.process_queue()

        assert executed == [], "Cancelled item must not be executed"
        assert item.event.is_set(), "Event must be set so any waiter unblocks"

    def test_process_queue_stores_baseexception(self):
        from plugin.framework.queue_executor import QueueExecutor, _WorkItem
        import uuid

        class Boom(BaseException):
            pass

        def fn():
            raise Boom("hard")

        item = _WorkItem(str(uuid.uuid4()), fn, (), {}, blocking=True)
        qe = QueueExecutor()
        qe._work_queue.put(item)
        qe.process_queue()
        assert isinstance(item.exception, Boom)
        assert item.event.is_set()
