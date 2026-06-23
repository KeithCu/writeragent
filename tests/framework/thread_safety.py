# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Layer B test helpers: thread-affine UNO mocks + synthetic main-thread pump.

Opt-in via the ``uno_thread_safety`` fixture in tests/framework/conftest.py.
See docs/uno-thread-safety-enforcement.md (Layer B).
"""

from __future__ import annotations

import threading
from typing import Any
from unittest.mock import MagicMock

from plugin.framework import queue_executor as qe
from plugin.framework import thread_guard as tg
from plugin.framework.worker_pool import run_in_background


def _affine_wrap_value(val: Any, main_thread: threading.Thread, label: str) -> Any:
    if val is None or isinstance(val, (bool, int, float, str, bytes)):
        return val
    if isinstance(val, ThreadAffineMock):
        return val
    if isinstance(val, MagicMock):
        return make_thread_affine_mock(val, main_thread=main_thread, name=label)
    return val


class ThreadAffineMock:
    """Wrap a mock so attribute access / calls only succeed on *main_thread*."""

    __slots__ = ("_target", "_main_thread", "_name")

    def __init__(self, target: Any, main_thread: threading.Thread, name: str = "uno") -> None:
        object.__setattr__(self, "_target", target)
        object.__setattr__(self, "_main_thread", main_thread)
        object.__setattr__(self, "_name", name)

    def _check(self, what: str) -> None:
        current = threading.current_thread()
        if current is self._main_thread:
            return
        task = tg.get_background_task_name() or current.name
        raise AssertionError(
            "UNO mock %r.%s touched from background task %r; marshal via execute_on_main_thread()."
            % (self._name, what, task)
        )

    def __getattr__(self, name: str) -> Any:
        self._check(name)
        val = getattr(self._target, name)
        return _affine_wrap_value(val, self._main_thread, f"{self._name}.{name}")

    def __setattr__(self, name: str, value: Any) -> None:
        if name.startswith("_"):
            object.__setattr__(self, name, value)
            return
        self._check(f"set .{name}")
        setattr(self._target, name, value)

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self._check("()")
        res = self._target(*args, **kwargs)
        return _affine_wrap_value(res, self._main_thread, f"{self._name}()")

    def __iter__(self):
        self._check("iter")
        for item in self._target:
            yield _affine_wrap_value(item, self._main_thread, f"{self._name}[]")

    def __getitem__(self, key: Any) -> Any:
        self._check("getitem")
        return _affine_wrap_value(self._target[key], self._main_thread, f"{self._name}[{key!r}]")

    def __repr__(self) -> str:
        return f"<ThreadAffineMock {self._name!r} for {self._target!r}>"


def make_thread_affine_mock(
    target: Any,
    *,
    main_thread: threading.Thread | None = None,
    name: str = "uno",
) -> ThreadAffineMock:
    """Stamp *target* so only *main_thread* (or designated main) may touch it."""
    if isinstance(target, ThreadAffineMock):
        return target
    if main_thread is None:
        main_thread = tg.get_designated_main_thread() or threading.main_thread()
    return ThreadAffineMock(target, main_thread, name)


class TestMainPump:
    """Dedicated thread that drains QueueExecutor work (mirrors production marshal)."""

    def __init__(self, executor: qe.QueueExecutor) -> None:
        self._executor = executor
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="test-main-pump", daemon=True)
        self._thread.start()
        self._ready.wait(timeout=2.0)
        self.main_thread = self._thread

    def _loop(self) -> None:
        self._ready.set()
        while not self._stop.is_set():
            self._wake.wait(timeout=0.05)
            self._wake.clear()
            while not self._stop.is_set():
                try:
                    self._executor.process_queue()
                except Exception:
                    break
                if self._executor._work_queue.empty():
                    break

    def poke(self, _executor: qe.QueueExecutor) -> None:
        self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        self._thread.join(timeout=2.0)


class UnoThreadSafetySession:
    """Active Layer B session: pump + marshal mode + designated main thread."""

    def __init__(self) -> None:
        self.pump = TestMainPump(qe.default_executor)
        tg.set_designated_main_thread(self.pump.main_thread)
        qe.set_force_marshal_mode(True)
        qe.set_test_poke_handler(self.pump.poke)

    def close(self) -> None:
        qe.set_force_marshal_mode(False)
        qe.set_test_poke_handler(None)
        tg.set_designated_main_thread(None)
        self.pump.stop()

    def make_mock(self, target: Any, *, name: str = "uno") -> ThreadAffineMock:
        return make_thread_affine_mock(target, main_thread=self.pump.main_thread, name=name)


def start_uno_thread_safety_session() -> UnoThreadSafetySession:
    return UnoThreadSafetySession()
