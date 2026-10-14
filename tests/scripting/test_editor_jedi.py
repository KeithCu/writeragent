# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for the persistent Jedi completions engine in Monaco child process."""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock

from plugin.scripting.venv import editor_main as ej


def test_jedi_completions_mapping(monkeypatch):
    """Verify that Jedi completions are correctly mapped and adjusted for 0-indexed columns."""
    mock_jedi = MagicMock()
    monkeypatch.setattr(ej, "jedi", mock_jedi)

    mock_comp1 = MagicMock()
    mock_comp1.name = "my_func"
    mock_comp1.type = "function"
    mock_comp1.description = "def my_func(x)"
    mock_comp1.docstring.return_value = "This is a function docstring."

    mock_comp2 = MagicMock()
    mock_comp2.name = "MyClass"
    mock_comp2.type = "class"
    mock_comp2.description = "class MyClass"
    mock_comp2.docstring.return_value = "This is a class docstring."

    mock_script = MagicMock()
    mock_script.complete.return_value = [mock_comp1, mock_comp2]
    mock_jedi.Script.return_value = mock_script

    session = ej.JediSession()
    res = session.get_completions("def ", 1, 5)

    # Assert that jedi.Script was called with expected arguments and environment
    from plugin.scripting.venv.venv_sandbox import apply_auto_imports
    expected_code, lines_added = apply_auto_imports("def ")
    mock_jedi.Script.assert_called_once_with(expected_code, environment=session._env)
    mock_script.complete.assert_called_once_with(1 + lines_added, 4)  # Monaco 5 maps to Jedi 4

    # Assert correct structure of returned items
    assert len(res["items"]) == 2
    assert res["items"][0] == {
        "label": "my_func",
        "kind": "function",
        "insertText": "my_func",
        "detail": "def my_func(x)",
        "documentation": "This is a function docstring.",
    }
    assert res["items"][1] == {
        "label": "MyClass",
        "kind": "class",
        "insertText": "MyClass",
        "detail": "class MyClass",
        "documentation": "This is a class docstring.",
    }


def test_jedi_graceful_fallback(monkeypatch):
    """Verify that the engine handles missing jedi module without crashing."""
    monkeypatch.setattr(ej, "jedi", None)
    session = ej.JediSession()
    assert session.is_available() is False
    res = session.get_completions("def ", 1, 5)
    assert res == {"items": []}


def test_jedi_docstring_exception(monkeypatch):
    """Verify that exceptions in comp.docstring() do not cause completion failure."""
    mock_jedi = MagicMock()
    monkeypatch.setattr(ej, "jedi", mock_jedi)

    mock_comp = MagicMock()
    mock_comp.name = "broken_func"
    mock_comp.type = "function"
    mock_comp.description = "def broken_func()"
    mock_comp.docstring.side_effect = Exception("failed to read docstring")

    mock_script = MagicMock()
    mock_script.complete.return_value = [mock_comp]
    mock_jedi.Script.return_value = mock_script

    session = ej.JediSession()
    res = session.get_completions("def ", 1, 5)

    assert len(res["items"]) == 1
    assert res["items"][0] == {
        "label": "broken_func",
        "kind": "function",
        "insertText": "broken_func",
        "detail": "def broken_func()",
        "documentation": "",  # Falls back to empty string
    }


def test_jedi_complete_exception(monkeypatch):
    """Verify that general exceptions in completion query are handled gracefully."""
    mock_jedi = MagicMock()
    monkeypatch.setattr(ej, "jedi", mock_jedi)
    mock_jedi.Script.side_effect = Exception("General jedi error")

    session = ej.JediSession()
    res = session.get_completions("def ", 1, 5)

    assert res == {"items": []}


def test_api_init_does_not_build_a_jedi_environment(monkeypatch):
    mock_jedi = MagicMock()
    mock_jedi.create_environment.side_effect = AssertionError("should not be called")
    monkeypatch.setattr(ej, "jedi", mock_jedi)
    api = ej.MonacoEditorApi()
    assert api.is_jedi_available() is True
    mock_jedi.create_environment.assert_not_called()

    monkeypatch.setattr(ej, "jedi", None)
    assert api.is_jedi_available() is False
    assert api.get_completions("def ", 1, 5) == {"items": []}
    assert api._jedi_thread is None


def test_jedi_completions_caps_docstrings(monkeypatch):
    mock_jedi = MagicMock()
    monkeypatch.setattr(ej, "jedi", mock_jedi)

    comps = []
    for i in range(25):
        c = MagicMock()
        c.name = f"func_{i}"
        c.type = "function"
        c.description = f"def func_{i}()"
        c.docstring.return_value = f"doc_{i}"
        comps.append(c)

    mock_script = MagicMock()
    mock_script.complete.return_value = comps
    mock_jedi.Script.return_value = mock_script

    session = ej.JediSession()
    res = session.get_completions("x.", 1, 3)

    assert len(res["items"]) == 25
    # First 20 have docstrings, subsequent items do not
    assert res["items"][0]["documentation"] == "doc_0"
    assert res["items"][19]["documentation"] == "doc_19"
    assert res["items"][20]["documentation"] == ""
    assert comps[20].docstring.call_count == 0


def test_monaco_api_get_completions_drops_stale_request(monkeypatch):
    api = ej.MonacoEditorApi()
    mock_jedi_session = MagicMock()
    # Simulate a slow request that completes after generation advances
    def slow_completions(*args, **kwargs):
        api._completion_gen += 1  # newer request arrives
        return {"items": [{"label": "stale"}]}

    mock_jedi_session.get_completions.side_effect = slow_completions
    api._jedi = mock_jedi_session

    res = api.get_completions("x.", 1, 3)
    assert res == {"items": []}


def test_get_completions_returns_empty_while_a_call_is_in_flight():
    api = ej.MonacoEditorApi()
    mock_jedi = MagicMock()

    started_ev = threading.Event()
    release_ev = threading.Event()

    calls = 0

    def mock_get_completions(code, line, column):
        nonlocal calls
        calls += 1
        if calls == 1:
            started_ev.set()
            release_ev.wait(timeout=5.0)
            return {"items": [{"label": "stale"}]}
        return {"items": [{"label": "fresh"}]}

    mock_jedi.get_completions.side_effect = mock_get_completions
    api._jedi = mock_jedi

    first_result = {}

    def run_first():
        first_result["res"] = api.get_completions("def ", 1, 5)

    t1 = threading.Thread(target=run_first)
    try:
        t1.start()
        assert started_ev.wait(timeout=5.0)

        first_worker = api._jedi_thread
        assert first_worker is not None

        # Second get_completions on the test thread
        second_res = api.get_completions("def ", 1, 5)
        assert second_res == {"items": []}
        assert mock_jedi.get_completions.call_count == 1

        release_ev.set()
        t1.join(timeout=2.0)
        assert not t1.is_alive()
        first_worker.join(timeout=2.0)
        assert not first_worker.is_alive()
        assert first_result.get("res") == {"items": []}

        third_res = api.get_completions("def ", 1, 5)
        assert third_res == {"items": [{"label": "fresh"}]}
        assert mock_jedi.get_completions.call_count == 2
        assert api._jedi_thread is not None
        assert api._jedi_thread.daemon is True
    finally:
        release_ev.set()
        t1.join(timeout=2.0)
        if api._jedi_thread is not None and api._jedi_thread.is_alive():
            api._jedi_thread.join(timeout=2.0)


def test_get_completions_abandons_a_stuck_worker(monkeypatch):
    assert not hasattr(ej, "_jedi_pool")
    monkeypatch.setattr(ej, "_JEDI_CALL_TIMEOUT_SEC", 0.05)

    api = ej.MonacoEditorApi()
    started_ev = threading.Event()
    release_ev = threading.Event()

    blocking_mock = MagicMock()

    def block_call(code, line, column):
        started_ev.set()
        release_ev.wait(timeout=5.0)
        return {"items": [{"label": "blocked"}]}

    blocking_mock.get_completions.side_effect = block_call
    api._jedi = blocking_mock

    t1 = threading.Thread(target=lambda: api.get_completions("def ", 1, 5))
    try:
        t1.start()
        assert started_ev.wait(timeout=5.0)
        assert api._jedi_thread is not None
        assert api._jedi_thread.daemon is True

        time.sleep(0.08)

        new_mock = MagicMock()
        new_mock.get_completions.return_value = {"items": [{"label": "new"}]}
        factory_calls = []

        def factory():
            factory_calls.append(1)
            return new_mock

        monkeypatch.setattr(ej, "JediSession", factory)

        second_res = api.get_completions("def ", 1, 5)
        assert second_res == {"items": [{"label": "new"}]}
        assert len(factory_calls) == 1
        assert blocking_mock.get_completions.call_count == 1
    finally:
        release_ev.set()
        t1.join(timeout=2.0)

