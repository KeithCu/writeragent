# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Layer B tests: thread-affine mocks + real cross-thread marshal in pytest."""

from __future__ import annotations

import threading
from unittest.mock import MagicMock, patch

import pytest

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
    """document_helpers entrypoints call assert_main_thread even on mocks."""
    from plugin.framework.errors import UnoObjectError

    monkeypatch.setattr(tg, "GUARD_ON", True)
    err: BaseException | None = None

    def worker():
        nonlocal err
        try:
            from plugin.doc import document_helpers

            document_helpers.get_document_type(MagicMock())
        except (RuntimeError, UnoObjectError) as e:
            err = e

    t = run_in_background(worker, name="run_get_doc_type", daemon=False)
    t.join(timeout=3.0)
    assert err is not None
    msg = str(err)
    if isinstance(err, UnoObjectError) and err.__cause__ is not None:
        msg = str(err.__cause__)
    assert "UNO thread violation" in msg


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
