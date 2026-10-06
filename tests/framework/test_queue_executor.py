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


def _drain_default_work_queue() -> None:
    while not default_executor._work_queue.empty():
        try:
            default_executor._work_queue.get_nowait()
        except queue.Empty:
            break


@pytest.fixture(autouse=True)
def empty_work_queue():
    # What was wrong: setup-only drain left a cancelled timeout item queued.
    # How it happened: execute()'s timeout marks the item cancelled and does not
    # dequeue it, and the mocked poke never runs process_queue. The next test
    # on this xdist worker is often another module.
    # Why this change: drain after the test too. qsize() != 0 makes
    # wait_while_pumping skip its off-main post until the 1s timeout.
    _drain_default_work_queue()
    yield
    _drain_default_work_queue()

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


def test_callable_is_scheduled_matches_fn_not_queue_depth() -> None:
    """Unrelated queued work is not this callback; the pending list counts."""
    from plugin.framework.queue_executor import QueueExecutor

    def pump() -> None:
        return None

    queued = QueueExecutor()
    queued._work_queue.put(object())
    assert queued.callable_is_scheduled(pump) is False
    queued._work_queue.put(_WorkItem("pump", pump, (), {}, blocking=False))
    assert queued.callable_is_scheduled(pump) is True

    pending = QueueExecutor()
    pending._work_queue.put(object())
    assert pending.callable_is_scheduled(pump) is False
    pending._pending_posts.append((pump, (), {}, None))
    assert pending.callable_is_scheduled(pump) is True

@pytest.fixture(autouse=True)
def reset_mt_globals():
    default_executor._ctx = None
    default_executor._initialized = False
    default_executor._async_callback_service = None
    default_executor._callback_instance = None
    default_executor._logged_missing_ctx = False
    default_executor._logged_async_callback_failure = False
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
    default_executor._logged_missing_ctx = False
    default_executor._logged_async_callback_failure = False

def test_async_callback_failure_is_not_sticky():
    """A failed createInstance must not latch marshaling off until the context changes."""
    mock_ctx = MagicMock()
    mock_smgr = MagicMock()
    mock_ctx.ServiceManager = mock_smgr
    mock_smgr.createInstanceWithContext.side_effect = RuntimeError("toolkit down")
    qe = lc.QueueExecutor(ctx=mock_ctx)
    assert qe._get_async_callback() is None
    assert qe._initialized is False

    service = MagicMock()
    mock_smgr.createInstanceWithContext.side_effect = None
    mock_smgr.createInstanceWithContext.return_value = service
    with patch.object(qe, "_make_callback_instance", return_value=MagicMock()):
        res = qe._get_async_callback()
    assert res is service
    assert qe._initialized is True


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
        again = default_executor._get_async_callback()
    assert (res is None)
    assert (again is None)
    # Missing context must not latch; the next marshal retries after set_context.
    assert (default_executor._initialized is False)
    assert (default_executor._async_callback_service is None)
    mock_warn.assert_called_once()

def test_get_async_callback_returns_none(monkeypatch):
    mock_ctx = MagicMock()
    mock_smgr = MagicMock()
    mock_ctx.ServiceManager = mock_smgr
    mock_smgr.createInstanceWithContext.return_value = None
    default_executor._ctx = mock_ctx
    with patch('plugin.framework.queue_executor.log.warning') as mock_warn:
        res = default_executor._get_async_callback()
    assert (res is None)
    assert (default_executor._initialized is False)
    mock_warn.assert_called_once()

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
        with patch('threading.current_thread') as mock_thread, patch('threading.main_thread') as mock_main:
            mock_cur = MagicMock()
            mock_cur.name = 'Thread-1'
            mock_main_cur = MagicMock()
            mock_main_cur.name = 'Thread-2'
            mock_thread.return_value = mock_cur
            mock_main.return_value = mock_main_cur
            with pytest.raises(RuntimeError, match="AsyncCallback unavailable"):
                mt.execute_on_main_thread((lambda x: (x * 2)), 5)

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


def test_post_tagged_worker_under_testing_enqueues_not_inline(monkeypatch):
    """WRITERAGENT_TESTING must not run a tagged worker's post() on that worker."""
    monkeypatch.setenv("WRITERAGENT_TESTING", "1")
    executor = mt.QueueExecutor()
    ran_on: list[str] = []

    def fn() -> None:
        ran_on.append(threading.current_thread().name)

    def run_on_worker() -> None:
        with (
            patch("plugin.framework.thread_guard.get_background_task_name", return_value="worker-test"),
            patch.object(executor, "_get_async_callback", return_value=MagicMock()),
            patch.object(executor, "_poke_main_thread", lambda: None),
        ):
            executor.post(fn)

    worker = threading.Thread(target=run_on_worker, name="bg-worker")
    worker.start()
    worker.join(timeout=2)
    assert not worker.is_alive()
    assert ran_on == []
    executor.process_queue()
    assert ran_on == ["MainThread"]


def test_post_pending_full_waits_until_a_slot_opens(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(mt, "_PENDING_POST_WAIT_SEC", 2.0)
    executor = mt.QueueExecutor()
    for _index in range(mt._PENDING_POST_CAP):
        executor._pending_posts.append((lambda: None, (), {}, None))
    errors: list[BaseException] = []
    done = threading.Event()

    def run_on_worker() -> None:
        try:
            with (
                patch("plugin.framework.thread_guard.get_background_task_name", return_value="worker-test"),
                patch.object(executor, "_get_async_callback", return_value=None),
                patch.object(executor, "_may_run_marshal_inline", return_value=False),
            ):
                executor.post(lambda: None)
        except BaseException as exc:
            errors.append(exc)
        finally:
            done.set()

    worker = threading.Thread(target=run_on_worker, name="bg-worker")
    worker.start()
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline and not done.is_set():
        with executor._pending_lock:
            if len(executor._pending_posts) >= mt._PENDING_POST_CAP:
                # Still full and the poster has not returned: it is waiting.
                break
        time.sleep(0.01)
    assert not done.is_set()
    with executor._pending_lock:
        executor._pending_posts.pop()
        executor._pending_lock.notify()
    assert done.wait(2.0)
    worker.join(timeout=1.0)
    assert errors == []
    assert len(executor._pending_posts) == mt._PENDING_POST_CAP


def test_post_pending_full_raises_when_callback_never_arrives(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(mt, "_PENDING_POST_WAIT_SEC", 5.0)
    executor = mt.QueueExecutor()
    executor._initialized = True
    executor._async_callback_service = None
    for _index in range(mt._PENDING_POST_CAP):
        executor._pending_posts.append((lambda: None, (), {}, None))
    started = time.monotonic()
    with pytest.raises(TimeoutError, match="pending list is full"):
        with (
            patch("plugin.framework.thread_guard.get_background_task_name", return_value="worker-test"),
            patch.object(executor, "_get_async_callback", return_value=None),
            patch.object(executor, "_may_run_marshal_inline", return_value=False),
        ):
            executor.post(lambda: None)
    assert time.monotonic() - started < 1.0
    assert len(executor._pending_posts) == mt._PENDING_POST_CAP


def test_post_tagged_worker_under_testing_drops_without_async(monkeypatch):
    monkeypatch.setenv("WRITERAGENT_TESTING", "1")
    executor = mt.QueueExecutor()
    called: list[int] = []

    def run_on_worker() -> None:
        with (
            patch("plugin.framework.thread_guard.get_background_task_name", return_value="worker-test"),
            patch.object(executor, "_get_async_callback", return_value=None),
        ):
            executor.post(lambda: called.append(1))

    worker = threading.Thread(target=run_on_worker, name="bg-worker")
    worker.start()
    worker.join(timeout=2)
    assert not worker.is_alive()
    assert called == []
    assert executor._work_queue.empty()


def test_post_untagged_under_testing_still_inlines(monkeypatch):
    monkeypatch.setenv("WRITERAGENT_TESTING", "1")
    executor = mt.QueueExecutor()
    called: list[str] = []
    with patch("plugin.framework.thread_guard.get_background_task_name", return_value=None):
        executor.post(lambda: called.append(threading.current_thread().name))
    assert called == ["MainThread"]
    assert executor._work_queue.empty()


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


def test_execute_refusal_log_has_no_synthetic_traceback(caplog: pytest.LogCaptureFixture) -> None:
    import logging

    def run_on_worker() -> None:
        with (
            patch.object(default_executor, "_get_async_callback", return_value=None),
            patch("plugin.framework.thread_guard.get_background_task_name", return_value="worker-test"),
            pytest.raises(RuntimeError, match="AsyncCallback unavailable from background thread"),
        ):
            default_executor.execute(lambda: None)

    with caplog.at_level(logging.ERROR, logger="writeragent.framework.queue_executor"):
        t = threading.Thread(target=run_on_worker)
        t.start()
        t.join()
    assert "AsyncCallback unavailable" in caplog.text
    assert "Traceback" not in caplog.text


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

    def test_timeout_returns_result_if_event_is_set_during_wait(self):
        # wait() can return false in the same window process_queue stores
        # the result and sets the event. The waiter must return that result.
        from plugin.framework.queue_executor import QueueExecutor, _WorkItem

        qe = QueueExecutor()
        item = _WorkItem("race", lambda: None, (), {}, blocking=True)

        def wait(timeout=None):
            item.result = "done"
            item.event.set()
            return False

        item.event.wait = wait
        assert qe._wait_for_result(item, 0.01) == "done"
        assert item.cancelled is False

    def test_claimed_timeout_waits_for_in_flight_result(self):
        # The UI thread already claimed the item. TimeoutError used to abandon
        # that call; a retry then applied the change twice.
        from plugin.framework.queue_executor import QueueExecutor, _WorkItem

        qe = QueueExecutor()
        item = _WorkItem("inflight", lambda: None, (), {}, blocking=True)
        item._claimed = True
        calls = {"n": 0}

        def wait(timeout=None):
            calls["n"] += 1
            if calls["n"] == 1:
                return False
            item.result = "late"
            return True

        item.event.wait = wait
        assert qe._wait_for_result(item, 0.01) == "late"
        assert calls["n"] == 2
        assert item.cancelled is False

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

    def test_process_queue_skips_item_scope_cancelled_after_dequeue(self):
        # Stop sets the scope flag, then drains whatever is still queued.
        # An item already removed is not marked cancelled. process_queue must
        # still skip it because the scope is cancelled.
        from plugin.framework.queue_executor import QueueExecutor, SendCancellation, SendCancelled, _WorkItem
        import uuid

        executed = []
        scope = SendCancellation()
        item = _WorkItem(str(uuid.uuid4()), lambda: executed.append(True), (), {}, blocking=True, scope=scope)
        qe = QueueExecutor()
        qe._work_queue.put(item)
        pulled = qe._work_queue.get_nowait()
        scope.bind_executor(qe)
        scope.cancel()
        qe._work_queue.put(pulled)
        qe.process_queue()

        assert executed == []
        assert item.event.is_set()
        assert isinstance(item.exception, SendCancelled)

    def test_process_queue_runs_when_scope_is_active(self):
        from plugin.framework.queue_executor import QueueExecutor, SendCancellation, _WorkItem
        import uuid

        executed = []
        scope = SendCancellation()
        item = _WorkItem(str(uuid.uuid4()), lambda: executed.append(True), (), {}, blocking=True, scope=scope)
        qe = QueueExecutor()
        qe._work_queue.put(item)
        qe.process_queue()
        assert executed == [True]

    def test_process_queue_logs_nonblocking_exception(self):
        from plugin.framework.queue_executor import QueueExecutor, _WorkItem
        from unittest.mock import patch
        import uuid

        def fn():
            raise RuntimeError("ui failed")

        item = _WorkItem(str(uuid.uuid4()), fn, (), {}, blocking=False)
        qe = QueueExecutor()
        qe._work_queue.put(item)
        with patch("plugin.framework.queue_executor.log") as mock_log:
            qe.process_queue()
        assert isinstance(item.exception, RuntimeError)
        mock_log.exception.assert_called_once()

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


class _ClaimLockProbe:
    """``with``-compatible lock that reports a thread blocked before acquire."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._mu = threading.Lock()
        self._cond = threading.Condition(self._mu)
        self.held = False
        self.blocked = 0

    def __enter__(self) -> "_ClaimLockProbe":
        with self._mu:
            self.blocked += 1
            self._cond.notify_all()
        self._lock.acquire()
        with self._mu:
            self.blocked -= 1
            self.held = True
            self._cond.notify_all()
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        with self._mu:
            self.held = False
        self._lock.release()

    def wait_blocked(self, timeout: float = 2.0) -> bool:
        with self._mu:
            return self._cond.wait_for(lambda: self.held and self.blocked >= 1, timeout)


def test_cancel_drain_excludes_concurrent_enqueue():
    """A put that overlaps cancel must wait until the drain has left ``_claim_lock``.

    ``get_nowait`` blocks on the empty queue while cancel still holds the lock.
    If ``_enqueue_work`` put without that lock, the item would land in the gap
    the drain already treated as empty.
    """
    from plugin.framework.queue_executor import QueueExecutor

    qe = QueueExecutor()
    probe = _ClaimLockProbe()
    qe._claim_lock = probe  # type: ignore[assignment]
    saw_empty = threading.Event()
    release_empty = threading.Event()
    put_count = {"n": 0}

    class GapQueue(queue.Queue):
        def get_nowait(self):  # type: ignore[no-untyped-def]
            try:
                return queue.Queue.get_nowait(self)
            except queue.Empty:
                saw_empty.set()
                if not release_empty.wait(timeout=2):
                    raise AssertionError("cancel drain was not released")
                raise

        def put(self, item, block=True, timeout=None):  # type: ignore[no-untyped-def]
            put_count["n"] += 1
            return queue.Queue.put(self, item, block, timeout)

    gap: queue.Queue = GapQueue()
    qe._work_queue = gap
    pre = _WorkItem("pre-cancel", lambda: None, (), {}, blocking=True)
    # Seed before counting puts from the racing enqueue.
    queue.Queue.put(gap, pre)
    errors: list[BaseException] = []

    def canceller() -> None:
        try:
            qe.cancel_pending_work()
        except BaseException as exc:
            errors.append(exc)

    def enqueuer() -> None:
        try:
            qe._enqueue_work(lambda: None, (), {}, blocking=False)
        except BaseException as exc:
            errors.append(exc)

    cancel_thread = threading.Thread(target=canceller)
    enqueue_thread = threading.Thread(target=enqueuer)
    cancel_thread.start()
    try:
        assert saw_empty.wait(timeout=2)
        enqueue_thread.start()
        assert probe.wait_blocked()
        assert put_count["n"] == 0
        assert gap.empty()
    finally:
        release_empty.set()
        cancel_thread.join(timeout=2)
        enqueue_thread.join(timeout=2)
    assert not cancel_thread.is_alive()
    assert not enqueue_thread.is_alive()
    assert errors == []
    assert pre.cancelled is True
    assert put_count["n"] == 1
    late = gap.get_nowait()
    assert late.cancelled is False


def test_cancel_after_claim_does_not_run():
    """scope.cancel() after the item is claimed and before fn() must not run it."""
    from plugin.framework.queue_executor import QueueExecutor, SendCancellation, _WorkItem

    qe = QueueExecutor()
    scope = SendCancellation()
    ran: list[int] = []
    item = _WorkItem("late", lambda: ran.append(1), (), {}, blocking=False, scope=scope)
    qe._work_queue.put(item)

    class _CancelOnFirstExit:
        def __init__(self, inner: object, cancel_scope: SendCancellation) -> None:
            self._inner = inner
            self._scope = cancel_scope
            self.exits = 0

        def __enter__(self) -> object:
            return self._inner.__enter__()

        def __exit__(self, exc_type: object, exc: object, tb: object) -> object:
            result = self._inner.__exit__(exc_type, exc, tb)
            self.exits += 1
            if self.exits == 1:
                self._scope.cancel()
            return result

    qe._claim_lock = _CancelOnFirstExit(qe._claim_lock, scope)  # type: ignore[assignment]
    qe.process_queue()
    assert ran == []
    assert item.cancelled is True
    assert item._claimed is False


def test_cancel_reput_pokes_kept_work():
    """Items kept for another send must be poked, or they sit until a later enqueue."""
    from plugin.framework.queue_executor import QueueExecutor, SendCancellation, _WorkItem

    qe = QueueExecutor()
    pokes: list[str] = []
    qe._poke_main_thread = lambda: pokes.append("poke")  # type: ignore[method-assign]
    keep_scope = SendCancellation()
    cancel_scope = SendCancellation()
    kept = _WorkItem("kept", lambda: None, (), {}, blocking=False, scope=keep_scope)
    doomed = _WorkItem("doomed", lambda: None, (), {}, blocking=False, scope=cancel_scope)
    qe._work_queue.put(kept)
    qe._work_queue.put(doomed)
    qe.cancel_pending_work(cancel_scope)
    assert pokes == ["poke"]
    assert qe._work_queue.get_nowait() is kept
    assert doomed.cancelled is True


def test_cancel_drops_pending_posts_for_that_scope_only():
    from plugin.framework.queue_executor import QueueExecutor, SendCancellation

    qe = QueueExecutor()
    scope = SendCancellation()
    other = SendCancellation()
    qe._pending_posts.append((lambda: None, (), {}, scope))
    qe._pending_posts.append((lambda: None, (), {}, other))
    qe.cancel_pending_work(scope)
    assert len(qe._pending_posts) == 1
    assert qe._pending_posts[0][3] is other


def test_flush_pending_posts_keeps_original_scope():
    from plugin.framework.queue_executor import QueueExecutor, SendCancellation

    qe = QueueExecutor()
    scope = SendCancellation()
    seen: list[object] = []

    def capture(fn, args, kwargs, blocking=True, *, bound_scope=None, **_kw):  # type: ignore[no-untyped-def]
        seen.append(bound_scope)
        return None

    qe._enqueue_work = capture  # type: ignore[method-assign]
    qe._initialized = True
    qe._async_callback_service = object()
    qe._pending_posts.append((lambda: None, (), {}, scope))
    qe._flush_pending_posts()
    assert seen == [scope]

def test_execute_accepts_and_passes_bound_scope() -> None:
    executor = mt.QueueExecutor()
    scope = mt.SendCancellation()

    # We must mock _enqueue_work to see if it received bound_scope.
    # But execute is blocking, so we need _wait_for_result to return immediately.
    # We'll just patch _wait_for_result and _enqueue_work, or mock _enqueue_work
    # to return a dummy item and check what was passed.

    with patch.object(executor, '_should_run_inline', return_value=False), \
         patch.object(executor, '_is_logical_main_thread', return_value=False), \
         patch.object(executor, '_get_async_callback', return_value=MagicMock()), \
         patch('plugin.framework.queue_executor._force_marshal_mode', True), \
         patch.object(executor, '_wait_for_result', return_value="dummy_result"), \
         patch.object(executor, '_enqueue_work') as mock_enqueue:

         # Mock enqueue to return a minimal WorkItem
         mock_item = mt._WorkItem("id", lambda: None, (), {}, True, scope)
         mock_enqueue.return_value = mock_item

         # Note: _force_marshal_mode=True guarantees we don't return inline
         def dummy_fn(): pass

         executor.execute(dummy_fn, 1, 2, bound_scope=scope, timeout=5.0)

         mock_enqueue.assert_called_once()
         assert mock_enqueue.call_args.kwargs.get("bound_scope") is scope
         assert mock_enqueue.call_args.kwargs.get("blocking") is True
         assert mock_enqueue.call_args.args[0] is dummy_fn
         assert mock_enqueue.call_args.args[1] == (1, 2)

def test_flush_pending_posts_order_concurrent_enqueue():
    """A direct enqueue overlapping with flush must not jump ahead of pending posts."""
    from plugin.framework.queue_executor import QueueExecutor
    import threading
    import time

    qe = QueueExecutor()
    qe._initialized = True
    qe._async_callback_service = object()  # fake callback service

    # Pre-populate pending posts
    qe._pending_posts.append((lambda: "pending1", (), {}, None))
    qe._pending_posts.append((lambda: "pending2", (), {}, None))

    enqueued_items = []

    def slow_put(items):
        time.sleep(0.05)
        enqueued_items.extend(items)

    qe._put_work_items = slow_put

    def direct_enqueue():
        time.sleep(0.01) # let flush start
        qe._enqueue_work(lambda: "direct", (), {}, blocking=False)

    t = threading.Thread(target=direct_enqueue)
    t.start()

    qe._flush_pending_posts()
    t.join()

    assert len(enqueued_items) == 3
    assert enqueued_items[0].fn() == "pending1"
    assert enqueued_items[1].fn() == "pending2"
    assert enqueued_items[2].fn() == "direct"

def test_set_context_wakes_pending_posts():
    """set_context must create AsyncCallback if there are pending posts."""
    from plugin.framework.queue_executor import QueueExecutor
    from unittest.mock import patch, MagicMock
    from plugin.framework.thread_guard import _unwrap_uno

    qe = QueueExecutor()
    # Add a pending post
    qe._pending_posts.append((lambda: "pending", (), {}, None))

    mock_ctx = MagicMock()
    mock_smgr = MagicMock()
    mock_callback_service = MagicMock()

    mock_smgr.createInstanceWithContext.return_value = mock_callback_service

    with patch("plugin.framework.uno_context.get_service_manager", return_value=mock_smgr), \
         patch.object(qe, "_make_callback_instance", return_value=MagicMock()), \
         patch.object(qe, "_enqueue_work") as mock_enqueue:

        qe.set_context(mock_ctx)

        # Verify that AsyncCallback was created
        mock_smgr.createInstanceWithContext.assert_called_once_with("com.sun.star.awt.AsyncCallback", _unwrap_uno(mock_ctx))
        # Verify that _enqueue_work was called
        mock_enqueue.assert_called_once()
        assert mock_enqueue.call_args.args[0]() == "pending"


def test_callable_is_scheduled_ignores_cancelled():
    """callable_is_scheduled must return False if the scheduled item is cancelled."""
    from plugin.framework.queue_executor import QueueExecutor, _WorkItem
    import uuid

    qe = QueueExecutor()
    def my_fn(): pass

    item = _WorkItem(str(uuid.uuid4()), my_fn, (), {}, blocking=False)
    qe._work_queue.put(item)

    assert qe.callable_is_scheduled(my_fn) is True

    item.cancelled = True
    assert qe.callable_is_scheduled(my_fn) is False


def test_process_queue_claim_gap_raises():
    """An exception between claim and fn() must not leave the waiter blocked forever."""
    from plugin.framework.queue_executor import QueueExecutor, _WorkItem
    from unittest.mock import patch
    import uuid

    qe = QueueExecutor()
    def my_fn(): pass

    item = _WorkItem(str(uuid.uuid4()), my_fn, (), {}, blocking=True)
    qe._work_queue.put(item)

    with patch("plugin.framework.queue_executor._fn_label", side_effect=ValueError("label error")):
        qe.process_queue()

    assert item.event.is_set(), "waiter is blocked forever"
    assert isinstance(item.exception, ValueError)


def test_wait_for_result_shutting_down_raises():
    """_wait_for_result must raise RuntimeError if sys.is_finalizing() is true while waiting."""
    from plugin.framework.queue_executor import QueueExecutor, _WorkItem
    from unittest.mock import patch

    qe = QueueExecutor()
    item = _WorkItem("id1", lambda: None, (), {}, blocking=True)
    item._claimed = True # Simulate that process_queue claimed it

    # Initially, wait for timeout fails, entering keep_waiting.
    # Then item.event.wait(5.0) returns False, and sys.is_finalizing() is true.
    with patch("sys.is_finalizing", return_value=True), \
         patch("plugin.framework.queue_executor._UNTIMED_WAIT_SLICE_SEC", 0.01):
        try:
            qe._wait_for_result(item, timeout=0.01)
            assert False, "Should have raised RuntimeError"
        except RuntimeError as e:
            assert "outcome unknown" in str(e)


def test_wait_for_result_claimed_item_keeps_waiting_past_slices():
    """A claimed item is waited out across slices; the result is returned, not a timeout."""
    from plugin.framework.queue_executor import QueueExecutor, _WorkItem
    from unittest.mock import patch

    qe = QueueExecutor()
    item = _WorkItem("id2", lambda: None, (), {}, blocking=True)
    item._claimed = True

    def finish_later():
        time.sleep(0.2)
        item.result = "done"
        item.event.set()

    t = threading.Thread(target=finish_later)
    with patch("plugin.framework.queue_executor._UNTIMED_WAIT_SLICE_SEC", 0.01):
        t.start()
        assert qe._wait_for_result(item, timeout=0.01) == "done"
    t.join()


def test_llm_request_lane_restores_status_after_waiting():
    from plugin.framework.queue_executor import llm_request_lane, _LLM_REQUEST_LOCK
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
    from plugin.framework.queue_executor import llm_request_lane, _LLM_REQUEST_LOCK

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
    from plugin.framework.async_stream import BlockingWaitStopped
    from plugin.framework.queue_executor import llm_request_lane, _LLM_REQUEST_LOCK

    # Block the lane
    _LLM_REQUEST_LOCK.acquire()
    try:
        class FakeCancellation:
            def is_cancelled(self):
                return True
        from unittest.mock import patch
        with patch('plugin.framework.queue_executor.get_current_send_cancellation', return_value=FakeCancellation()):
            try:
                with llm_request_lane(timeout=1.0):
                    pass
                assert False, "Should have raised BlockingWaitStopped"
            except BlockingWaitStopped:
                pass
    finally:
        _LLM_REQUEST_LOCK.release()
