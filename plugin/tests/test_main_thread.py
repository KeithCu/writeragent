import pytest
import queue
import threading

from plugin.framework.main_thread import (
    _WorkItem,
    execute_on_main_thread
)

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
