# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Layer B tests: thread-affine mocks + real cross-thread marshal in pytest."""

from __future__ import annotations

import threading
from unittest.mock import MagicMock


from plugin.framework import thread_guard as tg
from plugin.framework.queue_executor import execute_on_main_thread, post_to_main_thread
from plugin.framework.worker_pool import run_in_background
from tests.framework.thread_safety import make_thread_affine_mock, start_uno_thread_safety_session


def test_designated_main_thread_hook(monkeypatch):
    fake_main = MagicMock()
    fake_main.name = "fake-main"
    monkeypatch.setattr(tg, "_designated_main_thread", fake_main)
    monkeypatch.setattr(threading, "current_thread", lambda: fake_main)
    assert tg.on_main_thread() is True

    other = MagicMock()
    other.name = "other"
    monkeypatch.setattr(threading, "current_thread", lambda: other)
    assert tg.on_main_thread() is False
    monkeypatch.setattr(tg, "_designated_main_thread", None)


def test_thread_affine_mock_allows_designated_main_thread():
    session = start_uno_thread_safety_session()
    try:
        raw = MagicMock()
        raw.value = 7
        mock = session.make_mock(raw, name="doc")
        assert execute_on_main_thread(lambda: mock.value) == 7
    finally:
        session.close()


def test_thread_affine_mock_rejects_background_access():
    session = start_uno_thread_safety_session()
    try:
        mock = session.make_mock(MagicMock(), name="desktop")
        err: AssertionError | None = None

        def bg():
            nonlocal err
            try:
                _ = mock.getCurrentComponent
            except AssertionError as e:
                err = e

        t = threading.Thread(target=bg, name="worker-direct")
        t.start()
        t.join()
        assert err is not None
        assert "background task" in str(err)
        assert "execute_on_main_thread" in str(err)
    finally:
        session.close()


def test_marshal_via_execute_on_main_thread_allows_affine_access(uno_thread_safety):
    raw = MagicMock()
    raw.getText.return_value = "hello"
    doc = uno_thread_safety.make_mock(raw, name="doc")
    result: list[str] = []
    err: BaseException | None = None

    def worker():
        nonlocal err
        try:
            out = execute_on_main_thread(lambda: doc.getText())
            result.append(out)
        except BaseException as e:
            err = e

    t = run_in_background(worker, name="run_marshal_ok", daemon=False)
    t.join(timeout=3.0)
    assert err is None
    assert result == ["hello"]


def test_post_to_main_thread_allows_affine_access(uno_thread_safety):
    raw = MagicMock()
    doc = uno_thread_safety.make_mock(raw, name="doc")
    done = threading.Event()
    err: BaseException | None = None

    def worker():
        nonlocal err
        try:
            def touch():
                _ = doc.getText()
                done.set()

            post_to_main_thread(touch)
        except BaseException as e:
            err = e
            done.set()

    t = run_in_background(worker, name="run_post_ok", daemon=False)
    done.wait(timeout=3.0)
    t.join(timeout=3.0)
    assert err is None


def test_direct_affine_access_from_run_in_background_fails(uno_thread_safety):
    doc = uno_thread_safety.make_mock(MagicMock(), name="doc")
    err: AssertionError | None = None

    def worker():
        nonlocal err
        try:
            _ = doc.getText()
        except AssertionError as e:
            err = e

    t = run_in_background(worker, name="run_direct_bad", daemon=False)
    t.join(timeout=3.0)
    assert err is not None
    assert "run_direct_bad" in str(err)


def test_guarded_getter_from_background_fails_with_marshal_fixture(uno_thread_safety, monkeypatch):
    """doc_type entrypoints call assert_main_thread even on mocks."""
    from plugin.doc import doc_type
    from plugin.framework.errors import UnoObjectError

    # What was wrong (GHA 34423268523, Windows xdist gw2): the worker did
    # ``from plugin.doc import doc_type`` after ``run_in_background`` started.
    # ``join(timeout=3)`` returned with ``err`` still None; captured log had
    # only worker_pool "Starting task" — no completion, no violation. How:
    # first import of ``plugin.doc`` (package ``__init__`` → CommonModule) ran
    # on the dedicated worker while the Layer B pump was live, so the getter
    # never reached ``assert_main_thread`` before the timeout. Why this: load
    # the module on the test thread; the worker only calls the getter. Event +
    # liveness / unexpected-exception notes so a future miss names the cause
    # (alive thread vs wrong type vs GUARD_ON / designated-main).
    monkeypatch.setattr(tg, "GUARD_ON", True)
    done = threading.Event()
    err: BaseException | None = None
    notes: dict[str, object] = {}

    def worker() -> None:
        nonlocal err
        try:
            notes["guard_on"] = tg.GUARD_ON
            notes["on_main_thread"] = tg.on_main_thread()
            designated = tg.get_designated_main_thread()
            notes["designated_name"] = None if designated is None else designated.name
            notes["current_name"] = threading.current_thread().name
            notes["task_name"] = tg.get_background_task_name()
            doc_type.get_document_type(MagicMock())
            notes["returned"] = True
        except BaseException as exc:
            err = exc
            notes["exc_type"] = type(exc).__name__
        finally:
            done.set()

    t = run_in_background(worker, name="run_get_doc_type", daemon=False)
    finished = done.wait(timeout=3.0)
    t.join(timeout=1.0)
    state = (
        f"finished={finished} alive={t.is_alive()} notes={notes!r} "
        f"GUARD_ON={tg.GUARD_ON} designated={tg.get_designated_main_thread()!r}"
    )
    assert finished, f"worker did not finish; {state}"
    assert err is not None, f"guard did not raise; {state}"
    msg = str(err)
    if isinstance(err, UnoObjectError) and err.__cause__ is not None:
        msg = str(err.__cause__)
    assert "UNO thread violation" in msg, f"unexpected {type(err).__name__}: {err!r}; {state}"


def test_charts_process_events_regression_must_marshal(uno_thread_safety, monkeypatch):
    """Regression for commit 0cfc6891: _process_events must not call UNO getters off-worker.

    charts._process_events swallows exceptions, so we record assert_main_thread hits instead
    of expecting an error to escape the worker.
    """
    from plugin.calc import charts

    violations: list[str] = []

    def recording_assert(what: str) -> None:
        violations.append(what)
        raise RuntimeError(what)

    monkeypatch.setattr(tg, "assert_main_thread", recording_assert)
    monkeypatch.setattr("plugin.framework.uno_context.process_events_to_idle", lambda *a, **k: violations.append("process_events_to_idle"))

    t = run_in_background(lambda: charts._process_events(MagicMock()), name="chart_process_events", daemon=False)
    t.join(timeout=3.0)

    assert violations
    assert any("get_desktop" in v or "get_ctx" in v or "process_events" in v for v in violations)
    assert "process_events_to_idle" not in violations


def test_make_thread_affine_mock_standalone():
    main = threading.main_thread()
    raw = MagicMock()
    wrapped = make_thread_affine_mock(raw, main_thread=main, name="x")
    assert wrapped.foo._target is raw.foo


def test_yellow_context_refuses_execute_on_main_thread(uno_thread_safety):
    """Calling execute_on_main_thread from a worker in sync_host_dispatch must fail immediately (#402)."""
    err: BaseException | None = None

    def worker():
        nonlocal err
        try:
            with tg.sync_host_dispatch():
                assert tg.in_sync_host_dispatch() is True
                execute_on_main_thread(lambda: 42)
        except BaseException as e:
            err = e

    t = run_in_background(worker, name="yellow_bridge_worker", daemon=False)
    t.join(timeout=3.0)
    assert err is not None
    assert isinstance(err, RuntimeError)
    assert "deadlock hazard #402" in str(err) or "synchronous host dispatch" in str(err)


def test_yellow_context_allows_inline_when_on_main_thread():
    """On main thread, sync_host_dispatch allows inline execution without error."""
    with tg.sync_host_dispatch():
        assert tg.in_sync_host_dispatch() is True
        result = execute_on_main_thread(lambda: 42)
        assert result == 42
    assert tg.in_sync_host_dispatch() is False


def test_sync_host_dispatch_nesting_and_cleanup():
    """Verify sync_host_dispatch cleanly handles nesting and exceptions."""
    assert tg.in_sync_host_dispatch() is False
    with tg.sync_host_dispatch():
        assert tg.in_sync_host_dispatch() is True
        with tg.sync_host_dispatch():
            assert tg.in_sync_host_dispatch() is True
        assert tg.in_sync_host_dispatch() is True
    assert tg.in_sync_host_dispatch() is False

    try:
        with tg.sync_host_dispatch():
            assert tg.in_sync_host_dispatch() is True
            raise ValueError("simulated error")
    except ValueError:
        pass
    assert tg.in_sync_host_dispatch() is False


def test_notify_thread_violation_never_blocks(monkeypatch):
    """_notify_thread_violation must post asynchronously and never execute_on_main_thread (#402)."""
    from plugin.framework import queue_executor

    execute_called = False
    post_called = False

    def fake_execute(fn, *a, **k):
        nonlocal execute_called
        execute_called = True
        return fn(*a, **k)

    def fake_post(fn, *a, **k):
        nonlocal post_called
        post_called = True

    monkeypatch.setattr(queue_executor.default_executor, "_initialized", True)
    monkeypatch.setattr(queue_executor.default_executor, "_async_callback_service", MagicMock())
    monkeypatch.setattr(queue_executor, "execute_on_main_thread", fake_execute)
    monkeypatch.setattr(queue_executor, "post_to_main_thread", fake_post)
    monkeypatch.delenv("WRITERAGENT_TESTING", raising=False)
    # One popup per OS thread. An earlier test on this xdist worker (for
    # example get_runtime_uid off the designated main thread) already recorded
    # this tid, so the post was skipped and post_called stayed false.
    tid = threading.get_ident()
    with tg._violation_ui_lock:
        tg._violation_ui_threads.discard(tid)

    tg._notify_thread_violation("test violation from background worker")
    assert execute_called is False
    assert post_called is True

