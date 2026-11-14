from __future__ import annotations

from collections.abc import Callable
from typing import Any
from tests.harness.strip_bundle import is_release_build, requires_asserts
import threading
import time
import plugin.framework.queue_executor as lc
import plugin.framework.send_cancellation as sc
import queue
import pytest
import plugin.framework.queue_executor as mt

from unittest.mock import patch, MagicMock
from plugin.framework.worker_pool import run_in_background
from plugin.framework.queue_executor import _WorkItem, main

def test_agent_session_marks_active_with_nesting() -> None:
    assert (sc.is_agent_active() is False)
    with sc.agent_session():
        assert (sc.is_agent_active() is True)
        with sc.agent_session():
            assert (sc.is_agent_active() is True)
        assert (sc.is_agent_active() is True)
    assert (sc.is_agent_active() is False)


@pytest.fixture(autouse=True)
def _reset_main_state() -> Any:
    orig_ctx = main._ctx
    orig_init = main._initialized
    orig_service = main._async_callback_service
    orig_cb = main._callback_instance
    orig_logged = main._logged_callback_problem
    main._ctx = None
    main._initialized = False
    main._async_callback_service = None
    main._callback_instance = None
    main._logged_callback_problem = False
    with main._init_lock:
        pass
    _drain_default_work_queue()
    try:
        yield
    finally:
        main._ctx = orig_ctx
        main._initialized = orig_init
        main._async_callback_service = orig_service
        main._callback_instance = orig_cb
        main._logged_callback_problem = orig_logged


def _drain_default_work_queue() -> None:
    while not main._work_queue.empty():
        try:
            main._work_queue.get_nowait()
        except queue.Empty:
            break
    # A post with no AsyncCallback waits in _pending_posts; the next test's post() would flush it.
    with main._pending_lock:
        main._pending_posts.clear()
        main._pending_lock.notify_all()
    import plugin.framework.vcl_pumping as vp
    from plugin.framework.vcl_pumping import _secondary_idle_lock

    with _secondary_idle_lock:
        vp._secondary_idle_in_flight = False


@pytest.fixture(autouse=True)
def empty_work_queue():
    # Drain after the test too: execute()'s timeout marks items cancelled without
    # dequeuing them, and mocked pokes may not run process_queue before the next test.
    _drain_default_work_queue()
    yield
    _drain_default_work_queue()

def test_work_item():

    def func(x):
        return (x * 2)
    item = _WorkItem(func, (5,), {})
    assert (item.fn is func)
    assert (item.args == (5,))
    assert (not item.event.is_set())

def test_call_direct():

    def func():
        return 42
    assert (threading.current_thread() is threading.main_thread())
    res = main.call(func)
    assert (res == 42)

@patch.object(mt.MainThread, '_get_async_callback')
@patch.object(mt.MainThread, '_poke_main_thread')
def test_call_background(mock_poke, mock_get_async):
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
        main.process_queue()
    mock_poke.side_effect = mock_poke_main_thread
    results = {}
    exceptions = {}

    def bg_thread(val):
        try:
            res = main.call(func_to_run, val)
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

@patch.object(mt.MainThread, '_get_async_callback')
@patch.object(mt.MainThread, '_poke_main_thread')
def test_call_timeout(mock_poke, mock_get_async):
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
            main.call(slow_func, timeout=0.1)
        except Exception as e:
            exc_caught = e
    t = run_in_background(bg_thread, daemon=False)
    t.join(timeout=1.0)
    assert isinstance(exc_caught, TimeoutError)
    assert ('slow_func' in str(exc_caught))
    assert ('timed out after 0.1s' in str(exc_caught))

@patch.object(mt.MainThread, '_get_async_callback')
@patch.object(mt.MainThread, '_poke_main_thread')
def test_post_fire_and_forget(mock_poke, mock_get_async):
    '''
    Test for main.post() that ensures it enqueues the work item
    without blocking (and still calls _poke_main_thread()).
    '''
    mock_get_async.return_value = MagicMock()

    def my_func():
        pass
    main.post(my_func)
    item = main._work_queue.get_nowait()
    assert (item.fn is my_func)
    mock_poke.assert_called_once()


def test_async_callback_failure_is_not_sticky():
    """A failed createInstance must not latch marshaling off until the context changes."""
    mock_ctx = MagicMock()
    mock_smgr = MagicMock()
    mock_ctx.ServiceManager = mock_smgr
    mock_smgr.createInstanceWithContext.side_effect = RuntimeError("toolkit down")
    qe = lc.MainThread(ctx=mock_ctx)
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
    main._ctx = mock_ctx
    with patch.object(main, '_make_callback_instance') as mock_make:
        mock_instance = MagicMock()
        mock_make.return_value = mock_instance
        res = main._get_async_callback()
    assert (res == mock_service)
    assert (main._initialized)
    assert (main._async_callback_service == mock_service)
    assert (main._callback_instance == mock_instance)

def test_get_async_callback_with_explicit_ctx():
    mock_ctx = MagicMock()
    mock_smgr = MagicMock()
    mock_ctx.ServiceManager = mock_smgr
    mock_service = MagicMock()
    mock_smgr.createInstanceWithContext.return_value = mock_service

    qe = lc.MainThread(ctx=mock_ctx)
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

    qe = lc.MainThread(ctx=mock_ctx)
    with patch.object(qe, '_make_callback_instance') as mock_make:
        mock_instance = MagicMock()
        mock_make.return_value = mock_instance
        res = qe._get_async_callback()

    assert res == mock_service
    assert mock_uno.getComponentContext.call_count == 0

def test_get_async_callback_unwraps_layer_a_proxy():
    """Creating AsyncCallback from a worker must not getattr() a guard proxy.

    Without unwrapping, getattr on a guard proxy fires Layer A while
    ``_init_lock`` is held and startup deadlocks (UI ``set_context`` vs nested
    ``main.call``).
    """
    import plugin.framework.thread_guard as tg

    raw_ctx = MagicMock()
    raw_smgr = MagicMock()
    raw_ctx.ServiceManager = raw_smgr
    raw_svc = MagicMock()
    raw_smgr.createInstanceWithContext.return_value = raw_svc
    qe = lc.MainThread(ctx=tg._UnoThreadGuardProxy(raw_ctx))
    assert qe._ctx is raw_ctx
    with patch.object(qe, "_make_callback_instance", return_value=MagicMock()):
        res = qe._get_async_callback()
    assert res is raw_svc
    raw_smgr.createInstanceWithContext.assert_called_once_with("com.sun.star.awt.AsyncCallback", raw_ctx)


def test_set_context_unwraps_and_dedupes_proxies():
    import plugin.framework.thread_guard as tg

    raw = MagicMock()
    qe = lc.MainThread(ctx=raw)
    qe._initialized = True
    qe.set_context(tg._UnoThreadGuardProxy(raw))
    assert qe._ctx is raw
    assert qe._initialized is True

def test_queue_executor_set_context_invalidates():
    mock_ctx1 = MagicMock()
    mock_ctx2 = MagicMock()
    qe = lc.MainThread(ctx=mock_ctx1)
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

def test_init_config_sets_main_context(monkeypatch, tmp_path):
    from plugin.framework.config import init_config, reset_config_for_tests
    reset_config_for_tests()
    mock_ctx = MagicMock()
    mock_cfg = str(tmp_path / "mock_config.json")
    monkeypatch.setattr("plugin.framework.config._resolve_config_path_from_ctx", lambda _c: mock_cfg)

    init_config(mock_ctx)
    assert main._ctx is mock_ctx
    reset_config_for_tests()


def test_get_async_callback_already_init():
    main._initialized = True
    mock_svc = MagicMock()
    main._async_callback_service = mock_svc
    assert (main._get_async_callback() == mock_svc)

@pytest.mark.parametrize("mode", ["ctx_raises", "service_none"])
def test_get_async_callback_unavailable(monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    if mode == "ctx_raises":
        import sys
        mock_uno = MagicMock()
        mock_uno.getComponentContext.side_effect = Exception("No UNO")
        monkeypatch.setitem(sys.modules, "uno", mock_uno)
    else:
        mock_ctx = MagicMock()
        mock_smgr = MagicMock()
        mock_ctx.ServiceManager = mock_smgr
        mock_smgr.createInstanceWithContext.return_value = None
        main._ctx = mock_ctx

    with patch("plugin.framework.queue_executor.log.warning") as mock_warn:
        res = main._get_async_callback()
        again = main._get_async_callback() if mode == "ctx_raises" else None
    assert res is None
    assert again is None
    # Missing context or unavailable service must not latch; the next marshal retries.
    assert main._initialized is False
    assert main._async_callback_service is None
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
        instance = main._make_callback_instance()
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
        instance = main._make_callback_instance()
        instance.notify(None)

        def dummy_fn(x):
            return (x * 2)
        item = _WorkItem(dummy_fn, (10,), {})
        main._work_queue.put(item)
        with patch.object(main, '_poke_main_thread') as mock_poke:
            instance.notify(None)
            assert (item.result == 20)
            assert (item.exception is None)
            assert item.event.is_set()
            mock_poke.assert_not_called()

        def dummy_fn_exc():
            raise ValueError('test error')
        item2 = _WorkItem(dummy_fn_exc, (), {})
        main._work_queue.put(item2)
        item3 = _WorkItem((lambda : 1), (), {})
        main._work_queue.put(item3)
        with patch.object(main, '_poke_main_thread') as mock_poke:
            instance.notify(None)
            assert (item2.result is None)
            assert isinstance(item2.exception, ValueError)
            assert item2.event.is_set()
            mock_poke.assert_called_once()
        main._work_queue.get_nowait()

def test_poke_vcl():
    main._async_callback_service = MagicMock()
    main._callback_instance = MagicMock()
    main._poke_main_thread()
    main._async_callback_service.addCallback.assert_called_with(main._callback_instance, None)
    main._async_callback_service.addCallback.reset_mock()
    main._async_callback_service.addCallback.side_effect = Exception('error')
    with patch('plugin.framework.queue_executor.log.log') as mock_warn:
        main._poke_main_thread()
        mock_warn.assert_called_once()
    main._async_callback_service.addCallback.assert_called_once_with(main._callback_instance, None)
    main._async_callback_service = None
    main._poke_main_thread()

def test_call_no_service():
    main._async_callback_service = None
    main._initialized = True
    with patch.object(main, '_get_async_callback') as mock_get:
        mock_get.return_value = None
        with patch('threading.current_thread') as mock_thread, patch('threading.main_thread') as mock_main:
            mock_cur = MagicMock()
            mock_cur.name = 'Thread-1'
            mock_main_cur = MagicMock()
            mock_main_cur.name = 'Thread-2'
            mock_thread.return_value = mock_cur
            mock_main.return_value = mock_main_cur
            with pytest.raises(RuntimeError, match="AsyncCallback unavailable"):
                mt.main.call((lambda x: (x * 2)), 5)

def test_post_no_service():
    with patch.object(main, '_get_async_callback') as mock_get:
        mock_get.return_value = None
        called = False

        def fn():
            nonlocal called
            called = True
        mt.main.post(fn)
        assert called


def test_post_drops_when_no_async_from_background():
    """A tagged worker must not run the callable when AsyncCallback is missing.

    The post is stored on that executor's pending list. It must not be stored
    on the process-wide main. post() flushes those rows once
    marshal is ready, and another test on the same xdist worker counted them.
    """
    executor = mt.MainThread()
    before = len(main._pending_posts)
    called = False

    def fn():
        nonlocal called
        called = True

    with patch.object(executor, "_get_async_callback", return_value=None), patch(
        "plugin.framework.thread_guard.get_background_task_name", return_value="worker-test"
    ):
        executor.post(fn)
    assert not called
    assert len(executor._pending_posts) == 1
    assert executor._pending_posts[0][0] is fn
    assert len(main._pending_posts) == before


def test_post_tagged_worker_under_testing_enqueues_not_inline():
    """WRITERAGENT_TESTING must not run a tagged worker's post() on that worker."""
    mt.MainThread.install(mt.Policy.test_inline)
    executor = mt.MainThread()
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
    executor = mt.MainThread()
    for _index in range(mt._PENDING_POST_CAP):
        executor._pending_posts.append((lambda: None, (), {}, None))
    errors: list[BaseException] = []
    done = threading.Event()

    def run_on_worker() -> None:
        try:
            with (
                patch("plugin.framework.thread_guard.get_background_task_name", return_value="worker-test"),
                patch.object(executor, "_get_async_callback", return_value=None),
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
    executor = mt.MainThread()
    executor._initialized = True
    executor._async_callback_service = None
    for _index in range(mt._PENDING_POST_CAP):
        executor._pending_posts.append((lambda: None, (), {}, None))
    started = time.monotonic()
    with pytest.raises(TimeoutError, match="pending list is full"):
        with (
            patch("plugin.framework.thread_guard.get_background_task_name", return_value="worker-test"),
            patch.object(executor, "_get_async_callback", return_value=None),
        ):
            executor.post(lambda: None)
    assert time.monotonic() - started < 1.0
    assert len(executor._pending_posts) == mt._PENDING_POST_CAP


def test_post_tagged_worker_under_testing_drops_without_async():
    mt.MainThread.install(mt.Policy.test_inline)
    executor = mt.MainThread()
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


def test_post_untagged_under_testing_still_inlines():
    mt.MainThread.install(mt.Policy.test_inline)
    executor = mt.MainThread()
    called: list[str] = []
    with patch("plugin.framework.thread_guard.get_background_task_name", return_value=None):
        executor.post(lambda: called.append(threading.current_thread().name))
    assert called == ["MainThread"]
    assert executor._work_queue.empty()


def test_call_untagged_worker_under_test_inline_inlines():
    """Policy.test_inline allows untagged background workers to inline call() without AsyncCallback."""
    mt.MainThread.install(mt.Policy.test_inline)
    executor = mt.MainThread()
    result: list[str] = []

    def run_on_worker() -> None:
        with patch("plugin.framework.thread_guard.get_background_task_name", return_value=None):
            val = executor.call(lambda: f"done on {threading.current_thread().name}")
            result.append(val)

    worker = threading.Thread(target=run_on_worker, name="untagged-worker")
    worker.start()
    worker.join(timeout=2)
    assert not worker.is_alive()
    assert result == ["done on untagged-worker"]


def test_call_tagged_worker_under_test_inline_refuses_inline():
    """Policy.test_inline refuses to inline call() for tagged background workers."""
    mt.MainThread.install(mt.Policy.test_inline)
    executor = mt.MainThread()
    errors: list[BaseException] = []

    def run_on_worker() -> None:
        with (
            patch("plugin.framework.thread_guard.get_background_task_name", return_value="bg-task"),
            patch.object(executor, "_get_async_callback", return_value=None),
        ):
            try:
                executor.call(lambda: "should-fail")
            except RuntimeError as exc:
                errors.append(exc)

    worker = threading.Thread(target=run_on_worker, name="tagged-worker")
    worker.start()
    worker.join(timeout=2)
    assert not worker.is_alive()
    assert len(errors) == 1
    assert "AsyncCallback unavailable from background thread" in str(errors[0])


def test_call_success():
    with patch('threading.current_thread') as mock_thread, patch('threading.main_thread') as mock_main, patch.object(main, '_get_async_callback') as mock_get, patch.object(main, '_poke_main_thread') as mock_poke:
        mock_cur = MagicMock()
        mock_cur.name = 'Thread-1'
        mock_main_cur = MagicMock()
        mock_main_cur.name = 'Thread-2'
        mock_thread.return_value = mock_cur
        mock_main.return_value = mock_main_cur
        mock_get.return_value = MagicMock()

        def mock_vcl():
            main.process_queue()
        mock_poke.side_effect = mock_vcl
        res = mt.main.call((lambda x: (x * 2)), 5)
        assert (res == 10)

def test_execute_background_task_on_logical_main_enqueues():
    """worker_pool tags bg threads; inline marshal on logical main must not run UNO there."""
    main._async_callback_service = MagicMock()
    main._callback_instance = MagicMock()
    main._initialized = True
    ran_on: list[str] = []

    def fn() -> str:
        ran_on.append(threading.current_thread().name)
        return "ok"

    def mock_vcl() -> None:
        main.process_queue()

    with (
        patch.object(main, "is_current", return_value=True),
        patch("plugin.framework.thread_guard.get_background_task_name", return_value="tool-async-test"),
        patch.object(main, "_poke_main_thread", side_effect=mock_vcl),
    ):
        res = main.call(fn)
    assert res == "ok"
    assert ran_on == ["MainThread"]

def test_execute_logical_main_without_py_main_enqueues():
    """main.is_current() alone must not inline UNO when caller is not Python MainThread."""
    main._async_callback_service = MagicMock()
    main._callback_instance = MagicMock()
    main._initialized = True
    poke_called: list[bool] = []
    res_holder: list[str] = []

    def mock_vcl() -> None:
        poke_called.append(True)
        main.process_queue()

    def run_on_worker() -> None:
        with (
            patch.object(main, "is_current", return_value=True),
            patch("plugin.framework.thread_guard.get_background_task_name", return_value=None),
            patch.object(main, "_poke_main_thread", side_effect=mock_vcl),
        ):
            res_holder.append(main.call(lambda: "ok"))

    t = threading.Thread(target=run_on_worker)
    t.start()
    t.join()
    assert res_holder == ["ok"]
    assert poke_called

@pytest.mark.parametrize("reason", ["agent_session", "background_task"])
def test_execute_refuses_fallback_from_background(reason: str) -> None:
    if reason == "agent_session":
        main._async_callback_service = None
        main._initialized = True
    res_holder: list[int] = []
    worker_exc: list[BaseException] = []

    def run_on_worker() -> None:
        try:
            task_name = None if reason == "agent_session" else "worker-test"
            with (
                patch.object(main, "_get_async_callback", return_value=None),
                patch("plugin.framework.thread_guard.get_background_task_name", return_value=task_name),
            ):
                if reason == "agent_session":
                    with sc.agent_session(), pytest.raises(
                        RuntimeError, match="AsyncCallback unavailable from background thread"
                    ):
                        main.call(lambda: res_holder.append(1))
                else:
                    with pytest.raises(
                        RuntimeError, match="AsyncCallback unavailable from background thread"
                    ):
                        main.call(lambda: res_holder.append(1))
        except BaseException as exc:
            worker_exc.append(exc)

    t = threading.Thread(target=run_on_worker)
    t.start()
    t.join()
    if worker_exc:
        raise worker_exc[0]
    assert res_holder == []


def test_execute_refusal_log_has_no_synthetic_traceback(caplog: pytest.LogCaptureFixture) -> None:
    import logging

    def run_on_worker() -> None:
        with (
            patch.object(main, "_get_async_callback", return_value=None),
            patch("plugin.framework.thread_guard.get_background_task_name", return_value="worker-test"),
            pytest.raises(RuntimeError, match="AsyncCallback unavailable from background thread"),
        ):
            main.call(lambda: None)

    with caplog.at_level(logging.ERROR, logger="writeragent.framework.queue_executor"):
        t = threading.Thread(target=run_on_worker)
        t.start()
        t.join()
    assert any(r.levelno == logging.ERROR for r in caplog.records)
    assert "Traceback" not in caplog.text


def test_get_async_callback_already_init_with_lock():
    main._initialized = False
    mock_svc = MagicMock()
    real_lock = main._init_lock

    class FakeLock():

        def __enter__(self):
            main._initialized = True
            main._async_callback_service = mock_svc
            return self

        def __exit__(self, exc_type, exc_val, exc_tb):
            pass
    main._init_lock = FakeLock()
    try:
        assert (main._get_async_callback() == mock_svc)
    finally:
        main._init_lock = real_lock


class TestWorkItemClaimLockTimeoutRace:
    """Regression tests for the timeout-then-execute race in _wait_for_result.

    Before the fix, item.cancelled was set after item.event.wait() returned,
    leaving a window where the main thread could execute a timed-out item.
    """

    def test_timeout_cancels_unclaimed_item(self):
        # When the main thread has not yet claimed the item, a timeout must
        # mark it cancelled so process_queue drops it.
        from plugin.framework.queue_executor import MainThread, _WorkItem

        qe = MainThread()
        item = _WorkItem(lambda: None, (), {}, blocking=True)
        item.event.wait = lambda timeout=None: False
        with pytest.raises(TimeoutError):
            qe._wait_for_result(item, 0.01)
        assert item.cancelled is True
        assert item._claimed is False

    def test_timeout_returns_result_if_event_is_set_during_wait(self):
        # wait() can return false in the same window process_queue stores
        # the result and sets the event. The waiter must return that result.
        from plugin.framework.queue_executor import MainThread, _WorkItem

        qe = MainThread()
        item = _WorkItem(lambda: None, (), {}, blocking=True)

        def wait(timeout=None):
            item.result = "done"
            item.event.set()
            return False

        item.event.wait = wait
        assert qe._wait_for_result(item, 0.01) == "done"
        assert item.cancelled is False

    def test_claimed_timeout_waits_for_in_flight_result(self):
        # The UI thread already claimed the item. A TimeoutError must keep
        # waiting for that in-flight result; abandoning it lets a retry apply
        # the change twice.
        from plugin.framework.queue_executor import MainThread, _WorkItem

        qe = MainThread()
        item = _WorkItem(lambda: None, (), {}, blocking=True)
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
        from plugin.framework.queue_executor import MainThread, _WorkItem

        executed = []
        fn = lambda: executed.append(True)  # noqa: E731
        item = _WorkItem(fn, (), {}, blocking=True)
        item.cancelled = True  # pre-cancel as the timeout path would do

        qe = MainThread()
        qe._work_queue.put(item)
        qe.process_queue()

        assert executed == [], "Cancelled item must not be executed"
        assert item.event.is_set(), "Event must be set so any waiter unblocks"

    def test_process_queue_skips_item_scope_cancelled_after_dequeue(self):
        # Stop sets the scope flag, then drains whatever is still queued.
        # An item already removed is not marked cancelled. process_queue must
        # still skip it because the scope is cancelled.
        from plugin.framework.queue_executor import MainThread, _WorkItem
        from plugin.framework.send_cancellation import SendCancellation, SendCancelled

        executed = []
        scope = SendCancellation()
        item = _WorkItem(lambda: executed.append(True), (), {}, blocking=True, scope=scope)
        qe = MainThread()
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
        from plugin.framework.queue_executor import MainThread, _WorkItem
        from plugin.framework.send_cancellation import SendCancellation

        executed = []
        scope = SendCancellation()
        item = _WorkItem(lambda: executed.append(True), (), {}, blocking=True, scope=scope)
        qe = MainThread()
        qe._work_queue.put(item)
        qe.process_queue()
        assert executed == [True]

    def test_process_queue_logs_nonblocking_exception(self):
        from plugin.framework.queue_executor import MainThread, _WorkItem
        from unittest.mock import patch

        def fn():
            raise RuntimeError("ui failed")

        item = _WorkItem(fn, (), {}, blocking=False)
        qe = MainThread()
        qe._work_queue.put(item)
        with patch("plugin.framework.queue_executor.log") as mock_log:
            qe.process_queue()
        assert isinstance(item.exception, RuntimeError)
        mock_log.exception.assert_called_once()

    def test_process_queue_stores_baseexception(self):
        from plugin.framework.queue_executor import MainThread, _WorkItem

        class Boom(BaseException):
            pass

        def fn():
            raise Boom("hard")

        item = _WorkItem(fn, (), {}, blocking=True)
        qe = MainThread()
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
    """A put that overlaps cancel must wait until the drain has left ``_order_lock``.

    ``get_nowait`` blocks on the empty queue while cancel still holds
    ``_order_lock`` then ``_claim_lock``. ``_enqueue_work`` takes that same
    order, so the racer blocks on ``_order_lock`` and cannot land in the gap
    the drain already treated as empty.
    """
    from plugin.framework.queue_executor import MainThread

    qe = MainThread()
    probe = _ClaimLockProbe()
    qe._order_lock = probe  # type: ignore[assignment]
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
    pre = _WorkItem(lambda: None, (), {}, blocking=True)
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
    from plugin.framework.queue_executor import MainThread, _WorkItem
    from plugin.framework.send_cancellation import SendCancellation

    qe = MainThread()
    scope = SendCancellation()
    ran: list[int] = []
    item = _WorkItem(lambda: ran.append(1), (), {}, blocking=False, scope=scope)
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
    from plugin.framework.queue_executor import MainThread, _WorkItem
    from plugin.framework.send_cancellation import SendCancellation

    qe = MainThread()
    pokes: list[str] = []
    qe._poke_main_thread = lambda: pokes.append("poke")  # type: ignore[method-assign]
    keep_scope = SendCancellation()
    cancel_scope = SendCancellation()
    kept = _WorkItem(lambda: None, (), {}, blocking=False, scope=keep_scope)
    unscoped = _WorkItem(lambda: None, (), {}, blocking=False, scope=None)
    doomed = _WorkItem(lambda: None, (), {}, blocking=False, scope=cancel_scope)
    qe._work_queue.put(kept)
    qe._work_queue.put(unscoped)
    qe._work_queue.put(doomed)
    qe.cancel_pending_work(cancel_scope)
    assert pokes == ["poke"]
    assert qe._work_queue.get_nowait() is kept
    assert qe._work_queue.get_nowait() is unscoped
    assert doomed.cancelled is True


def test_scoped_cancel_requeues_survivors_ahead_of_concurrent_enqueue():
    """A put that arrives during a scoped drain stays behind the survivors.

    If cancel drops ``_claim_lock`` and re-queues survivors afterwards, a
    concurrent enqueue can land ahead of them. The late enqueue uses
    ``poke=False`` so the single poke is the one cancel issues after both
    locks are released.
    """
    from plugin.framework.queue_executor import MainThread, _WorkItem
    from plugin.framework.send_cancellation import SendCancellation

    qe = MainThread()
    probe = _ClaimLockProbe()
    qe._order_lock = probe  # type: ignore[assignment]
    keep_scope = SendCancellation()
    cancel_scope = SendCancellation()
    saw_empty = threading.Event()
    release_empty = threading.Event()
    pokes: list[str] = []
    qe._poke_main_thread = lambda: pokes.append("poke")  # type: ignore[method-assign]

    class GapQueue(queue.Queue):
        def get_nowait(self):  # type: ignore[no-untyped-def]
            try:
                return queue.Queue.get_nowait(self)
            except queue.Empty:
                saw_empty.set()
                if not release_empty.wait(timeout=2):
                    raise AssertionError("cancel drain was not released")
                raise

    gap: queue.Queue = GapQueue()
    qe._work_queue = gap
    survivor = _WorkItem(lambda: None, (), {}, blocking=False, scope=keep_scope)
    doomed = _WorkItem(lambda: None, (), {}, blocking=False, scope=cancel_scope)
    queue.Queue.put(gap, survivor)
    queue.Queue.put(gap, doomed)
    errors: list[BaseException] = []

    def canceller() -> None:
        try:
            qe.cancel_pending_work(cancel_scope)
        except BaseException as exc:
            errors.append(exc)

    def enqueuer() -> None:
        try:
            qe._enqueue_work(lambda: "late", (), {}, blocking=False, poke=False)
        except BaseException as exc:
            errors.append(exc)

    cancel_thread = threading.Thread(target=canceller)
    enqueue_thread = threading.Thread(target=enqueuer)
    cancel_thread.start()
    try:
        assert saw_empty.wait(timeout=2)
        enqueue_thread.start()
        assert probe.wait_blocked()
        assert gap.empty()
    finally:
        release_empty.set()
        cancel_thread.join(timeout=2)
        enqueue_thread.join(timeout=2)
    assert not cancel_thread.is_alive()
    assert not enqueue_thread.is_alive()
    assert errors == []
    assert doomed.cancelled is True
    assert gap.get_nowait() is survivor
    assert gap.get_nowait().fn() == "late"
    assert pokes == ["poke"]


def test_cancel_drops_pending_posts_for_that_scope_only():
    from plugin.framework.queue_executor import MainThread
    from plugin.framework.send_cancellation import SendCancellation

    qe = MainThread()
    scope = SendCancellation()
    other = SendCancellation()
    qe._pending_posts.append((lambda: None, (), {}, scope))
    qe._pending_posts.append((lambda: None, (), {}, other))
    qe._pending_posts.append((lambda: None, (), {}, None))
    qe.cancel_pending_work(scope)
    assert len(qe._pending_posts) == 2
    assert qe._pending_posts[0][3] is other
    assert qe._pending_posts[1][3] is None


def test_flush_pending_posts_keeps_original_scope():
    from plugin.framework.queue_executor import MainThread
    from plugin.framework.send_cancellation import SendCancellation

    qe = MainThread()
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


def test_flush_pending_posts_keeps_unenqueued_tail():
    """Flush logs and returns; it does not raise."""
    from plugin.framework.queue_executor import MainThread

    qe = MainThread()
    qe._initialized = True
    qe._async_callback_service = object()

    def first() -> str:
        return "first"

    def second() -> str:
        return "second"

    def third() -> str:
        return "third"

    def newcomer() -> str:
        return "newcomer"

    qe._pending_posts.append((first, (), {}, None))
    qe._pending_posts.append((second, (), {}, None))
    qe._pending_posts.append((third, (), {}, None))
    real_enqueue = qe._enqueue_work
    calls = {"n": 0}

    def flaky(fn, args, kwargs, blocking=True, *, bound_scope=None, poke=True):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        if calls["n"] > 1:
            # A waiter notified by the swap can append before this raise.
            with qe._pending_lock:
                qe._pending_posts.append((newcomer, (), {}, None))
            raise RuntimeError("enqueue failed")
        return real_enqueue(fn, args, kwargs, blocking=blocking, bound_scope=bound_scope, poke=poke)

    qe._enqueue_work = flaky  # type: ignore[method-assign]
    qe._flush_pending_posts()
    assert calls["n"] == 2
    assert qe._work_queue.qsize() == 1
    queued = qe._work_queue.get_nowait()
    assert queued.fn is first
    assert [row[0] for row in qe._pending_posts] == [second, third, newcomer]


def test_flush_failure_logs_after_order_lock_released(monkeypatch: pytest.MonkeyPatch) -> None:
    from plugin.framework.queue_executor import MainThread, log

    qe = MainThread()
    qe._initialized = True
    qe._async_callback_service = object()

    def pending_fn() -> None:
        pass

    qe._pending_posts.append((pending_fn, (), {}, None))

    class ProbeRLock:
        def __init__(self) -> None:
            self._lock = threading.RLock()
            self.depth = 0

        def acquire(self, blocking: bool = True, timeout: float = -1.0) -> bool:
            acquired = self._lock.acquire(blocking, timeout)
            if acquired:
                self.depth += 1
            return acquired

        def release(self) -> None:
            self.depth -= 1
            self._lock.release()

        def __enter__(self) -> bool:
            return self.acquire()

        def __exit__(self, *args: object) -> None:
            self.release()

    probe = ProbeRLock()
    qe._order_lock = probe  # type: ignore[assignment]

    fail_exc = RuntimeError("enqueue failed")

    def failing_enqueue(fn: object, args: object, kwargs: object, blocking: bool = True, *, bound_scope: object = None, poke: bool = True) -> object:
        raise fail_exc

    qe._enqueue_work = failing_enqueue  # type: ignore[method-assign]

    logged: list[tuple[int, object]] = []

    def mock_log_error(msg: str, *args: object, **kwargs: object) -> None:
        logged.append((probe.depth, kwargs.get("exc_info")))

    monkeypatch.setattr(log, "error", mock_log_error)

    qe._flush_pending_posts()

    assert len(logged) == 1
    depth, exc_info = logged[0]
    assert depth == 0
    assert exc_info is fail_exc
    assert exc_info.__traceback__ is not None
    assert [row[0] for row in qe._pending_posts] == [pending_fn]


def test_execute_post_and_set_context_proceed_when_pending_flush_fails():
    from plugin.framework.queue_executor import MainThread

    def old() -> str:
        return "old"

    def execute_target() -> str:
        return "execute_target"

    def post_target() -> str:
        return "post_target"

    # call: policy test_marshal, _get_async_callback to truthy, _wait_for_result to return "mine"
    qe_exec = MainThread()
    mt.MainThread.install(mt.Policy.test_marshal)
    qe_exec._initialized = True
    qe_exec._async_callback_service = object()
    qe_exec._pending_posts.append((old, (), {}, None))
    real_enqueue_exec = qe_exec._enqueue_work
    calls_exec = {"n": 0}
    enqueued_items_exec = []

    def flaky_exec(fn, args, kwargs, blocking=True, *, bound_scope=None, poke=True):  # type: ignore[no-untyped-def]
        calls_exec["n"] += 1
        if calls_exec["n"] == 1:
            raise RuntimeError("enqueue failed")
        item = real_enqueue_exec(fn, args, kwargs, blocking=blocking, bound_scope=bound_scope, poke=poke)
        enqueued_items_exec.append(item)
        return item

    qe_exec._enqueue_work = flaky_exec  # type: ignore[method-assign]
    with patch.object(qe_exec, "_get_async_callback", return_value=object()), \
         patch.object(qe_exec, "_wait_for_result", return_value="mine"):
        res = qe_exec.call(execute_target)
        assert res == "mine"
    assert [row[0] for row in qe_exec._pending_posts] == [old]
    assert len(enqueued_items_exec) == 1
    assert enqueued_items_exec[0].fn is execute_target

    # post: same policy/callback patches, fresh executor and fresh one-shot flaky enqueue
    qe_post = MainThread()
    mt.MainThread.install(mt.Policy.test_marshal)
    qe_post._initialized = True
    qe_post._async_callback_service = object()
    qe_post._pending_posts.append((old, (), {}, None))
    real_enqueue_post = qe_post._enqueue_work
    calls_post = {"n": 0}
    enqueued_items_post = []

    def flaky_post(fn, args, kwargs, blocking=True, *, bound_scope=None, poke=True):  # type: ignore[no-untyped-def]
        calls_post["n"] += 1
        if calls_post["n"] == 1:
            raise RuntimeError("enqueue failed")
        item = real_enqueue_post(fn, args, kwargs, blocking=blocking, bound_scope=bound_scope, poke=poke)
        enqueued_items_post.append(item)
        return item

    qe_post._enqueue_work = flaky_post  # type: ignore[method-assign]
    with patch.object(qe_post, "_get_async_callback", return_value=object()):
        res_post = qe_post.post(post_target)
        assert res_post is None
    assert [row[0] for row in qe_post._pending_posts] == [old]
    assert len(enqueued_items_post) == 1
    assert enqueued_items_post[0].fn is post_target

    # set_context: fresh executor, _ctx set to plain object, initialized with dummy service, one pending old, flaky enqueue
    qe_ctx = MainThread()
    ctx_obj = object()
    qe_ctx._ctx = ctx_obj
    qe_ctx._initialized = True
    qe_ctx._async_callback_service = object()
    qe_ctx._pending_posts.append((old, (), {}, None))
    calls_ctx = {"n": 0}

    def flaky_ctx(fn, args, kwargs, blocking=True, *, bound_scope=None, poke=True):  # type: ignore[no-untyped-def]
        calls_ctx["n"] += 1
        if calls_ctx["n"] == 1:
            raise RuntimeError("enqueue failed")
        return qe_ctx._enqueue_work(fn, args, kwargs, blocking=blocking, bound_scope=bound_scope, poke=poke)

    qe_ctx._enqueue_work = flaky_ctx  # type: ignore[method-assign]
    qe_ctx.set_context(ctx_obj)
    assert [row[0] for row in qe_ctx._pending_posts] == [old]


def test_wait_for_result_timeout_uses_qualname():
    from plugin.framework.queue_executor import MainThread, _WorkItem

    def outer():
        def nested_target():
            pass
        return nested_target

    fn = outer()
    assert ".<locals>." in fn.__qualname__
    assert ".<locals>." not in fn.__name__

    qe = MainThread()
    item = _WorkItem(fn, (), {}, blocking=True)
    item._claimed = False

    with patch.object(item.event, "wait", return_value=False):
        with pytest.raises(TimeoutError) as exc_info:
            qe._wait_for_result(item, timeout=0.01)

    msg = str(exc_info.value)
    assert fn.__qualname__ in msg
    assert ".<locals>." in msg
    assert "timed out after 0.01s" in msg

def test_execute_accepts_and_passes_bound_scope() -> None:
    executor = mt.MainThread()
    mt.MainThread.install(mt.Policy.test_marshal)
    scope = sc.SendCancellation()
    seen_items: list[tuple[mt._WorkItem, float]] = []

    def fake_wait(item: mt._WorkItem, timeout: float) -> str:
        seen_items.append((item, timeout))
        return "dummy_result"

    def dummy_fn(*_args: Any) -> None:
        pass

    with patch.object(executor, "_get_async_callback", return_value=MagicMock()), \
         patch.object(executor, "_wait_for_result", side_effect=fake_wait):
        result = executor.call(dummy_fn, 1, 2, bound_scope=scope, timeout=5.0)

    assert result == "dummy_result"
    assert len(seen_items) == 1
    item, timeout = seen_items[0]
    assert item.scope is scope
    assert item.event is not None
    assert item.fn is dummy_fn
    assert item.args == (1, 2)
    assert timeout == 5.0

def test_flush_pending_posts_order_concurrent_enqueue():
    """A direct enqueue overlapping with flush must not jump ahead of pending posts."""
    from plugin.framework.queue_executor import MainThread
    import threading
    import time

    qe = MainThread()
    qe._initialized = True
    qe._async_callback_service = object()  # fake callback service

    # Pre-populate pending posts
    qe._pending_posts.append((lambda: "pending1", (), {}, None))
    qe._pending_posts.append((lambda: "pending2", (), {}, None))

    enqueued_items = []

    def slow_put(item, block=True, timeout=None):
        time.sleep(0.05)
        enqueued_items.append(item)

    qe._work_queue.put = slow_put

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

def test_set_context_resets_diagnostic_log_flags():
    """A new context object logs marshal failures again; the same object does not."""
    from plugin.framework.queue_executor import MainThread

    qe = MainThread()
    qe._logged_callback_problem = True
    first = object()
    qe.set_context(first)
    assert qe._logged_callback_problem is False
    qe._logged_callback_problem = True
    qe.set_context(first)
    assert qe._logged_callback_problem is True


def test_set_context_wakes_pending_posts():
    """set_context must create AsyncCallback if there are pending posts."""
    from plugin.framework.queue_executor import MainThread
    from unittest.mock import patch, MagicMock
    from plugin.framework.thread_guard import unwrap_uno

    qe = MainThread()
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
        mock_smgr.createInstanceWithContext.assert_called_once_with("com.sun.star.awt.AsyncCallback", unwrap_uno(mock_ctx))
        # Verify that _enqueue_work was called
        mock_enqueue.assert_called_once()
        assert mock_enqueue.call_args.args[0]() == "pending"




def test_process_queue_claim_gap_raises():
    """An exception between claim and fn() must not leave the waiter blocked forever."""
    from plugin.framework.queue_executor import MainThread, _WorkItem
    from unittest.mock import patch
    qe = MainThread()
    def my_fn(): pass

    item = _WorkItem(my_fn, (), {}, blocking=True)
    qe._work_queue.put(item)

    with patch("plugin.framework.queue_executor._fn_label", side_effect=ValueError("label error")):
        qe.process_queue()

    assert item.event.is_set(), "waiter is blocked forever"
    assert isinstance(item.exception, ValueError)


def test_wait_for_result_shutting_down_raises():
    """_wait_for_result must raise RuntimeError if sys.is_finalizing() is true while waiting."""
    from plugin.framework.queue_executor import MainThread, _WorkItem
    from unittest.mock import patch

    qe = MainThread()
    item = _WorkItem(lambda: None, (), {}, blocking=True)
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
    from plugin.framework.queue_executor import MainThread, _WorkItem
    from unittest.mock import patch

    qe = MainThread()
    item = _WorkItem(lambda: None, (), {}, blocking=True)
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


def test_is_marshal_ready():
    from plugin.framework.queue_executor import MainThread

    qe = MainThread()
    assert qe.is_marshal_ready() is False

    qe._initialized = True
    assert qe.is_marshal_ready() is False

    qe._initialized = False
    qe._async_callback_service = MagicMock()
    assert qe.is_marshal_ready() is False

    qe._initialized = True
    assert qe.is_marshal_ready() is True


def test_pump_main_thread_work_queue_skips_off_main_and_runs_on_main() -> None:
    from plugin.framework.queue_executor import MainThread, pump_main_thread_work_queue

    ex = MainThread()
    called: list[str] = []
    ex._enqueue_work(lambda: called.append("ran"), (), {}, blocking=False, poke=False)
    assert ex.pending_work_count() == 1

    def worker() -> None:
        pump_main_thread_work_queue(executor=ex)

    t = threading.Thread(target=worker)
    t.start()
    t.join(timeout=2.0)
    assert not t.is_alive()
    assert ex.pending_work_count() == 1
    assert called == []

    pump_main_thread_work_queue(executor=ex)
    assert ex.pending_work_count() == 0
    assert called == ["ran"]


def test_pump_ui_idle_skips_off_main_and_pumps_on_main() -> None:
    from plugin.framework.queue_executor import MainThread, pump_ui_idle

    ex = MainThread()
    calls: list[str] = []

    class Toolkit:
        def processEventsToIdle(self) -> None:
            calls.append("pump")

    toolkit = Toolkit()

    def worker() -> None:
        pump_ui_idle(toolkit, executor=ex)

    t = threading.Thread(target=worker)
    t.start()
    t.join(timeout=2.0)
    assert not t.is_alive()
    assert calls == []

    pump_ui_idle(toolkit, executor=ex)
    assert calls == ["pump"]


def test_pump_main_thread_work_queue_runs_on_designated_main() -> None:
    if is_release_build():
        pytest.skip("thread_guard is stubbed in release bundles; designated main thread is a no-op")
    from plugin.framework.queue_executor import MainThread, Policy, main, pump_main_thread_work_queue

    ex = MainThread()
    called: list[str] = []
    ex._enqueue_work(lambda: called.append("ran"), (), {}, blocking=False, poke=False)
    assert ex.pending_work_count() == 1

    def worker() -> None:
        pump_main_thread_work_queue(executor=ex)

    t = threading.Thread(target=worker)
    main.install(Policy.production, designated=t)
    try:
        t.start()
        t.join(timeout=2.0)
        assert not t.is_alive()
        assert ex.pending_work_count() == 0
        assert called == ["ran"]
    finally:
        main.install(Policy.production, designated=None)


def test_pump_main_thread_work_queue_runs_in_sync_host_on_main() -> None:
    from plugin.framework.queue_executor import MainThread, pump_main_thread_work_queue
    from plugin.framework.thread_guard import sync_host_dispatch

    ex = MainThread()
    called: list[str] = []
    ex._enqueue_work(lambda: called.append("ran"), (), {}, blocking=False, poke=False)
    assert ex.pending_work_count() == 1

    with sync_host_dispatch():
        pump_main_thread_work_queue(executor=ex)

    assert ex.pending_work_count() == 0
    assert called == ["ran"]


def test_main_run_inlines_when_current() -> None:
    from plugin.framework.queue_executor import main

    with patch.object(main, "is_current", return_value=True), \
         patch.object(main, "call") as mock_call:
        result = main.run(lambda x: x * 2, 21)
        assert result == 42
        mock_call.assert_not_called()


@pytest.mark.parametrize(
    ("timeout_kw", "expected_timeout"),
    [({}, 30.0), ({"timeout": 10.0}, 10.0)],
    ids=["default_timeout", "explicit_timeout"],
)
def test_main_run_marshals_when_off_main(timeout_kw: dict[str, float], expected_timeout: float) -> None:
    from plugin.framework.queue_executor import main

    def fn() -> str:
        return "marshaled"

    with patch.object(main, "is_current", return_value=False), \
         patch.object(main, "call", return_value="marshaled") as mock_call:
        result = main.run(fn, **timeout_kw)
        assert result == "marshaled"
        mock_call.assert_called_once_with(fn, timeout=expected_timeout)


def test_main_post_or_run_inlines_on_main() -> None:
    from plugin.framework.queue_executor import main

    with patch.object(main, "is_current", return_value=True), \
         patch("plugin.framework.thread_guard.get_background_task_name", return_value=None), \
         patch.object(main, "post") as mock_post:
        assert main.post_or_run(lambda x: x + 10, 5) == 15
        mock_post.assert_not_called()


_SCOPE_SENTINEL = object()


@pytest.mark.parametrize(
    ("is_current", "bg_task", "bound_scope", "args", "kwargs"),
    [
        (False, None, mt._SCOPE_UNSET, ("hello",), {}),
        (True, "mcp_worker", mt._SCOPE_UNSET, (), {}),
        (False, None, mt._SCOPE_UNSET, ("arg1",), {"kw": "val"}),
        (False, None, _SCOPE_SENTINEL, (), {}),
    ],
    ids=[
        "posts_off_main",
        "posts_when_bg_task_set",
        "posts_with_args_and_kwargs",
        "forwards_bound_scope",
    ],
)
def test_main_post_or_run_posts(
    is_current: bool,
    bg_task: str | None,
    bound_scope: object,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> None:
    from plugin.framework.queue_executor import main

    def fn(*_args: Any, **_kwargs: Any) -> None:
        pass

    call_kwargs = dict(kwargs)
    if bound_scope is not mt._SCOPE_UNSET:
        call_kwargs["bound_scope"] = bound_scope

    with patch.object(main, "is_current", return_value=is_current), \
         patch("plugin.framework.thread_guard.get_background_task_name", return_value=bg_task), \
         patch.object(main, "post") as mock_post:
        assert main.post_or_run(fn, *args, **call_kwargs) is None
        mock_post.assert_called_once_with(fn, *args, bound_scope=bound_scope, **kwargs)


@requires_asserts
def test_main_post_or_run_asserts_callable() -> None:
    from plugin.framework.queue_executor import main

    with pytest.raises(AssertionError):
        main.post_or_run("not_callable")


def _run_in_dummy_thread(fn: Callable[[], None], timeout: float = 5.0) -> None:
    import _thread

    done = threading.Event()
    exc: list[BaseException] = []

    def target() -> None:
        try:
            fn()
        except BaseException as err:
            exc.append(err)
        finally:
            done.set()

    _thread.start_new_thread(target, ())
    if not done.wait(timeout):
        raise TimeoutError("Dummy thread failed to finish within %s s" % timeout)
    if exc:
        raise exc[0]


def test_on_python_main_thread_is_designated_or_python_main() -> None:
    from plugin.framework.queue_executor import MainThread, Policy, main
    from plugin.framework.thread_guard import UNTAGGED_DUMMY_IS_CURRENT, set_background_task

    orig_policy = MainThread._policy
    orig_designated = MainThread.designated
    orig_poke = MainThread.poke
    try:
        # Case 1: Pytest thread with designated is None
        main.install(Policy.production, designated=None)
        assert MainThread.on_python_main_thread() is True
        assert main.is_current() is True
        assert main._is_marshal_thread() is True

        # Case 2: designated=worker; on worker all three are true; on pytest thread all three are false
        worker_results: dict[str, bool] = {}
        worker_done = threading.Event()
        worker_release = threading.Event()

        def worker_target() -> None:
            worker_results["on_python_main_thread"] = MainThread.on_python_main_thread()
            worker_results["is_current"] = main.is_current()
            worker_results["_is_marshal_thread"] = main._is_marshal_thread()
            worker_done.set()
            worker_release.wait(timeout=5.0)

        worker = threading.Thread(target=worker_target)
        main.install(Policy.production, designated=worker)
        worker.start()
        try:
            assert worker_done.wait(timeout=5.0)
            assert worker_results == {
                "on_python_main_thread": True,
                "is_current": True,
                "_is_marshal_thread": True,
            }
            assert MainThread.on_python_main_thread() is False
            assert main.is_current() is False
            assert main._is_marshal_thread() is False
        finally:
            worker_release.set()
            worker.join(timeout=2.0)
            main.install(Policy.production, designated=None)

        # Case 3: Policy.test_marshal with designated is None on pytest thread
        main.install(Policy.test_marshal, designated=None)
        assert MainThread.on_python_main_thread() is True
        assert main.is_current() is True
        assert main._is_marshal_thread() is False

        # Case 4: set_background_task("pe2i") on pytest thread under Policy.production
        main.install(Policy.production, designated=None)
        set_background_task("pe2i")
        try:
            assert MainThread.on_python_main_thread() is True
            assert main.is_current() is True
            assert main._is_marshal_thread() is False
        finally:
            set_background_task(None)

        # Case 5: Dummy thread via _thread.start_new_thread
        def dummy_target() -> None:
            try:
                assert MainThread.on_python_main_thread() is False
                assert main.is_current() is UNTAGGED_DUMMY_IS_CURRENT
                set_background_task("pe2i")
                assert main.is_current() is False
            finally:
                set_background_task(None)

        _run_in_dummy_thread(dummy_target)
    finally:
        main.install(orig_policy, designated=orig_designated, poke=orig_poke)
        set_background_task(None)


def test_eval_harness_disables_async_callback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mt.MainThread, "_policy", mt.MainThread._policy)
    monkeypatch.setenv("WRITERAGENT_EVAL_HARNESS", "1")
    mt.install_from_environ()
    ex = mt.MainThread()
    ex.set_context(MagicMock(name="ctx"))
    assert ex._get_async_callback() is None
    assert ex._initialized is True
