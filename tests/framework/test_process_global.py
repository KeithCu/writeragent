import sys
import threading
import pytest
from plugin.framework.process_global import process_global


def test_process_global_creates_once():
    name = "test_pg_creates_once"
    key = f"_writeragent_{name}"
    if hasattr(sys, key):
        delattr(sys, key)

    calls = 0

    def factory():
        nonlocal calls
        calls += 1
        return {"created": True}

    try:
        obj1 = process_global(name, factory)
        assert calls == 1
        assert obj1 == {"created": True}

        obj2 = process_global(name, factory)
        assert calls == 1
        assert obj2 is obj1
    finally:
        if hasattr(sys, key):
            delattr(sys, key)


def test_process_global_prefixed_name_handling():
    name = "_writeragent_test_pg_prefixed"
    key = name
    if hasattr(sys, key):
        delattr(sys, key)

    try:
        obj = process_global(name, lambda: 42)
        assert obj == 42
        assert getattr(sys, key) == 42

        # Access with unprefixed name also returns same object
        unprefixed = "test_pg_prefixed"
        obj2 = process_global(unprefixed, lambda: 99)
        assert obj2 == 42
    finally:
        if hasattr(sys, key):
            delattr(sys, key)


def test_process_global_existing_on_sys():
    name = "test_pg_existing"
    key = f"_writeragent_{name}"
    existing = object()
    setattr(sys, key, existing)

    try:
        calls = 0

        def factory():
            nonlocal calls
            calls += 1
            return object()

        res = process_global(name, factory)
        assert res is existing
        assert calls == 0
    finally:
        if hasattr(sys, key):
            delattr(sys, key)


def test_process_global_factory_failure_does_not_persist():
    name = "test_pg_fail"
    key = f"_writeragent_{name}"
    if hasattr(sys, key):
        delattr(sys, key)

    def failing_factory():
        raise RuntimeError("boom")

    try:
        with pytest.raises(RuntimeError, match="boom"):
            process_global(name, failing_factory)

        assert key not in sys.__dict__

        # Subsequent call with succeeding factory works
        res = process_global(name, lambda: "ok")
        assert res == "ok"
    finally:
        if hasattr(sys, key):
            delattr(sys, key)


def test_process_global_concurrent_access():
    name = "test_pg_concurrent"
    key = f"_writeragent_{name}"
    if hasattr(sys, key):
        delattr(sys, key)

    results = []

    def worker():
        res = process_global(name, lambda: object())
        results.append(res)

    threads = [threading.Thread(target=worker) for _ in range(10)]
    try:
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(results) == 10
        first = results[0]
        assert all(r is first for r in results)
    finally:
        if hasattr(sys, key):
            delattr(sys, key)
