# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for the Layer A UNO thread-safety guard (thread_guard.py)."""

import importlib
import threading
from unittest.mock import patch, MagicMock

import pytest

# Import after possible env setup in specific tests; we mutate the module flags for isolation.
import plugin.framework.thread_guard as tg


def _reload_thread_guard():
    """Re-read GUARD_ON from the environment (see tests/conftest.py setdefault)."""
    importlib.reload(tg)


def test_guard_on_by_default_when_env_unset(monkeypatch):
    monkeypatch.delenv("WRITERAGENT_UNO_THREAD_GUARD", raising=False)
    try:
        _reload_thread_guard()
        assert tg.GUARD_ON is True
    finally:
        monkeypatch.setenv("WRITERAGENT_UNO_THREAD_GUARD", "0")
        _reload_thread_guard()


def test_guard_off_when_env_zero(monkeypatch):
    monkeypatch.setenv("WRITERAGENT_UNO_THREAD_GUARD", "0")
    try:
        _reload_thread_guard()
        assert tg.GUARD_ON is False
    finally:
        _reload_thread_guard()


def _make_pyuno_like():
    """A minimal stand-in for a PyUNO object (has queryInterface, pyuno-ish module)."""
    obj = MagicMock()
    type(obj).__module__ = "pyuno"
    # Make it look UNO-ish
    obj.queryInterface = MagicMock(return_value=obj)
    return obj


def test_on_main_thread_detects(monkeypatch):
    # Force current == main
    monkeypatch.setattr(threading, "current_thread", lambda: threading.main_thread())
    assert tg.on_main_thread() is True


def test_assert_main_thread_noop_on_main(monkeypatch, caplog):
    monkeypatch.setattr(threading, "current_thread", lambda: threading.main_thread())
    tg.assert_main_thread("some.getter")
    # no exception, no warning
    assert not any("UNO thread violation" in r.message for r in caplog.records)


def test_assert_raises_when_guard_on_from_bg(monkeypatch):
    # Simulate background thread
    fake_bg = MagicMock()
    fake_bg.name = "worker-foo"
    monkeypatch.setattr(threading, "current_thread", lambda: fake_bg)
    monkeypatch.setattr(tg, "on_main_thread", lambda: False)
    # Ensure guard is on for this test
    was = tg.GUARD_ON
    tg.GUARD_ON = True
    tg.set_background_task("run_search")
    with tg._violation_ui_lock:
        tg._violation_dialog_open = False
    try:
        with patch.object(tg, "_notify_thread_violation") as notify:
            with pytest.raises(RuntimeError) as exc_info:
                tg.assert_main_thread("uno_context.get_desktop")
            notify.assert_called_once()
        msg = str(exc_info.value)
        assert "UNO thread violation" in msg
        assert "run_search" in msg or "worker-foo" in msg
    finally:
        tg.GUARD_ON = was
        tg.set_background_task(None)
        with tg._violation_ui_lock:
            tg._violation_dialog_open = False


def test_assert_logs_warning_when_guard_off_from_bg(monkeypatch):
    """Guard-off path must not raise; it logs at WARNING (tested via no-exception + message construction)."""
    fake_bg = MagicMock()
    fake_bg.name = "worker-bar"
    monkeypatch.setattr(threading, "current_thread", lambda: fake_bg)
    monkeypatch.setattr(tg, "on_main_thread", lambda: False)
    was = tg.GUARD_ON
    tg.GUARD_ON = False
    tg.set_background_task("run_thing")
    try:
        # Must not raise; the implementation does log.warning(..., stack_info=True)
        tg.assert_main_thread("document_helpers.resolve")
    finally:
        tg.GUARD_ON = was
        tg.set_background_task(None)


def test_main_thread_only_decorator_raises_from_bg(monkeypatch):
    @tg.main_thread_only
    def red_getter(x):
        return x * 2

    fake_bg = MagicMock()
    fake_bg.name = "bg-task"
    monkeypatch.setattr(threading, "current_thread", lambda: fake_bg)
    monkeypatch.setattr(tg, "on_main_thread", lambda: False)
    # Skip violation popup marshal (execute_on_main_thread timeout=5s) — nothing pumps the queue under pytest.
    monkeypatch.setenv("WRITERAGENT_TESTING", "1")
    was = tg.GUARD_ON
    tg.GUARD_ON = True
    try:
        with pytest.raises(RuntimeError):
            red_getter(21)
    finally:
        tg.GUARD_ON = was


def test_background_decorator_warns_on_main_thread(monkeypatch):
    @tg.background
    def blue_worker():
        return 1

    monkeypatch.setattr(threading, "current_thread", lambda: threading.main_thread())
    monkeypatch.setattr(tg, "on_main_thread", lambda: True)
    with patch.object(tg.log, "warning") as warn:
        assert blue_worker() == 1
    warn.assert_called_once()
    assert "@background fn" in warn.call_args[0][0]


def test_proxy_iter_asserts_on_each_next():
    real = [object(), object()]
    prox = tg._UnoThreadGuardProxy(real)
    calls = {"n": 0}

    def _count(msg: str = "") -> None:
        calls["n"] += 1

    with patch.object(tg, "assert_main_thread", side_effect=_count):
        iterator = iter(prox)
        after_iter = calls["n"]
        next(iterator)
        after_next = calls["n"]
    assert after_next > after_iter


def test_proxy_iter_asserts_before_raw_next():
    """The wrapper must not advance the raw iterator before the thread check."""
    order: list[str] = []

    class Raw:
        def __iter__(self):
            order.append("iter")
            return self

        def __next__(self):
            order.append("next")
            return object()

    def _mark(msg: str = "") -> None:
        order.append("assert")

    prox = tg._UnoThreadGuardProxy(Raw())
    with patch.object(tg, "assert_main_thread", side_effect=_mark):
        iterator = iter(prox)
        next(iterator)
    assert order == ["assert", "iter", "assert", "next"]


def test_proxy_wraps_pyuno_and_asserts_on_access(monkeypatch):
    # Directly exercise the proxy class (its behaviors); _wrap decision is tested below.
    real = _make_pyuno_like()
    prox = tg._UnoThreadGuardProxy(real)
    assert prox is not real
    # Accessing an attr should assert
    with patch.object(tg, "assert_main_thread") as am:
        _ = prox.getCurrentComponent
        am.assert_called()
    # Call should assert and wrap return
    with patch.object(tg, "assert_main_thread") as am:
        res = prox.foo(1, bar=2)
        am.assert_called()
        # The underlying was called; result should be wrapped if pyuno-like
        assert isinstance(res, tg._UnoThreadGuardProxy) or res is not None


def test_proxy_passthrough_plain_values_when_guard_on(monkeypatch):
    real = _make_pyuno_like()
    prox = tg._UnoThreadGuardProxy(real)
    with patch.object(tg, "assert_main_thread"):
        # Plain return should not be wrapped
        real.plain.return_value = "hello"
        assert prox.plain() == "hello"


def test_proxy_eq_and_hash_delegate_to_target():
    class UnoLike:
        __module__ = "pyuno"

        def queryInterface(self, *args, **kwargs):
            return self

    real = UnoLike()
    prox = tg._UnoThreadGuardProxy(real)
    other = tg._UnoThreadGuardProxy(real)
    with patch.object(tg, "assert_main_thread"):
        assert prox == real
        assert prox == other
        assert prox != UnoLike()
    assert hash(prox) == hash(real)


def test_proxy_str_repr_raise_off_thread(monkeypatch):
    from tests.harness.strip_bundle import skip_if_release_build

    skip_if_release_build("release stub proxy does not assert on str/repr")
    monkeypatch.setenv("WRITERAGENT_TESTING", "1")
    real = _make_pyuno_like()
    prox = tg._UnoThreadGuardProxy(real)
    assert "UNOProxy" in repr(prox)
    str(prox)
    was = tg.GUARD_ON
    tg.GUARD_ON = True
    errors: list[BaseException] = []

    def _call() -> None:
        for fn in (str, repr):
            try:
                fn(prox)
            except BaseException as exc:
                errors.append(exc)

    try:
        worker = threading.Thread(target=_call, name="bg-log")
        worker.start()
        worker.join(timeout=2)
        assert not worker.is_alive()
        assert len(errors) == 2
        assert all(isinstance(exc, RuntimeError) and "UNO thread violation" in str(exc) for exc in errors)
    finally:
        tg.GUARD_ON = was


def test_set_background_task_none_does_not_clear_violation_dialog():
    with tg._violation_ui_lock:
        tg._violation_dialog_open = True
    try:
        tg.set_background_task(None)
        with tg._violation_ui_lock:
            assert tg._violation_dialog_open is True
    finally:
        with tg._violation_ui_lock:
            tg._violation_dialog_open = False


def test_unwrap_roundtrip():
    real = _make_pyuno_like()
    p = tg._UnoThreadGuardProxy(real)
    assert tg._unwrap_uno(p) is real
    assert tg._unwrap_uno(real) is real


def test_wrap_uno_skips_pyuno_structs():
    """PropertyValue-like structs must stay unwrapped for C++ media descriptors."""

    class _Struct:
        __module__ = "pyuno"
        __pyunostruct__ = True

    struct = _Struct()
    was = tg.GUARD_ON
    tg.GUARD_ON = True
    try:
        assert tg._wrap_uno(struct) is struct
        tup = tg._wrap_uno((struct,))
        assert tup[0] is struct
    finally:
        tg.GUARD_ON = was


def test_unwrap_uno_tuple_of_proxies():
    real = _make_pyuno_like()
    proxy = tg._UnoThreadGuardProxy(real)
    out = tg._unwrap_uno((proxy, "x"))
    assert out[0] is real
    assert out[1] == "x"


def test_wrap_uno_one_level_sequences(monkeypatch):
    """Python list/tuple/dict values of UNO-like objects get a proxy when GUARD_ON."""
    inner = object()
    was = tg.GUARD_ON
    tg.GUARD_ON = True
    try:
        with patch.object(tg, "_is_pyuno", side_effect=lambda o: o is inner):
            wrapped_list = tg._wrap_uno([inner])
            wrapped_tup = tg._wrap_uno((inner,))
            wrapped_dict = tg._wrap_uno({"k": inner})
        assert isinstance(wrapped_list[0], tg._UnoThreadGuardProxy)
        assert isinstance(wrapped_tup[0], tg._UnoThreadGuardProxy)
        assert isinstance(wrapped_dict["k"], tg._UnoThreadGuardProxy)
        assert list(wrapped_dict.keys()) == ["k"]
        monkeypatch.setattr(tg, "on_main_thread", lambda: False)
        monkeypatch.setenv("WRITERAGENT_TESTING", "1")
        with pytest.raises(RuntimeError, match="UNO thread violation"):
            _unused = wrapped_list[0].getString
    finally:
        tg.GUARD_ON = was


def test_wrap_decision_uses_is_pyuno_and_guard_flag(monkeypatch):
    real = _make_pyuno_like()
    # When guard off, never wraps even if pyuno
    was = tg.GUARD_ON
    tg.GUARD_ON = False
    try:
        assert tg._wrap_uno(real) is real
    finally:
        tg.GUARD_ON = was

    # When guard on, wrap only if _is_pyuno says yes
    tg.GUARD_ON = True
    try:
        with patch.object(tg, "_is_pyuno", return_value=True):
            w = tg._wrap_uno(real)
            assert isinstance(w, tg._UnoThreadGuardProxy)
        with patch.object(tg, "_is_pyuno", return_value=False):
            assert tg._wrap_uno(real) is real
    finally:
        tg.GUARD_ON = was


def test_bypass_thread_guard_still_works_via_registry(monkeypatch):
    # This is a cross-check that the registry-level bypass still prevents hitting execute_safe's guard.
    # We import here to avoid import-order issues with the flag.
    from plugin.framework.tool import ToolRegistry, ToolContext

    calls = []

    class DummySync:
        name = "dummy"
        description = "d"
        parameters = {"type": "object", "properties": {}}
        uno_services = None
        doc_types = None

        def get_parameters(self, doc_type=None):
            return self.parameters

        def get_description(self, doc_type=None):
            return self.description

        def validate(self, *, doc_type=None, **kwargs):
            return True, None

        def execute(self, ctx, **kwargs):
            calls.append("execute")
            return {"status": "ok"}

    reg = ToolRegistry(MagicMock())
    reg.register(DummySync())  # type: ignore[arg-type]
    ctx = ToolContext(MagicMock(), MagicMock(), "writer", {}, "test")

    out = None

    def bg():
        nonlocal out
        # execute_unguarded calls .execute directly (no execute_safe, no assert)
        out = reg.execute_unguarded("dummy", ctx)

    t = threading.Thread(target=bg)
    t.start()
    t.join()

    assert out == {"status": "ok"}
    assert calls == ["execute"]


def test_notify_skipped_under_writeragent_testing(monkeypatch):
    monkeypatch.setenv("WRITERAGENT_TESTING", "1")
    with tg._violation_ui_lock:
        tg._violation_dialog_open = False
    posts = []

    def fake_post(fn, *args, **kwargs):
        posts.append(fn)

    with patch("plugin.framework.queue_executor.post_to_main_thread", fake_post):
        with patch.object(tg.log, "error") as mock_log_err:
            tg._notify_thread_violation("test violation")
            mock_log_err.assert_called_once_with("test violation", stack_info=True)
    assert posts == []
    with tg._violation_ui_lock:
        assert tg._violation_dialog_open is False


def test_notify_skips_when_async_callback_not_ready(monkeypatch):
    """Do not marshal a violation dialog during QueueExecutor lazy-init (startup deadlock)."""
    from plugin.framework.queue_executor import default_executor

    monkeypatch.delenv("WRITERAGENT_TESTING", raising=False)
    with tg._violation_ui_lock:
        tg._violation_dialog_open = False
    was_init = default_executor._initialized
    default_executor._initialized = False
    posts = []

    def fake_execute(fn, *args, **kwargs):
        posts.append(fn)

    try:
        with patch("plugin.framework.queue_executor.execute_on_main_thread", fake_execute):
            tg._notify_thread_violation("during async callback init")
        assert posts == []
        with tg._violation_ui_lock:
            assert tg._violation_dialog_open is False
    finally:
        default_executor._initialized = was_init
        with tg._violation_ui_lock:
            tg._violation_dialog_open = False


def test_notify_dedupes_per_thread(monkeypatch):
    from plugin.framework.queue_executor import default_executor

    monkeypatch.delenv("WRITERAGENT_TESTING", raising=False)
    with tg._violation_ui_lock:
        tg._violation_dialog_open = False
    default_executor._initialized = True
    default_executor._async_callback_service = MagicMock()
    posts = []

    def fake_post(fn, *args, **kwargs):
        posts.append(fn)

    with patch("plugin.framework.queue_executor.post_to_main_thread", fake_post):
        tg._notify_thread_violation("first")
        tg._notify_thread_violation("second")
    assert len(posts) == 1
    with tg._violation_ui_lock:
        tg._violation_dialog_open = False


def test_notify_one_dialog_across_threads(monkeypatch):
    from plugin.framework.queue_executor import default_executor

    monkeypatch.delenv("WRITERAGENT_TESTING", raising=False)
    with tg._violation_ui_lock:
        tg._violation_dialog_open = False
    default_executor._initialized = True
    default_executor._async_callback_service = MagicMock()
    posts = []

    def fake_post(fn, *args, **kwargs):
        posts.append(fn)

    cur_ident = 11
    monkeypatch.setattr(threading, "get_ident", lambda: cur_ident)

    try:
        with patch("plugin.framework.queue_executor.post_to_main_thread", fake_post):
            with patch("plugin.chatbot.dialogs.msgbox_with_report"):
                with patch("plugin.framework.uno_context.get_ctx"):
                    tg._notify_thread_violation("thread 11")
                    cur_ident = 22
                    tg._notify_thread_violation("thread 22")
                    assert len(posts) == 1
                    posts[0]()
                    cur_ident = 33
                    tg._notify_thread_violation("thread 33")
                    assert len(posts) == 2
    finally:
        with tg._violation_ui_lock:
            tg._violation_dialog_open = False


def test_assert_logs_and_notifies_when_guard_on(monkeypatch):
    fake_bg = MagicMock()
    fake_bg.name = "worker-notify"
    monkeypatch.setattr(threading, "current_thread", lambda: fake_bg)
    monkeypatch.setattr(tg, "on_main_thread", lambda: False)
    monkeypatch.delenv("WRITERAGENT_TESTING", raising=False)
    was = tg.GUARD_ON
    tg.GUARD_ON = True
    with tg._violation_ui_lock:
        tg._violation_dialog_open = False
    from plugin.framework.queue_executor import default_executor

    default_executor._initialized = True
    default_executor._async_callback_service = MagicMock()
    posts = []

    def fake_post(fn, *args, **kwargs):
        posts.append(fn)

    try:
        with patch("plugin.framework.queue_executor.post_to_main_thread", fake_post):
            with patch.object(tg.log, "error") as mock_error:
                with pytest.raises(RuntimeError):
                    tg.assert_main_thread("test.site")
                mock_error.assert_called_once()
                assert "UNO thread violation" in mock_error.call_args[0][0]
                assert mock_error.call_args[1].get("stack_info") is True
        assert len(posts) == 1
    finally:
        tg.GUARD_ON = was
        with tg._violation_ui_lock:
            tg._violation_dialog_open = False


def test_proxy_bool():
    # Target that does not define len or bool (defaults to truthy)
    class DummyTarget:
        pass
    
    dummy = DummyTarget()
    prox = tg._UnoThreadGuardProxy(dummy)
    with patch.object(tg, "assert_main_thread") as am:
        assert bool(prox) is True
        am.assert_called_with("UNO bool")


class _PicklableTarget:
    def hello(self) -> str:
        return "pickled"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, _PicklableTarget)


def test_proxy_copy():
    import copy

    class Target:
        def hello(self):
            return "world"

    target = Target()
    proxy = tg._UnoThreadGuardProxy(target)
    c = copy.copy(proxy)
    assert isinstance(c, tg._UnoThreadGuardProxy)
    assert c._target is target
    assert c.hello() == "world"


def test_proxy_pickle():
    import pickle

    target = _PicklableTarget()
    proxy = tg._UnoThreadGuardProxy(target)
    dumped = pickle.dumps(proxy)
    loaded = pickle.loads(dumped)
    assert isinstance(loaded, tg._UnoThreadGuardProxy)
    assert loaded._target == target
    assert loaded.hello() == "pickled"


def test_proxy_object_new_raises_attribute_error_not_recursion():
    proxy = object.__new__(tg._UnoThreadGuardProxy)
    with pytest.raises(AttributeError):
        _ = proxy.hello


def test_proxy_dir():
    class Target:
        def hello(self):
            return "world"

    target = Target()
    proxy = tg._UnoThreadGuardProxy(target)
    names = dir(proxy)
    assert "hello" in names
    assert "_target" not in names


def test_proxy_dir_raises_off_thread(monkeypatch):
    from tests.harness.strip_bundle import skip_if_release_build

    skip_if_release_build("release stub proxy does not assert on dir")
    monkeypatch.setenv("WRITERAGENT_TESTING", "1")
    real = _make_pyuno_like()
    prox = tg._UnoThreadGuardProxy(real)
    was = tg.GUARD_ON
    tg.GUARD_ON = True
    errors: list[BaseException] = []

    def _call() -> None:
        try:
            dir(prox)
        except BaseException as exc:
            errors.append(exc)

    try:
        worker = threading.Thread(target=_call, name="bg-dir")
        worker.start()
        worker.join(timeout=2)
        assert not worker.is_alive()
        assert len(errors) == 1
        assert isinstance(errors[0], RuntimeError) and "UNO thread violation" in str(errors[0])
    finally:
        tg.GUARD_ON = was


def test_violation_popup_is_unscoped(monkeypatch):
    from plugin.framework.queue_executor import SendCancellation, _current_send_cancellation, default_executor

    monkeypatch.delenv("WRITERAGENT_TESTING", raising=False)
    with tg._violation_ui_lock:
        tg._violation_dialog_open = False
    was_init = default_executor._initialized
    was_async = default_executor._async_callback_service
    default_executor._initialized = True
    default_executor._async_callback_service = MagicMock()
    token = None
    captured_kwargs: list[dict] = []

    def fake_post(fn, *args, **kwargs):
        captured_kwargs.append(kwargs)

    try:
        token = _current_send_cancellation.set(SendCancellation())
        with patch("plugin.framework.queue_executor.post_to_main_thread", side_effect=fake_post):
            tg._notify_thread_violation("test violation")
        assert len(captured_kwargs) == 1
        assert "bound_scope" in captured_kwargs[0]
        assert captured_kwargs[0]["bound_scope"] is None
    finally:
        if token is not None:
            _current_send_cancellation.reset(token)
        default_executor._initialized = was_init
        default_executor._async_callback_service = was_async
        with tg._violation_ui_lock:
            tg._violation_dialog_open = False


def test_stop_does_not_drop_violation_popup(monkeypatch):
    import queue as queue_mod

    from plugin.framework.queue_executor import SendCancellation, _current_send_cancellation, default_executor

    monkeypatch.delenv("WRITERAGENT_TESTING", raising=False)
    with tg._violation_ui_lock:
        tg._violation_dialog_open = False
    was_init = default_executor._initialized
    was_async = default_executor._async_callback_service

    # What was wrong: pending_work_count() == 1 failed as assert 2 == 1
    # (PR 1577 ubuntu run 38784025798, gw4). queue_depth was already 1 at
    # post_enqueue, which is logged after _flush_pending_posts and before
    # this popup is put.
    # How it happened: a previous test on this xdist worker left a row in
    # default_executor._pending_posts. post() flushes that row, then enqueues
    # _show_popup. --dist=loadgroup schedules unmarked tests as separate work
    # units, so the row is present only on some reruns.
    # Why this change: park the work queue and the pending list first, and
    # seed a decoy row so forgetting the park fails in one process. The popup
    # is still the unscoped post, so scope.cancel() does not drop it.
    def decoy() -> None:
        decoy_ran.append(True)

    decoy_ran: list[bool] = []
    saved_work: list[object] = []
    saved_pending = []
    token = None

    try:
        with default_executor._pending_lock:
            default_executor._pending_posts.append((decoy, (), {}, None))

        while True:
            try:
                saved_work.append(default_executor._work_queue.get_nowait())
            except queue_mod.Empty:
                break
        with default_executor._pending_lock:
            saved_pending = list(default_executor._pending_posts)
            default_executor._pending_posts = []
            default_executor._pending_lock.notify_all()
        saved_pending = [row for row in saved_pending if row[0] is not decoy]

        default_executor._initialized = True
        default_executor._async_callback_service = MagicMock()
        monkeypatch.setattr(default_executor, "_poke_main_thread", lambda: None)

        scope = SendCancellation()
        token = _current_send_cancellation.set(scope)
        tg.set_background_task("violation-worker")

        msgbox_calls: list[object] = []

        with patch("plugin.chatbot.dialogs.msgbox_with_report", side_effect=lambda *args, **kwargs: msgbox_calls.append(args)):
            with patch("plugin.framework.uno_context.get_ctx", return_value=MagicMock()):
                tg._notify_thread_violation("test stop violation")
                assert default_executor.pending_work_count() == 1
                with default_executor._work_queue.mutex:
                    queued = default_executor._work_queue.queue[0]
                assert queued.fn.__qualname__.endswith("._show_popup")
                assert queued.scope is None
                assert decoy_ran == []
                with tg._violation_ui_lock:
                    assert tg._violation_dialog_open is True

                scope.cancel()
                default_executor.process_queue()

                assert len(msgbox_calls) == 1
                with tg._violation_ui_lock:
                    assert tg._violation_dialog_open is False
    finally:
        tg.set_background_task(None)
        if token is not None:
            _current_send_cancellation.reset(token)
        default_executor._initialized = was_init
        default_executor._async_callback_service = was_async
        with tg._violation_ui_lock:
            tg._violation_dialog_open = False
        while True:
            try:
                default_executor._work_queue.get_nowait()
            except queue_mod.Empty:
                break
        for item in saved_work:
            default_executor._work_queue.put_nowait(item)
        with default_executor._pending_lock:
            current = [row for row in default_executor._pending_posts if row[0] is not decoy]
            default_executor._pending_posts = saved_pending + current
            default_executor._pending_lock.notify_all()


def test_proxy_setattr_only_target_is_local(monkeypatch):
    from tests.harness.strip_bundle import skip_if_release_build

    class Target:
        pass

    target = Target()
    proxy = tg._UnoThreadGuardProxy(target)

    with patch.object(tg, "assert_main_thread") as mock_assert:
        proxy._custom = 1
        mock_assert.assert_called_once_with("UNO set ._custom")
        assert target._custom == 1
        assert "_custom" not in proxy.__dict__

    other = Target()
    with patch.object(tg, "assert_main_thread") as mock_assert:
        proxy._target = other
        mock_assert.assert_not_called()
        assert proxy._target is other

    skip_if_release_build("release stub proxy does not assert on setattr")
    monkeypatch.setenv("WRITERAGENT_TESTING", "1")
    was = tg.GUARD_ON
    tg.GUARD_ON = True
    errors: list[BaseException] = []

    def _call() -> None:
        try:
            proxy._custom = 2
        except BaseException as exc:
            errors.append(exc)

    try:
        worker = threading.Thread(target=_call, name="bg-worker")
        worker.start()
        worker.join(timeout=2)
        assert not worker.is_alive()
        assert len(errors) == 1
        assert isinstance(errors[0], RuntimeError) and "UNO thread violation" in str(errors[0])
    finally:
        tg.GUARD_ON = was


def test_pyuno_callable_method_result_is_proxied(monkeypatch):
    class InnerUno:
        __module__ = "pyuno"
        value = 42

        def queryInterface(self, _type):
            return self

    class PyUNO_callable:
        def __init__(self, inner):
            self.inner = inner

        def __call__(self, *args, **kwargs):
            return self.inner

    class DocumentStandin:
        def __init__(self, inner):
            self.getText = PyUNO_callable(inner)

        def queryInterface(self, _type):
            return self

    inner = InnerUno()
    doc = DocumentStandin(inner)

    saved_guard = tg.GUARD_ON
    tg.GUARD_ON = True
    try:
        calls = []
        orig_assert = tg.assert_main_thread

        def _tracking_assert(what: str) -> None:
            calls.append(what)
            orig_assert(what)

        monkeypatch.setattr(tg, "assert_main_thread", _tracking_assert)
        monkeypatch.setattr(tg, "on_main_thread", lambda: True)

        prox = tg.guard_uno(doc)
        method = prox.getText
        assert isinstance(method, tg._UnoThreadGuardProxy)
        assert type(method._target).__name__ == "PyUNO_callable"

        res = method()
        assert isinstance(res, tg._UnoThreadGuardProxy)
        assert res._target is inner
        assert "UNO() call" in calls

        monkeypatch.setattr(tg, "on_main_thread", lambda: False)
        monkeypatch.setenv("WRITERAGENT_TESTING", "1")
        with pytest.raises(RuntimeError, match="UNO thread violation"):
            _ = res.value
    finally:
        tg.GUARD_ON = saved_guard



