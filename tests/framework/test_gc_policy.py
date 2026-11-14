"""Unit tests for main-thread-only cyclic GC policy (no LibreOffice)."""

from __future__ import annotations

import gc
import logging
import threading
import time
from typing import Any

import pytest

from plugin.framework import gc_policy


def _logging_lock() -> threading.RLock:
    lock = getattr(logging, "_lock", None)
    if lock is not None:
        return lock
    raise RuntimeError("logging module has no _lock")


def _acquire_logging_lock(timeout: float) -> bool:
    return _logging_lock().acquire(timeout=timeout)


def _release_logging_lock() -> None:
    release = getattr(logging, "_releaseLock", None)
    if release is not None:
        release()
        return
    _logging_lock().release()


@pytest.fixture(autouse=True)
def _gc_policy_tests_no_ticker_thread(request, monkeypatch):
    """install() must not start a real endless gc-ticker thread in pytest."""
    if getattr(request.module, "__name__", "") != "tests.framework.test_gc_policy":
        yield
        return

    def _record_run_in_background(fn, *args, **kwargs):
        return None

    monkeypatch.setattr(
        "plugin.framework.worker_pool.run_in_background",
        _record_run_in_background,
    )
    yield


class FakeExecutor:
    def __init__(self) -> None:
        self.posted: list[tuple[Any, tuple[Any, ...], dict[str, Any]]] = []
        self._async = object()
        self.post_raises = False

    def has_async_callback(self) -> bool:
        return True

    def _get_async_callback(self) -> Any:
        return self._async

    def post(self, fn: Any, /, *args: Any, bound_scope: Any = None, **kwargs: Any) -> None:
        if self.post_raises:
            raise TimeoutError("post failed")
        self.posted.append((fn, args, kwargs))


class NoAsyncExecutor:
    def has_async_callback(self) -> bool:
        return False

    def _get_async_callback(self) -> None:
        return None


class Leaf:
    solar: threading.RLock | None = None
    del_count = 0
    del_idents: list[int] = []

    def __init__(self) -> None:
        self.ref = self

    def __del__(self) -> None:
        Leaf.del_count += 1
        Leaf.del_idents.append(threading.get_ident())
        solar = Leaf.solar
        if solar is not None:
            solar.acquire()
            solar.release()


def _worker_build_cycles(hold: threading.Event, done: threading.Event) -> None:
    log_lock = _logging_lock()
    gc.set_threshold(1)
    log_lock.acquire()
    try:
        hold.set()
        for _ in range(200):
            Leaf()
        time.sleep(0.3)
    finally:
        log_lock.release()
        done.set()


def test_deadlock_reproducer_without_policy_then_fixed_with_install(gc_policy_restored):
    solar = threading.RLock()
    Leaf.solar = solar
    Leaf.del_count = 0
    Leaf.del_idents.clear()
    gc.enable()

    hold = threading.Event()
    done = threading.Event()
    worker = threading.Thread(target=_worker_build_cycles, args=(hold, done), daemon=True)
    solar.acquire()
    try:
        worker.start()
        assert hold.wait(timeout=2.0), "worker never entered logging lock"
        assert not _acquire_logging_lock(timeout=2.0), "deadlock without policy: main blocked on logging lock"
    finally:
        solar.release()
    worker.join(timeout=3.0)
    assert done.wait(timeout=1.0)
    assert Leaf.del_count >= 1, "without policy GC should run __del__ on the worker"

    Leaf.del_count = 0
    Leaf.del_idents.clear()
    fake = FakeExecutor()
    assert gc_policy.install(fake) is True
    assert not gc.isenabled()

    hold.clear()
    done.clear()
    worker = threading.Thread(target=_worker_build_cycles, args=(hold, done), daemon=True)
    solar.acquire()
    try:
        worker.start()
        assert hold.wait(timeout=2.0)
        assert _acquire_logging_lock(timeout=2.0), "with policy main should acquire logging lock"
        _release_logging_lock()
    finally:
        solar.release()
    worker.join(timeout=3.0)
    assert done.wait(timeout=1.0)
    assert Leaf.del_count == 0, "worker must not collect while GC is disabled"

    gc.set_threshold(0, 10, 10)
    gc_policy._ticker_iteration(fake, now=time.monotonic())
    assert len(fake.posted) >= 1
    fn, args, kwargs = fake.posted[-1]
    assert fn is gc_policy._collect_on_main
    fn(*args, **kwargs)
    assert Leaf.del_count >= 1
    assert all(ident == gc_policy._main_ident for ident in Leaf.del_idents)


def test_install_disables_gc_appends_sentinel_idempotent(gc_policy_restored):
    fake = FakeExecutor()
    assert gc_policy.install(fake) is True
    assert not gc.isenabled()
    assert gc_policy._sentinel in gc.callbacks
    assert gc_policy.install(fake) is True
    assert gc.callbacks.count(gc_policy._sentinel) == 1


def test_install_returns_false_when_no_async_callback(gc_policy_restored):
    assert gc.isenabled()
    assert gc_policy.install(NoAsyncExecutor()) is False
    assert gc.isenabled()
    assert gc_policy._main_ident is None


def test_install_skipped_without_pythonloader(gc_policy_restored, monkeypatch):
    import sys

    monkeypatch.delitem(sys.modules, "pythonloader", raising=False)
    assert gc.isenabled()
    assert gc_policy.install(FakeExecutor()) is False
    assert gc.isenabled()
    assert gc_policy._main_ident is None


def test_ticker_iteration_posts_when_due_and_respects_repost(gc_policy_restored):
    gc_policy._main_ident = threading.get_ident()
    gc.disable()
    fake = FakeExecutor()
    now = 1000.0
    gc_policy._last_full = now
    gc_policy._posted_at = 0.0
    gc.set_threshold(10_000, 10, 10)
    gc.collect()
    gc_policy._ticker_iteration(fake, now=now + 1.0)
    assert len(fake.posted) == 0

    gc.set_threshold(0, 10, 10)
    gc_policy._ticker_iteration(fake, now=now + 2.0)
    assert len(fake.posted) == 1
    gc_policy._ticker_iteration(fake, now=now + 2.1)
    assert len(fake.posted) == 1

    fake.post_raises = True
    gc_policy._ticker_iteration(fake, now=now + 10.0)
    assert gc_policy._posted_at == 0.0

    fake.post_raises = False
    gc_policy._last_full = now + 10.0
    gc_policy._ticker_iteration(fake, now=now + gc_policy._FULL_EVERY_SEC + 20.0)
    assert len(fake.posted) >= 2


def test_collect_on_main_re_disables_and_clears_sentinel(gc_policy_restored):
    gc_policy._main_ident = threading.get_ident()
    gc.disable()
    if gc_policy._sentinel not in gc.callbacks:
        gc.callbacks.append(gc_policy._sentinel)
    seen: list[int] = []

    def worker_collect() -> None:
        gc.enable()
        gc.collect()
        seen.append(threading.get_ident())

    worker = threading.Thread(target=worker_collect, daemon=True)
    worker.start()
    worker.join(timeout=2.0)
    assert seen and seen[0] != gc_policy._main_ident
    assert gc_policy._off_main_count

    gc_policy._collect_on_main()
    assert not gc.isenabled()
    assert not gc_policy._off_main_count
    assert not gc_policy._off_main_samples


def test_collect_on_main_off_main_refused(gc_policy_restored):
    gc_policy._main_ident = threading.get_ident()
    gc.disable()
    gc_policy._posted_at = 123.0
    gc_policy._refused = 0
    wrong_ident: list[int] = []

    def off_main_collect() -> None:
        wrong_ident.append(threading.get_ident())
        gc_policy._collect_on_main()

    worker = threading.Thread(target=off_main_collect, daemon=True)
    worker.start()
    worker.join(timeout=2.0)
    assert wrong_ident and wrong_ident[0] != gc_policy._main_ident
    assert gc_policy._posted_at == 0.0
    assert gc_policy._refused == 1

    gc_policy._collect_on_main()
    assert gc_policy._refused == 0


def test_sentinel_records_worker_gc_collect_stack(gc_policy_restored):
    gc_policy._main_ident = threading.get_ident()
    gc.disable()
    if gc_policy._sentinel not in gc.callbacks:
        gc.callbacks.append(gc_policy._sentinel)
    gc_policy._off_main_count = 0
    gc_policy._off_main_samples.clear()

    def worker_explicit_collect() -> None:
        gc.collect()

    worker = threading.Thread(target=worker_explicit_collect, daemon=True)
    worker.start()
    worker.join(timeout=2.0)
    assert gc_policy._off_main_count == 1
    assert len(gc_policy._off_main_samples) == 1
    ident, generation, enabled, stack = gc_policy._off_main_samples[0]
    assert ident != gc_policy._main_ident
    assert enabled is False
    assert any(frame[2] == "worker_explicit_collect" for frame in stack)


def test_ticker_iteration_re_disables_gc_before_post(gc_policy_restored):
    gc_policy._main_ident = threading.get_ident()
    gc.enable()
    gc_policy._reenabled = False
    fake = FakeExecutor()
    gc.set_threshold(0, 10, 10)
    gc_policy._ticker_iteration(fake, now=1000.0)
    assert not gc.isenabled()
    assert gc_policy._reenabled
    assert len(fake.posted) == 1
    fn, args, kwargs = fake.posted[0]
    fn(*args, **kwargs)
    assert not gc_policy._reenabled


def test_ticker_skips_disable_while_gc_suspended(gc_policy_restored):
    from plugin.framework.blocking_wait import automatic_gc_suspended

    gc_policy._main_ident = threading.get_ident()
    gc.enable()
    gc_policy._reenabled = False
    fake = FakeExecutor()
    gc.set_threshold(0, 10, 10)
    with automatic_gc_suspended():
        gc.enable()
        gc_policy._ticker_iteration(fake, now=1000.0)
    assert gc.isenabled()
    assert not gc_policy._reenabled
