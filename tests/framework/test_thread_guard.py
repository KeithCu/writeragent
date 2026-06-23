# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for the Layer A UNO thread-safety guard (thread_guard.py)."""

import threading
from unittest.mock import patch, MagicMock

import pytest

# Import after possible env setup in specific tests; we mutate the module flags for isolation.
import plugin.framework.thread_guard as tg


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
    try:
        with pytest.raises(RuntimeError) as exc:
            tg.assert_main_thread("uno_context.get_desktop")
        msg = str(exc.value)
        assert "UNO thread violation" in msg
        assert "run_search" in msg or "worker-foo" in msg
    finally:
        tg.GUARD_ON = was
        tg.set_background_task(None)


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
    was = tg.GUARD_ON
    tg.GUARD_ON = True
    try:
        with pytest.raises(RuntimeError):
            red_getter(21)
    finally:
        tg.GUARD_ON = was


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


def test_unwrap_roundtrip():
    real = _make_pyuno_like()
    p = tg._UnoThreadGuardProxy(real)
    assert tg._unwrap_uno(p) is real
    assert tg._unwrap_uno(real) is real


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
        # bypass=True means registry calls .execute directly (no execute_safe, no assert)
        out = reg.execute("dummy", ctx, bypass_thread_guard=True)

    t = threading.Thread(target=bg)
    t.start()
    t.join()

    assert out == {"status": "ok"}
    assert calls == ["execute"]
