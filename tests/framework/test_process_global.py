import sys
import threading
import time

import pytest

from plugin.framework.process_global import process_global


def _clear(key: str) -> None:
    sys.__dict__.pop(key, None)


def test_process_global_creates_once():
    name = "test_pg_creates_once"
    key = f"_writeragent_{name}"
    _clear(key)
    calls = 0

    def factory():
        nonlocal calls
        calls += 1
        return {"created": True}

    try:
        obj1 = process_global(name, factory)
        obj2 = process_global(name, factory)
        assert calls == 1
        assert obj1 == {"created": True}
        assert obj2 is obj1
        assert sys.__dict__[key] is obj1
    finally:
        _clear(key)


def test_process_global_existing_on_sys():
    name = "test_pg_existing"
    key = f"_writeragent_{name}"
    existing = object()
    setattr(sys, key, existing)
    calls = 0

    def factory():
        nonlocal calls
        calls += 1
        return object()

    try:
        assert process_global(name, factory) is existing
        assert calls == 0
    finally:
        _clear(key)


def test_process_global_factory_failure_does_not_persist():
    name = "test_pg_fail"
    key = f"_writeragent_{name}"
    _clear(key)

    def failing_factory():
        raise RuntimeError("boom")

    try:
        with pytest.raises(RuntimeError, match="boom"):
            process_global(name, failing_factory)
        assert key not in sys.__dict__
        assert process_global(name, lambda: "ok") == "ok"
    finally:
        _clear(key)


def test_process_global_concurrent_first_callers_run_factory_once():
    name = "test_pg_concurrent"
    key = f"_writeragent_{name}"
    _clear(key)
    calls = 0
    results = []

    def factory():
        nonlocal calls
        calls += 1
        time.sleep(0.01)
        return object()

    def worker():
        results.append(process_global(name, factory))

    threads = [threading.Thread(target=worker) for _ in range(10)]
    try:
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert calls == 1
        assert len(results) == 10
        assert all(r is results[0] for r in results)
    finally:
        _clear(key)
